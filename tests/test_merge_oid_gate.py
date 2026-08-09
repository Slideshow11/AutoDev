"""Tests for the merge transaction's server-OID gate.

Round-26 Codex Critical invariant:
  A nonempty mergeCommit OID is NOT sufficient. If the OID is
  malformed, missing from the fetched repository, unreachable,
  not valid relative to the expected base/merge, or otherwise
  cannot be positively verified, the post-merge state MUST be
  treated as AMBIGUOUS:

    - a PARTIAL recovery record MUST be persisted
    - ``MergeAmbiguousOutcome`` MUST be raised
    - ``local_main_sha`` MUST NEVER be substituted as the
      merge identity

The pre-round-26 code fell through to reconciliation with
``pr_merge_commit_oid = None`` on the success path with no
OID, causing the transaction to write a ``final_state='COMPLETE'``
record describing a commit the server never confirmed.

These tests exercise the gate under three malformed-input
modes (malformed, missing, unreachable) and assert that the
AMBIGUOUS contract holds in every case.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


from autocoder_orchestration.merge_authorization import (
    MergeAmbiguousOutcome,
    MergeTransactionInputs,
    _build_default_live_fetchers,
    _fetch_and_validate_merge_oid,
    execute_guarded_merge_transaction,
)


def _init_git_repo(repo: Path) -> tuple:
    """Create a tiny git repo with a feature commit on ``master``
    and a main branch pointing at an empty commit. Returns
    ``(feature_sha, local_main_sha)``.

    Honors ``init.defaultBranch`` (commonly ``main``) by
    explicitly renaming the initial branch to ``master`` before
    creating ``main`` as an orphan.
    """
    run = subprocess.run
    run(["git", "init", "-q", "-b", "master", repo], check=True)
    run(["git", "-C", str(repo), "config", "user.email", "t@t.t"], check=True)
    run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "x.py").write_text("X = 1\n")
    run(["git", "-C", str(repo), "add", "x.py"], check=True)
    run(["git", "-C", str(repo), "commit", "-q", "-m", "feat"], check=True)
    feature_sha = run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # Make a separate main branch pointing at an empty commit.
    run(["git", "-C", str(repo), "checkout", "-q", "--orphan", "main"], check=True)
    run(["git", "-C", str(repo), "rm", "-q", "-rf", "."], check=True)
    (repo / "main_only.py").write_text("MAIN = 1\n")
    run(["git", "-C", str(repo), "add", "main_only.py"], check=True)
    run(["git", "-C", str(repo), "commit", "-q", "-m", "main"], check=True)
    local_main_sha = run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return feature_sha, local_main_sha


def _valid_auth(evidence: Path, authorized_head: str) -> dict:
    return {
        "schema_version": "autocoder.merge_authorization.v1",
        "run_id": "oid-gate-test",
        "repo": "owner/repo",
        "pr_number": 3,
        "authorized_head": authorized_head,
        "candidate_sha256": "0" * 64,
        "verifier_record_sha256": "1" * 64,
        "base_branch": "main",
        "feature_branch": "feat/test",
        "merge_method": "squash",
        "delete_branch": False,
        "require_match_head_commit": True,
    }


def _write_artifacts(evidence: Path, authorized_head: str) -> dict:
    """Write authorization, candidate, and verifier artifacts
    with matching cross-digests so the transaction reaches the
    OID gate without failing earlier.

    The production code reads the candidate's exact-FILE digest
    (SHA-256 of the canonical JSON bytes on disk). The verifier's
    ``candidate_sha256`` field MUST equal that exact-file digest,
    not the ``_sha256`` sidecar embedded in the payload. The
    auth artifact's ``candidate_sha256`` and
    ``verifier_record_sha256`` fields also reference the
    exact-file digests of those artifacts.
    """
    from autocoder_orchestration.artifacts import write_artifact
    paths = {
        "auth": evidence / "authorization.json",
        "cand": evidence / "candidate.json",
        "ver": evidence / "verifier.json",
        "rec": evidence / "merge-record.json",
    }
    cand_payload = {"exact_head": authorized_head, "files": []}
    write_artifact(paths["cand"], cand_payload)
    cand_file_digest = _canonical_file_digest(paths["cand"])
    ver_payload = {
        "verdict": "VERIFIED", "defects": [],
        "candidate_sha256": cand_file_digest,
    }
    write_artifact(paths["ver"], ver_payload)
    ver_file_digest = _canonical_file_digest(paths["ver"])
    auth = _valid_auth(evidence, authorized_head)
    auth["candidate_sha256"] = cand_file_digest
    auth["verifier_record_sha256"] = ver_file_digest
    write_artifact(paths["auth"], auth)
    return paths


def _canonical_file_digest(path: Path) -> str:
    """Compute the canonical exact-file digest of an artifact.

    Mirrors the production canonical artifact writer: the
    digest is the SHA-256 of the canonical JSON bytes on disk
    AFTER the sidecar is appended. For test artifacts this
    matches the digest reported by ``read_artifact``.
    """
    import hashlib as _hashlib
    return _hashlib.sha256(path.read_bytes()).hexdigest()


def _make_inputs(paths, repo, state, evidence, authorized_head: str):
    inputs = MergeTransactionInputs(
        authorization_artifact_path=paths["auth"],
        candidate_artifact_path=paths["cand"],
        verifier_artifact_path=paths["ver"],
        merge_record_artifact_path=paths["rec"],
        repository_checkout=repo,
        run_state_root=state,
        evidence_root=evidence,
        live_pr_payload={
            "state": "open", "merged": False,
            "head": {"sha": authorized_head},
            "baseRefName": "main", "mergeable": "MERGEABLE",
            "autoMergeRequest": None,
            "reviewDecision": "APPROVED",
            "repo": "owner/repo",
        },
        live_ci_state={"all_required_passing": True, "coderabbit_passing": True},
        live_review_state={"latest_coderabbit_state": "APPROVED"},
        live_thread_inventory={"unresolved_current": 0, "unresolved_outdated": 0},
        working_tree_clean=True,
        # Hermetic test: opt out of the OID reachability check
        # via the test seam (not a public bypass).
        required_ci_names=(),
    )
    inputs._set_bypass_oid_reachability(True)
    # Round-27: inject the default live fetchers so the
    # mutable-gate comparator sees live == bound (no divergence).
    # Tests that want to exercise a divergence override these
    # with their own fetchers.
    inputs._set_live_fetchers(_build_default_live_fetchers(inputs, review_commit_oid=authorized_head))
    # Round-28 P3: override the review fetcher so the
    # latest_coderabbit_commit_oid matches the authorized head.
    # Without this, the round-28 P3 exact-head binding fires
    # BEFORE the OID gate (which is what the OID tests want to
    # exercise).
    fetchers = inputs._live_fetchers
    if fetchers is not None:
        original_review_fetcher = fetchers["review_state"]
        def _review_with_oid() -> dict:
            data = original_review_fetcher()
            data["latest_coderabbit_commit_oid"] = authorized_head
            data["reviews"] = [
                {**r, "commit_oid": authorized_head}
                for r in data["reviews"]
            ]
            return data
        fetchers["review_state"] = _review_with_oid
    return inputs


def _fake_subprocess_with_oid(oid_or_empty: str, authorized_head: str):
    """Return a fake ``_safe_run`` that handles the round-26
    call ordering: live-pr-payload refetch (FIRST), then
    ``gh pr merge``, then mergeCommit OID fetches, then the
    post-subprocess live re-query.

    The FIRST ``pr view`` (no mergeCommit) call is the
    refetch — return the OPEN/CLEAN snapshot that matches
    the bound inputs. The SECOND such call is the
    post-subprocess re-query — return the merged payload
    that the test expects.

    ``oid_or_empty`` is the value returned in the mergeCommit
    OID fetches: either a valid-looking 40-char string, an
    empty string (server returned no OID), or a malformed
    string (the malformed-OID case). ``authorized_head`` is
    the SHA the bound live_pr_payload carries — the refetch
    MUST report the same head (otherwise the round-26 P1#4
    gate rejects with ``MergeGateChanged``).
    """
    view_call_count = [0]

    def fake(cmd, **kwargs):
        joined = " ".join(str(x) for x in cmd)
        # Round-26 P1#4: live-pr-payload refetch happens FIRST,
        # inside the locked transaction. Return a clean /
        # APPROVED snapshot that matches the bound inputs.
        if "mergeCommit" not in joined and "pr view" in joined:
            view_call_count[0] += 1
            if view_call_count[0] == 1:
                return {
                    "returncode": 0,
                    "stdout": json.dumps({
                        "mergedAt": None,
                        "state": "open",
                        "isDraft": False,
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                        "headRefOid": authorized_head,
                        "baseRefName": "main",
                        "autoMergeRequest": None,
                        "reviewDecision": "APPROVED",
                    }),
                    "stderr": "", "timed_out": False,
                }
            # Post-subprocess re-query: report merged=true so
            # the OID-missing path triggers.
            payload = {
                "state": "MERGED",
                "mergedAt": "2026-08-09T00:00:00Z",
                "headRefOid": authorized_head,
                "baseRefName": "main",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "MERGED",
                "autoMergeRequest": None,
                "isDraft": False,
            }
            if oid_or_empty != "":
                payload["mergeCommit"] = {"oid": oid_or_empty}
            return {
                "returncode": 0,
                "stdout": json.dumps(payload),
                "stderr": "", "timed_out": False,
            }
        # gh pr merge — succeed (empty stdout).
        if "pr merge" in joined:
            return {
                "returncode": 0, "stdout": "",
                "stderr": "", "timed_out": False,
            }
        # mergeCommit OID fetches (two passes).
        if "mergeCommit" in joined:
            if oid_or_empty == "":
                # Missing OID: return an empty mergeCommit.
                return {
                    "returncode": 0,
                    "stdout": json.dumps({"mergeCommit": None}),
                    "stderr": "", "timed_out": False,
                }
            return {
                "returncode": 0,
                "stdout": json.dumps({"mergeCommit": {"oid": oid_or_empty}}),
                "stderr": "", "timed_out": False,
            }
        # Default (should not reach here).
        return {
            "returncode": 0, "stdout": "",
            "stderr": "", "timed_out": False,
        }
    return fake


class MalformedServerOidTests(unittest.TestCase):
    """Codex Critical: a malformed server OID MUST be treated
    as AMBIGUOUS. Never substitute ``local_main_sha``.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        self.repo = self.tmpdir / "repo"
        self.state = self.tmpdir / "state"
        self.evidence = self.tmpdir / "evidence"
        for d in (self.repo, self.state, self.evidence):
            d.mkdir()
        self.feature_sha, self.local_main_sha = _init_git_repo(self.repo)
        self.authorized_head = self.feature_sha

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_malformed_oid_uppercase_raises_merge_ambiguous_outcome(self) -> None:
        """The server returned an OID with uppercase hex (invalid).
        The transaction MUST persist a PARTIAL record and raise
        ``MergeAmbiguousOutcome``. ``local_main_sha`` MUST NOT
        be used as the merge identity.
        """
        paths = _write_artifacts(self.evidence, self.authorized_head)
        inputs = _make_inputs(paths, self.repo, self.state, self.evidence, self.authorized_head)
        fake = _fake_subprocess_with_oid("A" * 40, self.authorized_head)  # uppercase
        with mock.patch(
            "autocoder_orchestration.merge_authorization._safe_run",
            side_effect=fake,
        ):
            with self.assertRaises(MergeAmbiguousOutcome) as ctx:
                execute_guarded_merge_transaction(inputs)
        # The exception message MUST be unambiguous about
        # the post-merge ambiguity.
        msg = str(ctx.exception)
        self.assertIn("mergeCommit", msg)
        self.assertIn("unavailable", msg)
        # A PARTIAL record MUST have been written BEFORE the
        # raise (C-28).
        rec = json.loads(paths["rec"].read_text())
        self.assertEqual(rec["final_state"], "PARTIAL",
            f"PARTIAL recovery record MUST be written before "
            f"raising; got {rec['final_state']!r}")
        # The PARTIAL record MUST NOT describe local_main_sha
        # as the squash_merge_commit. An ambiguous OID means
        # the merge identity is unknown.
        self.assertNotEqual(rec["squash_merge_commit"], self.local_main_sha,
            "PARTIAL record MUST NOT use local_main_sha as the "
            "merge identity when the server OID is malformed")

    def test_malformed_oid_with_embedded_garbage_raises_merge_ambiguous_outcome(self) -> None:
        """A non-hex, mixed-length OID MUST also be treated as AMBIGUOUS.
        """
        paths = _write_artifacts(self.evidence, self.authorized_head)
        inputs = _make_inputs(paths, self.repo, self.state, self.evidence, self.authorized_head)
        fake = _fake_subprocess_with_oid("not-a-real-sha-12345", self.authorized_head)
        with mock.patch(
            "autocoder_orchestration.merge_authorization._safe_run",
            side_effect=fake,
        ):
            with self.assertRaises(MergeAmbiguousOutcome):
                execute_guarded_merge_transaction(inputs)
        rec = json.loads(paths["rec"].read_text())
        self.assertEqual(rec["final_state"], "PARTIAL")


