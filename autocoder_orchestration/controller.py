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
        """REPAIRING_REVIEW_FINDINGS -> AWAITING_CI."""
        sm = self._require_state_for_event()
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

    def verifier_started(self, *, head_observed: str, verifier_identity: Dict[str, Any]) -> StateMachine:
        """AWAITING_INDEPENDENT_VERIFICATION -> VERIFYING.

        Validates the transition before writing the artifact.
        """
        sm = self._require_state_for_event()
        next_sm = sm.transition(
            STATE_VERIFYING,
            ACTOR_VERIFIER,
            head_observed=head_observed,
            head_required=self.context.current_authorized_head,
        )
        self.store.write_atomic("verifier_process.json", {"verifier_process": dict(verifier_identity)})
        self.save_state_machine(next_sm)
        return next_sm

    def verifier_failed(self, *, head_observed: str, verifier_record: Dict[str, Any]) -> StateMachine:
        """VERIFYING -> VERIFICATION_FAILED."""
        sm = self._require_state_for_event()
        next_sm = sm.transition(
            STATE_VERIFICATION_FAILED,
            ACTOR_VERIFIER,
            head_observed=head_observed,
            head_required=self.context.current_authorized_head,
        )
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
        self.store.write_atomic("verifier-record.json", verifier_record)
        self.save_state_machine(next_sm)
        return next_sm

    def authorize_merge(self, auth: MergeAuthorization) -> StateMachine:
        """AWAITING_MERGE_AUTHORIZATION -> MERGE_AUTHORIZED.

        Validates the transition before writing the artifact.
        Caller-supplied merge authorization is validated by the
        caller; the controller stores the authorization and applies
        the transition.
        """
        if not isinstance(auth, MergeAuthorization):
            raise ControllerError("auth must be a MergeAuthorization")
        sm = self._require_state_for_event()
        next_sm = sm.transition(
            STATE_MERGE_AUTHORIZED,
            ACTOR_HUMAN,
            head_observed=auth.authorized_head,
            head_required=self.context.current_authorized_head,
        )
        auth_payload = auth.to_dict()
        auth_payload["_sha256"] = auth.compute_sha256()
        self.store.write_atomic("merge-authorization.json", auth_payload)
        self.save_state_machine(next_sm)
        return next_sm

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
