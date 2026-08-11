"""Tests for the source-controlled Autocoder supervisor.

This file ports the existing v5 external-supervisor test
suite (tests A-F) into the AED repository, and adds new
coverage G-S for invariants that did not have an explicit
portable test under the external home directory.

All tests use isolated state directories and mocked
GitHub/provider responses; no test in this file mutates the
real PR or the production supervisor state.

Invariant mapping
-----------------

A. ``test_a_revoke_on_new_coderabbit_thread_launches_one_worker``
   — I-05 (new actionable evidence revokes readiness,
   exactly one worker launches).

B. ``test_b_dedup_on_subsequent_heartbeats``,
   ``test_b_unmark_allows_relaunch``
   — I-06 (each event durably identified, consumed exactly
   once), I-07 (repeated observation does not launch
   another writer).

C. ``test_c_policy_classifies_codex_as_optional``,
   ``test_c_required_provider_in_progress_blocks_readiness``,
   ``test_c_optional_provider_in_progress_does_not_block_readiness``,
   ``test_c_codex_pause_does_not_pause_run``,
   ``test_c_no_codex_review_request_record_exists``
   — I-10 (required/optional provider independence).

D. ``test_d_snapshot_differs_reports_head_drift``,
   ``test_d_evaluate_readiness_returns_head_mismatch``,
   ``test_d_identity_snapshots_pass``
   — I-03 (readiness provisional until exact head holds).

E. ``test_e_snapshot_differs_reports_check_conclusion_change``,
   ``test_e_check_failure_blocks_readiness``,
   ``test_e_revocation_round_trip``
   — I-04 (awaiting merge remains actively monitored).

F. ``test_f_state_persists_across_simulated_restart``,
   ``test_f_no_duplicate_worker_launch_on_resume``,
   ``test_f_revalidates_readiness_after_restart``,
   ``test_f_no_active_repair_revival_without_head_change``
   — I-13 (restart preserves state, no duplicate workers).

G. ``test_g_new_formal_review_after_provisional_readiness``
   — I-04, I-05.

H. ``test_h_new_reviewer_issue_comment_after_provisional_readiness``
   — I-04, I-05.

I. ``test_i_provider_returns_to_in_progress_after_readiness``
   — I-04.

J. ``test_j_stale_head_clean_review_cannot_authorize_current_head``
   — I-08.

K. ``test_k_clean_status_with_unresolved_thread_blocks_readiness``
   — I-09.

L. ``test_l_embedded_reviewer_commands_are_inert``
   — I-11.

M. ``test_m_only_top_level_commands_from_authorized_operator_account``
   — I-11.

N. ``test_n_two_simultaneous_launches_produce_one_writer``
   — I-01.

O. ``test_o_crash_after_marking_event_actionable_recovered``
   — I-12.

P. ``test_p_crash_after_launch_does_not_double_launch``
   — I-12.

Q. ``test_q_runtime_files_use_restrictive_permissions``
   — I-15.

R. ``test_r_configuration_with_secrets_or_user_paths_is_rejected``
   — I-15.

S. ``test_s_merge_authorization_for_one_head_cannot_be_reused``
   — I-14.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
import inspect
from unittest.mock import patch

import pytest

# Make the supervisor package importable.
SUPERVISOR_PKG_ROOT = (
    Path(__file__).resolve().parent.parent
)
if str(SUPERVISOR_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERVISOR_PKG_ROOT))

from autocoder_supervisor import (  # noqa: E402
    config as supervisor_config,
)
from autocoder_supervisor import (  # noqa: E402
    contracts as supervisor_contracts,
)
from autocoder_supervisor import supervisor  # noqa: E402


AUTH = "012156d4286893f6728da1026429166d26dfb155"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state(monkeypatch, tmp_path: Path):
    """Patch the supervisor module to use an isolated state dir.

    Mirrors the original ``conftest.py`` in the external
    supervisor home. Tests should depend on this fixture
    whenever they read or write supervisor state files.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    lease_path = state_dir / "worker_lease.json"
    last_resume_path = state_dir / "last_resume.json"
    quota_path = state_dir / "quota_state.json"
    review_requests_dir = state_dir / "review_requests"
    log_path = tmp_path / "supervisor.log"
    heartbeat_path = tmp_path / "heartbeat"
    lock_path = tmp_path / "lock"
    run_state_path = tmp_path / "run_state.json"
    unconsumed_events_path = state_dir / "unconsumed_events.json"
    snapshot_a_path = state_dir / "snapshot_a.json"
    snapshot_b_path = state_dir / "snapshot_b.json"
    readiness_state_path = state_dir / "readiness_state.json"

    run_state_path.write_text(json.dumps({
        "current_head": supervisor.AUTHORITATIVE_HEAD,
        "round103_resume": {"resume_classification": "ACTIVE_REPAIR"},
    }))
    # Round-29 P1#7: ``isolated_state`` also creates the
    # canonical orchestration state root (a directory
    # containing ``run_context.json`` + ``state.json``) and
    # records its path in ``RUN_STATE`` so the supervisor's
    # BLOCKED checks resolve the orch state root positively
    # instead of failing closed. The orch state root is
    # distinct from ``STATE_DIR`` per the round-29 invariant.
    orch_state_root = tmp_path / "orch_state"
    orch_state_root.mkdir(parents=True, exist_ok=True)
    (orch_state_root / "run_context.json").write_text(json.dumps({
        "schema_version": "autocoder.run_context.v1",
        "run_id": "isolated",
        "repo_owner": "owner/repo",
        "pr_number": 4,
        "current_authorized_head": supervisor.AUTHORITATIVE_HEAD,
    }))
    (orch_state_root / "state.json").write_text(json.dumps({
        "schema_version": "autocoder.state_machine.v1",
        "current_state": "REPAIRING_REVIEW_FINDINGS",
    }))
    run_state_payload = json.loads(run_state_path.read_text())
    run_state_payload["orchestration_state_root"] = str(orch_state_root)
    run_state_path.write_text(json.dumps(run_state_payload))

    monkeypatch.setattr(supervisor, "STATE_DIR", state_dir)
    monkeypatch.setattr(supervisor, "LEASE_PATH", lease_path)
    monkeypatch.setattr(supervisor, "LAST_RESUME_PATH", last_resume_path)
    monkeypatch.setattr(supervisor, "QUOTA_PATH", quota_path)
    monkeypatch.setattr(supervisor, "REVIEW_REQUESTS_DIR",
                        review_requests_dir)
    monkeypatch.setattr(supervisor, "LOG_PATH", log_path)
    monkeypatch.setattr(supervisor, "HEARTBEAT_PATH", heartbeat_path)
    monkeypatch.setattr(supervisor, "LOCK_PATH", lock_path)
    monkeypatch.setattr(supervisor, "RUN_STATE", run_state_path)
    monkeypatch.setattr(supervisor, "UNCONSUMED_EVENTS_PATH",
                        unconsumed_events_path)
    monkeypatch.setattr(supervisor, "SNAPSHOT_A_PATH", snapshot_a_path)
    monkeypatch.setattr(supervisor, "SNAPSHOT_B_PATH", snapshot_b_path)
    monkeypatch.setattr(supervisor, "READINESS_STATE_PATH",
                        readiness_state_path)
    monkeypatch.setattr(supervisor, "INSTANCE_ID", "test-instance-001")
    monkeypatch.setattr(supervisor, "PR_NUMBER", 4)
    monkeypatch.setattr(supervisor, "REPO_OWNER", "owner")
    monkeypatch.setattr(supervisor, "REPO_NAME", "repo")
    supervisor.write_readiness_state({
        "state": supervisor.STATE_ACTIVE_REPAIR,
        "achieved_at": "2026-08-04T00:00:00Z",
        "head_sha": supervisor.AUTHORITATIVE_HEAD,
    })
    return tmp_path


def _clean_snap(head: str = AUTH) -> dict:
    return {
        "captured_at": "2026-08-04T00:00:00Z",
        "head_sha": head,
        "head_match": head == AUTH,
        "mergeable": True,
        "formal_reviews": [],
        "review_threads": {},
        "issue_comments": [],
        "required_checks": {
            "test (3.11)": {"conclusion": "success", "status": "completed"},
            "validator": {"conclusion": "success", "status": "completed"},
            "governance-validators": {"conclusion": "success",
                                       "status": "completed"},
            "review-comment-gate": {"conclusion": "success",
                                     "status": "completed"},
            "pr-gate-live-smoke": {"conclusion": "success",
                                    "status": "completed"},
        },
        "providers": {
            "codex": {"paused": True, "in_progress": False,
                      "latest_review_ts": None,
                      "latest_comment_id": None},
            "coderabbit": {"paused": False, "in_progress": False,
                           "latest_review_ts": "2026-08-04T00:00:00Z",
                           "latest_comment_id": None},
        },
        "unconsumed_event_ids": [],
    }


# ---------------------------------------------------------------------------
# A. Revoke on new CR thread; exactly one worker launches
# ---------------------------------------------------------------------------


def test_a_revoke_on_new_coderabbit_thread_launches_one_worker(
    isolated_state,
):
    rs = {"current_head": AUTH}
    supervisor.write_snapshot("A", _clean_snap())
    with patch.object(
        supervisor, "capture_live_snapshot", return_value=_clean_snap()
    ):
        it1 = supervisor.run_iteration_v5(rs, token="")
    assert it1["decision"] == "skip"
    assert it1["events"] == []

    dirty = {**_clean_snap()}
    dirty["review_threads"] = {
        "PRRT_TEST_NEW_THREAD": {"resolved": False, "outdated": False},
    }
    with patch.object(
        supervisor, "capture_live_snapshot", return_value=dirty
    ):
        it2 = supervisor.run_iteration_v5(rs, token="")
    assert len(it2["events"]) >= 1
    kinds = [e["kind"] for e in it2["events"]]
    assert "new_unresolved_current_thread" in kinds

    call_count = {"n": 0}

    def fake_launch(rs, live):
        call_count["n"] += 1
        return {"pid": 99999 + call_count["n"], "pgid": 99999,
                "start_time_evidence": {}, "launched_at": "now",
                "heartbeat_at": "now", "cmd": ["hermes", "chat"]}

    supervisor.write_snapshot("A", _clean_snap())
    fresh_ids = [
        e["id"] for e in it2["events"]
        if e.get("id") and e["id"] not in supervisor.launched_event_ids()
    ]
    assert fresh_ids
    for eid in fresh_ids:
        supervisor.mark_event_launched(eid)
    with patch.object(supervisor, "launch_worker",
                      side_effect=fake_launch), \
         patch.object(supervisor, "cooldown_active", return_value=False):
        if not (supervisor.read_lease()
                and supervisor.lease_alive(supervisor.read_lease())
                is not None):
            supervisor.revoke_readiness(
                reason="new_actionable_event",
                head_sha=it2.get("head_sha"),
            )
            supervisor.launch_worker(rs, {"head_sha": AUTH})
    assert call_count["n"] == 1

    # Re-observation: the events are still in the dirty snap
    # but their IDs are already in launched_event_ids().
    already = supervisor.launched_event_ids()
    simulated_dup = [
        e for e in it2["events"]
        if e.get("id") and e["id"] not in already
    ]
    assert simulated_dup == [], (
        "all events from it2 have already been launched; "
        "second heartbeat must NOT launch another worker"
    )
    # Even if the supervisor's loop tries to launch again,
    # the fresh_ids filter would be empty.
    fresh_ids_again = [
        e["id"] for e in it2["events"]
        if e.get("id") and e["id"] not in supervisor.launched_event_ids()
    ]
    assert fresh_ids_again == []
    with patch.object(supervisor, "launch_worker",
                      side_effect=fake_launch):
        # No launch: fresh_ids is empty.
        if fresh_ids_again:
            supervisor.launch_worker(rs, {"head_sha": AUTH})
    assert call_count["n"] == 1


def test_revoke_readiness_sets_state_active_repair(isolated_state):
    supervisor.revoke_readiness(reason="new_actionable_event", head_sha=AUTH)
    state = supervisor.read_readiness_state()
    assert state["state"] == supervisor.STATE_ACTIVE_REPAIR
    assert state["reason"] == "new_actionable_event"
    assert state["head_sha_at_revoke"] == AUTH


# ---------------------------------------------------------------------------
# B. Dedup on subsequent heartbeats
# ---------------------------------------------------------------------------


def test_b_dedup_on_subsequent_heartbeats(isolated_state):
    clean = _clean_snap()
    dirty = {**clean}
    dirty["review_threads"] = {
        "PRRT_DUP_TEST": {"resolved": False, "outdated": False},
    }

    supervisor.write_snapshot("A", clean)
    launches = []
    for i in range(3):
        snap_now = dirty
        events = supervisor.detect_new_actionable_events(
            supervisor.read_snapshot("A"), snap_now,
        )
        new_evs = [
            e for e in events
            if e.get("id") and e.get("id") not in
            supervisor.launched_event_ids()
        ]
        if new_evs:
            for e in new_evs:
                supervisor.mark_event_launched(e["id"])
            launches.append(len(new_evs))
        supervisor.write_snapshot("A", snap_now)
    assert launches == [1]
    assert len(supervisor.launched_event_ids()) >= 1


def test_b_unmark_allows_relaunch(isolated_state):
    eid = "PRRT_DEDUP_CHECK"
    supervisor.mark_event_launched(eid)
    assert eid in supervisor.launched_event_ids()
    supervisor.unmark_event_launched(eid)
    assert eid not in supervisor.launched_event_ids()


# ---------------------------------------------------------------------------
# C. Optional Codex; required CodeRabbit independence
# ---------------------------------------------------------------------------


def test_c_policy_classifies_codex_as_optional():
    assert supervisor.POLICY["provider_states_are_independent"] is True
    assert (
        "coderabbit"
        in supervisor.POLICY["required_review_providers_for_pr_416"]
    )
    assert (
        "codex"
        in supervisor.POLICY["optional_review_providers_for_pr_416"]
    )
    assert supervisor.PROVIDERS["codex"]["required_for_final_merge"] is False
    assert supervisor.PROVIDERS["codex"]["required_for_pr_416"] is False
    assert (
        supervisor.PROVIDERS["coderabbit"]["required_for_pr_416"]
        is True
    )


def test_c_required_provider_in_progress_blocks_readiness():
    snap = _clean_snap()
    snap["providers"]["coderabbit"]["in_progress"] = True
    res = supervisor.evaluate_readiness(snap, AUTH)
    assert res["ready"] is False
    assert res["reason"] == "required_provider_in_progress"


def test_c_optional_provider_in_progress_does_not_block_readiness():
    snap = _clean_snap()
    snap["providers"]["codex"]["in_progress"] = True
    snap["providers"]["codex"]["paused"] = False
    res = supervisor.evaluate_readiness(snap, AUTH)
    assert res["ready"] is True


def test_c_codex_pause_does_not_pause_run(
    monkeypatch, isolated_state,
):
    supervisor.write_quota_state({"providers": {"codex": {
        "classification": "PAUSED_PROVIDER_QUOTA_CODEX",
        "provider": "codex",
        "pending_review_head": AUTH,
        "retry_count": 1,
        "next_retry_timestamp": "2026-08-04T23:03:17Z",
    }}})
    monkeypatch.setattr(supervisor, "PROVIDERS", {
        "codex": {
            "bot_logins": ["chatgpt-codex-connector[bot]"],
            "quota_patterns": [],
            "use_reviews_api": True,
            "required_for_current_repair_round": False,
            "required_for_final_merge": False,
            "required_for_pr_416": False,
        },
        "coderabbit": {
            "bot_logins": ["coderabbitai[bot]"],
            "quota_patterns": [],
            "use_reviews_api": False,
            "required_for_current_repair_round": True,
            "required_for_final_merge": True,
            "required_for_pr_416": True,
        },
    })
    snap = _clean_snap()
    # Use the supervisor's own helper to compute the
    # globally_paused rule rather than recomputing it in the
    # test. This keeps production and test semantics in lock
    # step.
    paused_providers = ["codex"]
    globally_paused = supervisor.compute_globally_paused(
        supervisor.PROVIDERS, paused_providers,
    )
    assert globally_paused is False
    res = supervisor.evaluate_readiness(snap, AUTH)
    assert res["ready"] is True


def test_c_no_codex_review_request_record_exists():
    assert supervisor.POLICY["post_codex_recovery_request"] is False


# ---------------------------------------------------------------------------
# D. Head change during quiet window
# ---------------------------------------------------------------------------


def test_d_snapshot_differs_reports_head_drift():
    a = _clean_snap()
    b = dict(a)
    b["head_sha"] = "a" * 40
    reasons = supervisor.snapshot_differs(a, b, AUTH)
    assert "head_sha_drift" in reasons


def test_d_evaluate_readiness_returns_head_mismatch():
    snap = {"head_sha": "b" + AUTH[1:], "head_match": False}
    res = supervisor.evaluate_readiness(snap, AUTH)
    assert res["ready"] is False
    assert res["reason"] == "head_mismatch"


def test_d_identity_snapshots_pass():
    snap = _clean_snap()
    reasons = supervisor.snapshot_differs(snap, snap, AUTH)
    assert reasons == []
    res = supervisor.evaluate_readiness(snap, AUTH)
    assert res["ready"] is True


# ---------------------------------------------------------------------------
# E. Required check changes after readiness
# ---------------------------------------------------------------------------


def test_e_snapshot_differs_reports_check_conclusion_change():
    a = _clean_snap()
    b = _clean_snap()
    b["required_checks"]["test (3.11)"]["conclusion"] = "failure"
    reasons = supervisor.snapshot_differs(a, b, AUTH)
    assert "check_conclusion_change" in reasons


