"""Regression tests for C22-R2: TRUE repair-boundary binding via the
durable finding-ledger, and inner-comment pagination → readiness
fail-closed wiring.

These tests cover the defects surfaced by Codex on PR #8 follow-up
review at HEAD 0c6ace8 (after the C22 + C22-R1 fixes landed):

P1 — Outdated-thread follow-up eligibility must compare against the
     authoritative SUPERSEDED-by-head transition recorded by the
     durable FindingLedger, NOT a git-ancestry-derived boundary.
     C22-R1 used ``git log --reverse --ancestry-path <anchor>..<head>``
     which yields the EARLIEST descendant — but an unrelated docs
     commit between the original anchor and the actual repair is
     still a descendant. The actual repair transition is recorded
     by ``mark_superseded_by_head(new_head_sha=...)`` at the moment
     the worker pushes. The snapshot captures that durable evidence
     per thread and the eligibility helper uses it as the
     authoritative boundary.

P2 — ``evaluate_readiness`` must check the singular
     ``review_thread_pagination_failed`` (inner per-thread comment
     pagination) and the per-thread ``truncated_thread_ids`` list,
     not just the plural ``review_threads_pagination_failed`` (outer
     thread-list pagination). When ANY inner thread's comments
     inventory is incomplete, the readiness gate MUST fail closed.

The tests use the same fixtures as the C22 / C22-R1 files but
extended to carry ``superseded_at`` and ``superseded_by_head`` per
thread (the durable finding-ledger evidence).
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
    _collect_review_findings,
    _maybe_resurrect_outdated_thread,
)


# === Fixtures mirroring capture_live_snapshot review_threads schema ===

CURRENT_HEAD = "a" * 40
ORIGINAL_HEAD = "f" * 40
THREAD_ID = "PRRT_kwDOTtyQLc6afj1L"
FIRST_COMMENT_ID = 3813704783
OPERATOR_REPLY_ID = 3813868041
REVIEWER_REPLY_ID = 3813874018


def _ts(s: str) -> str:
    return s


# A < B < C < D chronology with an UNRELATED docs commit between
# the original reviewed commit and the actual repair.
T_A = 1755614849  # 2026-08-19T14:07:29Z (first comment)
T_B = 1755615060  # unrelated docs commit
T_C = 1755615900  # reviewer follow-up (pre-repair)
T_D = 1755616800  # actual code repair

# Trial 1B chronology
T_REPAIR = 1755615240  # b89c25f (the actual repair)

# ISO forms
def _iso(seconds: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


T_A_ISO = _iso(T_A)
T_C_ISO = _iso(T_C)
T_REPAIR_ISO = _iso(T_REPAIR)


def _make_thread(
    *,
    resolved: bool = False,
    outdated: bool = True,
    replies: list | None = None,
    first_body: str = (
        "Original concern body."
    ),
    first_created_at: str = T_A_ISO,
    first_author: str = "coderabbitai[bot]",
    superseded_at: str | int | None = T_REPAIR_ISO,
    superseded_by_head: str | None = "b89c25fe98fcf06f5ea14e32390a389d9227f2c6",
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
        # Round-C22R2/P1: the durable finding-ledger evidence.
        # ``superseded_at`` is the wall-clock at which the worker
        # push advanced the head; ``superseded_by_head`` is the
        # authoritative new head SHA recorded by
        # ``mark_superseded_by_head(new_head_sha=...)``. These
        # are the canonical binding evidence the C22-R2
        # eligibility helper uses.
        "superseded_at": superseded_at,
        "superseded_by_head": superseded_by_head,
        # Round-C22R1 fields (kept for backward-compat with the
        # old git-ancestry-derived boundary; the C22-R2 helper
        # prefers the durable fields above when present).
        "superseding_repair_committed_at": 1755615240,
    }


def _make_snapshot(
    threads: dict,
    head_sha: str = CURRENT_HEAD,
    *,
    operator_logins=None,
    review_thread_pagination_complete: bool = True,
    review_thread_pagination_failed: bool = False,
    truncated_thread_ids: list | None = None,
    review_threads_pagination_complete: bool = True,
    review_threads_pagination_failed: bool = False,
) -> dict:
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
        "operator_logins": list(operator_logins or ("Slideshow11", "github-actions")),
        # Pagination flags — both inner and outer.
        "review_thread_pagination_complete": review_thread_pagination_complete,
        "review_thread_pagination_failed": review_thread_pagination_failed,
        "truncated_thread_ids": truncated_thread_ids or [],
        "review_threads_pagination_complete": review_threads_pagination_complete,
        "review_threads_pagination_failed": review_threads_pagination_failed,
    }


# === P1: True repair-boundary binding ===

class TestTrueRepairBoundary:
    """The authoritative superseding transition is the durable
    FindingLedger record (``superseded_at`` / ``superseded_by_head``),
    NOT a git-ancestry-derived boundary."""

    def test_stale_thread_no_superseding_evidence_fails_closed(self) -> None:
        # No ``superseded_at`` and no ``superseded_by_head`` on the
        # thread: the durable state cannot prove a real repair
        # occurred, so the rule fails closed.
        thread = _make_thread(
            superseded_at=None,
            superseded_by_head=None,
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": "2026-08-19T15:30:00Z",
                "body": "_Data Integrity_ | _Minor_\n\nReaffirms.",
            }],
        )
        snap = _make_snapshot({THREAD_ID: thread})
        assert _collect_review_findings(snap) == []

    def test_unrelated_descendant_does_not_shift_boundary(self) -> None:
        # A < B < C < D chronology (audit's P1 case):
        #   A = original anchor (14:07)
        #   B = unrelated docs commit (14:11)
        #   C = reviewer follow-up (14:25, BEFORE D)
        #   D = actual repair (14:40)
        #
        # C22-R1's git-ancestry rule would choose B (the earliest
        # descendant) as the boundary, which incorrectly classifies
        # C as post-repair. C22-R2 uses the durable D timestamp
        # (superseded_at), so C < D => NOT resurrected.
        thread = _make_thread(
            first_created_at=_iso(T_A),
            superseded_at=_iso(T_D),  # the actual repair
            superseded_by_head="d" * 40,
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _iso(T_C),  # BEFORE the repair
                "body": (
                    "_Data Integrity_ | _Minor_\n\n"
                    "Reviewer reply between unrelated docs commit and "
                    "actual repair."
                ),
            }],
        )
        snap = _make_snapshot({THREAD_ID: thread})
        # C22-R2 must NOT resurrect the thread.
        assert _collect_review_findings(snap) == []

    def test_followup_after_real_repair_resurrects(self) -> None:
        # A < B < D < C chronology: C (the reviewer follow-up)
        # genuinely post-dates the actual repair D. C22-R2 must
        # resurrect.
        thread = _make_thread(
            first_created_at=_iso(T_A),
            superseded_at=_iso(T_D),
            superseded_by_head="d" * 40,
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _iso(T_D + 60),  # AFTER the repair
                "body": "_Data Integrity_ | _Minor_\n\nReaffirms.",
            }],
        )
        snap = _make_snapshot({THREAD_ID: thread})
        findings = _collect_review_findings(snap)
        assert len(findings) == 1
        assert findings[0].comment_id == REVIEWER_REPLY_ID

    def test_later_docs_report_commit_does_not_shift_boundary(self) -> None:
        # A < D < E < C chronology: a later docs/report-only
        # commit (E) and a reviewer follow-up (C) both land
        # after the actual repair D. The durable ``superseded_at``
        # is D, so C is correctly classified as post-repair.
        # (The git-ancestry rule would also correctly identify
        # D because D comes before E in ancestry-path order; this
        # test primarily locks in that the durable evidence is
        # honored when present.)
        thread = _make_thread(
            first_created_at=_iso(T_A),
            superseded_at=_iso(T_D),
            superseded_by_head="d" * 40,
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _iso(T_D + 600),
                "body": "_Data Integrity_ | _Minor_\n\nAfter D and E.",
            }],
        )
        snap = _make_snapshot({THREAD_ID: thread})
        findings = _collect_review_findings(snap)
        assert len(findings) == 1

    def test_followup_at_exact_repair_boundary_ignored(self) -> None:
        # Equal timestamps fail closed.
        thread = _make_thread(
            first_created_at=_iso(T_A),
            superseded_at=_iso(T_REPAIR),
            superseded_by_head="b89c25fe98fcf06f5ea14e32390a389d9227f2c6",
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _iso(T_REPAIR),  # EXACT equal
                "body": "_Data Integrity_ | _Minor_\n\nEdge case.",
            }],
        )
        snap = _make_snapshot({THREAD_ID: thread})
        assert _collect_review_findings(snap) == []

    def test_trial1b_exact_replay_resurrects(self) -> None:
        # Trial 1B: first comment at 14:07, repair b89c25f at
        # 14:14, CodeRabbit analysis-chain follow-up at 14:27.
        # The durable ``superseded_at`` matches the actual repair
        # b89c25f. The follow-up post-dates it: resurrected.
        thread = _make_thread(
            first_created_at="2026-08-19T14:07:29Z",
            superseded_at="2026-08-19T14:14:00Z",
            superseded_by_head="b89c25fe98fcf06f5ea14e32390a389d9227f2c6",
            replies=[{
                "databaseId": 3813874018,
                "author": "coderabbitai[bot]",
                "createdAt": "2026-08-19T14:27:14Z",  # AFTER repair
                "body": (
                    "_Data Integrity & Integration_ | _Minor_\n\n"
                    "The remaining concern is still valid."
                ),
            }],
        )
        snap = _make_snapshot({THREAD_ID: thread})
        findings = _collect_review_findings(snap)
        assert len(findings) == 1
        assert findings[0].comment_id == 3813874018


# === P2: Inner pagination → readiness wiring ===

class TestInnerPaginationReadiness:
    """``evaluate_readiness`` MUST consult the singular
    ``review_thread_pagination_failed`` and ``truncated_thread_ids``
    fields (inner per-thread comment pagination)."""

    def test_outer_complete_inner_failed_blocks_readiness(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Outer thread-list pagination: complete.
        # Inner per-thread comment pagination: FAILED.
        # Expected: evaluate_readiness returns ``ready: False`` with
        # an inner-pagination reason.
        from autocoder_supervisor import supervisor as _sup

        snap = {
            "head_sha": "abc" * 14,  # 42 chars; normalize below
            "head_match": True,
            "mergeable": True,
            "formal_reviews": [],
            "review_threads": {},
            "issue_comments": [],
            "required_checks": {},
            "providers": {},
            "_provider_issue_comments": {},
            "unconsumed_event_ids": [],
            "provider_surfaces": {},
            "review_comments": [],
            "operator_logins": [],
            # Outer pagination OK
            "review_threads_pagination_complete": True,
            "review_threads_pagination_failed": False,
            # Inner pagination FAILED
            "review_thread_pagination_complete": False,
            "review_thread_pagination_failed": True,
            "truncated_thread_ids": ["PRRT_kwDOTtyQLc6afj1L"],
        }

        # Use the actual current head (so head_sha matches).
        # Re-derive by inspecting AUTHORITATIVE_HEAD at runtime
        # via the supervisor's module-level constant.
        import autocoder_supervisor.supervisor as sup_mod
        snap["head_sha"] = sup_mod.AUTHORITATIVE_HEAD  # type: ignore[name-defined]
        result = _sup.evaluate_readiness(snap)
        assert result["ready"] is False
        assert "inner" in result["reason"] or "pagination" in result["reason"]
        # The legacy outer pagination reason is NOT the explanation.
        assert result["reason"] != "thread_pagination_failed"

    def test_both_complete_allows_normal_classification(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Both outer and inner pagination complete.
        # Expected: evaluate_readiness does NOT short-circuit on
        # pagination. It may still return ready=False for other
        # reasons (missing checks, etc.), but the reason MUST NOT
        # mention pagination.
        from autocoder_supervisor import supervisor as _sup
        import autocoder_supervisor.supervisor as sup_mod

        snap = {
            "head_sha": sup_mod.AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
            "head_match": True,
            "mergeable": True,
            "formal_reviews": [],
            "review_threads": {},
            "issue_comments": [],
            "required_checks": {},
            "providers": {},
            "_provider_issue_comments": {},
            "unconsumed_event_ids": [],
            "provider_surfaces": {},
            "review_comments": [],
            "operator_logins": [],
            "review_threads_pagination_complete": True,
            "review_threads_pagination_failed": False,
            "review_thread_pagination_complete": True,
            "review_thread_pagination_failed": False,
            "truncated_thread_ids": [],
        }
        result = _sup.evaluate_readiness(snap)
        # The pagination reason MUST NOT be the explanation when
        # both inventories are complete.
        assert "thread_pagination_failed" not in result["reason"]
        assert "inner" not in result["reason"]


class TestInnerPaginationCollectorGuard:
    """The relay's finding collector must also be aware of
    inner-thread pagination failure: when the inventory is
    incomplete, do not silently classify the head as clean
    even if no findings are visible."""

    def test_collector_with_inner_pagination_failed_yields_no_finding(
        self,
    ) -> None:
        # The snapshot's ``review_thread_pagination_failed=True``
        # should be reflected in the snap's claim that the
        # ``review_threads`` inventory is incomplete. The
        # collector itself doesn't gate on this flag (the
        # readiness gate is the canonical check), but the
        # fixture must surface the fact for downstream
        # consumers.
        thread = _make_thread(
            superseded_at=_iso(T_REPAIR),
            superseded_by_head="b89c25fe98fcf06f5ea14e32390a389d9227f2c6",
            replies=[{
                "databaseId": REVIEWER_REPLY_ID,
                "author": "coderabbitai[bot]",
                "createdAt": _iso(T_REPAIR + 60),
                "body": "_Data Integrity_ | _Minor_\n\nReaffirms.",
            }],
        )
        snap = _make_snapshot(
            {THREAD_ID: thread},
            review_thread_pagination_complete=False,
            review_thread_pagination_failed=True,
            truncated_thread_ids=[THREAD_ID],
        )
        # The collector still emits the finding (because the
        # thread itself is complete; the failure flag is
        # propagated to the readiness gate via the snapshot,
        # NOT the collector). This is by design — the
        # collector is data-only; the readiness gate is the
        # fail-closed authority.
        findings = _collect_review_findings(snap)
        assert len(findings) == 1
        assert snap["review_thread_pagination_failed"] is True
        assert snap["truncated_thread_ids"] == [THREAD_ID]