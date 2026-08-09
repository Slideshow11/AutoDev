"""Production-path behavioral tests for the merge-queue failure
mode.

When ``gh pr merge`` exits zero but the PR is queued (merge
queue) rather than merged, the production logic must NOT
write a successful merge record or transition to COMPLETE.
The explicit mergeCommit OID is the canary: a queued PR has
no mergeCommit.

This test exercises the production path through
``execute_guarded_merge_transaction`` with mocks that
simulate:

1. Subprocess returns zero (the merge command "succeeded").
2. ``gh pr view --json mergeCommit`` returns
   ``{"mergeCommit": null}`` (the PR is queued, not merged).
3. The local origin/main has not been advanced by the
   merge (the PR is queued, the local main is unchanged).

The test asserts that the production logic fails closed
and does NOT call the merge-record write path.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest


# Ensure the package root is on the import path for direct
# script runs.
# sys.path.insert(0, "PROJECT_ROOT")


from autocoder_orchestration.merge_authorization import (
    execute_guarded_merge_transaction,
    MergeTransactionInputs,
    MergeError,
    MergeSubprocessFailed,
    MergeAmbiguousOutcome,
    MergeAuthorization,
)
from autocoder_orchestration.artifacts import write_artifact


HEAD_SHA = "a" * 40


def _make_run_context(tmp_path: Path) -> None:
    """Initialize a git repo with main + feature branches."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "x@y.z"], cwd=str(repo), check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "x"], cwd=str(repo), check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "initial"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feat/test"],
        cwd=str(repo), check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "feature"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "-q", "main"],
        cwd=str(repo), check=True,
    )


def _write_artifacts(
    evidence_root: Path,
    repo: Path,
    authorized_head: str = HEAD_SHA,
) -> tuple[Path, Path, Path, str, str]:
    """Write authorization, candidate, verifier, and the merge-
    record target. Returns the artifact paths and the
    candidate and verifier record digests.

    The digests are computed from the on-disk files AFTER
    they are written, so the cross-references in the
    authorization and verifier are exact byte-equality
    matches.
    """
    import hashlib
    # Write the candidate first (the on-disk digest is
    # what the verifier and authorization will reference).
    candidate_path = evidence_root / "candidate.json"
    candidate_payload = {
        "schema_version": "autocoder.candidate.v1",
        "exact_head": authorized_head,
        "head": {"head_sha": authorized_head, "exact_head_sha": authorized_head},
        "candidate_sha256": "0" * 64,
        "created_at": "2026-08-08T00:00:00Z",
    }
    write_artifact(candidate_path, candidate_payload)
    candidate_digest = hashlib.sha256(
        candidate_path.read_bytes(),
    ).hexdigest()
    # Write the verifier with the candidate's digest.
    verifier_path = evidence_root / "verifier.json"
    verifier_payload = {
        "schema_version": "autocoder.verifier.v1",
        "head_observed": authorized_head,
        "candidate_sha256": candidate_digest,
        "candidate_path": str(candidate_path),
        "verdict": "VERIFIED",
        "created_at": "2026-08-08T00:00:00Z",
    }
    write_artifact(verifier_path, verifier_payload)
    verifier_digest = hashlib.sha256(
        verifier_path.read_bytes(),
    ).hexdigest()
    # Write the authorization with both digests.
    auth = MergeAuthorization(
        schema_version="autocoder.merge_authorization.v1",
        run_id="r1", repo="owner/repo", pr_number=4,
        authorized_head=authorized_head,
        candidate_sha256=candidate_digest,
        verifier_record_sha256=verifier_digest,
    )
    auth_path = evidence_root / "merge-authorization.json"
    write_artifact(auth_path, auth.to_dict())
    auth_digest = hashlib.sha256(auth_path.read_bytes()).hexdigest()
    merge_record_path = evidence_root / "merge-record.json"
    return (
        auth_path, candidate_path, verifier_path, merge_record_path,
        auth_digest, candidate_digest,
    )


def _build_inputs(
    repo: Path, evidence_root: Path,
    auth_path: Path, candidate_path: Path, verifier_path: Path,
    merge_record_path: Path,
    *, live_pr_merged: bool,
) -> MergeTransactionInputs:
    return MergeTransactionInputs(
        authorization_artifact_path=auth_path,
        candidate_artifact_path=candidate_path,
        verifier_artifact_path=verifier_path,
        merge_record_artifact_path=merge_record_path,
        repository_checkout=repo,
        run_state_root=evidence_root,
        evidence_root=evidence_root,
        live_pr_payload={
            "state": "open",
            "merged": live_pr_merged,
            "head": {"sha": HEAD_SHA},
            "baseRefName": "main",
            "mergeable": "MERGEABLE",
            "autoMergeRequest": None,
        },
        live_ci_state={
            "all_required_passing": True,
            "coderabbit_passing": True,
        },
        live_review_state={
            "latest_coderabbit_state": "APPROVED",
        },
        live_thread_inventory={
            "unresolved_current": 0,
            "unresolved_outdated": 0,
        },
        working_tree_clean=True,
    )


