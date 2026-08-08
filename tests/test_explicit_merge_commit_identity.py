"""Regression tests for the explicit mergeCommit identity contract.

The squash_merge_commit field in the post-merge record MUST be bound
to the explicit PR mergeCommit OID (fetched via gh pr view --json
mergeCommit), not to local_main_sha or origin_main_sha.

These tests verify:

* the explicit OID is preferred when valid and reachable;
* an invalid OID (bad length / non-hex) is rejected;
* an unreachable OID is rejected and the unavailable observation
  records the fallback;
* a missing mergeCommit field falls back to local_main_sha WITHOUT
  claiming it is the PR merge;
* parent count, parent SHA, and tree SHA are read from the explicit
  OID, not from local_main_sha.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure local module imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autocoder_orchestration.merge_authorization import (
    _LOWER_HEX_40_RE,
    PostMergeReconciliation,
    _run_git,
    reconcile_after_merge,
)


def _init_git_repo(path: Path) -> tuple[str, str]:
    """Initialize a fresh git repository with a base commit and a feature commit.

    Returns ``(feature_sha, origin_main_sha)`` after the squash merge
    on both local and origin clones. Local and origin share the same
    squash SHA by default.
    """
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    run = subprocess.run
    cwd = path

    def g(*args, **kwargs):
        kwargs["cwd"] = cwd
        kwargs["env"] = env
        kwargs["capture_output"] = True
        kwargs["text"] = True
        return run(["git"] + list(args), **kwargs)

    g("init", "-q", "-b", "main")
    g("config", "user.email", "test@example.com")
    g("config", "user.name", "test")

    (path / "README.md").write_text("# test repo\n")
    g("add", "README.md")
    g("commit", "-q", "-m", "initial commit")

    g("checkout", "-q", "-b", "feat/test")
    (path / "feature.py").write_text("FEATURE = True\n")
    g("add", "feature.py")
    g("commit", "-q", "-m", "feature commit")
    feature_sha = g("rev-parse", "HEAD").stdout.strip()

    # Squash-merge locally.
    g("checkout", "-q", "main")
    g("merge", "--squash", "feat/test")
    g("commit", "-q", "-m", "squash merge")
    local_main_sha = g("rev-parse", "HEAD").stdout.strip()

    # Clone (bare) to a sibling path so origin/<base> is reachable.
    origin_clone = path.parent / ("origin_" + path.name)
    g("clone", "-q", "--bare", str(path), str(origin_clone))
    # Replace the auto-set origin (which pointed at the local path)
    # with the bare clone so we can push through it.
    g("remote", "remove", "origin")
    g("remote", "add", "origin", str(origin_clone))
    g("fetch", "origin", "main")
    g("push", "origin", "main")
    origin_main_sha = local_main_sha

    return feature_sha, origin_main_sha


class ExplicitMergeCommitContractTests(unittest.TestCase):
    """The squash_merge_commit must follow the explicit mergeCommit OID."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir) / "repo"
        self.repo.mkdir()
        self.feature_sha, self.local_main_sha = _init_git_repo(self.repo)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_explicit_merge_commit_used_when_reachable(self) -> None:
        """An explicit OID that exists locally is used verbatim."""
        # Use a fabricated merge commit SHA that is reachable from main.
        # We use the actual local main SHA here because reconcile_after_merge
        # checks local-reachability.
        # Pretend the explicit OID is the local main tip — that satisfies
        # cat-file, and merge-base --is-ancestor returns 0.
        explicit = self.local_main_sha
        rc, _, _ = _run_git(["cat-file", "-t", explicit], self.repo)
        self.assertEqual(rc, 0, "test fixture: explicit OID must be reachable")
        recon = reconcile_after_merge(
            repository_checkout=self.repo,
            base_branch="main",
            feature_branch="feat/test",
            authorized_head=self.feature_sha,
            pr_merge_commit_oid=explicit,
        )
        self.assertEqual(recon.squash_merge_commit, explicit)

    def test_invalid_oid_format_rejected(self) -> None:
        """An OID that is not 40 lowercase hex is refused."""
        recon = reconcile_after_merge(
            repository_checkout=self.repo,
            base_branch="main",
            feature_branch="feat/test",
            authorized_head=self.feature_sha,
            pr_merge_commit_oid="not-a-valid-oid",
        )
        # Fallback to local_main_sha; the unavailable list records the
        # invalid format.
        self.assertEqual(recon.squash_merge_commit, self.local_main_sha)
        self.assertTrue(
            any(
                "valid 40-character lowercase hex SHA" in s
                for s in recon.unavailable_observations
            ),
            f"expected invalid-format note; got {recon.unavailable_observations}",
        )

    def test_unreachable_oid_falls_back(self) -> None:
        """An OID that is not reachable from origin/<base> falls back."""
        # Create a commit on a separate branch so it's reachable
        # locally but not from main.
        run = subprocess.run
        run(["git", "checkout", "-q", "-b", "other", self.local_main_sha],
            cwd=self.repo, capture_output=True)
        (self.repo / "other.py").write_text("OTHER = True\n")
        run(["git", "add", "other.py"], cwd=self.repo, capture_output=True)
        run(["git", "commit", "-q", "-m", "other commit"],
            cwd=self.repo, capture_output=True)
        other_sha = run(["git", "rev-parse", "HEAD"],
                         cwd=self.repo, capture_output=True, text=True).stdout.strip()
        # Back to main.
        run(["git", "checkout", "-q", "main"], cwd=self.repo, capture_output=True)
        # Now `other_sha` exists locally but is NOT reachable from
        # origin/main (since main does not contain it).
        recon = reconcile_after_merge(
            repository_checkout=self.repo,
            base_branch="main",
            feature_branch="feat/test",
            authorized_head=self.feature_sha,
            pr_merge_commit_oid=other_sha,
        )
        # Fallback to local_main_sha; the unavailable list records
        # that the OID was not reachable.
        self.assertEqual(recon.squash_merge_commit, self.local_main_sha)
        self.assertTrue(
            any(
                "not reachable from origin/main" in s
                for s in recon.unavailable_observations
            ),
            f"expected reachability note; got {recon.unavailable_observations}",
        )

    def test_missing_oid_falls_back_with_unavailable_observation(self) -> None:
        """Without an explicit OID, the unavailable observation is recorded."""
        recon = reconcile_after_merge(
            repository_checkout=self.repo,
            base_branch="main",
            feature_branch="feat/test",
            authorized_head=self.feature_sha,
            pr_merge_commit_oid=None,
        )
        self.assertEqual(recon.squash_merge_commit, self.local_main_sha)
        self.assertTrue(
            any(
                "pr_merge_commit_oid not supplied" in s
                for s in recon.unavailable_observations
            ),
            f"expected missing-oid note; got {recon.unavailable_observations}",
        )


