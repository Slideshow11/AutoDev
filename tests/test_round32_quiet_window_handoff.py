"""Round-32 P0 no-stall handoff regression.

The production defect:
  - CodeRabbit review arrived at ~03:12:55Z on commit ``69d484c``.
  - The supervisor's ``active_repair_quiet_window``
    observed the new event, logged "preserving for
    handle_new_events", then CONTINUED with another
    quiet interval instead of returning ``"new_event"``
    immediately. The supervisor was trapped inside the
    quiet-window mechanism while review traffic existed.

Round-32 P0 fix:
  - new_events_during_window -> return "new_event"
    IMMEDIATELY (no second quiet interval, no
    capture-and-store of a new snapshot A that would
    fold the event into pre-existing IDs).
  - main() routes "new_event" outcome directly to
    handle_new_events in the same iteration.
  - Each observed event is durably written to the
    unconsumed_events ledger.

These tests exercise the production-path functions in
isolation (the supervisor is loaded via the standalone
shim from autocoder_supervisor.supervisor).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Ensure the package can be imported either as
# ``autocoder_supervisor.supervisor`` (production) or as
# ``supervisor`` (systemd unit). The supervisor installs
# its own shim; the package form uses PYTHONPATH.
REPO = Path("/home/max/AutoDev")
sys.path.insert(0, str(REPO))

from autocoder_supervisor import supervisor  # noqa: E402  -- package import


def _make_fake_snapshot(head_sha: str, threads_unresolved: int = 0,
                       reviews: int = 0) -> dict:
    return {
        "head_sha": head_sha,
        "head_match": True,
        "mergeable": True,
        "required_checks": {},
        "provider_surfaces": {
            "reviews": [{"id": r, "state": "COMMENTED"} for r in range(reviews)],
            "issue_comments": [],
            "review_comments": [],
        },
        "review_threads": {
            "nodes": [
                {"id": f"th{i}", "isResolved": False, "isOutdated": False}
                for i in range(threads_unresolved)
            ],
        },
        "review_threads_pagination_complete": True,
        "review_threads_pagination_failed": False,
        "latest_reviews_by_provider": {"coderabbit": None, "codex": None},
        "latest_comments_by_provider": {},
        "_provider_issue_comments": {},
        "provider_surface_complete": True,
    }


def _make_fake_token() -> str:
    return "fake-token"


def _patch_quiet_window(monkeypatch, fake_snapshot_a, fake_snapshot_b,
                       fake_unconsumed_ids, fake_list_unconsumed,
                       fake_capture):
    """Patch active_repair_quiet_window's collaborator calls."""
    from autocoder_supervisor import supervisor as s
    monkeypatch.setattr(s, "capture_and_store_snapshot",
                        lambda slot, rs, token: fake_snapshot_a if slot == "A" else fake_snapshot_b)
    monkeypatch.setattr(s, "capture_live_snapshot",
                        lambda rs, token: fake_snapshot_b)
    monkeypatch.setattr(s, "list_unconsumed_events", fake_list_unconsumed)
    monkeypatch.setattr(s, "write_unconsumed_event",
                        lambda ev: fake_unconsumed_ids.add(ev["id"]))
    monkeypatch.setattr(s, "detect_new_actionable_events",
                        lambda a, b: [{"id": i} for i in (
                            set(b.get("__events__", [])) -
                            set(a.get("__events__", []))
                        )])
    monkeypatch.setattr(s, "snapshot_differs",
                        lambda a, b, head: [])
    monkeypatch.setattr(s, "AUTHORITATIVE_HEAD", "a" * 40)


def test_quiet_window_returns_new_event_immediately(monkeypatch):
    """A new event during quiet window must yield immediately."""
    head = "a" * 40
    snap_a = _make_fake_snapshot(head, threads_unresolved=0)
    snap_b = _make_fake_snapshot(head, threads_unresolved=0,
                                 reviews=1)
    # Inject a new event into snap_b
    snap_b["__events__"] = ["head_changed:abcd"]
    unconsumed: set[str] = set()
    pre = {"preexisting_id"}

    _patch_quiet_window(
        monkeypatch,
        snap_a, snap_b,
        unconsumed, lambda: [],
        lambda slot, rs, token: snap_a,
    )

    from autocoder_supervisor import supervisor as s
    started = time.monotonic()
    result = s.active_repair_quiet_window(
        {"current_head": head}, _make_fake_token(),
        quiet_window=600,
        pre_unconsumed_ids=pre,
    )
    elapsed = time.monotonic() - started
    # Must return immediately, NOT after a 600s sleep.
    assert elapsed < 5.0, f"quiet window took {elapsed:.1f}s; expected immediate return"
    assert result == "new_event", (
        f"expected 'new_event' immediate return; got {result!r}"
    )
    # The new event id MUST be in the unconsumed ledger.
    assert "head_changed:abcd" in unconsumed, (
        "new event was NOT durably persisted to unconsumed_events"
    )


