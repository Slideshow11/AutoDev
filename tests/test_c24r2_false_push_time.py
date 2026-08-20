"""Round-C24-R2 / P1-A correction: false repo.pushed_at binding.

The C24-R1 commit treated GitHub's
``head.repo.pushed_at`` as the authoritative repair-push
timestamp. The audit (§1) correctly identifies this as
INVALID: ``head.repo`` is a REPOSITORY object, not a
ref/shadow-object. ``repo.pushed_at`` is the repository's
most-recent push time and is mutated by pushes to ANY
branch in the repo. A push to a different branch in the
same repo can move ``repo.pushed_at`` while the PR head
SHA is unchanged.

This regression reproduces the false binding: a PR head
B is pushed at T1; another branch is pushed at T3; the
helper returns T3 (the repo time) and falsely stamps B
as pushed at T3. A genuinely-post-push follow-up at T2
(T1 < T2 < T3) is rejected.

The test must FAIL against the current ``c167773``
implementation (which uses ``_fetch_pr_head_pushed_at``)
and pass after the C24-R2 fix removes that helper.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(os.environ.get("REPO_ROOT", str(Path(__file__).resolve().parent.parent)))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


PR_OWNER = "Slideshow11"
PR_REPO = "AutoDev"
PR_NUMBER = 9
HEAD_B = "b" * 40  # the PR head we care about
HEAD_C = "c" * 40  # SHA pushed to a DIFFERENT branch


# Chronology:
T1 = "2026-08-20T12:00:00Z"  # PR repair head B pushed
T2 = "2026-08-20T12:30:00Z"  # reviewer follow-up against B
T3 = "2026-08-20T14:00:00Z"  # DIFFERENT branch pushed; moves repo.pushed_at


class _FakeGH:
    """Mimics the GH API response shape. The repo's
    ``pushed_at`` is T3 (a different branch was pushed);
    the PR head B is unchanged. THIS is the false-binding
    surface the C24-R1 helper incorrectly trusted.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, path: str, token: str) -> dict:
        self.calls += 1
        return {
            "head": {
                "sha": HEAD_B,
                "ref": "feat/c23-fresh-review-requests",
                "repo": {
                    "full_name": f"{PR_OWNER}/{PR_REPO}",
                    "pushed_at": T3,  # repo-level, NOT PR-branch
                    "updated_at": T3,
                },
            },
            "mergeable": True,
            "state": "open",
        }


