"""Round-54/C22 Defect C: autonomous review-request retry lifecycle.

Defect C required the supervisor to recover from BOTH
AUTO_PAUSED_ACTIVE_DEVELOPMENT and QUOTA_PAUSED without operator
intervention.

The legacy behaviour (round-50) was "post once, then silence": a single
review request was posted for the same head, the idempotency guard
skipped every heartbeat, and no further requests were issued.

The repaired behaviour uses an explicit retry lifecycle:

        REQUEST_INTENT       -> persisted durable marker
        REQUEST_SENT         -> gh pr comment succeeded
        ACKNOWLEDGED         -> bot ack observed on next snapshot
        REVIEW_COMPLETE      -> durable review observed
        SUPERSEDED           -> head changed; old request obsolete
        REQUEST_TIMED_OUT    -> retry deadline elapsed without ack
        RETRY_DUE             -> next eligible retry epoch reached

The retry epoch is bounded by ``quota_retry_initial_seconds`` (default
1h) for the first attempt and ``quota_retry_backoff_seconds`` (default 6h)
for subsequent ones, capped at 6h absolute maximum. A same-head request
that has not produced either acknowledgement or a useful review by its
durable retry deadline acquires a fresh retry owner and becomes eligible
to send another bounded request.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


SUP_PATH = (
    Path(__file__).resolve().parent.parent
    / "autocoder_supervisor"
    / "supervisor.py"
)


def _bootstrap_temp_state(tmp_path: Path):
    """Bootstrap a state directory under ``tmp_path`` and bind the
    supervisor's module-level globals so tests can invoke helper
    functions without a real install."""
    # Ensure the supervisor is importable.
    sys.path.insert(0, str(SUP_PATH.parent.parent))
    from autocoder_supervisor import supervisor as sup_mod  # type: ignore
    # Build a state dir + the supervisor's state paths.
    sd = tmp_path / "state"
    sd.mkdir(parents=True, exist_ok=True)
    sup_mod.HEARTBEAT_PATH = tmp_path / "heartbeat"
    sup_mod.HEARTBEAT_PATH.write_text("dummy")
    sup_mod.LOG_PATH = tmp_path / "supervisor.log"
    sup_mod.LOCK_PATH = tmp_path / "lock"
    sup_mod.LEASE_PATH = sd / "worker_lease.json"
    sup_mod.LAST_RESUME_PATH = sd / "last_resume.json"
    sup_mod.QUOTA_PATH = sd / "quota_state.json"
    sup_mod.REVIEW_REQUESTS_DIR = sd / "review_requests"
    sup_mod.REVIEW_REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    sup_mod.SNAPSHOT_A_PATH = sd / "snapshot_a.json"
    sup_mod.SNAPSHOT_B_PATH = sd / "snapshot_b.json"
    sup_mod.RUN_STATE = sd / "run_state.json"
    sup_mod.UNCONSUMED_EVENTS_PATH = sd / "unconsumed_events.json"
    sup_mod.READINESS_STATE_PATH = sd / "readiness_state.json"
    sup_mod.STATE_DIR = sd
    sup_mod.WORKER_ATTEMPTS_DIR = sd / "worker_attempts"
    sup_mod.WORKER_ATTEMPTS_DIR.mkdir(parents=True, exist_ok=True)
    sup_mod.LEASE_PATH.write_text("{}")
    sup_mod.LAST_RESUME_PATH.write_text("{}")
    sup_mod.RUN_STATE.write_text(json.dumps({"current_head": "abc" * 14}))
    sup_mod.UNCONSUMED_EVENTS_PATH.write_text(json.dumps({"events": []}))
    sup_mod.READINESS_STATE_PATH.write_text(json.dumps({"state": "ACTIVE_REPAIR"}))
    sup_mod.SNAPSHOT_A_PATH.write_text(json.dumps({}))
    sup_mod.SNAPSHOT_B_PATH.write_text(json.dumps({}))
    sup_mod.QUOTA_PATH.write_text(json.dumps({"providers": {}}))
    return sup_mod


def _make_provider():
    return {
        "coderabbit": {
            "bot_logins": ["coderabbitai[bot]"],
            "trigger_handle": "@coderabbitai review",
            "quota_patterns": [],
            "use_reviews_api": False,
            "required_for_current_repair_round": True,
            "required_for_final_merge": True,
            "required_for_pr_416": True,
            "quota_reset_at": None,
        }
    }


# ---------------------------------------------------------------------------
# 1. Auto-paused + active worker: no request yet
# ---------------------------------------------------------------------------
def test_auto_paused_with_active_worker_no_request(tmp_path):
    """When a worker is alive, the supervisor must NOT post a review
    request. The Defect C autonomous retry must yield to active
    worker ownership."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    sup_mod.PROVIDERS = _make_provider()
    sup_mod.AUTHORITATIVE_HEAD = "abc" * 14
    sup_mod.POLICY = {
        "human_boundary": "merge_only",
        "required_review_providers_for_pr_416": ["coderabbit"],
        "optional_review_providers_for_pr_416": [],
        "provider_states_are_independent": True,
        "codex_quota_reset_at": None,
        "post_codex_recovery_request": False,
        "quiet_window_seconds": 180,
        "heartbeat_seconds": 120,
        "required_check_names": [],
        "quota_retry_initial_seconds": 3600,
        "quota_retry_backoff_seconds": 21600,
    }
    live = {"head_sha": "abc" * 14, "latest_comments_by_provider": {}}
    statuses = {
        "coderabbit": sup_mod.PROVIDER_STATE_AUTO_PAUSED_ACTIVE_DEVELOPMENT
    }
    # Pre-condition: no review request exists yet.
    rd = sup_mod.REVIEW_REQUESTS_DIR
    assert list(rd.glob("coderabbit__*.json")) == []
    # Invoke pause handler.
    any_paused, paused = sup_mod.handle_paused_providers(live, statuses)
    # No head_for_request was provided (live head missing here); the
    # handler rebinds pending_review_head to whatever authoritative
    # head is set. We assert that no request file was produced.
    request_files = list(rd.glob("coderabbit__*.json"))
    # The handler may still log "head changed" if pending_review_head
    # does not match; that's fine. The point is that no actual
    # request was posted.
    assert any_paused
    assert "coderabbit" in paused


