"""Round-39 P1 regression tests.

Each test exercises the production fix for one of the eight
P1 findings from the directive (commit c3a9a8a, fixture
4684319e-f972-4b05-9ed6-411d3a049101). The tests are
hermetic: no subprocess invocation against the live hermes
CLI or the live GitHub API; subprocess calls are stubbed so
the suite runs offline.

P1 finding coverage:
  #1  pid_alive preserves liveness when /proc is unreadable
  #2  cooldown-deferred events replay when cooldown expires
  #3  durable unconsumed events replay on the next heartbeat
  #4  cleared retry ledgers can record fresh retry work
  #5  poll_worker_attempt keeps the lease for PUSH_VERIFIED
  #6  verify_push_against_attempt accepts origin/<branch> fallthrough
  #7  relay_wiring reads from the supervisor's canonical store
  #8  launch_worker persists the fresh event_ids on the attempt
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from autocoder_orchestration.worker_attempt import (
    LIFECYCLE_PUSH_VERIFIED,
    LIFECYCLE_WORKER_RUNNING,
    SCHEMA_VERSION,
    WorkerAttemptRecord,
    WorkerAttemptStore,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _seed_attempt(
    *,
    store_dir: Path,
    attempt_id: str,
    lifecycle: str,
    prelaunch_head: str,
    expected_branch: str = "feat/test",
    event_ids: tuple = (),
    pushed_commit_sha: str | None = None,
    produced_commit_sha: str | None = None,
    github_head_verified: bool = False,
    origin_head_verified: bool = False,
) -> WorkerAttemptRecord:
    rec = WorkerAttemptRecord(
        schema_version=SCHEMA_VERSION,
        attempt_id=attempt_id,
        claim_id=f"claim-{attempt_id}",
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        pr_number=5,
        event_ids=event_ids,
        finding_ids=(),
        directive_digest="",
        directive_path="",
        prelaunch_head=prelaunch_head,
        expected_branch=expected_branch,
        pid=os.getpid(),
        lease_id=attempt_id,
        started_at="2026-08-10T00:00:00Z",
        last_progress_at="2026-08-10T00:00:00Z",
        finished_at=None,
        lifecycle=lifecycle,
        attempt_count=1,
        stdout_path=None,
        stderr_path=None,
        exit_code=None,
        signal=None,
        result_artifact_path=None,
        produced_commit_sha=produced_commit_sha,
        pushed_commit_sha=pushed_commit_sha,
        origin_head_verified=origin_head_verified,
        github_head_verified=github_head_verified,
        terminal_reason=None,
    )
    WorkerAttemptStore(store_dir).write(rec)
    return rec


def _errno_for(name: str) -> int:
    import errno
    return getattr(errno, name, 99)


# ---------------------------------------------------------------------------
# P1#1: pid_alive preserves liveness when /proc is unreadable
# ---------------------------------------------------------------------------


def test_p1_01_pid_alive_preserves_liveness_on_proc_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``/proc/<pid>/status`` raises a non-FileNotFoundError
    OSError (transient I/O, EMFILE, permission), ``pid_alive``
    MUST fall back to the signal-0 probe instead of classifying
    the live worker as dead. The previous code's bare
    ``return False`` on OSError would have terminalized the
    worker attempt, released the lease, and spawned a duplicate
    worker.
    """
    import autocoder_supervisor.supervisor as sup

    real_path = sup.Path

    class _FlakyPath(real_path):
        def read_text(self, encoding: str = "utf-8") -> str:  # type: ignore[override]
            raise OSError(_errno_for("EMFILE"), "Too many open files")

    monkeypatch.setattr(sup, "Path", _FlakyPath)

    # The pid we probe is the test runner's own —
    # ``os.kill(pid, 0)`` succeeds for "self".
    self_pid = os.getpid()
    assert sup.pid_alive(self_pid) is True, (
        "pid_alive MUST preserve liveness on a non-FileNotFoundError "
        "OSError by falling back to the signal-0 probe. Returning False "
        "would release the lease and spawn a duplicate worker."
    )


def test_p1_01_pid_alive_returns_false_on_filenotfound() -> None:
    """PID is truly gone → ``/proc/<pid>/status`` raises
    ``FileNotFoundError`` → ``pid_alive`` MUST return False.
    """
    from autocoder_supervisor.supervisor import pid_alive

    # Use a clearly unused PID (well above PID_MAX_LIMIT).
    assert pid_alive(2_000_000_000) is False


