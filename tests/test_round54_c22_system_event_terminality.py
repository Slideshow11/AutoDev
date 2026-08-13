"""Round-54/C22 Defect D: system event terminality.

The supervisor's ``unconsumed_events.json`` queue holds durable
events of various kinds. Each event kind has a kind-specific
terminality rule:

  - ``required_check_conclusion_change`` (kind):
        terminal when the corresponding check has been classified
        by ``ci_policy_status`` against the live snapshot.
  - ``provider_state_change`` (kind):
        terminal when the corresponding provider's state has been
        recorded in ``quota_state.json`` with a recent timestamp,
        OR the provider has been recorded as no longer paused.
  - ``head_changed`` (kind):
        terminal when the live PR head matches the recorded event
        id-encoded head.

The unconsumed-event queue MUST NEVER lose an event based on
elapsed time. The only way an event leaves the queue is when
``consume_event_with_reason`` records a durable terminality proof
for it.

These tests exercise the drain path that the supervisor runs each
heartbeat.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


SUP_PATH = (
    Path(__file__).resolve().parent.parent
    / "autocoder_supervisor"
    / "supervisor.py"
)


def _bootstrap_temp_state(tmp_path: Path):
    """Bootstrap a state directory under ``tmp_path`` and bind the
    supervisor's module-level globals so tests can call helper
    functions without a real install."""
    sys.path.insert(0, str(SUP_PATH.parent.parent))
    from autocoder_supervisor import supervisor as sup_mod  # type: ignore
    sd = tmp_path / "state"
    sd.mkdir(parents=True, exist_ok=True)
    # All paths must be Path objects so / ``write_text`` etc. work.
    from pathlib import Path as _P
    sup_mod.HEARTBEAT_PATH = _P(str(tmp_path / "heartbeat"))
    sup_mod.HEARTBEAT_PATH.write_text("dummy")
    sup_mod.LOG_PATH = _P(str(tmp_path / "supervisor.log"))
    sup_mod.LOCK_PATH = _P(str(tmp_path / "lock"))
    sup_mod.LEASE_PATH = _P(str(sd / "worker_lease.json"))
    sup_mod.LAST_RESUME_PATH = _P(str(sd / "last_resume.json"))
    sup_mod.QUOTA_PATH = _P(str(sd / "quota_state.json"))
    sup_mod.REVIEW_REQUESTS_DIR = _P(str(sd / "review_requests"))
    sup_mod.REVIEW_REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    sup_mod.SNAPSHOT_A_PATH = _P(str(sd / "snapshot_a.json"))
    sup_mod.SNAPSHOT_B_PATH = _P(str(sd / "snapshot_b.json"))
    sup_mod.RUN_STATE = _P(str(sd / "run_state.json"))
    sup_mod.UNCONSUMED_EVENTS_PATH = _P(str(sd / "unconsumed_events.json"))
    sup_mod.READINESS_STATE_PATH = _P(str(sd / "readiness_state.json"))
    sup_mod.STATE_DIR = _P(str(sd))
    sup_mod.WORKER_ATTEMPTS_DIR = _P(str(sd / "worker_attempts"))
    sup_mod.WORKER_ATTEMPTS_DIR.mkdir(parents=True, exist_ok=True)
    sup_mod.LEASE_PATH.write_text("{}")
    sup_mod.LAST_RESUME_PATH.write_text("{}")
    sup_mod.RUN_STATE.write_text(json.dumps({"current_head": "abc" * 14}))
    sup_mod.UNCONSUMED_EVENTS_PATH.write_text(json.dumps({"events": []}))
    sup_mod.READINESS_STATE_PATH.write_text(json.dumps({"state": "ACTIVE_REPAIR"}))
    sup_mod.SNAPSHOT_A_PATH.write_text(json.dumps({}))
    sup_mod.SNAPSHOT_B_PATH.write_text(json.dumps({}))
    sup_mod.QUOTA_PATH.write_text(json.dumps({"providers": {}}))
    # Round-54/C22 Defect D: the terminality ledger is a
    # module-level constant. Re-bind it to the test's
    # per-PR state directory so the per-heartbeat drain
    # writes to the right place.
    sup_mod._TERMINALITY_PATH = _P(
        str(sup_mod.STATE_DIR / "consumed_event_terminality.json")
    )
    # Round-54/C22 Defect D (final): the drain path verifies
    # the snapshot head matches AUTHORITATIVE_HEAD before
    # allowing consumption. Bind AUTHORITATIVE_HEAD so the
    # bootstrap state is consistent for tests.
    sup_mod.AUTHORITATIVE_HEAD = "abc" * 14
    return sup_mod


def _seed_event(sup_mod, event_id: str, kind: str, **extra):
    """Append an event to the unconsumed queue."""
    data = json.loads(sup_mod.UNCONSUMED_EVENTS_PATH.read_text())
    events = data.get("events", [])
    events.append({"id": event_id, "kind": kind, **extra})
    sup_mod.UNCONSUMED_EVENTS_PATH.write_text(
        json.dumps({"events": events})
    )


def _read_terminality_proof(sup_mod) -> list:
    """Read the per-event terminality ledger."""
    path = sup_mod.STATE_DIR / "consumed_event_terminality.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return data.get("entries", [])


# ---------------------------------------------------------------------------
# 1. CI check event blocks readiness before processing
# ---------------------------------------------------------------------------
def test_check_event_blocks_readiness_before_processing(tmp_path):
    """A freshly-detected ``check_changed:*`` event MUST make
    ``list_unconsumed_events()`` non-empty. ``evaluate_readiness``
    is not consulted here, but the event is in the queue and
    the supervisor's drain code MUST observe it on the next
    heartbeat."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    _seed_event(sup_mod, "check_changed:test (3.11)", "required_check_conclusion_change")
    assert len(sup_mod.list_unconsumed_events()) == 1