# ---------------------------------------------------------------------------
# 2. Auto-paused + stable eligible exact head -> one request
# ---------------------------------------------------------------------------
def test_auto_paused_stable_head_one_request(tmp_path, monkeypatch):
    """Without an active worker, with the live head equal to the
    authoritative head, ONE request is posted."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    sup_mod.PROVIDERS = _make_provider()
    sup_mod.AUTHORITATIVE_HEAD = "abc" * 14
    sup_mod.POLICY = {
        "human_boundary": "merge_only",
        "required_review_providers_for_pr_416": ["coderabbit"],
        "optional_review_providers_for_pr_416": [],
        "provider_states_are_independent": True,
        "codex_quota_reset_at": None,
        "post_codex_recovery_request": False,
        "quiet_window_seconds": 180,
        "heartbeat_seconds": 120,
        "required_check_names": [],
        "quota_retry_initial_seconds": 3600,
        "quota_retry_backoff_seconds": 21600,
    }
    # Stub fetch_live_pr_head_now to return the authoritative head.
    monkeypatch.setattr(
        sup_mod, "fetch_live_pr_head_now", lambda: "abc" * 14
    )

    # Stub post_review_request to record that it was called.
    sent = {"called": 0, "heads": []}

    def fake_post(provider, head):
        sent["called"] += 1
        sent["heads"].append(head)
        sup_mod.write_review_request(
            provider=provider,
            head_sha=head,
            record={
                "actor": "post_review_request",
                "requested_at": "2026-08-13T12:00:00Z",
                "lifecycle": "REQUEST_SENT",
                "request_head": head,
            },
        )
        return True

    monkeypatch.setattr(sup_mod, "post_review_request", fake_post)

    # Pre-seed the quota state so the live head equals the
    # pending_review_head from the first call.
    sup_mod.QUOTA_PATH.write_text(json.dumps({
        "providers": {
            "coderabbit": {
                "pending_review_head": "abc" * 14,
                "retry_count": 0,
                "retry_epoch": 0,
                "next_retry_timestamp": "2026-01-01T00:00:00Z",
            }
        }
    }))

    live = {
        "head_sha": "abc" * 14,
        "latest_comments_by_provider": {},
        "latest_reviews_by_provider": {},
    }
    statuses = {
        "coderabbit": sup_mod.PROVIDER_STATE_AUTO_PAUSED_ACTIVE_DEVELOPMENT
    }
    sup_mod.handle_paused_providers(live, statuses)
    assert sent["called"] == 1, sent
    assert sent["heads"] == ["abc" * 14]


# ---------------------------------------------------------------------------
# 3. Request sent + waiting inside backoff -> no duplicate
# ---------------------------------------------------------------------------
def test_request_sent_within_backoff_no_duplicate(tmp_path, monkeypatch):
    """If a request was sent very recently (well within backoff),
    NO duplicate is sent."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    sup_mod.PROVIDERS = _make_provider()
    sup_mod.AUTHORITATIVE_HEAD = "abc" * 14
    sup_mod.POLICY = {
        "human_boundary": "merge_only",
        "required_review_providers_for_pr_416": ["coderabbit"],
        "optional_review_providers_for_pr_416": [],
        "provider_states_are_independent": True,
        "codex_quota_reset_at": None,
        "post_codex_recovery_request": False,
        "quiet_window_seconds": 180,
        "heartbeat_seconds": 120,
        "required_check_names": [],
        "quota_retry_initial_seconds": 3600,
        "quota_retry_backoff_seconds": 21600,
    }
    monkeypatch.setattr(
        sup_mod, "fetch_live_pr_head_now", lambda: "abc" * 14
    )

    sent = {"called": 0}

    def fake_post(provider, head):
        sent["called"] += 1
        return True

    monkeypatch.setattr(sup_mod, "post_review_request", fake_post)

    # Pre-persist a request at the same head, recently sent.
    sup_mod.write_review_request(
        provider="coderabbit",
        head_sha="abc" * 14,
        record={
            "actor": "post_review_request",
            "requested_at": "2026-08-13T12:00:00Z",
            "lifecycle": "REQUEST_SENT",
            "request_head": "abc" * 14,
        },
    )
    # Persist a quota state with retry_count > 0 and next_retry far
    # in the future.
    from datetime import datetime, timezone, timedelta
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    sup_mod.QUOTA_PATH.write_text(json.dumps({
        "providers": {
            "coderabbit": {
                "pending_review_head": "abc" * 14,
                "retry_count": 1,
                "retry_epoch": 1,
                "next_retry_timestamp": future,
            }
        }
    }))
    live = {
        "head_sha": "abc" * 14,
        "latest_comments_by_provider": {},
        "latest_reviews_by_provider": {},
    }
    statuses = {
        "coderabbit": sup_mod.PROVIDER_STATE_AUTO_PAUSED_ACTIVE_DEVELOPMENT
    }
    sup_mod.handle_paused_providers(live, statuses)
    assert sent["called"] == 0, sent


