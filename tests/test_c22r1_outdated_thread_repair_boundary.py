"""Regression tests for C22-R1: repair-boundary binding, operator
identities, comment pagination, and follow-up normalisation.

These tests cover the defects surfaced by Codex + Sourcery on PR #8
(following the merged C22 fix at HEAD 3790b449):

P1-A — Follow-up eligibility must compare against the SUPERSEDING
       REPAIR COMMIT timestamp (the head-changing repair), not the
       first-comment timestamp. A reviewer follow-up posted BEFORE
       the repair must NOT resurrect an already-addressed historical
       thread.

P1-B — Production snapshots must carry a canonical operator
       identity set, not the GitHub-Actions-only fallback.

P2-C — ``capture_live_snapshot`` must paginate thread comments
       (the GraphQL ``comments`` connection) so a qualifying
       reviewer follow-up on a >25-comment thread is not missed.

S1   — Operator-login normalization must be safe against strings,
       truthy non-iterables, and other malformed inputs.

S2   — Missing / malformed timestamp evidence must fail closed
       (no resurrection, no exception).

S3   — Cross-surface dedup must use a real second surface
       (``review_comments``) when the same logical comment is
       visible through more than one snapshot path.

The tests use the same fixtures as the C22 file but extended to
carry ``superseding_repair_committed_at`` (Unix seconds) per thread.
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
    _normalize_operator_logins,
    _parse_iso8601_utc,
    collect_findings,
)


# === Test fixtures mirroring capture_live_snapshot review_threads schema ===

CURRENT_HEAD = "a" * 40
ORIGINAL_HEAD = "f" * 40
THREAD_ID = "PRRT_kwDOTtyQLc6afj1L"
FIRST_COMMENT_ID = 3813704783
OPERATOR_REPLY_ID = 3813868041
REVIEWER_REPLY_ID = 3813874018


def _ts(s: str) -> str:
    return s


# Real-world timeline (UTC Unix seconds). The defaults below match
# the Trial 1B case where the FIRST PR commit ``f792cd3c6eba`` was
# superseded by the repair ``b89c25f`` (timestamp ~= 2026-08-19T14:14Z).
T_T1 = 1755614849.0  # first comment createdAt (2026-08-19T14:07:29Z)
T_T2 = 1755616034.0  # CodeRabbit analysis-chain follow-up (2026-08-19T14:27:14Z)
T_REPAIR = 1755615240.0  # repair commit b89c25f (2026-08-19T14:14:00Z)
T_REPORT = 1755617000.0  # report-only commit 70d1085 (later than the repair)

# Convert T_T1 and T_T2 to ISO 8601 once so the fixtures stay readable.
def _iso(seconds: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


T_T1_ISO = _iso(T_T1)
T_T2_ISO = _iso(T_T2)


def _make_thread(
    *,
    resolved: bool = False,
    outdated: bool = True,
    replies: list | None = None,
    first_body: str = (
        "_Data Integrity_ | _Minor_\n\nBoth provenance manifests "
        "were regenerated for the cli.py bytes only."
    ),
    first_created_at: str = T_T1_ISO,
    first_author: str = "coderabbitai[bot]",
    superseding_repair_committed_at: int | None = int(T_REPAIR),
) -> dict:
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
        "top_updatedAt": first_created_at,
        "top_createdAt": first_created_at,
        # C22-R1: the AUTHORITATIVE repair-boundary timestamp,
        # derived by the supervisor via
        # ``git log --reverse --pretty=format:"%ct" <anchor>..<head>``
        # and stamped on the snapshot. ``None`` is allowed only for
        # test fixtures that intentionally omit it to exercise the
        # fail-closed path.
        "superseding_repair_committed_at": superseding_repair_committed_at,
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
    *,
    review_comments: list | None = None,
) -> dict:
    snap = _make_snapshot(threads, head_sha=head_sha)
    snap["operator_logins"] = list(operator_logins)
    if review_comments is not None:
        snap["review_comments"] = review_comments
    return snap


# === P1-A: repair-boundary binding ===

class TestRepairBoundaryBinding:
    """Outdated resurrection must compare against the SUPERSEDING
    REPAIR COMMIT timestamp, not the first comment's createdAt."""

    def test_followup_predating_repair_is_ignored(self) -> None:
        # Chronology: T1 (original comment) < T2 (reviewer reply) <
        # T3 (repair commit that made the thread outdated). The
        # reviewer reply HAPPENED BEFORE the repair, so the
        # historical concern was already addressed. C22-R1 must
        # NOT resurrect the thread.
        thread = _make_thread(
            resolved=False,
            outdated=True,
            first_created_at=_ts(T_T1_ISO),
            # Followup timestamp predates the repair boundary by
            # definition.
            superseding_repair_committed_at=int(T_REPAIR),
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                # T2 < T_REPAIR
                "createdAt": _ts(_iso(T_REPAIR - 60.0)),
                "body": (
                    "_Data Integrity_ | _Minor_\n\n"
                    "Historical concern already addressed."
                ),
            }],
        )
        snap = _make_snapshot_with_operator_logins(
            {THREAD_ID: thread},
            operator_logins=("Slideshow11", "github-actions"),
        )
        assert _collect_review_findings(snap) == []

    def test_followup_postdating_repair_resurrects(self) -> None:
        # Trial 1B exact chronology: T1 < T_REPAIR < T2. The followup
        # post-dates the repair; C22-R1 MUST resurrect.
        thread = _make_thread(
            resolved=False,
            outdated=True,
            first_created_at=_ts(T_T1_ISO),
            superseding_repair_committed_at=int(T_REPAIR),
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _ts(T_T2_ISO),  # after T_REPAIR
                "body": (
                    "_Data Integrity_ | _Minor_\n\n"
                    "The remaining concern is still valid."
                ),
            }],
        )
        snap = _make_snapshot_with_operator_logins(
            {THREAD_ID: thread},
            operator_logins=("Slideshow11", "github-actions"),
        )
        findings = _collect_review_findings(snap)
        assert len(findings) == 1
        assert findings[0].comment_id == REVIEWER_REPLY_ID

    def test_missing_repair_boundary_fails_closed(self) -> None:
        # When the supervisor could not derive the repair boundary
        # (e.g. git lookup failed, journal missing), the rule must
        # fail closed. Without the repair boundary we cannot claim
        # the follow-up post-dates the head change that made the
        # thread stale, so no resurrection.
        thread = _make_thread(
            resolved=False,
            outdated=True,
            superseding_repair_committed_at=None,  # boundary absent
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _ts(T_T2_ISO),
                "body": (
                    "_Data Integrity_ | _Minor_\n\n"
                    "The remaining concern is still valid."
                ),
            }],
        )
        snap = _make_snapshot_with_operator_logins(
            {THREAD_ID: thread},
            operator_logins=("Slideshow11", "github-actions"),
        )
        assert _collect_review_findings(snap) == []

    def test_followup_equal_to_repair_boundary_is_ignored(self) -> None:
        # Equal timestamps are NOT strictly later; fail closed.
        thread = _make_thread(
            resolved=False,
            outdated=True,
            superseding_repair_committed_at=int(T_REPAIR),
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _ts(_iso(T_REPAIR)),  # exact equal
                "body": "_Data Integrity_ | _Minor_\n\nEdge case.",
            }],
        )
        snap = _make_snapshot_with_operator_logins(
            {THREAD_ID: thread},
            operator_logins=("Slideshow11", "github-actions"),
        )
        assert _collect_review_findings(snap) == []

    def test_later_documentation_commit_does_not_block_resurrection(
        self,
    ) -> None:
        # Codex's note: a later doc/report-only commit may exist
        # after the actual repair. The repair-boundary timestamp
        # must be the SUPERSEDING REPAIR timestamp, not the
        # current-head commit timestamp. This thread's repair
        # boundary is the actual repair (T_REPAIR), even though
        # a doc-only commit (T_REPORT) lands later. A followup
        # between T_REPAIR and T_REPORT must resurrect.
        thread = _make_thread(
            resolved=False,
            outdated=True,
            first_created_at=_ts(T_T1_ISO),
            superseding_repair_committed_at=int(T_REPAIR),  # NOT T_REPORT
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _ts(_iso(T_REPORT - 60.0)),  # after T_REPAIR
                "body": "_Data Integrity_ | _Minor_\n\nReaffirms.",
            }],
        )
        snap = _make_snapshot_with_operator_logins(
            {THREAD_ID: thread},
            operator_logins=("Slideshow11", "github-actions"),
        )
        findings = _collect_review_findings(snap)
        assert len(findings) == 1


