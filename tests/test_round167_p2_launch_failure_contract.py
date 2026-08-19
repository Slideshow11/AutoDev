"""Round-167/P2: pre-envelope launch-failure artifact MUST validate.

When the worker wrapper aborts BEFORE launching the worker
process (subprocess.Popen raised, malformed inherited
GIT_CONFIG_COUNT, incomplete GIT_CONFIG_KEY/VALUE pair, etc.),
the wrapper writes a canonical ``WORKER_EXECUTION_FAILED``
artifact. The artifact has four independent markers of
"no envelope was observed":

  * ``result_type == "WORKER_EXECUTION_FAILED"``
  * ``extra.worker_result_envelope_seen == False``
  * ``extra.envelope_status == "missing"``
  * ``extra.launch_failure`` is set to a non-empty string

The supervisor's ``WorkerResultArtifact.validate_against_attempt``
MUST accept this artifact without misclassifying it as a
result-contract violation. The supervisor's poll path then maps
the artifact's ``result_type`` to ``LIFECYCLE_WORKER_EXITED_NO_PUSH``
so the attempt terminalizes cleanly WITHOUT consuming source
work and WITHOUT being demoted to ``LIFECYCLE_WORKER_RESULT_INVALID``.

The pre-envelope launch-failure special case is the ONLY
special case. Any artifact that CLAIMS a worker envelope
observation (``worker_result_envelope_seen == True`` or
``envelope_status == "present"``) MUST still be rejected if
its observed/expected result contract ids are missing or
mismatched.

This test exercises the ACTUAL
``WorkerResultArtifact.validate_against_attempt`` validator
end-to-end against a synthetic WorkerAttemptRecord. It does
not use AST or source-string inspection.

These tests MUST FAIL against commit a25fc955 (before the
validator special case is added) and MUST PASS after.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


WRAPPER_PATH = (
    Path(__file__).resolve().parent.parent
    / "autocoder_supervisor" / "aed_worker_wrapper.py"
)


# ---------------------------------------------------------------------------
# Test fixture builders (kept local — do not import from other test files
# because we want this regression isolated and explicit)
# ---------------------------------------------------------------------------

def _build_attempt_record(
    *,
    attempt_id: str = "att-r167-launch-failure",
    result_contract_id: str = "rc-r167-supervisor-prelaunch",
    prelaunch_head: str = "a" * 40,
    expected_branch: str = "feat/review-repair-relay-v1",
    repo_owner: str = "Slideshow11",
    repo_name: str = "AutoDev",
    pr_number: int = 5,
) -> "WorkerAttemptRecord":
    from autocoder_orchestration.worker_attempt import WorkerAttemptRecord
    return WorkerAttemptRecord(
        schema_version="autocoder.worker_attempt.v1",
        attempt_id=attempt_id,
        claim_id=attempt_id,
        repo_owner=repo_owner,
        repo_name=repo_name,
        pr_number=pr_number,
        event_ids=(),
        finding_ids=(),
        directive_digest="deadbeef" * 8,
        directive_path=f"/tmp/{attempt_id}/directive.json",
        prelaunch_head=prelaunch_head,
        expected_branch=expected_branch,
        pid=153759,
        lease_id="lease-r167",
        started_at="2026-08-19T00:00:00Z",
        last_progress_at="2026-08-19T00:00:00Z",
        finished_at="2026-08-19T00:00:00Z",
        lifecycle="WORKER_RUNNING",
        attempt_count=1,
        stdout_path=None,
        stderr_path=None,
        exit_code=None,
        signal=None,
        result_artifact_path=f"/tmp/{attempt_id}.worker_result.json",
        produced_commit_sha=None,
        pushed_commit_sha=None,
        origin_head_verified=False,
        github_head_verified=False,
        terminal_reason=None,
        extra={
            "result_contract_id": result_contract_id,
            "attempt_nonce": attempt_id,
        },
    )


def _build_launch_failure_artifact_dict(
    *,
    result_contract_id: str = "rc-r167-supervisor-prelaunch",
    launch_failure_reason: str = "subprocess.Popen raised OSError",
    envelope_seen: bool = False,
    envelope_status: str = "missing",
    worker_envelope_source: str = "round167_p2_launch_failure",
    attempt_id: str = "att-r167-launch-failure",
) -> dict:
    """Build a launch-failure artifact dict that mirrors the
    wrapper's actual ``_write_launch_failure_artifact`` output.

    This is the exact field set the wrapper writes (after the
    round-167 repair), so the test exercises the real production
    shape of the artifact.
    """
    return {
        "schema_version": "autocoder.worker_result.v1",
        "attempt_id": attempt_id,
        "claim_id": attempt_id,
        "directive_digest": "deadbeef" * 8,
        "result_type": "WORKER_EXECUTION_FAILED",
        "produced_commit_shas": [],
        "pushed_commit_shas": [],
        "completed_at": "2026-08-19T00:00:00Z",
        "no_changes_required_proof": None,
        "tests_run": 0,
        "tests_passed": 0,
        "attempt_nonce": attempt_id.rsplit("-", 1)[0],
        "repo": "Slideshow11/AutoDev",
        "pr_number": 5,
        "expected_branch": "feat/review-repair-relay-v1",
        "prelaunch_head": "a" * 40,
        "worker_pid": 153759,
        "extra": {
            "launch_failure": launch_failure_reason,
            "worker_result_envelope_seen": envelope_seen,
            "worker_envelope_source": worker_envelope_source,
            "envelope_status": envelope_status,
            "envelope_match_count": 0,
            "result_contract_id": result_contract_id,
            "expected_result_contract_id": result_contract_id,
            "observed_result_contract_id": "",
            "result_contract_match": False,
            "result_contract_mismatch_reason": (
                "worker did not launch; no envelope was produced"
            ),
        },
    }


def _run_wrapper_launch_failure(
    tmp_path: Path,
    *,
    result_contract_id: str,
) -> dict:
    """Invoke the actual wrapper with an environment that forces
    the wrapper's pre-launch GIT_CONFIG_COUNT validation to
    reject the launch (and so emit the canonical
    ``WORKER_EXECUTION_FAILED`` artifact). Then return the
    parsed artifact dict.

    This exercises the actual production code path:
        ``autocoder_supervisor.aed_worker_wrapper._write_launch_failure_artifact``
    end-to-end, exactly the same way the supervisor will see it.
    """
    artifact_path = (
        tmp_path / f"r167-att-launch-failure-{os.getpid()}.worker_result.json"
    )
    stdout_log_path = tmp_path / f"r167-att-launch-failure-{os.getpid()}.stdout.log"
    # Pass --worker-hooks-path so the wrapper enters the
    # GIT_CONFIG_COUNT validation branch (otherwise it skips
    # the validation entirely and just runs the worker
    # normally, writing a success-path artifact). Use the
    # repo's actual autocoder_worker_hooks directory because
    # the wrapper refuses non-absolute or non-existent paths.
    repo_root = Path(__file__).resolve().parent.parent
    worker_hooks_dir = repo_root / "autocoder_worker_hooks"
    cmd = [
        sys.executable, str(WRAPPER_PATH),
        "--attempt-id", "r167-att-launch-failure",
        "--directive-digest", "deadbeef" * 8,
        "--directive-id", "r167-test-uuid",
        "--prelaunch-head", "a" * 40,
        "--result-artifact-path", str(artifact_path),
        "--stdout-log-path", str(stdout_log_path),
        "--repo", "Slideshow11/AutoDev",
        "--pr-number", "5",
        "--result-contract-id", result_contract_id,
        "--worker-hooks-path", str(worker_hooks_dir),
        # Negative GIT_CONFIG_COUNT triggers the wrapper's
        # pre-launch validation that emits
        # _write_launch_failure_artifact (autocoder_supervisor/
        # aed_worker_wrapper.py:418-432).
        "--",
        "/bin/true",
    ]
    env = dict(os.environ)
    env["GIT_CONFIG_COUNT"] = "-1"
    proc = subprocess.run(
        cmd, capture_output=True, text=True, env=env,
    )
    # The wrapper exits 127 on the launch-failure path.
    assert proc.returncode == 127, (
        f"wrapper must exit 127 on launch failure; got {proc.returncode}\n"
        f"stderr={proc.stderr}\nstdout={proc.stdout}"
    )
    return json.loads(artifact_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_validator_accepts_pre_envelope_launch_failure_artifact():
    """The supervisor-side validator MUST accept a launch-failure
    artifact without producing result-contract validation errors.

    Pre-condition: the artifact's ``extra`` block carries all four
    "no envelope observed" markers
    (``result_type == WORKER_EXECUTION_FAILED``,
    ``worker_result_envelope_seen == False``,
    ``envelope_status == "missing"``,
    ``launch_failure`` is a non-empty string).

    Pre-condition: the artifact's observed/expected contract ids
    are the round-167 repair shape (expected == supervisor-owned
    prelaunch, observed == ""). The validator MUST NOT reject
    this as a contract violation.

    This is the exact defect the previous round-167 repair
    attempted to fix but did not: ``validate_against_attempt``
    in worker_attempt.py still required
    ``observed_result_contract_id`` to be non-empty, so the
    launch-failure artifact was always classified as
    ``WORKER_RESULT_INVALID`` by the supervisor's
    ``_round50_ingest_worker_result_artifact`` path.
    """
    from autocoder_orchestration.worker_attempt import (
        WorkerResultArtifact,
    )
    rec = _build_attempt_record(
        result_contract_id="rc-r167-supervisor-prelaunch",
    )
    artifact_dict = _build_launch_failure_artifact_dict(
        result_contract_id="rc-r167-supervisor-prelaunch",
        launch_failure_reason="subprocess.Popen raised OSError",
    )
    art = WorkerResultArtifact.from_dict(artifact_dict)
    errs = art.validate_against_attempt(rec)
    # Assert no result-contract validation errors.
    joined = " ".join(errs).lower()
    assert "contract" not in joined or "missing required contract fields" not in joined, (
        f"launch-failure artifact must NOT produce result-contract "
        f"validation errors; got: {errs}"
    )
    assert "does not match supervisor-owned prelaunch" not in joined, (
        f"launch-failure artifact's expected contract id matches "
        f"the supervisor prelaunch id; validator must not flag it. "
        f"errors={errs}"
    )
    # The artifact's result_type remains WORKER_EXECUTION_FAILED —
    # the validator MUST NOT mutate it (the caller in the supervisor
    # ingest path uses the result_type to drive lifecycle
    # classification: WORKER_EXECUTION_FAILED → WORKER_EXITED_NO_PUSH,
    # not WORKER_RESULT_INVALID).
    assert art.result_type == "WORKER_EXECUTION_FAILED", (
        f"artifact result_type must remain WORKER_EXECUTION_FAILED; "
        f"got {art.result_type!r}"
    )


def test_production_wrapper_artifact_validates_cleanly():
    """End-to-end: invoke the actual wrapper with an environment
    that forces the wrapper's pre-launch GIT_CONFIG_COUNT
    rejection. The artifact written by the wrapper MUST validate
    cleanly against a supervisor-owned ``WorkerAttemptRecord``.

    This proves the production ``_write_launch_failure_artifact``
    function (autocoder_supervisor/aed_worker_wrapper.py) emits
    an artifact that the supervisor-side validator accepts.
    """
    import tempfile
    from autocoder_orchestration.worker_attempt import (
        WorkerResultArtifact,
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        artifact_dict = _run_wrapper_launch_failure(
            tmp_path,
            result_contract_id="rc-r167-supervisor-prelaunch",
        )
        # Sanity-check the artifact shape mirrors what the wrapper
        # is supposed to write after the round-167 repair.
        assert artifact_dict["result_type"] == "WORKER_EXECUTION_FAILED"
        extra = artifact_dict["extra"]
        assert extra["envelope_status"] == "missing"
        assert extra["worker_result_envelope_seen"] is False
        assert extra["launch_failure"]
        assert extra["expected_result_contract_id"] == "rc-r167-supervisor-prelaunch"
        assert extra["observed_result_contract_id"] == ""
        assert extra["result_contract_match"] is False

        art = WorkerResultArtifact.from_dict(artifact_dict)
        rec = _build_attempt_record(
            result_contract_id="rc-r167-supervisor-prelaunch",
            attempt_id=artifact_dict["attempt_id"],
        )
        errs = art.validate_against_attempt(rec)
        assert errs == [], (
            f"wrapper-produced launch-failure artifact MUST validate "
            f"cleanly against supervisor-owned attempt record; "
            f"errors={errs}"
        )


def test_validator_rejects_envelope_claiming_missing_contract_ids():
    """The special case MUST NOT extend to artifacts that CLAIM
    a worker envelope observation. If
    ``worker_result_envelope_seen == True`` (or
    ``envelope_status != "missing"``) but the contract ids are
    missing, the validator MUST still fail closed.

    This proves the special case is bounded by the four
    pre-envelope markers.
    """
    from autocoder_orchestration.worker_attempt import (
        WorkerResultArtifact,
    )
    rec = _build_attempt_record(
        result_contract_id="rc-r167-supervisor-prelaunch",
    )
    artifact_dict = _build_launch_failure_artifact_dict(
        result_contract_id="rc-r167-supervisor-prelaunch",
        launch_failure_reason="subprocess.Popen raised OSError",
        # Artifact claims it SAW an envelope — so the validator
        # MUST demand the contract fields be present.
        envelope_seen=True,
        envelope_status="present",
        worker_envelope_source="round51_c19_wrapper",
    )
    art = WorkerResultArtifact.from_dict(artifact_dict)
    errs = art.validate_against_attempt(rec)
    joined = " ".join(errs).lower()
    assert "missing required contract fields" in joined, (
        f"validator must still reject envelope-claiming artifact "
        f"with missing contract ids; errors={errs}"
    )


def test_validator_rejects_envelope_with_mismatched_contract_id():
    """The special case MUST NOT extend to artifacts that have
    a present envelope with mismatched contract ids. A worker
    that emits an envelope with a wrong contract id MUST still
    fail closed.
    """
    from autocoder_orchestration.worker_attempt import (
        WorkerResultArtifact,
    )
    rec = _build_attempt_record(
        result_contract_id="rc-r167-supervisor-prelaunch",
    )
    artifact_dict = _build_launch_failure_artifact_dict(
        result_contract_id="rc-r167-supervisor-prelaunch",
        launch_failure_reason="subprocess.Popen raised OSError",
        envelope_seen=True,
        envelope_status="present",
        worker_envelope_source="round51_c19_wrapper",
    )
    # Inject contract IDs that disagree with the supervisor prelaunch.
    artifact_dict["extra"]["expected_result_contract_id"] = (
        "rc-WORKER-LYING"
    )
    artifact_dict["extra"]["observed_result_contract_id"] = (
        "rc-WORKER-LYING"
    )
    artifact_dict["extra"]["result_contract_match"] = True
    art = WorkerResultArtifact.from_dict(artifact_dict)
    errs = art.validate_against_attempt(rec)
    joined = " ".join(errs).lower()
    assert "does not match supervisor-owned" in joined, (
        f"validator must reject mismatched contract id even when "
        f"envelope_status==present; errors={errs}"
    )


# ---------------------------------------------------------------------------
# Round-167/P2: push-gate worktree path uniqueness
# ---------------------------------------------------------------------------
def test_push_gate_wt_dir_two_invocations_in_same_process_are_distinct():
    """Two ``_push_gate_wt_dir`` invocations in the SAME
    process for the SAME outgoing SHA MUST produce distinct
    paths. This proves the per-invocation discriminator is
    invocation-unique (not machine-stable like
    ``os.getpid() + uuid.getnode()`` was).

    The bug being guarded against: the worker pre-push hook
    and the supervisor post-push validator both run against
    the same outgoing SHA. If their worktree paths collide,
    the second invocation's ``worktree remove --force``
    deletes the worktree the first invocation is still
    scanning.
    """
    from autocoder_supervisor.push_gate import _push_gate_wt_dir
    repo_root = Path(__file__).resolve().parent.parent
    outgoing_head = "a" * 40
    paths = {
        _push_gate_wt_dir(repo_root, outgoing_head).as_posix()
        for _ in range(10)
    }
    assert len(paths) == 10, (
        f"two same-process invocations on the same outgoing SHA "
        f"must produce DISTINCT worktree paths; got {len(paths)} "
        f"unique paths from 10 calls: {paths}"
    )


def test_push_gate_wt_dir_uses_outgoing_head_prefix():
    """The worktree path MUST include the first 12 hex chars
    of the outgoing head (this is the round-1064-P2 key)."""
    from autocoder_supervisor.push_gate import _push_gate_wt_dir
    repo_root = Path(__file__).resolve().parent.parent
    outgoing_head = "abcdef1234567890" + "0" * 24
    p = _push_gate_wt_dir(repo_root, outgoing_head)
    assert "abcdef123456" in p.name, (
        f"worktree path must embed the first 12 hex chars of "
        f"the outgoing head; got {p}"
    )
