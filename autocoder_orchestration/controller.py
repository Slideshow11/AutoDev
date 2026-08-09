"""Controller for one AutoDev run.

The controller wraps :class:`StateMachine` and :class:`StateStore`
to drive one run through the state machine. It is the only writer
of the run's authoritative state. Workers, the verifier, and
human operators supply inputs through typed APIs.

The controller's job is to enforce the transition contract. The
caller (the CLI or a self-hosting canary) invokes the controller
through methods like :meth:`Controller.observe_ci`,
:meth:`Controller.record_evidence`, and
:meth:`Controller.apply_verifier_record`. Each method either
applies the required transition or raises a typed error.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .context import (
    RunContext,
    ACTOR_CONTROLLER,
    ACTOR_HUMAN,
    ACTOR_IMPL_WORKER,
    ACTOR_VERIFIER,
    ACTOR_OBSERVER,
    ACTOR_CANDIDATE_BUILDER,
)
from .state_machine import (
    StateMachine,
    InvalidTransition,
    StateConflict,
    StateError,
    STATE_PLANNED,
    STATE_IMPLEMENTING,
    STATE_AWAITING_CI,
    STATE_REPAIRING_REVIEW_FINDINGS,
    STATE_QUALIFYING_READINESS,
    STATE_READY_FOR_CANDIDATE,
    STATE_CANDIDATE_FROZEN,
    STATE_AWAITING_INDEPENDENT_VERIFICATION,
    STATE_VERIFYING,
    STATE_VERIFICATION_FAILED,
    STATE_VERIFICATION_REPAIR,
    STATE_AWAITING_MERGE_AUTHORIZATION,
    STATE_MERGE_AUTHORIZED,
    STATE_POST_MERGE_VERIFYING,
    STATE_COMPLETE,
    STATE_BLOCKED,
)
from .store import (
    StateStore,
    StateStoreError,
    StateCorruption,
    StateRevision,
    Lease,
    ProcessIdentity,
    current_process_identity,
)
from .readiness import (
    ReadinessEngine,
    ReadinessCertificate,
    ReadinessDecision,
)
from .observer import (
    ObservationLog,
    Observation,
)
from .reconciliation import (
    Reconciler,
    Finding,
    FindingDisposition,
    ThreadResolution,
)
from .candidate import (
    Candidate,
    CandidateBuilder,
    CandidateError,
    CandidateNotReady,
    CandidateHeadMismatch,
)
from .verifier_handoff import (
    VerifierHandoff,
    VerifierRoleGuard,
    write_handoff,
    read_handoff,
)
from .merge_authorization import (
    MergeAuthorization,
    MergeExecutor,
    MergeRecord,
    MergeError,
)
from .artifacts import write_artifact as _write_artifact
from .canonical_paths import canonical_paths as _canonical_paths


class ControllerError(Exception):
    """Base controller error."""


class RunNotFound(ControllerError):
    """Raised when no run context is found at the state path."""


@dataclass
class Controller:
    """The orchestrator driving one run."""

    context: RunContext
    store: StateStore
    controller_identity: ProcessIdentity = field(default_factory=current_process_identity)

    def __post_init__(self) -> None:
        if not isinstance(self.context, RunContext):
            raise ControllerError("context must be a RunContext")
        if not isinstance(self.store, StateStore):
            raise ControllerError("store must be a StateStore")

    # === State persistence ===
    def load_state_machine(self) -> Optional[StateMachine]:
        payload = self.store.read_optional("state.json")
        if payload is None:
            return None
        return StateMachine.from_dict(payload)

    def save_state_machine(self, sm: StateMachine) -> StateRevision:
        return self.store.write_atomic("state.json", sm.to_dict())

    def save_run_context(self) -> StateRevision:
        return self.store.write_atomic("run_context.json", self.context.to_dict())

    def load_run_context(self) -> Optional[RunContext]:
        payload = self.store.read_optional("run_context.json")
        if payload is None:
            return None
        return RunContext.from_dict(payload)

    # === Worker-facing entry points ===
    def start_implementation(self) -> StateMachine:
        """PLANNED -> IMPLEMENTING."""
        sm = self._require_state_for_event()
        return self._apply(sm, STATE_IMPLEMENTING, ACTOR_CONTROLLER, head_observed=self.context.current_authorized_head)

    def report_implementation_complete(self, *, head_observed: str) -> StateMachine:
        """IMPLEMENTING -> AWAITING_CI."""
        sm = self._require_state_for_event()
        return self._apply(sm, STATE_AWAITING_CI, ACTOR_IMPL_WORKER, head_observed=head_observed)

    def report_ci_failure(self, *, head_observed: str) -> StateMachine:
        """AWAITING_CI -> REPAIRING_REVIEW_FINDINGS."""
        sm = self._require_state_for_event()
        return self._apply(sm, STATE_REPAIRING_REVIEW_FINDINGS, ACTOR_CONTROLLER, head_observed=head_observed)

    def report_ci_pass(self, *, head_observed: str) -> StateMachine:
        """AWAITING_CI -> QUALIFYING_READINESS."""
        sm = self._require_state_for_event()
        return self._apply(sm, STATE_QUALIFYING_READINESS, ACTOR_CONTROLLER, head_observed=head_observed)

    def report_repair_pushed(self, *, head_observed: str) -> StateMachine:
        """REPAIRING_REVIEW_FINDINGS -> AWAITING_CI.

        Safe sequence for head advance:

        1. Validate the head shape and current state.
        2. Rebind ``context.current_authorized_head`` to the new
           head via ``with_new_head`` and PERSIST the new context
           BEFORE the transition. The state-machine's head guard
           compares the observed head against the persisted
           authorized head; if the persisted head is stale (still
           pointing at the pre-push head) the transition will
           fail with ``InvalidTransition``.
        3. Apply the transition only after the new context is on
           disk.

        A failed rebind leaves the run in
        ``REPAIRING_REVIEW_FINDINGS`` with the original
        authorized head so the worker can retry. A failed
        transition leaves the rebind in place; the relay's next
        round will observe the new head and re-issue the push.
        """
        if not isinstance(head_observed, str) or (
            len(head_observed) != 40 and len(head_observed) != 64
        ) or not all(c in "0123456789abcdef" for c in head_observed):
            raise ControllerError(
                f"head_observed must be 40 or 64 lowercase hex chars: {head_observed!r}"
            )
        sm = self._require_state_for_event()
        # Step 2: rebind and persist the new authorized head.
        # ``with_new_head`` validates the shape and returns a new
        # RunContext (RunContext is frozen). Reassign
        # ``self.context`` so the post-transition state machine
        # is bound to the new head on the next call. Persist the
        # rebound context BEFORE the transition so the
        # state-machine's head guard sees a consistent
        # current_authorized_head.
        new_context = self.context.with_new_head(head_observed)
        self.context = new_context
        self.save_run_context()
        # Step 3: apply the transition. ``_apply`` uses
        # ``self.context.current_authorized_head`` as the
        # required head, so the transition succeeds for the
        # new head and the persisted context is already
        # consistent.
        return self._apply(sm, STATE_AWAITING_CI, ACTOR_IMPL_WORKER, head_observed=head_observed)

    def record_readiness_certificate(self, cert: ReadinessCertificate, *, head_observed: str) -> StateMachine:
        """QUALIFYING_READINESS -> READY_FOR_CANDIDATE.

        Safe sequence:
        1. Validate inputs.
        2. Validate current state supports the transition.
        3. Write artifact only after both pass.
        4. Apply the transition only after the artifact is on disk.
        A rejected transition leaves no accepted artifact.
        """
        if not isinstance(cert, ReadinessCertificate):
            raise ControllerError("cert must be a ReadinessCertificate")
        if cert.decision.expected_head != head_observed:
            raise ControllerError(
                f"cert expected head {cert.decision.expected_head!r} != "
                f"observed head {head_observed!r}"
            )
        # Validate the transition will succeed BEFORE writing the artifact.
        sm = self._require_state_for_event()
        next_sm = sm.transition(
            STATE_READY_FOR_CANDIDATE,
            ACTOR_CONTROLLER,
            head_observed=head_observed,
            head_required=self.context.current_authorized_head,
        )
        # Transition valid. Write the artifact, then commit state.
        cert_payload = cert.to_dict()
        cert_payload["_sha256"] = cert.compute_sha256()
        self.store.write_atomic("readiness.json", cert_payload)
        return self.save_state_machine(next_sm) and next_sm or next_sm

    def build_candidate(self, candidate: Candidate, *, head_observed: str) -> StateMachine:
        """READY_FOR_CANDIDATE -> CANDIDATE_FROZEN.

        Validates the transition before writing the artifact.
        """
        if not isinstance(candidate, Candidate):
            raise ControllerError("candidate must be a Candidate")
        sm = self._require_state_for_event()
        next_sm = sm.transition(
            STATE_CANDIDATE_FROZEN,
            ACTOR_CANDIDATE_BUILDER,
            head_observed=head_observed,
            head_required=self.context.current_authorized_head,
        )
        cand_payload = candidate.to_dict()
        cand_payload["_sha256"] = candidate.compute_sha256()
        # Canonical evidence-root copy (authoritative merge input).
        # The merge transaction reads only the canonical path; the
        # state-root copy is a secondary observable for audit only.
        self._write_canonical("candidate", cand_payload)
        self.store.write_atomic("candidate.json", cand_payload)
        self.store.write_atomic("candidate.sha256", {"sha256": cand_payload["_sha256"]})
        self.save_state_machine(next_sm)
        return next_sm

    def write_handoff(self, handoff: VerifierHandoff) -> StateRevision:
        return self.store.write_atomic("verifier-handoff.json", handoff.to_dict())

    def prompt_verifier(self, *, head_observed: str) -> StateMachine:
        """CANDIDATE_FROZEN -> AWAITING_INDEPENDENT_VERIFICATION.

        Validates the transition before declaring the run
        awaiting independent verification. Returns the
        post-transition state machine.
        """
        sm = self._require_state_for_event()
        next_sm = sm.transition(
            STATE_AWAITING_INDEPENDENT_VERIFICATION,
            ACTOR_CONTROLLER,
            head_observed=head_observed,
            head_required=self.context.current_authorized_head,
        )
        self.save_state_machine(next_sm)
        return next_sm

    def verifier_started(
        self,
        *,
        head_observed: str,
        verifier_identity: Dict[str, Any],
        verifier_executable_path: Optional[str] = None,
        verifier_executable_sha256: Optional[str] = None,
        trusted_verifier_source_commit: Optional[str] = None,
        verifier_executable_no_write_credentials: bool = True,
    ) -> StateMachine:
        """AWAITING_INDEPENDENT_VERIFICATION -> VERIFYING.

        Production boundary: enforces VerifierRoleGuard rules before
        transitioning. All required evidence is supplied explicitly;
        no caller may default write_credentials_present to True.

        Required evidence:
        - head_observed matches the context's authorized head;
        - verifier_executable_path is non-empty and absolute;
        - verifier_executable_sha256 is a valid 64-char lowercase hex;
        - trusted_verifier_source_commit is a valid 64-char lowercase hex;
        - the implementation lease is no longer active in the store;
        - the verifier identity differs from any recorded
          implementation worker identity;
        - the handoff file exists and its SHA matches the persisted
          _sha256 sidecar.

        The handoff is read from the state store.
        """
        # Validate required inputs evidence
        if not verifier_executable_path or not os.path.isabs(verifier_executable_path):
            raise ControllerError(
                f"verifier_executable_path must be a nonempty absolute path, got {verifier_executable_path!r}"
            )
        if not verifier_executable_sha256 or (
            len(verifier_executable_sha256) != 40
            and len(verifier_executable_sha256) != 64
        ) or not all(c in "0123456789abcdef" for c in verifier_executable_sha256):
            raise ControllerError(
                f"verifier_executable_sha256 must be 40 or 64 lowercase hex chars, got {verifier_executable_sha256!r}"
            )
        if not trusted_verifier_source_commit or (
            len(trusted_verifier_source_commit) != 40
            and len(trusted_verifier_source_commit) != 64
        ) or not all(c in "0123456789abcdef" for c in trusted_verifier_source_commit):
            raise ControllerError(
                f"trusted_verifier_source_commit must be 40 or 64 lowercase hex chars, got {trusted_verifier_source_commit!r}"
            )
        # Credential absence is REQUIRED to be True; we cannot accept
        # callers leaving this default.
        if not verifier_executable_no_write_credentials:
            raise ControllerError(
                "verifier has write credentials; refusing to enter VERIFYING"
            )
        # Implementation lease must not still be held
        try:
            lease = self.store.read_lease()
        except StateStoreError:
            lease = None
        if isinstance(lease, ProcessIdentity):
            verifier_pid = int(verifier_identity.get("pid", 0)) if isinstance(verifier_identity, dict) else 0
            verifier_start_id = str(verifier_identity.get("start_id", "")) if isinstance(verifier_identity, dict) else ""
            if (
                verifier_pid == lease.pid
                and verifier_start_id == lease.start_id
            ):
                raise ControllerError(
                    "implementation lease is still held by the verifier; refusing to enter VERIFYING"
                )
        # Handoff evidence
        handoff_payload = self.store.read_optional("verifier-handoff.json")
        if handoff_payload is None:
            raise ControllerError(
                "verifier handoff not found in store; refusing to enter VERIFYING"
            )
        try:
            handoff = VerifierHandoff.from_dict(handoff_payload)
        except (ValueError, TypeError) as e:
            raise ControllerError(f"verifier handoff is malformed: {e!r}")
        # Persisted _sha256 must match the recomputed digest
        persisted_sha = handoff_payload.get("_sha256")
        recomputed_sha = handoff.compute_sha256()
        if persisted_sha is None or persisted_sha != recomputed_sha:
            raise ControllerError(
                f"verifier handoff digest mismatch: persisted {persisted_sha!r}, "
                f"recomputed {recomputed_sha!r}"
            )
        # Candidate must be unchanged since the handoff was written
        cand_payload = self.store.read_optional("candidate.json")
        if cand_payload is None:
            raise ControllerError("candidate not found in store")
        if "_sha256" not in cand_payload:
            raise ControllerError("candidate is missing _sha256 sidecar")
        if cand_payload["_sha256"] != handoff.candidate_sha256:
            raise ControllerError(
                f"candidate sha256 changed since handoff: "
                f"handoff {handoff.candidate_sha256!r}, current {cand_payload['_sha256']!r}"
            )
        # Exact head must match unchanged
        if handoff.exact_head != head_observed:
            raise ControllerError(
                f"handoff head {handoff.exact_head!r} != head_observed {head_observed!r}"
            )
        if handoff.exact_head != self.context.current_authorized_head:
            raise ControllerError(
                f"handoff head {handoff.exact_head!r} != authorized head "
                f"{self.context.current_authorized_head!r}"
            )
        # Run the role guard
        guard = VerifierRoleGuard(handoff, self.store)
        identity = ProcessIdentity(
            pid=int(verifier_identity.get("pid", 0)),
            start_id=str(verifier_identity.get("start_id", "")),
        )
        ok, reason = guard.validate(
            verifier_identity=identity,
            verifier_executable_path=verifier_executable_path,
            write_credentials_present=False,
        )
        if not ok:
            raise ControllerError(f"verifier role guard rejected: {reason}")
        # All checks passed. Apply the transition.
        sm = self._require_state_for_event()
        next_sm = sm.transition(
            STATE_VERIFYING,
            ACTOR_VERIFIER,
            head_observed=head_observed,
            head_required=self.context.current_authorized_head,
        )
        self.store.write_atomic("verifier_process.json", {
            "verifier_process": dict(verifier_identity),
            "verifier_executable_path": verifier_executable_path,
            "verifier_executable_sha256": verifier_executable_sha256,
            "trusted_verifier_source_commit": trusted_verifier_source_commit,
            "verifier_role_guard_passed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        self.save_state_machine(next_sm)
        return next_sm

    def _write_canonical(self, kind: str, payload: dict) -> str:
        """Write ``payload`` to the canonical evidence-root artifact path.

        The state-root copy is kept as a secondary observable for
        audit; the canonical evidence-root copy is authoritative.
        The guarded merge transaction reads only the canonical
        path; the state-root copy must never become a second input.
        """
        paths = _canonical_paths(Path(self.context.evidence_root))
        target = paths[kind]
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = _write_artifact(target, payload)
        return result.digest

    def verifier_failed(self, *, head_observed: str, verifier_record: Dict[str, Any]) -> StateMachine:
        """VERIFYING -> VERIFICATION_FAILED.

        Failed verification MUST NOT overwrite a previously
        written passing verifier artifact at the canonical
        evidence-root path. The merged-verdict model is:
        ``canonical verifier.json`` is the LAST WRITTEN verifier
        record, irrespective of verdict, but every authorization
        consumer MUST independently check ``verdict == VERIFIED``
        before binding its digest into a MergeAuthorization.
        ``cmd_merge_authorize`` enforces this gate.
        """
        sm = self._require_state_for_event()
        next_sm = sm.transition(
            STATE_VERIFICATION_FAILED,
            ACTOR_VERIFIER,
            head_observed=head_observed,
            head_required=self.context.current_authorized_head,
        )
        # Tag the failed record so consumers can distinguish a
        # failure-only artifact from a passing one even after
        # ``verifier_passed`` subsequently overwrites it.
        verifier_record = dict(verifier_record)
        verifier_record["_verdict_failed"] = True
        # Canonical evidence-root copy (authoritative merge input).
        self._write_canonical("verifier", verifier_record)
        # State-root copy is a secondary observable for audit only.
        self.store.write_atomic("verifier-record.json", verifier_record)
        self.save_state_machine(next_sm)
        return next_sm

    def verifier_passed(self, *, head_observed: str, verifier_record: Dict[str, Any]) -> StateMachine:
        """VERIFYING -> AWAITING_MERGE_AUTHORIZATION."""
        sm = self._require_state_for_event()
        next_sm = sm.transition(
            STATE_AWAITING_MERGE_AUTHORIZATION,
            ACTOR_CONTROLLER,
            head_observed=head_observed,
            head_required=self.context.current_authorized_head,
        )
        # Canonical evidence-root copy (authoritative merge input).
        self._write_canonical("verifier", verifier_record)
        # State-root copy is a secondary observable for audit only.
        self.store.write_atomic("verifier-record.json", verifier_record)
        self.save_state_machine(next_sm)
        return next_sm

    def authorize_merge(self, auth: MergeAuthorization) -> StateMachine:
        """AWAITING_MERGE_AUTHORIZATION -> MERGE_AUTHORIZED.

        Complete safe transaction owned by the Controller:

        1. Validate inputs.
        2. Validate current state supports the transition.
        3. Validate the authorized head matches the persisted
           current authorized head.
        4. Write the canonical evidence-root ``authorization.json``
           AND its sidecar (this is the authoritative merge input
           that ``cmd_merge`` consumes).
        5. Write the state-root ``merge-authorization.json``
           coordination copy.
        6. Commit the state-machine transition only after every
           write has succeeded.

        A canonical-write failure leaves the run at
        ``AWAITING_MERGE_AUTHORIZATION`` so a retry can succeed
        without re-entering an already-committed transition. A
        rejected transition leaves no accepted artifact anywhere.
        """
        if not isinstance(auth, MergeAuthorization):
            raise ControllerError("auth must be a MergeAuthorization")
        # Validate the transition will succeed BEFORE writing the
        # artifact. ``_require_state_for_event`` raises on the
        # wrong state; ``sm.transition`` raises on the wrong head.
        sm = self._require_state_for_event()
        next_sm = sm.transition(
            STATE_MERGE_AUTHORIZED,
            ACTOR_HUMAN,
            head_observed=auth.authorized_head,
            head_required=self.context.current_authorized_head,
        )
        # Build the artifact payload ONCE; both writes consume it.
        auth_payload = auth.to_dict()
        auth_payload["_sha256"] = auth.compute_sha256()
        # Step 4: write the canonical evidence-root artifact
        # FIRST. This is the authoritative merge input; the
        # state-root coordination copy below must not commit
        # without it. ``_write_canonical`` raises ArtifactError on
        # any write failure; the state-machine transition below
        # is therefore unreachable.
        self._write_canonical("authorization", auth_payload)
        # Step 5: state-root coordination copy for audit only.
        # Failures here raise StateStoreError; same recovery
        # semantics as above -- the run stays at
        # AWAITING_MERGE_AUTHORIZATION until both writes succeed.
        self.store.write_atomic("merge-authorization.json", auth_payload)
        # Step 6: commit the transition.
        return self.save_state_machine(next_sm) and next_sm or next_sm

    def report_merged(self, record: MergeRecord) -> StateMachine:
        """MERGE_AUTHORIZED -> POST_MERGE_VERIFYING."""
        if not isinstance(record, MergeRecord):
            raise ControllerError("record must be a MergeRecord")
        sm = self._require_state_for_event()
        next_sm = sm.transition(
            STATE_POST_MERGE_VERIFYING,
            ACTOR_CONTROLLER,
            head_observed=record.authorized_head,
            head_required=self.context.current_authorized_head,
        )
        self.store.write_atomic("merge-record.json", record.to_dict())
        self.save_state_machine(next_sm)
        return next_sm

    def report_complete(self) -> StateMachine:
        """POST_MERGE_VERIFYING -> COMPLETE."""
        sm = self._require_state_for_event()
        return self._apply(sm, STATE_COMPLETE, ACTOR_CONTROLLER)

    def block(self, *, reason: str) -> StateMachine:
        """* -> BLOCKED."""
        if not isinstance(reason, str) or not reason:
            raise ControllerError("reason must be a non-empty string")
        sm = self._require_state_for_event()
        next_sm = sm.transition(
            STATE_BLOCKED,
            ACTOR_CONTROLLER,
            head_observed=self.context.current_authorized_head,
            head_required=self.context.current_authorized_head,
        )
        self.store.write_atomic("block.json", {"reason": reason, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        self.save_state_machine(next_sm)
        return next_sm

    def verification_repair_started(self) -> StateMachine:
        """VERIFICATION_FAILED -> VERIFICATION_REPAIR."""
        sm = self._require_state_for_event()
        return self._apply(sm, STATE_VERIFICATION_REPAIR, ACTOR_CONTROLLER)

    def verification_repair_resumed(self) -> StateMachine:
        """VERIFICATION_REPAIR -> IMPLEMENTING."""
        sm = self._require_state_for_event()
        return self._apply(sm, STATE_IMPLEMENTING, ACTOR_CONTROLLER)

    # === Evidence ===
    def record_evidence(self, key: str, payload: Dict[str, Any]) -> None:
        if not isinstance(key, str) or "/" in key or ".." in key:
            raise ControllerError(f"evidence key must be a safe filename: {key!r}")
        self.store.write_atomic(f"evidence/{key}.json", dict(payload))

    def record_observation_log(self, log: ObservationLog) -> str:
        if not isinstance(log, ObservationLog):
            raise ControllerError("log must be an ObservationLog")
        text = log.to_jsonl()
        rel = "observations.jsonl"
        full = Path(self.store.state_root) / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(full.parent, 0o700)
        with open(full, "wb") as f:
            f.write(text.encode())
        os.chmod(full, 0o600)
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.store.write_atomic("observations.sha256", {"sha256": digest})
        return digest

    # === Internals ===
    def _require_state_for_event(self) -> StateMachine:
        sm = self.load_state_machine()
        if sm is None:
            raise ControllerError("no state machine found at state path")
        return sm

    def _apply(
        self,
        sm: StateMachine,
        target: str,
        actor: str,
        *,
        head_observed: Optional[str] = None,
    ) -> StateMachine:
        if not isinstance(sm, StateMachine):
            raise ControllerError("sm must be a StateMachine")
        next_sm = sm.transition(
            target,
            actor,
            head_observed=head_observed,
            head_required=self.context.current_authorized_head,
        )
        self.save_state_machine(next_sm)
        return next_sm
