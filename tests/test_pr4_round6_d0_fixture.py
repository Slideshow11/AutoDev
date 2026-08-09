"""Round-6 directive 9g + Round-7 directive proof repair:
complete D0 behavioral proof.

PRRT_kwDOTtyQLc6XPdD0 evidence: a passing verifier artifact
exists; authorization binds its exact digest; verifier_failed
(or any other tampering) replaces the canonical verifier
afterward; the guarded merge re-reads the canonical verifier;
the replacement is detected by digest mismatch; the guarded
transaction fails; ``gh pr merge`` invocation count remains
exactly ZERO.

The round-6 evidence was insufficient because:
* git rev-parse HEAD did not use check=True (PRRT_kwDOTtyQLc6XV60h);
* the control test swallowed arbitrary exceptions and only
  counted every helper invocation instead of the exact
  gh pr merge command (PRRT_kwDOTtyQLc6XV604);
* the failed-verdict test did not actually exercise the verdict
  guard because the digest guard fired first.

The round-7 D0 fixture builds a comprehensive, correct
merge-transaction scenario:

* real Git checkout (with a real commit on the merged branch);
* the authorization, candidate, and live_pr_payload.head.sha
  all reference that real committed head;
* valid distinct repository root, run-state root, and
  evidence root;
* valid canonical candidate artifact + sidecar;
* valid canonical passing verifier artifact + sidecar;
* valid MergeAuthorization;
* correct repo, PR number, exact authorized head;
* correct candidate exact-file digest;
* correct original verifier exact-file digest;
* correct merge method, delete-branch policy, and
  require_match_head_commit flag;
* valid live PR payload matching the authorization;
* OPEN state, not merged, not draft, MERGEABLE, CLEAN,
  no auto-merge;
* all required CI passing;
* CodeRabbit passing (latest APPROVED);
* zero unresolved current threads;
* working tree clean.

Three tests:

1. POSITIVE CONTROL: with the original authorized verifier
   and every precondition valid, the production guarded
   transaction reaches the EXACT gh pr merge command and
   invokes it exactly once. The test captures every
   _safe_run argv, counts the gh pr merge invocations, and
   verifies:
   * gh_pr_merge_invocation_count == 1
   * authorized PR number is correct;
   * repository is correct;
   * --squash is present;
   * --match-head-commit contains the fixture's authorized
     head;
   * --delete-branch matches the authorization policy;
   * --admin absent;
   * --auto absent;
   * --merge absent;
   * --rebase absent;
   * no force option exists.
   The control may fail AFTER the merge invocation is
   proven (the mocked post-merge reconciliation may fail,
   but the merge invocation is on the record).

2. REPLACEMENT-DIGEST NEGATIVE: the canonical verifier is
   replaced AFTER authorization with a structurally-acceptable
   record (verdict=VERIFIED, defects=[], candidate_sha256
   unchanged; only a non-critical field differs). The
   guarded merge MUST detect the digest mismatch and refuse
   to invoke the gh pr merge command. The test counts
   gh pr merge invocations and asserts exactly zero.

3. FAILED-VERDICT NEGATIVE: the FAILED verifier record is
   constructed FIRST; the authorization binds to that
   record's exact-file digest. The digest guard then passes
   (the digest matches) and the verdict guard fires. The
   test asserts the MergeError diagnostic references the
   FAILED verdict (NOT a digest mismatch) and that no gh pr
   merge invocation occurred.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _make_run_context(repo_path, run_state_root, evidence_root,
                       authorized_head):
    from autocoder_orchestration.context import (
        SCHEMA_VERSION as RC_SCHEMA,
    )
    from autocoder_orchestration.context import RunContext
    return RunContext(
        schema_version=RC_SCHEMA,
        run_id="test-r7-d0",
        created_at="2026-08-07T15:00:00Z",
        repo_owner="Slideshow11", repo_name="AutoDev",
        local_checkout=str(repo_path),
        base_branch="main",
        authorized_base_sha="a" * 40,
        feature_branch="fix/test",
        pr_number=4,
        current_authorized_head=authorized_head,
        task_specification_path=str(evidence_root / "task.txt"),
        task_specification_sha256="c" * 64,
        required_ci_jobs=("test (3.10)", "test (3.11)",
                          "test (3.12)", "package-smoke",
                          "provenance", "committed-state-scan"),
        reviewer_policy="approve-only",
        quiet_window_seconds=180,
        implementation_worker_command=("echo", "worker"),
        verifier_command=None,
        verifier_handoff_policy="strict",
        permitted_mutations=("candidate.json", "verifier.json",
                             "merge-record.json"),
        human_only_actions=("merge",),
        evidence_root=str(evidence_root),
        state_root=str(run_state_root),
        next_wave_policy="none",
    )


def _init_real_git_repo(repo_path: Path) -> str:
    """Initialize a real Git checkout with a single commit on
    ``main``. Returns the HEAD SHA. Per round-7 finding
    PRRT_kwDOTtyQLc6XV60h, ``git rev-parse HEAD`` uses
    ``check=True`` so a Git failure raises immediately instead
    of returning an empty or invalid SHA."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo_path)],
                   check=True, env=env, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test"],
                   cwd=str(repo_path), check=True, env=env, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"],
                   cwd=str(repo_path), check=True, env=env, capture_output=True)
    (repo_path / "README.md").write_text("test")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_path),
                   check=True, env=env, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_path),
                   check=True, env=env, capture_output=True)
    # Per round-7 finding PRRT_kwDOTtyQLc6XV60h: use check=True
    # so a Git failure raises immediately.
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo_path),
        env=env, capture_output=True, text=True, check=True,
    )
    head = proc.stdout.strip()
    assert len(head) == 40 and all(
        c in "0123456789abcdef" for c in head
    ), f"git rev-parse HEAD returned invalid SHA: {head!r}"
    return head