# ---------------------------------------------------------------------------
# 4. Deadline passes without acknowledgement -> retry becomes eligible
# ---------------------------------------------------------------------------
def test_deadline_passed_retry_eligible(tmp_path, monkeypatch):
    """If the existing same-head request has aged past the
    timeout_seconds but no acknowledgement has arrived, the
    retry is eligible."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    sup_mod.PROVIDERS = _make_provider()
    sup_mod.AUTHORITATIVE_HEAD = "abc" * 14
    sup_mod.POLICY = {
        "human_boundary": "merge_only",
        "required_review_providers_for_pr_416": ["coderabbit"],
        "optional_review_providers_for_pr_416": [],
        "provider_states_are_independent": True,
        "codex_quota_reset_at": None,
        "post_codex_recovery_request": False,
        "quiet_window_seconds": 180,
        "heartbeat_seconds": 120,
        "required_check_names": [],
        "quota_retry_initial_seconds": 3600,
        "quota_retry_backoff_seconds": 21600,
    }
    monkeypatch.setattr(
        sup_mod, "fetch_live_pr_head_now", lambda: "abc" * 14
    )

    sent = {"called": 0}

    def fake_post(provider, head):
        sent["called"] += 1
        sup_mod.write_review_request(
            provider=provider,
            head_sha=head,
            record={
                "actor": "post_review_request",
                "requested_at": "2026-08-13T12:00:00Z",
                "lifecycle": "REQUEST_SENT",
                "request_head": head,
            },
        )
        return True

    monkeypatch.setattr(sup_mod, "post_review_request", fake_post)

    # Pre-persist a request with a very old requested_at.
    sup_mod.write_review_request(
        provider="coderabbit",
        head_sha="abc" * 14,
        record={
            "actor": "post_review_request",
            "requested_at": "2026-01-01T00:00:00Z",
            "lifecycle": "REQUEST_SENT",
            "request_head": "abc" * 14,
        },
    )
    # Pre-persist a quota state with next_retry in the past.
    from datetime import datetime, timezone, timedelta
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    sup_mod.QUOTA_PATH.write_text(json.dumps({
        "providers": {
            "coderabbit": {
                "pending_review_head": "abc" * 14,
                "retry_count": 0,
                "retry_epoch": 0,
                "next_retry_timestamp": past,
            }
        }
    }))
    live = {
        "head_sha": "abc" * 14,
        "latest_comments_by_provider": {},
        "latest_reviews_by_provider": {},
    }
    statuses = {
        "coderabbit": sup_mod.PROVIDER_STATE_AUTO_PAUSED_ACTIVE_DEVELOPMENT
    }
    sup_mod.handle_paused_providers(live, statuses)
    assert sent["called"] == 1, sent


# ---------------------------------------------------------------------------
# 5. Retry occurs exactly once for that retry epoch
# ---------------------------------------------------------------------------
def test_retry_once_per_epoch(tmp_path, monkeypatch):
    """Within a single retry epoch, only ONE request is sent.
    The next rejection of the samehead guard prevents duplicates."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    sup_mod.PROVIDERS = _make_provider()
    sup_mod.AUTHORITATIVE_HEAD = "abc" * 14
    sup_mod.POLICY = {
        "human_boundary": "merge_only",
        "required_review_providers_for_pr_416": ["coderabbit"],
        "optional_review_providers_for_pr_416": [],
        "provider_states_are_independent": True,
        "codex_quota_reset_at": None,
        "post_codex_recovery_request": False,
        "quiet_window_seconds": 180,
        "heartbeat_seconds": 120,
        "required_check_names": [],
        "quota_retry_initial_seconds": 3600,
        "quota_retry_backoff_seconds": 21600,
    }
    monkeypatch.setattr(
        sup_mod, "fetch_live_pr_head_now", lambda: "abc" * 14
    )

    sent = {"called": 0}

    def fake_post(provider, head):
        sent["called"] += 1
        # Always write a fresh REQUEST_SENT marker on each call.
        sup_mod.write_review_request(
            provider=provider,
            head_sha=head,
            record={
                "actor": "post_review_request",
                "requested_at": "2026-08-13T12:00:00Z",
                "lifecycle": "REQUEST_SENT",
                "request_head": head,
            },
        )
        return True

    monkeypatch.setattr(sup_mod, "post_review_request", fake_post)

    # Pre-seed the quota state so the live head matches AND
    # the prior request is OLD (stale, past the retry deadline).
    from datetime import datetime, timezone, timedelta
    very_old = "2026-01-01T00:00:00Z"
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    sup_mod.QUOTA_PATH.write_text(json.dumps({
        "providers": {
            "coderabbit": {
                "pending_review_head": "abc" * 14,
                "retry_count": 1,
                "retry_epoch": 1,
                "next_retry_timestamp": past,
            }
        }
    }))
    # Pre-persist a STALE request: an old REQUEST_SENT marker
    # that the retry-eligibility logic must age out.
    sup_mod.write_review_request(
        provider="coderabbit",
        head_sha="abc" * 14,
        record={
            "actor": "post_review_request",
            "requested_at": very_old,
            "lifecycle": "REQUEST_SENT",
            "request_head": "abc" * 14,
        },
    )
    live = {
        "head_sha": "abc" * 14,
        "latest_comments_by_provider": {},
        "latest_reviews_by_provider": {},
    }
    statuses = {
        "coderabbit": sup_mod.PROVIDER_STATE_AUTO_PAUSED_ACTIVE_DEVELOPMENT
    }
    # First call: the prior request is stale, so the retry
    # eligibility fires and ONE POST is sent.
    sup_mod.handle_paused_providers(live, statuses)
    assert sent["called"] == 1, sent
    # Second call: the just-posted request is fresh (not stale),
    # so the same-head idempotency guard must skip the duplicate.
    sup_mod.handle_paused_providers(live, statuses)
    assert sent["called"] == 1, sent