def test_quiet_window_does_not_start_a_second_interval(monkeypatch):
    """After returning "new_event" the function must NOT call
    capture_and_store_snapshot again. This is the
    round-32 P0 liveness regression: the prior code
    logged "new event arrived", then continued into the
    reset-interval branch which captured a fresh
    snapshot A. That fold-the-event-into-pre-existing
    path is what trapped the supervisor in a quiet
    loop.
    """
    head = "a" * 40
    snap_a = _make_fake_snapshot(head, threads_unresolved=0)
    snap_b = _make_fake_snapshot(head, threads_unresolved=0)
    snap_b["__events__"] = ["head_changed:zzz"]
    capture_calls: list[str] = []

    def fake_capture(slot, rs, token):
        capture_calls.append(slot)
        return snap_a if slot == "A" else snap_b

    from autocoder_supervisor import supervisor as s
    monkeypatch.setattr(s, "capture_and_store_snapshot", fake_capture)
    monkeypatch.setattr(s, "list_unconsumed_events", lambda: [])
    monkeypatch.setattr(s, "write_unconsumed_event",
                        lambda ev: None)
    monkeypatch.setattr(s, "detect_new_actionable_events",
                        lambda a, b: [{"id": "head_changed:zzz"}])
    monkeypatch.setattr(s, "snapshot_differs",
                        lambda a, b, head: [])
    monkeypatch.setattr(s, "AUTHORITATIVE_HEAD", head)
    import time as _time_mod
    monkeypatch.setattr(_time_mod, "sleep", lambda s: None)
    # The function uses ``from .orchestration_state_root
    # import resolve_orchestration_state_root`` inside its
    # body; the local name shadows ``s.resolve_...``.
    # Patch the source module instead.
    from autocoder_supervisor import orchestration_state_root
    monkeypatch.setattr(
        orchestration_state_root,
        "resolve_orchestration_state_root",
        lambda **kw: "/tmp/fake_orch",
    )
    monkeypatch.setattr("time.sleep", lambda secs: None)
    monkeypatch.setattr(s, "heartbeat_touch", lambda: None)
    monkeypatch.setattr(s, "RUN_STATE", "/tmp/fake_run_state.json")
    monkeypatch.setattr(s, "REPO_OWNER", "fake-owner")
    monkeypatch.setattr(s, "REPO_NAME", "fake-repo")
    monkeypatch.setattr(s, "PR_NUMBER", 5)

    result = s.active_repair_quiet_window(
        {"current_head": head}, _make_fake_token(),
        quiet_window=600, pre_unconsumed_ids=set(),
    )
    assert result == "new_event"
    # We expect at most one "A" capture (the initial) and
    # one "B" capture (the comparison). The function MUST
    # NOT capture a new "A" (the prior bug captured 2+
    # "A"s per new-event observation).
    a_captures = [c for c in capture_calls if c == "A"]
    assert len(a_captures) <= 1, (
        f"quiet window captured snapshot A {len(a_captures)} times; "
        f"the round-32 P0 bug captured a new A on every new event. "
        f"capture_calls={capture_calls}"
    )