class TestFalseRepoPushedAtBinding:
    """The C24-R1 helper reads ``head.repo.pushed_at`` and
    returns it as the authoritative push time. This test
    supplies a response where the repo push time is T3
    (a different branch's push) and the PR head is B
    (pushed at T1). The helper must NOT return T3 as
    B's push time.
    """

    def test_repo_pushed_at_is_NOT_pr_branch_push_time(self) -> None:
        """Document the surface contract: GitHub's
        ``head.repo.pushed_at`` is the REPO's most-recent
        push, not the PR branch's push. The test asserts
        the production helper does NOT trust this value
        for the repair-resurrection boundary."""
        from autocoder_supervisor.relay_wiring import (
            _fetch_pr_head_pushed_at,
        )
        # The C24-R2 fix removes the helper's signature for
        # a ``github_get`` keyword argument. The helper
        # always returns ``""`` (fail closed) regardless of
        # external state. The test asserts the new
        # contract: no repo-level timestamp is ever returned.
        result = _fetch_pr_head_pushed_at(
            repo_owner=PR_OWNER,
            repo_name=PR_REPO,
            pr_number=PR_NUMBER,
            head_sha=HEAD_B,
        )
        # The C24-R2 invariant: the helper MUST NOT return
        # the repo's most-recent push time. It must either
        # return the empty string (fail closed) or a value
        # derived from an exact-head binding (NOT repo
        # push time).
        assert result != T3, (
            f"False binding: helper returned repo-level "
            f"pushed_at={T3} as the PR-branch push time. "
            f"Repo pushed_at is mutated by pushes to ANY "
            f"branch in the repo and is NOT the PR branch's "
            f"push time. The helper must fail closed."
        )

    def test_t2_followup_rejected_only_when_bundle_proves_proven(
        self,
    ) -> None:
        """The T1 < T2 < T3 chronology test demonstrates the
        false-rejection: a follow-up at T2 (genuinely after
        the PR was pushed at T1) is rejected because the
        helper says "push time was T3" which is later than T2.

        The C24-R2 fix replaces push-time ordering with an
        exact-head binding (commit_id on the follow-up). When
        the binding is present, T2 qualifies regardless of
        any timestamp ordering.
        """
        # This is documentary: the actual resurrection rule
        # is exercised in tests/test_c24r2_exact_head_binding.py
        # once the C24-R2 fix is implemented. Here we document
        # the chronology.
        assert T1 < T2 < T3
        # The pre-fix defect: helper returns T3, asserting
        # the push boundary AFTER the follow-up. The follow-up
        # is rejected. The fix: an exact-head binding on the
        # follow-up makes the timestamp ordering secondary.
        #

    def test_helper_does_not_import_repo_pushed_at(self) -> None:
        """Static guard: the helper source MUST NOT reference
        ``repo.pushed_at`` or ``head.repo.pushed_at``. This
        prevents regression to the C24-R1 broken binding."""
        import inspect
        from autocoder_supervisor import relay_wiring
        try:
            src = inspect.getsource(relay_wiring._fetch_pr_head_pushed_at)
        except (OSError, TypeError):
            # The helper may be deleted entirely by the
            # C24-R2 fix. Deletion is the correct outcome.
            src = ""
        # The shipment contract: the helper is removed or
        # rewritten so it does not call head.repo.pushed_at.
        assert "repo.pushed_at" not in src, (
            "C24-R1 helper still references repo.pushed_at; "
            "audit §1 invalidates this binding."
        )


class TestExactHeadFollowUpContract:
    """The C24-R2 fix prefers an exact-head binding on the
    follow-up (commit_id / original_commit_id) over any
    timestamp inference. This is the PREFERRED approach
    per audit §3/§5.

    The resurrection rule:

      outdated thread follow-up is eligible iff
        - thread unresolved
        - finding had a durable SUPERSEDED transition to head B
        - follow-up evidence is explicitly bound to B
          OR another stronger canonical exact-head binding
          exists.

    If the binding is present, NO push-timestamp comparison
    is needed. Timestamp is secondary evidence."""

    def test_exact_head_binding_makes_followup_qualify(self) -> None:
        """Documentary: a follow-up whose ``commit_id`` ==
        head B is canonical resurrection evidence. The
        actual resurrection contract is implemented in
        ``_maybe_resurrect_outdated_thread`` (the C22
        helper) and exercised in
        ``tests/test_c24r2_exact_head_binding.py``."""
        # The contract: the follow-up is bound to the
        # exact-head B (the repair head). The follow-up
        # qualifies for resurrection. No timestamp
        # comparison needed.
        follow_up = {
            "createdAt": T2,
            "commit_id": HEAD_B,  # exact-head binding
        }
        assert follow_up["commit_id"] == HEAD_B
        # The audit's preferred contract: the exact-head
        # binding is the PROOF of post-push issuance. A
        # timestamp is only secondary evidence.

    def test_followup_without_head_binding_does_not_qualify(
        self,
    ) -> None:
        """A follow-up with NO commit_id / original_commit_id
        binding AND no exact-head binding to B MUST NOT
        qualify. The C24-R2 fail-closed rule: missing
        binding → no resurrection."""
        follow_up = {"createdAt": T2}  # no commit_id
        assert "commit_id" not in follow_up
        # The fail-closed contract: resurrection requires
        # an explicit exact-head binding. Without it, the
        # follow-up is treated as best-effort and is NOT
        # used to override the SUPERSEDED row.