def _build_d0_fixture(verifier_payload_override=None):
    """Build a comprehensive, valid merge-transaction scenario
    in a tempdir. Returns ``(tmp, ctx, paths, candidate_digest,
    original_verifier_digest, fixtures)``.

    Per round-7 finding ``Bind the real Git HEAD to the
    authorized head``: ``current_authorized_head`` is set to
    the real ``init_head`` returned by ``_init_real_git_repo``
    so the post-merge Git reconciliation operates on a head
    that exists in the checkout.

    The optional ``verifier_payload_override`` parameter
    allows the failed-verdict test to construct a
    FAILED verifier record FIRST (so the authorization binds
    to that record's exact digest; the digest guard then
    passes; the verdict guard is the only remaining barrier).
    """
    from autocoder_orchestration.artifacts import (
        read_artifact, write_artifact,
    )
    from autocoder_orchestration.canonical_paths import canonical_paths
    from autocoder_orchestration.store import StateStore
    from autocoder_orchestration.state_machine import (
        STATE_AWAITING_MERGE_AUTHORIZATION,
        StateMachine,
    )

    tmp = Path(tempfile.mkdtemp(prefix="aed-r7-d0-"))
    repo_path = tmp / "repo"
    state_path = tmp / "state"
    evidence_root = tmp / "evidence"
    state_path.mkdir(parents=True)
    evidence_root.mkdir(parents=True)
    init_head = _init_real_git_repo(repo_path)
    paths = canonical_paths(evidence_root)
    for p in paths.values():
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ctx = _make_run_context(repo_path, state_path, evidence_root,
                            init_head)
    store = StateStore(str(state_path))
    store.write_atomic("run_context.json", ctx.to_dict())
    sm = StateMachine(current_state=STATE_AWAITING_MERGE_AUTHORIZATION)
    store.write_atomic("state.json", sm.to_dict())

    cand = {
        "schema_version": "autocoder.candidate.v1",
        "run_id": ctx.run_id,
        "repo": f"{ctx.repo_owner}/{ctx.repo_name}",
        "pr_number": ctx.pr_number,
        "exact_head": ctx.current_authorized_head,
        "base_sha": ctx.authorized_base_sha,
        "base_branch": ctx.base_branch,
        "task_specification_sha256": ctx.task_specification_sha256,
        "readiness_certificate_id": "cert-1",
        "readiness_certificate_sha256": "f" * 64,
        "readiness_overall_passed": True,
        "ci_inventory": [], "review_inventory": [],
        "thread_inventory": {"unresolved_current": 0,
                              "unresolved_outdated": 0},
        "strict_observation_log_hash": "",
        "process_identity": {}, "lock_release_evidence": {},
        "controller_state_revision": 1,
        "controller_state_path": "state.json",
        "input_hashes": {}, "source_files": {},
        "aed_source_files": {},
        "created_at": "2026-08-07T15:00:00Z",
        "head": {"head_sha": ctx.current_authorized_head,
                   "exact_head_sha": ctx.current_authorized_head},
    }
    write_artifact(paths["candidate"], cand)
    candidate_digest = read_artifact(paths["candidate"]).digest

    if verifier_payload_override is None:
        verifier_payload = {
            "schema_version": "autocoder.verifier_record.v1",
            "candidate_sha256": candidate_digest,
            "verdict": "VERIFIED",
            "defects": [],
            "verified_at_utc": "2026-08-07T15:00:00Z",
        }
    else:
        verifier_payload = dict(verifier_payload_override)
        verifier_payload["candidate_sha256"] = candidate_digest
    write_artifact(paths["verifier"], verifier_payload)
    original_verifier_digest = read_artifact(paths["verifier"]).digest

    auth = {
        "schema_version": "autocoder.merge_authorization.v1",
        "run_id": ctx.run_id,
        "repo": f"{ctx.repo_owner}/{ctx.repo_name}",
        "pr_number": ctx.pr_number,
        "authorized_head": ctx.current_authorized_head,
        "candidate_sha256": candidate_digest,
        "verifier_record_sha256": original_verifier_digest,
        "merge_method": "squash",
        "delete_branch": False,
        "require_match_head_commit": True,
        "authorization_timestamp": "2026-08-07T15:00:00Z",
        "author": "test",
        "next_wave_authorization": None,
        "notes": "",
    }
    write_artifact(paths["authorization"], auth)

    fixtures = {
        "init_head": init_head,
        "repo": f"{ctx.repo_owner}/{ctx.repo_name}",
        "pr_number": ctx.pr_number,
        "authorized_head": ctx.current_authorized_head,
        "live_pr_payload": {
            "state": "open",
            "merged": False,
            "head": {"sha": ctx.current_authorized_head},
            "baseRefName": "main",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "autoMergeRequest": None,
            "isDraft": False,
            "reviewDecision": "APPROVED",
        },
        "live_ci_state": {"all_required_passing": True,
                            "coderabbit_passing": True},
        "live_review_state": {"latest_coderabbit_state": "APPROVED"},
        "live_thread_inventory": {"unresolved_current": 0,
                                  "unresolved_outdated": 0},
        "working_tree_clean": True,
    }
    return (tmp, ctx, paths, candidate_digest,
            original_verifier_digest, fixtures)