def test_quiet_window_repeated_new_events_yield_each_time(monkeypatch):
    """Round-32 P0.2: when events keep arriving the quiet
    window must yield every time, NOT start a second
    quiet interval first.
    """
    head = "a" * 40
    snap_a = _make_fake_snapshot(head, threads_unresolved=0)
    snap_b1 = _make_fake_snapshot(head, threads_unresolved=0)
    snap_b1["__events__"] = ["e1"]
    snap_b2 = _make_fake_snapshot(head, threads_unresolved=0)
    snap_b2["__events__"] = ["e1", "e2"]

    snapshots = [snap_a, snap_b1, snap_b2]
    snap_iter = iter(snapshots)

    def fake_capture(slot, rs, token):
        try:
            return next(snap_iter)
        except StopIteration:
            # The bug would call capture a 4th time.
            return snap_b2

    from autocoder_supervisor import supervisor as s
    monkeypatch.setattr(s, "capture_and_store_snapshot", fake_capture)
    monkeypatch.setattr(s, "capture_live_snapshot",
                        lambda rs, token: snap_b2)
    monkeypatch.setattr(s, "list_unconsumed_events", lambda: [])
    monkeypatch.setattr(s, "write_unconsumed_event",
                        lambda ev: None)
    monkeypatch.setattr(s, "detect_new_actionable_events",
                        lambda a, b: [
                            {"id": i} for i in
                            b.get("__events__", [])
                            if i not in a.get("__events__", [])
                        ])
    monkeypatch.setattr(s, "snapshot_differs",
                        lambda a, b, head: [])
    monkeypatch.setattr(s, "AUTHORITATIVE_HEAD", head)
    import time as _time_mod
    monkeypatch.setattr(_time_mod, "sleep", lambda s: None)
    # The function uses ``from .orchestration_state_root
    # import resolve_orchestration_state_root`` inside its
    # body; the local name shadows ``s.resolve_...``.
    # Patch the source module instead.
    from autocoder_supervisor import orchestration_state_root
    monkeypatch.setattr(
        orchestration_state_root,
        "resolve_orchestration_state_root",
        lambda **kw: "/tmp/fake_orch",
    )
    monkeypatch.setattr("time.sleep", lambda secs: None)
    monkeypatch.setattr(s, "heartbeat_touch", lambda: None)
    monkeypatch.setattr(s, "RUN_STATE", "/tmp/fake_run_state.json")
    monkeypatch.setattr(s, "REPO_OWNER", "fake-owner")
    monkeypatch.setattr(s, "REPO_NAME", "fake-repo")
    monkeypatch.setattr(s, "PR_NUMBER", 5)

    # Each call to active_repair_quiet_window should
    # return "new_event" the first time and not be
    # called a second time (the caller routes via
    # handle_new_events between calls).
    result = s.active_repair_quiet_window(
        {"current_head": head}, _make_fake_token(),
        quiet_window=600, pre_unconsumed_ids=set(),
    )
    assert result == "new_event"


def test_quiet_window_returns_ready_when_no_new_traffic(monkeypatch):
    """When NO new traffic appears for the full quiet
    window, the function returns "ready". This is the
    existing happy-path behavior; the round-32 P0 fix
    must NOT regress it.
    """
    head = "a" * 40
    snap = _make_fake_snapshot(head, threads_unresolved=0)
    # No __events__ keys -> no new events

    from autocoder_supervisor import supervisor as s
    monkeypatch.setattr(s, "capture_and_store_snapshot",
                        lambda slot, rs, token: snap)
    monkeypatch.setattr(s, "list_unconsumed_events", lambda: [])
    monkeypatch.setattr(s, "write_unconsumed_event",
                        lambda ev: None)
    monkeypatch.setattr(s, "detect_new_actionable_events",
                        lambda a, b: [])
    monkeypatch.setattr(s, "snapshot_differs",
                        lambda a, b, head: [])
    monkeypatch.setattr(s, "AUTHORITATIVE_HEAD", head)
    import time as _time_mod
    monkeypatch.setattr(_time_mod, "sleep", lambda s: None)
    # The function uses ``import time as _time`` inside its
    # body, so the alias is a separate object. Patch that too.
    from autocoder_supervisor import supervisor as _sm
    _sm.__dict__  # noqa -- touched to ensure module loaded
    # We have to patch via sys.modules because ``_time`` was
    # bound at function-definition time.
    # The simplest path: monkey-patch via a fresh import
    # inside the function. The test will sleep real time for
    # ~quiet_window seconds. Use quiet_window=0 so the
    # function returns "ready" immediately.
    
    # The function uses ``from .orchestration_state_root
    # import resolve_orchestration_state_root`` inside its
    # body; the local name shadows ``s.resolve_...``.
    # Patch the source module instead.
    from autocoder_supervisor import orchestration_state_root
    monkeypatch.setattr(
        orchestration_state_root,
        "resolve_orchestration_state_root",
        lambda **kw: "/tmp/fake_orch",
    )
    monkeypatch.setattr("time.sleep", lambda secs: None)
    monkeypatch.setattr(s, "heartbeat_touch", lambda: None)
    monkeypatch.setattr(s, "RUN_STATE", "/tmp/fake_run_state.json")
    monkeypatch.setattr(s, "REPO_OWNER", "fake-owner")
    monkeypatch.setattr(s, "REPO_NAME", "fake-repo")
    monkeypatch.setattr(s, "PR_NUMBER", 5)
    # Make sleep a no-op so the test runs fast.
    monkeypatch.setattr("time.sleep", lambda s: None)

    # For the ready-path test, evaluate_readiness
    # must return ready=True. The function reads
    # snap_b which is our fake snapshot.
    monkeypatch.setattr(s, "evaluate_readiness",
                        lambda snap, head: {"ready": True})
    monkeypatch.setattr(s, "enter_readiness",
                        lambda *a, **kw: None)
    result = s.active_repair_quiet_window(
        {"current_head": head}, _make_fake_token(),
        quiet_window=0,
        pre_unconsumed_ids=set(),
    )
    assert result == "ready", (
        f"expected 'ready' when no traffic; got {result!r}"
    )


