"""Round-658 P1 regression: ``_collect_coderabbit_exact_head_surfaces`` must read
CodeRabbit authors from BOTH production snapshot shapes.

Fresh evidence from the canonical writer (``capture_live_snapshot`` /
``collect_provider_surfaces``):

- ``issue_comments`` carry the author as a top-level ``login`` string
  (the writer collapses ``c["user"]["login"]`` to a top-level field).
- ``review_comments`` (inline comments) omit the ``user`` field entirely;
  only path/line/body are propagated to the surface blob.

The earlier reader assumed a nested ``(c.get("user") or {}).get("login")``
shape for both surfaces, which silently dropped every real CodeRabbit
comment from the relay's head assessment.  These tests pin the
behavior at three layers:

1. The author-login resolver accepts nested dict, top-level string,
   top-level ``login``, and the legacy fallback keys.
2. Inline ``review_comments`` with no ``user`` field are still recorded
   under ``inline_comments_collected`` (the surface presence is what
   matters, not the author fingerprint).
3. ``issue_comments`` whose author lands at top-level ``login`` are
   correctly attributed and recorded under both
   ``top_level_comment_collected`` and the head-prefix completion
   fallback.
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


EXPECTED_HEAD = "6e3fcfbdaff706a9496bcff902022b94210474c4"
HEAD_PREFIX = EXPECTED_HEAD[:7]


def _eval(snap):
    return _collect_coderabbit_exact_head_surfaces(
        snap, expected_head=EXPECTED_HEAD
    )


def test_issue_comment_top_level_login_is_attributed():
    """Issue-comment schema is top-level login (production shape)."""
    snap = {
        "formal_reviews": [],
        "review_comments": [],
        "issue_comments": [
            {
                "id": 9001,
                "login": "coderabbitai[bot]",
                "body": (
                    f"Reviewing {HEAD_PREFIX} — completed; all "
                    f"findings addressed at head {HEAD_PREFIX}."
                ),
            }
        ],
        "review_threads": {},
    }
    out = _eval(snap)
    assert out["top_level_comment_collected"] is True
    assert out["statuses_collected"] is True
    assert out["completion_proof"]["exact_head_status"] == "success"


def test_inline_review_comment_without_user_field_is_collected():
    """Inline schema omits user entirely; the surface presence must register."""
    snap = {
        "formal_reviews": [],
        "review_comments": [
            {
                "id": 9002,
                "path": "autocoder_supervisor/hermes_fingerprint.py",
                "line": 1099,
                "body": "Inline review comment from CodeRabbit.",
                # No ``user`` field — production canonical-writer
                # behavior (supervisor.py:9396-9401).
            }
        ],
        "issue_comments": [],
        "review_threads": {},
    }
    out = _eval(snap)
    assert out["inline_comments_collected"] is True


def test_nested_user_login_shape_legacy_still_works():
    """Legacy nested-(user)-dict shape must continue to work."""
    snap = {
        "formal_reviews": [],
        "review_comments": [
            {
                "id": 9003,
                "user": {"login": "coderabbitai[bot]"},
                "body": "Inline comment.",
                "path": "x.py",
                "line": 1,
            }
        ],
        "issue_comments": [
            {
                "id": 9004,
                "user": {"login": "coderabbitai[bot]"},
                # Canonical completion-verbatim that the
                # production ``exact_head_status == "success"``
                # classifier recognizes.
                "body": f"Review completed at {HEAD_PREFIX}.",
            }
        ],
        "review_threads": {},
    }
    out = _eval(snap)
    assert out["inline_comments_collected"] is True
    assert out["top_level_comment_collected"] is True
    assert out["statuses_collected"] is True
    assert out["completion_proof"]["exact_head_status"] == "success"


def test_inline_unattributed_records_still_register_surface_presence():
    """Unattributed inline records (no ``user`` field at all) — the
    production canonical-writer shape from supervisor.py:9396-9401 —
    must register as a collected surface. The snapshot collector
    already filtered by ``commit_id == head_sha``, so presence alone
    is sufficient evidence for inline comments."""
    snap = {
        "formal_reviews": [],
        "review_comments": [
            {
                "id": 9005,
                # No ``user`` field — production canonical-writer
                # shape. ``path`` / ``line`` / ``body`` only.
                "path": "x.py",
                "line": 1,
                "body": "Plain inline review with no author field.",
            }
        ],
        "issue_comments": [],
        "review_threads": {},
    }
    out = _eval(snap)
    assert out["inline_comments_collected"] is True
    assert out["inline_comments_unattributed_count"] == 1
    assert out["inline_comments_attributed_count"] == 0