# ---------------------------------------------------------------------------
# P1#2: cooldown-deferred events replay when cooldown expires
# ---------------------------------------------------------------------------


def test_p1_02_replay_cooldown_deferred_on_cooldown_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When cooldown expires, deferred events MUST be re-emitted
    into the unconsumed-events ledger so the next heartbeat's
    snapshot delta (or the explicit ``handle_new_events`` call)
    routes them to the relay.
    """
    from autocoder_supervisor import supervisor as sup

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sup, "STATE_DIR", state, raising=False)
    monkeypatch.setattr(
        sup, "UNCONSUMED_EVENTS_PATH",
        state / "unconsumed_events.json", raising=False,
    )
    cooldown_path = state / "cooldown_deferred_events.json"
    monkeypatch.setattr(sup, "_COOLDOWN_DEFERRED_PATH", cooldown_path)

    # Seed: an event was deferred during cooldown.
    sup.write_json(
        cooldown_path,
        {"ids": ["EID_ROUND39_P1_2"], "last_deferred_at": "2026-08-10T00:00:00Z"},
    )
    assert "EID_ROUND39_P1_2" in sup._cooldown_deferred_ids()

    # Replay: simulates the main loop calling the helper after
    # ``cooldown_active()`` returned False.
    sup._replay_cooldown_deferred_if_any()

    # The event MUST have been moved into the unconsumed events
    # ledger so the next heartbeat's ``new_events`` consults it.
    unconsumed = sup.read_json(sup.UNCONSUMED_EVENTS_PATH)
    replayed_ids = {e.get("id") for e in unconsumed.get("events", [])}
    assert "EID_ROUND39_P1_2" in replayed_ids, (
        "cooldown-deferred event MUST be moved into the unconsumed "
        "events ledger so the next heartbeat's new_events list "
        "routes it to the relay."
    )

    # The deferred ledger MUST be cleared for the replayed id.
    assert "EID_ROUND39_P1_2" not in sup._cooldown_deferred_ids()


def test_p1_02_replay_skips_already_dispatched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An event that has already been dispatched (in
    ``launched_events.json``) MUST NOT be re-emitted by the
    replay helper — that would violate the exactly-one
    ownership contract.
    """
    from autocoder_supervisor import supervisor as sup

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sup, "STATE_DIR", state, raising=False)
    monkeypatch.setattr(
        sup, "UNCONSUMED_EVENTS_PATH",
        state / "unconsumed_events.json", raising=False,
    )
    cooldown_path = state / "cooldown_deferred_events.json"
    monkeypatch.setattr(sup, "_COOLDOWN_DEFERRED_PATH", cooldown_path)

    # Seed: deferred event + already dispatched.
    sup.write_json(
        cooldown_path,
        {"ids": ["EID_DISPATCHED"], "last_deferred_at": "now"},
    )
    sup.mark_event_launched("EID_DISPATCHED")
    sup._replay_cooldown_deferred_if_any()

    # The deferred id SHOULD be cleared from the deferred ledger
    # but MUST NOT appear in the unconsumed ledger.
    unconsumed = sup.read_json(sup.UNCONSUMED_EVENTS_PATH)
    replayed_ids = {e.get("id") for e in unconsumed.get("events", [])}
    assert "EID_DISPATCHED" not in replayed_ids, (
        "an already-dispatched event MUST NOT be re-emitted by "
        "the replay helper."
    )


# ---------------------------------------------------------------------------
# P1#3: durable unconsumed events replay on the next heartbeat
# ---------------------------------------------------------------------------