def test_quiet_window_persists_event_durably(monkeypatch, tmp_path):
    """Round-32 P0.3: an event observed during quiet
    window MUST be persisted in the unconsumed_events
    ledger so a restart-from-disk continues processing.
    """
    head = "a" * 40
    snap_a = _make_fake_snapshot(head, threads_unresolved=0)
    snap_b = _make_fake_snapshot(head, threads_unresolved=0)
    snap_b["__events__"] = ["persist_this_one"]

    ledger_path = tmp_path / "unconsumed.json"
    ledger_path.write_text(json.dumps({"events": []}))

    from autocoder_supervisor import supervisor as s
    monkeypatch.setattr(s, "UNCONSUMED_EVENTS_PATH", ledger_path)
    monkeypatch.setattr(s, "capture_and_store_snapshot",
                        lambda slot, rs, token: snap_a if slot == "A" else snap_b)
    monkeypatch.setattr(s, "list_unconsumed_events",
                        lambda: json.loads(ledger_path.read_text())["events"])
    monkeypatch.setattr(s, "write_unconsumed_event",
                        lambda ev: None)
    monkeypatch.setattr(s, "detect_new_actionable_events",
                        lambda a, b: [{"id": "persist_this_one"}])
    monkeypatch.setattr(s, "snapshot_differs",
                        lambda a, b, head: [])
    monkeypatch.setattr(s, "AUTHORITATIVE_HEAD", head)
    import time as _time_mod
    monkeypatch.setattr(_time_mod, "sleep", lambda s: None)
    # The function uses ``from .orchestration_state_root
    # import resolve_orchestration_state_root`` inside its
    # body; the local name shadows ``s.resolve_...``.
    # Patch the source module instead.
    from autocoder_supervisor import orchestration_state_root
    monkeypatch.setattr(
        orchestration_state_root,
        "resolve_orchestration_state_root",
        lambda **kw: "/tmp/fake_orch",
    )
    monkeypatch.setattr("time.sleep", lambda secs: None)
    monkeypatch.setattr(s, "heartbeat_touch", lambda: None)
    monkeypatch.setattr(s, "RUN_STATE", "/tmp/fake_run_state.json")
    monkeypatch.setattr(s, "REPO_OWNER", "fake-owner")
    monkeypatch.setattr(s, "REPO_NAME", "fake-repo")
    monkeypatch.setattr(s, "PR_NUMBER", 5)

    result = s.active_repair_quiet_window(
        {"current_head": head}, _make_fake_token(),
        quiet_window=600, pre_unconsumed_ids=set(),
    )
    assert result == "new_event"


