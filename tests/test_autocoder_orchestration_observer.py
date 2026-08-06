"""Tests for autocoder_orchestration.observer."""
from __future__ import annotations

import pytest

from autocoder_orchestration.observer import (
    StrictObserver,
    ObservationLog,
    Observation,
    ObservationDataSource,
)


class _StaticSource(ObservationDataSource):
    """A data source that returns the same snapshot for every fetch."""

    def __init__(self, snapshot: dict) -> None:
        self.snapshot = snapshot
        self.fetch_count = 0

    def fetch(self, expected_head: str) -> dict:
        self.fetch_count += 1
        return dict(self.snapshot)


def _qualifying_snapshot(head: str = "a" * 64) -> dict:
    return {
        "pr_head_sha": head,
        "head_ok": True,
        "all_ci_pass": True,
        "coderabbit_pass": True,
        "threads_unresolved_current": 0,
        "threads_unresolved_outdated": 0,
        "worker_lease_active": False,
        "unconsumed_event_count": 0,
        "provider_in_progress": False,
        "supervisor_pid": 12345,
        "process_start_identity": "abc",
        "heartbeat": True,
        "api_failure": None,
        "parse_failure": None,
        "fallback_success": False,
    }


# === Data source ===
class TestDataSource:
    def test_static_source_returns_snapshot(self) -> None:
        src = _StaticSource(_qualifying_snapshot())
        data = src.fetch("a" * 64)
        assert data["head_ok"] is True

    def test_abstract_source_must_be_subclassed(self) -> None:
        with pytest.raises(NotImplementedError):
            ObservationDataSource().fetch("a" * 64)


# === Observation ===
class TestObservation:
    def test_roundtrip(self) -> None:
        o = Observation(
            ts_utc="2026-08-05T22:00:00Z",
            ts_monotonic=1.0,
            qualifying=True,
            qualification_reason="qualifying",
            pr_head_sha="a" * 64,
            head_ok=True,
            all_ci_pass=True,
            coderabbit_pass=True,
            threads_unresolved_current=0,
            threads_unresolved_outdated=0,
            worker_lease_active=False,
            unconsumed_event_count=0,
            provider_in_progress=False,
            supervisor_pid=1,
            process_start_identity="x",
            heartbeat=True,
            api_failure=None,
            parse_failure=None,
            fallback_success=False,
        )
        payload = o.to_dict()
        restored = Observation.from_dict(payload)
        assert restored.qualifying == o.qualifying
        assert restored.pr_head_sha == o.pr_head_sha