class SquashMergeIdentityTests(unittest.TestCase):
    """Parent count, parent SHA, tree SHA read from the explicit OID."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir) / "repo"
        self.repo.mkdir()
        self.feature_sha, self.local_main_sha = _init_git_repo(self.repo)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_parent_tree_read_from_explicit_oid(self) -> None:
        """The tree and parent come from the explicit OID, not local_main_sha.

        This test creates a scenario where local_main_sha and the
        explicit OID are different. The recorded squash_merge_commit,
        parent, and tree SHA must match the explicit OID, not local.
        """
        # Create an "alt" commit on a side branch, then push it to
        # origin so reconcile_after_merge can verify ancestor
        # relationship. The alt branch is reachable from origin/main
        # after push, so the explicit OID is valid.
        run = subprocess.run
        run(["git", "checkout", "-q", "-b", "alt"], cwd=self.repo, capture_output=True)
        (self.repo / "alt.py").write_text("ALT = True\n")
        run(["git", "add", "alt.py"], cwd=self.repo, capture_output=True)
        run(["git", "commit", "-q", "-m", "alt commit"],
            cwd=self.repo, capture_output=True)
        alt_sha = run(["git", "rev-parse", "HEAD"],
                       cwd=self.repo, capture_output=True, text=True).stdout.strip()
        # Push alt to origin.
        origin_clone = self.repo.parent / ("origin_" + self.repo.name)
        run(["git", "push", "origin", "alt"],
            cwd=self.repo, capture_output=True)
        # Fetch so origin/alt exists in local.
        run(["git", "fetch", "origin", "alt"],
            cwd=self.repo, capture_output=True)
        # Merge alt into local main so origin/main contains it.
        run(["git", "checkout", "-q", "main"], cwd=self.repo, capture_output=True)
        run(["git", "merge", "--no-ff", "-q", "alt"],
            cwd=self.repo, capture_output=True)
        # Push to origin so origin/main includes the merge.
        run(["git", "push", "origin", "main"],
            cwd=self.repo, capture_output=True)
        # Recompute local main.
        new_local = run(["git", "rev-parse", "HEAD"],
                         cwd=self.repo, capture_output=True, text=True).stdout.strip()
        self.assertNotEqual(new_local, alt_sha)
        recon = reconcile_after_merge(
            repository_checkout=self.repo,
            base_branch="main",
            feature_branch="feat/test",
            authorized_head=self.feature_sha,
            pr_merge_commit_oid=alt_sha,
        )
        self.assertEqual(recon.squash_merge_commit, alt_sha)
        self.assertNotEqual(recon.squash_merge_commit, new_local)
        # The recorded parent count, parent SHA, and tree SHA
        # MUST come from the explicit OID, not local_main_sha.
        # A regression that read the parent or tree from the
        # local main commit would still pass the assertions
        # above; the assertions below bind the reconciliation
        # result to the exact git-reported values for ``alt_sha``.
        rev_list = run(
            ["git", "rev-list", "--parents", "-n", "1", alt_sha],
            cwd=self.repo, capture_output=True, text=True,
        ).stdout.strip().split()
        alt_parent_count = len(rev_list) - 1
        alt_parent_sha = rev_list[1]
        alt_tree_sha = run(
            ["git", "rev-parse", f"{alt_sha}^{{tree}}"],
            cwd=self.repo, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(recon.squash_parent_count, alt_parent_count,
            "squash_parent_count must come from the explicit "
            "OID, not from local_main_sha")
        self.assertEqual(recon.squash_parent, alt_parent_sha,
            "squash_parent must be the explicit OID's parent, "
            "not local_main_sha")
        self.assertEqual(recon.squash_tree_sha256, alt_tree_sha,
            "squash_tree_sha256 must be the explicit OID's tree, "
            "not local_main_sha")


class LowerHex40ValidationTests(unittest.TestCase):
    """The 40-character lowercase hex validator is exactly that."""

    def test_accepts_40_lowercase_hex(self) -> None:
        self.assertIsNotNone(_LOWER_HEX_40_RE.match("a" * 40))

    def test_rejects_uppercase_hex(self) -> None:
        # SHA-1 is conventionally lowercase; uppercase is rejected.
        self.assertIsNone(_LOWER_HEX_40_RE.match("A" * 40))

    def test_rejects_39_chars(self) -> None:
        self.assertIsNone(_LOWER_HEX_40_RE.match("a" * 39))

    def test_rejects_41_chars(self) -> None:
        self.assertIsNone(_LOWER_HEX_40_RE.match("a" * 41))

    def test_rejects_64_char_digest(self) -> None:
        # 64-char SHA-256 must not match the 40-char validator.
        self.assertIsNone(_LOWER_HEX_40_RE.match("a" * 64))


if __name__ == "__main__":
    unittest.main()