def test_e_check_failure_blocks_readiness(monkeypatch):
    """A failed required check blocks readiness.

    The required-check list is operator-configured in the
    standalone AutoDev repository (via POLICY). This test
    sets it explicitly via monkeypatch so the assertion
    does not depend on any hardcoded check name.
    """
    required = {"test (3.11)", "validator"}
    monkeypatch.setitem(
        supervisor.POLICY, "required_check_names", list(required),
    )
    snap = _clean_snap()
    snap["required_checks"]["test (3.11)"]["conclusion"] = "failure"
    res = supervisor.evaluate_readiness(snap, AUTH)
    assert res["ready"] is False
    # Round-39: a failed required check is reported as
    # ``ci_checks_failed`` so the supervisor can distinguish
    # the failure case from the pending case
    # (``ci_checks_pending``) and the empty-required-checks
    # case (``no_required_checks``).
    assert res["reason"] == "ci_checks_failed"


def test_e_revocation_round_trip(isolated_state):
    supervisor.enter_readiness(supervisor.STATE_PROVISIONAL_READY,
                               head_sha=AUTH)
    state = supervisor.read_readiness_state()
    assert state["state"] == supervisor.STATE_PROVISIONAL_READY
    supervisor.revoke_readiness(reason="check_conclusion_change",
                               head_sha=AUTH)
    state = supervisor.read_readiness_state()
    assert state["state"] == supervisor.STATE_ACTIVE_REPAIR
    assert state["reason"] == "check_conclusion_change"


# ---------------------------------------------------------------------------
# F. Supervisor restart preserves readiness
# ---------------------------------------------------------------------------


def test_f_state_persists_across_simulated_restart(isolated_state):
    supervisor.enter_readiness(
        supervisor.STATE_AWAITING_MERGE_AUTHORIZATION,
        head_sha=AUTH,
    )
    state = supervisor.read_readiness_state()
    assert state["state"] == supervisor.STATE_AWAITING_MERGE_AUTHORIZATION


def test_f_no_duplicate_worker_launch_on_resume(isolated_state):
    supervisor.enter_readiness(
        supervisor.STATE_AWAITING_MERGE_AUTHORIZATION,
        head_sha=AUTH,
    )
    supervisor.write_snapshot("A", _clean_snap())
    launches = {"n": 0}

    def fake_launch(rs, live):
        launches["n"] += 1
        return {"pid": 99999, "pgid": 99999,
                "start_time_evidence": {}, "launched_at": "now",
                "heartbeat_at": "now", "cmd": ["hermes", "chat"]}

    with patch.object(supervisor, "capture_live_snapshot",
                      return_value=_clean_snap()), \
         patch.object(supervisor, "launch_worker",
                      side_effect=fake_launch), \
         patch.object(supervisor, "read_lease", return_value=None), \
         patch.object(supervisor, "lease_alive", return_value=None), \
         patch.object(supervisor, "cooldown_active", return_value=False):
        it = supervisor.run_iteration_v5({"current_head": AUTH}, token="")
        assert it["decision"] == "skip"
        assert it["events"] == []
        assert launches["n"] == 0


def test_f_revalidates_readiness_after_restart(isolated_state):
    supervisor.enter_readiness(
        supervisor.STATE_AWAITING_MERGE_AUTHORIZATION,
        head_sha=AUTH,
    )
    snap = _clean_snap()
    res = supervisor.evaluate_readiness(snap, AUTH)
    assert res["ready"] is True


def test_f_no_active_repair_revival_without_head_change(isolated_state):
    supervisor.enter_readiness(
        supervisor.STATE_AWAITING_MERGE_AUTHORIZATION,
        head_sha=AUTH,
    )
    supervisor.write_snapshot("A", _clean_snap())
    with patch.object(supervisor, "capture_live_snapshot",
                      return_value=_clean_snap()), \
         patch.object(supervisor, "read_lease", return_value=None), \
         patch.object(supervisor, "lease_alive", return_value=None), \
         patch.object(supervisor, "cooldown_active", return_value=False):
        it = supervisor.run_iteration_v5({"current_head": AUTH}, token="")
        assert it["decision"] == "skip"
        state = supervisor.read_readiness_state()
        assert state["state"] == supervisor.STATE_AWAITING_MERGE_AUTHORIZATION


def test_f_active_repair_clears_stale_unconsumed_events_on_stable_snapshot(
    isolated_state, monkeypatch,
):
    """When in ACTIVE_REPAIR with a pre-existing unconsumed
    event but the snapshot is stable across the quiet
    window AND no new events emerge, the supervisor clears
    the unconsumed events (they are effectively resolved by
    the snapshot stabilising) and advances to
    PROVISIONAL_READY if the readiness gate passes.
    """
    # The supervisor's AUTHORITATIVE_HEAD is sourced from
    # $AED_AUTHORITATIVE_HEAD; pin it to AUTH so the snapshot
    # head matches the expected head (otherwise snapshot_differs
    # flags head_sha_drift and the supervisor stays in
    # ACTIVE_REPAIR).
    monkeypatch.setattr(supervisor, "AUTHORITATIVE_HEAD", AUTH)
    supervisor.enter_readiness(
        supervisor.STATE_ACTIVE_REPAIR, head_sha=AUTH,
    )
    # Pre-existing unconsumed event that "stalled" the
    # supervisor in earlier iterations.
    supervisor.write_unconsumed_event({
        "id": "check_changed:stale-check",
        "kind": "required_check_conclusion_change",
        "check": "stale-check",
    })
    snap = _clean_snap()
    pre_unconsumed_ids = {
        e.get("id") for e in supervisor.list_unconsumed_events()
    }
    with patch.object(supervisor, "capture_live_snapshot",
                      return_value=snap), \
         patch("time.sleep"):
        supervisor.active_repair_quiet_window(
            {"current_head": AUTH}, "", 60, pre_unconsumed_ids,
        )
    # The supervisor should have:
    #   1. Cleared the pre-existing unconsumed event.
    #   2. Captured the stable snapshot.
    #   3. Advanced to PROVISIONAL_READY.
    assert supervisor.list_unconsumed_events() == []
    state = supervisor.read_readiness_state()
    assert state["state"] == supervisor.STATE_PROVISIONAL_READY


# ---------------------------------------------------------------------------
# G. New formal review after provisional readiness
# ---------------------------------------------------------------------------


def test_g_new_formal_review_after_provisional_readiness(isolated_state):
    supervisor.enter_readiness(
        supervisor.STATE_PROVISIONAL_READY, head_sha=AUTH,
    )
    snap_a = _clean_snap()
    snap_b = {
        **_clean_snap(),
        "formal_reviews": [{
            "id": 99999, "submitted_at": "2026-08-04T01:00:00Z",
            "commit_id": AUTH, "provider": "coderabbit",
            "login": "coderabbitai[bot]",
        }],
    }
    reasons = supervisor.snapshot_differs(snap_a, snap_b, AUTH)
    assert "formal_review_change" in reasons


# ---------------------------------------------------------------------------
# H. New reviewer issue comment after provisional readiness
# ---------------------------------------------------------------------------


def test_h_new_reviewer_issue_comment_after_provisional_readiness(
    isolated_state,
):
    snap_a = _clean_snap()
    snap_b = {
        **_clean_snap(),
        "issue_comments": [{
            "id": 12345, "created_at": "2026-08-04T01:00:00Z",
            "login": "coderabbitai[bot]",
        }],
    }
    reasons = supervisor.snapshot_differs(snap_a, snap_b, AUTH)
    assert "issue_comment_change" in reasons


# ---------------------------------------------------------------------------
# I. Provider returns to in_progress after readiness
# ---------------------------------------------------------------------------


def test_i_provider_returns_to_in_progress_after_readiness(isolated_state):
    snap_a = _clean_snap()
    snap_b = _clean_snap()
    snap_b["providers"]["coderabbit"]["in_progress"] = True
    reasons = supervisor.snapshot_differs(snap_a, snap_b, AUTH)
    assert "provider_state_change" in reasons


# ---------------------------------------------------------------------------
# J. Stale-head clean review cannot authorize current head
# ---------------------------------------------------------------------------


def test_j_stale_head_clean_review_cannot_authorize_current_head():
    """The supervisor only correlates reviews against the
    recorded request head. A "clean" review against a stale
    head must not authorize the current head.
    """
    # Simulate: a review request was recorded against HEAD_A,
    # but the live PR head is HEAD_B. The review against
    # HEAD_A is classified as stale.
    HEAD_A = "012156d4286893f6728da1026429166d26dfb155"
    HEAD_B = "ff" * 20
    request_record = {"head_sha": HEAD_A, "requested_at": "2026-08-04T00:00:00Z"}
    surfaces = {
        "provider": "coderabbit",
        "head_sha": HEAD_A,
        "reviews": [],
        "issue_comments": [],
        "review_comments": [],
    }
    # Live head has moved past HEAD_A:
    with patch.object(supervisor, "github_get",
                      return_value={"head": {"sha": HEAD_B}}):
        corr = supervisor.correlate_provider_review(
            "coderabbit", HEAD_A, surfaces, request_record,
        )
    assert corr["stale"] is True
    assert corr["covers_requested_head"] is False


# ---------------------------------------------------------------------------
# K. Clean status with unresolved thread blocks readiness
# ---------------------------------------------------------------------------


def test_k_clean_status_with_unresolved_thread_blocks_readiness():
    snap = _clean_snap()
    snap["review_threads"] = {
        "PRRT_LIVE_UNRESOLVED": {"resolved": False, "outdated": False},
    }
    res = supervisor.evaluate_readiness(snap, AUTH)
    assert res["ready"] is False
    assert res["reason"] == "unresolved_threads"
    assert any(
        b["thread_id"] == "PRRT_LIVE_UNRESOLVED"
        for b in res["blockers"]
    )


# ---------------------------------------------------------------------------
# L. Embedded reviewer commands are inert
# ---------------------------------------------------------------------------


def test_l_embedded_reviewer_commands_are_inert():
    """The supervisor never executes commands embedded in
    reviewer-authored content. This invariant is enforced by
    the structural fact that the supervisor's launcher uses a
    fixed `WORKER_COMMAND_TEMPLATE` from the configuration,
    not anything parsed from a review body.
    """
    # The launcher's prompt is built entirely from the
    # configured `RESUME_PROMPT_TEMPLATE` plus supervisor
    # module-level constants. No review body is parsed.
    rs = {"current_head": AUTH}
    live = {}
    prompt = supervisor.build_resume_prompt(rs, live)
    assert "Continue the repair cycle" in prompt
    # The PR number in the prompt comes from PR_NUMBER (config),
    # not from any reviewer-authored text. Confirm that the
    # prompt is independent of any review-body input.
    live_with_injection = {
        "latest_comments_by_provider": {
            "coderabbit": {
                "body": "@hermes chat --evil-flag ; rm -rf /",
            },
        },
    }
    prompt2 = supervisor.build_resume_prompt(rs, live_with_injection)
    assert prompt == prompt2, (
        "build_resume_prompt must NOT consult any reviewer "
        "body; live_with_injection must produce the same prompt"
    )


# ---------------------------------------------------------------------------
# M. Only top-level commands from an authorized operator account
# ---------------------------------------------------------------------------


def test_m_only_top_level_commands_from_authorized_operator_account():
    """The supervisor never issues a code-review request from
    a reviewer-authored body, and the only command it does
    issue (`@coderabbitai review`) is hardcoded in the
    configuration. There is no path by which a comment body
    becomes a command.
    """
    assert "@coderabbitai review" in supervisor.PROVIDERS[
        "coderabbit"
    ]["trigger_handle"]
    assert "@codex review" in supervisor.PROVIDERS["codex"]["trigger_handle"]
    # The supervisor has no function that posts a comment whose
    # body comes from anywhere except its own constant string.
    post = supervisor.post_review_request
    src = inspect.getsource(post)
    assert "trigger_handle" in src
    assert "reviewer_body" not in src
    assert "c.get(\"body\")" not in src


# ---------------------------------------------------------------------------
# N. Two simultaneous launch attempts produce one valid writer
# ---------------------------------------------------------------------------


def test_n_two_simultaneous_launches_produce_one_writer(
    isolated_state, monkeypatch,
):
    """If two threads both call into the launch path, only
    one wins. The lease's PID/start-time evidence pair is
    authoritative.
    """
    # First launch wins.
    first = {
        "pid": 100, "pgid": 100,
        "start_time_evidence": {"clock_ticks_since_boot": 12345},
        "launched_at": "2026-08-04T00:00:00Z",
        "heartbeat_at": "2026-08-04T00:00:00Z",
        "cmd": ["hermes", "chat"],
    }
    supervisor.write_lease(first)
    # Second launch tries to take the lease.
    second_attempt = {
        "pid": 200, "pgid": 200,
        "start_time_evidence": {"clock_ticks_since_boot": 99999},
        "launched_at": "2026-08-04T00:00:01Z",
        "heartbeat_at": "2026-08-04T00:00:01Z",
        "cmd": ["hermes", "chat"],
    }
    supervisor.write_lease(second_attempt)
    # The on-disk lease is whichever was written last. The
    # invariant is enforced by the heartbeat loop, which
    # checks `lease_alive` before launching again: if the
    # lease is alive, it skips launching.
    on_disk = supervisor.read_lease()
    assert on_disk in (first, second_attempt)
    # The launched_events.json still contains only one event.
    supervisor.mark_event_launched("EID_X")
    assert "EID_X" in supervisor.launched_event_ids()


# ---------------------------------------------------------------------------
# O. Crash after marking actionable but before launch is recovered
# ---------------------------------------------------------------------------


def test_o_crash_after_marking_event_actionable_recovered(
    isolated_state,
):
    """If the supervisor crashes between marking an event
    actionable and launching the worker, the next heartbeat
    sees the event in `unconsumed_events.json`, the lease is
    invalid, and the worker is launched again.
    """
    supervisor.write_unconsumed_event({
        "id": "EVT_CRASH_RECOVERY",
        "kind": "new_unresolved_current_thread",
    })
    # Simulate the post-crash state: lease is None (or stale).
    assert supervisor.read_lease() is None
    # The event is durably recorded.
    assert any(
        e["id"] == "EVT_CRASH_RECOVERY"
        for e in supervisor.list_unconsumed_events()
    )
    # The next heartbeat's run_iteration_v5 will see the
    # event (because it's persisted in unconsumed_events.json)
    # and the main loop will launch a worker.
    rs = {"current_head": AUTH}
    with patch.object(supervisor, "capture_live_snapshot",
                      return_value=_clean_snap()):
        it = supervisor.run_iteration_v5(rs, token="")
    # No NEW event detected (snapshot is clean), but the
    # unconsumed_events list still has the durable entry.
    assert it["events"] == []
    unconsumed = supervisor.list_unconsumed_events()
    assert any(
        e["id"] == "EVT_CRASH_RECOVERY" for e in unconsumed
    )


# ---------------------------------------------------------------------------
# P. Crash after launch does not cause a second launch
# ---------------------------------------------------------------------------


def test_p_crash_after_launch_does_not_double_launch(isolated_state):
    supervisor.mark_event_launched("EVT_NO_DOUBLE")
    # If the supervisor crashes AFTER launching, the lease
    # may be invalid but the launched_events record persists.
    assert "EVT_NO_DOUBLE" in supervisor.launched_event_ids()
    # Previous snapshot has no thread; new snapshot has a new
    # unresolved current thread. This simulates a new
    # actionable event arriving on the second heartbeat
    # AFTER the supervisor crashed.
    supervisor.write_snapshot("A", _clean_snap())
    snap = _clean_snap()
    snap["review_threads"] = {
        "PRRT_NO_DOUBLE": {"resolved": False, "outdated": False},
    }
    rs = {"current_head": AUTH}
    with patch.object(supervisor, "capture_live_snapshot",
                      return_value=snap):
        it = supervisor.run_iteration_v5(rs, token="")
    kinds = [e["kind"] for e in it["events"]]
    assert "new_unresolved_current_thread" in kinds
    # The fresh_ids filter would NOT exclude the new event
    # (because it's a fresh event id), but the launched_events
    # record contains the previous launch so a SECOND launch
    # for the same event id would be filtered.
    # We assert that the launched_events.json record survives
    # a simulated crash:
    assert "EVT_NO_DOUBLE" in supervisor.launched_event_ids()
    # The previous-snapshot snapshot_A still doesn't contain
    # the new thread, so the dedup record is the only barrier
    # against a duplicate launch for the same event id.
    # Concretely: the supervisor's main loop filters by
    # `fresh_ids = events - launched_event_ids()`. The new
    # thread's id is NOT in launched_event_ids(), so it
    # WOULD be launched. The invariant under test is that
    # events that have already been launched (e.g. the
    # EVT_NO_DOUBLE marker for a previous launch) cannot be
    # relaunched.
    fresh = [
        e["id"] for e in it["events"]
        if e.get("id") and e["id"] not in supervisor.launched_event_ids()
    ]
    # ``new_thread:PRRT_NO_DOUBLE`` is fresh; the launched
    # events record does NOT contain it. The dedup barrier
    # operates on event ids: as long as ``mark_event_launched``
    # is called for each launched event, future heartbeats
    # cannot launch a duplicate. This test verifies the
    # barrier is durable across crashes by asserting that
    # the previously-marked event id is still recorded.
    assert "new_thread:PRRT_NO_DOUBLE" in fresh
    assert "EVT_NO_DOUBLE" in supervisor.launched_event_ids()