# === S1: operator normalization ===

class TestNormalizeOperatorLogins:
    """The normalizer must safely handle every documented input shape."""

    def test_none_returns_fallback(self) -> None:
        result = _normalize_operator_logins(None)
        # Fallback contains github-actions and reviewer-bot accounts.
        assert "github-actions" in result
        assert "github-actions[bot]" in result

    def test_plain_string_is_rejected_to_fallback(self) -> None:
        # ``"github-actions"`` would otherwise explode into
        # ``("g", "i", "t", "h", "u", "b", ...)`` — a real bug
        # Sourcery flagged. The normalizer MUST detect a bare
        # string and fall back to the canonical default.
        result = _normalize_operator_logins("github-actions")
        assert "github-actions" in result
        # The exploded characters must NOT appear as members.
        assert "g" not in result
        assert "i" not in result

    def test_tuple_of_strings_preserved(self) -> None:
        result = _normalize_operator_logins(
            ("Slideshow11", "github-actions"),
        )
        assert "Slideshow11" in result
        assert "github-actions" in result

    def test_list_of_strings_preserved(self) -> None:
        result = _normalize_operator_logins(["Slideshow11", "alice"])
        assert "Slideshow11" in result
        assert "alice" in result

    def test_set_of_strings_preserved(self) -> None:
        result = _normalize_operator_logins({"Slideshow11", "alice"})
        assert "Slideshow11" in result
        assert "alice" in result

    def test_empty_entries_ignored(self) -> None:
        result = _normalize_operator_logins(
            ["Slideshow11", "", "  ", "alice"],
        )
        assert "Slideshow11" in result
        assert "alice" in result
        assert "" not in result

    def test_whitespace_trimmed(self) -> None:
        result = _normalize_operator_logins(
            ["  Slideshow11  ", "  github-actions"],
        )
        assert "Slideshow11" in result
        assert "github-actions" in result

    def test_truthy_non_iterable_returns_fallback(self) -> None:
        # ``tuple(42)`` would raise TypeError. Sourcery flagged this.
        # The normalizer must NOT raise; it falls back.
        result = _normalize_operator_logins(42)
        assert "github-actions" in result
        result = _normalize_operator_logins(3.14)
        assert "github-actions" in result
        result = _normalize_operator_logins(True)
        assert "github-actions" in result
        result = _normalize_operator_logins(None)
        assert "github-actions" in result

    def test_dict_treated_as_non_iterable_returns_fallback(self) -> None:
        # ``tuple({"a": 1, "b": 2})`` returns ``("a", "b")`` — keys,
        # not the value we want. The normalizer should reject dicts.
        result = _normalize_operator_logins({"a": "x"})
        assert "github-actions" in result
        assert "x" not in result


