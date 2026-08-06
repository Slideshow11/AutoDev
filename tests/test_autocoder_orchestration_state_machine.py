"""Tests for autocoder_orchestration.state_machine.StateMachine."""
from __future__ import annotations

import copy

import pytest

from autocoder_orchestration import (
    StateMachine,
    InvalidTransition,
    StateConflict,
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
    ACTOR_CONTROLLER,
    ACTOR_IMPL_WORKER,
    ACTOR_VERIFIER,
    ACTOR_HUMAN,
    ACTOR_CANDIDATE_BUILDER,
)
from autocoder_orchestration.state_machine import (
    CONTROLLER_ONLY_STATES,
    TERMINAL_STATES,
    get_transition,
    is_worker_forbidden_to_set,
    is_terminal,
)


H1 = "a" * 64
H2 = "b" * 64


def _sm(state: str = STATE_PLANNED) -> StateMachine:
    return StateMachine(current_state=state)


# === Valid transitions ===
class TestValidTransitions:
    def test_planned_to_implementing(self) -> None:
        sm = _sm().transition(STATE_IMPLEMENTING, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_IMPLEMENTING

    def test_implementing_to_awaiting_ci(self) -> None:
        sm = _sm(STATE_IMPLEMENTING).transition(STATE_AWAITING_CI, ACTOR_IMPL_WORKER, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_AWAITING_CI

    def test_awaiting_ci_to_qualifying(self) -> None:
        sm = _sm(STATE_AWAITING_CI).transition(STATE_QUALIFYING_READINESS, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_QUALIFYING_READINESS

    def test_awaiting_ci_to_repairing(self) -> None:
        sm = _sm(STATE_AWAITING_CI).transition(STATE_REPAIRING_REVIEW_FINDINGS, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_REPAIRING_REVIEW_FINDINGS

    def test_repairing_to_awaiting_ci(self) -> None:
        sm = _sm(STATE_REPAIRING_REVIEW_FINDINGS).transition(STATE_AWAITING_CI, ACTOR_IMPL_WORKER, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_AWAITING_CI

    def test_qualifying_to_ready_for_candidate(self) -> None:
        sm = _sm(STATE_QUALIFYING_READINESS).transition(STATE_READY_FOR_CANDIDATE, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_READY_FOR_CANDIDATE

    def test_ready_to_candidate_frozen(self) -> None:
        sm = _sm(STATE_READY_FOR_CANDIDATE).transition(STATE_CANDIDATE_FROZEN, ACTOR_CANDIDATE_BUILDER, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_CANDIDATE_FROZEN

    def test_candidate_frozen_to_awaiting_verification(self) -> None:
        sm = _sm(STATE_CANDIDATE_FROZEN).transition(STATE_AWAITING_INDEPENDENT_VERIFICATION, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_AWAITING_INDEPENDENT_VERIFICATION

    def test_awaiting_verification_to_verifying(self) -> None:
        sm = _sm(STATE_AWAITING_INDEPENDENT_VERIFICATION).transition(STATE_VERIFYING, ACTOR_VERIFIER, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_VERIFYING

    def test_verifying_to_verification_failed(self) -> None:
        sm = _sm(STATE_VERIFYING).transition(STATE_VERIFICATION_FAILED, ACTOR_VERIFIER, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_VERIFICATION_FAILED

    def test_verifying_to_awaiting_merge(self) -> None:
        sm = _sm(STATE_VERIFYING).transition(STATE_AWAITING_MERGE_AUTHORIZATION, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_AWAITING_MERGE_AUTHORIZATION

    def test_verification_failed_to_repair(self) -> None:
        sm = _sm(STATE_VERIFICATION_FAILED).transition(STATE_VERIFICATION_REPAIR, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_VERIFICATION_REPAIR

    def test_verification_repair_to_implementing(self) -> None:
        sm = _sm(STATE_VERIFICATION_REPAIR).transition(STATE_IMPLEMENTING, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_IMPLEMENTING

    def test_awaiting_merge_to_authorized(self) -> None:
        sm = _sm(STATE_AWAITING_MERGE_AUTHORIZATION).transition(STATE_MERGE_AUTHORIZED, ACTOR_HUMAN, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_MERGE_AUTHORIZED

    def test_authorized_to_post_merge(self) -> None:
        sm = _sm(STATE_MERGE_AUTHORIZED).transition(STATE_POST_MERGE_VERIFYING, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_POST_MERGE_VERIFYING

    def test_post_merge_to_complete(self) -> None:
        sm = _sm(STATE_POST_MERGE_VERIFYING).transition(STATE_COMPLETE, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_COMPLETE

    def test_journal_records_transition(self) -> None:
        sm = _sm().transition(STATE_IMPLEMENTING, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)
        assert len(sm.journal) == 1
        assert sm.journal[0]["to"] == STATE_IMPLEMENTING
        assert sm.journal[0]["actor"] == ACTOR_CONTROLLER

    def test_revision_increments(self) -> None:
        sm = _sm()
        assert sm.revision == 0
        sm = sm.transition(STATE_IMPLEMENTING, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)
        assert sm.revision == 1


# === Prohibited transitions ===
class TestProhibitedTransitions:
    def test_planned_to_complete_prohibited(self) -> None:
        with pytest.raises(InvalidTransition):
            _sm().transition(STATE_COMPLETE, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)

    def test_planned_to_awaiting_ci_prohibited(self) -> None:
        with pytest.raises(InvalidTransition):
            _sm().transition(STATE_AWAITING_CI, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)

    def test_implementation_worker_cannot_set_candidate_frozen(self) -> None:
        with pytest.raises(InvalidTransition):
            _sm(STATE_READY_FOR_CANDIDATE).transition(STATE_CANDIDATE_FROZEN, ACTOR_IMPL_WORKER, head_observed=H1, head_required=H1)

    def test_implementation_worker_cannot_set_verifying(self) -> None:
        with pytest.raises(InvalidTransition):
            _sm(STATE_AWAITING_INDEPENDENT_VERIFICATION).transition(STATE_VERIFYING, ACTOR_IMPL_WORKER, head_observed=H1, head_required=H1)

    def test_implementation_worker_cannot_set_awaiting_merge(self) -> None:
        with pytest.raises(InvalidTransition):
            _sm(STATE_VERIFYING).transition(STATE_AWAITING_MERGE_AUTHORIZATION, ACTOR_IMPL_WORKER, head_observed=H1, head_required=H1)

    def test_implementation_worker_cannot_set_authorized(self) -> None:
        with pytest.raises(InvalidTransition):
            _sm(STATE_AWAITING_MERGE_AUTHORIZATION).transition(STATE_MERGE_AUTHORIZED, ACTOR_IMPL_WORKER, head_observed=H1, head_required=H1)

    def test_implementation_worker_cannot_set_complete(self) -> None:
        with pytest.raises(InvalidTransition):
            _sm(STATE_POST_MERGE_VERIFYING).transition(STATE_COMPLETE, ACTOR_IMPL_WORKER, head_observed=H1, head_required=H1)

    def test_verifier_cannot_authorize_merge(self) -> None:
        with pytest.raises(InvalidTransition):
            _sm(STATE_AWAITING_MERGE_AUTHORIZATION).transition(STATE_MERGE_AUTHORIZED, ACTOR_VERIFIER, head_observed=H1, head_required=H1)

    def test_verifier_cannot_set_complete(self) -> None:
        with pytest.raises(InvalidTransition):
            _sm(STATE_POST_MERGE_VERIFYING).transition(STATE_COMPLETE, ACTOR_VERIFIER, head_observed=H1, head_required=H1)

    def test_no_transition_from_complete(self) -> None:
        with pytest.raises(InvalidTransition):
            _sm(STATE_COMPLETE).transition(STATE_BLOCKED, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)

    def test_no_transition_from_blocked(self) -> None:
        with pytest.raises(InvalidTransition):
            _sm(STATE_BLOCKED).transition(STATE_PLANNED, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)

    def test_head_observed_mismatch_blocks(self) -> None:
        # QUALIFYING_READINESS -> READY_FOR_CANDIDATE has head_stability="exact_head"
        with pytest.raises(InvalidTransition, match="head stability"):
            _sm(STATE_QUALIFYING_READINESS).transition(STATE_READY_FOR_CANDIDATE, ACTOR_CONTROLLER, head_observed=H2, head_required=H1)


# === Block from any state ===
class TestBlock:
    def test_block_from_planned(self) -> None:
        sm = _sm(STATE_PLANNED).transition(STATE_BLOCKED, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_BLOCKED

    def test_block_from_implementing(self) -> None:
        sm = _sm(STATE_IMPLEMENTING).transition(STATE_BLOCKED, ACTOR_HUMAN, head_observed=H1, head_required=H1)
        assert sm.current_state == STATE_BLOCKED


# === Idempotency ===
class TestIdempotency:
    def test_same_state_transition_invalid(self) -> None:
        # Same-state is not a defined transition; the controller must
        # rely on revision comparison for idempotency, not on a fake
        # self-loop.
        with pytest.raises(InvalidTransition):
            _sm(STATE_PLANNED).transition(STATE_PLANNED, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)


# === Invalidation ===
class TestInvalidation:
    def test_awaiting_ci_to_repairing_invalidates_ci_inventory(self) -> None:
        sm = _sm(STATE_AWAITING_CI)
        sm = sm.with_evidence("ci_inventory", {"items": []})
        sm = sm.transition(STATE_REPAIRING_REVIEW_FINDINGS, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)
        assert "ci_inventory" not in sm.evidence

    def test_repairing_to_awaiting_ci_invalidates_review_inventory(self) -> None:
        sm = _sm(STATE_REPAIRING_REVIEW_FINDINGS)
        sm = sm.with_evidence("ci_inventory", {"items": []})
        sm = sm.with_evidence("review_inventory", {"items": []})
        sm = sm.transition(STATE_AWAITING_CI, ACTOR_IMPL_WORKER, head_observed=H1, head_required=H1)
        assert "ci_inventory" not in sm.evidence
        assert "review_inventory" not in sm.evidence


# === Round-trip ===
class TestRoundtrip:
    def test_to_dict_from_dict(self) -> None:
        sm = StateMachine(
            current_state=STATE_QUALIFYING_READINESS,
            revision=5,
            expected_revision=2,
            journal=[{"from": "PLANNED", "to": "IMPLEMENTING", "actor": "controller"}],
            evidence={"ci_inventory": {"x": 1}},
        )
        payload = sm.to_dict()
        restored = StateMachine.from_dict(payload)
        assert restored.current_state == sm.current_state
        assert restored.revision == sm.revision
        assert restored.expected_revision == sm.expected_revision
        assert restored.journal == sm.journal
        assert restored.evidence == sm.evidence


# === Restart ===
class TestRestart:
    def test_restart_no_op(self) -> None:
        sm = _sm(STATE_IMPLEMENTING).with_evidence("x", {"y": 1})
        sm = sm.restart_check()
        assert sm.current_state == STATE_IMPLEMENTING
        assert sm.evidence["x"] == {"y": 1}


# === Helpers ===
class TestHelpers:
    def test_controller_only_states(self) -> None:
        assert STATE_CANDIDATE_FROZEN in CONTROLLER_ONLY_STATES
        assert STATE_VERIFYING in CONTROLLER_ONLY_STATES
        assert STATE_AWAITING_MERGE_AUTHORIZATION in CONTROLLER_ONLY_STATES
        assert STATE_MERGE_AUTHORIZED in CONTROLLER_ONLY_STATES
        assert STATE_COMPLETE in CONTROLLER_ONLY_STATES

    def test_worker_forbidden_to_set(self) -> None:
        for s in CONTROLLER_ONLY_STATES:
            assert is_worker_forbidden_to_set(s) is True

    def test_terminal_states(self) -> None:
        assert is_terminal(STATE_COMPLETE)
        assert is_terminal(STATE_BLOCKED)
        assert not is_terminal(STATE_PLANNED)

    def test_get_transition_known(self) -> None:
        t = get_transition(STATE_PLANNED, STATE_IMPLEMENTING)
        assert t is not None
        assert t.source == STATE_PLANNED
        assert t.target == STATE_IMPLEMENTING

    def test_get_transition_unknown(self) -> None:
        assert get_transition(STATE_PLANNED, STATE_COMPLETE) is None