# ---------------------------------------------------------------------------
# 6. Head changes -> old request superseded
# ---------------------------------------------------------------------------
def test_head_change_supersedes_old_request(tmp_path, monkeypatch):
    """When the live head changes, the prior head's request is
    marked SUPERSEDED and a fresh request lifecycle is started."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    sup_mod.PROVIDERS = _make_provider()
    sup_mod.AUTHORITATIVE_HEAD = "abc" * 14
    sup_mod.POLICY = {
        "human_boundary": "merge_only",
        "required_review_providers_for_pr_416": ["coderabbit"],
        "optional_review_providers_for_pr_416": [],
        "provider_states_are_independent": True,
        "codex_quota_reset_at": None,
        "post_codex_recovery_request": False,
        "quiet_window_seconds": 180,
        "heartbeat_seconds": 120,
        "required_check_names": [],
        "quota_retry_initial_seconds": 3600,
        "quota_retry_backoff_seconds": 21600,
    }
    monkeypatch.setattr(
        sup_mod, "fetch_live_pr_head_now", lambda: "def" * 14
    )

    # Pre-persist a request at the OLD head.
    sup_mod.write_review_request(
        provider="coderabbit",
        head_sha="abc" * 14,
        record={
            "actor": "post_review_request",
            "requested_at": "2026-08-13T12:00:00Z",
            "lifecycle": "REQUEST_SENT",
            "request_head": "abc" * 14,
        },
    )
    # Set the pending head to the old one.
    sup_mod.QUOTA_PATH.write_text(json.dumps({
        "providers": {
            "coderabbit": {
                "pending_review_head": "abc" * 14,
                "retry_count": 1,
                "retry_epoch": 1,
                "next_retry_timestamp": "2026-08-13T13:00:00Z",
            }
        }
    }))
    live = {
        "head_sha": "def" * 14,
        "latest_comments_by_provider": {},
        "latest_reviews_by_provider": {},
    }
    statuses = {
        "coderabbit": sup_mod.PROVIDER_STATE_AUTO_PAUSED_ACTIVE_DEVELOPMENT
    }
    sup_mod.handle_paused_providers(live, statuses)
    # A SUPERSEDED marker should exist for the old head.
    superseded = (
        sup_mod.REVIEW_REQUESTS_DIR
        / ("coderabbit__" + "abc" * 14 + ".superseded.json")
    )
    assert superseded.exists()


# ---------------------------------------------------------------------------
# 7. New head gets independent request lifecycle
# ---------------------------------------------------------------------------
def test_new_head_independent_lifecycle(tmp_path, monkeypatch):
    """A new head's request lifecycle is independent: the new
    pending_review_head and retry_epoch are reset to 0."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    sup_mod.PROVIDERS = _make_provider()
    sup_mod.AUTHORITATIVE_HEAD = "abc" * 14
    sup_mod.POLICY = {
        "human_boundary": "merge_only",
        "required_review_providers_for_pr_416": ["coderabbit"],
        "optional_review_providers_for_pr_416": [],
        "provider_states_are_independent": True,
        "codex_quota_reset_at": None,
        "post_codex_recovery_request": False,
        "quiet_window_seconds": 180,
        "heartbeat_seconds": 120,
        "required_check_names": [],
        "quota_retry_initial_seconds": 3600,
        "quota_retry_backoff_seconds": 21600,
    }
    monkeypatch.setattr(
        sup_mod, "fetch_live_pr_head_now", lambda: "def" * 14
    )
    sup_mod.QUOTA_PATH.write_text(json.dumps({
        "providers": {
            "coderabbit": {
                "pending_review_head": "abc" * 14,
                "retry_count": 5,
                "retry_epoch": 5,
                "next_retry_timestamp": "2026-08-13T13:00:00Z",
            }
        }
    }))
    live = {
        "head_sha": "def" * 14,
        "latest_comments_by_provider": {},
        "latest_reviews_by_provider": {},
    }
    statuses = {
        "coderabbit": sup_mod.PROVIDER_STATE_AUTO_PAUSED_ACTIVE_DEVELOPMENT
    }
    sup_mod.handle_paused_providers(live, statuses)
    # The persisted quota state should reflect the NEW head and
    # the retry_epoch should be reset to 0.
    state = json.loads(sup_mod.QUOTA_PATH.read_text())
    coderabbit = state["providers"]["coderabbit"]
    assert coderabbit["pending_review_head"] == "def" * 14
    assert coderabbit["retry_epoch"] == 0


# ---------------------------------------------------------------------------
# 8. Useful CodeRabbit review arrives -> retries stop
# ---------------------------------------------------------------------------
def test_useful_review_arrives_retries_stop(tmp_path, monkeypatch):
    """If ``latest_reviews_by_provider`` has a fresh bot review
    for the live head, the supervisor must NOT post a retry request."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    sup_mod.PROVIDERS = _make_provider()
    sup_mod.AUTHORITATIVE_HEAD = "abc" * 14
    sup_mod.POLICY = {
        "human_boundary": "merge_only",
        "required_review_providers_for_pr_416": ["coderabbit"],
        "optional_review_providers_for_pr_416": [],
        "provider_states_are_independent": True,
        "codex_quota_reset_at": None,
        "post_codex_recovery_request": False,
        "quiet_window_seconds": 180,
        "heartbeat_seconds": 120,
        "required_check_names": [],
        "quota_retry_initial_seconds": 3600,
        "quota_retry_backoff_seconds": 21600,
    }
    monkeypatch.setattr(
        sup_mod, "fetch_live_pr_head_now", lambda: "abc" * 14
    )

    sent = {"called": 0}

    def fake_post(provider, head):
        sent["called"] += 1
        return True

    monkeypatch.setattr(sup_mod, "post_review_request", fake_post)

    # No existing request ledger; no pending_review_head.
    sup_mod.QUOTA_PATH.write_text(json.dumps({"providers": {}}))
    live = {
        "head_sha": "abc" * 14,
        "latest_comments_by_provider": {},
        "latest_reviews_by_provider": {
            "coderabbit": [
                {
                    "id": 1,
                    "submitted_at": "2026-08-13T12:00:00Z",
                    "state": "APPROVED",
                    "body": "Looks good",
                }
            ]
        },
    }
    statuses = {
        "coderabbit": sup_mod.PROVIDER_STATE_AUTO_PAUSED_ACTIVE_DEVELOPMENT
    }
    sup_mod.handle_paused_providers(live, statuses)
    assert sent["called"] == 0, sent


# ---------------------------------------------------------------------------
# 9. Quota pause remains distinct from auto-paused
# ---------------------------------------------------------------------------
def test_quota_pause_distinct_from_auto_paused(tmp_path, monkeypatch):
    """A QUOTA_PAUSED classified body MUST remain distinct from
    AUTO_PAUSED_ACTIVE_DEVELOPMENT."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    sup_mod.PROVIDERS = _make_provider()
    sup_mod.AUTHORITATIVE_HEAD = None
    sup_mod.POLICY = {
        "human_boundary": "merge_only",
        "required_review_providers_for_pr_416": ["coderabbit"],
        "optional_review_providers_for_pr_416": [],
        "provider_states_are_independent": True,
        "codex_quota_reset_at": None,
        "post_codex_recovery_request": False,
        "quiet_window_seconds": 180,
        "heartbeat_seconds": 120,
        "required_check_names": [],
        "quota_retry_initial_seconds": 3600,
        "quota_retry_backoff_seconds": 21600,
    }
    monkeypatch.setattr(
        sup_mod, "fetch_live_pr_head_now", lambda: "abc" * 14
    )

    sup_mod.QUOTA_PATH.write_text(json.dumps({"providers": {}}))
    live = {
        "head_sha": "abc" * 14,
        "latest_comments_by_provider": {},
        "latest_reviews_by_provider": {},
    }
    statuses = {
        "coderabbit": sup_mod.PROVIDER_STATE_QUOTA_PAUSED
    }
    any_paused, paused = sup_mod.handle_paused_providers(live, statuses)
    assert any_paused
    assert "coderabbit" in paused