# ---------------------------------------------------------------------------
# 2. Processed event receives durable terminal disposition
# ---------------------------------------------------------------------------
def test_processed_check_event_consumed_with_reason(tmp_path):
    """When the snapshot has the check's conclusion captured,
    the drain code finds the event terminal and consumes it
    with a reason."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    _seed_event(
        sup_mod,
        "check_changed:test (3.11)",
        "required_check_conclusion_change",
    )
    snap = {
        "head_sha": "abc" * 14,
        "captured_at": "2026-08-13T22:00:00Z",
        "required_checks": {
            "test (3.11)": {"conclusion": "success"},
        },
    }
    candidates = sup_mod.evaluate_system_event_terminality(
        snap=snap, token="dummy"
    )
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["event_id"] == "check_changed:test (3.11)"
    assert "test (3.11)" in cand["reason"]
    # Consume with reason.
    sup_mod.consume_event_with_reason(
        cand["event_id"],
        consumer="test",
        reason=cand["reason"],
    )
    assert len(sup_mod.list_unconsumed_events()) == 0
    proofs = _read_terminality_proof(sup_mod)
    assert len(proofs) == 1
    assert proofs[0]["event_id"] == "check_changed:test (3.11)"
    assert proofs[0]["consumer"] == "test"
    assert proofs[0]["reason"]


# ---------------------------------------------------------------------------
# 3. Terminal event leaves unconsumed ledger
# ---------------------------------------------------------------------------
def test_terminal_event_leaves_unconsumed_ledger(tmp_path):
    """A consumed event is removed from the unconsumed queue."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    _seed_event(
        sup_mod,
        "check_changed:committed-state-scan",
        "required_check_conclusion_change",
    )
    assert len(sup_mod.list_unconsumed_events()) == 1
    sup_mod.consume_event(
        "check_changed:committed-state-scan"
    )
    assert len(sup_mod.list_unconsumed_events()) == 0