def _is_gh_pr_merge(call) -> bool:
    """True when the captured ``_safe_run`` call is the exact
    ``gh pr merge`` command. Per round-8 directive (matched
    by adjacency, not membership):
    * argv[0] == "gh"
    * argv[1] == "pr"
    * argv[2] == "merge"
    The function safely handles calls without positional
    arguments and non-list argv values.
    """
    if not call.args:
        return False
    argv = call.args[0]
    if not isinstance(argv, (list, tuple)):
        return False
    argv = list(argv)
    return (
        len(argv) >= 3
        and argv[0] == "gh"
        and argv[1] == "pr"
        and argv[2] == "merge"
    )


def _count_gh_pr_merge_calls(safe_run_mock) -> int:
    """Count invocations of the ``gh pr merge`` command in a
    mock of ``_safe_run``. Other ``_safe_run`` calls (e.g.
    ``gh pr view --json mergeCommit``) are NOT counted. This
    is the controlling invariant for the D0 proof."""
    return sum(1 for call in safe_run_mock.call_args_list
                if _is_gh_pr_merge(call))


def _find_gh_pr_merge_call(safe_run_mock) -> list:
    """Return the argv list of the gh pr merge call, or an
    empty list if none was issued."""
    for call in safe_run_mock.call_args_list:
        if _is_gh_pr_merge(call):
            return list(call.args[0])
    return []


class FailedVerifierZeroGhInvocationsFullFixtureTests(unittest.TestCase):
    """Comprehensive D0 fixture tests for PRRT_kwDOTtyQLc6XPdD0."""

    def _build_inputs(self, ctx, paths, fixtures):
        from autocoder_orchestration.merge_authorization import (
            MergeTransactionInputs,
        )
        return MergeTransactionInputs(
            authorization_artifact_path=paths["authorization"],
            candidate_artifact_path=paths["candidate"],
            verifier_artifact_path=paths["verifier"],
            merge_record_artifact_path=paths["merge_record"],
            repository_checkout=ctx.local_checkout,
            run_state_root=ctx.state_root,
            evidence_root=ctx.evidence_root,
            live_pr_payload=fixtures["live_pr_payload"],
            live_ci_state=fixtures["live_ci_state"],
            live_review_state=fixtures["live_review_state"],
            live_thread_inventory=fixtures["live_thread_inventory"],
            working_tree_clean=fixtures["working_tree_clean"],
        )

    def _safe_run_successful(self):
        """Build a ``_safe_run`` mock that returns a successful
        result."""
        return mock.MagicMock(return_value={
            "returncode": 0, "stdout": "", "stderr": "",
            "timed_out": False,
        })

    def test_control_fixture_reaches_exact_guarded_merge_invocation(self):
        """POSITIVE CONTROL: with the original authorized
        verifier and every precondition valid, the production
        guarded transaction reaches the EXACT gh pr merge
        command and invokes it exactly once. Per round-7
        directive finding PRRT_kwDOTtyQLc6XV604, the test
        must NOT swallow the exception silently. Any
        exception that escapes past the protected merge
        invocation is captured into diagnostics so the
        negative test cannot be stopped by an earlier
        unrelated guard.

        Required proof:
        * gh_pr_merge_invocation_count == 1
        * PR number correct
        * repo correct
        * --squash present
        * --match-head-commit contains the authorized head
        * --delete-branch matches the authorization policy
        * --admin, --auto, --merge, --rebase all absent
        * no force option
        """
        from autocoder_orchestration.merge_authorization import (
            execute_guarded_merge_transaction,
        )

        tmp, ctx, paths, cand_digest, orig_v_digest, fixtures = _build_d0_fixture()
        try:
            inputs = self._build_inputs(ctx, paths, fixtures)
            safe_run = self._safe_run_successful()
            post_merge_exception = None
            with mock.patch(
                "autocoder_orchestration.merge_authorization._safe_run",
                safe_run,
            ):
                try:
                    execute_guarded_merge_transaction(inputs)
                except Exception as exc:
                    # Capture the exception so the test does
                    # not silently swallow it. The control may
                    # fail after the protected merge invocation
                    # is on the record (e.g. mocked
                    # reconciliation may not return a complete
                    # PostMergeReconciliation). Such failures
                    # are diagnostic only -- they must not
                    # invalidate the positive proof.
                    post_merge_exception = repr(exc)
            # The controlling invariant: gh pr merge
            # invocation count is exactly ONE.
            gh_merge_count = _count_gh_pr_merge_calls(safe_run)
            self.assertEqual(
                gh_merge_count, 1,
                f"control fixture must invoke gh pr merge "
                f"exactly once; got {gh_merge_count}. "
                f"post_merge_exception={post_merge_exception!r}; "
                f"safe_run calls={safe_run.call_args_list!r}",
            )
            # Inspect the argv of the gh pr merge call.
            argv = _find_gh_pr_merge_call(safe_run)
            self.assertTrue(argv,
                "gh pr merge invocation must be present in "
                "the captured _safe_run calls")
            # PR number is the fixture's authorized PR.
            self.assertIn(str(ctx.pr_number), argv)
            # Repository is the fixture's authorized repo.
            self.assertIn(ctx.repo_owner + "/" + ctx.repo_name, argv)
            # --squash is the only approved merge method.
            self.assertIn("--squash", argv)
            # --match-head-commit contains the authorized head.
            self.assertIn("--match-head-commit", argv)
            # The --match-head-commit value is the authorized
            # head SHA.
            idx = argv.index("--match-head-commit")
            self.assertEqual(
                argv[idx + 1], ctx.current_authorized_head,
                f"--match-head-commit value {argv[idx + 1]!r} != "
                f"authorized head {ctx.current_authorized_head!r}",
            )
            # --delete-branch must match the authorization
            # policy (delete_branch=False in this fixture).
            self.assertNotIn("--delete-branch", argv,
                f"--delete-branch present; authorization "
                f"policy is delete_branch=False; argv={argv!r}")
            # Forbidden flags are absent.
            for forbidden in ("--admin", "--auto", "--merge", "--rebase"):
                self.assertNotIn(forbidden, argv,
                    f"forbidden flag {forbidden!r} present; "
                    f"argv={argv!r}")
            # No force option exists.
            for force in ("--force", "--force-with-lease"):
                self.assertNotIn(force, argv,
                    f"force option {force!r} present; "
                    f"argv={argv!r}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_d0_replacement_verifier_digest_mismatch_blocks_gh_pr_merge(self):
        """The verifier is replaced AFTER authorization with a
        structurally-acceptable record (verdict=VERIFIED,
        defects=[], candidate_sha256 matches; only a
        non-critical field differs). The exact-file digest
        changes. The guarded merge MUST detect the digest
        mismatch and refuse to invoke ``gh pr merge``.

        Per round-7 directive, the controlling invariant is:
        ``gh_pr_merge_invocation_count == 0``. ``_safe_run``
        may also be called for ``gh pr view --json mergeCommit``
        (the guarded merge transaction checks whether the
        server reports the merge despite a subprocess error);
        that call is NOT a gh pr merge invocation and is not
        counted by ``_count_gh_pr_merge_calls``.
        """
        from autocoder_orchestration.merge_authorization import (
            execute_guarded_merge_transaction,
            MergeError,
        )
        from autocoder_orchestration.artifacts import (
            read_artifact, write_artifact,
        )

        tmp, ctx, paths, cand_digest, orig_v_digest, fixtures = _build_d0_fixture()
        try:
            # Replace the canonical verifier AFTER authorization.
            # Keep verdict=VERIFIED, defects=[], candidate_sha256
            # matching the original; change a non-critical field
            # so the exact-file digest differs.
            replaced = {
                "schema_version": "autocoder.verifier_record.v1",
                "candidate_sha256": cand_digest,
                "verdict": "VERIFIED",
                "defects": [],
                "verified_at_utc": "2026-08-07T16:00:00Z",
            }
            write_artifact(paths["verifier"], replaced)
            # Assert the replacement precondition: the new
            # exact-file digest differs from the original.
            new_v_digest = read_artifact(paths["verifier"]).digest
            self.assertNotEqual(
                new_v_digest, orig_v_digest,
                "replacement must change the exact-file digest; "
                "otherwise the test proves nothing",
            )

            safe_run = self._safe_run_successful()
            with mock.patch(
                "autocoder_orchestration.merge_authorization._safe_run",
                safe_run,
            ):
                with self.assertRaises(MergeError) as ctx_exc:
                    execute_guarded_merge_transaction(
                        self._build_inputs(ctx, paths, fixtures))
            # Controlling invariant: gh pr merge invocation
            # count is exactly ZERO.
            gh_merge_count = _count_gh_pr_merge_calls(safe_run)
            self.assertEqual(
                gh_merge_count, 0,
                f"gh pr merge must NOT be invoked; got "
                f"{gh_merge_count}. The verifier-digest "
                f"replacement must be detected by the guarded "
                f"merge before any remote merge invocation. "
                f"safe_run calls={safe_run.call_args_list!r}",
            )
            # Additional invariant: every _safe_run helper
            # invocation is also zero. The guarded transaction
            # does not invoke _safe_run at all when the
            # digest guard fires first.
            self.assertEqual(
                safe_run.call_count, 0,
                f"every _safe_run invocation must be zero; got "
                f"{safe_run.call_count}",
            )
            # The failure diagnostic identifies the digest
            # mismatch.
            msg = str(ctx_exc.exception).lower()
            self.assertIn("verifier", msg)
            self.assertTrue(
                "digest" in msg or "sha256" in msg,
                f"MergeError must name the verifier digest/SHA "
                f"mismatch; got: {msg!r}",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_d0_failed_verdict_record_is_unauthorizable(self):
        """The failed-verdict path (verdict="FAILED" + _verdict_failed)
        must remain un-authorizable. Per round-7 directive,
        this test exercises the VERDICT GUARD (not the digest
        guard): the FAILED record is constructed FIRST; the
        authorization binds to that record's exact-file
        digest; the digest guard therefore passes; only the
        verdict guard can reject the merge.
        """
        from autocoder_orchestration.merge_authorization import (
            execute_guarded_merge_transaction,
            MergeError,
        )

        # Build the FAILED verifier record FIRST so the
        # authorization binds to its exact-file digest.
        failed = {
            "schema_version": "autocoder.verifier_record.v1",
            "candidate_sha256": "placeholder",  # overwritten
            "verdict": "FAILED",
            "defects": ["synthetic test failure"],
            "_verdict_failed": True,
        }
        tmp, ctx, paths, cand_digest, orig_v_digest, fixtures = (
            _build_d0_fixture(verifier_payload_override=failed)
        )
        try:
            # The authorization was bound to the FAILED
            # verifier's exact-file digest, so the digest
            # guard passes. Only the verdict guard can reject.
            safe_run = self._safe_run_successful()
            with mock.patch(
                "autocoder_orchestration.merge_authorization._safe_run",
                safe_run,
            ):
                with self.assertRaises(MergeError) as ctx_exc:
                    execute_guarded_merge_transaction(
                        self._build_inputs(ctx, paths, fixtures))
            # Controlling invariant: no gh pr merge invocation.
            gh_merge_count = _count_gh_pr_merge_calls(safe_run)
            self.assertEqual(
                gh_merge_count, 0,
                f"gh pr merge must NOT be invoked on a FAILED "
                f"verifier; got {gh_merge_count}",
            )
            self.assertEqual(
                safe_run.call_count, 0,
                f"every _safe_run invocation must be zero; got "
                f"{safe_run.call_count}",
            )
            # The MergeError diagnostic references the verdict
            # (NOT a digest mismatch).
            msg = str(ctx_exc.exception).lower()
            self.assertIn("verdict", msg,
                f"MergeError must name the verdict guard; "
                f"got: {msg!r}")
            # The diagnostic does NOT reference a digest
            # mismatch -- the digest guard is satisfied for
            # this fixture (authorization was bound to the
            # FAILED record's exact-file digest).
            self.assertNotIn("digest mismatch", msg,
                f"MergeError must not reference a digest "
                f"mismatch (the digest guard passed); got: "
                f"{msg!r}")
            self.assertNotIn("digest of canonical verifier", msg,
                f"MergeError must not reference a digest "
                f"mismatch (the digest guard passed); got: "
                f"{msg!r}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()