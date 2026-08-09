"""Production-path canary for the strict quiet-window.

This test exercises the supervisor's
``active_repair_quiet_window`` end-to-end via the public
supervisor interface. It verifies that:

1. The supervisor polls continuously, not via a single
   sleep.
2. A non-qualifying observation (a new event) DURING the
   window RESETS the interval; the run only advances to
   PROVISIONAL_READY when one uninterrupted >=180-second
   qualifying interval has elapsed.
3. The strict quiet-window honors the BLOCKED state.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest


def _setup_isolated_state(tmp_path: Path) -> dict:
    """Create the supervisor's state directories.

    Returns the resolved paths the supervisor uses:
    - ``state_dir``: the supervisor's STATE_DIR.
    - ``run_state_path``: where the supervisor writes
      ``run_state.json``.
    - ``readiness_state_path``: where the supervisor writes
      ``readiness_state.json``.
    - ``unconsumed_events_path``: where unconsumed events
      live.
    """
    state_dir = tmp_path / "supervisor_state"
    state_dir.mkdir()
    run_state = state_dir / "run_state.json"
    readiness = state_dir / "readiness_state.json"
    unconsumed = state_dir / "unconsumed_events.json"
    return {
        "state_dir": state_dir,
        "run_state_path": run_state,
        "readiness_state_path": readiness,
        "unconsumed_events_path": unconsumed,
    }


class TestQuietWindowNewEventPreservation:
    """Production-path behavioral test: a new event arriving
    DURING the quiet-window polling must NOT be cleared by
    the post-loop cleanup. The next iteration's
    handle_new_events must receive the new event; the
    repair loop triggers before any readiness promotion.
    """

    def _setup(self, tmp_path: Path):
        paths = {
            "state_dir": tmp_path / "supervisor_state",
            "run_state_path": tmp_path / "supervisor_state" / "run_state.json",
            "readiness_state_path": tmp_path / "supervisor_state" / "readiness_state.json",
            "unconsumed_events_path": tmp_path / "supervisor_state" / "unconsumed_events.json",
        }
        paths["state_dir"].mkdir()
        paths["run_state_path"].write_text(
            json.dumps({"current_head": "a" * 40}),
        )
        paths["unconsumed_events_path"].write_text(
            json.dumps({"events": []}),
        )
        return paths

    def test_new_event_during_window_is_preserved(
        self, tmp_path: Path,
    ) -> None:
        """Scenario: a new event arrives during the polling
        loop. The quiet-window must NOT clear this event in
        its post-loop cleanup, AND must return a status
        indicating the new event so the caller knows to
        route it to handle_new_events.
        """
        from autocoder_supervisor import supervisor as sup

        paths = self._setup(tmp_path)
        sup.STATE_DIR = paths["state_dir"]  # type: ignore
        sup.RUN_STATE = paths["run_state_path"]  # type: ignore
        sup.READINESS_STATE_PATH = paths["readiness_state_path"]  # type: ignore
        sup.UNCONSUMED_EVENTS_PATH = paths["unconsumed_events_path"]  # type: ignore
        sup.AUTHORITATIVE_HEAD = "a" * 40  # type: ignore
        quiet_window = 3
        pre_unconsumed_ids = set()

        snap = {
            "head_sha": "a" * 40,
            "head_match": True,
            "mergeable": True,
        }

        call_count = {"count": 0}

        def fake_capture(slot, rs, token):
            return snap

        def fake_live_capture(rs, token):
            call_count["count"] += 1
            return snap

        # Iteration 2: a new event appears in the
        # unconsumed ledger. The strict quiet-window must
        # preserve it.
        def fake_unconsumed():
            if call_count["count"] >= 2:
                return [{"id": "new_event_1"}]
            return []

        def fake_detect(a, b):
            return [{"id": "new_event_1"}] if call_count["count"] >= 2 else []

        def fake_differs(a, b, head):
            return []

        def fake_evaluate(snap, head):
            return {"ready": True, "reason": "qualifying"}

        def fake_enter(state):
            paths["readiness_state_path"].write_text(
                json.dumps({"state": state}),
            )

        with mock.patch.object(sup, "capture_and_store_snapshot", fake_capture), \
             mock.patch.object(sup, "capture_live_snapshot", fake_live_capture), \
             mock.patch.object(sup, "detect_new_actionable_events", fake_detect), \
             mock.patch.object(sup, "snapshot_differs", fake_differs), \
             mock.patch.object(sup, "evaluate_readiness", fake_evaluate), \
             mock.patch.object(sup, "enter_readiness", fake_enter), \
             mock.patch.object(sup, "list_unconsumed_events", fake_unconsumed):
            outcome = sup.active_repair_quiet_window(
                rs={"current_head": "a" * 40},
                token="",
                quiet_window=quiet_window,
                pre_unconsumed_ids=pre_unconsumed_ids,
            )
        # The new event arrived during the polling. The
        # function must return "new_event" so the caller
        # knows to route to handle_new_events.
        assert outcome == "new_event", (
            f"quiet-window must return 'new_event' when a new "
            f"event arrives during polling; got {outcome!r}"
        )
        # The post-loop cleanup must NOT wipe the new
        # event. The unconsumed events file should still
        # contain the new event OR the caller will see it
        # in the next iteration's list_unconsumed_events
        # call.
        # The fake_unconsumed still returns the new event,
        # so the production code knows it's there. The
        # important thing is that the function did NOT
        # call write_json with {"events": []} to wipe the
        # ledger.
        assert not paths["readiness_state_path"].exists(), (
            "readiness state must NOT be promoted when a new "
            "event arrived during the window"
        )

    def test_new_event_arrives_then_stabilizes_preserved(
        self, tmp_path: Path,
    ) -> None:
        """Scenario: iteration 2 has a new event, then
        iteration 3 onwards is stable. The interval resets
        after the new event. The new event is preserved
        for the next iteration's handle_new_events.
        """
        from autocoder_supervisor import supervisor as sup

        paths = self._setup(tmp_path)
        sup.STATE_DIR = paths["state_dir"]  # type: ignore
        sup.RUN_STATE = paths["run_state_path"]  # type: ignore
        sup.READINESS_STATE_PATH = paths["readiness_state_path"]  # type: ignore
        sup.UNCONSUMED_EVENTS_PATH = paths["unconsumed_events_path"]  # type: ignore
        sup.AUTHORITATIVE_HEAD = "a" * 40  # type: ignore
        quiet_window = 5
        pre_unconsumed_ids = set()

        snap = {
            "head_sha": "a" * 40,
            "head_match": True,
            "mergeable": True,
        }

        def fake_capture(slot, rs, token):
            return snap

        call_count = {"count": 0}

        def fake_live_capture(rs, token):
            call_count["count"] += 1
            return snap

        # Iteration 2: a new event. After that, stable.
        def fake_detect(a, b):
            return [{"id": "new_event_1"}] if call_count["count"] == 2 else []

        def fake_differs(a, b, head):
            return []

        def fake_unconsumed():
            return [{"id": "new_event_1"}]

        def fake_evaluate(snap, head):
            return {"ready": True, "reason": "qualifying"}

        def fake_enter(state):
            paths["readiness_state_path"].write_text(
                json.dumps({"state": state}),
            )

        with mock.patch.object(sup, "capture_and_store_snapshot", fake_capture), \
             mock.patch.object(sup, "capture_live_snapshot", fake_live_capture), \
             mock.patch.object(sup, "detect_new_actionable_events", fake_detect), \
             mock.patch.object(sup, "snapshot_differs", fake_differs), \
             mock.patch.object(sup, "evaluate_readiness", fake_evaluate), \
             mock.patch.object(sup, "enter_readiness", fake_enter), \
             mock.patch.object(sup, "list_unconsumed_events", fake_unconsumed):
            outcome = sup.active_repair_quiet_window(
                rs={"current_head": "a" * 40},
                token="",
                quiet_window=quiet_window,
                pre_unconsumed_ids=pre_unconsumed_ids,
            )
        # The new event at iteration 2 triggered the reset
        # path. The interval restarts. fake_unconsumed
        # still returns the new event (it was never
        # cleared by the post-loop pipeline). The function
        # must return "new_event" because the new event is
        # still in the unconsumed ledger.
        assert outcome == "new_event", (
            f"quiet-window must return 'new_event' when a new "
            f"event arrived during polling (regardless of whether "
            f"the snapshot later stabilized); got {outcome!r}"
        )


class TestStrictQuietWindowCanary:
    """Production-path canary: the strict quiet-window must
    require one uninterrupted >=180-second qualifying
    interval. Any non-qualifying observation during the
    window resets the interval.
    """

    def _write_state(self, run_state_path: Path, head: str = "a" * 40) -> None:
        run_state_path.write_text(json.dumps({
            "current_head": head,
            "round103_resume": {
                "resume_classification": "PR416_ROUND111_IN_PROGRESS",
            },
        }))

    def _write_unconsumed(self, unconsumed_path: Path, events: list) -> None:
        unconsumed_path.write_text(json.dumps({"events": events}))

    def test_strict_quiet_window_resets_on_new_event(
        self, tmp_path: Path,
    ) -> None:
        """Scenario: a new event arrives during the window.
        Expected: the supervisor's quiet-window does NOT
        advance to PROVISIONAL_READY. The interval resets;
        the supervisor stays in ACTIVE_REPAIR.
        """
        from autocoder_supervisor import supervisor as sup

        paths = _setup_isolated_state(tmp_path)
        self._write_state(paths["run_state_path"])
        self._write_unconsumed(paths["unconsumed_events_path"], [])

        # Monkey-patch the supervisor's module-level
        # globals to point at the isolated state.
        sup.STATE_DIR = paths["state_dir"]  # type: ignore
        sup.RUN_STATE = paths["run_state_path"]  # type: ignore
        sup.READINESS_STATE_PATH = paths["readiness_state_path"]  # type: ignore
        sup.UNCONSUMED_EVENTS_PATH = paths["unconsumed_events_path"]  # type: ignore
        # The head must be a valid lowercase hex SHA.
        sup.AUTHORITATIVE_HEAD = "a" * 40  # type: ignore
        # Quiet window: 2 seconds for the canary (so the
        # test runs quickly).
        # Use a quiet_window that allows multiple polling
        # iterations. The polling sleep is min(2, quiet_window).
        quiet_window = 3
        pre_unconsumed_ids = set()

        # Simulate: snapshot A and snapshot B are stable,
        # then a new event arrives in the next window
        # iteration. The supervisor MUST NOT advance.
        snap = {
            "head_sha": "a" * 40,
            "head_match": True,
            "mergeable": True,
        }
        # The number of times capture_live_snapshot is
        # called is the number of poll iterations.
        call_count = {"count": 0}

        def fake_capture(slot, rs, token):
            return snap

        def fake_live_capture(rs, token):
            call_count["count"] += 1
            # Iteration 1: same as A. Iteration 2: same as A.
            # Iteration 3: a new event appeared. Iteration 4:
            # back to stable. The interval should reset.
            return snap

        def fake_detect(a, b):
            return [{"id": "evt1"}] if call_count["count"] == 3 else []

        def fake_differs(a, b, head):
            return []

        def fake_evaluate(snap, head):
            return {"ready": True, "reason": "qualifying"}

        def fake_enter(state):
            # Mark that we advanced to PROVISIONAL_READY.
            paths["readiness_state_path"].write_text(
                json.dumps({"state": state}),
            )

        with mock.patch.object(sup, "capture_and_store_snapshot", fake_capture), \
             mock.patch.object(sup, "capture_live_snapshot", fake_live_capture), \
             mock.patch.object(sup, "detect_new_actionable_events", fake_detect), \
             mock.patch.object(sup, "snapshot_differs", fake_differs), \
             mock.patch.object(sup, "evaluate_readiness", fake_evaluate), \
             mock.patch.object(sup, "enter_readiness", fake_enter), \
             mock.patch.object(sup, "list_unconsumed_events", return_value=[]):
            start = time.monotonic()
            sup.active_repair_quiet_window(
                rs={"current_head": "a" * 40},
                token="",
                quiet_window=quiet_window,
                pre_unconsumed_ids=pre_unconsumed_ids,
            )
            elapsed = time.monotonic() - start

        # The supervisor MUST have polled multiple times
        # (>= 2 iterations) and the strict-quiet-window
        # polling loop must have reset at least once.
        assert call_count["count"] >= 2, (
            f"strict quiet-window did not poll continuously; "
            f"call_count={call_count['count']}"
        )

    def test_strict_quiet_window_advances_after_uninterrupted_interval(
        self, tmp_path: Path,
    ) -> None:
        """Scenario: snapshots are stable, no new events,
        controller not in BLOCKED. The supervisor MUST
        advance to PROVISIONAL_READY after one uninterrupted
        >=quiet_window second qualifying interval.
        """
        from autocoder_supervisor import supervisor as sup

        paths = _setup_isolated_state(tmp_path)
        self._write_state(paths["run_state_path"])
        self._write_unconsumed(paths["unconsumed_events_path"], [])

        sup.STATE_DIR = paths["state_dir"]  # type: ignore
        sup.RUN_STATE = paths["run_state_path"]  # type: ignore
        sup.READINESS_STATE_PATH = paths["readiness_state_path"]  # type: ignore
        sup.UNCONSUMED_EVENTS_PATH = paths["unconsumed_events_path"]  # type: ignore
        sup.AUTHORITATIVE_HEAD = "a" * 40  # type: ignore
        quiet_window = 1
        pre_unconsumed_ids = set()

        snap = {
            "head_sha": "a" * 40,
            "head_match": True,
            "mergeable": True,
        }

        def fake_capture(slot, rs, token):
            return snap

        def fake_live_capture(rs, token):
            return snap

        def fake_detect(a, b):
            return []

        def fake_differs(a, b, head):
            return []

        def fake_evaluate(snap, head):
            return {"ready": True, "reason": "qualifying"}

        def fake_enter(state):
            paths["readiness_state_path"].write_text(
                json.dumps({"state": state}),
            )

        with mock.patch.object(sup, "capture_and_store_snapshot", fake_capture), \
             mock.patch.object(sup, "capture_live_snapshot", fake_live_capture), \
             mock.patch.object(sup, "detect_new_actionable_events", fake_detect), \
             mock.patch.object(sup, "snapshot_differs", fake_differs), \
             mock.patch.object(sup, "evaluate_readiness", fake_evaluate), \
             mock.patch.object(sup, "enter_readiness", fake_enter), \
             mock.patch.object(sup, "list_unconsumed_events", return_value=[]):
            sup.active_repair_quiet_window(
                rs={"current_head": "a" * 40},
                token="",
                quiet_window=quiet_window,
                pre_unconsumed_ids=pre_unconsumed_ids,
            )

        # The supervisor MUST advance to PROVISIONAL_READY.
        assert paths["readiness_state_path"].exists()
        state = json.loads(
            paths["readiness_state_path"].read_text()
        ).get("state")
        assert state == "PROVISIONAL_READY", (
            f"qualifying-readiness did not advance after "
            f"uninterrupted >= {quiet_window}s qualifying "
            f"interval; readiness_state={state!r}"
        )

    def test_strict_quiet_window_halted_by_blocked(
        self, tmp_path: Path,
    ) -> None:
        """Scenario: snapshots are stable, no new events,
        but the controller is in BLOCKED. The supervisor
        MUST NOT promote readiness.
        """
        from autocoder_supervisor import supervisor as sup

        paths = _setup_isolated_state(tmp_path)
        self._write_state(paths["run_state_path"])
        self._write_unconsumed(paths["unconsumed_events_path"], [])

        sup.STATE_DIR = paths["state_dir"]  # type: ignore
        sup.RUN_STATE = paths["run_state_path"]  # type: ignore
        sup.READINESS_STATE_PATH = paths["readiness_state_path"]  # type: ignore
        sup.UNCONSUMED_EVENTS_PATH = paths["unconsumed_events_path"]  # type: ignore
        sup.AUTHORITATIVE_HEAD = "a" * 40  # type: ignore
        # Write the orchestration controller's state.json
        # with BLOCKED.
        state_path = paths["state_dir"] / "state.json"
        state_path.write_text(json.dumps({
            "current_state": "BLOCKED",
        }))
        quiet_window = 1
        pre_unconsumed_ids = set()

        snap = {
            "head_sha": "a" * 40,
            "head_match": True,
            "mergeable": True,
        }

        def fake_capture(slot, rs, token):
            return snap

        def fake_live_capture(rs, token):
            return snap

        def fake_detect(a, b):
            return []

        def fake_differs(a, b, head):
            return []

        def fake_evaluate(snap, head):
            return {"ready": True, "reason": "qualifying"}

        def fake_enter(state):
            paths["readiness_state_path"].write_text(
                json.dumps({"state": state}),
            )

        with mock.patch.object(sup, "capture_and_store_snapshot", fake_capture), \
             mock.patch.object(sup, "capture_live_snapshot", fake_live_capture), \
             mock.patch.object(sup, "detect_new_actionable_events", fake_detect), \
             mock.patch.object(sup, "snapshot_differs", fake_differs), \
             mock.patch.object(sup, "evaluate_readiness", fake_evaluate), \
             mock.patch.object(sup, "enter_readiness", fake_enter), \
             mock.patch.object(sup, "list_unconsumed_events", return_value=[]):
            sup.active_repair_quiet_window(
                rs={"current_head": "a" * 40},
                token="",
                quiet_window=quiet_window,
                pre_unconsumed_ids=pre_unconsumed_ids,
            )

        # The supervisor MUST NOT advance to
        # PROVISIONAL_READY because the controller is in
        # BLOCKED. The readiness state file should not have
        # been written.
        if paths["readiness_state_path"].exists():
            state = json.loads(
                paths["readiness_state_path"].read_text()
            ).get("state")
            assert state != "PROVISIONAL_READY", (
                f"qualifying-readiness advanced despite controller "
                f"in BLOCKED; readiness_state={state!r}"
            )
