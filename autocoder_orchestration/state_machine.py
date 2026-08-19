"""Canonical state machine for the AutoDev control plane.

The state machine is the only writer of the authoritative run state.
Workers report observations; the controller applies state
transitions. A transition is valid only if:

1. the current state is in the transition's allowed predecessor set;
2. the called actor is in the transition's authorized-actor set;
3. the required evidence is present in the run context;
4. the new head stability requirements are met.

Allowed transitions are encoded explicitly. Anything not encoded is
prohibited by mechanical rejection, not by prompt text.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Callable, Collection, Dict, FrozenSet, Iterable, List, Optional, Tuple


# === State constants ===
STATE_PLANNED = "PLANNED"
STATE_IMPLEMENTING = "IMPLEMENTING"
STATE_AWAITING_CI = "AWAITING_CI"
STATE_REPAIRING_REVIEW_FINDINGS = "REPAIRING_REVIEW_FINDINGS"
STATE_QUALIFYING_READINESS = "QUALIFYING_READINESS"
STATE_READY_FOR_CANDIDATE = "READY_FOR_CANDIDATE"
STATE_CANDIDATE_FROZEN = "CANDIDATE_FROZEN"
STATE_AWAITING_INDEPENDENT_VERIFICATION = "AWAITING_INDEPENDENT_VERIFICATION"
STATE_VERIFYING = "VERIFYING"
STATE_VERIFICATION_FAILED = "VERIFICATION_FAILED"
STATE_VERIFICATION_REPAIR = "VERIFICATION_REPAIR"
STATE_AWAITING_MERGE_AUTHORIZATION = "AWAITING_MERGE_AUTHORIZATION"
STATE_MERGE_AUTHORIZED = "MERGE_AUTHORIZED"
STATE_POST_MERGE_VERIFYING = "POST_MERGE_VERIFYING"
STATE_COMPLETE = "COMPLETE"
STATE_BLOCKED = "BLOCKED"

ALL_STATES: Tuple[str, ...] = (
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

TERMINAL_STATES: FrozenSet[str] = frozenset({STATE_COMPLETE, STATE_BLOCKED})

# Workers MUST NOT be able to set these states. The controller is the
# only authorized actor for these transitions.
CONTROLLER_ONLY_STATES: FrozenSet[str] = frozenset(
    {
        STATE_CANDIDATE_FROZEN,
        STATE_VERIFYING,
        STATE_AWAITING_MERGE_AUTHORIZATION,
        STATE_MERGE_AUTHORIZED,
        STATE_COMPLETE,
    }
)


# === Errors ===
class StateError(Exception):
    """Base error for state machine failures."""


class InvalidTransition(StateError):
    """Raised when a transition is not allowed by the state machine."""


class StateConflict(StateError):
    """Raised when a state-mutation attempt finds the state at a
    different revision than expected (optimistic concurrency)."""


# === Transition definition ===
@dataclass(frozen=True)
class Transition:
    """Description of one allowed transition."""

    source: str
    target: str
    authorized_actors: FrozenSet[str]
    required_evidence: FrozenSet[str]
    head_stability: str  # "any" | "live" | "exact_head" | "post_head"
    idempotency: str  # "any" | "noop_same_state" | "first_wins"
    durable_event: str
    invalidates: FrozenSet[str]  # set of evidence keys to invalidate

    def require_authorized(self, actor: str) -> None:
        if actor not in self.authorized_actors:
            raise InvalidTransition(
                f"actor {actor!r} not authorized for transition "
                f"{self.source} -> {self.target}"
            )


# === Forward transitions ===
_FORWARD_TRANSITIONS: Tuple[Transition, ...] = (
    Transition(
        source=STATE_PLANNED,
        target=STATE_IMPLEMENTING,
        authorized_actors=frozenset({"controller"}),
        required_evidence=frozenset({"run_context"}),
        head_stability="any",
        idempotency="first_wins",
        durable_event="control_plane.planned_to_implementing",
        invalidates=frozenset(),
    ),
    Transition(
        source=STATE_IMPLEMENTING,
        target=STATE_AWAITING_CI,
        authorized_actors=frozenset({"implementation_worker", "controller"}),
        required_evidence=frozenset({"implementation_lease"}),
        head_stability="any",
        idempotency="first_wins",
        durable_event="control_plane.implemented",
        invalidates=frozenset(),
    ),
    Transition(
        source=STATE_AWAITING_CI,
        target=STATE_REPAIRING_REVIEW_FINDINGS,
        authorized_actors=frozenset({"controller"}),
        required_evidence=frozenset({"ci_inventory"}),
        head_stability="live",
        idempotency="first_wins",
        durable_event="control_plane.repairing_findings",
        invalidates=frozenset({"ci_inventory"}),
    ),
    Transition(
        source=STATE_AWAITING_CI,
        target=STATE_QUALIFYING_READINESS,
        authorized_actors=frozenset({"controller"}),
        required_evidence=frozenset({"ci_inventory", "review_inventory"}),
        head_stability="live",
        idempotency="first_wins",
        durable_event="control_plane.ci_passed",
        invalidates=frozenset(),
    ),
    Transition(
        source=STATE_REPAIRING_REVIEW_FINDINGS,
        target=STATE_AWAITING_CI,
        authorized_actors=frozenset({"implementation_worker", "controller"}),
        required_evidence=frozenset({"repair_commit_shas"}),
        head_stability="live",
        idempotency="first_wins",
        durable_event="control_plane.repair_pushed",
        invalidates=frozenset({"ci_inventory", "review_inventory"}),
    ),
    # Round-41: a worker that executed the directive and
    # emitted structured ``NO_CHANGES_REQUIRED`` proof can
    # advance REPAIRING_REVIEW_FINDINGS directly to
    # QUALIFYING_READINESS, skipping AWAITING_CI because
    # no commit was produced (so no CI gate is required for
    # THIS round's commit). The supervisor's CI policy /
    # quiet-window machinery handles the rest of the
    # qualification. The required evidence is the
    # ``no_changes_required_proof`` blob carrying
    # per-finding disposition and verification summary.
    Transition(
        source=STATE_REPAIRING_REVIEW_FINDINGS,
        target=STATE_QUALIFYING_READINESS,
        authorized_actors=frozenset({"implementation_worker", "controller"}),
        required_evidence=frozenset({"no_changes_required_proof"}),
        head_stability="exact_head",
        idempotency="first_wins",
        durable_event="control_plane.no_changes_required",
        invalidates=frozenset({"ci_inventory", "review_inventory"}),
    ),
    Transition(
        source=STATE_QUALIFYING_READINESS,
        target=STATE_READY_FOR_CANDIDATE,
        authorized_actors=frozenset({"controller"}),
        required_evidence=frozenset({"readiness_certificate"}),
        head_stability="exact_head",
        idempotency="first_wins",
        durable_event="control_plane.ready_for_candidate",
        invalidates=frozenset(),
    ),
    # Round-33: a head that previously qualified (CI clean, reviews clean)
    # can receive NEW actionable reviews on the SAME head. The relay must
    # be able to re-enter REPAIRING_REVIEW_FINDINGS without an
    # intermediate QUALIFYING_READINESS -> READY_FOR_CANDIDATE -> ...
    # back-walk. The required evidence is the actionable-review
    # inventory that was not present at the prior qualifying decision;
    # ``invalidates`` clears the prior readiness certificate so the
    # next QUALIFYING_READINESS->READY_FOR_CANDIDATE transition must
    # be re-earned with a fresh certificate.
    Transition(
        source=STATE_QUALIFYING_READINESS,
        target=STATE_REPAIRING_REVIEW_FINDINGS,
        authorized_actors=frozenset({"controller"}),
        required_evidence=frozenset({"new_actionable_review_inventory"}),
        head_stability="live",
        idempotency="first_wins",
        durable_event="control_plane.reopened_for_new_actionable_review",
        invalidates=frozenset({"readiness_certificate"}),
    ),
    Transition(
        source=STATE_READY_FOR_CANDIDATE,
        target=STATE_CANDIDATE_FROZEN,
        authorized_actors=frozenset({"candidate_builder", "controller"}),
        required_evidence=frozenset({"readiness_certificate", "candidate"}),
        head_stability="exact_head",
        idempotency="first_wins",
        durable_event="control_plane.candidate_frozen",
        invalidates=frozenset(),
    ),
    Transition(
        source=STATE_CANDIDATE_FROZEN,
        target=STATE_AWAITING_INDEPENDENT_VERIFICATION,
        authorized_actors=frozenset({"controller"}),
        required_evidence=frozenset({"candidate", "verifier_handoff"}),
        head_stability="exact_head",
        idempotency="first_wins",
        durable_event="control_plane.handoff",
        invalidates=frozenset(),
    ),
    Transition(
        source=STATE_AWAITING_INDEPENDENT_VERIFICATION,
        target=STATE_VERIFYING,
        authorized_actors=frozenset({"independent_verifier", "controller"}),
        required_evidence=frozenset({"verifier_process_identity"}),
        head_stability="exact_head",
        idempotency="first_wins",
        durable_event="control_plane.verification_started",
        invalidates=frozenset(),
    ),
    Transition(
        source=STATE_VERIFYING,
        target=STATE_VERIFICATION_FAILED,
        authorized_actors=frozenset({"independent_verifier", "controller"}),
        required_evidence=frozenset({"verifier_record"}),
        head_stability="exact_head",
        idempotency="first_wins",
        durable_event="control_plane.verification_failed",
        invalidates=frozenset({"readiness_certificate", "candidate"}),
    ),
    Transition(
        source=STATE_VERIFYING,
        target=STATE_AWAITING_MERGE_AUTHORIZATION,
        authorized_actors=frozenset({"controller"}),
        required_evidence=frozenset({"verifier_record", "merge_authorization_plan"}),
        head_stability="exact_head",
        idempotency="first_wins",
        durable_event="control_plane.verification_passed",
        invalidates=frozenset(),
    ),
    Transition(
        source=STATE_VERIFICATION_FAILED,
        target=STATE_VERIFICATION_REPAIR,
        authorized_actors=frozenset({"controller"}),
        required_evidence=frozenset({"failed_gates"}),
        head_stability="exact_head",
        idempotency="first_wins",
        durable_event="control_plane.repair_from_verification",
        invalidates=frozenset({"candidate", "verifier_record"}),
    ),
    Transition(
        source=STATE_VERIFICATION_REPAIR,
        target=STATE_IMPLEMENTING,
        authorized_actors=frozenset({"controller"}),
        required_evidence=frozenset({"repair_brief"}),
        head_stability="exact_head",
        idempotency="first_wins",
        durable_event="control_plane.repair_back_to_implementing",
        invalidates=frozenset({"ci_inventory", "review_inventory", "candidate", "verifier_record"}),
    ),
    Transition(
        source=STATE_AWAITING_MERGE_AUTHORIZATION,
        target=STATE_MERGE_AUTHORIZED,
        authorized_actors=frozenset({"human_operator", "controller"}),
        required_evidence=frozenset({"merge_authorization"}),
        head_stability="exact_head",
        idempotency="first_wins",
        durable_event="control_plane.authorized",
        invalidates=frozenset(),
    ),
    Transition(
        source=STATE_MERGE_AUTHORIZED,
        target=STATE_POST_MERGE_VERIFYING,
        authorized_actors=frozenset({"controller"}),
        required_evidence=frozenset({"merge_record"}),
        head_stability="post_head",
        idempotency="first_wins",
        durable_event="control_plane.merged",
        invalidates=frozenset(),
    ),
    Transition(
        source=STATE_POST_MERGE_VERIFYING,
        target=STATE_COMPLETE,
        authorized_actors=frozenset({"controller"}),
        required_evidence=frozenset({"post_merge_verification"}),
        head_stability="post_head",
        idempotency="first_wins",
        durable_event="control_plane.complete",
        invalidates=frozenset(),
    ),
    # Block from any state
    *(Transition(
        source=s,
        target=STATE_BLOCKED,
        authorized_actors=frozenset({"controller", "human_operator"}),
        required_evidence=frozenset({"block_reason"}),
        head_stability="any",
        idempotency="any",
        durable_event=f"control_plane.blocked_from_{s}",
        invalidates=frozenset(),
    ) for s in ALL_STATES if s not in TERMINAL_STATES),
)

# Map (source, target) -> Transition
_TRANSITIONS_BY_KEY: Dict[Tuple[str, str], Transition] = {
    (t.source, t.target): t for t in _FORWARD_TRANSITIONS
}


def get_transition(source: str, target: str) -> Optional[Transition]:
    return _TRANSITIONS_BY_KEY.get((source, target))


@dataclass
class StateMachine:
    """Authoritative state machine.

    The state machine is a value object: it holds the current state,
    the state revision, and the journal. Mutations are returned as
    new state-machine instances; the controller's store is
    responsible for atomic persistence.
    """

    current_state: str = STATE_PLANNED
    revision: int = 0
    expected_revision: int = 0
    journal: List[dict] = field(default_factory=list)
    evidence: Dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.current_state not in ALL_STATES:
            raise ValueError(f"unknown state: {self.current_state!r}")

    def with_evidence(self, key: str, payload: dict) -> "StateMachine":
        new_ev = dict(self.evidence)
        new_ev[key] = payload
        return dataclasses.replace(self, evidence=new_ev)

    def can_transition(self, target: str, actor: str) -> Tuple[bool, Optional[Transition]]:
        """Return whether ``target`` is reachable from the current
        state by ``actor`` without performing the transition.
        """
        if self.current_state in TERMINAL_STATES:
            return False, None
        t = get_transition(self.current_state, target)
        if t is None:
            return False, None
        if actor not in t.authorized_actors:
            return False, t
        return True, t

    def transition(
        self,
        target: str,
        actor: str,
        *,
        head_observed: Optional[str] = None,
        head_required: Optional[str] = None,
    ) -> "StateMachine":
        """Apply a transition. Raises on invalid source/target/actor.

        Returns a new state machine with the updated state, an
        incremented revision, and a journal entry.
        """
        if self.current_state in TERMINAL_STATES:
            raise InvalidTransition(
                f"cannot transition out of terminal state {self.current_state!r}"
            )
        t = get_transition(self.current_state, target)
        if t is None:
            raise InvalidTransition(
                f"no transition from {self.current_state!r} to {target!r}"
            )
        t.require_authorized(actor)
        # Required evidence is checked at the controller level; the
        # state machine only enforces source/target/actor validity.
        # Head stability is also enforced at the controller level via
        # callers passing head_observed.
        if t.head_stability == "exact_head" and head_observed is not None and head_required is not None:
            if head_observed != head_required:
                raise InvalidTransition(
                    f"head stability violated: observed {head_observed!r} != "
                    f"required {head_required!r}"
                )
        # Idempotency: same-state no-op allowed
        if self.current_state == target and t.idempotency == "noop_same_state":
            return self
        if self.current_state == target and t.idempotency == "first_wins":
            # First-wins on identical state: return unchanged.
            return self
        # Apply invalidations
        new_evidence = dict(self.evidence)
        for key in t.invalidates:
            new_evidence.pop(key, None)
        new_journal = list(self.journal)
        new_journal.append({
            "from": self.current_state,
            "to": target,
            "actor": actor,
            "event": t.durable_event,
            "revision": self.revision + 1,
            "head_observed": head_observed,
            "head_required": head_required,
        })
        return dataclasses.replace(
            self,
            current_state=target,
            revision=self.revision + 1,
            journal=new_journal,
            evidence=new_evidence,
        )

    def restart_check(self) -> "StateMachine":
        """Re-evaluate from the journal.

        Used by the controller when it restarts. The default behavior
        is to return the state machine unchanged; the controller
        decides whether to replay the last transition or roll back.
        """
        return self

    def to_dict(self) -> dict:
        return {
            "current_state": self.current_state,
            "revision": self.revision,
            "expected_revision": self.expected_revision,
            "journal": list(self.journal),
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "StateMachine":
        if not isinstance(payload, dict):
            raise ValueError("state machine payload must be a dict")
        for key in ("current_state", "revision", "expected_revision", "journal", "evidence"):
            if key not in payload:
                raise ValueError(f"state machine missing field: {key!r}")
        return cls(
            current_state=payload["current_state"],
            revision=int(payload["revision"]),
            expected_revision=int(payload["expected_revision"]),
            journal=list(payload["journal"]),
            evidence=dict(payload["evidence"]),
        )


def is_worker_forbidden_to_set(state: str) -> bool:
    """Return True iff an implementation worker is not allowed to
    place the run directly into the given state.
    """
    return state in CONTROLLER_ONLY_STATES


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES
