"""Round-42 regression tests — worker commit ownership
and the rejection of false push attribution.

Covers the production paths introduced by the round-42
infrastructure repair:

  Section 3   — reconstruct the C9 false-attribution
                incident.
  Section 4   — REMOTE HEAD MAY NEVER DISCOVER the
                commit to attribute to the worker.
  Section 6   — record commit immediately after worker
                creation; record push as a separate
                causal event.
  Section 8   — remove timestamp-only / origin-only /
                ancestry-only attribution.
  Section 9   — generic head movement classified as
                UNATTRIBUTED_HEAD_ADVANCE.
  Section 10  — manual / external commit canary
                (C9-style incident).
  Section 11  — positive worker commit canary.
  Section 12  — NO_CHANGES_REQUIRED must have ZERO
                produced/pushed commits.
  Section 13  — multi-commit worker attempts round-trip
                durably.

The tests exercise the production modules directly:
    autocoder_supervisor.supervisor
    autocoder_orchestration.worker_attempt
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


from autocoder_supervisor import supervisor
from autocoder_orchestration.worker_attempt import (
    LIFECYCLE_NO_CHANGES_REQUIRED,
    LIFECYCLE_PUSH_VERIFIED,
    LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE,
    LIFECYCLE_WORKER_EXITED_NO_PUSH,
    LIFECYCLE_WORKER_RUNNING,
    RESULT_TYPE_NO_CHANGES_REQUIRED,
    RESULT_TYPE_REPAIR_PUSHED,
    WorkerAttemptRecord,
    WorkerResultArtifact,
    WorkerAttemptStore,
    WORKER_RESULT_SCHEMA_VERSION,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(
    *,
    attempt_id: str = "att-test-1",
    prelaunch_head: str = "a" * 40,
    pushed_commit_sha: str = "",
    produced_commit_sha: str = "",
    extra: dict | None = None,
) -> WorkerAttemptRecord:
    return WorkerAttemptRecord(
        schema_version="autocoder.worker_attempt.v1",
        attempt_id=attempt_id,
        claim_id="claim-test",
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        pr_number=5,
        event_ids=("ev-1",),
        finding_ids=(),
        directive_digest="d" * 64,
        directive_path="/tmp/directive.json",
        prelaunch_head=prelaunch_head,
        expected_branch="feat/review-repair-relay-v1",
        pid=99999,
        lease_id=attempt_id,
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
        produced_commit_sha=produced_commit_sha or None,
        pushed_commit_sha=pushed_commit_sha or None,
        origin_head_verified=False,
        github_head_verified=False,
        terminal_reason=None,
        extra=extra or {},
    )


# ---------------------------------------------------------------------------
# TEST 1 — Manual C9-style commit during worker lifetime is REJECTED
# ---------------------------------------------------------------------------

def test_manual_commit_during_worker_lifetime_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-42 §10: a worker attempt that did NOT record
    any pushed_commit_sha MUST NOT be promoted to
    PUSH_VERIFIED even when the live head advanced past
    the prelaunch head, the origin branch matches the
    live head, AND the commit's committer date is after
    the worker started. These three signals together
    ARE NOT sufficient for worker ownership.
    """
    store_dir = tmp_path / "worker_attempts"
    store_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(supervisor, "WORKER_ATTEMPTS_DIR", store_dir)

    h0 = "a" * 40
    h1 = "b" * 40  # manual C9-style commit during worker lifetime
    attempt_id = "att-round42-c9-canary"

    # The worker attempt contains NO pushed_commit_sha
    # and NO produced_commit_sha. The worker is in
    # WORKER_RUNNING. The live head advanced from h0
    # to h1. The origin branch matches h1. The h1
    # committer date is after the worker started.
    rec = _make_record(
        attempt_id=attempt_id,
        prelaunch_head=h0,
        pushed_commit_sha="",
        produced_commit_sha="",
    )
    WorkerAttemptStore(store_dir).write(rec)
    monkeypatch.setattr(supervisor, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(supervisor, "_reap_worker", lambda _pid: (0, None))
    monkeypatch.setattr(supervisor, "remove_lease", lambda: None)

    # Mock the GitHub live-head fetch to return h1.
    monkeypatch.setattr(supervisor, "get_github_token", lambda: "tok")
    monkeypatch.setattr(
        supervisor, "github_get",
        lambda path, token, **_kw: {"head": {"sha": h1}} if "pulls" in path else None,
    )

    result = supervisor.poll_worker_attempt(
        attempt_id=attempt_id,
        lease={"attempt_id": attempt_id, "pid": 99999,
               "session_id": "ses_FAKE"},
    )
    # The poll returns the new state.
    rec = WorkerAttemptStore(store_dir).read(attempt_id)
    assert rec is not None
    # Round-42 invariant: the attempt is NOT promoted to
    # PUSH_VERIFIED. It is classified as
    # UNATTRIBUTED_HEAD_ADVANCE.
    assert rec.lifecycle != LIFECYCLE_PUSH_VERIFIED
    assert rec.lifecycle == LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE
    # The worker-emitted pushed_commit_sha is preserved
    # (empty) — the supervisor MUST NOT invent one.
    assert not rec.pushed_commit_sha
    assert not rec.produced_commit_sha


# ---------------------------------------------------------------------------
# TEST 2 — Worker-emitted pushed_commit_sha matches live head → PUSH_VERIFIED
# ---------------------------------------------------------------------------

def test_worker_reported_push_matches_live_head_promotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-42 §11: a worker attempt that recorded
    ``pushed_commit_sha == live_head`` IS promoted to
    PUSH_VERIFIED. The worker durably claimed the
    commit; the supervisor confirms via remote evidence.
    """
    store_dir = tmp_path / "worker_attempts"
    store_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(supervisor, "WORKER_ATTEMPTS_DIR", store_dir)

    h0 = "a" * 40
    h_d = "d" * 40  # worker-produced and worker-pushed
    attempt_id = "att-round42-positive"

    rec = _make_record(
        attempt_id=attempt_id,
        prelaunch_head=h0,
        pushed_commit_sha=h_d,
        produced_commit_sha=h_d,
    )
    WorkerAttemptStore(store_dir).write(rec)
    monkeypatch.setattr(supervisor, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(supervisor, "_reap_worker", lambda _pid: (0, None))
    monkeypatch.setattr(supervisor, "remove_lease", lambda: None)

    monkeypatch.setattr(supervisor, "get_github_token", lambda: "tok")
    monkeypatch.setattr(
        supervisor, "github_get",
        lambda path, token, **_kw: {"head": {"sha": h_d}} if "pulls" in path else None,
    )

    result = supervisor.poll_worker_attempt(
        attempt_id=attempt_id,
        lease={"attempt_id": attempt_id, "pid": 99999,
               "session_id": "ses_FAKE"},
    )
    rec = WorkerAttemptStore(store_dir).read(attempt_id)
    assert rec is not None
    # Round-42 positive: the worker durably reported the
    # live head as the worker's own push. PUSH_VERIFIED.
    assert rec.lifecycle == LIFECYCLE_PUSH_VERIFIED
    assert rec.pushed_commit_sha == h_d
    assert rec.produced_commit_sha == h_d


# ---------------------------------------------------------------------------
# TEST 3 — Multi-commit list round-trips durably
# ---------------------------------------------------------------------------

def test_multi_commit_list_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-42 §13: a worker attempt may produce more
    than one commit. The supervisor records the
    ORDERED list of produced/pushed SHAs in
    ``extra.produced_commit_shas`` /
    ``extra.pushed_commit_shas``. The final element is
    the head after the chain.
    """
    h0 = "a" * 40
    d1 = "1" * 40
    d2 = "2" * 40
    d3 = "3" * 40
    attempt_id = "att-round42-multi"

    rec = _make_record(
        attempt_id=attempt_id,
        prelaunch_head=h0,
        pushed_commit_sha=d3,
        produced_commit_sha=d1,  # first commit
        extra={
            "produced_commit_shas": [d1, d2, d3],
            "pushed_commit_shas": [d3],
        },
    )
    # Round-trip the record through the store.
    store = WorkerAttemptStore(tmp_path)
    store.write(rec)
    reloaded = store.read(attempt_id)
    assert reloaded is not None
    # The extra dict round-trips; the SHA list is intact.
    assert reloaded.extra["produced_commit_shas"] == [d1, d2, d3]
    assert reloaded.extra["pushed_commit_shas"] == [d3]
    # The single-SHA fields carry the chain endpoints.
    assert reloaded.produced_commit_sha == d1
    assert reloaded.pushed_commit_sha == d3


# ---------------------------------------------------------------------------
# TEST 4 — verify_push_against_attempt rejects when worker has no record
# ---------------------------------------------------------------------------

def test_verify_push_rejects_without_worker_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-42 §8/§10: ``verify_push_against_attempt``
    must return ``False`` for both ``github_head_verified``
    and ``origin_head_verified`` when the worker has
    NOT recorded the commit. Time-based and origin-based
    signals are INSUFFICIENT.
    """
    h0 = "a" * 40
    h1 = "b" * 40
    attempt_id = "att-round42-no-claim"

    rec = _make_record(
        attempt_id=attempt_id,
        prelaunch_head=h0,
        pushed_commit_sha="",
        produced_commit_sha="",
    )
    store = WorkerAttemptStore(tmp_path)
    store.write(rec)

    # Mock origin to return h1 (a C9-style manual commit).
    monkeypatch.setattr(
        supervisor.subprocess, "check_output",
        lambda *a, **kw: h1 + "\n",
    )
    out = supervisor.verify_push_against_attempt(store=store, 
        attempt_id=attempt_id, new_head_sha=h1,
    )
    assert out is not None
    # Round-42: both verification flags are False because
    # the worker did NOT durably record the commit. The
    # head-rebind path treats the advance as external.
    assert out["github_head_verified"] is False
    assert out["origin_head_verified"] is False


# ---------------------------------------------------------------------------
# TEST 5 — verify_push_against_attempt accepts when worker has pushed record
# ---------------------------------------------------------------------------

def test_verify_push_accepts_with_worker_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-42 §11: ``verify_push_against_attempt`` returns
    True when the worker's recorded ``pushed_commit_sha``
    equals the new head.
    """
    h0 = "a" * 40
    h_d = "d" * 40
    attempt_id = "att-round42-claim"

    rec = _make_record(
        attempt_id=attempt_id,
        prelaunch_head=h0,
        pushed_commit_sha=h_d,
        produced_commit_sha=h_d,
    )
    store = WorkerAttemptStore(tmp_path)
    store.write(rec)
    out = supervisor.verify_push_against_attempt(store=store, 
        attempt_id=attempt_id, new_head_sha=h_d,
    )
    assert out is not None
    assert out["github_head_verified"] is True
    assert out["origin_head_verified"] is True


# ---------------------------------------------------------------------------
# TEST 6 — NO_CHANGES_REQUIRED must have ZERO produced/pushed commits
# ---------------------------------------------------------------------------

def test_no_changes_required_zero_commits(
    tmp_path: Path,
) -> None:
    """Round-42 §12: ``NO_CHANGES_REQUIRED`` must have
    ZERO produced_commit_shas and ZERO
    pushed_commit_shas. If a no-op result is paired with
    a commit, the artifact is INVALID.
    """
    # Valid no-op: zero produced, zero pushed.
    # Round-54/C22 hardening: the artifact must carry the
    # canonical repo / pr / branch / head / result_contract_id
    # identity fields so the validator can cross-check the
    # prelaunch identity.
    artifact = WorkerResultArtifact(
        schema_version=WORKER_RESULT_SCHEMA_VERSION,
        attempt_id="att-1",
        claim_id="claim-test",
        directive_digest="d" * 64,
        result_type=RESULT_TYPE_NO_CHANGES_REQUIRED,
        produced_commit_shas=(),
        pushed_commit_shas=(),
        completed_at="2026-08-10T00:00:00Z",
        repo="Slideshow11/AutoDev",
        pr_number=5,
        expected_branch="feat/review-repair-relay-v1",
        prelaunch_head="a" * 40,
        extra={
            "result_contract_id": "rc-test-valid",
            "expected_result_contract_id": "rc-test-valid",
            "observed_result_contract_id": "rc-test-valid",
            "result_contract_match": True,
        },
    )
    # Round-54/C22 §1: the attempt record MUST carry the
    # supervisor-owned result_contract_id so the
    # validator's identity check passes.
    rec = _make_record(
        attempt_id="att-1",
        extra={"result_contract_id": "rc-test-valid"},
    )
    errors = artifact.validate_against_attempt(rec)
    assert errors == []

    # Invalid no-op: non-empty produced.
    artifact2 = WorkerResultArtifact(
        schema_version=WORKER_RESULT_SCHEMA_VERSION,
        attempt_id="att-1",
        claim_id="claim-test",
        directive_digest="d" * 64,
        result_type=RESULT_TYPE_NO_CHANGES_REQUIRED,
        produced_commit_shas=("c" * 40,),
        pushed_commit_shas=(),
        completed_at="2026-08-10T00:00:00Z",
    )
    errors2 = artifact2.validate_against_attempt(rec)
    assert any("NO_CHANGES_REQUIRED" in e for e in errors2)


# ---------------------------------------------------------------------------
# TEST 7 — WorkerResultArtifact validates attempt/claim/directive binding
# ---------------------------------------------------------------------------

def test_artifact_rejects_mismatched_attempt() -> None:
    """Round-42 §5/§15: the artifact MUST be causally
    bound to the exact attempt. attempt_id / claim_id /
    directive_digest mismatches are rejected.
    """
    artifact = WorkerResultArtifact(
        schema_version=WORKER_RESULT_SCHEMA_VERSION,
        attempt_id="att-A",
        claim_id="claim-A",
        directive_digest="d" * 64,
        result_type=RESULT_TYPE_REPAIR_PUSHED,
        produced_commit_shas=("c" * 40,),
        pushed_commit_shas=("c" * 40,),
        completed_at="2026-08-10T00:00:00Z",
    )
    rec = _make_record(attempt_id="att-B")
    errors = artifact.validate_against_attempt(rec)
    assert any("attempt_id mismatch" in e for e in errors)


def test_artifact_rejects_mismatched_directive() -> None:
    """The directive digest on the artifact MUST equal
    the attempt's directive digest.
    """
    artifact = WorkerResultArtifact(
        schema_version=WORKER_RESULT_SCHEMA_VERSION,
        attempt_id="att-1",
        claim_id="claim-1",
        directive_digest="a" * 64,  # wrong
        result_type=RESULT_TYPE_REPAIR_PUSHED,
        produced_commit_shas=("c" * 40,),
        pushed_commit_shas=("c" * 40,),
        completed_at="2026-08-10T00:00:00Z",
    )
    rec = _make_record(attempt_id="att-1")
    rec = WorkerAttemptRecord(
        **{**rec.__dict__, "directive_digest": "b" * 64}
    )
    errors = artifact.validate_against_attempt(rec)
    assert any("directive_digest mismatch" in e for e in errors)


# ---------------------------------------------------------------------------
# TEST 8 — Round-trip artifact through write/read
# ---------------------------------------------------------------------------

def test_artifact_round_trip(tmp_path: Path) -> None:
    """Round-42 §5: the artifact is the SOLE source of
    truth for worker produced/pushed SHAs. The
    write/read round-trip must preserve every field.
    """
    artifact = WorkerResultArtifact(
        schema_version=WORKER_RESULT_SCHEMA_VERSION,
        attempt_id="att-1",
        claim_id="claim-1",
        directive_digest="d" * 64,
        result_type=RESULT_TYPE_REPAIR_PUSHED,
        produced_commit_shas=("c1" * 20, "c2" * 20),
        pushed_commit_shas=("c2" * 20,),
        completed_at="2026-08-10T00:00:00Z",
        no_changes_required_proof=None,
        tests_run=42,
        tests_passed=42,
        attempt_nonce="nonce-abc-123",
        repo="Slideshow11/AutoDev",
        pr_number=5,
        expected_branch="feat/review-repair-relay-v1",
        prelaunch_head="a" * 40,
        worker_session_id="20260810_191108_7924eb",
        worker_pid=99999,
    )
    path = tmp_path / "result.json"
    artifact.write(path)
    reloaded = WorkerResultArtifact.read(path)
    assert reloaded is not None
    assert reloaded.attempt_id == "att-1"
    assert reloaded.produced_commit_shas == ("c1" * 20, "c2" * 20)
    assert reloaded.pushed_commit_shas == ("c2" * 20,)
    assert reloaded.attempt_nonce == "nonce-abc-123"
    assert reloaded.tests_passed == 42


# ---------------------------------------------------------------------------
# TEST 9 — Committer date alone is insufficient
# ---------------------------------------------------------------------------

def test_committer_date_alone_insufficient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-42 §8: committer-date evidence alone (no
    worker record) MUST NOT verify a push. This is the
    central C9-style canary.
    """
    h0 = "a" * 40
    h1 = "c" * 40
    attempt_id = "att-round42-time-only"

    rec = _make_record(
        attempt_id=attempt_id,
        prelaunch_head=h0,
        pushed_commit_sha="",
        produced_commit_sha="",
    )
    store = WorkerAttemptStore(tmp_path)
    store.write(rec)

    # Mock origin to return h1 (manual commit after worker start).
    monkeypatch.setattr(
        supervisor.subprocess, "check_output",
        lambda *a, **kw: h1 + "\n",
    )
    out = supervisor.verify_push_against_attempt(store=store, 
        attempt_id=attempt_id, new_head_sha=h1,
    )
    assert out is not None
    # Round-42: committer date + origin equality are
    # not sufficient. Both flags are False.
    assert out["github_head_verified"] is False
    assert out["origin_head_verified"] is False


# ---------------------------------------------------------------------------
# TEST 10 — Origin equality alone is insufficient
# ---------------------------------------------------------------------------

def test_origin_equality_alone_insufficient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-42 §8: origin/<branch> == new_head_sha
    alone is NOT sufficient. Worker must durably
    record the commit.
    """
    h0 = "a" * 40
    h1 = "o" * 40  # matches origin, but no worker record
    attempt_id = "att-round42-origin-only"

    rec = _make_record(
        attempt_id=attempt_id,
        prelaunch_head=h0,
        pushed_commit_sha="",
        produced_commit_sha="",
    )
    store = WorkerAttemptStore(tmp_path)
    store.write(rec)

    # Mock origin to return h1.
    monkeypatch.setattr(
        supervisor.subprocess, "check_output",
        lambda *a, **kw: h1 + "\n",
    )
    out = supervisor.verify_push_against_attempt(store=store, 
        attempt_id=attempt_id, new_head_sha=h1,
    )
    assert out is not None
    # Both flags must be False.
    assert out["github_head_verified"] is False
    assert out["origin_head_verified"] is False


# ---------------------------------------------------------------------------
# TEST 11 — LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE is a new terminal state
# ---------------------------------------------------------------------------

def test_unattributed_lifecycle_is_terminal() -> None:
    """Round-42: the new ``UNATTRIBUTED_HEAD_ADVANCE``
    lifecycle is in the terminal set so the attempt is
    considered finished (RETRY_PENDING for the new
    head's finding).
    """
    from autocoder_orchestration.worker_attempt import (
        TERMINAL_LIFECYCLES,
    )
    assert LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE in TERMINAL_LIFECYCLES


# ---------------------------------------------------------------------------
# TEST 12 — Unattributed head advance does NOT call report_repair_pushed
# ---------------------------------------------------------------------------

def test_unattributed_does_not_call_report_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-42 §9/§10: when an attempt is classified as
    UNATTRIBUTED_HEAD_ADVANCE, the supervisor MUST NOT
    call ``report_repair_pushed`` for the new head. The
    attempt's lifecycle is terminal and the head-rebind
    path's bound-active-attempt lookup will return this
    attempt as a PUSH_VERIFIED candidate only when the
    worker has durably recorded the commit.

    In this test we directly drive the verifier
    (``verify_push_against_attempt``) which is what the
    head-rebind path consults.
    """
    h0 = "a" * 40
    h1 = "u" * 40
    attempt_id = "att-round42-unattributed"

    rec = _make_record(
        attempt_id=attempt_id,
        prelaunch_head=h0,
        pushed_commit_sha="",
        produced_commit_sha="",
    )
    store = WorkerAttemptStore(tmp_path)
    store.write(rec)

    # Mock origin to return h1.
    monkeypatch.setattr(
        supervisor.subprocess, "check_output",
        lambda *a, **kw: h1 + "\n",
    )
    out = supervisor.verify_push_against_attempt(store=store, 
        attempt_id=attempt_id, new_head_sha=h1,
    )
    # Round-42: both flags False. The head-rebind path
    # treats the advance as external and skips
    # ``mark_head_advanced_public``.
    assert out is not None
    assert out["github_head_verified"] is False
    assert out["origin_head_verified"] is False


# ---------------------------------------------------------------------------
# TEST 13 — Multi-commit list end-to-end: head equals LAST pushed
# ---------------------------------------------------------------------------

def test_multi_commit_final_pushed_matches_live_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-42 §13: a multi-commit worker attempt
    (D1 -> D2 -> D3) reports an ORDERED list of
    pushed SHAs in ``extra.pushed_commit_shas``. The
    supervisor promotes the attempt when the live head
    equals the LAST element of that list (the chain
    head after the final commit).
    """
    h0 = "a" * 40
    d1 = "1" * 40
    d2 = "2" * 40
    d3 = "3" * 40
    attempt_id = "att-round42-multi-final"

    rec = _make_record(
        attempt_id=attempt_id,
        prelaunch_head=h0,
        pushed_commit_sha=d3,  # final commit
        produced_commit_sha=d1,
        extra={
            "produced_commit_shas": [d1, d2, d3],
            "pushed_commit_shas": [d3],
        },
    )
    store = WorkerAttemptStore(tmp_path)
    store.write(rec)
    out = supervisor.verify_push_against_attempt(store=store, 
        attempt_id=attempt_id, new_head_sha=d3,
    )
    assert out is not None
    assert out["github_head_verified"] is True
    assert out["origin_head_verified"] is True


# ---------------------------------------------------------------------------
# TEST 14 — Multi-commit list where live head matches an INTERMEDIATE commit
# ---------------------------------------------------------------------------

def test_multi_commit_intermediate_head_not_promoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-42 §13: when a multi-commit attempt's pushed
    list is [D1, D2, D3] and the live head is D2 (an
    intermediate), the verifier MUST NOT promote. The
    worker reported the final head as D3, not D2. The
    head movement is unattributed.
    """
    h0 = "a" * 40
    d1 = "1" * 40
    d2 = "2" * 40
    d3 = "3" * 40
    attempt_id = "att-round42-multi-mid"

    rec = _make_record(
        attempt_id=attempt_id,
        prelaunch_head=h0,
        pushed_commit_sha=d3,
        produced_commit_sha=d1,
        extra={
            "produced_commit_shas": [d1, d2, d3],
            "pushed_commit_shas": [d3],
        },
    )
    store = WorkerAttemptStore(tmp_path)
    store.write(rec)
    out = supervisor.verify_push_against_attempt(store=store, 
        attempt_id=attempt_id, new_head_sha=d2,
    )
    assert out is not None
    # Round-42: live head (D2) does not match the LAST
    # pushed (D3). The verifier returns False.
    assert out["github_head_verified"] is False
    assert out["origin_head_verified"] is False


# ---------------------------------------------------------------------------
# TEST 15 — poll_worker_attempt with no head movement falls back to NO_PUSH
# ---------------------------------------------------------------------------

def test_no_head_movement_falls_back_to_no_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-42: when the worker exits without recording
    pushed_commit_sha and the live head is unchanged,
    the attempt is classified as
    WORKER_EXITED_NO_PUSH (the round-37 fallback).
    """
    store_dir = tmp_path / "worker_attempts"
    store_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(supervisor, "WORKER_ATTEMPTS_DIR", store_dir)

    h0 = "a" * 40
    attempt_id = "att-round42-no-movement"

    rec = _make_record(
        attempt_id=attempt_id,
        prelaunch_head=h0,
        pushed_commit_sha="",
        produced_commit_sha="",
    )
    WorkerAttemptStore(store_dir).write(rec)
    monkeypatch.setattr(supervisor, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(supervisor, "_reap_worker", lambda _pid: (0, None))
    monkeypatch.setattr(supervisor, "remove_lease", lambda: None)

    # Live head == prelaunch head (no movement).
    monkeypatch.setattr(supervisor, "get_github_token", lambda: "tok")
    monkeypatch.setattr(
        supervisor, "github_get",
        lambda path, token, **_kw: {"head": {"sha": h0}} if "pulls" in path else None,
    )

    supervisor.poll_worker_attempt(
        attempt_id=attempt_id,
        lease={"attempt_id": attempt_id, "pid": 99999,
               "session_id": "ses_FAKE"},
    )
    rec = WorkerAttemptStore(store_dir).read(attempt_id)
    assert rec is not None
    # No head movement, no worker record → NO_PUSH.
    assert rec.lifecycle == LIFECYCLE_WORKER_EXITED_NO_PUSH


# ---------------------------------------------------------------------------
# TEST 16 — Required tests listed in Section 13 of round-42
# ---------------------------------------------------------------------------

def test_lifecycle_unattributed_is_defined() -> None:
    """The new ``LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE``
    constant is exported and equals the documented
    string.
    """
    assert LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE == (
        "UNATTRIBUTED_HEAD_ADVANCE"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