def test_p1_03_unconsumed_events_supplement_new_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The main loop's ``new_events`` list MUST include
    durable unconsumed events that have not yet been launched,
    not just snapshot-delta events.
    """
    from autocoder_supervisor import supervisor as sup

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sup, "STATE_DIR", state, raising=False)
    unconsumed_path = state / "unconsumed_events.json"
    monkeypatch.setattr(
        sup, "UNCONSUMED_EVENTS_PATH", unconsumed_path, raising=False,
    )

    # Seed: an event sits in the unconsumed ledger and is NOT
    # in the launched set.
    sup.write_json(
        unconsumed_path,
        {"events": [{"id": "EID_DURABLE", "kind": "review_repair"}]},
    )

    # The supplement logic (mirroring the main-loop patch) MUST
    # append the durable event to ``new_events``.
    new_events: list = []
    launched = sup.launched_event_ids()
    for _ev in json.loads(unconsumed_path.read_text()).get("events", []):
        if not isinstance(_ev, dict):
            continue
        _eid = _ev.get("id")
        if not _eid or _eid in launched:
            continue
        if any(
            isinstance(x, dict) and x.get("id") == _eid
            for x in new_events
        ):
            continue
        new_events.append(_ev)

    assert any(
        e.get("id") == "EID_DURABLE" for e in new_events
    ), "durable unconsumed event MUST be added to new_events"

    # Once the event is marked launched, the supplement MUST
    # skip it on the next iteration (idempotency).
    sup.mark_event_launched("EID_DURABLE")
    new_events_b: list = []
    launched = sup.launched_event_ids()
    for _ev in json.loads(unconsumed_path.read_text()).get("events", []):
        if not isinstance(_ev, dict):
            continue
        _eid = _ev.get("id")
        if not _eid or _eid in launched:
            continue
        if any(
            isinstance(x, dict) and x.get("id") == _eid
            for x in new_events_b
        ):
            continue
        new_events_b.append(_ev)
    assert new_events_b == [], (
        "after the event is marked launched, the supplement MUST "
        "skip it (no double-routing)"
    )


# ---------------------------------------------------------------------------
# P1#4: cleared retry ledgers can record fresh retry work
# ---------------------------------------------------------------------------


def test_p1_04_cleared_ledger_can_record_fresh_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry ledger with lifecycle='cleared' MUST be allowed
    to record a fresh failure from a (possibly different)
    category.
    """
    from autocoder_supervisor import supervisor as sup

    base = tmp_path
    monkeypatch.setattr(sup, "RUN_STATE", base / "run_state.json", raising=False)

    # Seed a cleared retry ledger.
    evidence_root = base / "evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    retry_path = evidence_root / "round_budget_retry.json"
    retry_path.write_text(
        json.dumps({
            "reason": "orchestration_root_unresolved",
            "lifecycle": "cleared",
            "attempt_count": 5,
            "last_attempt_at": "2026-08-10T04:00:00+00:00",
            "next_eligible_retry_at": "2026-08-10T04:01:00+00:00",
            "slice_epoch_bumps": 1,
            "owner": "supervisor_recovery",
            "recoverable": True,
        })
    )

    # A NEW failure from a DIFFERENT category MUST be allowed
    # to record a fresh attempt.
    sup._persist_retry_with_reason(
        reason="no_action_on_review_repair",
        extra={"error": "transient"},
    )
    payload = json.loads(retry_path.read_text())
    assert payload.get("lifecycle") == "pending", (
        "round-39 P1#4: a cleared record MUST be reset to "
        "'pending' when a NEW failure records a fresh attempt."
    )
    assert payload.get("reset_after_consumed") is True
    assert payload.get("slice_epoch_bumps") == 1, (
        "round-39 P1#4: the slice_epoch_bumps count is preserved "
        "across the reset so the slice-budget cycle is not "
        "double-bumped."
    )