class MissingServerOidTests(unittest.TestCase):
    """Codex Critical: the server returned no OID (replication
    lag). The transaction MUST persist PARTIAL and raise
    ``MergeAmbiguousOutcome`` rather than fall through to
    ``local_main_sha`` substitution.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        self.repo = self.tmpdir / "repo"
        self.state = self.tmpdir / "state"
        self.evidence = self.tmpdir / "evidence"
        for d in (self.repo, self.state, self.evidence):
            d.mkdir()
        self.feature_sha, self.local_main_sha = _init_git_repo(self.repo)
        self.authorized_head = self.feature_sha

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_oid_with_merged_true_raises_merge_ambiguous_outcome(self) -> None:
        """Server reports merged=true but no OID. Production MUST
        NOT proceed to reconciliation against local_main_sha.
        """
        paths = _write_artifacts(self.evidence, self.authorized_head)
        inputs = _make_inputs(paths, self.repo, self.state, self.evidence, self.authorized_head)
        fake = _fake_subprocess_with_oid("", self.authorized_head)  # missing
        with mock.patch(
            "autocoder_orchestration.merge_authorization._safe_run",
            side_effect=fake,
        ):
            with self.assertRaises(MergeAmbiguousOutcome) as ctx:
                execute_guarded_merge_transaction(inputs)
        # Verify the exception message.
        self.assertIn("unavailable", str(ctx.exception))
        rec = json.loads(paths["rec"].read_text())
        self.assertEqual(rec["final_state"], "PARTIAL")
        # The PARTIAL record's unavailable_observations MUST
        # contain the OID-missing reason.
        self.assertTrue(
            any("OID" in s or "mergeCommit" in s
                for s in rec.get("unavailable_observations", [])),
            f"unavailable_observations must reference the "
            f"missing OID; got {rec.get('unavailable_observations')}",
        )
        # No local_main_sha substitution in the record.
        self.assertNotEqual(rec["squash_merge_commit"], self.local_main_sha)


class UnreachableServerOidTests(unittest.TestCase):
    """Codex Critical: the server returned an OID that is NOT
    reachable in the local repo (e.g. the OID is from a
    different history, or the local clone is stale). The
    transaction MUST persist PARTIAL and raise
    ``MergeAmbiguousOutcome``.

    Note: when ``_set_bypass_oid_reachability(True)`` (hermetic test
    setup); production MUST NOT call this helper. The bypass
    is private (leading underscore on the field) to flag the
    production intent.
    setup) the gate does NOT trigger on the reachability
    check. The gate IS triggered when
    ``require_oid_reachable=True`` and the OID is missing
    from the local repo. These tests exercise the latter
    path explicitly.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        self.repo = self.tmpdir / "repo"
        self.state = self.tmpdir / "state"
        self.evidence = self.tmpdir / "evidence"
        for d in (self.repo, self.state, self.evidence):
            d.mkdir()
        self.feature_sha, self.local_main_sha = _init_git_repo(self.repo)
        self.authorized_head = self.feature_sha

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unreachable_oid_in_local_repo_raises_merge_ambiguous_outcome(self) -> None:
        """The server returned an OID that is well-formed but
        does NOT exist in the local repo. Production MUST
        persist PARTIAL and raise ``MergeAmbiguousOutcome``.
        ``local_main_sha`` MUST NOT be substituted.
        """
        paths = _write_artifacts(self.evidence, self.authorized_head)
        # Build inputs WITH ``require_oid_reachable=True`` so
        # the production reachability check is exercised.
        inputs = MergeTransactionInputs(
            authorization_artifact_path=paths["auth"],
            candidate_artifact_path=paths["cand"],
            verifier_artifact_path=paths["ver"],
            merge_record_artifact_path=paths["rec"],
            repository_checkout=self.repo,
            run_state_root=self.state,
            evidence_root=self.evidence,
            live_pr_payload={
                "state": "open", "merged": False,
                "head": {"sha": self.authorized_head},
                "baseRefName": "main", "mergeable": "MERGEABLE",
                "autoMergeRequest": None,
                "reviewDecision": "APPROVED",
                "repo": "owner/repo",
            },
            live_ci_state={"all_required_passing": True, "coderabbit_passing": True},
            live_review_state={"latest_coderabbit_state": "APPROVED"},
            live_thread_inventory={"unresolved_current": 0, "unresolved_outdated": 0},
            working_tree_clean=True,
            required_ci_names=(),
        )
        # Round-27 P1#4: the reachability check is MANDATORY
        # by default. ``require_oid_reachable=True`` (the
        # production invariant) is the default; do NOT bypass.
        inputs._set_live_fetchers(_build_default_live_fetchers(inputs, review_commit_oid=self.authorized_head))
        # Round-28 P3: override the review fetcher so the
        # latest_coderabbit_commit_oid matches the authorized
        # head, otherwise the round-28 P3 exact-head binding
        # fires BEFORE the OID gate (which is what this test
        # wants to exercise).
        fetchers = inputs._live_fetchers
        if fetchers is not None:
            original = fetchers["review_state"]
            def _review_with_oid() -> dict:
                data = original()
                data["latest_coderabbit_commit_oid"] = self.authorized_head
                data["reviews"] = [
                    {**r, "commit_oid": self.authorized_head}
                    for r in data["reviews"]
                ]
                return data
            fetchers["review_state"] = _review_with_oid
        # A well-formed but unreachable OID. The repo has no
        # object with this SHA.
        unreachable_oid = "9" * 40
        fake = _fake_subprocess_with_oid(unreachable_oid, self.authorized_head)
        with mock.patch(
            "autocoder_orchestration.merge_authorization._safe_run",
            side_effect=fake,
        ):
            with self.assertRaises(MergeAmbiguousOutcome):
                execute_guarded_merge_transaction(inputs)
        # PARTIAL record was persisted BEFORE the raise.
        rec = json.loads(paths["rec"].read_text())
        self.assertEqual(rec["final_state"], "PARTIAL")
        # No local_main_sha substitution in the PARTIAL record.
        self.assertNotEqual(rec["squash_merge_commit"], self.local_main_sha)
        # And the helper itself returned None for the
        # unreachable OID.
        oid = _fetch_and_validate_merge_oid(
            gh_executable="gh",
            repo="owner/repo",
            pr_number=3,
            repository_checkout=self.repo,
            require_oid_reachable=True,
        )
        self.assertIsNone(oid,
            f"_fetch_and_validate_merge_oid must return None "
            f"for an unreachable OID; got {oid!r}")