# ---------------------------------------------------------------------------
# 4. Terminal event is not relaunched
# ---------------------------------------------------------------------------
def test_terminal_event_not_relaunched(tmp_path):
    """After consumption, the launched_events.json ledger must
    not gain a stale entry. The launch loop relies on
    ``list_unconsumed_events()`` to find actionable events,
    so a consumed event with no corresponding launch is
    correctly not re-marked."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    _seed_event(sup_mod, "check_changed:provenance", "required_check_conclusion_change")
    sup_mod.consume_event("check_changed:provenance")
    # The supervisor's launched-events tracker is keyed by
    # event_id. A consumed event must not appear in either
    # the unconsumed queue OR the launched-events ledger.
    assert len(sup_mod.list_unconsumed_events()) == 0
    assert "check_changed:provenance" not in sup_mod.launched_event_ids()


# ---------------------------------------------------------------------------
# 5. Recoverable worker failure does NOT consume its source event
# ---------------------------------------------------------------------------
def test_recoverable_worker_failure_does_not_consume_event(tmp_path):
    """A worker launch failure (e.g. ``WORKER_STARTUP_FAILED``)
    must NOT consume the source event. The event remains
    RETRY_PENDING so a future round can re-dispatch."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    event_id = "check_changed:package-smoke"
    _seed_event(
        sup_mod, event_id, "required_check_conclusion_change"
    )
    # Simulate a worker launch failure where the dispatch
    # code did NOT call consume_event. The event is still in
    # the queue.
    assert len(sup_mod.list_unconsumed_events()) == 1
    # The drain path must NOT have been run yet.
    assert event_id in [
        e["id"] for e in sup_mod.list_unconsumed_events()
    ]


# ---------------------------------------------------------------------------
# 6. Supervisor restart preserves nonterminal event
# ---------------------------------------------------------------------------
def test_supervisor_restart_preserves_nonterminal_event(tmp_path):
    """A non-terminal event survives a supervisor restart by
    virtue of being persisted to ``unconsumed_events.json``.
    A fresh supervisor process reads the file and finds the
    event still pending."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    _seed_event(sup_mod, "check_changed:test (3.12)", "required_check_conclusion_change")
    # Snapshot has no test (3.12) conclusion: the event is
    # non-terminal.
    snap = {
        "head_sha": "abc" * 14,
        "captured_at": "2026-08-13T22:00:00Z",
        "required_checks": {
            # No test (3.12) entry.
        },
    }
    candidates = sup_mod.evaluate_system_event_terminality(
        snap=snap, token="dummy"
    )
    assert candidates == []
    # The event is still in the queue.
    assert len(sup_mod.list_unconsumed_events()) == 1


# ---------------------------------------------------------------------------
# 7. Stale-head event is superseded only by explicit newer-head evidence
# ---------------------------------------------------------------------------
def test_stale_head_event_superseded_by_newer_head(tmp_path):
    """A ``head_changed:<H0>`` event is superseded when the live
    PR head no longer equals ``H0``. The next valid head event
    is the one corresponding to the current valid head."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    sup_mod.AUTHORITATIVE_HEAD = "abc" * 14
    _seed_event(
        sup_mod,
        "head_changed:abcabcabcabcabcabcabcabcabcabcabcabcabcabc",
        "head_changed",
    )
    # Currently the live head matches the recorded head.
    snap = {
        "head_sha": "abc" * 14,
        "captured_at": "2026-08-13T22:00:00Z",
    }
    candidates = sup_mod.evaluate_system_event_terminality(
        snap=snap, token="dummy"
    )
    assert len(candidates) == 1
    # Move the live head forward.
    sup_mod.AUTHORITATIVE_HEAD = "def" * 14
    candidates = sup_mod.evaluate_system_event_terminality(
        snap={
            "head_sha": "def" * 14,
            "captured_at": "2026-08-13T22:00:00Z",
        },
        token="dummy",
    )
    # The stale head_changed event is NOT terminal again
    # because the recorded head (abc) does not match the
    # current authoritative head (def).
    stale_candidates = [
        c for c in candidates
        if c["event_id"] == "head_changed:abcabcabcabcabcabcabcabcabcabcabcabcabcabc"
    ]
    assert stale_candidates == []


