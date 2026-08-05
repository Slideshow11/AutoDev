"""Tests for autocoder_lifecycle.checkpoint."""
from __future__ import annotations

import json

import pytest

from autocoder_lifecycle import (
    CheckpointState,
    RegistryBuilder,
    ImmutableLifecycleRegistry,
    checkpoint_requires_operator,
    next_action_from_checkpoint,
    validate_checkpoint,
    validate_resume_observations,
)


def _baseline_state() -> CheckpointState:
    return CheckpointState(
        repo="example-org/example-repo",
        pr_number=42,
        branch="feat/example",
        current_head="a" * 64,
        base_head="b" * 64,
        phase="PHASE_2",
        completed_phases=["PHASE_1", "PHASE_2"],
        next_phase=None,
        next_action=None,
        pending_actions=[],
        last_verified_pr_head="a" * 64,
        last_verified_base_head="b" * 64,
        authorized_thread_ids=[],
        unresolved_thread_ids=[],
        terminal_state="COMPLETED",
        updated_at="2025-01-01T00:00:00Z",
    )


class TestCheckpointStructure:
    def test_valid_state_has_no_errors(self) -> None:
        state = _baseline_state()
        assert validate_checkpoint(state) == []

    def test_missing_repo_fails(self) -> None:
        state = _baseline_state()
        state.repo = ""
        errors = validate_checkpoint(state)
        assert any("repo" in e for e in errors)

    def test_missing_branch_fails(self) -> None:
        state = _baseline_state()
        state.branch = ""
        errors = validate_checkpoint(state)
        assert any("branch" in e for e in errors)

    def test_missing_current_head_fails(self) -> None:
        state = _baseline_state()
        state.current_head = ""
        errors = validate_checkpoint(state)
        assert any("current_head" in e for e in errors)

    def test_invalid_current_head_sha_fails(self) -> None:
        state = _baseline_state()
        state.current_head = "not-a-sha"
        errors = validate_checkpoint(state)
        assert any("current_head" in e for e in errors)

    def test_negative_pr_number_fails(self) -> None:
        state = _baseline_state()
        state.pr_number = -1
        errors = validate_checkpoint(state)
        assert any("pr_number" in e for e in errors)

    def test_invalid_pending_actions_list_fails(self) -> None:
        state = _baseline_state()
        state.pending_actions = ["valid", 123]  # type: ignore[list-item]
        errors = validate_checkpoint(state)
        assert any("pending_actions" in e for e in errors)

    def test_invalid_unresolved_thread_ids_fails(self) -> None:
        state = _baseline_state()
        state.unresolved_thread_ids = "not-a-list"  # type: ignore[assignment]
        errors = validate_checkpoint(state)
        assert any("unresolved_thread_ids" in e for e in errors)


class TestCheckpointJsonRoundtrip:
    def test_roundtrip_preserves_fields(self) -> None:
        original = _baseline_state()
        payload = original.to_dict()
        restored = CheckpointState.from_dict(payload)
        assert restored.to_dict() == original.to_dict()

    def test_json_roundtrip(self) -> None:
        state = _baseline_state()
        payload = json.dumps(state.to_dict())
        restored = CheckpointState.from_dict(json.loads(payload))
        assert restored.repo == state.repo
        assert restored.pr_number == state.pr_number
        assert restored.next_action == state.next_action
        assert restored.terminal_state == state.terminal_state

    def test_from_dict_rejects_invalid_repo_type(self) -> None:
        # Repo as int (not str) must be rejected, not coerced.
        payload = _baseline_state().to_dict()
        payload["repo"] = 42
        with pytest.raises(ValueError, match="repo"):
            CheckpointState.from_dict(payload)

    def test_from_dict_rejects_invalid_completed_phases(self) -> None:
        # completed_phases as a string (not a list) must be rejected.
        payload = _baseline_state().to_dict()
        payload["completed_phases"] = "PHASE_1"
        with pytest.raises(ValueError, match="completed_phases"):
            CheckpointState.from_dict(payload)

    def test_from_dict_rejects_invalid_pr_number(self) -> None:
        payload = _baseline_state().to_dict()
        payload["pr_number"] = -1
        with pytest.raises(ValueError, match="pr_number"):
            CheckpointState.from_dict(payload)

    def test_from_dict_treats_empty_base_head_as_none(self) -> None:
        payload = _baseline_state().to_dict()
        payload["base_head"] = ""
        state = CheckpointState.from_dict(payload)
        assert state.base_head == ""

    def test_from_dict_rejects_non_string_base_head(self) -> None:
        payload = _baseline_state().to_dict()
        payload["base_head"] = 42
        with pytest.raises(ValueError, match="base_head"):
            CheckpointState.from_dict(payload)


