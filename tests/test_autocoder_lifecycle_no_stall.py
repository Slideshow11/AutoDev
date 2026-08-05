"""Tests for autocoder_lifecycle.no_stall."""
from __future__ import annotations

import pytest

from autocoder_lifecycle import (
    OK_PROGRESS_WITH_NEXT_ACTION,
    OK_TERMINAL,
    STALL_NO_CHECKPOINT,
    STALL_NO_TERMINAL_STATE,
    STALL_PHASE_HEADER_ONLY,
    STALL_WAITING_FOR_CONTINUE,
    classify_terminal_message,
    is_terminal_lifecycle_state,
)
from autocoder_lifecycle import RegistryBuilder


class TestClassifyTerminalMessage:
    def test_phase_header_only(self) -> None:
        msg = "Starting PHASE 1 — building autocoder_supervisor"
        assert classify_terminal_message(msg) == STALL_PHASE_HEADER_ONLY

    def test_phase_header_now_form(self) -> None:
        msg = "Now PHASE 8 — testing"
        assert classify_terminal_message(msg) == STALL_PHASE_HEADER_ONLY

    def test_waiting_for_continue_short(self) -> None:
        msg = "Continue?"
        assert classify_terminal_message(msg) == STALL_WAITING_FOR_CONTINUE

    def test_waiting_for_continue_y_question(self) -> None:
        msg = "Some context.\nY?"
        assert classify_terminal_message(msg) == STALL_WAITING_FOR_CONTINUE

    def test_progress_with_both_action_and_checkpoint(self) -> None:
        msg = (
            "Phase 3 progress\n"
            "checkpoint: /tmp/run.json\n"
            "next_action: REPAIR_THREAD"
        )
        assert classify_terminal_message(msg) == OK_PROGRESS_WITH_NEXT_ACTION

    def test_terminal_state(self) -> None:
        msg = "Done.\nterminal_state: COMPLETED"
        assert classify_terminal_message(msg) == OK_TERMINAL

    def test_terminal_state_awaiting(self) -> None:
        msg = "Awaiting human.\nterminal_state: AWAITING_HUMAN_AUTHORIZATION"
        assert classify_terminal_message(msg) == OK_TERMINAL

    def test_action_without_checkpoint(self) -> None:
        msg = "Phase 2 progress.\nnext_action: REPAIR"
        assert classify_terminal_message(msg) == STALL_NO_CHECKPOINT

    def test_checkpoint_without_action(self) -> None:
        msg = "Phase 4 progress.\ncheckpoint: /tmp/x.json"
        assert classify_terminal_message(msg) == STALL_NO_TERMINAL_STATE

    def test_generic_progress_no_signals(self) -> None:
        msg = "Some narrative log entry with no actionable signal."
        assert classify_terminal_message(msg) == STALL_NO_TERMINAL_STATE

    def test_empty_string(self) -> None:
        assert classify_terminal_message("") == STALL_NO_TERMINAL_STATE

    def test_whitespace_only(self) -> None:
        assert classify_terminal_message("   \n  \t  ") == STALL_NO_TERMINAL_STATE


class TestIsTerminalLifecycleState:
    def test_default_terminal(self) -> None:
        assert is_terminal_lifecycle_state("COMPLETED") is True
        assert is_terminal_lifecycle_state("FAILED") is True

    def test_default_parked(self) -> None:
        assert is_terminal_lifecycle_state("AWAITING_HUMAN_AUTHORIZATION") is True
        assert is_terminal_lifecycle_state("OPERATOR_REQUIRED") is True

    def test_default_hold_not_terminal(self) -> None:
        # HOLD states are not terminal-or-parked; they are parked-only-when-recorded.
        assert is_terminal_lifecycle_state("HEAD_CHANGED") is False

    def test_unknown_state_not_terminal(self) -> None:
        assert is_terminal_lifecycle_state("UNKNOWN_STATE_X") is False

    def test_non_string_not_terminal(self) -> None:
        assert is_terminal_lifecycle_state(None) is False
        assert is_terminal_lifecycle_state(42) is False
        assert is_terminal_lifecycle_state("") is False

    def test_custom_registry(self) -> None:
        reg = RegistryBuilder().with_terminal(["CUSTOM_TERMINAL"]).build()
        assert is_terminal_lifecycle_state("CUSTOM_TERMINAL", reg) is True
        # Without passing reg, the default doesn't know about it.
        assert is_terminal_lifecycle_state("CUSTOM_TERMINAL") is False


class TestNextActionPlusCheckpointRequired:
    """The strict-both-required contract is documented in the module."""
    def test_action_plus_checkpoint_required(self) -> None:
        # If action is present but no checkpoint, must be STALL_NO_CHECKPOINT.
        msg = "phase 2 progress.\nnext_action: REPAIR"
        assert classify_terminal_message(msg) == STALL_NO_CHECKPOINT

    def test_checkpoint_without_action_falls_through(self) -> None:
        msg = "phase 4 progress.\ncheckpoint: /tmp/x.json"
        assert classify_terminal_message(msg) == STALL_NO_TERMINAL_STATE