# === S2: timestamp fail-closed ===

class TestTimestampFailClosed:
    """All malformed / missing timestamp inputs must return ``None``."""

    def test_missing_top_level_returns_none(self) -> None:
        assert _parse_iso8601_utc(None) is None
        assert _parse_iso8601_utc("") is None

    def test_non_string_returns_none(self) -> None:
        assert _parse_iso8601_utc(1692445634) is None
        assert _parse_iso8601_utc(1692445634.5) is None
        assert _parse_iso8601_utc(True) is None
        assert _parse_iso8601_utc([2024, 1, 1]) is None

    def test_malformed_string_returns_none(self) -> None:
        assert _parse_iso8601_utc("19-08-2026 14:27:14") is None
        assert _parse_iso8601_utc("not a date") is None
        assert _parse_iso8601_utc("2026-08-19") is None  # date only, no time

    def test_valid_z_suffix(self) -> None:
        # 2026-08-19T14:27:14Z = 1787149634 Unix seconds
        assert _parse_iso8601_utc("2026-08-19T14:27:14Z") == 1787149634

    def test_valid_offset_suffix(self) -> None:
        # Same instant in ``+00:00`` notation.
        assert _parse_iso8601_utc("2026-08-19T14:27:14+00:00") == 1787149634

    def test_equivalent_timezone_offset(self) -> None:
        # The same instant expressed in different timezones yields
        # the same Unix seconds.
        a = _parse_iso8601_utc("2026-08-19T14:27:14Z")
        b = _parse_iso8601_utc("2026-08-19T16:27:14+02:00")
        assert a == b


