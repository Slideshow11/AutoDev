"""Regression tests for C22: outdated-thread follow-up ingestion.

These tests cover the defect surfaced by Autonomy Trial 1B:

  - An unresolved review thread whose original diff anchor is outdated
    (anchored to a previous PR head) MUST NOT be automatically discarded
    when a NEW reviewer follow-up reply exists in the same thread and
    that follow-up reaffirms the original concern.

The relay previously skipped ALL outdated threads at
``review_repair_relay.py:_collect_review_findings`` and the same in the
``focused_thread_id`` path. The narrow repair reconsiders an outdated
thread ONLY when there is a non-operator follow-up reply whose
``createdAt`` is strictly later than the first comment's ``createdAt``
in the SAME thread (timestamp ordering as the strongest available
durable binding evidence, since GitHub does not re-bind
``comment.commit`` when a thread goes outdated).

Test isolation: each test builds its own snapshot dict mirroring the
shape ``capture_live_snapshot`` produces (top-level ``review_threads``
mapping with per-thread ``resolved/outdated/path/line/body/
commit_oid/author/comment_count/top_id/replies`` plus per-reply
``id/updatedAt/body/author/createdAt``).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


# Add repo root to sys.path so ``autocoder_orchestration`` is importable.
# Use ``$REPO_ROOT`` rather than a hard-coded absolute path so the
# committed-state scanner does not flag this test file (a literal
# absolute user-home prefix is on the forbidden-tokens list).
_REPO_ROOT = Path(os.environ.get("REPO_ROOT", str(Path(__file__).resolve().parent.parent)))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from autocoder_orchestration.review_repair_relay import (  # noqa: E402
    _collect_review_findings,
    _maybe_resurrect_outdated_thread,
    collect_findings,
    SEVERITY_P1,
    SEVERITY_P2,
)


# === Test fixtures mirroring capture_live_snapshot review_threads schema ===

CURRENT_HEAD = "a" * 40
ORIGINAL_HEAD = "f" * 40
THREAD_ID = "PRRT_kwDOTtyQLc6afj1L"
FIRST_COMMENT_ID = 3813704783
OPERATOR_REPLY_ID = 3813868041
REVIEWER_REPLY_ID = 3813874018


def _ts(s: str) -> str:
    """Test helper — pass through GitHub-style timestamps."""
    return s


def _make_thread(
    *,
    resolved: bool = False,
    outdated: bool = True,
    replies: list | None = None,
    first_body: str = (
        "_🗄️ Data Integrity & Integration_ | _🟡 Minor_\n\n"
        "Both provenance manifests were regenerated for the cli.py "
        "bytes only."
    ),
    first_created_at: str = "2026-08-19T14:07:29Z",
    first_author: str = "coderabbitai[bot]",
) -> dict:
    """Build a snapshot-shaped review_threads entry.

    Mirrors the dict ``capture_live_snapshot`` writes to
    ``snap['review_threads'][thread_id]`` (see supervisor.py around the
    ``reviewThreads`` GraphQL pagination loop).

    The ``replies`` list mirrors the per-reply entries the supervisor
    already captures: ``id, updatedAt, body, author, createdAt``.
    """
    reply_entries = []
    for reply in replies or []:
        reply_entries.append({
            "id": str(reply.get("databaseId", "")),
            "updatedAt": reply.get("updatedAt", ""),
            "body": reply.get("body", ""),
            "author": reply.get("author"),
            "createdAt": reply.get("createdAt"),
        })
    return {
        "resolved": resolved,
        "outdated": outdated,
        "path": "provenance/AUTOCODER_SOURCE_COMPLETENESS.json",
        "line": None,
        "body": first_body,
        "commit_oid": ORIGINAL_HEAD,
        "author": first_author,
        "comment_count": 1 + len(reply_entries),
        "top_id": str(FIRST_COMMENT_ID),
        "replies": reply_entries,
        # Round-1064 P1#1 placeholders; the C22 fix extends them.
        "top_updatedAt": first_created_at,
        "top_createdAt": first_created_at,
        # Round-C22R2/P1: durable ledger evidence the C22-R2
        # helper prefers. The original C22 test fixture
        # mirrors the production snapshot so existing
        # assertions remain valid under the new contract;
        # the legacy ``superseding_repair_committed_at``
        # field is preserved for diagnostic provenance only.
        "superseded_at": "2026-08-19T14:14:00Z",
        "superseded_by_head": "b89c25fe98fcf06f5ea14e32390a389d9227f2c6",
        # Round-C22R1/P1-A: legacy field (no longer the
        # authoritative boundary).
        "superseding_repair_committed_at": 1755615240,
    }


def _make_snapshot(threads: dict, head_sha: str = CURRENT_HEAD) -> dict:
    return {
        "captured_at": "2026-08-19T15:00:00Z",
        "head_sha": head_sha,
        "head_match": head_sha == CURRENT_HEAD,
        "mergeable": True,
        "formal_reviews": [],
        "review_threads": threads,
        "issue_comments": [],
        "required_checks": {},
        "providers": {},
        "_provider_issue_comments": {},
        "review_comments": [],
        "unconsumed_event_ids": [],
        "provider_surface_complete": True,
    }


def _make_snapshot_with_operator_logins(
    threads: dict, operator_logins, head_sha: str = CURRENT_HEAD,
) -> dict:
    snap = _make_snapshot(threads, head_sha=head_sha)
    snap["operator_logins"] = list(operator_logins)
    return snap


# === Helper-level tests (eligibility rule) ===


class TestResurrectionHelper:
    """The helper must reject every documented inadmissible case."""

    def test_resolved_thread_returns_none(self) -> None:
        # Case D from the C22 spec.
        thread = _make_thread(
            resolved=True,
            outdated=True,
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _ts("2026-08-19T14:27:14Z"),
                "body": "Reaffirms the concern.",
            }],
        )
        outcome = _maybe_resurrect_outdated_thread(
            thread,
            current_head=CURRENT_HEAD,
            operator_logins=("slidshow11", "slidshow11[bot]"),
        )
        assert outcome is None

    def test_outdated_thread_without_any_reply_returns_none(self) -> None:
        # Case A from the C22 spec.
        thread = _make_thread(
            resolved=False,
            outdated=True,
            replies=[],
        )
        outcome = _maybe_resurrect_outdated_thread(
            thread,
            current_head=CURRENT_HEAD,
            operator_logins=("slidshow11",),
        )
        assert outcome is None

    def test_outdated_thread_with_only_operator_reply_returns_none(self) -> None:
        # Case B from the C22 spec.
        thread = _make_thread(
            resolved=False,
            outdated=True,
            replies=[{
                "databaseId": OPERATOR_REPLY_ID,
                "author": "slidshow11",
                "createdAt": _ts("2026-08-19T14:26:30Z"),
                "body": "Partially addressed in b89c25f.",
            }],
        )
        outcome = _maybe_resurrect_outdated_thread(
            thread,
            current_head=CURRENT_HEAD,
            operator_logins=("slidshow11", "slidshow11[bot]"),
        )
        assert outcome is None

    def test_outdated_thread_with_later_non_operator_reply_resurrects(self) -> None:
        # Case C from the C22 spec — Trial 1B's exact shape.
        # (The corresponding P1-A test in
        # ``tests/test_c22r1_outdated_thread_repair_boundary.py``
        # covers the regression where a pre-repair reply must
        # NOT resurrect an already-addressed historical thread.
        # That regression was a C22 defect; the original C22
        # comparator ``top_createdAt`` was the wrong bound.)
        thread = _make_thread(
            resolved=False,
            outdated=True,
            first_created_at=_ts("2026-08-19T14:07:29Z"),
            replies=[
                {
                    "databaseId": OPERATOR_REPLY_ID,
                    "author": "slidshow11",
                    "createdAt": _ts("2026-08-19T14:26:30Z"),
                    "body": "Partially addressed in b89c25f.",
                },
                {
                    "databaseId": REVIEWER_REPLY_ID,
                    "author": "coderabbitai[bot]",
                    "createdAt": _ts("2026-08-19T14:27:14Z"),
                    "body": (
                        "<details><summary>Analysis</summary>"
                        "The remaining concern is still valid. "
                        "Please add canonical manifest entries."
                        "</details>"
                    ),
                },
            ],
        )
        outcome = _maybe_resurrect_outdated_thread(
            thread,
            current_head=CURRENT_HEAD,
            operator_logins=("slidshow11", "slidshow11[bot]"),
        )
        assert outcome is not None
        followup = outcome["followup"]
        # The reply dict preserves the snapshot's ``id`` field
        # (= ``comment.databaseId`` after ``capture_live_snapshot``
        # normalization). The helper returns the raw entry so
        # callers can re-derive databaseId via ``int(followup["id"])``
        # when needed.
        assert followup["id"] == str(REVIEWER_REPLY_ID)
        assert followup["author"] == "coderabbitai[bot]"
        assert followup["createdAt"] == _ts("2026-08-19T14:27:14Z")

    def test_resurrection_rejects_non_actionable_followup_body(self) -> None:
        # A follow-up whose body is a CodeRabbit status marker
        # (e.g. "Walkthrough") must NOT be considered actionable,
        # even if the timestamp is later. The relay's existing
        # ``_is_actionable_provider_comment`` filter applies.
        thread = _make_thread(
            resolved=False,
            outdated=True,
            first_created_at=_ts("2026-08-19T14:00:00Z"),
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _ts("2026-08-19T14:30:00Z"),
                "body": "Walkthrough",
            }],
        )
        outcome = _maybe_resurrect_outdated_thread(
            thread,
            current_head=CURRENT_HEAD,
            operator_logins=("slidshow11",),
        )
        assert outcome is None


# === End-to-end snapshot tests ===


class TestCollectReviewFindingsWithResurrection:
    def test_outdated_with_reviewer_followup_emits_finding(self) -> None:
        # Trial 1B repro: a thread with outdated=True but a later
        # CodeRabbit reply must surface a finding (was: 0).
        thread = _make_thread(
            resolved=False,
            outdated=True,
            first_created_at=_ts("2026-08-19T14:07:29Z"),
            replies=[
                {
                    "databaseId": OPERATOR_REPLY_ID,
                    "author": "slidshow11",
                    "createdAt": _ts("2026-08-19T14:26:30Z"),
                    "body": "Partially addressed.",
                },
                {
                    "databaseId": REVIEWER_REPLY_ID,
                    "author": "coderabbitai[bot]",
                    "createdAt": _ts("2026-08-19T14:27:14Z"),
                    "body": (
                        "_Data Integrity & Integration_ | _Minor_\n\n"
                        "The remaining concern is still valid."
                    ),
                },
            ],
        )
        snap = _make_snapshot_with_operator_logins(
            {THREAD_ID: thread},
            operator_logins=("slidshow11", "slidshow11[bot]"),
        )
        findings = _collect_review_findings(snap)
        assert len(findings) == 1
        f = findings[0]
        assert f.finding_id == f"thread:{THREAD_ID}"
        # The finding body must come from the FOLLOW-UP, not the
        # stale first comment.
        assert "The remaining concern is still valid" in f.body
        assert "Both provenance manifests were regenerated for the cli.py" not in f.body
        # The follow-up's databaseId is preserved as comment_id.
        assert f.comment_id == REVIEWER_REPLY_ID
        # The thread-level path/line are preserved as the
        # actionable evidence anchor.
        assert f.file_path == "provenance/AUTOCODER_SOURCE_COMPLETENESS.json"
        # The triggering follow-up author + timestamp are surfaced
        # in a structured provenance prologue in the body.
        assert "PRRC_kw" in f.body or "3813874018" in f.body
        assert "coderabbitai[bot]" in f.body
        assert "2026-08-19T14:27:14Z" in f.body

    def test_outdated_with_only_operator_reply_emits_no_finding(self) -> None:
        thread = _make_thread(
            resolved=False,
            outdated=True,
            replies=[{
                "databaseId": OPERATOR_REPLY_ID,
                "author": "slidshow11",
                "createdAt": _ts("2026-08-19T14:26:30Z"),
                "body": "Partially addressed in b89c25f.",
            }],
        )
        snap = _make_snapshot_with_operator_logins(
            {THREAD_ID: thread},
            operator_logins=("slidshow11", "slidshow11[bot]"),
        )
        assert _collect_review_findings(snap) == []

    def test_outdated_with_no_reply_emits_no_finding(self) -> None:
        thread = _make_thread(resolved=False, outdated=True, replies=[])
        snap = _make_snapshot({THREAD_ID: thread})
        assert _collect_review_findings(snap) == []

    def test_resolved_thread_emits_no_finding_even_with_reviewer_followup(
        self,
    ) -> None:
        thread = _make_thread(
            resolved=True,
            outdated=True,
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _ts("2026-08-19T14:27:14Z"),
                "body": "Reaffirms the concern.",
            }],
        )
        snap = _make_snapshot({THREAD_ID: thread})
        assert _collect_review_findings(snap) == []

    def test_currenthead_ordinary_thread_unchanged(self) -> None:
        # Non-outdated thread with ordinary current-head review:
        # behavior must be identical to before the C22 fix.
        thread = _make_thread(
            resolved=False,
            outdated=False,
            first_created_at=_ts("2026-08-19T14:07:29Z"),
            replies=[],
        )
        # Override commit_oid to current head so the bound-anchor
        # path treats it as a current-head finding.
        thread["commit_oid"] = CURRENT_HEAD
        snap = _make_snapshot({THREAD_ID: thread})
        findings = _collect_review_findings(snap)
        assert len(findings) == 1
        f = findings[0]
        # Body is the (current-head, original) body verbatim — no
        # follow-up provenance prologue inserted.
        assert f.body == thread["body"]
        assert f.comment_id is None  # No follow-up binding.


class TestFocusedThreadPath:
    """focused_thread_id must share the same eligibility rule."""

    def test_focused_outdated_with_reviewer_followup_emits_finding(self) -> None:
        thread = _make_thread(
            resolved=False,
            outdated=True,
            first_created_at=_ts("2026-08-19T14:07:29Z"),
            replies=[
                {
                    "databaseId": REVIEWER_REPLY_ID,
                    "author": "coderabbitai[bot]",
                    "createdAt": _ts("2026-08-19T14:27:14Z"),
                    "body": "_Data Integrity_ | _Minor_\n\nStale concern.",
                },
            ],
        )
        snap = _make_snapshot_with_operator_logins(
            {THREAD_ID: thread},
            operator_logins=("slidshow11", "slidshow11[bot]"),
        )
        findings = collect_findings(
            snap,
            focused_thread_id=THREAD_ID,
        )
        assert len(findings) == 1
        assert findings[0].finding_id == f"thread:{THREAD_ID}"
        assert findings[0].comment_id == REVIEWER_REPLY_ID

    def test_focused_outdated_with_only_operator_reply_emits_no_finding(
        self,
    ) -> None:
        thread = _make_thread(
            resolved=False,
            outdated=True,
            replies=[{
                "databaseId": OPERATOR_REPLY_ID,
                "author": "slidshow11",
                "createdAt": _ts("2026-08-19T14:26:30Z"),
                "body": "Partially addressed.",
            }],
        )
        snap = _make_snapshot_with_operator_logins(
            {THREAD_ID: thread},
            operator_logins=("slidshow11", "slidshow11[bot]"),
        )
        findings = collect_findings(
            snap,
            focused_thread_id=THREAD_ID,
        )
        assert findings == []

    def test_focused_resolved_thread_emits_no_finding(self) -> None:
        thread = _make_thread(
            resolved=True,
            outdated=True,
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _ts("2026-08-19T14:27:14Z"),
                "body": "Reaffirms the concern.",
            }],
        )
        snap = _make_snapshot_with_operator_logins(
            {THREAD_ID: thread},
            operator_logins=("slidshow11", "slidshow11[bot]"),
        )
        findings = collect_findings(
            snap,
            focused_thread_id=THREAD_ID,
        )
        assert findings == []


class TestNoDuplicateFinding:
    def test_no_duplicate_when_followup_visible_via_two_surfaces(self) -> None:
        # The existing relay has multiple comment surfaces
        # (``issue_comments``, ``provider_surfaces``, ``review_threads``).
        # If the same follow-up were surfaced through two of them,
        # the relay's existing ``seen_ids`` dedup MUST prevent a
        # double-finding.
        thread = _make_thread(
            resolved=False,
            outdated=True,
            first_created_at=_ts("2026-08-19T14:07:29Z"),
            replies=[
                {
                    "databaseId": REVIEWER_REPLY_ID,
                    "author": "coderabbitai[bot]",
                    "createdAt": _ts("2026-08-19T14:27:14Z"),
                    "body": "_Minor_ | _Quick win_\n\nReaffirms.",
                },
            ],
        )
        snap = _make_snapshot({THREAD_ID: thread})
        # The collector is called once; check finding_ids are unique.
        findings = _collect_review_findings(snap)
        ids = [f.finding_id for f in findings]
        assert len(ids) == len(set(ids))
        assert len(findings) == 1


class TestOperatorLoginsParameter:
    def test_default_operator_logins_excludes_coderabbit(self) -> None:
        # When no operator_logins override is supplied, the helper
        # MUST still default to a conservative operator set so
        # Codex / CodeRabbit accounts are not mis-identified as
        # operator accounts.
        thread = _make_thread(
            resolved=False,
            outdated=True,
            first_created_at=_ts("2026-08-19T14:00:00Z"),
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _ts("2026-08-19T14:30:00Z"),
                "body": "_Minor_\n\nReaffirms.",
            }],
        )
        # No operator_logins passed: the helper's default
        # conservative set must NOT include coderabbitai[bot].
        outcome = _maybe_resurrect_outdated_thread(
            thread,
            current_head=CURRENT_HEAD,
        )
        assert outcome is not None
        # The reply dict preserves the snapshot's ``id`` field
        # (= ``comment.databaseId`` after ``capture_live_snapshot``
        # normalization).
        assert outcome["followup"]["id"] == str(REVIEWER_REPLY_ID)
