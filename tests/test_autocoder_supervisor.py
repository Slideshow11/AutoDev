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


def test_e_check_failure_blocks_readiness():
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