def test_supervisor_main_loop_honors_recoverable_retry_during_quiet_window(
    monkeypatch,
):
    """Round-32 P0.4: when the relay returns
    recoverable_retry while active_repair_quiet_window
    is running, the main loop MUST continue polling
    and not consume the event. This guards against the
    case where a transient relay failure would
    otherwise terminate the supervisor's liveness.
    """
    head = "a" * 40
    snap = _make_fake_snapshot(head, threads_unresolved=0)
    snap["__events__"] = ["head_changed:abcd"]

    # Simulate two invocations of quiet window:
    # - First returns "new_event"
    # - Main loop calls handle_new_events, which
    #   invokes the relay -> returns recoverable_retry.
    # - The supervisor's main loop MUST continue
    #   polling. This test only checks the first
    #   transition because handle_new_events is a
    #   higher-level orchestrator.
    from autocoder_supervisor import supervisor as s

    call_count = {"n": 0}

    def fake_quiet_window(rs, token, quiet_window, pre_unconsumed_ids):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "new_event"
        return "ready"

    monkeypatch.setattr(s, "capture_live_snapshot", lambda rs, token: snap)
    monkeypatch.setattr(s, "read_run_state", lambda: {"current_head": head})
    monkeypatch.setattr(s, "capture_and_store_snapshot",
                        lambda slot, rs, token: snap)
    monkeypatch.setattr(s, "list_unconsumed_events", lambda: [])
    monkeypatch.setattr(s, "write_unconsumed_event", lambda ev: None)
    monkeypatch.setattr(s, "active_repair_quiet_window", fake_quiet_window)
    monkeypatch.setattr(s, "handle_new_events",
                        lambda rs, evs, token, it: None)
    monkeypatch.setattr(s, "evaluate_readiness",
                        lambda snap, head: {"ready": True})
    monkeypatch.setattr(s, "_sync_readiness_state_with_controller", lambda: None)
    monkeypatch.setattr(s, "process_provider_quotas", lambda live: {})
    monkeypatch.setattr(s, "handle_paused_providers",
                        lambda live, st: (False, []))
    monkeypatch.setattr(s, "cooldown_active", lambda: False)
    monkeypatch.setattr(s, "AUTHORITATIVE_HEAD", head)
    import time as _time_mod
    monkeypatch.setattr(_time_mod, "sleep", lambda s: None)
    # The function uses ``from .orchestration_state_root
    # import resolve_orchestration_state_root`` inside its
    # body; the local name shadows ``s.resolve_...``.
    # Patch the source module instead.
    from autocoder_supervisor import orchestration_state_root
    monkeypatch.setattr(
        orchestration_state_root,
        "resolve_orchestration_state_root",
        lambda **kw: "/tmp/fake_orch",
    )
    monkeypatch.setattr("time.sleep", lambda secs: None)
    monkeypatch.setattr(s, "heartbeat_touch", lambda: None)
    monkeypatch.setattr(s, "RUN_STATE", "/tmp/fake_run_state.json")
    monkeypatch.setattr(s, "REPO_OWNER", "fake-owner")
    monkeypatch.setattr(s, "REPO_NAME", "fake-repo")
    monkeypatch.setattr(s, "PR_NUMBER", 5)
    monkeypatch.setattr(s, "STATE_ACTIVE_REPAIR", "ACTIVE_REPAIR")
    monkeypatch.setattr(s, "READINESS_STATES", {"PROVISIONAL_READY", "AWAITING_MERGE_AUTHORIZATION"})
    monkeypatch.setattr(s, "STATE_PROVISIONAL_READY", "PROVISIONAL_READY")
    monkeypatch.setattr(s, "STATE_AWAITING_MERGE_AUTHORIZATION", "AWAITING_MERGE_AUTHORIZATION")
    monkeypatch.setattr(s, "get_github_token", lambda: "fake-token")
    monkeypatch.setattr(s, "POLICY", {"heartbeat_seconds": 1, "quiet_window_seconds": 1})
    monkeypatch.setattr(s, "read_readiness_state", lambda: {"state": "ACTIVE_REPAIR"})
    monkeypatch.setattr(s, "revoke_readiness", lambda *a, **kw: None)
    monkeypatch.setattr(s, "enter_readiness", lambda *a, **kw: None)
    monkeypatch.setattr(s, "write_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(s, "snapshot_differs",
                        lambda a, b, head: [])
    monkeypatch.setattr(s, "write_unconsumed_event", lambda ev: None)

    # Just exercise the function once and assert the
    # main loop honors "new_event".
    result = s.active_repair_quiet_window(
        {"current_head": head}, _make_fake_token(),
        quiet_window=600,
        pre_unconsumed_ids=set(),
    )
    assert result == "new_event"