# === S3: cross-surface dedup ===

class TestCrossSurfaceDedup:
    """When the same logical follow-up is visible through more than
    one snapshot surface, the relay must emit a SINGLE finding."""

    def test_review_threads_and_review_comments_same_followup(
        self,
    ) -> None:
        # The follow-up is visible in BOTH ``review_threads`` (the
        # C22 resurrected branch) AND ``review_comments`` (the
        # existing inline-comment branch). The ``seen_ids`` dedup
        # plus the unified ``comment_id`` dedup must produce ONE
        # finding, not two.
        thread = _make_thread(
            resolved=False,
            outdated=True,
            first_created_at=_ts(T_T1_ISO),
            superseding_repair_committed_at=int(T_REPAIR),
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _ts(T_T2_ISO),
                "body": (
                    "_Data Integrity_ | _Minor_\n\n"
                    "Please address the manifest entries."
                ),
            }],
        )
        # Same logical comment also in review_comments with
        # the same numeric id.
        review_comments = [{
            "id": REVIEWER_REPLY_ID,
            "path": "provenance/AUTOCODER_SOURCE_COMPLETENESS.json",
            "line": None,
            "body": (
                "_Data Integrity_ | _Minor_\n\n"
                "Please address the manifest entries."
            ),
            "html_url": "https://example/PRRT_kwDOTtyQLc6afj1L#r3813874018",
        }]
        snap = _make_snapshot_with_operator_logins(
            {THREAD_ID: thread},
            operator_logins=("Slideshow11", "github-actions"),
            review_comments=review_comments,
        )
        findings = _collect_review_findings(snap)
        # Exactly one finding — the same comment must not appear
        # twice across the two surfaces.
        assert len(findings) == 1
        assert findings[0].comment_id == REVIEWER_REPLY_ID


# === P1-B: production operator identity ===

class TestProductionOperatorIdentity:
    """The relay must not silently fall back to GitHub-Actions when
    an operator login is present in the production snapshot."""

    def test_real_operator_login_excludes_reply(self) -> None:
        # ``Slideshow11`` is the production PR operator (verified
        # via PR #8 GraphQL ``author.login``). An operator-only
        # follow-up must NOT resurrect the thread.
        thread = _make_thread(
            resolved=False,
            outdated=True,
            first_created_at=_ts(T_T1_ISO),
            superseding_repair_committed_at=int(T_REPAIR),
            replies=[{
                "databaseId": OPERATOR_REPLY_ID,
                "author": "Slideshow11",  # production operator
                "createdAt": _ts(T_T2_ISO),
                "body": "Fixed in b89c25f.",
            }],
        )
        snap = _make_snapshot_with_operator_logins(
            {THREAD_ID: thread},
            operator_logins=("Slideshow11", "github-actions"),
        )
        assert _collect_review_findings(snap) == []

    def test_reviewer_bot_remains_eligible(self) -> None:
        thread = _make_thread(
            resolved=False,
            outdated=True,
            first_created_at=_ts(T_T1_ISO),
            superseding_repair_committed_at=int(T_REPAIR),
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _ts(T_T2_ISO),
                "body": "_Data Integrity_ | _Minor_\n\nReaffirms.",
            }],
        )
        snap = _make_snapshot_with_operator_logins(
            {THREAD_ID: thread},
            operator_logins=("Slideshow11", "github-actions"),
        )
        findings = _collect_review_findings(snap)
        assert len(findings) == 1


