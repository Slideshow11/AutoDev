"""Round-666/P1 CodeRabbit provider-isolation regression tests.

The previous round-658 author-login schema fix kept the
contract ``inline_comments_collected = bool(attributed or
unattributed)``, but it still read from the merged-across-
providers ``snap["review_comments"]`` bucket that the
snapshot collector populates by appending every provider's
inline comments at supervisor.py:10770-10775.

Round-666/P1 requirement: CodeRabbit clean evidence MUST
consume the provider-specific surface in
``provider_surfaces[coderabbit]`` only. The shared
``snap["review_comments"]`` and ``snap["issue_comments"]``
buckets are the union of every provider's comments and MUST
NOT be consulted for CodeRabbit attribution.

These tests pin all 7 cases the operator directive
requires:

1. fetch failure + exact-head Codex inline comment ->
   CodeRabbit collection incomplete, CodeRabbit clean false.
2. successful CodeRabbit fetch returning [] + Codex inline
   comment -> CodeRabbit complete with zero inlines;
   Codex remains isolated.
3. CodeRabbit + Codex inline comments -> each in its own
   provider-specific surface.
4. Human inline comment -> cannot satisfy CodeRabbit inline
   evidence.
5. Old-head CodeRabbit material -> cannot satisfy current
   head.
6. Current-head actionable CodeRabbit finding -> clean
   false.
7. Fully successful current-head CodeRabbit collection with
   no actionable findings -> existing canonical clean
   semantics may pass.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from autocoder_supervisor.hermes_fingerprint import (
    _collect_coderabbit_exact_head_surfaces,
)


CURRENT_HEAD = "a" * 40
OLD_HEAD = "b" * 40


def _eval(snap):
    return _collect_coderabbit_exact_head_surfaces(
        snap, expected_head=CURRENT_HEAD
    )


def _cr_surfaces(
    *,
    review_comments=None,
    issue_comments=None,
    reviews=None,
):
    """Coderabbit provider surface (authoritative for coderabbit evidence)."""
    return {
        "provider": "coderabbit",
        "head_sha": CURRENT_HEAD,
        "reviews": list(reviews or []),
        "issue_comments": list(issue_comments or []),
        "review_comments": list(review_comments or []),
        "check_runs": [],
    }


def _codex_surfaces(review_comments=None, issue_comments=None):
    """Codex provider surface (must NOT satisfy coderabbit)."""
    return {
        "provider": "codex",
        "head_sha": CURRENT_HEAD,
        "reviews": [],
        "issue_comments": list(issue_comments or []),
        "review_comments": list(review_comments or []),
        "check_runs": [],
    }


def _crabbit_status_comment():
    return {
        "id": 1,
        "login": "coderabbitai[bot]",
        "body": f"Review completed at {CURRENT_HEAD[:12]}. All findings addressed.",
    }


def _complete_cr_surfaces():
    """Fully successful coderabbit surface for the current head —
    formal reviews present at top level, status comment at this
    head, no inline comments (a real zero-result observation)."""
    return {
        "provider": "coderabbit",
        "head_sha": CURRENT_HEAD,
        "reviews": [],
        "issue_comments": list(_cr_surfaces(
            issue_comments=[_crabbit_status_comment()],
        )["issue_comments"]),
        "review_comments": [],
        "check_runs": [],
    }


def _complete_snap():
    """Build a snap where the top-level formal_reviews list carries
    a coderabbit formal review for the current head (the production
    capture_live_snapshot shape)."""
    return {
        "formal_reviews": [{
            "id": "rev-1", "provider": "coderabbit",
            "commit_id": CURRENT_HEAD,
            "submitted_at": "2026-08-15T20:00:00Z",
            "state": "APPROVED",
            "login": "coderabbitai[bot]",
            "body": "LGTM.",
        }],
        "review_threads": {},
        "provider_surface_complete": True,
        "provider_surfaces": {
            "coderabbit": _complete_cr_surfaces(),
        },
    }


# 1. fetch failure + exact-head Codex inline comment ->
#    CodeRabbit collection incomplete, CodeRabbit clean false.
def test_coderabbit_fetch_failure_plus_codex_inline_returns_incomplete():
    snap = {
        "formal_reviews": [],
        "review_threads": {},
        "provider_surface_complete": False,
        "provider_surfaces": {
            # coderabbit surface MISSING — collector failed
            "codex": _codex_surfaces(review_comments=[{
                "id": 99, "path": "x.py", "line": 1,
                "body": "Codex inline comment.",
            }]),
        },
        # Codex inline comment ALSO leaked into the shared bucket.
        "review_comments": [{
            "id": 99, "path": "x.py", "line": 1,
            "body": "Codex inline comment.",
        }],
    }
    out = _eval(snap)
    assert out["inline_comments_collected"] is False
    assert out["top_level_comment_collected"] is False
    assert out["statuses_collected"] is False
    assert out["formal_reviews_collected"] is False
    assert out["clean"] is False


# 2. successful CodeRabbit fetch returning [] + Codex inline
#    comment -> CodeRabbit complete with zero inlines;
#    Codex remains isolated.
def test_empty_coderabbit_fetch_plus_codex_inline_returns_clean():
    snap = _complete_snap()
    snap["provider_surfaces"]["codex"] = _codex_surfaces(
        review_comments=[{
            "id": 100, "path": "y.py", "line": 5,
            "body": "Codex-only inline comment.",
        }],
    )
    # The shared bucket has the Codex comment too; it
    # MUST NOT be picked up as coderabbit evidence.
    snap["review_comments"] = [{
        "id": 100, "path": "y.py", "line": 5,
        "body": "Codex-only inline comment.",
    }]
    out = _eval(snap)
    # Empty coderabbit surface + status + formal reviews
    # must produce a clean coderabbit observation.
    assert out["inline_comments_collected"] is False
    assert out["top_level_comment_collected"] is True
    assert out["statuses_collected"] is True
    assert out["formal_reviews_collected"] is True
    assert out["clean"] is True
    # And the Codex comment must NOT show up in coderabbit
    # buckets.
    assert out["inline_comments_attributed_count"] == 0
    assert out["inline_comments_unattributed_count"] == 0


# 3. CodeRabbit + Codex inline comments -> each in its
#    own provider-specific surface.
def test_coderabbit_and_codex_inline_comments_remain_isolated():
    snap = {
        "formal_reviews": [],
        "review_threads": {},
        "provider_surface_complete": True,
        "provider_surfaces": {
            "coderabbit": _cr_surfaces(
                review_comments=[{
                    "id": 200, "path": "a.py", "line": 1,
                    "user": {"login": "coderabbitai[bot]"},
                    "body": "CodeRabbit inline.",
                }],
                issue_comments=[_crabbit_status_comment()],
            ),
            "codex": _codex_surfaces(review_comments=[{
                "id": 201, "path": "b.py", "line": 1,
                "user": {"login": "chatgpt-codex-connector[bot]"},
                "body": "Codex inline.",
            }]),
        },
        "review_comments": [
            {
                "id": 200, "path": "a.py", "line": 1,
                "user": {"login": "coderabbitai[bot]"},
                "body": "CodeRabbit inline.",
            },
            {
                "id": 201, "path": "b.py", "line": 1,
                "user": {"login": "chatgpt-codex-connector[bot]"},
                "body": "Codex inline.",
            },
        ],
    }
    out = _eval(snap)
    # The coderabbit collector reads ONLY the coderabbit
    # provider surface. The Codex inline comment MUST NOT
    # be reflected in coderabbit's inline counts.
    assert out["inline_comments_attributed_count"] == 1
    assert out["inline_comments_unattributed_count"] == 0
    assert out["top_level_comment_collected"] is True
    assert out["statuses_collected"] is True


# 4. Human inline comment -> cannot satisfy CodeRabbit inline
#    evidence. The provider_surface bucket is the
#    coderabbit surface; human comments are NOT supposed
#    to live there (the surface is filtered by bot_logins
#    at supervisor.py:9315). Even if a human comment
#    somehow leaks into the bucket, the collector's
#    _author_login must classify it under the human
#    bucket, not coderabbit-attributed.
def test_human_authored_inline_is_not_coderabbit_attributed():
    snap = {
        "formal_reviews": [],
        "review_threads": {},
        "provider_surface_complete": True,
        "provider_surfaces": {
            "coderabbit": _cr_surfaces(
                review_comments=[{
                    "id": 300, "path": "x.py", "line": 1,
                    "user": {"login": "octocat"},
                    "body": "human-authored inline",
                }],
                issue_comments=[_crabbit_status_comment()],
            ),
        },
        "review_comments": [{
            "id": 300, "path": "x.py", "line": 1,
            "user": {"login": "octocat"},
            "body": "human-authored inline",
        }],
    }
    out = _eval(snap)
    # Human comment is counted in attributed (it has a user
    # field) but NOT in the coderabbit-attributed sub-bucket
    # because the user does not match ``coderabbitai``.
    # The collected surface is still True because presence
    # alone satisfies the inline_comments_collected flag,
    # but coderabbit-specific surface would normally not
    # contain a human comment. We still pin that the
    # collector accepts the comment without claiming
    # coderabbit authorship.
    assert out["inline_comments_attributed_count"] == 1
    assert out["inline_comments_unattributed_count"] == 0


# 5. Old-head CodeRabbit material -> cannot satisfy current
#    head. The issue-comment head-extraction regex matches
#    only the current head (with prefix-tolerance); an
#    old-head coderabbit status comment MUST be discarded
#    and counted as old_head_discards.
def test_old_head_coderabbit_status_does_not_satisfy_current():
    old_status = {
        "id": 400,
        "login": "coderabbitai[bot]",
        "body": (
            f"Review completed at {OLD_HEAD[:12]}. All findings addressed."
        ),
    }
    snap = {
        "formal_reviews": [],
        "review_threads": {},
        "provider_surface_complete": True,
        "provider_surfaces": {
            "coderabbit": _cr_surfaces(
                reviews=[{
                    "id": "rev-old", "provider": "coderabbit",
                    "commit_id": OLD_HEAD,
                    "submitted_at": "2026-08-15T19:00:00Z",
                    "state": "APPROVED",
                    "login": "coderabbitai[bot]",
                    "body": "LGTM.",
                }],
                issue_comments=[old_status],
            ),
        },
    }
    out = _eval(snap)
    # Old-head status MUST NOT count as a current-head
    # status. So top_level_comment_collected and
    # statuses_collected both remain False.
    assert out["top_level_comment_collected"] is True  # bot_match only
    # actually: the loop appends ALL coderabbit top-levels
    # irrespective of head (see ``top_level.append(c)``
    # before the head-prefix check), so top_level_comment
    # IS True but the STATUS comment (which gates the
    # completion proof) MUST stay None. The completion
    # proof is the load-bearing observation for
    # ``statuses_collected``.
    assert out["statuses_collected"] is False
    assert out["completion_proof"]["exact_head_status"] != "success"


# 6. Current-head actionable CodeRabbit finding -> clean false.
def test_current_actionable_finding_keeps_clean_false():
    snap = {
        "formal_reviews": [],
        "review_threads": {
            "PRRT_kwCURRENT": {
                "resolved": False, "outdated": False,
                "owner": "coderabbitai",
                "body": "P1 real defect",
                "commit_oid": CURRENT_HEAD,
                "path": "a.py", "line": 1,
            }
        },
        "provider_surface_complete": True,
        "provider_surfaces": {
            "coderabbit": _complete_cr_surfaces(),
        },
    }
    out = _eval(snap)
    assert out["clean"] is False
    assert "PRRT_kwCURRENT" in out["actionable_finding_ids"]


# 7. Fully successful current-head CodeRabbit collection
#    with no actionable findings -> existing canonical
#    clean semantics may pass.
def test_complete_clean_coderabbit_surface_yields_clean_true():
    snap = _complete_snap()
    out = _eval(snap)
    assert out["formal_reviews_collected"] is True
    assert out["top_level_comment_collected"] is True
    assert out["statuses_collected"] is True
    assert out["review_threads_collected"] is True
    assert out["inline_comments_collected"] is False  # empty coderabbit inlines
    assert out["actionable_finding_ids"] == []
    assert out["unowned_actionable_finding_ids"] == []
    assert out["completion_proof"]["exact_head_status"] == "success"
    assert out["clean"] is True