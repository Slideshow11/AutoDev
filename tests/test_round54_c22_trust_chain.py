"""Round 54 (C22) regression tests for trust-chain repairs.

These tests exercise the production reconciliation path through
reconcile_orphaned_worker_attempts. They MUST fail against the
pre-C22 source and pass after the C22 source repairs.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Ensure the project root is on sys.path so the supervisor module
# can be imported as `autocoder_supervisor.supervisor`.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Section 2 regression: _disposition variable bug.
# Pre-fix: NameError is swallowed by except Exception: pass; remote
# resolution still runs without source terminality.
# Post-fix: source terminalization must complete before
# resolveReviewThread.
# ---------------------------------------------------------------------------
def _c22_make_running_dict(attempt_id, prelaunch_head, extra=None,
                          result_artifact_path=None, event_ids=(),
                          finding_ids=()):
    return {
        "schema_version": "autocoder.worker_attempt.v1",
        "attempt_id": attempt_id,
        "claim_id": f"lease-c22-{attempt_id[-6:]}",
        "repo_owner": "OWNER", "repo_name": "REPO", "pr_number": 5,
        "event_ids": list(event_ids), "finding_ids": list(finding_ids),
        "directive_digest": "c22" + "0" * 60,
        "directive_path": "/tmp/c22/d.json",
        "prelaunch_head": prelaunch_head,
        "expected_branch": "feat/review-repair-relay-v1",
        "pid": 999999, "lease_id": f"lease-c22-{attempt_id[-6:]}",
        "started_at": "2026-01-01T00:00:00Z",
        "last_progress_at": "2026-01-01T00:00:00Z",
        "finished_at": None,
        "lifecycle": "WORKER_RUNNING",
        "attempt_count": 1,
        "stdout_path": None, "stderr_path": None,
        "exit_code": None, "signal": None,
        "result_artifact_path": result_artifact_path,
        "produced_commit_sha": None,
        "pushed_commit_sha": None,
        "origin_head_verified": False,
        "github_head_verified": False,
        "terminal_reason": None,
        "extra": extra or {"attempt_nonce": attempt_id.rsplit("-", 1)[0]},
    }


def _c22_make_artifact(attempt_id, claim_id, result_type, findings=None,
                       produced_shas=None, pushed_shas=None,
                       directive_digest=None, prelaunch_head=None):
    return {
        "schema_version": "autocoder.worker_result.v1",
        "attempt_id": attempt_id,
        "claim_id": claim_id,
        "directive_digest": directive_digest or ("c22" + "0" * 60),
        "result_type": result_type,
        "produced_commit_shas": produced_shas or [],
        "pushed_commit_shas": pushed_shas or [],
        "completed_at": "2026-01-01T00:00:00Z",
        "no_changes_required_proof": {
            "findings": findings or [],
            "source": "round50_envelope_parser",
        },
        "attempt_nonce": attempt_id.rsplit("-", 1)[0],
        "repo": "OWNER/REPO", "pr_number": 5,
        "expected_branch": "feat/review-repair-relay-v1",
        "prelaunch_head": prelaunch_head or ("0" * 40),
    }


def test_round54_c22_consumer_uses_exact_per_thread_disposition(tmp_path, monkeypatch):
    """C22 §2: the consume call must pass the PER-THREAD normalized
    disposition (ALREADY_SATISFIED, SUPERSEDED, REPAIRED), not a
    non-existent variable or a single attempt-level default.
    """
    from autocoder_supervisor import supervisor as sm
    wa_dir = tmp_path / "wa"
    wa_dir.mkdir()
    rs_path = tmp_path / "run_state.json"
    rs_path.write_text(
        json.dumps({"current_head": "0" * 40, "orchestration_state_root": ""})
    )
    monkeypatch.setattr(sm, "RUN_STATE", rs_path)
    monkeypatch.setattr(sm, "WORKER_ATTEMPTS_DIR", wa_dir)

    attempt_id = "att-c22-disp-fixture"
    rec_dict = _c22_make_running_dict(
        attempt_id=attempt_id,
        prelaunch_head="0" * 40,
        result_artifact_path=str(wa_dir / f"{attempt_id}.worker_result.json"),
        event_ids=["unresolved_thread_drain:PRRT_kwDOTtyQLc6Xsatisfied"],
    )
    artifact = _c22_make_artifact(
        attempt_id=attempt_id,
        claim_id=rec_dict["claim_id"],
        result_type="NO_CHANGES_REQUIRED",
        findings=[
            {"finding_id": "thread:PRRT_kwDOTtyQLc6Xsatisfied",
             "disposition": "ALREADY_SATISFIED"},
        ],
    )
    (wa_dir / f"{attempt_id}.json").write_text(json.dumps(rec_dict, indent=2))
    (wa_dir / f"{attempt_id}.worker_result.json").write_text(
        json.dumps(artifact, indent=2)
    )

    consume_calls = []
    def fake_consume(**kw):
        consume_calls.append(kw)
    remote_calls = []
    monkeypatch.setattr(sm, "read_lease", lambda: None)
    monkeypatch.setattr(sm, "consume_thread_drain_event_in_terminal_disposition",
                       fake_consume)
    monkeypatch.setattr(sm, "resolveReviewThread",
                       lambda **kw: remote_calls.append(kw))

    sm.reconcile_orphaned_worker_attempts(work_dir=tmp_path / "wa")

    assert len(consume_calls) == 1, (
        f"Expected exactly 1 consume call, got {len(consume_calls)}"
    )
    dr = consume_calls[0].get("disposition_raw")
    assert dr == "ALREADY_SATISFIED", (
        f"disposition_raw must be ALREADY_SATISFIED (per-finding); got {dr!r}"
    )


def test_round54_c22_consumer_uses_superseded_disposition(tmp_path, monkeypatch):
    """C22 §2 / §13: terminal SUPERSEDED finding must be passed as
    disposition_raw=SUPERSEDED to the consume helper.
    """
    from autocoder_supervisor import supervisor as sm
    wa_dir = tmp_path / "wa"
    wa_dir.mkdir()
    rs_path = tmp_path / "run_state.json"
    rs_path.write_text(
        json.dumps({"current_head": "0" * 40, "orchestration_state_root": ""})
    )
    monkeypatch.setattr(sm, "RUN_STATE", rs_path)
    monkeypatch.setattr(sm, "WORKER_ATTEMPTS_DIR", wa_dir)

    attempt_id = "att-c22-superseded"
    rec_dict = _c22_make_running_dict(
        attempt_id=attempt_id,
        prelaunch_head="0" * 40,
        result_artifact_path=str(wa_dir / f"{attempt_id}.worker_result.json"),
        event_ids=["unresolved_thread_drain:PRRT_kwDOTtyQLc6Xsupersed"],
    )
    artifact = _c22_make_artifact(
        attempt_id=attempt_id,
        claim_id=rec_dict["claim_id"],
        result_type="NO_CHANGES_REQUIRED",
        findings=[
            {"finding_id": "thread:PRRT_kwDOTtyQLc6Xsupersed",
             "disposition": "SUPERSEDED"},
        ],
    )
    (wa_dir / f"{attempt_id}.json").write_text(json.dumps(rec_dict, indent=2))
    (wa_dir / f"{attempt_id}.worker_result.json").write_text(
        json.dumps(artifact, indent=2)
    )

    consume_calls = []
    def fake_consume(**kw):
        consume_calls.append(kw)
    remote_calls = []
    monkeypatch.setattr(sm, "read_lease", lambda: None)
    monkeypatch.setattr(sm, "consume_thread_drain_event_in_terminal_disposition",
                       fake_consume)
    monkeypatch.setattr(sm, "resolveReviewThread",
                       lambda **kw: remote_calls.append(kw))

    sm.reconcile_orphaned_worker_attempts(work_dir=tmp_path / "wa")

    assert len(consume_calls) == 1
    dr = consume_calls[0].get("disposition_raw")
    assert dr == "SUPERSEDED", f"disposition_raw must be SUPERSEDED; got {dr!r}"


def test_round54_c22_consumer_uses_repaired_disposition(tmp_path, monkeypatch):
    """C22 §2 / §13: REPAIR_PUSHED with REPAIRED disposition must
    be passed as disposition_raw=REPAIRED to the consume helper.
    """
    from autocoder_supervisor import supervisor as sm
    wa_dir = tmp_path / "wa"
    wa_dir.mkdir()
    rs_path = tmp_path / "run_state.json"
    D = "c22test" + "1" * 36  # 40 hex chars
    rs_path.write_text(
        json.dumps({"current_head": D, "orchestration_state_root": ""})
    )
    monkeypatch.setattr(sm, "RUN_STATE", rs_path)
    monkeypatch.setattr(sm, "WORKER_ATTEMPTS_DIR", wa_dir)

    attempt_id = "att-c22-repaired"
    rec_dict = _c22_make_running_dict(
        attempt_id=attempt_id,
        prelaunch_head=D,
        result_artifact_path=str(wa_dir / f"{attempt_id}.worker_result.json"),
        event_ids=["unresolved_thread_drain:PRRT_kwDOTtyQLc6Xrepaired"],
    )
    artifact = _c22_make_artifact(
        attempt_id=attempt_id,
        claim_id=rec_dict["claim_id"],
        result_type="REPAIR_PUSHED",
        findings=[
            {"finding_id": "thread:PRRT_kwDOTtyQLc6Xrepaired",
             "disposition": "REPAIRED"},
        ],
        produced_shas=[D],
        pushed_shas=[D],
        prelaunch_head=D,
    )
    (wa_dir / f"{attempt_id}.json").write_text(json.dumps(rec_dict, indent=2))
    (wa_dir / f"{attempt_id}.worker_result.json").write_text(
        json.dumps(artifact, indent=2)
    )

    consume_calls = []
    remote_calls = []
    monkeypatch.setattr(sm, "read_lease", lambda: None)
    monkeypatch.setattr(sm, "consume_thread_drain_event_in_terminal_disposition",
                       lambda **kw: consume_calls.append(kw))
    monkeypatch.setattr(sm, "resolveReviewThread",
                       lambda **kw: remote_calls.append(kw))
    monkeypatch.setattr(sm, "verify_push_against_attempt",
                       lambda **kw: {"github_head_verified": True,
                                     "origin_head_verified": True,
                                     "produced_commit_sha": D})

    sm.reconcile_orphaned_worker_attempts(work_dir=tmp_path / "wa")

    drs = [c.get("disposition_raw") for c in consume_calls]
    assert "REPAIRED" in drs, (
        f"disposition_raw REPAIRED must be in the consume calls; got {drs}"
    )


def test_round54_c22_remote_resolution_blocked_when_consumer_raises(
    tmp_path, monkeypatch
):
    """C22 §3: if the durable source terminalization (consume)
    raises, the supervisor MUST NOT call resolveReviewThread.
    """
    from autocoder_supervisor import supervisor as sm
    wa_dir = tmp_path / "wa"
    wa_dir.mkdir()
    rs_path = tmp_path / "run_state.json"
    rs_path.write_text(
        json.dumps({"current_head": "0" * 40, "orchestration_state_root": ""})
    )
    monkeypatch.setattr(sm, "RUN_STATE", rs_path)
    monkeypatch.setattr(sm, "WORKER_ATTEMPTS_DIR", wa_dir)

    attempt_id = "att-c22-consumer-raises"
    rec_dict = _c22_make_running_dict(
        attempt_id=attempt_id,
        prelaunch_head="0" * 40,
        result_artifact_path=str(wa_dir / f"{attempt_id}.worker_result.json"),
    )
    artifact = _c22_make_artifact(
        attempt_id=attempt_id,
        claim_id=rec_dict["claim_id"],
        result_type="NO_CHANGES_REQUIRED",
        findings=[
            {"finding_id": "thread:PRRT_kwDOTtyQLc6XraFail",
             "disposition": "ALREADY_SATISFIED"},
        ],
    )
    (wa_dir / f"{attempt_id}.json").write_text(json.dumps(rec_dict, indent=2))
    (wa_dir / f"{attempt_id}.worker_result.json").write_text(
        json.dumps(artifact, indent=2)
    )

    def raising_consume(**kw):
        raise RuntimeError("simulated durable write failure")
    remote_calls = []
    monkeypatch.setattr(sm, "read_lease", lambda: None)
    monkeypatch.setattr(sm, "consume_thread_drain_event_in_terminal_disposition",
                       raising_consume)
    monkeypatch.setattr(sm, "resolveReviewThread",
                       lambda **kw: remote_calls.append(kw))

    sm.reconcile_orphaned_worker_attempts(work_dir=tmp_path / "wa")

    assert len(remote_calls) == 0, (
        f"resolveReviewThread called {len(remote_calls)} times despite "
        f"consume raising; remote resolution must be blocked when "
        f"source terminalization fails"
    )


def test_round54_c22_incomplete_evidence_blocks_remote_resolution(
    tmp_path, monkeypatch
):
    """C22 §7: INCOMPLETE_EVIDENCE finding MUST NOT trigger
    resolveReviewThread.
    """
    from autocoder_supervisor import supervisor as sm
    wa_dir = tmp_path / "wa"
    wa_dir.mkdir()
    rs_path = tmp_path / "run_state.json"
    rs_path.write_text(
        json.dumps({"current_head": "0" * 40, "orchestration_state_root": ""})
    )
    monkeypatch.setattr(sm, "RUN_STATE", rs_path)
    monkeypatch.setattr(sm, "WORKER_ATTEMPTS_DIR", wa_dir)

    attempt_id = "att-c22-incomplete"
    rec_dict = _c22_make_running_dict(
        attempt_id=attempt_id,
        prelaunch_head="0" * 40,
        result_artifact_path=str(wa_dir / f"{attempt_id}.worker_result.json"),
    )
    artifact = _c22_make_artifact(
        attempt_id=attempt_id,
        claim_id=rec_dict["claim_id"],
        result_type="NO_CHANGES_REQUIRED",
        findings=[
            {"finding_id": "thread:PRRT_kwDOTtyQLc6Xincomplete",
             "disposition": "INCOMPLETE_EVIDENCE"},
        ],
    )
    (wa_dir / f"{attempt_id}.json").write_text(json.dumps(rec_dict, indent=2))
    (wa_dir / f"{attempt_id}.worker_result.json").write_text(
        json.dumps(artifact, indent=2)
    )

    remote_calls = []
    consume_calls = []
    monkeypatch.setattr(sm, "read_lease", lambda: None)
    monkeypatch.setattr(sm, "consume_thread_drain_event_in_terminal_disposition",
                       lambda **kw: consume_calls.append(kw))
    monkeypatch.setattr(sm, "resolveReviewThread",
                       lambda **kw: remote_calls.append(kw))

    sm.reconcile_orphaned_worker_attempts(work_dir=tmp_path / "wa")

    assert len(remote_calls) == 0, (
        f"INCOMPLETE_EVIDENCE must not trigger resolveReviewThread; "
        f"got {len(remote_calls)} calls"
    )
    assert len(consume_calls) == 0, (
        f"INCOMPLETE_EVIDENCE must not consume drain event; "
        f"got {len(consume_calls)} calls"
    )


def test_round54_c22_still_actionable_blocks_remote_resolution(
    tmp_path, monkeypatch
):
    """C22 §7: STILL_ACTIONABLE finding MUST NOT trigger
    resolveReviewThread.
    """
    from autocoder_supervisor import supervisor as sm
    wa_dir = tmp_path / "wa"
    wa_dir.mkdir()
    rs_path = tmp_path / "run_state.json"
    rs_path.write_text(
        json.dumps({"current_head": "0" * 40, "orchestration_state_root": ""})
    )
    monkeypatch.setattr(sm, "RUN_STATE", rs_path)
    monkeypatch.setattr(sm, "WORKER_ATTEMPTS_DIR", wa_dir)

    attempt_id = "att-c22-still-actionable"
    rec_dict = _c22_make_running_dict(
        attempt_id=attempt_id,
        prelaunch_head="0" * 40,
        result_artifact_path=str(wa_dir / f"{attempt_id}.worker_result.json"),
    )
    artifact = _c22_make_artifact(
        attempt_id=attempt_id,
        claim_id=rec_dict["claim_id"],
        result_type="NO_CHANGES_REQUIRED",
        findings=[
            {"finding_id": "thread:PRRT_kwDOTtyQLc6Xstillactionable",
             "disposition": "STILL_ACTIONABLE"},
        ],
    )
    (wa_dir / f"{attempt_id}.json").write_text(json.dumps(rec_dict, indent=2))
    (wa_dir / f"{attempt_id}.worker_result.json").write_text(
        json.dumps(artifact, indent=2)
    )

    remote_calls = []
    consume_calls = []
    monkeypatch.setattr(sm, "read_lease", lambda: None)
    monkeypatch.setattr(sm, "consume_thread_drain_event_in_terminal_disposition",
                       lambda **kw: consume_calls.append(kw))
    monkeypatch.setattr(sm, "resolveReviewThread",
                       lambda **kw: remote_calls.append(kw))

    sm.reconcile_orphaned_worker_attempts(work_dir=tmp_path / "wa")

    assert len(remote_calls) == 0
    assert len(consume_calls) == 0