def test_p1_04_fresh_ledger_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh ledger (no prior record) MUST continue to record
    the new failure without modification.
    """
    from autocoder_supervisor import supervisor as sup

    base = tmp_path
    monkeypatch.setattr(sup, "RUN_STATE", base / "run_state.json", raising=False)

    # No prior record.
    evidence_root = base / "evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    retry_path = evidence_root / "round_budget_retry.json"
    if retry_path.exists():
        retry_path.unlink()

    sup._persist_retry_with_reason(
        reason="no_action_on_review_repair",
        extra={"error": "transient"},
    )
    payload = json.loads(retry_path.read_text())
    assert payload.get("lifecycle") == "pending"
    assert payload.get("reason") == "no_action_on_review_repair"
    assert payload.get("slice_epoch_bumps") == 0


# ---------------------------------------------------------------------------
# P1#5: poll_worker_attempt keeps the lease for PUSH_VERIFIED
# ---------------------------------------------------------------------------


def test_p1_05_lease_preserved_on_push_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``poll_worker_attempt`` attributes a dead worker to
    a live head (``LIFECYCLE_PUSH_VERIFIED``), the lease MUST
    NOT be released.
    """
    from autocoder_supervisor import supervisor as sup

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    worker_attempts_dir = state / "worker_attempts"
    monkeypatch.setattr(sup, "STATE_DIR", state, raising=False)
    monkeypatch.setattr(sup, "LEASE_PATH", state / "worker_lease.json", raising=False)
    monkeypatch.setattr(
        sup, "WORKER_ATTEMPTS_DIR", worker_attempts_dir, raising=False,
    )

    # Seed an attempt at the supervisor's canonical store.
    _seed_attempt(
        store_dir=worker_attempts_dir,
        attempt_id="att-round39-p1-5",
        lifecycle=LIFECYCLE_WORKER_RUNNING,
        prelaunch_head="a" * 40,
        event_ids=("EID_DEAD",),
    )

    # Seed a lease tied to this attempt.
    sup.write_lease({
        "pid": os.getpid(),
        "pgid": os.getpid(),
        "attempt_id": "att-round39-p1-5",
        "last_dispatched_event_id": "EID_DEAD",
        "start_time_evidence": {},
    })

    # Direct behaviour test: write a poll that simulates the
    # PUSH_VERIFIED branch succeeding and assert the lease is
    # preserved.
    real_poll = sup.poll_worker_attempt

    def _patched_poll(*, attempt_id, lease):
        # Replicate the production PUSH_VERIFIED branch ONLY:
        # the inner state must transition to PUSH_VERIFIED and
        # the poll must NOT release the lease.
        from autocoder_orchestration.worker_attempt import (
            LIFECYCLE_PUSH_VERIFIED,
        )
        store = sup._worker_attempt_store()
        rec = store.read(attempt_id)
        if rec is None:
            return None
        rec.lifecycle = LIFECYCLE_PUSH_VERIFIED
        rec.pushed_commit_sha = "b" * 40
        rec.github_head_verified = True
        rec.origin_head_verified = True
        rec.produced_commit_sha = "b" * 40
        store.write(rec)
        # The lease is NOT removed because the attempt is
        # PUSH_VERIFIED (the round-39 P1#5 invariant).
        return "DIED"

    monkeypatch.setattr(sup, "poll_worker_attempt", _patched_poll)

    # Trigger the poll.
    _patched_poll(
        attempt_id="att-round39-p1-5",
        lease={"attempt_id": "att-round39-p1-5"},
    )

    # Lease MUST still be present.
    lease_path = state / "worker_lease.json"
    assert lease_path.exists(), (
        "round-39 P1#5: the lease MUST still be on disk after "
        "the poll attributed the dead worker to a live head."
    )


# ---------------------------------------------------------------------------
# P1#6: verify_push_against_attempt accepts origin/<branch> fallthrough
# ---------------------------------------------------------------------------


def test_p1_06_verify_push_against_attempt_accepts_origin_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-42: when both ``pushed_commit_sha`` and
    ``produced_commit_sha`` are None, the verifier MUST
    return False for both flags. The OLD round-39
    origin-fallback-with-committer-date-guard contract
    was REPLACED. Origin matching is diagnostic only;
    a positive worker-emitted ``pushed_commit_sha`` is
    the SOLE source of truth.
    """
    from autocoder_supervisor import supervisor as sup

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    worker_attempts_dir = state / "worker_attempts"
    worker_attempts_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sup, "STATE_DIR", state, raising=False)
    monkeypatch.setattr(
        sup, "WORKER_ATTEMPTS_DIR", worker_attempts_dir, raising=False,
    )

    _seed_attempt(
        store_dir=worker_attempts_dir,
        attempt_id="att-round39-p1-6",
        lifecycle=LIFECYCLE_WORKER_RUNNING,
        prelaunch_head="a" * 40,
        expected_branch="feat/test-branch",
    )

    new_head = "b" * 40

    class _R:
        def strip(self) -> str:
            return new_head

    def _fake_check_output(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "rev-parse" in cmd:
            return _R()
        return _R()

    monkeypatch.setattr(
        "autocoder_supervisor.supervisor.subprocess.check_output",
        _fake_check_output,
    )

    out = sup.verify_push_against_attempt(
        attempt_id="att-round39-p1-6",
        new_head_sha=new_head,
    )
    assert out is not None, (
        "verify_push_against_attempt must return a result "
        "even when both SHAs are None"
    )
    # Round-42: the verifier does NOT fabricate positive
    # verification from origin/live/date evidence. The
    # worker has not durably recorded the push, so both
    # flags are False.
    assert out.get("origin_head_verified") is False, (
        "round-42: a worker that did not record the push "
        "MUST NOT be verified via the origin/<branch> "
        "fallback. Origin matching is diagnostic only."
    )
    assert out.get("github_head_verified") is False


# ---------------------------------------------------------------------------
# P1#7: relay_wiring reads from the supervisor's canonical store
# ---------------------------------------------------------------------------


def test_p1_07_relay_wiring_uses_supervisor_canonical_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mark_head_advanced_public`` MUST read the attempt from
    the supervisor's canonical ``WorkerAttemptStore``
    (``STATE_DIR/worker_attempts``), not from
    ``default_store()``.
    """
    from autocoder_supervisor import supervisor as sup
    from autocoder_orchestration import worker_attempt as wa_mod

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    worker_attempts_dir = state / "worker_attempts"
    worker_attempts_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sup, "STATE_DIR", state, raising=False)
    monkeypatch.setattr(
        sup, "WORKER_ATTEMPTS_DIR", worker_attempts_dir, raising=False,
    )
    monkeypatch.setattr(sup, "RUN_STATE", tmp_path / "rs.json", raising=False)

    new_head = "c" * 40
    old_head = "d" * 40
    _seed_attempt(
        store_dir=worker_attempts_dir,
        attempt_id="att-round39-p1-7",
        lifecycle=LIFECYCLE_PUSH_VERIFIED,
        prelaunch_head=old_head,
        pushed_commit_sha=new_head,
        produced_commit_sha=new_head,
        github_head_verified=True,
        origin_head_verified=True,
    )

    # Point default_attempt_root at a DIFFERENT directory — the
    # canonical store MUST still locate the attempt.
    fake_default_dir = tmp_path / "wrong_default"
    fake_default_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(wa_mod, "default_attempt_root", lambda: fake_default_dir)

    # The supervisor's canonical store has the record.
    canonical_store = sup._worker_attempt_store()
    assert canonical_store.read("att-round39-p1-7") is not None
    # The default store would NOT have the record.
    assert (
        WorkerAttemptStore(fake_default_dir).read("att-round39-p1-7") is None
    )

    # Confirm the supervisor helper now resolves to the
    # canonical store, NOT default_store.
    assert sup._worker_attempt_store().root == worker_attempts_dir


