"""Tests for autocoder_lifecycle.watchdog."""
from __future__ import annotations

import pytest

from autocoder_lifecycle import (
    STALL_RISK,
    WATCHDOG_PROGRESS_REQUIRED,
    WATCHDOG_OK,
    WatchdogState,
    evaluate_watchdog,
    should_continue_polling,
)


class TestEvaluateWatchdog:
    def test_returns_ok_within_budget(self) -> None:
        state = WatchdogState(
            phase_name="PHASE_X",
            started_at=0.0,
            last_progress_at=90.0,
            max_idle_seconds=100.0,
            max_phase_seconds=200.0,
        )
        assert evaluate_watchdog(state, now=100.0) == WATCHDOG_OK

    def test_returns_progress_required_when_idle(self) -> None:
        state = WatchdogState(
            phase_name="PHASE_X",
            started_at=0.0,
            last_progress_at=0.0,
            max_idle_seconds=100.0,
            max_phase_seconds=200.0,
        )
        # now=150 means idle=150 > max_idle=100
        assert evaluate_watchdog(state, now=150.0) == WATCHDOG_PROGRESS_REQUIRED

    def test_returns_stall_risk_on_phase_timeout(self) -> None:
        state = WatchdogState(
            phase_name="PHASE_X",
            started_at=0.0,
            last_progress_at=0.0,
            max_idle_seconds=100.0,
            max_phase_seconds=200.0,
        )
        # now=300 means elapsed=300 > max_phase=200
        assert evaluate_watchdog(state, now=300.0) == STALL_RISK

    def test_with_terminal_state_returns_stall_risk(self) -> None:
        state = WatchdogState(
            phase_name="PHASE_X",
            started_at=0.0,
            last_progress_at=0.0,
            max_idle_seconds=100.0,
            max_phase_seconds=200.0,
            terminal_state="COMPLETED",
        )
        assert evaluate_watchdog(state, now=10.0) == STALL_RISK

    def test_with_parked_state_returns_stall_risk(self) -> None:
        state = WatchdogState(
            phase_name="PHASE_X",
            started_at=0.0,
            last_progress_at=0.0,
            max_idle_seconds=100.0,
            max_phase_seconds=200.0,
            terminal_state="AWAITING_HUMAN_AUTHORIZATION",
        )
        assert evaluate_watchdog(state, now=10.0) == STALL_RISK

    def test_caller_exhaustion_classifier_overrides(self) -> None:
        state = WatchdogState(
            phase_name="PHASE_X",
            started_at=0.0,
            last_progress_at=50.0,
            max_idle_seconds=100.0,
            max_phase_seconds=200.0,
        )

        def classifier(s, now):
            return "CUSTOM_VERDICT"

        assert evaluate_watchdog(
            state, now=60.0, exhaustion_classifier=classifier
        ) == "CUSTOM_VERDICT"


class TestShouldContinuePolling:
    def test_continue_when_within_budget(self) -> None:
        state = WatchdogState(
            phase_name="PHASE_X",
            started_at=0.0,
            last_progress_at=50.0,
            max_idle_seconds=100.0,
            max_phase_seconds=200.0,
        )
        assert should_continue_polling(state, now=70.0, deadline=1000.0) is True

    def test_stop_after_deadline(self) -> None:
        state = WatchdogState(
            phase_name="PHASE_X",
            started_at=0.0,
            last_progress_at=50.0,
            max_idle_seconds=100.0,
            max_phase_seconds=200.0,
        )
        # past deadline
        assert should_continue_polling(state, now=1500.0, deadline=1000.0) is False

    def test_stop_on_stall_risk(self) -> None:
        state = WatchdogState(
            phase_name="PHASE_X",
            started_at=0.0,
            last_progress_at=0.0,
            max_idle_seconds=10.0,
            max_phase_seconds=20.0,
        )
        # now=100 -> stalled
        assert should_continue_polling(state, now=100.0, deadline=1000.0) is False

    def test_stop_when_ok_and_terminal(self) -> None:
        state = WatchdogState(
            phase_name="PHASE_X",
            started_at=0.0,
            last_progress_at=50.0,
            max_idle_seconds=100.0,
            max_phase_seconds=200.0,
            terminal_state="COMPLETED",
        )
        # Within budgets, but terminal state set -> stop.
        assert should_continue_polling(state, now=60.0, deadline=1000.0) is False


class TestNoClockReads:
    def test_pure_injected_time(self) -> None:
        # If we never call evaluate_watchdog with the wrong ``now``, no clock is read.
        # The contract: caller always supplies ``now``. We verify the dataclass
        # is purely data.
        state = WatchdogState(
            phase_name="PHASE_X",
            started_at=0.0,
            last_progress_at=0.0,
            max_idle_seconds=100.0,
            max_phase_seconds=200.0,
        )
        # No ``now`` attribute on the watchdog state.
        assert not hasattr(state, "now")
        assert not hasattr(state, "clock")

    def test_no_filesystem_or_github_methods(self) -> None:
        state = WatchdogState(
            phase_name="PHASE_X",
            started_at=0.0,
            last_progress_at=0.0,
            max_idle_seconds=100.0,
            max_phase_seconds=200.0,
        )
        for attr in dir(state):
            assert not attr.startswith("fetch_"), attr
            assert not attr.startswith("read_"), attr