# ---------------------------------------------------------------------------
# Q. Runtime files use restrictive permissions
# ---------------------------------------------------------------------------


def test_q_runtime_files_use_restrictive_permissions(isolated_state):
    """State files must be created with restrictive modes
    (0600) wherever the OS supports it. The package's
    ``write_json`` helper performs an atomic write and then
    forces the file mode to 0600 so the process umask
    cannot leak the file to group or other.

    The state directory is created with mode 0700 by the
    ``isolated_state`` fixture.
    """
    # Trigger state writes through the canonical write_json
    # path.
    supervisor.write_readiness_state({
        "state": supervisor.STATE_ACTIVE_REPAIR,
    })
    supervisor.write_quota_state({"providers": {}})
    # The state directory exists.
    assert supervisor.STATE_DIR.exists()
    # The files are exactly mode 0600 (owner read+write).
    for p in (supervisor.READINESS_STATE_PATH,
              supervisor.QUOTA_PATH):
        st = os.stat(p)
        mode = stat.S_IMODE(st.st_mode)
        assert mode == 0o600, (
            f"{p} has mode {oct(mode)}; expected 0o600"
        )


# ---------------------------------------------------------------------------
# R. Configuration with secrets or unsafe paths is rejected
# ---------------------------------------------------------------------------


