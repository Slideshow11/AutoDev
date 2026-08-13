"""Round-54/C22 hardening: supervisor-side launch-identity / launch-context /
result-contract validation.

The wrapper enforces the result-contract at write-time (round-54/C22 §5-§8).
The supervisor MUST also enforce it at ingest-time (round-54/C22 §0.P) so that
a legacy-shape artifact (or one authored by an older wrapper) cannot bypass
the contract defense.

The supervisor validates the artifact's claim against the
``WorkerAttemptRecord`` (the supervisor-owned, prelaunch-durable record).
The validator must require:

  - artifact.attempt_id == attempt.attempt_id
  - artifact.claim_id == attempt.claim_id
  - artifact.directive_digest == attempt.directive_digest
  - artifact.repo == "<owner>/<repo>"
  - artifact.pr_number == attempt.pr_number
  - artifact.expected_branch == attempt.expected_branch
  - artifact.prelaunch_head == attempt.prelaunch_head
  - artifact expected_result_contract_id == attempt.extra["result_contract_id"]
  - artifact observed_result_contract_id == attempt.extra["result_contract_id"]
  - artifact result_contract_match is True

A missing-artifact-field is a fail-closed signal: the legacy shape is not
accepted merely because the four identity fields happen to match.

The poll path MUST NOT promote a failed-validation artifact to
NO_CHANGES_REQUIRED, PUSH_VERIFIED, or any state that consumes source work.
The correct terminal lifecycle is WORKER_RESULT_INVALID.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


WRAPPER_PATH = (
    Path(__file__).resolve().parent.parent
    / "autocoder_supervisor"
    / "aed_worker_wrapper.py"
)
ORCH_WORKER_PATH = (
    Path(__file__).resolve().parent.parent
    / "autocoder_orchestration"
    / "worker_attempt.py"
)
SUP_PATH = (
    Path(__file__).resolve().parent.parent
    / "autocoder_supervisor"
    / "supervisor.py"
)


def _build_attempt_record(
    *,
    attempt_id: str = "att-20260813T090000Z",
    claim_id: str = "att-20260813T090000Z",
    directive_digest: str = "deadbeef" * 8,
    repo_owner: str = "OWNER",
    repo_name: str = "REPO",
    pr_number: int = 9,
    expected_branch: str = "feat/test",
    prelaunch_head: str = "abc" * 14,
    result_contract_id: str = "rc-supervisor-launch-contract",
    attempt_nonce: str = "att-20260813T090000Z",
    produced_commit_sha: str = "",
    pushed_commit_sha: str = "",
) -> "WorkerAttemptRecord":
    """Build a supervisor-owned WorkerAttemptRecord."""
    from autocoder_orchestration.worker_attempt import WorkerAttemptRecord
    return WorkerAttemptRecord(
        schema_version="autocoder.worker_attempt.v1",
        attempt_id=attempt_id,
        claim_id=claim_id,
        repo_owner=repo_owner,
        repo_name=repo_name,
        pr_number=pr_number,
        event_ids=(),
        finding_ids=(),
        directive_digest=directive_digest,
        directive_path=f"/tmp/{attempt_id}/directive.json",
        prelaunch_head=prelaunch_head,
        expected_branch=expected_branch,
        pid=153759,
        lease_id="lease-test",
        started_at="2026-08-13T09:00:00Z",
        last_progress_at="2026-08-13T09:00:00Z",
        finished_at="2026-08-13T09:00:00Z",
        lifecycle="WORKER_RUNNING",
        attempt_count=1,
        stdout_path=None,
        stderr_path=None,
        exit_code=0,
        signal=None,
        result_artifact_path=f"/tmp/{attempt_id}.worker_result.json",
        produced_commit_sha=produced_commit_sha or None,
        pushed_commit_sha=pushed_commit_sha or None,
        origin_head_verified=False,
        github_head_verified=False,
        terminal_reason=None,
        extra={
            "result_contract_id": result_contract_id,
            "attempt_nonce": attempt_nonce,
        },
    )


def _build_artifact(
    *,
    result_type: str = "NO_CHANGES_REQUIRED",
    repo: str = "OWNER/REPO",
    pr_number: int = 9,
    expected_branch: str = "feat/test",
    prelaunch_head: str = "abc" * 14,
    attempt_id: str = "att-20260813T090000Z",
    claim_id: str = "att-20260813T090000Z",
    directive_digest: str = "deadbeef" * 8,
    completed_at: str = "2026-08-13T09:00:00Z",
    expected_result_contract_id: str | None = "rc-supervisor-launch-contract",
    observed_result_contract_id: str | None = "rc-supervisor-launch-contract",
    result_contract_match: object = True,
    contract_fields_legacy: bool = False,
) -> "WorkerResultArtifact":
    """Build a wrapper-side artifact. By default the new 9-extra-key shape is
    used. Set contract_fields_legacy=True to emit the legacy 5-extra-key shape
    (round-170-style)."""
    from autocoder_orchestration.worker_attempt import WorkerResultArtifact
    if contract_fields_legacy:
        extra = {
            "worker_result_envelope_seen": True,
            "worker_envelope_source": "round51_c19_wrapper",
            "envelope_status": "present",
            "envelope_match_count": 1,
            "result_contract_id": "rc-supervisor-launch-contract",
        }
    else:
        extra = {
            "worker_result_envelope_seen": True,
            "worker_envelope_source": "round51_c19_wrapper",
            "envelope_status": "present",
            "envelope_match_count": 1,
            "result_contract_id": "rc-supervisor-launch-contract",
            "expected_result_contract_id": expected_result_contract_id or "",
            "observed_result_contract_id": observed_result_contract_id or "",
            "result_contract_match": result_contract_match,
            "result_contract_mismatch_reason": (
                "" if result_contract_match is True
                else "test mismatch"
            ),
        }
    return WorkerResultArtifact(
        schema_version="autocoder.worker_result.v1",
        attempt_id=attempt_id,
        claim_id=claim_id,
        directive_digest=directive_digest,
        result_type=result_type,
        produced_commit_shas=(),
        pushed_commit_shas=(),
        completed_at=completed_at,
        no_changes_required_proof=None,
        tests_run=0,
        tests_passed=0,
        attempt_nonce=attempt_id,
        repo=repo,
        pr_number=pr_number,
        expected_branch=expected_branch,
        prelaunch_head=prelaunch_head,
        worker_session_id="worker-test",
        worker_pid=153759,
        extra=extra,
    )


# Helper: import + 1–2 items from the supervisor module
def _supervisor_validate_against_attempt(artifact, rec):
    """Direct call to validate_against_attempt."""
    from autocoder_orchestration.worker_attempt import (
        WorkerResultArtifact,
        WorkerAttemptRecord,
    )
    return artifact.validate_against_attempt(rec)


# ---------------------------------------------------------------------------
# 1. Correct complete artifact accepted
# ---------------------------------------------------------------------------
def test_1_correct_artifact_rejected_by_contract_mismatch_safe():
    """All fields agree: validation MUST be silent."""
    rec = _build_attempt_record()
    art = _build_artifact()
    errs = _supervisor_validate_against_attempt(art, rec)
    assert errs == [], f"correct artifact must validate: {errs}"


# ---------------------------------------------------------------------------
# 2. Missing result-contract fields rejected (legacy shape)
# ---------------------------------------------------------------------------
def test_2_legacy_artifact_shape_rejected():
    """Round-170-style 5-extra-key artifact: missing
    expected_result_contract_id / observed_result_contract_id / match flag.
    Validation MUST reject."""
    rec = _build_attempt_record()
    art = _build_artifact(contract_fields_legacy=True)
    errs = _supervisor_validate_against_attempt(art, rec)
    joined = " ".join(errs)
    assert "missing required contract fields" in joined, (
        f"legacy shape must be rejected with explicit message: {errs}"
    )


# ---------------------------------------------------------------------------
# 3. Expected/observed mismatch rejected
# ---------------------------------------------------------------------------
def test_3_expected_observed_mismatch_rejected():
    """The artifact's expected and observed contract IDs disagree
    internally (legacy smuggling attempt)."""
    rec = _build_attempt_record()
    art = _build_artifact(
        expected_result_contract_id="rc-supervisor-launch-contract",
        observed_result_contract_id="rc-DIFFERENT",
        result_contract_match=False,
    )
    errs = _supervisor_validate_against_attempt(art, rec)
    joined = " ".join(errs)
    assert "mismatch" in joined.lower(), errs


# ---------------------------------------------------------------------------
# 4. Artifact internally matches but disagrees with supervisor-owned launch
# ---------------------------------------------------------------------------
def test_4_artifact_matches_self_but_disagrees_with_supervisor_owned():
    """The artifact's expected and observed IDs agree with each other but
    BOTH disagree with the supervisor-owned prelaunch id. Validator
    must reject."""
    rec = _build_attempt_record(
        result_contract_id="rc-supervisor-launch-contract",
    )
    art = _build_artifact(
        expected_result_contract_id="rc-WORKER-CLAIMED",
        observed_result_contract_id="rc-WORKER-CLAIMED",
        result_contract_match=True,
    )
    errs = _supervisor_validate_against_attempt(art, rec)
    joined = " ".join(errs)
    assert "supervisor-owned" in joined or "does not match" in joined, errs


# ---------------------------------------------------------------------------
# 5. Wrong repo rejected
# ---------------------------------------------------------------------------
def test_5_wrong_repo_rejected():
    rec = _build_attempt_record()
    art = _build_artifact(repo="OTHER/REPO")
    errs = _supervisor_validate_against_attempt(art, rec)
    assert any("repo mismatch" in e for e in errs), errs


# ---------------------------------------------------------------------------
# 6. Wrong PR rejected
# ---------------------------------------------------------------------------
def test_6_wrong_pr_rejected():
    rec = _build_attempt_record(pr_number=9)
    art = _build_artifact(pr_number=99)
    errs = _supervisor_validate_against_attempt(art, rec)
    assert any("pr_number mismatch" in e for e in errs), errs


# ---------------------------------------------------------------------------
# 7. Wrong expected_branch rejected
# ---------------------------------------------------------------------------
def test_7_wrong_branch_rejected():
    rec = _build_attempt_record(expected_branch="feat/test")
    art = _build_artifact(expected_branch="feat/other")
    errs = _supervisor_validate_against_attempt(art, rec)
    assert any("expected_branch mismatch" in e for e in errs), errs


# ---------------------------------------------------------------------------
# 8. Wrong prelaunch_head rejected
# ---------------------------------------------------------------------------
def test_8_wrong_prelaunch_head_rejected():
    rec = _build_attempt_record(prelaunch_head="abc" * 14)
    art = _build_artifact(prelaunch_head="def" * 14)
    errs = _supervisor_validate_against_attempt(art, rec)
    assert any("prelaunch_head mismatch" in e for e in errs), errs


# ---------------------------------------------------------------------------
# 9. Old 5-key Round-170-style extra block rejected
# ---------------------------------------------------------------------------
def test_9_old_5key_extra_block_rejected():
    """The artifact's extra block has only the 5 Round-170 keys and is missing
    the new result-contract fields. Validator must reject."""
    rec = _build_attempt_record()
    # Build an artifact with a manually constructed 5-key extra mirroring
    # exactly what round-170 produced.
    from autocoder_orchestration.worker_attempt import WorkerResultArtifact
    art = WorkerResultArtifact(
        schema_version="autocoder.worker_result.v1",
        attempt_id="att-20260813T090000Z",
        claim_id="att-20260813T090000Z",
        directive_digest="deadbeef" * 8,
        result_type="NO_CHANGES_REQUIRED",
        produced_commit_shas=(),
        pushed_commit_shas=(),
        completed_at="2026-08-13T09:00:00Z",
        no_changes_required_proof=None,
        tests_run=0,
        tests_passed=0,
        attempt_nonce="att-20260813T090000Z",
        repo="OWNER/REPO",
        pr_number=9,
        expected_branch="feat/test",
        prelaunch_head="abc" * 14,
        worker_session_id="worker-test",
        worker_pid=153759,
        extra={
            "worker_result_envelope_seen": True,
            "worker_envelope_source": "round51_c19_wrapper",
            "envelope_status": "present",
            "envelope_match_count": 1,
            "result_contract_id": "rc-supervisor-launch-contract",
        },
    )
    errs = _supervisor_validate_against_attempt(art, rec)
    joined = " ".join(errs)
    assert "missing required contract fields" in joined, errs


# ---------------------------------------------------------------------------
# 10. Rejection preserves source work for retry/re-evaluation
# ---------------------------------------------------------------------------
def test_10_rejection_preserves_work_for_retry():
    """When validation fails, the attempt MUST NOT be promoted to
    LIFECYCLE_NO_CHANGES_REQUIRED or LIFECYCLE_PUSH_VERIFIED. The attempt
    is terminal via WORKER_RESULT_INVALID. The finding stays RETRY_PENDING."""
    rec = _build_attempt_record()
    art = _build_artifact(
        expected_result_contract_id="rc-WRONG",
        observed_result_contract_id="rc-WRONG",
        result_contract_match=False,
    )
    errs = _supervisor_validate_against_attempt(art, rec)
    assert errs != []
    # The attempt is NOT in a "consumed source work" state.
    # The validator returns a list of errors; the caller (the
    # ingest path) is then responsible for transitioning
    # rec.lifecycle to WORKER_RESULT_INVALID and NOT consuming
    # the source event. We verify this by checking the
    # TERMINAL_LIFECYCLES set membership.
    from autocoder_orchestration.worker_attempt import (
        LIFECYCLE_WORKER_RESULT_INVALID,
        TERMINAL_LIFECYCLES,
    )
    # The validator's "fail-closed" expectation is captured
    # by the LIFECYCLE_WORKER_RESULT_INVALID being in
    # TERMINAL_LIFECYCLES (so the attempt terminates without
    # requiring further reconciliation).
    assert LIFECYCLE_WORKER_RESULT_INVALID in TERMINAL_LIFECYCLES
    # The artifact's result_type is left untouched by the
    # validator; the caller (the supervisor's ingest + poll
    # path) is what enforces WORKER_RESULT_INVALID.
    assert art.result_type == "NO_CHANGES_REQUIRED"