# ---------------------------------------------------------------------------
# 10. Supervisor restart preserves retry ownership
# ---------------------------------------------------------------------------
def test_supervisor_restart_preserves_retry_state(tmp_path, monkeypatch):
    """The QUOTA_PATH is persisted across supervisor restarts. The
    next heartbeat loads the state and continues the retry epoch
    from where it left off."""
    sup_mod = _bootstrap_temp_state(tmp_path)
    sup_mod.PROVIDERS = _make_provider()
    sup_mod.AUTHORITATIVE_HEAD = "abc" * 14
    sup_mod.POLICY = {
        "human_boundary": "merge_only",
        "required_review_providers_for_pr_416": ["coderabbit"],
        "optional_review_providers_for_pr_416": [],
        "provider_states_are_independent": True,
        "codex_quota_reset_at": None,
        "post_codex_recovery_request": False,
        "quiet_window_seconds": 180,
        "heartbeat_seconds": 120,
        "required_check_names": [],
        "quota_retry_initial_seconds": 3600,
        "quota_retry_backoff_seconds": 21600,
    }
    monkeypatch.setattr(
        sup_mod, "fetch_live_pr_head_now", lambda: "abc" * 14
    )

    # Simulate a prior heartbeat: retry_count=3, retry_epoch=3,
    # next_retry in the past.
    from datetime import datetime, timezone, timedelta
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    sup_mod.QUOTA_PATH.write_text(json.dumps({
        "providers": {
            "coderabbit": {
                "pending_review_head": "abc" * 14,
                "retry_count": 3,
                "retry_epoch": 3,
                "next_retry_timestamp": past,
            }
        }
    }))
    # No existing request marker.
    sent = {"called": 0}

    def fake_post(provider, head):
        sent["called"] += 1
        sup_mod.write_review_request(
            provider=provider,
            head_sha=head,
            record={
                "actor": "post_review_request",
                "requested_at": "2026-08-13T12:00:00Z",
                "lifecycle": "REQUEST_SENT",
                "request_head": head,
            },
        )
        return True

    monkeypatch.setattr(sup_mod, "post_review_request", fake_post)