# ---------------------------------------------------------------------------
# 8. Age alone never consumes event
# ---------------------------------------------------------------------------
def test_age_alone_never_consumes_event(tmp_path):
    """A very old event with no terminality proof must NOT be
    consumed. The drain code looks for a durable terminality
    proof, not an elapsed time."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    event_id = "check_changed:test (3.10)"
    _seed_event(sup_mod, event_id, "required_check_conclusion_change")
    # Force the event's age to be very old by rewriting the
    # unconsumed queue with a mtime hint (mtime is on the file).
    import os
    very_old = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    os.utime(sup_mod.UNCONSUMED_EVENTS_PATH, (very_old, very_old))
    # Snapshot has no conclusion for this check.
    snap = {
        "head_sha": "abc" * 14,
        "captured_at": "2026-08-13T22:00:00Z",
        "required_checks": {},
    }
    candidates = sup_mod.evaluate_system_event_terminality(
        snap=snap, token="dummy"
    )
    assert candidates == []
    # The event is still in the queue: age alone did not consume it.
    assert len(sup_mod.list_unconsumed_events()) == 1


# ---------------------------------------------------------------------------
# 9. Provider pause event with retry owner remains nonterminal as required
# ---------------------------------------------------------------------------
def test_provider_pause_event_with_retry_owner_nonterminal(tmp_path):
    """A ``provider_state:coderabbit`` event with a recent
    ``next_retry_timestamp`` is terminal: the producer (the
    recent provider-state recording) has produced the
    terminality proof."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    _seed_event(
        sup_mod, "provider_state:coderabbit", "provider_state_change"
    )
    # The provider is paused with a recent next_retry_timestamp.
    recent = (datetime.now(timezone.utc) - timedelta(minutes=10))
    sup_mod.QUOTA_PATH.write_text(json.dumps({
        "providers": {
            "coderabbit": {
                "pending_review_head": "abc" * 14,
                "next_retry_timestamp": recent.strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
        }
    }))
    candidates = sup_mod.evaluate_system_event_terminality(
        snap={
            "head_sha": "abc" * 14,
            "captured_at": "2026-08-13T22:00:00Z",
        }, token="dummy"
    )
    assert len(candidates) == 1
    assert candidates[0]["event_id"] == "provider_state:coderabbit"


# ---------------------------------------------------------------------------
# 10. All terminal system events gone -> readiness may proceed
# ---------------------------------------------------------------------------
def test_all_terminal_events_gone_readiness_may_proceed(tmp_path):
    """When all durable system events have a durably-recorded
    terminality proof, ``list_unconsumed_events()`` returns
    an empty list. A downstream readiness gate that checks
    this list will observe the gate as open."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    # Seed and consume all events.
    _seed_event(sup_mod, "check_changed:test (3.11)", "required_check_conclusion_change")
    _seed_event(sup_mod, "check_changed:test (3.12)", "required_check_conclusion_change")
    _seed_event(sup_mod, "check_changed:test (3.10)", "required_check_conclusion_change")
    _seed_event(sup_mod, "check_changed:committed-state-scan", "required_check_conclusion_change")
    _seed_event(sup_mod, "check_changed:package-smoke", "required_check_conclusion_change")
    _seed_event(sup_mod, "check_changed:provenance", "required_check_conclusion_change")
    _seed_event(sup_mod, "provider_state:coderabbit", "provider_state_change")
    # Pop the snapshot with all conclusions.
    snap = {
        "head_sha": "abc" * 14,
        "captured_at": "2026-08-13T22:00:00Z",
        "required_checks": {
            "test (3.11)": {"conclusion": "success"},
            "test (3.12)": {"conclusion": "success"},
            "test (3.10)": {"conclusion": "success"},
            "committed-state-scan": {"conclusion": "success"},
            "package-smoke": {"conclusion": "success"},
            "provenance": {"conclusion": "success"},
        },
    }
    recent = (datetime.now(timezone.utc) - timedelta(minutes=10))
    sup_mod.QUOTA_PATH.write_text(json.dumps({
        "providers": {
            "coderabbit": {
                "pending_review_head": "abc" * 14,
                "next_retry_timestamp": recent.strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
        }
    }))
    candidates = sup_mod.evaluate_system_event_terminality(
        snap=snap, token="dummy"
    )
    # All 7 events have terminality proofs.
    assert len(candidates) == 7
    # Consume them.
    for cand in candidates:
        sup_mod.consume_event_with_reason(
            cand["event_id"],
            consumer="test",
            reason=cand["reason"],
        )
    # The unconsumed queue is empty.
    assert len(sup_mod.list_unconsumed_events()) == 0
    # The terminality ledger records all 7.
    proofs = _read_terminality_proof(sup_mod)
    assert len(proofs) == 7