class TestMergeQueueQueuedPRFailsClosed:
    """The merge-queue failure mode: subprocess zero, PR queued,
    no mergeCommit OID. The production path must NOT write a
    successful merge record.
    """

    def _setup(self, tmp_path: Path):
        tmp_path.mkdir(exist_ok=True)
        repo = tmp_path / "repo"
        evidence_root = tmp_path / "evidence"
        evidence_root.mkdir()
        _make_run_context(tmp_path)
        (
            auth_path, candidate_path, verifier_path, merge_record_path,
            auth_digest, candidate_digest,
        ) = _write_artifacts(evidence_root, repo)
        return repo, evidence_root, auth_path, candidate_path, verifier_path, merge_record_path

    def test_queued_pr_with_no_mergecommit_oid_fails_closed(
        self, tmp_path: Path,
    ) -> None:
        """Scenario:
        - gh pr merge exits zero (command "succeeded")
        - gh pr view --json mergeCommit returns
          {"mergeCommit": null} (PR is queued, not merged)
        - local main is unchanged (the PR is queued, no local
          merge happened)
        Expected: execute_guarded_merge_transaction does NOT
        write a merge record. The OID-dependent reconciler
        either raises (preferred) or the record is written
        with squash_merge_commit empty and observation
        records the queued state.
        """
        (
            repo, evidence_root, auth_path, candidate_path, verifier_path,
            merge_record_path,
        ) = self._setup(tmp_path)
        inputs = _build_inputs(
            repo, evidence_root, auth_path, candidate_path,
            verifier_path, merge_record_path, live_pr_merged=False,
        )
        # Use a separate run_state_root to avoid the
        # distinct-roots collision guard.
        inputs = MergeTransactionInputs(
            authorization_artifact_path=inputs.authorization_artifact_path,
            candidate_artifact_path=inputs.candidate_artifact_path,
            verifier_artifact_path=inputs.verifier_artifact_path,
            merge_record_artifact_path=inputs.merge_record_artifact_path,
            repository_checkout=inputs.repository_checkout,
            run_state_root=tmp_path / "state",
            evidence_root=inputs.evidence_root,
            live_pr_payload=inputs.live_pr_payload,
            live_ci_state=inputs.live_ci_state,
            live_review_state=inputs.live_review_state,
            live_thread_inventory=inputs.live_thread_inventory,
            working_tree_clean=inputs.working_tree_clean,
        )
        # gh pr merge exits zero (PR is queued). The
        # production code calls fetch_live_pr_payload to
        # re-query the live PR — the mock returns null
        # mergeCommit + null mergedAt for the queued PR.
        # git commands return plausible text output for the
        # reconciler to work through.
        def fake_run(cmd, **kwargs):
            class _R:
                returncode = 0
                if cmd and "gh" in cmd[0]:
                    stdout = (
                        b'{"mergeCommit": null, "mergedAt": null, '
                        b'"state": "open", "isDraft": false, '
                        b'"mergeable": "MERGEABLE", '
                        b'"mergeStateStatus": "BLOCKED", '
                        b'"headRefOid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", '
                        b'"baseRefName": "main", '
                        b'"autoMergeRequest": null, "number": 4}'
                    )
                else:
                    if "rev-parse" in cmd and "abbrev-ref" in cmd:
                        stdout = "main"
                    elif "status" in cmd:
                        stdout = ""
                    else:
                        stdout = "main"
                stderr = ""
            return _R()
        def fake_check_output(cmd, **kwargs):
            raise subprocess.CalledProcessError(1, cmd)
        with mock.patch(
            "autocoder_orchestration.merge_authorization.subprocess.run",
            side_effect=fake_run,
        ), mock.patch(
            "autocoder_orchestration.merge_authorization.subprocess.check_output",
            side_effect=fake_check_output,
        ):
            try:
                execute_guarded_merge_transaction(inputs)
            except (MergeError, MergeSubprocessFailed, MergeAmbiguousOutcome):
                # Preferred: production logic raises because
                # the queued PR is not yet merged.
                pass
        # Verify: merge-record.json was NOT written.
        assert not merge_record_path.exists(), (
            f"merge-record must NOT be written for a queued PR; "
            f"path {merge_record_path} exists"
        )