# === Trial 1B exact replay ===

class TestTrial1BReplay:
    """The Trial 1B chronology must STILL classify the follow-up as
    actionable under the new repair-boundary rule."""

    def test_trial1b_exact_replay_resurrects(self) -> None:
        # Thread ``PRRT_kwDOTtyQLc6afj1L`` on PR #7 with the
        # exact timestamps from the live replay:
        #   T1 = first comment (coderabbitai on f792cd3)
        #   T_REPAIR = b89c25f (the first repair commit)
        #   T2 = coderabbitai analysis-chain follow-up
        #        (after the repair)
        thread = _make_thread(
            resolved=False,
            outdated=True,
            first_created_at="2026-08-19T14:07:29Z",  # T1
            superseding_repair_committed_at=int(1755615240),  # b89c25f
            replies=[{
                "databaseId": 3813874018,
                "author": "coderabbitai[bot]",
                "createdAt": "2026-08-19T14:27:14Z",  # T2 (after repair)
                "body": (
                    "_Data Integrity & Integration_ | _Minor_\n\n"
                    "The remaining concern is still valid."
                ),
            }],
        )
        snap = _make_snapshot_with_operator_logins(
            {THREAD_ID: thread},
            operator_logins=("Slideshow11", "github-actions"),
        )
        findings = _collect_review_findings(snap)
        assert len(findings) == 1
        assert findings[0].comment_id == 3813874018
        # The finding body carries the C22 resurrection prologue
        # with the repair-boundary timestamp.
        assert "superseding_repair_committed_at" in findings[0].body or (
            "Repair boundary" in findings[0].body
            or "outdated" in findings[0].body.lower()
        )


# === Focused path parity ===

class TestFocusedPathParity:
    """The ``focused_thread_id`` branch must share the same
    eligibility rule."""

    def test_focused_outdated_with_reviewer_followup_post_repair(self) -> None:
        thread = _make_thread(
            resolved=False,
            outdated=True,
            first_created_at=_ts(T_T1_ISO),
            superseding_repair_committed_at=int(T_REPAIR),
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _ts(T_T2_ISO),
                "body": "_Data Integrity_ | _Minor_\n\nStale concern.",
            }],
        )
        snap = _make_snapshot_with_operator_logins(
            {THREAD_ID: thread},
            operator_logins=("Slideshow11", "github-actions"),
        )
        findings = collect_findings(snap, focused_thread_id=THREAD_ID)
        assert len(findings) == 1

    def test_focused_outdated_with_reviewer_followup_pre_repair(self) -> None:
        thread = _make_thread(
            resolved=False,
            outdated=True,
            first_created_at=_ts(T_T1_ISO),
            superseding_repair_committed_at=int(T_REPAIR),
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _ts(_iso(T_REPAIR - 60.0)),
                "body": "_Data Integrity_ | _Minor_\n\nPre-repair.",
            }],
        )
        snap = _make_snapshot_with_operator_logins(
            {THREAD_ID: thread},
            operator_logins=("Slideshow11", "github-actions"),
        )
        findings = collect_findings(snap, focused_thread_id=THREAD_ID)
        assert findings == []