class OidGateHelperTests(unittest.TestCase):
    """Direct unit tests of ``_fetch_and_validate_merge_oid``."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        self.repo = self.tmpdir / "repo"
        self.repo.mkdir()
        _init_git_repo(self.repo)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_returns_none_for_missing_oid(self) -> None:
        """A ``mergeCommit: null`` response MUST return None.
        """
        # Use a head SHA that we'll inject into the refetch
        # response so the round-26 P1#4 gate is exercised but
        # does not block the test (the gate only fires on
        # positive divergence).
        head = "a" * 40
        fake = _fake_subprocess_with_oid("", head)
        with mock.patch(
            "autocoder_orchestration.merge_authorization._safe_run",
            side_effect=fake,
        ):
            oid = _fetch_and_validate_merge_oid(
                gh_executable="gh",
                repo="owner/repo",
                pr_number=3,
                repository_checkout=self.repo,
            )
        self.assertIsNone(oid)

    def test_returns_none_for_malformed_oid(self) -> None:
        head = "a" * 40
        fake = _fake_subprocess_with_oid("not-hex", head)
        with mock.patch(
            "autocoder_orchestration.merge_authorization._safe_run",
            side_effect=fake,
        ):
            oid = _fetch_and_validate_merge_oid(
                gh_executable="gh",
                repo="owner/repo",
                pr_number=3,
                repository_checkout=self.repo,
            )
        self.assertIsNone(oid)

    def test_returns_none_for_unreachable_oid_when_required(self) -> None:
        """An OID that is well-formed but not in the local
        repo returns None when ``require_oid_reachable=True``.
        """
        head = "a" * 40
        fake = _fake_subprocess_with_oid("9" * 40, head)
        with mock.patch(
            "autocoder_orchestration.merge_authorization._safe_run",
            side_effect=fake,
        ):
            oid = _fetch_and_validate_merge_oid(
                gh_executable="gh",
                repo="owner/repo",
                pr_number=3,
                repository_checkout=self.repo,
                require_oid_reachable=True,
            )
        self.assertIsNone(oid)

    def test_returns_oid_when_reachable_and_required(self) -> None:
        """A well-formed OID that is in the local repo returns
        the OID string when ``require_oid_reachable=True``.
        """
        run = subprocess.run
        head_sha = run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        fake = _fake_subprocess_with_oid(head_sha, head_sha)
        with mock.patch(
            "autocoder_orchestration.merge_authorization._safe_run",
            side_effect=fake,
        ):
            oid = _fetch_and_validate_merge_oid(
                gh_executable="gh",
                repo="owner/repo",
                pr_number=3,
                repository_checkout=self.repo,
                require_oid_reachable=True,
            )
        self.assertEqual(oid, head_sha)


if __name__ == "__main__":
    unittest.main()
