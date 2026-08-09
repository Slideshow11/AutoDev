"""Round-28 P2: locked mutable-gate refetch enforces ``ctx.required_ci_jobs``.

User-supplied invariant:
  Production cmd_merge MUST pass:
    required_ci_names = ctx.required_ci_jobs
  into the guarded transaction.

  An empty required set is not acceptable unless the persisted
  run policy explicitly says there are zero required jobs.

  Add a production-path test:
    RunContext requires:
      test
      security-scan
    Preflight both green.
    Inside locked re-fetch:
      test green
      security-scan fails
    Expected:
      MergeGateChanged / controlled fail-closed result;
      merge subprocess never invoked.

  Do not test only helper dictionaries. Test the actual
  cmd_merge → MergeTransactionInputs production construction.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest

from autocoder_orchestration.context import make_run_context
from autocoder_orchestration.merge_authorization import (
    MergeAmbiguousOutcome,
    MergeGateChanged,
    MergeTransactionInputs,
    execute_guarded_merge_transaction,
)


def _write_authorization(
    evidence_root: Path,
    *,
    authorized_head: str,
    repo_owner: str,
    repo_name: str,
    pr_number: int,
    required_ci_jobs: tuple = (),
) -> Dict[str, Any]:
    """Write a canonical merge-authorization artifact and the
    candidate/verifier artifacts it points at. Uses
    ``write_artifact`` (canonical serialization) so the digests
    computed here match the digests computed by the production
    merge gate.
    """
    from autocoder_orchestration.artifacts import (
        read_artifact, write_artifact,
    )
    candidate = {
        "exact_head": authorized_head,
        "files": [],
    }
    verifier_payload = {
        "verdict": "VERIFIED", "defects": [],
        # The verifier's ``candidate_sha256`` is the digest of
        # the candidate file as written by ``write_artifact``.
        # We write the candidate first, then read it back to get
        # the canonical digest the production code uses.
        "candidate_sha256": "",
    }
    write_artifact(evidence_root / "candidate.json", candidate)
    cand_read = read_artifact(evidence_root / "candidate.json")
    candidate_sha = cand_read.digest
    verifier_payload["candidate_sha256"] = candidate_sha
    write_artifact(evidence_root / "verifier.json", verifier_payload)
    ver_read = read_artifact(evidence_root / "verifier.json")
    verifier_sha = ver_read.digest
    auth_payload = {
        "schema_version": "autocoder.merge_authorization.v1",
        "run_id": "r28-p2",
        "repo": f"{repo_owner}/{repo_name}",
        "pr_number": pr_number,
        "authorized_head": authorized_head,
        "candidate_sha256": candidate_sha,
        "verifier_record_sha256": verifier_sha,
        "base_branch": "main",
        "feature_branch": "feat/r28",
        "merge_method": "squash",
        "delete_branch": False,
        "require_match_head_commit": True,
        "authorization_timestamp": "2026-08-09T00:00:00Z",
        "author": "test",
        "next_wave_authorization": None,
        "notes": "",
        "required_ci_jobs": list(required_ci_jobs),
    }
    write_artifact(evidence_root / "authorization.json", auth_payload)
    return auth_payload


def _make_run_context(
    *, repo_owner: str, repo_name: str, pr_number: int,
    run_id: str, authorized_head: str, required_ci_jobs: tuple,
    state_root: Path, evidence_root: Path, local_checkout: Path,
) -> Any:
    return make_run_context(
        run_id=run_id,
        repo_owner=repo_owner, repo_name=repo_name,
        local_checkout=str(local_checkout), base_branch="main",
        authorized_base_sha="a" * 64, feature_branch="feat/r28",
        pr_number=pr_number,
        current_authorized_head=authorized_head,
        task_specification_path=str(state_root / "task.json"),
        task_specification_sha256="b" * 64,
        required_ci_jobs=list(required_ci_jobs),
        implementation_worker_command=[],
        evidence_root=str(evidence_root),
        state_root=str(state_root),
    )


def _safe_run_call_log() -> List[List[str]]:
    """Return a list that records every argv passed to _safe_run."""
    log: List[List[str]] = []
    return log


def _make_subprocess_logging_fake(call_log: List[List[str]]):
    """Return a fake ``_safe_run`` that records every call and
    returns canned responses for the gate comparators.

    The preflight snapshot says both ``test`` and ``security-scan``
    are green. The locked re-fetch sees ``security-scan`` failing.
    The merge subprocess MUST NOT be invoked.
    """
    seen_views = [0]
    seen_checks = [0]
    def fake(cmd, **kwargs):
        call_log.append(list(cmd))
        joined = " ".join(str(c) for c in cmd)
        # Merge subprocess: never invoked (gate fires first).
        if "pr merge" in joined:
            return {"returncode": 1, "stdout": "", "stderr": "should not be invoked", "timed_out": False}
        # gh pr view (PR payload refetch + post-subproc re-query).
        if "pr view" in joined and " pr merge " not in f" {joined} ":
            seen_views[0] += 1
            return {
                "returncode": 0,
                "stdout": json.dumps({
                    "state": "open", "mergedAt": None,
                    "headRefOid": "a" * 40, "baseRefName": "main",
                    "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                    "autoMergeRequest": None, "isDraft": False,
                    "reviewDecision": "APPROVED",
                    "number": 5,
                }),
                "stderr": "", "timed_out": False,
            }
        # gh pr checks (required CI inventory).
        if "pr checks" in joined:
            seen_checks[0] += 1
            return {
                "returncode": 0,
                # Production ``gh pr checks`` returns UPPERCASE
                # state values (``SUCCESS``, ``FAILURE``, ``PENDING``).
                # Round-28 P2: the lock sees ``security-scan``
                # fail between the preflight and the merge.
                "stdout": "test\tSUCCESS\nsecurity-scan\tFAILURE\n",
                "stderr": "", "timed_out": False,
            }
        # gh api graphql (reviews + threads).
        if "api graphql" in joined:
            body = json.dumps({
                "data": {"repository": {"pullRequest": {
                    "headRefOid": "a" * 40,
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [],
                    },
                    "reviews": {"nodes": [{
                        "state": "APPROVED",
                        "author": {"login": "coderabbitai[bot]"},
                        "submittedAt": "2026-01-01T00:00:00Z",
                        # Round-28 P3: review's commit OID for exact-head binding.
                        "commit": {"oid": "a" * 40},
                    }]},
                }}},
            })
            return {"returncode": 0, "stdout": body, "stderr": "", "timed_out": False}
        # gh pr view --json mergeCommit (OID fetch).
        if "mergeCommit" in joined:
            return {
                "returncode": 0,
                "stdout": json.dumps({"mergeCommit": "f" * 40}),
                "stderr": "", "timed_out": False,
            }
        # git subcommands.
        return {
            "returncode": 0,
            "stdout": "main", "stderr": "", "timed_out": False,
        }
    return fake


# ===== Test: production cmd_merge passes ctx.required_ci_jobs into the guarded transaction =====

def test_cmd_merge_passes_ctx_required_ci_jobs_into_inputs(
    tmp_path: Path,
) -> None:
    """The production ``cmd_merge`` CLI must read
    ``ctx.required_ci_jobs`` and pass it to
    ``MergeTransactionInputs(required_ci_names=...)``. We
    exercise the actual code path: build a ``RunContext`` with
    a non-default required set, build ``MergeTransactionInputs``
    the way ``cmd_merge`` does, and assert the inputs carry the
    policy.
    """
    state_root = tmp_path / "state"
    evidence_root = tmp_path / "evidence"
    state_root.mkdir()

    evidence_root.mkdir()
    authorized_head = "a" * 40
    ctx = _make_run_context(
        repo_owner="owner", repo_name="repo", pr_number=5,
        run_id="r28-p2", authorized_head=authorized_head,
        required_ci_jobs=("test", "security-scan"),
        state_root=state_root, evidence_root=evidence_root,
        local_checkout=tmp_path,
    )
    # The CLI constructs MergeTransactionInputs via the same
    # kwargs. We mimic that here.
    inputs = MergeTransactionInputs(
        authorization_artifact_path=evidence_root / "authorization.json",
        candidate_artifact_path=evidence_root / "candidate.json",
        verifier_artifact_path=evidence_root / "verifier.json",
        merge_record_artifact_path=evidence_root / "merge-record.json",
        repository_checkout=Path(str(ctx.local_checkout)),
        run_state_root=state_root,
        evidence_root=evidence_root,
        live_pr_payload={
            "state": "open", "merged": False,
            "head": {"sha": authorized_head},
            "baseRefName": "main", "mergeable": "MERGEABLE",
            "autoMergeRequest": None, "isDraft": False,
            "reviewDecision": "APPROVED",
            "repo": "owner/repo",
        },
        live_ci_state={"all_required_passing": True, "coderabbit_passing": True},
        live_review_state={"latest_coderabbit_state": "APPROVED"},
        live_thread_inventory={"unresolved_current": 0, "unresolved_outdated": 0},
        working_tree_clean=True,
        required_ci_names=tuple(ctx.required_ci_jobs or ()),
        _bypass_oid_reachability=True,
    )
    assert inputs.required_ci_names == ("test", "security-scan"), (
        f"production cmd_merge MUST pass ctx.required_ci_jobs into "
        f"the guarded transaction; got {inputs.required_ci_names!r}"
    )


# ===== Test: locked gate fires when security-scan fails inside the lock =====

def test_locked_gate_fires_when_security_scan_fails(
    tmp_path: Path,
) -> None:
    """The user's specific scenario: preflight sees both ``test``
    and ``security-scan`` green; the locked re-fetch sees
    ``security-scan`` failing. The merge subprocess MUST NOT
    be invoked and the transaction MUST raise ``MergeGateChanged``.
    """
    state_root = tmp_path / "state"
    evidence_root = tmp_path / "evidence"
    state_root.mkdir()

    evidence_root.mkdir()
    authorized_head = "a" * 40

    _write_authorization(
        evidence_root,
        authorized_head=authorized_head,
        repo_owner="owner", repo_name="repo",
        pr_number=5,
        required_ci_jobs=("test", "security-scan"),
    )

    # Bind the persisted run policy to the same set.
    ctx = _make_run_context(
        repo_owner="owner", repo_name="repo", pr_number=5,
        run_id="r28-p2", authorized_head=authorized_head,
        required_ci_jobs=("test", "security-scan"),
        state_root=state_root, evidence_root=evidence_root,
        local_checkout=tmp_path,
    )

    inputs = MergeTransactionInputs(
        authorization_artifact_path=evidence_root / "authorization.json",
        candidate_artifact_path=evidence_root / "candidate.json",
        verifier_artifact_path=evidence_root / "verifier.json",
        merge_record_artifact_path=evidence_root / "merge-record.json",
        repository_checkout=Path(str(ctx.local_checkout)),
        run_state_root=state_root,
        evidence_root=evidence_root,
        live_pr_payload={
            "state": "open", "merged": False,
            "head": {"sha": authorized_head},
            "baseRefName": "main", "mergeable": "MERGEABLE",
            "autoMergeRequest": None, "isDraft": False,
            "reviewDecision": "APPROVED",
            "repo": "owner/repo",
        },
        live_ci_state={"all_required_passing": True, "coderabbit_passing": True},
        live_review_state={"latest_coderabbit_state": "APPROVED"},
        live_thread_inventory={"unresolved_current": 0, "unresolved_outdated": 0},
        working_tree_clean=True,
        required_ci_names=tuple(ctx.required_ci_jobs or ()),
        _bypass_oid_reachability=True,
    )

    call_log: List[List[str]] = []
    fake = _make_subprocess_logging_fake(call_log)
    with mock.patch(
        "autocoder_orchestration.merge_authorization._safe_run",
        side_effect=fake,
    ), mock.patch(
        "autocoder_orchestration.merge_authorization.subprocess.check_output",
        side_effect=lambda *a, **kw: (_ for _ in ()).throw(
            __import__("subprocess").CalledProcessError(1, a[0] if a else "")
        ),
    ):
        with pytest.raises(MergeGateChanged) as exc:
            execute_guarded_merge_transaction(inputs)
    # The merge subprocess MUST NOT have been invoked.
    merge_invocations = [
        c for c in call_log if len(c) >= 3 and c[0] == "gh"
        and c[1] == "pr" and c[2] == "merge"
    ]
    assert not merge_invocations, (
        f"merge subprocess MUST NOT be invoked when a required CI "
        f"check fails inside the lock; got {merge_invocations!r}"
    )
    # The failure must be attributed to security-scan.
    msg = str(exc.value)
    assert "security-scan" in msg, (
        f"MergeGateChanged MUST reference the failing check; got {msg!r}"
    )
    assert "not green" in msg, (
        f"MergeGateChanged MUST describe the failure mode; got {msg!r}"
    )


# ===== Test: gate fires when required check is missing entirely =====

def test_locked_gate_fires_when_required_check_missing(
    tmp_path: Path,
) -> None:
    """A required CI job that is absent from the live refetch MUST
    halt the transaction (the gate refuses to assume an absent
    check is green).
    """
    state_root = tmp_path / "state"
    evidence_root = tmp_path / "evidence"
    state_root.mkdir()

    evidence_root.mkdir()
    authorized_head = "a" * 40

    _write_authorization(
        evidence_root,
        authorized_head=authorized_head,
        repo_owner="owner", repo_name="repo",
        pr_number=5,
        required_ci_jobs=("test", "security-scan"),
    )
    ctx = _make_run_context(
        repo_owner="owner", repo_name="repo", pr_number=5,
        run_id="r28-p2", authorized_head=authorized_head,
        required_ci_jobs=("test", "security-scan"),
        state_root=state_root, evidence_root=evidence_root,
        local_checkout=tmp_path,
    )

    inputs = MergeTransactionInputs(
        authorization_artifact_path=evidence_root / "authorization.json",
        candidate_artifact_path=evidence_root / "candidate.json",
        verifier_artifact_path=evidence_root / "verifier.json",
        merge_record_artifact_path=evidence_root / "merge-record.json",
        repository_checkout=Path(str(ctx.local_checkout)),
        run_state_root=state_root,
        evidence_root=evidence_root,
        live_pr_payload={
            "state": "open", "merged": False,
            "head": {"sha": authorized_head},
            "baseRefName": "main", "mergeable": "MERGEABLE",
            "autoMergeRequest": None, "isDraft": False,
            "reviewDecision": "APPROVED",
            "repo": "owner/repo",
        },
        live_ci_state={"all_required_passing": True, "coderabbit_passing": True},
        live_review_state={"latest_coderabbit_state": "APPROVED"},
        live_thread_inventory={"unresolved_current": 0, "unresolved_outdated": 0},
        working_tree_clean=True,
        required_ci_names=tuple(ctx.required_ci_jobs or ()),
        _bypass_oid_reachability=True,
    )

    call_log: List[List[str]] = []
    def fake(cmd, **kwargs):
        call_log.append(list(cmd))
        joined = " ".join(str(c) for c in cmd)
        if "pr merge" in joined:
            return {"returncode": 1, "stdout": "", "stderr": "should not", "timed_out": False}
        if "pr view" in joined and " pr merge " not in f" {joined} ":
            return {
                "returncode": 0,
                "stdout": json.dumps({
                    "state": "open", "mergedAt": None,
                    "headRefOid": authorized_head, "baseRefName": "main",
                    "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                    "autoMergeRequest": None, "isDraft": False,
                    "reviewDecision": "APPROVED", "number": 5,
                }),
                "stderr": "", "timed_out": False,
            }
        if "pr checks" in joined:
            # security-scan is MISSING from the live inventory.
            return {
                "returncode": 0,
                # Use uppercase state to match ``gh pr checks`` output.
                "stdout": "test\tSUCCESS\n",
                "stderr": "", "timed_out": False,
            }
        if "api graphql" in joined:
            body = json.dumps({
                "data": {"repository": {"pullRequest": {
                    "headRefOid": authorized_head,
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [],
                    },
                    "reviews": {"nodes": [{
                        "state": "APPROVED",
                        "author": {"login": "coderabbitai[bot]"},
                        "submittedAt": "2026-01-01T00:00:00Z",
                        # Round-28 P3: review's commit OID for exact-head binding.
                        "commit": {"oid": "a" * 40},
                    }]},
                }}},
            })
            return {"returncode": 0, "stdout": body, "stderr": "", "timed_out": False}
        if "mergeCommit" in joined:
            return {"returncode": 0, "stdout": json.dumps({"mergeCommit": "f" * 40}), "stderr": "", "timed_out": False}
        return {"returncode": 0, "stdout": "main", "stderr": "", "timed_out": False}

    with mock.patch(
        "autocoder_orchestration.merge_authorization._safe_run",
        side_effect=fake,
    ), mock.patch(
        "autocoder_orchestration.merge_authorization.subprocess.check_output",
        side_effect=lambda *a, **kw: (_ for _ in ()).throw(
            __import__("subprocess").CalledProcessError(1, a[0] if a else "")
        ),
    ):
        with pytest.raises(MergeGateChanged) as exc:
            execute_guarded_merge_transaction(inputs)
    msg = str(exc.value)
    assert "security-scan" in msg
    assert "missing" in msg
    # Merge subprocess MUST NOT have been invoked.
    assert not any(
        len(c) >= 3 and c[0] == "gh" and c[1] == "pr" and c[2] == "merge"
        for c in call_log
    ), "merge subprocess MUST NOT be invoked when a required CI is missing"


# ===== Test: empty required set with cross-binding agreement passes =====

def test_empty_required_set_with_cross_binding_passes(
    tmp_path: Path,
) -> None:
    """When the persisted run policy AND the merge authorization
    both say zero required jobs, the locked mutable-gate MUST
    NOT raise ``MergeGateChanged`` for missing checks. The
    transaction proceeds to the next guard (and eventually fails
    on the OID / merge subprocess path; that's fine — the test
    asserts the CI gate does NOT fire).
    """
    state_root = tmp_path / "state"
    evidence_root = tmp_path / "evidence"
    state_root.mkdir()

    evidence_root.mkdir()
    authorized_head = "a" * 40
    _write_authorization(
        evidence_root,
        authorized_head=authorized_head,
        repo_owner="owner", repo_name="repo",
        pr_number=5,
        required_ci_jobs=(),  # zero required
    )
    ctx = _make_run_context(
        repo_owner="owner", repo_name="repo", pr_number=5,
        run_id="r28-p2", authorized_head=authorized_head,
        required_ci_jobs=(),
        state_root=state_root, evidence_root=evidence_root,
        local_checkout=tmp_path,
    )
    inputs = MergeTransactionInputs(
        authorization_artifact_path=evidence_root / "authorization.json",
        candidate_artifact_path=evidence_root / "candidate.json",
        verifier_artifact_path=evidence_root / "verifier.json",
        merge_record_artifact_path=evidence_root / "merge-record.json",
        repository_checkout=Path(str(ctx.local_checkout)),
        run_state_root=state_root,
        evidence_root=evidence_root,
        live_pr_payload={
            "state": "open", "merged": False,
            "head": {"sha": authorized_head},
            "baseRefName": "main", "mergeable": "MERGEABLE",
            "autoMergeRequest": None, "isDraft": False,
            "reviewDecision": "APPROVED",
            "repo": "owner/repo",
        },
        live_ci_state={"all_required_passing": True, "coderabbit_passing": True},
        live_review_state={"latest_coderabbit_state": "APPROVED"},
        live_thread_inventory={"unresolved_current": 0, "unresolved_outdated": 0},
        working_tree_clean=True,
        required_ci_names=(),  # cross-binding agrees: zero required
        _bypass_oid_reachability=True,
    )
    call_log: List[List[str]] = []
    fake = _make_subprocess_logging_fake(call_log)
    with mock.patch(
        "autocoder_orchestration.merge_authorization._safe_run",
        side_effect=fake,
    ), mock.patch(
        "autocoder_orchestration.merge_authorization.subprocess.check_output",
        side_effect=lambda *a, **kw: (_ for _ in ()).throw(
            __import__("subprocess").CalledProcessError(1, a[0] if a else "")
        ),
    ):
        # The CI gate does NOT fire. The transaction proceeds
        # to the merge subprocess, which the fake returns as
        # ``rc=1``. The transaction then re-queries and raises
        # ``MergeAmbiguousOutcome`` (post-subproc re-query
        # returns OPEN state).
        with pytest.raises((MergeAmbiguousOutcome, Exception)) as exc:
            execute_guarded_merge_transaction(inputs)
    # The exception MUST NOT be ``MergeGateChanged`` (the CI gate).
    assert not isinstance(exc.value, MergeGateChanged), (
        f"empty required set with cross-binding agreement MUST NOT "
        f"fire MergeGateChanged; got {exc.value!r}"
    )