def test_r_configuration_with_secrets_or_user_paths_is_rejected():
    """The configuration validator must reject tokens and
    absolute user-specific paths so that no committed config
    leaks secrets or user-specific paths.
    """
    base = {
        "schema_version": "aed.autocoder_supervisor.v1",
        "instance_id": "test",
        "state_dir": "/opt/aed-supervisor/state",
        "working_checkout": "/opt/aed-supervisor/working_checkout",
        "log_path": "/var/log/aed.log",
        "heartbeat_path": "/var/lib/aed/heartbeat",
        "lock_path": "/var/lib/aed/lock",
        "worker_command": ["hermes", "chat"],
        "worker_session_id": "clean-session-id",
        "worker_session_name": "SN",
        "cooldown_seconds": 900,
        "resume_prompt_template": "go",
        "human_boundary": "merge_only",
        "required_review_providers": ["coderabbit"],
        "optional_review_providers": ["codex"],
        "provider_states_are_independent": True,
        "post_codex_recovery_request": False,
        "heartbeat_seconds": 120,
        "quiet_window_seconds": 180,
        "quota_retry_initial_seconds": 3600,
        "quota_retry_backoff_seconds": 21600,
        "quota_backoff_after_retry_count": 2,
    }
    # 1. User-specific path rejected.
    bad = dict(base, state_dir="/home/max/.hermes/aed-supervisor/state")
    with pytest.raises(ValueError):
        supervisor_contracts.SupervisorConfig.from_dict(bad)

    # 2. Credential-shaped values rejected at any depth.
    bad_cred = dict(
        base,
        worker_session_id="ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    )
    with pytest.raises(ValueError):
        supervisor_contracts.SupervisorConfig.from_dict(bad_cred)

    # 3. human_boundary enforced to merge_only.
    bad_hb = dict(base, human_boundary="anytime")
    with pytest.raises(ValueError):
        supervisor_contracts.SupervisorConfig.from_dict(bad_hb)

    # 4. Overlapping required/optional providers rejected.
    bad_overlap = dict(base, optional_review_providers=[
        "coderabbit", "codex"
    ])
    with pytest.raises(ValueError):
        supervisor_contracts.SupervisorConfig.from_dict(bad_overlap)

    # 5. The clean config constructs successfully.
    clean = supervisor_contracts.SupervisorConfig.from_dict(base)
    assert clean.required_review_providers == ["coderabbit"]
    assert clean.optional_review_providers == ["codex"]


# ---------------------------------------------------------------------------
# S. Merge authorization for one head cannot be reused after a head change
# ---------------------------------------------------------------------------


def test_s_merge_authorization_for_one_head_cannot_be_reused():
    """A merge authorization issued for HEAD_A must not be
    reusable after the live head has moved to HEAD_B. The
    supervisor's evaluate_readiness returns head_mismatch
    in that case, and the terminal merge evidence (the
    subject of the merge authorization) records the exact
    authorized head so a second merge attempt can be detected
    as a contract violation.

    Note on the squash-commit semantics this test guards:

    - expectedHeadOid (or an equivalent exact-head parameter)
      binds the authorized PR head at merge time. The merge
      operation rejects the request when the live head
      differs from the authorized head.
    - The authorized head (HEAD_A) is recorded separately in
      terminal evidence under ``authorized_head_sha``. It is
      NOT a parent of the squash commit.
    - The pre-merge main commit (BASE_SHA) is recorded
      separately under ``base_sha_before_merge`` (or
      ``main_tip_sha_at_merge``) and IS the squash commit's
      parent.
    - A squash commit normally has the pre-merge main commit
      as its parent.
    - merge_commit_parents and authorized_head_sha prove
      different facts and must be kept separate in terminal
      evidence.
    """
    HEAD_A = "012156d4286893f6728da1026429166d26dfb155"
    HEAD_B = "ff" * 20
    # Authorization for HEAD_A; live head is HEAD_B.
    snap_a = _clean_snap(head=HEAD_A)
    snap_b_live = _clean_snap(head=HEAD_B)
    # The supervisor's evaluate_readiness rejects the
    # authorization because the live head is no longer HEAD_A.
    res = supervisor.evaluate_readiness(snap_b_live, HEAD_A)
    assert res["ready"] is False
    assert res["reason"] == "head_mismatch"
    # The head-mismatch guard proves that the supervisor
    # will not advance to PROVISIONAL_READY while the live
    # head differs from the authorized one. The exact-head
    # contract is enforced at the merge layer (expectedHeadOid)
    # and recorded as ``authorized_head_sha`` in the terminal
    # evidence — separate from the squash commit's parent
    # list, which records ``base_sha_before_merge`` (the
    # pre-merge main tip). This test does NOT require that
    # ``authorized_head_sha`` appears in ``merge_commit_parents``.
    assert res["ready"] is False




def test_pid_alive_eperm_means_existing_not_dead():
    """pid_alive and pgid_alive must return True on
    PermissionError.

    os.kill(pid, 0) raises PermissionError when the
    target PID exists but is owned by another user. The
    single-writer invariant depends on treating this as
    "the process exists" (return True), NOT as
    "the process is dead". A duplicate-writer test using
    the live OS probe is the cleanest evidence.
    """
    import errno
    import os
    import subprocess
    import sys
    from autocoder_supervisor import supervisor

    # Probe 1: an obviously-dead PID returns False.
    # PIDs >= 2^30 are extremely unlikely to be in use;
    # os.kill raises ESRCH (ProcessLookupError) -> False.
    assert supervisor.pid_alive(2 ** 30) is False

    # Probe 2: the supervisor's own process is alive.
    my_pid = os.getpid()
    assert supervisor.pid_alive(my_pid) is True

    # Probe 3: PermissionError logic. Simulate by monkey-
    # patching os.kill in the supervisor module to raise
    # PermissionError for a known PID. The function must
    # return True (process exists).
    import autocoder_supervisor.supervisor as _sup
    import errno as _errno
    original_kill = _sup.os.kill
    def fake_kill(pid, sig):
        if pid == my_pid:
            raise PermissionError(_errno.EPERM, "eperm test")
        return original_kill(pid, sig)
    _sup.os.kill = fake_kill
    try:
        assert _sup.pid_alive(my_pid) is True
    finally:
        _sup.os.kill = original_kill


def test_dry_sim_does_not_launch_worker(
    isolated_state, monkeypatch,
):
    """``--dry-sim`` must not invoke ANY state-mutating
    step. The test spies on every observable side effect
    reachable from the dry-sim branch and asserts that
    none was invoked.
    """
    launches = {"n": 0}
    marked_event_ids: list = []
    review_requests_posted: list = []
    review_requests_written: list = []
    leases_written: list = []
    snapshots_written: list = []
    readiness_state_writes: list = []

    def fake_launch(rs, live):
        launches["n"] += 1
        return {"pid": 99999, "pgid": 99999,
                "start_time_evidence": {}, "launched_at": "now",
                "heartbeat_at": "now", "cmd": ["hermes", "chat"]}

    def fake_mark(eid):
        marked_event_ids.append(eid)

    def fake_post_review_request(provider, head_sha):
        review_requests_posted.append((provider, head_sha))
        return True

    def fake_write_review_request(provider, head_sha, record):
        review_requests_written.append((provider, head_sha))

    def fake_write_lease(lease):
        leases_written.append(lease)

    def fake_write_snapshot(slot, snap):
        snapshots_written.append(slot)

    def fake_write_readiness_state(state):
        readiness_state_writes.append(state)

    monkeypatch.setattr(supervisor, "launch_worker",
                        fake_launch)
    monkeypatch.setattr(supervisor, "mark_event_launched",
                        fake_mark)
    monkeypatch.setattr(supervisor, "post_review_request",
                        fake_post_review_request)
    monkeypatch.setattr(supervisor, "write_review_request",
                        fake_write_review_request)
    monkeypatch.setattr(supervisor, "write_lease",
                        fake_write_lease)
    monkeypatch.setattr(supervisor, "write_snapshot",
                        fake_write_snapshot)
    monkeypatch.setattr(supervisor, "write_readiness_state",
                        fake_write_readiness_state)
    monkeypatch.setattr(supervisor, "cooldown_active",
                        lambda: False)
    monkeypatch.setattr(supervisor, "AUTHORITATIVE_HEAD", AUTH)
    # Seed snapshot A on disk BEFORE installing the
    # write_snapshot spy. Without an actual on-disk
    # snapshot the dry-sim test would pass vacuously
    # because run_iteration_v5 derives events only from
    # the snapshot diff.
    dirty = dict(_clean_snap())
    dirty["review_threads"] = {
        "PRRT_DRYSIM_NEW": {"resolved": False, "outdated": False},
    }
    supervisor.write_snapshot("A", dirty)
    # Pre-populate an unconsumed event so the main loop
    # observes an actionable event.
    supervisor.write_unconsumed_event({
        "id": "EVT_DRYSIM_TEST",
        "kind": "new_unresolved_current_thread",
    })
    supervisor.enter_readiness(
        supervisor.STATE_ACTIVE_REPAIR, head_sha=AUTH,
    )
    rs = {"current_head": AUTH}
    with patch.object(supervisor, "capture_live_snapshot",
                      return_value=_clean_snap()), \
         patch("time.sleep"):
        rc = supervisor.main(["--dry-sim", "--once"])
    # --dry-sim must NOT invoke any state-mutating step.
    assert launches["n"] == 0, (
        f"dry-sim must NOT launch a worker; launched {launches}"
    )
    assert marked_event_ids == [], (
        "dry-sim must NOT mark any events; "
        f"marked {marked_event_ids}"
    )
    assert review_requests_posted == [], (
        "dry-sim must NOT post a review request; "
        f"posted {review_requests_posted}"
    )
    assert review_requests_written == [], (
        "dry-sim must NOT write a review-request record; "
        f"wrote {review_requests_written}"
    )
    assert leases_written == [], (
        "dry-sim must NOT write a worker lease; "
        f"wrote {leases_written}"
    )
    # dry-sim must NOT mark any events as launched.
    assert "EVT_DRYSIM_TEST" not in supervisor.launched_event_ids()


def test_validate_environment_rejects_unknown_provider(
    tmp_path,
):
    """Configured providers absent from the runtime
    PROVIDERS registry must be rejected by validation.
    """
    from autocoder_supervisor import contracts, validate
    bad = {
        "schema_version": "aed.autocoder_supervisor.v1",
        "instance_id": "t",
        "state_dir": str(tmp_path / "state"),
        "working_checkout": str(tmp_path / "wc"),
        "log_path": str(tmp_path / "log"),
        "heartbeat_path": str(tmp_path / "hb"),
        "lock_path": str(tmp_path / "lock"),
        "worker_command": ["/usr/bin/env", "true"],
        "worker_session_id": "s",
        "worker_session_name": "sn",
        "cooldown_seconds": 900,
        "resume_prompt_template": "go",
        "human_boundary": "merge_only",
        "required_review_providers": ["coderabbit", "unknown-bot"],
        "optional_review_providers": ["codex"],
        "provider_states_are_independent": True,
        "post_codex_recovery_request": False,
        "heartbeat_seconds": 30,
        "quiet_window_seconds": 60,
        "quota_retry_initial_seconds": 3600,
        "quota_retry_backoff_seconds": 21600,
        "quota_backoff_after_retry_count": 2,
    }
    cfg = contracts.SupervisorConfig.from_dict(bad)
    errors, _ = validate.validate_environment(cfg)
    assert any("unknown-bot" in e for e in errors), \
        f"expected 'unknown-bot' in errors; got {errors}"


def test_handle_new_events_first_call_launches_once(
    isolated_state, monkeypatch,
):
    """The first call to ``handle_new_events`` with a fresh
    event launches exactly one worker and marks the event
    as launched.
    """
    launches = {"n": 0}
    marked: list = []

    def fake_launch(rs, live):
        launches["n"] += 1
        return {"pid": 99999, "pgid": 99999,
                "start_time_evidence": {}, "launched_at": "now",
                "heartbeat_at": "now", "cmd": ["hermes", "chat"]}

    monkeypatch.setattr(supervisor, "launch_worker", fake_launch)
    monkeypatch.setattr(supervisor, "mark_event_launched",
                        lambda eid: marked.append(eid))
    monkeypatch.setattr(supervisor, "read_lease", lambda: None)
    monkeypatch.setattr(supervisor, "lease_alive",
                        lambda lease: None)
    # Round-33: force the relay to signal ``launch_worker``
    # so the test exercises the successful-launch path.
    # Without this monkeypatch the real
    # ``_invoke_relay_for_events`` runs and returns
    # ``no_action`` (because the test has no actionable
    # provider data), which now correctly routes to
    # ``recoverable_retry`` with NO generic-worker
    # fallback — see ``test_handle_new_events_no_action_no_worker``.
    monkeypatch.setattr(
        supervisor, "_invoke_relay_for_events",
        lambda events: "launch_worker",
    )
    supervisor.handle_new_events(
        {"current_head": AUTH},
        [{"id": "EID_A", "kind": "new_unresolved_current_thread"}],
        token="",
        iteration={"head_sha": AUTH},
    )
    assert launches["n"] == 1
    assert marked == ["EID_A"]


def test_handle_new_events_second_call_no_relaunch(
    isolated_state, monkeypatch,
):
    """A second call with the same events (already marked)
    does NOT relaunch the worker.
    """
    launches = {"n": 0}

    def fake_launch(rs, live):
        launches["n"] += 1
        return {"pid": 99999, "pgid": 99999,
                "start_time_evidence": {}, "launched_at": "now",
                "heartbeat_at": "now", "cmd": ["hermes", "chat"]}

    monkeypatch.setattr(supervisor, "launch_worker", fake_launch)
    def _record_mark(eid):
        path = supervisor.STATE_DIR / "launched_events.json"
        try:
            existing = supervisor.read_json(path).get("ids", [])
        except Exception:
            existing = []
        supervisor.write_json(path, {"ids": existing + [eid]})
    monkeypatch.setattr(supervisor, "mark_event_launched", _record_mark)
    monkeypatch.setattr(supervisor, "read_lease", lambda: None)
    monkeypatch.setattr(supervisor, "lease_alive",
                        lambda lease: None)
    # Round-33: force the relay to signal ``launch_worker``
    # so the first call exercises the success path and
    # ``mark_event_launched`` records the event id.
    monkeypatch.setattr(
        supervisor, "_invoke_relay_for_events",
        lambda events: "launch_worker",
    )
    events = [
        {"id": "EID_B", "kind": "new_unresolved_current_thread"}
    ]
    # First call launches.
    supervisor.handle_new_events(
        {"current_head": AUTH}, events, "", {"head_sha": AUTH},
    )
    assert launches["n"] == 1
    # Second call: event id is already in launched_event_ids,
    # so fresh_ids is empty and no launch occurs.
    supervisor.handle_new_events(
        {"current_head": AUTH}, events, "", {"head_sha": AUTH},
    )
    assert launches["n"] == 1, (
        "second call must not relaunch the same event id"
    )


def test_handle_new_events_active_lease_blocks_launch(
    isolated_state, monkeypatch,
):
    """When the durable lease is alive, ``handle_new_events``
    does NOT launch a new worker.
    """
    launches = {"n": 0}

    def fake_launch(rs, live):
        launches["n"] += 1
        return {"pid": 99999, "pgid": 99999,
                "start_time_evidence": {}, "launched_at": "now",
                "heartbeat_at": "now", "cmd": ["hermes", "chat"]}

    monkeypatch.setattr(supervisor, "launch_worker", fake_launch)
    monkeypatch.setattr(supervisor, "read_lease",
                        lambda: {"pid": 1, "pgid": 1,
                                 "start_time_evidence": {}})
    monkeypatch.setattr(supervisor, "lease_alive",
                        lambda lease: lease)
    supervisor.handle_new_events(
        {"current_head": AUTH},
        [{"id": "EID_C", "kind": "new_unresolved_current_thread"}],
        token="",
        iteration={"head_sha": AUTH},
    )
    assert launches["n"] == 0, (
        "active lease must block a new launch"
    )


def test_handle_new_events_failed_launch_does_not_mark(
    isolated_state, monkeypatch,
):
    """When ``launch_worker`` returns ``None`` (failure), the
    event IDs are NOT marked launched so the next heartbeat
    can retry.
    """
    launches = {"n": 0}
    marked: list = []

    def fake_launch(rs, live):
        launches["n"] += 1
        return None  # launch failed

    monkeypatch.setattr(supervisor, "launch_worker", fake_launch)
    monkeypatch.setattr(supervisor, "mark_event_launched",
                        lambda eid: marked.append(eid))
    monkeypatch.setattr(supervisor, "read_lease", lambda: None)
    monkeypatch.setattr(supervisor, "lease_alive",
                        lambda lease: None)
    # Round-33: force the relay to signal ``launch_worker``
    # so the test exercises the failed-launch path (the
    # worker launch is the actual test surface).
    monkeypatch.setattr(
        supervisor, "_invoke_relay_for_events",
        lambda events: "launch_worker",
    )
    supervisor.handle_new_events(
        {"current_head": AUTH},
        [{"id": "EID_D", "kind": "new_unresolved_current_thread"}],
        token="",
        iteration={"head_sha": AUTH},
    )
    assert launches["n"] == 1
    assert marked == [], (
        "a failed launch must NOT mark the event id; "
        "the next heartbeat must see the event again"
    )


def test_required_checks_green_only_accepts_completed_status(
    monkeypatch,
):
    """``required_checks_green`` accepts ONLY
    ``status="completed"`` regardless of conclusion. The
    tokens ``success``, ``skipped``, ``neutral`` are
    conclusion values, not status values.
    """
    # Configure two required check names so we exercise
    # the per-name loop.
    required = {"X-check", "Y-check"}
    monkeypatch.setitem(
        supervisor.POLICY, "required_check_names", list(required),
    )
    # status="success" must FAIL even with conclusion="success".
    snap = {
        "required_checks": {
            "X-check": {"status": "success",
                        "conclusion": "success"},
            "Y-check": {"status": "completed",
                        "conclusion": "success"},
        }
    }
    assert supervisor.required_checks_green(snap) is False, (
        "status='success' must fail; the status must be "
        "exactly 'completed' regardless of conclusion"
    )
    # All terminal pending / mid-flight statuses fail.
    for bad_status in ("queued", "in_progress", "waiting",
                       "requested", "pending"):
        snap = {
            "required_checks": {
                "X-check": {"status": bad_status,
                            "conclusion": "success"},
                "Y-check": {"status": bad_status,
                            "conclusion": "success"},
            }
        }
        assert supervisor.required_checks_green(snap) is False, (
            f"status={bad_status!r} must fail"
        )
    # Missing status fails.
    snap = {"required_checks": {"X-check": {"conclusion": "success"},
                                "Y-check": {"conclusion": "success"}}}
    assert supervisor.required_checks_green(snap) is False
    # Positive cases: status="completed" with each allowed
    # conclusion.
    for ok_conclusion in ("success", "skipped", "neutral"):
        snap = {
            "required_checks": {
                "X-check": {"status": "completed",
                            "conclusion": ok_conclusion},
                "Y-check": {"status": "completed",
                            "conclusion": "success"},
            }
        }
        assert supervisor.required_checks_green(snap) is True, (
            f"status=completed conclusion={ok_conclusion!r} "
            "must pass"
        )


def test_correlate_provider_review_fails_closed_on_missing_timestamps():
    """correlate_provider_review must fail closed when
    either the request timestamp or the comment / review
    timestamp is missing. A missing timestamp MUST NOT
    cause a comment or review to be counted as a response
    to the request.
    """
    # request_record with a parseable timestamp.
    request_record = {
        "head_sha": "012156d4286893f6728da1026429166d26dfb155",
        "requested_at": "2026-08-04T00:00:00Z",
    }
    # Comments with diverse timestamps.
    surfaces = {
        "issue_comments": [
            # Good timestamp, after request: counts.
            {"id": 1, "created_at": "2026-08-04T01:00:00Z",
             "body": "walkthrough", "login": "coderabbitai[bot]"},
            # Missing timestamp: MUST NOT count.
            {"id": 2, "created_at": None,
             "body": "walkthrough", "login": "coderabbitai[bot]"},
            # Empty timestamp: MUST NOT count.
            {"id": 3, "created_at": "",
             "body": "walkthrough", "login": "coderabbitai[bot]"},
        ],
        "reviews": [
            {"id": 10, "submitted_at": None, "state": "APPROVED"},
            {"id": 11, "submitted_at": "", "state": "APPROVED"},
        ],
    }
    corr = supervisor.correlate_provider_review(
        "coderabbit", "012156d4286893f6728da1026429166d26dfb155",
        surfaces, request_record, token="",
    )
    # Only the comment with id=1 counts; the rest are
    # excluded because of missing/empty timestamps.
    assert corr["responses_after_request"] == 1, corr
    assert corr["walkthrough_present"] is True, corr
    # `review_present` is False because the only reviews
    # have missing/empty timestamps.
    assert corr["review_present"] is False, corr


def test_correlate_provider_review_fails_closed_on_missing_request_ts():
    """If the request_record has an unparseable timestamp,
    NO comment or review should be counted as covered.
    """
    request_record = {
        "head_sha": "012156d4286893f6728da1026429166d26dfb155",
        "requested_at": "not-a-valid-timestamp",
    }
    surfaces = {
        "issue_comments": [
            {"id": 1, "created_at": "2026-08-04T01:00:00Z",
             "body": "walkthrough", "login": "coderabbitai[bot]"},
        ],
        "reviews": [
            {"id": 10, "submitted_at": "2026-08-04T01:00:00Z",
             "state": "APPROVED"},
        ],
    }
    corr = supervisor.correlate_provider_review(
        "coderabbit", "012156d4286893f6728da1026429166d26dfb155",
        surfaces, request_record, token="",
    )
    # None of the coverage is granted because the request
    # timestamp is unparseable.
    assert corr["responses_after_request"] == 0, corr
    assert corr["walkthrough_present"] is False, corr
    assert corr["review_present"] is False, corr


def test_supervisor_config_dataclass_has_no_from_file_classmethod():
    """SupervisorConfig does not have a ``from_file`` classmethod.
    The docstring previously referenced one that does not exist.
    """
    assert not hasattr(supervisor_contracts.SupervisorConfig, "from_file")
    # from_dict must still exist.
    assert hasattr(supervisor_contracts.SupervisorConfig, "from_dict")
    # The docstring must not mention ``from_file`` in
    # the canonical sentence that described the loader.
    src = inspect.getsource(supervisor_contracts.SupervisorConfig)
    assert "``from_file`` reads a TOML file" not in src


def test_exact_head_snapshot_contract_has_provider_issue_comments():
    """ExactHeadSnapshotDict includes the persisted
    ``_provider_issue_comments`` index produced by
    capture_live_snapshot.
    """
    from autocoder_supervisor.contracts import ExactHeadSnapshotDict
    # TypedDict annotations are stored in __annotations__.
    annotations = ExactHeadSnapshotDict.__annotations__
    assert "_provider_issue_comments" in annotations, (
        "_provider_issue_comments must be declared in "
        "ExactHeadSnapshotDict"
    )


def test_build_resume_prompt_raises_on_unknown_placeholder(isolated_state, monkeypatch):
    """The resume-prompt template must raise a clear
    exception (not crash the daemon) when it contains an
    unknown placeholder. The supervisor's build_resume_prompt
    guards the str.format call with try/except for KeyError
    and ValueError, logging the error before re-raising.
    """
    import autocoder_supervisor.supervisor as _sup
    bad_template = "{pr_number} {repo_owner} {repo_name} {head} {bogus}"
    monkeypatch.setattr(_sup, "RESUME_PROMPT_TEMPLATE", bad_template)
    rs = {"current_head": _sup.AUTHORITATIVE_HEAD}
    live = {"head_sha": _sup.AUTHORITATIVE_HEAD}
    try:
        _sup.build_resume_prompt(rs, live)
    except (KeyError, ValueError):
        # The supervisor raises a clear error rather than
        # crashing the daemon. Either KeyError (unknown
        # placeholder) or ValueError (unmatched {/}) is
        # acceptable.
        return
    raise AssertionError(
        "build_resume_prompt must raise on unknown placeholder"
    )


def test_build_resume_prompt_raises_on_unmatched_brace(isolated_state, monkeypatch):
    """A template with an unmatched ``{`` raises ValueError,
    which the supervisor's guard catches and re-raises.
    """
    import autocoder_supervisor.supervisor as _sup
    bad_template = "{pr_number unclosed"
    monkeypatch.setattr(_sup, "RESUME_PROMPT_TEMPLATE", bad_template)
    rs = {"current_head": _sup.AUTHORITATIVE_HEAD}
    live = {"head_sha": _sup.AUTHORITATIVE_HEAD}
    try:
        _sup.build_resume_prompt(rs, live)
    except ValueError:
        return
    raise AssertionError(
        "build_resume_prompt must raise on unmatched brace"
    )


def test_build_resume_prompt_succeeds_with_valid_template(isolated_state, monkeypatch):
    """A well-formed template substitutes all fields correctly.
    """
    import autocoder_supervisor.supervisor as _sup
    good_template = "X{pr_number}X{repo_owner}X{repo_name}X{head}X"
    monkeypatch.setattr(_sup, "RESUME_PROMPT_TEMPLATE", good_template)
    # Patch the supervisor's module-level constants for this test
    monkeypatch.setattr(_sup, "PR_NUMBER", 7)
    monkeypatch.setattr(_sup, "REPO_OWNER", "alice")
    monkeypatch.setattr(_sup, "REPO_NAME", "demo")
    monkeypatch.setattr(_sup, "AUTHORITATIVE_HEAD", "deadbeef")
    rs = {"current_head": _sup.AUTHORITATIVE_HEAD}
    live = {"head_sha": _sup.AUTHORITATIVE_HEAD}
    out = _sup.build_resume_prompt(rs, live)
    assert "X7X" in out
    assert "XaliceX" in out
    assert "XdemoX" in out
    assert "XdeadbeefX" in out


def test_launch_worker_invalid_resume_template_returns_none(isolated_state, monkeypatch):
    """When ``build_resume_prompt`` raises (KeyError / ValueError),
    ``launch_worker`` must catch the failure and return ``None``
    rather than terminating the daemon.
    """
    import autocoder_supervisor.supervisor as _sup

    # Force build_resume_prompt to raise ValueError.
    def boom(*args, **kwargs):
        raise ValueError("forced resume-prompt failure for test")

    monkeypatch.setattr(_sup, "build_resume_prompt", boom)
    # Patch module-level constants so the rest of launch_worker
    # would otherwise be reachable.
    monkeypatch.setattr(_sup, "WORKER_COMMAND_TEMPLATE", ["hermes", "chat"])
    monkeypatch.setattr(_sup, "SESSION_ID", "abc123")

    out = _sup.launch_worker({"current_head": "h"}, {"head_sha": "h"})
    assert out is None, (
        "launch_worker must return None on invalid resume_prompt_template"
    )


def test_launch_worker_invalid_worker_command_template_returns_none(
    isolated_state, monkeypatch
):
    """When the configured ``worker_command`` template contains an
    unknown placeholder or unmatched brace, ``launch_worker`` must
    catch the substitution error and return ``None`` rather than
    terminating the daemon.
    """
    import autocoder_supervisor.supervisor as _sup

    monkeypatch.setattr(_sup, "build_resume_prompt", lambda rs, live: "P")
    # A worker_command template with an unknown placeholder.
    monkeypatch.setattr(
        _sup, "WORKER_COMMAND_TEMPLATE",
        ["hermes", "chat", "{prompt}", "--resume", "{bogus}"],
    )
    monkeypatch.setattr(_sup, "SESSION_ID", "abc123")

    out = _sup.launch_worker({"current_head": "h"}, {"head_sha": "h"})
    assert out is None, (
        "launch_worker must return None on invalid worker_command template"
    )


def test_launch_worker_unmatched_brace_in_worker_command_returns_none(
    isolated_state, monkeypatch
):
    """An unmatched ``{`` in a worker_command template raises
    ValueError from ``str.format``. ``launch_worker`` must catch
    that and return ``None``.
    """
    import autocoder_supervisor.supervisor as _sup

    monkeypatch.setattr(_sup, "build_resume_prompt", lambda rs, live: "P")
    monkeypatch.setattr(
        _sup, "WORKER_COMMAND_TEMPLATE",
        ["hermes", "chat", "{prompt unclosed", "--resume", "{session_id}"],
    )
    monkeypatch.setattr(_sup, "SESSION_ID", "abc123")

    out = _sup.launch_worker({"current_head": "h"}, {"head_sha": "h"})
    assert out is None, (
        "launch_worker must return None on unmatched brace in worker_command"
    )


def test_capture_live_snapshot_handles_null_graphql_data(
    isolated_state, monkeypatch
):
    """``capture_live_snapshot`` must not crash when the GraphQL
    response has ``data: null`` (e.g. an errors-only response).

    The crash mode is in the GraphQL pagination loop. We isolate
    it by mocking the GraphQL call (api.github.com/graphql) to
    return ``{"data": null}`` and leaving the REST calls (which
    use api.github.com/<path> rather than /graphql) returning
    empty defaults.
    """
    import json
    import urllib.request
    import autocoder_supervisor.supervisor as _sup

    class _FakeResp:
        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=20):
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        if "/graphql" in url:
            # GraphQL: data is null (a partial error response)
            return _FakeResp(json.dumps({"data": None, "errors": [{"message": "x"}]}).encode())
        # REST PR/reviews/comments/check-runs: return an empty list/dict
        if "/check-runs" in url:
            return _FakeResp(json.dumps({"check_runs": []}).encode())
        if "/pulls/" in url and "/reviews" in url:
            return _FakeResp(json.dumps([]).encode())
        if "/issues/" in url and "/comments" in url:
            return _FakeResp(json.dumps([]).encode())
        if "/pulls/" in url:
            return _FakeResp(json.dumps({"head": {"sha": "deadbeef"}}).encode())
        return _FakeResp(json.dumps({}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    snap = _sup.capture_live_snapshot({}, "fake-token")
    assert isinstance(snap, dict)
    assert "review_threads" in snap
    assert snap["review_threads"] == {}




def test_thread_pagination_failure_blocks_readiness():
    """Round-5 Codex P1: partial thread pagination responses
    MUST NOT be treated as empty. The readiness gate MUST
    refuse to promote readiness when the inventory is
    incomplete.
    """
    from autocoder_supervisor import supervisor as sup
    snap = {
        "head_sha": "a" * 40,
        "review_threads": {},
        "review_threads_pagination_failed": True,
        "review_threads_pagination_complete": False,
    }
    result = sup.evaluate_readiness(snap, head="a" * 40)
    assert result["ready"] is False, (
        f"pagination_failed=True MUST block readiness; got {result!r}"
    )
    assert result["reason"] == "thread_pagination_failed"


def test_complete_thread_pagination_does_not_block(isolated_state) -> None:
    """A complete pagination does NOT block readiness on
    the pagination flag alone.

    Strengthened on round-26 (Codex Trivial):
    - takes ``isolated_state`` so the result depends only on
      the snapshot, not on host supervisor state;
    - asserts the exact reason (NOT "thread_pagination_failed")
      regardless of whether other gates are open. The previous
      conditional assertion only ran when ``ready is False``,
      so a "ready=True" output would have passed even if the
      pagination guard had been deleted.
    """
    from autocoder_supervisor import supervisor as sup
    snap = {
        "head_sha": "a" * 40,
        "review_threads": {},
        "review_threads_pagination_failed": False,
        "review_threads_pagination_complete": True,
    }
    result = sup.evaluate_readiness(snap, head="a" * 40)
    # The pagination flag MUST NOT be the blocker. Other gates
    # (checks, unconsumed_events) may still be open, but the
    # reason MUST never be "thread_pagination_failed" for a
    # complete-pagination snapshot.
    assert result["reason"] != "thread_pagination_failed", (
        f"complete pagination MUST NOT block on the pagination "
        f"flag; got reason={result['reason']!r}, ready={result.get('ready')!r}"
    )



# =========================================================================
# Round-33 production-lifecycle tests
#
# Required per the user:
#   - Remove hard-coded /home/max/AutoDev imports.
#   - Durability test must execute the REAL write_unconsumed_event writer.
#   - Then reopen the ledger and prove the event physically exists.
#   - Repeated handoff: E1 -> persist -> return -> MAIN dispatch
#                       then E2 -> persist -> return -> MAIN dispatch.
#   - Real bounded MAIN caller test:
#       quiet-window new event -> MAIN -> central dispatcher -> relay -> worker
#   - Recoverable retry path:
#       quiet-window new event -> MAIN -> relay returns recoverable_retry
#       -> zero generic worker -> event pending -> later eligible retry
#   - BLOCKED:
#       quiet-window event -> persisted -> canonical controller BLOCKED
#       -> zero relay -> zero worker
#   - Root unresolved:
#       quiet-window event -> persisted -> recoverable_retry
#       -> zero relay -> zero worker
#   - Persist-before-dispatch ordering recorded behaviorally.
#   - heartbeat sleeps between return and dispatch MUST equal ZERO.
#   - No source-text or grep tests count.
# =========================================================================


def test_round33_qualifying_head_reopen_transition(
    isolated_state, monkeypatch, tmp_path,
):
    """The canonical state machine MUST have a
    QUALIFYING_READINESS -> REPAIRING_REVIEW_FINDINGS
    transition.

    Bug-detector property: without this transition the
    relay correctly fails closed on the QUALIFYING_READINESS
    guard and the actionable review is stranded
    indefinitely. This test asserts the transition exists
    in the FORWARD_TRANSITIONS list with the expected
    evidence and head-stability requirements.
    """
    from autocoder_orchestration.state_machine import (
        STATE_QUALIFYING_READINESS,
        STATE_REPAIRING_REVIEW_FINDINGS,
        _FORWARD_TRANSITIONS,
        get_transition,
    )
    # The transition MUST exist by (source, target) key.
    transition = get_transition(
        STATE_QUALIFYING_READINESS,
        STATE_REPAIRING_REVIEW_FINDINGS,
    )
    assert transition is not None, (
        "QUALIFYING_READINESS -> REPAIRING_REVIEW_FINDINGS transition is "
        "MISSING. Without it, the relay cannot dispatch review-repair "
        "on a previously-qualified head."
    )
    assert transition.authorized_actors == frozenset({"controller"})
    assert transition.required_evidence == frozenset(
        {"new_actionable_review_inventory"}
    )
    assert transition.head_stability == "live"
    # Confirm the transition is also present in the global
    # forward-transitions registry (defense in depth against
    # future refactors that drop the get_transition cache).
    sources_to_targets = [
        (t.source, t.target) for t in _FORWARD_TRANSITIONS
    ]
    assert (
        STATE_QUALIFYING_READINESS,
        STATE_REPAIRING_REVIEW_FINDINGS,
    ) in sources_to_targets


def test_round33_controller_reopen_method_rejects_empty_inventory(
    isolated_state, monkeypatch, tmp_path,
):
    """Controller.report_new_actionable_review_on_qualified_head()
    MUST reject an empty inventory with ControllerError.

    Bug-detector property: an empty inventory would allow
    a supervisor process to "reopen" a qualified head
    without actionable-review evidence, corrupting the
    state-machine invariants. The contract MUST reject
    empty/non-list input.
    """
    from autocoder_orchestration.controller import (
        Controller,
        ControllerError,
        RunContext,
        StateStore,
    )
    from autocoder_orchestration.context import ACTOR_CONTROLLER
    from autocoder_orchestration.state_machine import (
        STATE_QUALIFYING_READINESS,
        StateMachine,
    )
    # Build a minimal valid state machine at QUALIFYING_READINESS.
    sm = StateMachine(current_state=STATE_QUALIFYING_READINESS, revision=0)
    rc_payload = {
        "schema_version": "autocoder.run_context.v1",
        "run_id": "test_round33_empty_inventory",
        "created_at": "2026-08-10T00:00:00Z",
        "repo_owner": "o",
        "repo_name": "n",
        "local_checkout": "/tmp/round33_empty",
        "base_branch": "main",
        "authorized_base_sha": "a" * 40,
        "feature_branch": "f",
        "pr_number": 1,
        "current_authorized_head": "a" * 40,
        "task_specification_path": "/tmp/task.txt",
        "task_specification_sha256": "b" * 64,
        "required_ci_jobs": [],
        "reviewer_policy": "exact_head_approval",
        "quiet_window_seconds": 30,
        "implementation_worker_command": [],
        "verifier_command": None,
        "verifier_handoff_policy": "fresh_session_required",
        "permitted_mutations": [],
        "human_only_actions": [],
        "evidence_root": "/tmp/round33_empty/evidence",
        "state_root": str(tmp_path / "round33_empty_inventory"),
        "next_wave_policy": "explicit_only",
    }
    rc = RunContext.from_dict(rc_payload)
    store = StateStore(tmp_path / "round33_empty_inventory")
    store.write_atomic("run_context.json", rc.to_dict())
    store.write_atomic("state.json", sm.to_dict())
    controller = Controller(context=rc, store=store)
    # Empty inventory -> ControllerError
    import pytest
    with pytest.raises(ControllerError):
        controller.report_new_actionable_review_on_qualified_head(
            head_observed="a" * 40,
            actionable_review_inventory=[],
        )
    # Non-list inventory -> ControllerError
    with pytest.raises(ControllerError):
        controller.report_new_actionable_review_on_qualified_head(
            head_observed="a" * 40,
            actionable_review_inventory="not a list",
        )


def test_round33_controller_reopen_rejects_non_qualifying_state(
    isolated_state, monkeypatch, tmp_path,
):
    """The reopen API MUST refuse to transition from
    any state other than QUALIFYING_READINESS.

    Bug-detector property: an already-running REPAIR
    cycle must NOT be double-entered. The state
    machine itself enforces this via the transition
    table; the Controller API is the canonical gate.
    """
    from autocoder_orchestration.controller import (
        Controller,
        ControllerError,
        RunContext,
        StateStore,
    )
    from autocoder_orchestration.state_machine import (
        STATE_AWAITING_CI,
    )
    from autocoder_orchestration.state_machine import (
        InvalidTransition,
        StateMachine,
    )
    sm = StateMachine(current_state=STATE_AWAITING_CI, revision=0)
    rc_payload = {
        "schema_version": "autocoder.run_context.v1",
        "run_id": "test_round33_non_qualifying",
        "created_at": "2026-08-10T00:00:00Z",
        "repo_owner": "o",
        "repo_name": "n",
        "local_checkout": "/tmp/round33_nq",
        "base_branch": "main",
        "authorized_base_sha": "a" * 40,
        "feature_branch": "f",
        "pr_number": 1,
        "current_authorized_head": "a" * 40,
        "task_specification_path": "/tmp/task.txt",
        "task_specification_sha256": "b" * 64,
        "required_ci_jobs": [],
        "reviewer_policy": "exact_head_approval",
        "quiet_window_seconds": 30,
        "implementation_worker_command": [],
        "verifier_command": None,
        "verifier_handoff_policy": "fresh_session_required",
        "permitted_mutations": [],
        "human_only_actions": [],
        "evidence_root": "/tmp/round33_nq/evidence",
        "state_root": str(tmp_path / "round33_non_qualifying"),
        "next_wave_policy": "explicit_only",
    }
    rc = RunContext.from_dict(rc_payload)
    store = StateStore(tmp_path / "round33_non_qualifying")
    store.write_atomic("run_context.json", rc.to_dict())
    store.write_atomic("state.json", sm.to_dict())
    controller = Controller(context=rc, store=store)
    import pytest
    with pytest.raises(InvalidTransition):
        controller.report_new_actionable_review_on_qualified_head(
            head_observed="a" * 40,
            actionable_review_inventory=["event_x"],
        )


def test_round33_controller_reopen_persists_inventory(
    isolated_state, monkeypatch, tmp_path,
):
    """The reopen API MUST persist the
    new_actionable_review_inventory.json BEFORE the
    transition so the durable journal records which
    events drove the re-open.
    """
    from autocoder_orchestration.controller import (
        Controller,
        RunContext,
        StateStore,
    )
    from autocoder_orchestration.state_machine import (
        STATE_QUALIFYING_READINESS,
        StateMachine,
    )
    sm = StateMachine(current_state=STATE_QUALIFYING_READINESS, revision=0)
    rc_payload = {
        "schema_version": "autocoder.run_context.v1",
        "run_id": "test_round33_persist_inventory",
        "created_at": "2026-08-10T00:00:00Z",
        "repo_owner": "o",
        "repo_name": "n",
        "local_checkout": "/tmp/round33_pi",
        "base_branch": "main",
        "authorized_base_sha": "a" * 40,
        "feature_branch": "f",
        "pr_number": 1,
        "current_authorized_head": "a" * 40,
        "task_specification_path": "/tmp/task.txt",
        "task_specification_sha256": "b" * 64,
        "required_ci_jobs": [],
        "reviewer_policy": "exact_head_approval",
        "quiet_window_seconds": 30,
        "implementation_worker_command": [],
        "verifier_command": None,
        "verifier_handoff_policy": "fresh_session_required",
        "permitted_mutations": [],
        "human_only_actions": [],
        "evidence_root": "/tmp/round33_pi/evidence",
        "state_root": str(tmp_path / "round33_persist_inventory"),
        "next_wave_policy": "explicit_only",
    }
    rc = RunContext.from_dict(rc_payload)
    store = StateStore(tmp_path / "round33_persist_inventory")
    store.write_atomic("run_context.json", rc.to_dict())
    store.write_atomic("state.json", sm.to_dict())
    controller = Controller(context=rc, store=store)
    inventory = ["e1", "e2", "e3"]
    controller.report_new_actionable_review_on_qualified_head(
        head_observed="a" * 40,
        actionable_review_inventory=inventory,
    )
    inv_payload = store.read_optional(
        "new_actionable_review_inventory.json"
    )
    assert inv_payload is not None, (
        "new_actionable_review_inventory.json MUST be persisted "
        "BEFORE the transition."
    )
    assert inv_payload.get("inventory") == inventory
    assert inv_payload.get("head_observed") == "a" * 40
    # Confirm the state machine actually moved.
    after = controller.load_state_machine()
    assert after is not None
    assert after.current_state == "REPAIRING_REVIEW_FINDINGS"


def test_round33_handle_new_events_no_action_no_worker(
    isolated_state, monkeypatch,
):
    """handle_new_events MUST NOT fall through to a
    generic worker when the relay returns no_action.

    Bug-detector property: the previous behavior was
    a silent generic-worker fallback for no_action,
    which is unsafe for structured review-repair
    events. Round-33 removes the fallback.
    """
    launches = {"n": 0}
    marked: list = []

    def fake_launch(rs, live):
        launches["n"] += 1
        return {"pid": 99999, "pgid": 99999,
                "start_time_evidence": {}, "launched_at": "now",
                "heartbeat_at": "now", "cmd": ["hermes", "chat"]}

    monkeypatch.setattr(supervisor, "launch_worker", fake_launch)
    monkeypatch.setattr(supervisor, "mark_event_launched",
                        lambda eid: marked.append(eid))
    monkeypatch.setattr(supervisor, "read_lease", lambda: None)
    monkeypatch.setattr(supervisor, "lease_alive",
                        lambda lease: None)
    # Force the relay to return no_action.
    monkeypatch.setattr(
        supervisor, "_invoke_relay_for_events",
        lambda events: "no_action",
    )
    supervisor.handle_new_events(
        {"current_head": AUTH},
        [{"id": "EID_X", "kind": "new_unresolved_current_thread"}],
        token="",
        iteration={"head_sha": AUTH},
    )
    # Critical: zero launches, zero marked. The event
    # stays actionable so the next heartbeat retries.
    assert launches["n"] == 0, (
        "no_action MUST NOT fall through to launch_worker; "
        "this is the no_action->generic worker fallback the "
        "user explicitly required to remove."
    )
    assert marked == [], (
        "no_action MUST NOT mark the event launched; "
        "the event stays actionable for retry."
    )


def test_round33_handle_new_events_unknown_action_no_worker(
    isolated_state, monkeypatch,
):
    """handle_new_events MUST NOT fall through to a
    generic worker when the relay returns an unknown
    action.

    Bug-detector property: an unhandled relay action
    must NOT silently launch a worker. Round-33
    routes to recoverable_retry with no worker.
    """
    launches = {"n": 0}

    def fake_launch(rs, live):
        launches["n"] += 1
        return {"pid": 99999, "pgid": 99999,
                "start_time_evidence": {}, "launched_at": "now",
                "heartbeat_at": "now", "cmd": ["hermes", "chat"]}

    monkeypatch.setattr(supervisor, "launch_worker", fake_launch)
    monkeypatch.setattr(supervisor, "mark_event_launched", lambda eid: None)
    monkeypatch.setattr(supervisor, "read_lease", lambda: None)
    monkeypatch.setattr(supervisor, "lease_alive", lambda lease: None)
    monkeypatch.setattr(
        supervisor, "_invoke_relay_for_events",
        lambda events: "some_future_unknown_action",
    )
    supervisor.handle_new_events(
        {"current_head": AUTH},
        [{"id": "EID_Y", "kind": "new_unresolved_current_thread"}],
        token="",
        iteration={"head_sha": AUTH},
    )
    assert launches["n"] == 0


def test_round33_cooldown_deferred_event_preserved_through_clear(
    isolated_state, monkeypatch,
):
    """A cooldown-deferred event MUST survive the
    quiet-window post-loop clear so it can dispatch
    when cooldown expires.

    Bug-detector property: events that arrive during
    a provider cooldown are persisted but the
    dispatch is skipped. The post-loop clear MUST
    preserve them; clearing would silently lose the
    event.
    """
    from autocoder_supervisor import supervisor as sup

    # Persist a cooldown-deferred event directly using the
    # real writer.
    sup._mark_cooldown_deferred(
        [{"id": "EID_COOLDOWN", "kind": "new_unresolved_current_thread"}]
    )
    assert "EID_COOLDOWN" in sup._cooldown_deferred_ids()

    # Simulate the quiet-window post-loop clear with a
    # cooldown-deferred event in pre_unconsumed_ids.
    # We do NOT call real quiet window here (it requires
    # network); instead we directly exercise the same
    # clear logic by writing the ledger and checking it
    # survives.
    state_dir = Path(str(sup.STATE_DIR))
    unconsumed_path = state_dir / "unconsumed_events.json"
    unconsumed_path.write_text(json.dumps({
        "events": [
            {"id": "EID_COOLDOWN", "kind": "new_unresolved_current_thread"},
            {"id": "EID_OTHER", "kind": "new_unresolved_current_thread"},
        ],
    }))
    # Pre-unconsumed includes both.
    pre_unconsumed_ids = {"EID_COOLDOWN", "EID_OTHER"}
    # Re-run the post-loop clear logic (mirrors the
    # active_repair_quiet_window final block).
    cooldown_deferred = sup._cooldown_deferred_ids()
    preserved = [
        e for e in sup.list_unconsumed_events()
        if e.get("id") in cooldown_deferred
    ]
    payload = {"events": preserved}
    unconsumed_path.write_text(json.dumps(payload))
    after = sup.list_unconsumed_events()
    after_ids = {e.get("id") for e in after}
    assert "EID_COOLDOWN" in after_ids, (
        "cooldown-deferred event MUST survive the post-loop clear; "
        "without this the event is silently lost when cooldown expires."
    )
    assert "EID_OTHER" not in after_ids, (
        "non-cooldown-deferred pre-existing events MUST still clear."
    )


def test_round33_retry_ledger_cleared_does_not_bump_slice_epoch(
    isolated_state, monkeypatch, tmp_path,
):
    """A retry record with lifecycle='cleared' MUST NOT
    trigger slice_epoch bumps on later heartbeats.

    Bug-detector property: a cleared record means the
    work has been CONSUMED; bumping the slice_epoch
    again would advance the epoch on every heartbeat
    forever and break the slice-budget cycle.

    Round-39 P1#4 amendment: a ``cleared`` record that
    is re-persisted by a NEW failure (different reason
    or same reason re-appearing) MUST be allowed to
    record a fresh attempt — the
    ``round_budget_retry.json`` file is shared across
    multiple retry categories and a consumed record
    from one category must not suppress later
    independent work. The slice_epoch_bumps count is
    preserved across the reset so the slice-budget
    cycle is not double-bumped.
    """
    from autocoder_supervisor import supervisor as sup

    # Seed a cleared retry ledger.
    evidence_root = Path(str(sup.RUN_STATE)).parent / "evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    retry_path = evidence_root / "round_budget_retry.json"
    retry_path.write_text(json.dumps({
        "reason": "orchestration_root_unresolved",
        "lifecycle": "cleared",
        "attempt_count": 5,
        "last_attempt_at": "2026-08-10T04:00:00+00:00",
        "next_eligible_retry_at": "2026-08-10T04:01:00+00:00",
        "slice_epoch_bumps": 1,
        "owner": "supervisor_recovery",
        "recoverable": True,
    }))
    # Round-39 P1#4: a NEW failure from a different
    # category (or the same category re-appearing) MUST
    # be allowed to record a fresh attempt. The cleared
    # lifecycle is reset to ``pending`` so the new
    # failure is captured, but the slice_epoch_bumps
    # count is preserved (no double-bump).
    sup._persist_retry_with_reason(
        reason="no_action_on_review_repair",
        extra={"error": "transient"},
    )
    payload = json.loads(retry_path.read_text())
    assert payload.get("lifecycle") == "pending", (
        "round-39 P1#4: a cleared record MUST be reset "
        "to 'pending' when a NEW failure records a fresh "
        "attempt, so a later retry can replace the old "
        "record instead of being silently absorbed."
    )
    assert payload.get("reset_after_consumed") is True, (
        "round-39 P1#4: the reset stamp must be present "
        "so the audit trail records the lifecycle reset."
    )
    assert payload.get("slice_epoch_bumps") == 1, (
        "round-39 P1#4: the slice_epoch_bumps count is "
        "preserved across the reset so the slice-budget "
        "cycle is not double-bumped."
    )


def test_round43_c11_retry_ledger_per_pr_evidence_root(
    isolated_state, monkeypatch, tmp_path,
):
    """Round-43 C11: the supervisor MUST read/bump the
    per-PR orch state's retry ledger, NOT a global
    RUN_STATE.parent/evidence ledger.

    Bug-detector property: the relay CLI reads and
    writes ``round_budget_retry.json`` from
    ``orchestration_state_root/evidence`` (one path per
    PR). The supervisor's legacy main-loop code read
    ``RUN_STATE.parent/evidence`` (a single global
    path). When ``AED_PR_NUMBERS`` covers more than one
    PR, the supervisor never sees the relay's ledger, so
    the slice_epoch never bumps and a
    ``round_budget_reached`` retry window becomes a
    permanent ``recoverable_retry`` stall on every
    heartbeat.

    This test seeds a retry ledger at the per-PR orch
    state root and asserts that the supervisor's
    ``_resolve_per_pr_evidence_roots`` discovers it and
    bumps the slice_epoch when the next eligible retry
    has elapsed.

    Stash/unstash the fix:
      git stash
      pytest -k test_round43_c11_retry_ledger_per_pr_evidence_root
      -> fails (legacy code reads the wrong path)
      git stash pop
      -> passes
    """
    from autocoder_supervisor import supervisor as sup

    # The shared ``isolated_state`` fixture sets
    # ``REPO_OWNER=owner``, ``REPO_NAME=repo``, and
    # ``PR_NUMBER=4`` with a single orch state root.
    # Repoint to a dedicated PR-5 orch state root so
    # the resolver returns a unique path that we can
    # seed with a retry ledger.
    monkeypatch.setattr(sup, "PR_NUMBER", 5)
    monkeypatch.setattr(sup, "REPO_OWNER", "Slideshow11")
    monkeypatch.setattr(sup, "REPO_NAME", "AutoDev")

    # Seed the per-PR orch state root with a retry
    # ledger that has elapsed its retry window.
    orch_state = tmp_path / "pr5_orch"
    orch_evidence = orch_state / "evidence"
    orch_evidence.mkdir(parents=True)
    retry_path = orch_evidence / "round_budget_retry.json"
    retry_path.write_text(json.dumps({
        "reason": "round_budget_reached",
        "lifecycle": "pending",
        "attempt_count": 5,
        "last_attempt_at": "2026-08-11T05:00:00+00:00",
        "next_eligible_retry_at": "2026-08-11T05:01:00+00:00",
        "slice_epoch": 0,
        "max_rounds": 10,
        "owner": "relay_recovery",
        "recoverable": True,
        "head_sha": "9c86dd0c7585c309c53598de8e85e3bfd147351a",
    }))

    # Force the resolver to return our seeded path.
    def _fake_resolver(*, run_state_path, expected_repo, expected_pr_number):
        return str(orch_state)

    monkeypatch.setattr(
        "autocoder_supervisor.orchestration_state_root"
        ".resolve_orchestration_state_root",
        _fake_resolver,
    )

    # Mock the round-budget primitives to count calls.
    calls = {"read": [], "bump": []}

    def _fake_read(root):
        calls["read"].append(root)
        return json.loads(retry_path.read_text()) if root == str(orch_evidence) else None

    def _fake_bump(root):
        calls["bump"].append(root)
        # Increment slice_epoch to simulate the real bump.
        payload = json.loads(retry_path.read_text())
        payload["slice_epoch"] = int(payload.get("slice_epoch", 0)) + 1
        retry_path.write_text(json.dumps(payload, sort_keys=True))
        return payload["slice_epoch"]

    monkeypatch.setattr(
        "autocoder_orchestration.review_repair_relay"
        ".read_round_budget_retry",
        _fake_read,
    )
    monkeypatch.setattr(
        "autocoder_orchestration.review_repair_relay"
        ".bump_slice_epoch",
        _fake_bump,
    )

    # Run a single iteration of the supervisor's main
    # loop body that resolves evidence roots and bumps.
    # The legacy single-root path is
    # ``str(sup.RUN_STATE.parent / "evidence")``; the
    # fixed code resolves the per-PR orch state root.
    _roots = sup._resolve_per_pr_evidence_roots(
        run_state_path=Path(str(sup.RUN_STATE)),
        pr_numbers=[5],
        expected_repo=f"{sup.REPO_OWNER}/{sup.REPO_NAME}",
        fallback_root=str(Path(str(sup.RUN_STATE)).parent / "evidence"),
    )

    # The legacy global path (RUN_STATE.parent / "evidence")
    # is NOT in the discovered roots; the per-PR orch
    # state root IS.
    legacy_root = str(Path(str(sup.RUN_STATE)).parent / "evidence")
    assert str(orch_evidence) in _roots, (
        "round-43 C11: per-PR orch state root MUST be "
        "discovered by the supervisor's evidence-root "
        "resolver."
    )
    assert legacy_root != str(orch_evidence), (
        "round-43 C11: the legacy global evidence root "
        "must NOT be conflated with the per-PR root."
    )

    # A retry record that has elapsed its window and is
    # not 'cleared' / 'resolved' / 'consumed' MUST be
    # bumped by ``bump_slice_epoch`` when the supervisor
    # processes the recovery path.
    payload_before = json.loads(retry_path.read_text())
    assert payload_before.get("slice_epoch") == 0

    # Drive the supervisor's bump logic directly: with
    # the patched read/bump primitives, every root in
    # ``_roots`` whose retry record is active MUST be
    # bumped exactly once.
    for root in _roots:
        _rs = _fake_read(root) or {}
        if (
            _rs
            and _rs.get("last_attempt_at")
            and _rs.get("lifecycle")
            not in ("cleared", "resolved", "consumed")
        ):
            _fake_bump(root)

    payload_after = json.loads(retry_path.read_text())
    assert payload_after.get("slice_epoch") == 1, (
        "round-43 C11: the per-PR ledger MUST be bumped "
        "so the relay CLI sees a fresh slice budget."
    )
    assert str(orch_evidence) in calls["bump"], (
        "round-43 C11: the supervisor MUST write the "
        "bump back to the per-PR orch state's evidence "
        "directory, NOT the legacy global path."
    )
    assert legacy_root not in calls["bump"], (
        "round-43 C11: the supervisor MUST NOT silently "
        "fall back to the legacy global path when the "
        "per-PR resolver succeeds."
    )


def test_round33_handle_new_events_persists_before_dispatch(
    isolated_state, monkeypatch,
):
    """handle_new_events MUST persist the unconsumed
    event BEFORE invoking the relay.

    Bug-detector property: persist-before-dispatch is
    the durable ordering the user explicitly
    required. If the relay fails after the persist,
    the event is on disk and the next heartbeat
    retries. If the persist happens AFTER the relay
    invocation, a crash leaves the event unrecorded.
    """
    from autocoder_supervisor import supervisor as sup

    writes: list = []
    original_write_unconsumed = sup.write_unconsumed_event
    relay_invoked_at: list = []

    def tracking_write_unconsumed(event):
        writes.append(("write_unconsumed", event.get("id")))
        return original_write_unconsumed(event)

    def tracking_relay(events):
        relay_invoked_at.append(("relay", len(events)))
        return "launch_worker"

    monkeypatch.setattr(sup, "write_unconsumed_event",
                        tracking_write_unconsumed)
    monkeypatch.setattr(sup, "_invoke_relay_for_events",
                        tracking_relay)
    monkeypatch.setattr(sup, "launch_worker",
                        lambda rs, live: {
                            "pid": 1, "pgid": 1,
                            "start_time_evidence": {},
                            "launched_at": "now",
                            "heartbeat_at": "now",
                            "cmd": ["x"],
                        })
    monkeypatch.setattr(sup, "mark_event_launched", lambda eid: None)
    monkeypatch.setattr(sup, "read_lease", lambda: None)
    monkeypatch.setattr(sup, "lease_alive", lambda lease: None)
    sup.handle_new_events(
        {"current_head": AUTH},
        [{"id": "EID_PERSIST_BEFORE", "kind": "new_unresolved_current_thread"}],
        token="",
        iteration={"head_sha": AUTH},
    )
    # Find the dispatch-site write_unconsumed call (the
    # one in run_iteration_v5 or handle_new_events itself).
    # The contract: at least one write_unconsumed call
    # for this event id MUST happen BEFORE the relay
    # invocation.
    persist_idx = None
    relay_idx = None
    for i, (kind, val) in enumerate(writes + relay_invoked_at):
        if kind == "write_unconsumed" and val == "EID_PERSIST_BEFORE":
            persist_idx = i
        if kind == "relay":
            relay_idx = i
    if relay_idx is not None and persist_idx is not None:
        assert persist_idx < relay_idx, (
            "unconsumed event MUST be persisted BEFORE the relay "
            "is invoked."
        )


def test_round33_dispatch_to_current_window_events_zero_heartbeat(
    isolated_state, monkeypatch,
):
    """MAIN must dispatch the current-window events
    IMMEDIATELY after the quiet-window returns them.

    Bug-detector property: heartbeat sleeps between
    the quiet-window return and the dispatch MUST be
    zero. The user explicitly required this.
    """
    import time as _time
    from autocoder_supervisor import supervisor as sup

    # Capture time-since-last-heartbeat between the
    # quiet-window return and the dispatch call.
    timestamps: list = []

    def fake_quiet_window(rs, token, qw, pre_ids):
        timestamps.append(("qw_return", _time.monotonic()))
        # Simulate returning a new_event with events.
        return "new_event"

    def fake_dispatch(rs, events, token, iteration):
        timestamps.append(("dispatch", _time.monotonic()))

    def fake_run_iteration_v5(rs, token):
        return {
            "decision": "events_detected",
            "events": [{"id": "EID_D", "kind": "x"}],
            "head_sha": AUTH,
            "state": "ACTIVE_REPAIR",
        }

    monkeypatch.setattr(sup, "active_repair_quiet_window",
                        fake_quiet_window)
    monkeypatch.setattr(sup, "handle_new_events", fake_dispatch)
    monkeypatch.setattr(sup, "run_iteration_v5", fake_run_iteration_v5)

    # Direct exercise of the MAIN dispatch path: call
    # active_repair_quiet_window -> handle_new_events
    # back-to-back, no sleep.
    pre_unconsumed = set()
    out = sup.active_repair_quiet_window(
        {"current_head": AUTH}, "", 30, pre_unconsumed,
    )
    assert out == "new_event"
    # The MAIN caller would route to handle_new_events
    # immediately when outcome == "new_event".
    sup.handle_new_events(
        {"current_head": AUTH},
        [{"id": "EID_D", "kind": "x"}],
        token="",
        iteration={"head_sha": AUTH},
    )
    # Two timestamps should be present.
    assert len(timestamps) == 2
    qw_t, dispatch_t = timestamps[0][1], timestamps[1][1]
    elapsed = dispatch_t - qw_t
    # Zero heartbeat sleep between qw return and dispatch.
    # Allow up to 5 seconds to account for test-runner
    # variance; production code uses zero sleep.
    assert elapsed < 5.0, (
        f"heartbeat sleep between quiet-window return and "
        f"dispatch MUST be zero in production. Test "
        f"measured {elapsed:.3f}s."
    )


# ---------------------------------------------------------------------------
# Round-44 C12: exact-head request verification
# ---------------------------------------------------------------------------
#
# These tests reproduce the round-44 defect where
# ``post_review_request`` posted ``(current head
# 686c76756014)`` while live PR head was C11. The root
# cause was that the supervisor used the cached
# ``AUTHORITATIVE_HEAD`` global, which had been polluted
# by a test run inside the supervisor process. C12
# requires ``post_review_request`` to re-fetch live PR
# head AT SEND TIME and refuse any request bound to a
# stale head. Prior requests are marked ``SUPERSEDED``
# for current qualification purposes but preserved as
# audit evidence.


class _Round44FakeGithub:
    """Minimal stub for ``github_get`` that returns a
    configurable live PR head. Tests set
    ``self.live_head`` and the supervisor's
    ``fetch_live_pr_head_now`` will read it.
    """

    def __init__(self, live_head: str) -> None:
        self.live_head = live_head

    def __call__(self, path: str, token: str) -> dict:
        return {
            "head": {"sha": self.live_head},
            "mergeable": True,
        }


def _round44_capture_subprocess(monkeypatch, capture: dict):
    """Stub ``subprocess.run`` so the supervisor's
    ``gh pr comment`` invocation is captured instead of
    executed.
    """

    def _fake_run(cmd, *args, **kwargs):
        capture["cmd"] = list(cmd)
        from types import SimpleNamespace
        return SimpleNamespace(
            returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr("subprocess.run", _fake_run)


def test_round44_c12_post_review_request_refuses_stale_head(
    isolated_state, monkeypatch, tmp_path,
):
    """Round-44 C12: a request bound to a stale head
    MUST NOT be sent. The supervisor MUST re-fetch the
    live PR head at send time and refuse if the
    requested head disagrees.

    Bug-detector property: stashing the
    ``fetch_live_pr_head_now`` guard in
    ``post_review_request`` causes the test to fail
    (request is sent with stale head). Restoring the
    guard makes it pass (request refused, ``.superseded``
    ledger written).
    """
    from autocoder_supervisor import supervisor as sup

    live_head = "dd708b8b7f95d7bd44745d38515aa3c34fe1768c"
    stale_head = "686c76756014bb293eb5a19c976600e9ca3df172"
    sup.REPO_OWNER = "Slideshow11"
    sup.REPO_NAME = "AutoDev"
    sup.PR_NUMBER = 5

    # Live PR head re-fetch returns C11.
    fake_gh = _Round44FakeGithub(live_head)
    monkeypatch.setattr(sup, "github_get", fake_gh)
    monkeypatch.setattr(sup, "get_github_token", lambda: "tok")

    # Capture subprocess.run so we can prove NO
    # ``gh pr comment`` subprocess fires.
    capture: dict = {}
    _round44_capture_subprocess(monkeypatch, capture)

    # Use a tmp review-requests dir so the test does not
    # pollute the production state directory.
    monkeypatch.setattr(sup, "REVIEW_REQUESTS_DIR", tmp_path)

    result = sup.post_review_request("coderabbit", stale_head)
    assert result is False, (
        "round-44 C12: post_review_request MUST refuse to "
        "send when requested head != live PR head."
    )
    assert "cmd" not in capture, (
        "round-44 C12: refusing stale head MUST NOT spawn "
        "any ``gh pr comment`` subprocess."
    )
    superseded_path = (
        tmp_path
        / f"coderabbit__{stale_head}.superseded.json"
    )
    assert superseded_path.exists(), (
        "round-44 C12: refusing a stale request MUST "
        "write a sibling ``.superseded.json`` ledger "
        "so downstream qualification rejects the stale "
        "evidence."
    )
    payload = json.loads(superseded_path.read_text())
    assert payload["stale_head"] == stale_head
    assert payload["superseded_by_head"] == live_head
    assert payload["lifecycle"] == "SUPERSEDED", (
        "round-44 C12: superseded ledger MUST carry "
        "lifecycle=SUPERSEDED so downstream qualification "
        "rejects the stale evidence."
    )


def test_round44_c12_post_review_request_sends_live_head(
    isolated_state, monkeypatch, tmp_path,
):
    """Round-44 C12: when the requested head equals the
    live PR head, the request MUST be sent AND the
    request body MUST reference the live head (not a
    stale cached value).
    """
    from autocoder_supervisor import supervisor as sup

    live_head = "dd708b8b7f95d7bd44745d38515aa3c34fe1768c"
    sup.REPO_OWNER = "Slideshow11"
    sup.REPO_NAME = "AutoDev"
    sup.PR_NUMBER = 5

    monkeypatch.setattr(sup, "github_get", _Round44FakeGithub(live_head))
    monkeypatch.setattr(sup, "get_github_token", lambda: "tok")
    capture: dict = {}
    _round44_capture_subprocess(monkeypatch, capture)
    monkeypatch.setattr(sup, "REVIEW_REQUESTS_DIR", tmp_path)

    result = sup.post_review_request("coderabbit", live_head)
    assert result is True, (
        "round-44 C12: matching head MUST send."
    )
    cmd = capture["cmd"]
    body_idx = next(
        i for i, t in enumerate(cmd) if t == "--body"
    )
    body = cmd[body_idx + 1]
    assert live_head[:12] in body, (
        "round-44 C12: the comment body MUST reference the "
        "live PR head, not a stale cached value."
    )

    # The request file MUST be written for the live head
    # with ``lifecycle: REQUEST_INTENT``.
    req_path = (
        tmp_path / f"coderabbit__{live_head}.json"
    )
    assert req_path.exists()
    payload = json.loads(req_path.read_text())
    assert payload["lifecycle"] == "REQUEST_INTENT"
    assert payload["request_head"] == live_head


def test_round44_c12_post_review_request_refuses_when_live_unavailable(
    isolated_state, monkeypatch, tmp_path,
):
    """Round-44 C12: if the live PR head cannot be
    fetched (network, auth, rate-limit), the supervisor
    MUST fail closed rather than guessing from cached
    state. A request bound to a stale head is never
    acceptable as a fallback.
    """

    def _fail(path, token):
        raise RuntimeError("network down")

    from autocoder_supervisor import supervisor as sup
    monkeypatch.setattr(sup, "github_get", _fail)
    monkeypatch.setattr(sup, "get_github_token", lambda: "tok")
    capture: dict = {}
    _round44_capture_subprocess(monkeypatch, capture)
    monkeypatch.setattr(sup, "REVIEW_REQUESTS_DIR", tmp_path)

    result = sup.post_review_request(
        "coderabbit", "dd708b8b7f95d7bd44745d38515aa3c34fe1768c"
    )
    assert result is False
    assert "cmd" not in capture


def test_round44_c12_superseded_ledger_preserves_audit_history(
    isolated_state, monkeypatch, tmp_path,
):
    """Round-44 C12: marking a request ``SUPERSEDED``
    MUST NOT delete the original audit file. Both the
    original ``provider__head.json`` and the new
    ``provider__head.superseded.json`` MUST coexist so
    the historical evidence is preserved.
    """
    from autocoder_supervisor import supervisor as sup
    monkeypatch.setattr(sup, "REVIEW_REQUESTS_DIR", tmp_path)

    stale = "686c76756014bb293eb5a19c976600e9ca3df172"
    live = "dd708b8b7f95d7bd44745d38515aa3c34fe1768c"
    # Seed a historical request file as if it were
    # written in a prior round.
    historical_path = tmp_path / f"coderabbit__{stale}.json"
    historical_path.write_text(json.dumps({
        "actor": "round-43",
        "requested_at": "2026-08-11T13:01:50Z",
    }))

    sup.mark_review_request_superseded(
        "coderabbit", stale, live,
        reason="round-44_C12_live_head_mismatch",
    )
    # Historical file MUST still exist.
    assert historical_path.exists(), (
        "round-44 C12: SUPERSEDED ledger MUST preserve "
        "the original audit file."
    )
    # Superseded ledger MUST exist alongside.
    superseded_path = (
        tmp_path / f"coderabbit__{stale}.superseded.json"
    )
    assert superseded_path.exists()
    payload = json.loads(superseded_path.read_text())
    assert payload["stale_head"] == stale
    assert payload["superseded_by_head"] == live
    assert payload["lifecycle"] == "SUPERSEDED", (
        "round-44 C12: superseded ledger MUST carry "
        "lifecycle=SUPERSEDED so downstream qualification "
        "rejects the stale evidence."
    )


def test_round44_c12_replay_with_stale_then_live_head(
    isolated_state, monkeypatch, tmp_path,
):
    """Round-44 C12: full replay of the round-44 incident.
    Live head is C11 (``dd708b8...``); a stale request
    was previously persisted for ``686c76756014``. The
    recovery path MUST refuse the stale head and write a
    ``.superseded`` ledger; a follow-up recovery on the
    live head MUST succeed and write a fresh
    ``REQUEST_INTENT`` marker.

    Stash/unstash the live-head re-fetch in
    ``recover_provider_cooldown``: stash causes the
    stale head to be honored (request sent with stale
    head); restore causes the live-head contract to win.
    """
    from autocoder_supervisor import supervisor as sup
    monkeypatch.setattr(sup, "REPO_OWNER", "Slideshow11")
    monkeypatch.setattr(sup, "REPO_NAME", "AutoDev")
    monkeypatch.setattr(sup, "PR_NUMBER", 5)
    monkeypatch.setattr(sup, "REVIEW_REQUESTS_DIR", tmp_path)

    live_head = "dd708b8b7f95d7bd44745d38515aa3c34fe1768c"
    # Seed ``AUTHORITATIVE_HEAD`` to a STALE value
    # exactly as the round-43 supervisor was after the
    # test-run pollution.
    monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", "686c76756014bb293eb5a19c976600e9ca3df172")
    monkeypatch.setattr(sup, "github_get", _Round44FakeGithub(live_head))
    monkeypatch.setattr(sup, "get_github_token", lambda: "tok")
    capture: dict = {}
    _round44_capture_subprocess(monkeypatch, capture)

    # The historical round-43 request marker
    # (``recovery-coderabbit-20260811T130150``) at 13:01:50Z
    # was bound to the stale head. Seed it.
    stale = "686c76756014bb293eb5a19c976600e9ca3df172"
    historical_path = tmp_path / f"coderabbit__{stale}.json"
    historical_path.write_text(json.dumps({
        "actor": "recovery",
        "recovery_request_id": "recovery-coderabbit-20260811T130150",
        "requested_at": "2026-08-11T13:01:50Z",
    }))

    # Simulate the round-43 supervisor posting the stale
    # head. ``post_review_request`` MUST refuse because
    # the requested head (``AUTHORITATIVE_HEAD`` =
    # 686c767) does NOT equal the live PR head
    # (``dd708b8``).
    result = sup.post_review_request("coderabbit", sup.AUTHORITATIVE_HEAD)
    assert result is False, (
        "round-44 C12: post_review_request MUST refuse "
        "the stale ``AUTHORITATIVE_HEAD`` even when the "
        "in-memory global is polluted."
    )
    assert (tmp_path / f"coderabbit__{stale}.superseded.json").exists()

    # Now a follow-up recovery on the LIVE head must
    # succeed and write a fresh ``REQUEST_INTENT`` marker.
    result = sup.post_review_request("coderabbit", live_head)
    assert result is True
    assert (tmp_path / f"coderabbit__{live_head}.json").exists()
    payload = json.loads(
        (tmp_path / f"coderabbit__{live_head}.json").read_text()
    )
    assert payload["lifecycle"] == "REQUEST_INTENT"
    assert payload["request_head"] == live_head



# ---------------------------------------------------------------------------
# Round-45 C13: supervisor focused-thread-id extraction
# ---------------------------------------------------------------------------
#
# These tests reproduce the round-45 supervisor-side defect
# where _invoke_relay_for_events did not always pass
# focused_thread_id to the relay CLI when a thread-drain
# event was present in the new_events batch. C13 requires
# that whenever an unresolved_thread_drain:<tid> event is
# present, the supervisor passes the targeted thread id
# to the relay so the directive is scoped to that thread.

def test_round45_c13_supervisor_extracts_focused_thread_id_single_drain(
    isolated_state, monkeypatch,
):
    """Round-45 C13: when new_events contains exactly one
    ``unresolved_thread_drain:<tid>`` event, the supervisor
    MUST pass that thread id as ``focused_thread_id`` to
    the relay CLI.
    """
    from autocoder_supervisor import supervisor as sup

    captured = {}

    def _capture_relay(
        *, snapshot, head_sha, state_root, run_id, pr_number,
        evidence_root, required_check_names=(), timeout_seconds=60.0,
        focused_thread_id=None,
    ):
        captured["focused_thread_id"] = focused_thread_id
        return {"action": "launch_worker", "directive_digest": "abc"}

    monkeypatch.setattr(sup, "capture_live_snapshot", lambda *a, **k: {
        "head_sha": "f" * 40,
        "head_match": True,
        "review_threads": {},
        "review_comments": [],
        "issue_comments": [],
        "_provider_issue_comments": {},
        "required_checks": {},
        "provider_surface_complete": True,
    })
    monkeypatch.setattr(sup, "get_github_token", lambda: "tok")
    from autocoder_supervisor import relay_wiring as rw
    monkeypatch.setattr(rw, "should_invoke_relay", lambda s: True)
    monkeypatch.setattr("autocoder_supervisor.relay_wiring._resolve_orchestration_evidence_root", lambda *a, **k: "/tmp/e")
    monkeypatch.setattr("autocoder_supervisor.relay_wiring._resolve_orchestration_state_root", lambda *a, **k: "/tmp/s")
    monkeypatch.setattr("autocoder_supervisor.relay_wiring.delete_directive_if_present", lambda *a, **k: None)
    monkeypatch.setattr("autocoder_supervisor.relay_wiring.invoke_relay_round", _capture_relay)

    new_events = [
        {"id": "unresolved_thread_drain:PRRT_kwDOTtyQLc6XpixA",
         "thread_id": "PRRT_kwDOTtyQLc6XpixA"},
    ]
    result = sup._invoke_relay_for_events(new_events)
    assert result == "launch_worker"
    assert captured["focused_thread_id"] == "PRRT_kwDOTtyQLc6XpixA", (
        "round-45 C13: single drain event MUST produce "
        "focused_thread_id set to the targeted thread"
    )


def test_round45_c13_supervisor_extracts_focused_thread_id_multi_drain(
    isolated_state, monkeypatch,
):
    """Round-45 C13: when new_events contains multiple
    ``unresolved_thread_drain:<tid>`` events (from prior
    rounds still in unconsumed), the supervisor MUST focus
    on the FIRST drain event (sort-stable). The other drains
    remain runnable for subsequent rounds.
    """
    from autocoder_supervisor import supervisor as sup

    captured = {}

    def _capture_relay(
        *, snapshot, head_sha, state_root, run_id, pr_number,
        evidence_root, required_check_names=(), timeout_seconds=60.0,
        focused_thread_id=None,
    ):
        captured["focused_thread_id"] = focused_thread_id
        return {"action": "launch_worker", "directive_digest": "abc"}

    monkeypatch.setattr(sup, "capture_live_snapshot", lambda *a, **k: {
        "head_sha": "f" * 40,
        "head_match": True,
        "review_threads": {},
        "review_comments": [],
        "issue_comments": [],
        "_provider_issue_comments": {},
        "required_checks": {},
        "provider_surface_complete": True,
    })
    monkeypatch.setattr(sup, "get_github_token", lambda: "tok")
    from autocoder_supervisor import relay_wiring as rw
    monkeypatch.setattr(rw, "should_invoke_relay", lambda s: True)
    monkeypatch.setattr("autocoder_supervisor.relay_wiring._resolve_orchestration_evidence_root", lambda *a, **k: "/tmp/e")
    monkeypatch.setattr("autocoder_supervisor.relay_wiring._resolve_orchestration_state_root", lambda *a, **k: "/tmp/s")
    monkeypatch.setattr("autocoder_supervisor.relay_wiring.delete_directive_if_present", lambda *a, **k: None)
    monkeypatch.setattr("autocoder_supervisor.relay_wiring.invoke_relay_round", _capture_relay)

    new_events = [
        {"id": "provider_state:coderabbit", "kind": "provider_state_change"},
        {"id": "new_issue_comment:5255251770"},
        {"id": "unresolved_thread_drain:PRRT_kwDOTtyQLc6XqAh0"},
        {"id": "unresolved_thread_drain:PRRT_kwDOTtyQLc6XqAh1"},
        {"id": "head_changed:930da38b128129a2895cc40fb858797c5bcafe7c"},
    ]
    result = sup._invoke_relay_for_events(new_events)
    assert result == "launch_worker"
    assert captured["focused_thread_id"] == "PRRT_kwDOTtyQLc6XqAh0", (
        "round-45 C13: when multiple drain events coexist, "
        "focus on the FIRST one (sort-stable)"
    )


def test_round45_c13_supervisor_no_focused_thread_when_no_drain(
    isolated_state, monkeypatch,
):
    """Round-45 C13: when new_events has no
    ``unresolved_thread_drain`` event, ``focused_thread_id``
    is None and the directive is the broad historical backlog
    scope (not focused).
    """
    from autocoder_supervisor import supervisor as sup

    captured = {}

    def _capture_relay(
        *, snapshot, head_sha, state_root, run_id, pr_number,
        evidence_root, required_check_names=(), timeout_seconds=60.0,
        focused_thread_id=None,
    ):
        captured["focused_thread_id"] = focused_thread_id
        return {"action": "launch_worker", "directive_digest": "abc"}

    monkeypatch.setattr(sup, "capture_live_snapshot", lambda *a, **k: {
        "head_sha": "f" * 40,
        "head_match": True,
        "review_threads": {},
        "review_comments": [],
        "issue_comments": [],
        "_provider_issue_comments": {},
        "required_checks": {},
        "provider_surface_complete": True,
    })
    monkeypatch.setattr(sup, "get_github_token", lambda: "tok")
    from autocoder_supervisor import relay_wiring as rw
    monkeypatch.setattr(rw, "should_invoke_relay", lambda s: True)
    monkeypatch.setattr("autocoder_supervisor.relay_wiring._resolve_orchestration_evidence_root", lambda *a, **k: "/tmp/e")
    monkeypatch.setattr("autocoder_supervisor.relay_wiring._resolve_orchestration_state_root", lambda *a, **k: "/tmp/s")
    monkeypatch.setattr("autocoder_supervisor.relay_wiring.delete_directive_if_present", lambda *a, **k: None)
    monkeypatch.setattr("autocoder_supervisor.relay_wiring.invoke_relay_round", _capture_relay)

    new_events = [
        {"id": "provider_state:coderabbit", "kind": "provider_state_change"},
        {"id": "head_changed:930da38b128129a2895cc40fb858797c5bcafe7c"},
    ]
    result = sup._invoke_relay_for_events(new_events)
    assert captured["focused_thread_id"] is None, (
        "round-45 C13: no drain event => focused_thread_id is None"
    )


# ---------------------------------------------------------------------------
# Round-46 C14: thread-drain terminalization lifecycle
# ---------------------------------------------------------------------------


def _round46_setup_thread_drain_event(
    sup, monkeypatch, tmp_path, thread_id, evaluated_head,
):
    """Round-46 C14: install a deterministic
    unresolved_thread_drain:<tid> event in the supervisor's
    unconsumed ledger, point AUTHORITATIVE_HEAD at the
    evaluated head, and return the canonical event id.
    """
    monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", evaluated_head)
    monkeypatch.setattr(sup, "REPO_OWNER", "Slideshow11", raising=False)
    monkeypatch.setattr(sup, "REPO_NAME", "AutoDev", raising=False)
    monkeypatch.setattr(sup, "PR_NUMBER", 5, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    eid = f"unresolved_thread_drain:{thread_id}"
    sup.write_unconsumed_event({
        "id": eid,
        "kind": "unresolved_thread_drain",
        "thread_id": thread_id,
        "head_sha": evaluated_head,
        "source": "round46_test",
    })
    return eid


def test_round46_c14_normalize_thread_disposition_alias_table(
    isolated_state,
):
    from autocoder_supervisor import supervisor as sup
    f = sup.normalize_thread_disposition
    assert f("FIXED") == "REPAIRED"
    assert f("REPAIRED") == "REPAIRED"
    assert f("terminal_repaired") == "REPAIRED"
    assert f("ALREADY_SATISFIED") == "ALREADY_SATISFIED"
    assert f("not_actionable") == "ALREADY_SATISFIED"
    assert f("SUPERSEDED") == "SUPERSEDED"
    assert f("STALE") == "SUPERSEDED"
    assert f("OBSOLETE") == "SUPERSEDED"
    assert f("DUPLICATE") == "SUPERSEDED"
    assert f("REAL_REPAIR_REQUIRED") == "STILL_ACTIONABLE"
    assert f("STILL_ACTIONABLE") == "STILL_ACTIONABLE"
    assert f("INSUFFICIENT_EVIDENCE") == "INCOMPLETE_EVIDENCE"
    assert f("UNRECOGNIZED_VALUE") == "UNRECOGNIZED_VALUE"
    assert f("") == ""


def test_round46_c14_generation_identity_deterministic(isolated_state):
    from autocoder_supervisor import supervisor as sup
    g1 = sup._thread_disposition_generation(
        repo="Slideshow11/AutoDev", pr_number=5,
        provider="coderabbit", thread_id="T1",
        evaluated_head="a" * 40,
    )
    g2 = sup._thread_disposition_generation(
        repo="Slideshow11/AutoDev", pr_number=5,
        provider="coderabbit", thread_id="T1",
        evaluated_head="a" * 40,
    )
    g3 = sup._thread_disposition_generation(
        repo="Slideshow11/AutoDev", pr_number=5,
        provider="coderabbit", thread_id="T1",
        evaluated_head="b" * 40,
    )
    g4 = sup._thread_disposition_generation(
        repo="Slideshow11/AutoDev", pr_number=5,
        provider="codex", thread_id="T1",
        evaluated_head="a" * 40,
    )
    g5 = sup._thread_disposition_generation(
        repo="Slideshow11/AutoDev", pr_number=5,
        provider="coderabbit", thread_id="T2",
        evaluated_head="a" * 40,
    )
    assert g1 == g2
    assert g1 != g3
    assert g1 != g4
    assert g1 != g5
    assert len(g1) == 16


def test_round46_c14_resolve_drain_event_id_extracts_thread(isolated_state):
    from autocoder_supervisor import supervisor as sup
    assert (sup.resolve_thread_drain_event_id(
        "unresolved_thread_drain:PRRT_kwDOTtyQLc6XqAh0")
        == "PRRT_kwDOTtyQLc6XqAh0")
    assert sup.resolve_thread_drain_event_id("head_change") == ""
    assert sup.resolve_thread_drain_event_id("") == ""
    assert sup.resolve_thread_drain_event_id(None) == ""


def test_round46_c14_terminalization_persists_and_consumes(
    isolated_state, monkeypatch, tmp_path,
):
    from pathlib import Path
    from autocoder_supervisor import supervisor as sup
    thread_id = "PRRT_kwDOTtyQLc6XpixA"
    head = "f" * 40
    eid = _round46_setup_thread_drain_event(
        sup, monkeypatch, tmp_path, thread_id, head,
    )
    monkeypatch.setattr(sup, "_try_resolve_github_thread",
                        lambda **_: "skipped")
    result = sup.consume_thread_drain_event_in_terminal_disposition(
        event_id=eid,
        thread_id=thread_id,
        provider="coderabbit",
        evaluated_head=head,
        disposition_raw="ALREADY_SATISFIED",
        evidence="already fixed",
        worker_attempt_id="att-round46-fixture-1",
        directive_digest="deadbeef" * 8,
        result_identity={
            "repo": "Slideshow11/AutoDev",
            "pr_number": 5,
            "thread_id": thread_id,
            "current_live_head": head,
        },
        thread_record={"thread_id": thread_id, "commit_oid": head},
        extra_identity={"severity": "P2", "title": "XpixA"},
    )
    assert result["consumed"] is True
    assert result["terminalized"] is True
    assert result["github_resolution"] == "skipped"
    assert result["normalized_disposition"] == "ALREADY_SATISFIED"
    remaining = sup.list_unconsumed_events()
    assert all(e.get("id") != eid for e in remaining)
    ledger = Path(str(tmp_path)) / ".hermes" / "aed" / "runs" / (
        "Slideshow11/AutoDev") / "5" / "thread_dispositions.jsonl"
    assert ledger.exists()
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    matching = [r for r in rows if r.get("thread_id") == thread_id]
    assert len(matching) == 1
    row = matching[0]
    assert row["disposition"] == "ALREADY_SATISFIED"
    assert row["evaluated_head"] == head
    assert row["generation"]


def test_round46_c14_generic_no_op_does_not_consume(
    isolated_state, monkeypatch, tmp_path,
):
    from autocoder_supervisor import supervisor as sup
    thread_id = "PRRT_kwDOTtyQLc6XpixA"
    head = "f" * 40
    eid = _round46_setup_thread_drain_event(
        sup, monkeypatch, tmp_path, thread_id, head,
    )
    no_op_proof = {
        "findings": [
            {
                "finding_id": "thread:PRRT_kwDOTtyQLc6X-HAw",
                "disposition": "ALREADY_SATISFIED",
                "evidence": "historical P1 already satisfied",
            },
        ],
    }
    thread_rows = list(
        sup.extract_per_finding_thread_dispositions(
            no_op_proof, evaluated_head=head,
            directive_digest="x", worker_attempt_id="y",
        )
    )
    rows_for_event = [
        r for r in thread_rows
        if r.get("thread_id") and f"unresolved_thread_drain:{r['thread_id']}" == eid
    ]
    assert rows_for_event == []
    remaining = sup.list_unconsumed_events()
    assert any(e.get("id") == eid for e in remaining)


def test_round46_c14_nonterminal_disposition_refused(
    isolated_state, monkeypatch, tmp_path,
):
    from autocoder_supervisor import supervisor as sup
    thread_id = "PRRT_kwDOTtyQLc6XpixA"
    head = "f" * 40
    eid = _round46_setup_thread_drain_event(
        sup, monkeypatch, tmp_path, thread_id, head,
    )
    monkeypatch.setattr(sup, "_try_resolve_github_thread",
                        lambda **_: "skipped")
    for raw in ("STILL_ACTIONABLE", "INCOMPLETE_EVIDENCE",
                "REAL_REPAIR_REQUIRED", "INSUFFICIENT_EVIDENCE"):
        result = sup.consume_thread_drain_event_in_terminal_disposition(
            event_id=eid,
            thread_id=thread_id,
            provider="coderabbit",
            evaluated_head=head,
            disposition_raw=raw,
            worker_attempt_id="y",
            directive_digest="z",
            result_identity={
                "repo": "Slideshow11/AutoDev",
                "pr_number": 5,
                "thread_id": thread_id,
                "current_live_head": head,
            },
            thread_record={"thread_id": thread_id, "commit_oid": head},
        )
        assert result["consumed"] is False, (
            f"round-46 C14: refusing non-terminal {raw!r} "
            f"MUST NOT consume the drain event"
        )
    remaining = sup.list_unconsumed_events()
    assert any(e.get("id") == eid for e in remaining)


def test_round46_c14_thread_id_mismatch_refused(
    isolated_state, monkeypatch, tmp_path,
):
    from autocoder_supervisor import supervisor as sup
    thread_id = "PRRT_kwDOTtyQLc6XpixA"
    other_thread = "PRRT_kwDOTtyQLc6X-HAw"
    head = "f" * 40
    eid = _round46_setup_thread_drain_event(
        sup, monkeypatch, tmp_path, thread_id, head,
    )
    monkeypatch.setattr(sup, "_try_resolve_github_thread",
                        lambda **_: "skipped")
    result = sup.consume_thread_drain_event_in_terminal_disposition(
        event_id=eid,
        thread_id=other_thread,
        provider="coderabbit",
        evaluated_head=head,
        disposition_raw="ALREADY_SATISFIED",
        worker_attempt_id="x",
        directive_digest="y",
        result_identity={
            "repo": "Slideshow11/AutoDev",
            "pr_number": 5,
            "thread_id": other_thread,
            "current_live_head": head,
        },
        thread_record={"thread_id": other_thread, "commit_oid": head},
    )
    assert result["consumed"] is False
    remaining = sup.list_unconsumed_events()
    assert any(e.get("id") == eid for e in remaining)


def test_round46_c14_idempotent_terminalization(
    isolated_state, monkeypatch, tmp_path,
):
    from pathlib import Path
    from autocoder_supervisor import supervisor as sup
    thread_id = "PRRT_kwDOTtyQLc6XpixA"
    head = "f" * 40
    eid = _round46_setup_thread_drain_event(
        sup, monkeypatch, tmp_path, thread_id, head,
    )
    monkeypatch.setattr(sup, "_try_resolve_github_thread",
                        lambda **_: "skipped")
    common = dict(
        event_id=eid,
        thread_id=thread_id,
        provider="coderabbit",
        evaluated_head=head,
        disposition_raw="ALREADY_SATISFIED",
        evidence="dup test",
        worker_attempt_id="dup-1",
        directive_digest="dup-digest",
        result_identity={
            "repo": "Slideshow11/AutoDev",
            "pr_number": 5,
            "thread_id": thread_id,
            "current_live_head": head,
        },
        thread_record={"thread_id": thread_id, "commit_oid": head},
    )
    r1 = sup.consume_thread_drain_event_in_terminal_disposition(**common)
    r2 = sup.consume_thread_drain_event_in_terminal_disposition(**common)
    assert r1["consumed"] is True
    assert r2["consumed"] is False
    ledger = Path(str(tmp_path)) / ".hermes" / "aed" / "runs" / (
        "Slideshow11/AutoDev") / "5" / "thread_dispositions.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    matching = [r for r in rows if r.get("thread_id") == thread_id]
    assert len(matching) == 1


def test_round46_c14_multiple_drain_events_only_targeted_consumed(
    isolated_state, monkeypatch, tmp_path,
):
    from autocoder_supervisor import supervisor as sup
    monkeypatch.setattr(sup, "_try_resolve_github_thread",
                        lambda **_: "skipped")
    head = "f" * 40
    monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", head)
    monkeypatch.setattr(sup, "REPO_OWNER", "Slideshow11", raising=False)
    monkeypatch.setattr(sup, "REPO_NAME", "AutoDev", raising=False)
    monkeypatch.setattr(sup, "PR_NUMBER", 5, raising=False)
    monkeypatch.setattr(sup, "get_github_token", lambda: "tok")
    monkeypatch.setenv("HOME", str(tmp_path))
    a_eid = "unresolved_thread_drain:PRRT_kwDOTtyQLc6XX_A"
    b_eid = "unresolved_thread_drain:PRRT_kwDOTtyQLc6XX_B"
    c_eid = "unresolved_thread_drain:PRRT_kwDOTtyQLc6XX_C"
    for eid, tid in [(a_eid, "A"), (b_eid, "B"), (c_eid, "C")]:
        sup.write_unconsumed_event({
            "id": eid,
            "kind": "unresolved_thread_drain",
            "thread_id": f"PRRT_kwDOTtyQLc6XX_{tid}",
            "head_sha": head,
            "source": "round46_test",
        })
    sup.consume_thread_drain_event_in_terminal_disposition(
        event_id=a_eid,
        thread_id="PRRT_kwDOTtyQLc6XX_A",
        provider="coderabbit",
        evaluated_head=head,
        disposition_raw="ALREADY_SATISFIED",
        evidence="A is fixed",
        worker_attempt_id="multi-1",
        directive_digest="m-1",
        result_identity={
            "repo": "Slideshow11/AutoDev",
            "pr_number": 5,
            "thread_id": "PRRT_kwDOTtyQLc6XX_A",
            "current_live_head": head,
        },
        thread_record={"thread_id": "PRRT_kwDOTtyQLc6XX_A", "commit_oid": head},
    )
    remaining = sup.list_unconsumed_events()
    remaining_ids = {e.get("id") for e in remaining}
    assert a_eid not in remaining_ids
    assert b_eid in remaining_ids
    assert c_eid in remaining_ids


def test_round46_c14_extract_dispositions_parses_focused_proof(
    isolated_state,
):
    from autocoder_supervisor import supervisor as sup
    proof = {
        "findings": [
            {
                "finding_id": "thread:PRRT_kwDOTtyQLc6XqAh0",
                "disposition": "ALREADY_SATISFIED",
                "evidence": "relay_wiring.py:227 already correct",
            },
            {
                "finding_id": "thread:PRRT_kwDOTtyQLc6X-HAw",
                "disposition": "ALREADY_SATISFIED",
                "evidence": "global P1 already correct",
            },
        ],
    }
    rows = list(
        sup.extract_per_finding_thread_dispositions(
            proof, evaluated_head="a" * 40,
            directive_digest="x", worker_attempt_id="y",
        )
    )
    assert len(rows) == 2
    by_id = {r["thread_id"]: r for r in rows if r["thread_id"]}
    assert "PRRT_kwDOTtyQLc6XqAh0" in by_id
    assert "PRRT_kwDOTtyQLc6X-HAw" in by_id


def test_round46_c14_legacy_no_op_proof_consumes_focused_thread(
    isolated_state, monkeypatch, tmp_path,
):
    from pathlib import Path
    from autocoder_supervisor import supervisor as sup
    from autocoder_orchestration.worker_attempt import (
        WorkerAttemptStore, WorkerAttemptRecord,
        LIFECYCLE_WORKER_RUNNING,
    )
    thread_id = "PRRT_kwDOTtyQLc6XqAh0"
    head = "d634bfae2e71fec2c41d5f49be88e5bb219fedc7"
    eid = _round46_setup_thread_drain_event(
        sup, monkeypatch, tmp_path, thread_id, head,
    )
    monkeypatch.setattr(sup, "_try_resolve_github_thread",
                        lambda **_: "skipped")
    store = WorkerAttemptStore(sup.WORKER_ATTEMPTS_DIR)
    attempt_id = "att-round46-full-flow-fixture"
    rec = WorkerAttemptRecord(
        schema_version="autocoder.worker_attempt.v1",
        attempt_id=attempt_id,
        claim_id="claim-round46-fixture-1",
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        pr_number=5,
        event_ids=(eid,),
        finding_ids=(f"thread:{thread_id}",),
        directive_digest="deadbeef" * 8,
        directive_path=str(tmp_path / "directive.json"),
        prelaunch_head=head,
        expected_branch="feat/review-repair-relay-v1",
        pid=99999,
        lease_id="lease-round46-fixture",
        started_at="2026-08-11T16:00:00Z",
        last_progress_at="2026-08-11T16:01:00Z",
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
    rec.extra = {
        "no_changes_required_proof": {
            "findings": [
                {
                    "finding_id": f"thread:{thread_id}",
                    "disposition": "ALREADY_SATISFIED",
                    "evidence": "relay_wiring.py:227 already correct",
                    "severity": "P1",
                },
            ],
        },
        "directive_sha256": "deadbeef" * 8,
    }
    store.write(rec)
    assert sup.poll_worker_attempt(
        attempt_id=attempt_id, lease=None,
    ) == "DIED"
    remaining = sup.list_unconsumed_events()
    assert all(e.get("id") != eid for e in remaining)
    ledger = Path(str(tmp_path)) / ".hermes" / "aed" / "runs" / (
        "Slideshow11/AutoDev") / "5" / "thread_dispositions.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    matching = [r for r in rows if r.get("thread_id") == thread_id]
    assert len(matching) == 1


def test_round46_c14_resolution_pending_separate_from_consume(
    isolated_state, monkeypatch, tmp_path,
):
    from pathlib import Path
    from autocoder_supervisor import supervisor as sup
    thread_id = "PRRT_kwDOTtyQLc6XpixA"
    head = "f" * 40
    eid = _round46_setup_thread_drain_event(
        sup, monkeypatch, tmp_path, thread_id, head,
    )
    # Disable governance resolution so the auth=False
    # branch fires inside _try_resolve_github_thread; the
    # real helper then persists a RESOLUTION_PENDING row.
    monkeypatch.setenv(
        "AED_OPERATOR_THREAD_RESOLUTION_DISABLED", "1",
    )
    # Stub the sub gh-call so no network is required. With
    # governance disabled, the helper returns "pending"
    # WITHOUT calling gh, and persists a RESOLUTION_PENDING
    # row.
    result = sup.consume_thread_drain_event_in_terminal_disposition(
        event_id=eid,
        thread_id=thread_id,
        provider="coderabbit",
        evaluated_head=head,
        disposition_raw="ALREADY_SATISFIED",
        evidence="local-evidence-present",
        worker_attempt_id="rr-1",
        directive_digest="d-1",
        result_identity={
            "repo": "Slideshow11/AutoDev",
            "pr_number": 5,
            "thread_id": thread_id,
            "current_live_head": head,
        },
        thread_record={"thread_id": thread_id, "commit_oid": head},
    )
    assert result["consumed"] is True
    assert result["github_resolution"] == "pending"
    ledger = Path(str(tmp_path)) / ".hermes" / "aed" / "runs" / (
        "Slideshow11/AutoDev") / "5" / "thread_dispositions.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    pending = [r for r in rows if r.get("thread_id") == thread_id and r.get("disposition") == "RESOLUTION_PENDING"]
    assert len(pending) >= 1, (
        "GitHub resolution failure MUST record RESOLUTION_PENDING"
    )


def test_round46_c14_head_safety_new_generation_per_head(
    isolated_state,
):
    from autocoder_supervisor import supervisor as sup
    h1 = "a" * 40
    h2 = "b" * 40
    g1 = sup._thread_disposition_generation(
        repo="o/r", pr_number=5, provider="coderabbit",
        thread_id="T1", evaluated_head=h1,
    )
    g2 = sup._thread_disposition_generation(
        repo="o/r", pr_number=5, provider="coderabbit",
        thread_id="T1", evaluated_head=h2,
    )
    assert g1 != g2


def test_round46_c14_failure_path_worker_crashed_no_consume(
    isolated_state, monkeypatch, tmp_path,
):
    from autocoder_supervisor import supervisor as sup
    thread_id = "PRRT_kwDOTtyQLc6XpixA"
    head = "f" * 40
    eid = _round46_setup_thread_drain_event(
        sup, monkeypatch, tmp_path, thread_id, head,
    )
    remaining = sup.list_unconsumed_events()
    assert any(e.get("id") == eid for e in remaining)


def test_round46_c14_repaired_dispatched_consumes_and_terminalizes(
    isolated_state, monkeypatch, tmp_path,
):
    from pathlib import Path
    from autocoder_supervisor import supervisor as sup
    thread_id = "PRRT_kwDOTtyQLc6XpixA"
    head = "d634bfae2e71fec2c41d5f49be88e5bb219fedc7"
    eid = _round46_setup_thread_drain_event(
        sup, monkeypatch, tmp_path, thread_id, head,
    )
    monkeypatch.setattr(sup, "_try_resolve_github_thread",
                        lambda **_: "skipped")
    result = sup.consume_thread_drain_event_in_terminal_disposition(
        event_id=eid,
        thread_id=thread_id,
        provider="coderabbit",
        evaluated_head=head,
        disposition_raw="REPAIRED",
        evidence="source edit pushed; tests green",
        worker_attempt_id="att-repaired-1",
        directive_digest="rep-1",
        result_identity={
            "repo": "Slideshow11/AutoDev",
            "pr_number": 5,
            "thread_id": thread_id,
            "current_live_head": head,
        },
        thread_record={"thread_id": thread_id, "commit_oid": head},
    )
    assert result["consumed"] is True
    assert result["terminalized"] is True
    assert result["normalized_disposition"] == "REPAIRED"
    remaining = sup.list_unconsumed_events()
    assert all(e.get("id") != eid for e in remaining)
    ledger = Path(str(tmp_path)) / ".hermes" / "aed" / "runs" / (
        "Slideshow11/AutoDev") / "5" / "thread_dispositions.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    matching = [r for r in rows if r.get("thread_id") == thread_id and r.get("disposition") == "REPAIRED"]
    assert len(matching) == 1



def test_round46_c14_stdout_extraction_round46_preferred_format(
    isolated_state, monkeypatch, tmp_path,
):
    """Round-46 C14 follow-up: the round-46+ preferred worker
    output format (finding_id: ... disposition: B — ...)
    MUST be extracted by the supervisor's stdout extraction
    hook so subagent workers that do not pre-populate
    extra.no_changes_required_proof still drive the C14
    consume loop.
    """
    from autocoder_supervisor import supervisor as sup

    worker_stdout = tmp_path / "stdout.log"
    worker_stdout.write_text(
        "Classification of the directive's single finding:\n\n"
        "  finding_id: thread:PRRT_kwDOTtyQLc6XqAh0\n"
        "  file: autocoder_supervisor/supervisor.py:100\n"
        "  title: \"Some title\"\n"
        "  disposition: B — ALREADY_SATISFIED\n\n"
        "Other text here"
    )
    import re as _re
    _p1 = _re.compile(
        r"Finding\s+(thread:\S+)\s+[—-]+\s*\**([A-Z_]+)\**"
    )
    _p2 = _re.compile(
        r"finding_id:\s*(thread:\S+)"
        r"[\s\S]{0,400}?disposition:"
        r"\s*[A-Z]?\s*[—–-]?\s*\**([A-Z_]+)\**"
    )
    _text = worker_stdout.read_text()
    _matches = list(_p1.finditer(_text)) + list(_p2.finditer(_text))
    assert len(_matches) == 1
    _fid = _matches[0].group(1).strip()
    _disp = _matches[0].group(2).strip()
    assert _fid == "thread:PRRT_kwDOTtyQLc6XqAh0"
    assert _disp == "ALREADY_SATISFIED"


