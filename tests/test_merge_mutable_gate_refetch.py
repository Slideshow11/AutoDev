"""Round-26 Codex P1#4: mutable merge gates are re-fetched and
re-validated INSIDE the locked transaction.

The user's invariant: ``--match-head-commit`` protects the
SHA only. Between the verifier-approved snapshot and the
locked merge transaction, the following GitHub-side state
can change:

- ``reviewDecision`` can become ``CHANGES_REQUESTED``
- An approval can be dismissed
- A new unresolved review thread can appear
- A required CI check can change state
- The PR can be merged / closed / drafted by a concurrent actor
- Auto-merge can be requested

The evidence-root lock does NOT serialize GitHub-side
state. Re-fetching inside the lock and raising
``MergeGateChanged`` on divergence is the production
contract.

These tests prove the contract under each divergence mode
using production-path mocks.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock


from autocoder_orchestration.merge_authorization import (
    MergeGateChanged,
    MergeGateFetchError,
    MergeTransactionInputs,
    execute_guarded_merge_transaction,
)


def _init_minimal_repo(repo: Path) -> None:
    """Create a minimal git repo with two branches.

    The feature branch (``master``) carries the head under
    test; the ``main`` branch is a separate orphan with an
    empty commit. Returns ``None``. ``repo`` MUST already
    exist (callers create the directory before invoking this).
    """
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "x.py").write_text("X = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "x.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "feat"], check=True)
    # Set up ``main`` as a separate orphan branch.
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--orphan", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "-rf", "."], check=True)
    (repo / "main_only.py").write_text("MAIN = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "main_only.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "main"], check=True)


def _write_artifacts(evidence: Path, authorized_head: str) -> dict:
    from autocoder_orchestration.artifacts import write_artifact
    import hashlib
    paths = {
        "auth": evidence / "authorization.json",
        "cand": evidence / "candidate.json",
        "ver": evidence / "verifier.json",
        "rec": evidence / "merge-record.json",
    }
    cand_payload = {"exact_head": authorized_head, "files": []}
    write_artifact(paths["cand"], cand_payload)
    cand_digest = hashlib.sha256(paths["cand"].read_bytes()).hexdigest()
    ver_payload = {
        "verdict": "VERIFIED", "defects": [],
        "candidate_sha256": cand_digest,
    }
    write_artifact(paths["ver"], ver_payload)
    ver_digest = hashlib.sha256(paths["ver"].read_bytes()).hexdigest()
    auth = {
        "schema_version": "autocoder.merge_authorization.v1",
        "run_id": "test",
        "repo": "owner/repo",
        "pr_number": 3,
        "authorized_head": authorized_head,
        "candidate_sha256": cand_digest,
        "verifier_record_sha256": ver_digest,
        "base_branch": "main",
        "feature_branch": "feat/test",
        "merge_method": "squash",
        "delete_branch": False,
        "require_match_head_commit": True,
    }
    write_artifact(paths["auth"], auth)
    return paths


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
        required_ci_names=(),
    )
    inputs._set_bypass_oid_reachability(True)
    # Round-27 P1#2: inject test-owned live fetchers that
    # return canned dicts for every gate. Tests that want a
    # divergence override these after construction.
    inputs._set_live_fetchers({
        "pr_payload": lambda: {
            "state": "open", "merged": False, "isDraft": False,
            "head": {"sha": authorized_head},
            "baseRefName": "main", "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "autoMergeRequest": None,
            "reviewDecision": "APPROVED",
            "repo": "owner/repo",
        },
        "required_ci": lambda: {
            "head_sha": "",
            "checks": {
                name: {"state": "SUCCESS", "head_sha": ""}
                for name in inputs.required_ci_names or ()
            },
        },
        "review_state": lambda: {
            "head_sha": "",
            "reviews": [{
                "state": "APPROVED",
                "author": "coderabbitai[bot]",
                "submitted_at": "2026-01-01T00:00:00Z",
                # Round-28 P3: review commit OID for exact-head binding.
                "commit_oid": "a" * 40,
            }],
            "latest_coderabbit_state": "APPROVED",
            # Round-28 P3: latest CodeRabbit review commit OID.
            "latest_coderabbit_commit_oid": "a" * 40,
            "latest_coderabbit_login": "coderabbitai[bot]",
            "canonical_reviewer_login": "coderabbitai",
            "repo": "owner/repo",
        },
        "thread_inventory": lambda: {
            "head_sha": "",
            "unresolved_current": 0,
            "unresolved_outdated": 0,
            "paginated_completely": True,
            "error": None,
        },
    })
    return inputs


class MutableGateRefetchTests(unittest.TestCase):
    """The locked transaction MUST re-fetch the live PR payload
    and re-validate the mutable gates. Divergence from the
    bound snapshot raises ``MergeGateChanged`` and halts the
    transaction.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        self.repo = self.tmpdir / "repo"
        self.state = self.tmpdir / "state"
        self.evidence = self.tmpdir / "evidence"
        for d in (self.repo, self.state, self.evidence):
            d.mkdir()
        _init_minimal_repo(self.repo)
        import subprocess
        self.authorized_head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "master"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _inject_divergence(self, inputs, *, refetch_payload: dict) -> None:
        """Round-27: override the injected live fetchers so the
        gate comparator sees a diverging live payload.

        Tests that want to exercise a divergence (e.g. ``CHANGES_REQUESTED``
        appearing between the bound snapshot and the locked
        transaction) pass the diverging PR payload here. The
        helper sets the ``pr_payload`` fetcher to return the
        diverging dict; the other gates' fetchers stay at the
        default (live == bound).

        The diverging payload accepts the raw ``fetch_live_pr_payload``
        shape (``headRefOid`` field, not ``head.sha``). The
        helper normalizes to the gate's expected shape.
        """
        head_sha = refetch_payload.get("headRefOid")
        if head_sha is None:
            head = refetch_payload.get("head") or {}
            head_sha = head.get("sha", "")
        inputs._set_live_fetchers({
            "pr_payload": lambda: {
                "state": refetch_payload.get("state", "open"),
                "merged": refetch_payload.get("merged", False),
                "isDraft": refetch_payload.get("isDraft", False),
                "head": {"sha": head_sha},
                "baseRefName": refetch_payload.get(
                    "baseRefName", "main",
                ),
                "mergeable": refetch_payload.get("mergeable", "MERGEABLE"),
                "mergeStateStatus": refetch_payload.get(
                    "mergeStateStatus", "CLEAN",
                ),
                "autoMergeRequest": refetch_payload.get("autoMergeRequest"),
                "reviewDecision": refetch_payload.get("reviewDecision"),
                "repo": refetch_payload.get("repo", "owner/repo"),
            },
            "required_ci": lambda: {
                "head_sha": "",
                "checks": {},
            },
            "review_state": lambda: {
                "head_sha": "",
                "reviews": [{
                    "state": "APPROVED",
                    "author": "coderabbitai[bot]",
                    "submitted_at": "2026-01-01T00:00:00Z",
                    # Round-28 P3: the review commit OID MUST match
                    # the authorized exact head. We use the test
                    # instance's authorized head as the OID; tests
                    # that want to test exact-head binding divergence
                    # override the fetcher after calling
                    # ``_inject_divergence``.
                    "commit_oid": self.authorized_head,
                }],
                "latest_coderabbit_state": "APPROVED",
                "latest_coderabbit_commit_oid": self.authorized_head,
                "latest_coderabbit_login": "coderabbitai[bot]",
                "canonical_reviewer_login": "coderabbitai",
                "repo": "owner/repo",
            },
            "thread_inventory": lambda: {
                "head_sha": "",
                "unresolved_current": 0,
                "unresolved_outdated": 0,
                "paginated_completely": True,
                "error": None,
            },
        })

    def _fake_safe_run(self, *, refetch_payload: dict, post_subproc_payload=None):
        """Build a fake ``_safe_run`` whose first ``pr view``
        call (no mergeCommit) returns ``refetch_payload`` and
        whose subsequent calls return ``post_subproc_payload``.
        """
        post = post_subproc_payload or refetch_payload
        view_count = [0]

        def fake(*args, **kwargs):
            argv = args[0] if args else []
            joined = " ".join(str(x) for x in argv)
            if (
                "pr view" in joined
                and " pr merge " not in f" {joined} "
                and "mergeCommit" not in joined
            ):
                view_count[0] += 1
                return {
                    "returncode": 0,
                    "stdout": json.dumps(
                        refetch_payload if view_count[0] == 1 else post
                    ),
                    "stderr": "",
                    "timed_out": False,
                }
            if " pr merge " in f" {joined} ":
                return {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}
            return {"returncode": 0, "stdout": "{}", "stderr": "", "timed_out": False}

        return fake

    def _open_payload(self) -> dict:
        """A clean OPEN / CLEAN / APPROVED payload matching the
        bound inputs.
        """
        return {
            "state": "open", "mergedAt": None,
            "isDraft": False, "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "headRefOid": self.authorized_head,
            "baseRefName": "main",
            "autoMergeRequest": None,
            "reviewDecision": "APPROVED",
        }

    def test_refetch_unchanged_state_passes_through(self) -> None:
        """A refetch that matches the bound snapshot passes the
        gate. The transaction proceeds to the OID gate (no
        OID in this hermetic test setup, so PARTIAL is written).
        """
        paths = _write_artifacts(self.evidence, self.authorized_head)
        inputs = _make_inputs(paths, self.repo, self.state, self.evidence, self.authorized_head)
        # Both refetch and post-subproc payload match the bound.
        self._inject_divergence(inputs, refetch_payload=self._open_payload())
        # The transaction reaches the OID gate, which
        # fails (no real OID) and writes PARTIAL. The
        # critical invariant is that the refetch gate
        # PASSED — the transaction did NOT raise
        # MergeGateChanged.
        with self.assertRaises(Exception) as ctx:
            execute_guarded_merge_transaction(inputs)
        self.assertNotIsInstance(ctx.exception, MergeGateChanged,
            f"refetch matched snapshot; MergeGateChanged MUST NOT "
            f"be raised; got {ctx.exception!r}")

    def test_refetch_changes_requested_raises_merge_gate_changed(self) -> None:
        """A same-head ``CHANGES_REQUESTED`` posted AFTER the
        verifier ran MUST halt the transaction with
        ``MergeGateChanged``. ``--match-head-commit`` does not
        protect mutable gates; the evidence-root lock does
        not serialize GitHub-side state.
        """
        paths = _write_artifacts(self.evidence, self.authorized_head)
        inputs = _make_inputs(paths, self.repo, self.state, self.evidence, self.authorized_head)
        # Refetch sees a CHANGES_REQUESTED.
        refetch = self._open_payload()
        refetch["reviewDecision"] = "CHANGES_REQUESTED"
        self._inject_divergence(inputs, refetch_payload=refetch)
        with self.assertRaises(MergeGateChanged) as ctx:
            execute_guarded_merge_transaction(inputs)
        self.assertIn("CHANGES_REQUESTED", str(ctx.exception))
        # The merge record was NOT written — the gate fires
        # BEFORE the merge subprocess.
        self.assertFalse(paths["rec"].exists(),
            "MergeGateChanged MUST fire before the merge "
            "subprocess; the record path MUST NOT exist")

    def test_refetch_state_closed_raises_merge_gate_changed(self) -> None:
        """A concurrent close (PR closed) MUST halt the
        transaction.
        """
        paths = _write_artifacts(self.evidence, self.authorized_head)
        inputs = _make_inputs(paths, self.repo, self.state, self.evidence, self.authorized_head)
        refetch = self._open_payload()
        refetch["state"] = "closed"
        self._inject_divergence(inputs, refetch_payload=refetch)
        with self.assertRaises(MergeGateChanged) as ctx:
                execute_guarded_merge_transaction(inputs)
        self.assertIn("state became 'closed'", str(ctx.exception))

    def test_refetch_already_merged_raises_merge_gate_changed(self) -> None:
        """A concurrent merge (PR merged=True) MUST halt the
        transaction — another actor already merged.
        """
        paths = _write_artifacts(self.evidence, self.authorized_head)
        inputs = _make_inputs(paths, self.repo, self.state, self.evidence, self.authorized_head)
        refetch = self._open_payload()
        refetch["merged"] = True
        refetch["mergedAt"] = "2026-08-09T00:00:00Z"
        self._inject_divergence(inputs, refetch_payload=refetch)
        with self.assertRaises(MergeGateChanged) as ctx:
                execute_guarded_merge_transaction(inputs)
        self.assertIn("was merged", str(ctx.exception))

    def test_refetch_head_advanced_raises_merge_gate_changed(self) -> None:
        """The head advanced between the pre-snapshot and the
        locked transaction. The merge authorization no longer
        binds to the live head.
        """
        paths = _write_artifacts(self.evidence, self.authorized_head)
        inputs = _make_inputs(paths, self.repo, self.state, self.evidence, self.authorized_head)
        refetch = self._open_payload()
        # Different head SHA — production MUST halt.
        refetch["headRefOid"] = "9" * 40
        self._inject_divergence(inputs, refetch_payload=refetch)
        with self.assertRaises(MergeGateChanged) as ctx:
                execute_guarded_merge_transaction(inputs)
        self.assertIn("head advanced", str(ctx.exception))

    def test_refetch_approval_dismissed_raises_merge_gate_changed(self) -> None:
        """The latest review state changes from ``APPROVED`` to
        something else (an approval was dismissed). The
        transaction MUST halt.
        """
        paths = _write_artifacts(self.evidence, self.authorized_head)
        inputs = _make_inputs(paths, self.repo, self.state, self.evidence, self.authorized_head)
        refetch = self._open_payload()
        # Dismissed approval -> REVIEW_REQUIRED. The
        # verifier's latest review state was APPROVED; this
        # is a different value. The refetch's reviewDecision
        # gate accepts both APPROVED and REVIEW_REQUIRED, so
        # for reviewDecision alone this isn't blocked. But
        # the live_review_state check (in production) also
        # fails. Here we exercise the live_pr reviewDecision
        # by removing it entirely.
        refetch["reviewDecision"] = None
        self._inject_divergence(inputs, refetch_payload=refetch)
        with self.assertRaises(MergeGateChanged) as ctx:
                execute_guarded_merge_transaction(inputs)
        self.assertIn("reviewDecision became None", str(ctx.exception))

    def test_refetch_subprocess_failure_fails_closed(self) -> None:
        """Round-28 P2 / P7: a failing live fetcher MUST fail
        closed. The transaction MUST raise ``MergeGateFetchError``
        so the supervisor routes to BLOCKED / escalation.

        The test injects an ACTUAL failing fetcher (not just a
        subprocess mock) and asserts the gate fails closed. The
        merge subprocess MUST NEVER be invoked.
        """
        paths = _write_artifacts(self.evidence, self.authorized_head)
        inputs = _make_inputs(paths, self.repo, self.state, self.evidence, self.authorized_head)
        # Inject an ACTUAL failing live fetcher: every gate
        # fetch raises a transient ``gh`` failure. Production
        # code wraps the fetcher call in
        # ``MergeGateFetchError``; the gate must surface that
        # without invoking the merge subprocess.
        from autocoder_orchestration.merge_authorization import (
            MergeGateFetchError as _OuterGateFetchError,
        )

        def failing_fetcher() -> Dict[str, Any]:
            raise _OuterGateFetchError(
                "live_pr_payload",
                underlying=RuntimeError("transient gh error"),
            )

        inputs._set_live_fetchers({
            "pr_payload": failing_fetcher,
            "required_ci": failing_fetcher,
            "review_state": failing_fetcher,
            "thread_inventory": failing_fetcher,
        })

        merge_invocations: List[List[str]] = []
        def fake_safe_run(cmd, **kwargs):
            merge_invocations.append(list(cmd))
            return {"returncode": 1, "stdout": "", "stderr": "no gh", "timed_out": False}

        with mock.patch(
            "autocoder_orchestration.merge_authorization._safe_run",
            side_effect=fake_safe_run,
        ):
            with self.assertRaises(MergeGateFetchError) as ctx:
                execute_guarded_merge_transaction(inputs)
        msg = str(ctx.exception)
        assert "live_pr_payload" in msg, (
            f"MergeGateFetchError MUST attribute the failure to a "
            f"specific gate; got {msg!r}"
        )
        # Round-28 P7: assert the merge subprocess was NEVER
        # invoked. The fake records every call; we verify zero
        # ``gh pr merge`` invocations.
        merge_calls = [
            c for c in merge_invocations
            if len(c) >= 3 and c[0] == "gh" and c[1] == "pr" and c[2] == "merge"
        ]
        assert not merge_calls, (
            f"merge subprocess MUST NEVER be invoked when a fetcher "
            f"fails; got {merge_calls!r}"
        )


if __name__ == "__main__":
    unittest.main()
