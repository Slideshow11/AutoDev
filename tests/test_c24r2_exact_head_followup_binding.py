"""Round-C24-R2 / Exact-head follow-up binding contract.

The audit (§3 §5) prefers a non-time-based identity contract
for resurrection. The canonical exact-head binding is the
follow-up comment's ``commit_id`` field (GitHub's
``inline_review_comments`` and ``issue_comments`` REST API).

A follow-up qualifies for resurrection when:

  1. The thread is unresolved AND outdated.
  2. The FindingLedger has a ``SUPERSEDED`` row for the
     prior finding identity with ``superseded_by_head``
     equal to the new repair head.
  3. The follow-up's ``commit_id`` (or ``original_commit_id``)
     equals ``superseded_by_head`` (the new head).
  4. The follow-up is non-operator, actionable.

When the binding is established, the strict timestamp
comparison in ``_c22_is_followup_eligible`` is RELAXED:
the follow-up qualifies regardless of the
``superseded_at``/``createdAt`` comparison.

When the binding is NOT present, the legacy timestamp
comparison remains in force.

This test pins the contract.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(os.environ.get("REPO_ROOT", str(Path(__file__).resolve().parent.parent)))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from autocoder_orchestration.review_repair_relay import (  # noqa: E402
    _maybe_resurrect_outdated_thread,
)


HEAD_OLD = "a" * 40
HEAD_NEW = "b" * 40

OLD_TS = "2026-08-20T12:00:00Z"  # superseded_at on prior head
FOLLOWUP_TS = "2026-08-20T13:00:00Z"  # follow-up createdAt (AFTER head advance)

NON_OPERATOR_LOGINS = ()


def _build_thread(
    *,
    outdated: bool,
    resolved: bool,
    followup_commit_id: str | None,
    followup_original_commit_id: str | None = None,
    followup_ts: str = FOLLOWUP_TS,
    followup_login: str = "chatgpt-codex-connector[bot]",
    body: str = "<sub><sub>![P1 Badge](...)</sub></sub> test followup",
    superseded_by_head: str | None = HEAD_NEW,
    superseded_at: str | None = OLD_TS,
) -> dict:
    return {
        "id": "PRRT_TEST",
        "thread_id": "PRRT_TEST",
        "outdated": outdated,
        "resolved": resolved,
        "superseded_by_head": superseded_by_head,
        "superseded_at": superseded_at,
        "replies": [
            {
                "id": 1,
                "createdAt": followup_ts,
                "author": followup_login,
                "body": body,
                "commit_id": followup_commit_id,
                "original_commit_id": followup_original_commit_id,
            }
        ],
    }


class TestExactHeadBindingContract:
    def test_binding_commit_id_matches_new_head_qualifies(self) -> None:
        """Follow-up with commit_id == HEAD_NEW qualifies regardless
        of timestamp comparison."""
        thread = _build_thread(
            outdated=True,
            resolved=False,
            followup_commit_id=HEAD_NEW,
        )
        result = _maybe_resurrect_outdated_thread(
            thread,
            current_head=HEAD_NEW,
            operator_logins=NON_OPERATOR_LOGINS,
        )
        assert result is not None, (
            "Exact-head binding (commit_id == HEAD_NEW) must qualify the "
            "follow-up for resurrection."
        )
        assert result["followup"]["commit_id"] == HEAD_NEW

    def test_binding_original_commit_id_matches_new_head_qualifies(self) -> None:
        """Follow-up with original_commit_id == HEAD_NEW qualifies:
        GitHub emits the original commit (when the thread's
        diff hunk was first placed) and the current commit
        (latest push). Either is sufficient."""
        thread = _build_thread(
            outdated=True,
            resolved=False,
            followup_commit_id=None,
            followup_original_commit_id=HEAD_NEW,
        )
        result = _maybe_resurrect_outdated_thread(
            thread,
            current_head=HEAD_NEW,
            operator_logins=NON_OPERATOR_LOGINS,
        )
        # The exact-head binding via original_commit_id MAY
        # qualify. If the current helper does not yet check
        # original_commit_id, the test is a regression witness
        # for the C24-R2 fix to add it.
        assert result is not None, (
            "Original-commit-id binding must also qualify as "
            "exact-head evidence for resurrection."
        )

    def test_no_binding_within_timestamp_window_qualifies_legacy(self) -> None:
        """Legacy path: no exact-head binding, but the
        follow-up is later than ``superseded_at`` AND is
        non-operator AND actionable. This continues to work
        for backwards compatibility."""
        thread = _build_thread(
            outdated=True,
            resolved=False,
            followup_commit_id=None,
            followup_ts="2026-08-20T13:00:00Z",  # > OLD_TS
        )
        result = _maybe_resurrect_outdated_thread(
            thread,
            current_head=HEAD_NEW,
            operator_logins=NON_OPERATOR_LOGINS,
        )
        assert result is not None

    def test_no_binding_before_timestamp_window_rejected_legacy(self) -> None:
        """Legacy path: no exact-head binding, AND follow-up
        timestamp is BEFORE the superseded_at. The follow-up
        is treated as pre-repair and rejected."""
        thread = _build_thread(
            outdated=True,
            resolved=False,
            followup_commit_id=None,
            followup_ts="2026-08-20T11:00:00Z",  # < OLD_TS
        )
        result = _maybe_resurrect_outdated_thread(
            thread,
            current_head=HEAD_NEW,
            operator_logins=NON_OPERATOR_LOGINS,
        )
        assert result is None

    def test_binding_with_different_head_rejected(self) -> None:
        """Follow-up bound to a DIFFERENT head (not HEAD_NEW) does
        NOT qualify. The exact-head binding is strict."""
        thread = _build_thread(
            outdated=True,
            resolved=False,
            followup_commit_id="0" * 40,  # different from HEAD_NEW
        )
        result = _maybe_resurrect_outdated_thread(
            thread,
            current_head=HEAD_NEW,
            operator_logins=NON_OPERATOR_LOGINS,
        )
        assert result is None

    def test_resolved_thread_never_resurrected(self) -> None:
        """Audit: even with exact-head binding, a RESOLVED thread
        is not resurrected."""
        thread = _build_thread(
            outdated=True,
            resolved=True,  # RESOLVED — never resurrect
            followup_commit_id=HEAD_NEW,
        )
        result = _maybe_resurrect_outdated_thread(
            thread,
            current_head=HEAD_NEW,
            operator_logins=NON_OPERATOR_LOGINS,
        )
        assert result is None

    def test_not_outdated_thread_never_resurrected(self) -> None:
        """not-outdated thread cannot be resurrected even with
        exact-head binding."""
        thread = _build_thread(
            outdated=False,  # not outdated
            resolved=False,
            followup_commit_id=HEAD_NEW,
        )
        result = _maybe_resurrect_outdated_thread(
            thread,
            current_head=HEAD_NEW,
            operator_logins=NON_OPERATOR_LOGINS,
        )
        assert result is None

    def test_operator_login_rejected(self) -> None:
        """Operator login (e.g. ``github-actions``) is rejected
        even with exact-head binding."""
        thread = _build_thread(
            outdated=True,
            resolved=False,
            followup_commit_id=HEAD_NEW,
            followup_login="github-actions",
        )
        result = _maybe_resurrect_outdated_thread(
            thread,
            current_head=HEAD_NEW,
            operator_logins=("github-actions",),
        )
        assert result is None

    def test_no_replies_never_resurrected(self) -> None:
        """Empty replies list must not resurrect."""
        thread = _build_thread(
            outdated=True,
            resolved=False,
            followup_commit_id=HEAD_NEW,
        )
        thread["replies"] = []
        result = _maybe_resurrect_outdated_thread(
            thread,
            current_head=HEAD_NEW,
            operator_logins=NON_OPERATOR_LOGINS,
        )
        assert result is None
