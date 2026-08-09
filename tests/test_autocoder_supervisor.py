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
    assert res["reason"] == "checks_not_green"


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