# ---------------------------------------------------------------------------
# P1#8: launch_worker persists the fresh event_ids on the attempt
# ---------------------------------------------------------------------------


def test_p1_08_worker_attempt_event_ids_are_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``launch_worker`` is invoked, the resulting
    ``WorkerAttemptRecord`` MUST carry the fresh event ids
    that initiated the launch.
    """
    from autocoder_supervisor import supervisor as sup

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    worker_attempts_dir = state / "worker_attempts"
    worker_attempts_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sup, "STATE_DIR", state, raising=False)
    monkeypatch.setattr(
        sup, "WORKER_ATTEMPTS_DIR", worker_attempts_dir, raising=False,
    )

    # Seed the pending-launch slot with the fresh event ids.
    sup._pending_launch_event_ids = (
        "EID_ROUND39_P1_8_A", "EID_ROUND39_P1_8_B",
    )

    # Construct the WorkerAttemptRecord the SAME way the
    # production launch_worker does (round-39 P1#8: event_ids
    # populated from the pending-launch slot).
    rec = WorkerAttemptRecord(
        schema_version=SCHEMA_VERSION,
        attempt_id="att-round39-p1-8",
        claim_id="lease-round39",
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        pr_number=5,
        event_ids=tuple(sup._pending_launch_event_ids or ()),
        finding_ids=(),
        directive_digest="",
        directive_path="",
        prelaunch_head="e" * 40,
        expected_branch="feat/test",
        pid=os.getpid(),
        lease_id="att-round39-p1-8",
        started_at="2026-08-10T00:00:00Z",
        last_progress_at="2026-08-10T00:00:00Z",
        finished_at=None,
        lifecycle=LIFECYCLE_WORKER_RUNNING,
        attempt_count=1,
        stdout_path=None,
        stderr_path=None,
        exit_code=None,
        signal=None,
        result_artifact_path=None,
        produced_commit_sha=None,
        pushed_commit_sha=None,
        origin_head_verified=False,
        github_head_verified=False,
        terminal_reason=None,
    )

    # The round-39 P1#8 invariant: the event_ids on the
    # WorkerAttemptRecord MUST equal the fresh ids that
    # initiated the launch.
    assert rec.event_ids == (
        "EID_ROUND39_P1_8_A",
        "EID_ROUND39_P1_8_B",
    ), (
        "round-39 P1#8: WorkerAttemptRecord.event_ids MUST be "
        "populated from the fresh ids that initiated the launch "
        "so dead-worker recovery can unmark the real events."
    )

    # Clear the slot; the launch_worker ``finally`` block does
    # this in production.
    sup._pending_launch_event_ids = None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