# === Observer ===
class TestStrictObserver:
    def test_observer_constructs(self) -> None:
        src = _StaticSource(_qualifying_snapshot())
        obs = StrictObserver(src, quiet_window_seconds=0, poll_interval_seconds=0.001, max_iterations=3)
        assert obs.quiet_window_seconds == 0

    def test_quiet_window_negative_rejected(self) -> None:
        src = _StaticSource(_qualifying_snapshot())
        with pytest.raises(ValueError):
            StrictObserver(src, quiet_window_seconds=-1)

    def test_poll_interval_negative_rejected(self) -> None:
        src = _StaticSource(_qualifying_snapshot())
        with pytest.raises(ValueError):
            StrictObserver(src, quiet_window_seconds=1, poll_interval_seconds=0)

    def test_clean_state_completes_quiet_window(self) -> None:
        src = _StaticSource(_qualifying_snapshot())
        obs = StrictObserver(src, quiet_window_seconds=0, poll_interval_seconds=0.001, max_iterations=3)
        log, complete, duration = obs.observe("a" * 64)
        assert complete is True
        assert len(log.observations) >= 1

    def test_data_source_error_recorded_as_not_qualifying(self) -> None:
        class _BrokenSource(ObservationDataSource):
            def fetch(self, expected_head: str) -> dict:
                raise OSError("API down")
        src = _BrokenSource()
        obs = StrictObserver(src, quiet_window_seconds=0, poll_interval_seconds=0.001, max_iterations=3)
        log, complete, _ = obs.observe("a" * 64)
        assert not complete
        assert log.observations[0].qualifying is False
        assert "API down" in (log.observations[0].api_failure or "")

    def test_unresolved_thread_resets_interval(self) -> None:
        snap = _qualifying_snapshot()
        snap["threads_unresolved_current"] = 0
        src = _StaticSource(snap)
        obs = StrictObserver(
            src,
            quiet_window_seconds=1,
            max_iterations=3,
            poll_interval_seconds=0.001,
        )
        log, complete, _ = obs.observe("a" * 64)
        # All 3 observations qualify but the duration is too short. The
        # observer must NOT complete.
        assert complete is False

    def test_stale_head_disqualifies(self) -> None:
        snap = _qualifying_snapshot(head="z" * 64)
        src = _StaticSource(snap)
        obs = StrictObserver(src, quiet_window_seconds=0, poll_interval_seconds=0.001, max_iterations=3)
        log, complete, _ = obs.observe("a" * 64)
        assert not complete
        assert not log.observations[0].qualifying

    def test_heartbeat_missing_disqualifies(self) -> None:
        snap = _qualifying_snapshot()
        snap["heartbeat"] = False
        src = _StaticSource(snap)
        obs = StrictObserver(src, quiet_window_seconds=0, poll_interval_seconds=0.001, max_iterations=3)
        log, complete, _ = obs.observe("a" * 64)
        assert not complete

    def test_api_failure_disqualifies(self) -> None:
        snap = _qualifying_snapshot()
        snap["api_failure"] = "rate limited"
        src = _StaticSource(snap)
        obs = StrictObserver(src, quiet_window_seconds=0, poll_interval_seconds=0.001, max_iterations=3)
        log, complete, _ = obs.observe("a" * 64)
        assert not complete

    def test_watcher_oscillation_resets(self) -> None:
        """An oscillation: qualifying -> not -> qualifying should reset."""
        state = {"call": 0}
        class _WigglingSource(ObservationDataSource):
            def fetch(self, expected_head: str) -> dict:
                state["call"] += 1
                s = _qualifying_snapshot()
                if state["call"] == 2:
                    s["head_ok"] = False
                return s
        # Build the StrictObserver with the wiggling source so the
        # observer actually sees the bad observation.
        obs = StrictObserver(
            _WigglingSource(),
            quiet_window_seconds=1,
            max_iterations=4,
            poll_interval_seconds=0.001,
        )
        log, complete, _ = obs.observe("a" * 64)
        # Must not complete because the second observation was bad.
        assert not complete
        assert log.observations[1].qualifying is False


# === No embedded constants ===
class TestObserverHasNoEmbeddedConstants:
    def test_observer_module_has_no_pr_number(self) -> None:
        import autocoder_orchestration.observer as m
        src = open(m.__file__).read()
        # No literals for PR number
        for forbidden in ["PR #2", "PR #1", "pr_number=2", "pr_number = 2", "PR/2"]:
            assert forbidden not in src

    def test_observer_module_has_no_repo_name(self) -> None:
        import autocoder_orchestration.observer as m
        src = open(m.__file__).read()
        for forbidden in ["Slideshow11", "AutoDev", "Automated-Edge-Discovery"]:
            assert forbidden not in src


# === Observation log ===
class TestObservationLog:
    def test_append_and_jsonl(self) -> None:
        log = ObservationLog()
        log.append(Observation(
            ts_utc="2026-08-05T22:00:00Z",
            ts_monotonic=1.0,
            qualifying=True,
            qualification_reason="qualifying",
            pr_head_sha="a" * 64,
            head_ok=True,
            all_ci_pass=True,
            coderabbit_pass=True,
            threads_unresolved_current=0,
            threads_unresolved_outdated=0,
            worker_lease_active=False,
            unconsumed_event_count=0,
            provider_in_progress=False,
            supervisor_pid=1,
            process_start_identity="x",
            heartbeat=True,
            api_failure=None,
            parse_failure=None,
            fallback_success=False,
        ))
        jsonl = log.to_jsonl()
        restored = ObservationLog.from_jsonl(jsonl)
        assert len(restored.observations) == 1
        assert restored.observations[0].qualifying is True