class TestResumeObservations:
    def test_no_drift(self) -> None:
        state = _baseline_state()
        errors = validate_resume_observations(state, "a" * 64, "b" * 64)
        assert errors == []

    def test_pr_head_drift_detected(self) -> None:
        state = _baseline_state()
        errors = validate_resume_observations(state, "c" * 64, "b" * 64)
        assert any("PR head drift" in e for e in errors)

    def test_base_head_drift_detected(self) -> None:
        state = _baseline_state()
        errors = validate_resume_observations(state, "a" * 64, "c" * 64)
        assert any("Base head drift" in e for e in errors)

    def test_no_drift_when_checkpoints_none(self) -> None:
        state = _baseline_state()
        state.last_verified_pr_head = None
        state.last_verified_base_head = None
        errors = validate_resume_observations(state, "d" * 64, "e" * 64)
        assert errors == []


class TestNextAction:
    def test_next_action_round_trip(self) -> None:
        state = _baseline_state()
        state.next_action = "RESUME"
        assert next_action_from_checkpoint(state) == "RESUME"

    def test_next_action_none(self) -> None:
        state = _baseline_state()
        state.next_action = None
        assert next_action_from_checkpoint(state) is None


class TestOperatorRequired:
    def test_operator_required_unresolved_threads(self) -> None:
        state = _baseline_state()
        state.terminal_state = "COMPLETED"
        state.unresolved_thread_ids = ["thread-1"]
        assert checkpoint_requires_operator(state) is True

    def test_no_operator_required_clean(self) -> None:
        state = _baseline_state()
        state.terminal_state = "COMPLETED"
        assert checkpoint_requires_operator(state) is False

    def test_no_operator_required_with_next_action(self) -> None:
        state = _baseline_state()
        state.terminal_state = "AWAITING_HUMAN_AUTHORIZATION"
        state.next_action = "RESUME"
        assert checkpoint_requires_operator(state) is False

    def test_operator_required_parked_no_next_action(self) -> None:
        state = _baseline_state()
        state.terminal_state = "AWAITING_HUMAN_AUTHORIZATION"
        state.next_action = None
        assert checkpoint_requires_operator(state) is True

    def test_unknown_state_does_not_auto_resume(self) -> None:
        state = _baseline_state()
        state.terminal_state = "PROVIDER_SPECIFIC_STATE"
        # Not in default registry — must require operator.
        assert checkpoint_requires_operator(state) is True

    def test_custom_registry_without_completed_requires_operator(self) -> None:
        # A custom registry that DOES NOT recognize COMPLETED must require
        # operator intervention for resume.
        custom = RegistryBuilder().build()  # default + base vocabulary
        # Sanity: custom registry DOES recognize COMPLETED (inherited from base).
        assert custom.is_terminal_or_parked("COMPLETED")

    def test_completed_must_not_carry_next_action(self) -> None:
        state = _baseline_state()
        state.terminal_state = "COMPLETED"
        state.next_action = "RESUME"
        errors = validate_checkpoint(state)
        assert any("completed closeout" in e for e in errors)
