"""Round-C23R1 regression tests for the defects surfaced by
the audit:

A. ``_provider_review_record_for_head`` previously returned
   the FIRST matching provider review instead of walking
   ALL reviews and picking the best one by priority. The
   fix walks all reviews and selects:

   1. Any exact-head (``commit_id == head_sha``) match
   2. Otherwise the most-recent STALE record
   3. Otherwise the most-recent UNBOUND record
   4. Fallback to issue comments / provider_surfaces

B. The default C23 policy marked CodeRabbit as REQUIRED on
   every repair head, which created a budget deadlock
   (initial review consumed → repair head → BLOCK). The
   fix introduces a phase-aware policy:
   ``Codex: required on both phases``,
   ``CodeRabbit: required on INITIAL_HEAD, optional on
   REPAIR_HEAD`` (its quota was already consumed by the
   initial review).

C. ``_count_active_request_records`` previously returned a
   binary ``max(0, 1 - superseded_for_provider)`` that
   conflated per-head and PR-lifetime accounting. The fix
   walks the canonical ``{provider}__{head}.json`` files
   and returns ``(on_current_head, in_pr_lifecycle)``.

The tests cover all the required cases from the C23R1
directive §5 (end-to-end policy tests A–F) plus the defect
reproduction list from §4.
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


from autocoder_supervisor.reviewer_policy import (  # noqa: E402
    DEFAULT_POLICY_PROFILES,
    FRESHNESS_FRESH,
    FRESHNESS_OPTIONAL_STALE,
    FRESHNESS_PENDING,
    FRESHNESS_STALE,
    PHASE_INITIAL_HEAD,
    PHASE_REPAIR_HEAD,
    ReviewerPolicy,
    ReviewerTriggerPlan,
    _count_active_request_records,
    _most_recent_comment,
    _most_recent_review,
    _provider_review_record_for_head,
    check_provider_freshness,
    load_policies_from_providers,
    plan_reviewer_actions,
    resolve_phase,
    resolve_phase_from_state_root,
)


CURRENT_HEAD = "b" * 40
PRIOR_HEAD = "a" * 40
OTHER_HEAD = "c" * 40


# === Defect A — formal review ordering ===

class TestDefectAFormalReviewOrdering:
    """The audit's specific reproduction cases. An exact-head
    formal review must always win regardless of position in
    the list."""

    def test_old_then_fresh_review_picks_fresh(self) -> None:
        # List order: [old_A, fresh_B]. The old implementation
        # iterated in snapshot order and returned A as stale,
        # missing B entirely.
        snap = {
            "formal_reviews": [
                {
                    "provider": "codex",
                    "commit_id": PRIOR_HEAD,
                    "submitted_at": "2026-08-19T13:00:00Z",
                    "review_id": "rev-old",
                },
                {
                    "provider": "codex",
                    "commit_id": CURRENT_HEAD,
                    "submitted_at": "2026-08-19T14:30:00Z",
                    "review_id": "rev-fresh",
                },
            ],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
        }
        record = _provider_review_record_for_head(
            snap=snap, provider="codex", head_sha=CURRENT_HEAD,
        )
        assert record is not None
        assert record["kind"] == "formal_review"
        assert record["commit_id"] == CURRENT_HEAD
        assert record["review_id"] == "rev-fresh"

    def test_fresh_then_old_review_picks_fresh(self) -> None:
        snap = {
            "formal_reviews": [
                {
                    "provider": "codex",
                    "commit_id": CURRENT_HEAD,
                    "submitted_at": "2026-08-19T14:30:00Z",
                    "review_id": "rev-fresh",
                },
                {
                    "provider": "codex",
                    "commit_id": PRIOR_HEAD,
                    "submitted_at": "2026-08-19T13:00:00Z",
                    "review_id": "rev-old",
                },
            ],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
        }
        record = _provider_review_record_for_head(
            snap=snap, provider="codex", head_sha=CURRENT_HEAD,
        )
        assert record is not None
        assert record["kind"] == "formal_review"
        assert record["commit_id"] == CURRENT_HEAD
        assert record["review_id"] == "rev-fresh"

    def test_multiple_old_and_one_fresh_picks_fresh(self) -> None:
        snap = {
            "formal_reviews": [
                {"provider": "codex", "commit_id": "x" * 40,
                 "submitted_at": "2026-08-19T10:00:00Z",
                 "review_id": "rev-x"},
                {"provider": "codex", "commit_id": "y" * 40,
                 "submitted_at": "2026-08-19T11:00:00Z",
                 "review_id": "rev-y"},
                {"provider": "codex", "commit_id": "z" * 40,
                 "submitted_at": "2026-08-19T12:00:00Z",
                 "review_id": "rev-z"},
                {"provider": "codex", "commit_id": CURRENT_HEAD,
                 "submitted_at": "2026-08-19T14:30:00Z",
                 "review_id": "rev-fresh"},
            ],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
        }
        record = _provider_review_record_for_head(
            snap=snap, provider="codex", head_sha=CURRENT_HEAD,
        )
        assert record is not None
        assert record["kind"] == "formal_review"
        assert record["review_id"] == "rev-fresh"

    def test_multiple_stale_records_picks_most_recent(self) -> None:
        # No exact-head match: the planner must surface the
        # MOST RECENT stale record (priority #2). The audit
        # specifically calls out "do not depend on GitHub
        # list ordering"; the fix selects by ``submitted_at``.
        snap = {
            "formal_reviews": [
                {"provider": "codex", "commit_id": "x" * 40,
                 "submitted_at": "2026-08-19T10:00:00Z",
                 "review_id": "rev-x"},
                {"provider": "codex", "commit_id": "y" * 40,
                 "submitted_at": "2026-08-19T13:00:00Z",
                 "review_id": "rev-y"},
                {"provider": "codex", "commit_id": "z" * 40,
                 "submitted_at": "2026-08-19T11:00:00Z",
                 "review_id": "rev-z"},
            ],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
        }
        record = _provider_review_record_for_head(
            snap=snap, provider="codex", head_sha=CURRENT_HEAD,
        )
        assert record is not None
        assert record["kind"] == "formal_review_stale_commit"
        # Priority #2 picks the most-recent stale record by
        # ``submitted_at``: y @ 13:00 > z @ 11:00 > x @ 10:00.
        assert record["review_id"] == "rev-y"
        assert record["commit_id"] == "y" * 40

    def test_malformed_entries_does_not_break_selection(self) -> None:
        # Mix of valid and malformed entries. The fix must
        # silently skip non-dict entries and entries missing
        # ``provider`` / ``submitted_at`` without crashing.
        snap = {
            "formal_reviews": [
                "not-a-dict",  # malformed
                {"provider": "different-provider"},  # wrong provider
                {"provider": "codex", "commit_id": "x" * 40,
                 "submitted_at": "invalid-timestamp",
                 "review_id": "rev-x"},
                {"provider": "codex", "commit_id": CURRENT_HEAD,
                 "submitted_at": "2026-08-19T14:30:00Z",
                 "review_id": "rev-fresh"},
            ],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
        }
        record = _provider_review_record_for_head(
            snap=snap, provider="codex", head_sha=CURRENT_HEAD,
        )
        assert record is not None
        assert record["kind"] == "formal_review"
        assert record["review_id"] == "rev-fresh"

    def test_exact_head_provider_surface_wins_over_stale_formal(
        self,
    ) -> None:
        # The audit explicitly calls out: "exact-head
        # provider_surface available alongside stale formal
        # review". The fix must consult provider_surfaces
        # AFTER formal reviews (priority #5), so an exact-
        # head provider_surface only wins when no exact-head
        # formal review exists.
        snap = {
            "formal_reviews": [
                {"provider": "codex", "commit_id": PRIOR_HEAD,
                 "submitted_at": "2026-08-19T13:00:00Z",
                 "review_id": "rev-stale"},
            ],
            "provider_surfaces": {
                "codex": {
                    "heads": {
                        CURRENT_HEAD: {
                            "reviewed_at": "2026-08-19T14:30:00Z",
                            "review_id": "surf-fresh",
                        },
                    },
                },
            },
            "_provider_issue_comments": {},
            "providers": {},
        }
        record = _provider_review_record_for_head(
            snap=snap, provider="codex", head_sha=CURRENT_HEAD,
        )
        assert record is not None
        # Provider-surface exact-head binding wins (priority
        # #5 — the supervisor persisted it at snapshot time).
        assert record["kind"] == "provider_surface"
        assert record["review_id"] == "surf-fresh"


class TestFreshnessRespectsReviewOrdering:
    """``check_provider_freshness`` MUST honor the new
    review-selection algorithm."""

    def test_old_then_fresh_is_fresh(self) -> None:
        snap = {
            "formal_reviews": [
                {"provider": "codex", "commit_id": PRIOR_HEAD,
                 "submitted_at": "2026-08-19T13:00:00Z",
                 "review_id": "rev-old"},
                {"provider": "codex", "commit_id": CURRENT_HEAD,
                 "submitted_at": "2026-08-19T14:30:00Z",
                 "review_id": "rev-fresh"},
            ],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
        }
        f = check_provider_freshness(
            provider="codex",
            policy=ReviewerPolicy(name="codex", required=True, auto_trigger=True),
            snap=snap, head_sha=CURRENT_HEAD,
        )
        assert f.state == FRESHNESS_FRESH


# === Defect B — phase-aware policy ===

class TestDefectBPhaseAwareCodeRabbit:
    """The C23R1 fix changes CodeRabbit from
    ``required=True`` everywhere to ``required[INITIAL]=True``,
    ``required[REPAIR]=False``. The repair head must NOT
    block on stale/absent CodeRabbit evidence."""

    def test_initial_head_coderabbit_required_and_requested(
        self, tmp_path: Path,
    ) -> None:
        snap = {
            "formal_reviews": [],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
        }
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={
                "coderabbit": ReviewerPolicy(**{
                    **DEFAULT_POLICY_PROFILES["coderabbit"],
                    "name": "coderabbit",
                }),
            },
            ledger_path=tmp_path / "review_requests",
            phase=PHASE_INITIAL_HEAD,
        )
        assert plans["coderabbit"].action == "REQUEST"

    def test_repair_head_coderabbit_optional_and_not_needed(
        self, tmp_path: Path,
    ) -> None:
        # Initial CodeRabbit review was already consumed on
        # the prior head. The repair head must NOT request
        # another one and must NOT block.
        snap = {
            "formal_reviews": [],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
        }
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={
                "coderabbit": ReviewerPolicy(**{
                    **DEFAULT_POLICY_PROFILES["coderabbit"],
                    "name": "coderabbit",
                }),
            },
            ledger_path=tmp_path / "review_requests",
            phase=PHASE_REPAIR_HEAD,
        )
        assert plans["coderabbit"].action == "NOT_NEEDED"
        assert "phase_disabled" in plans["coderabbit"].reason

    def test_repair_head_coderabbit_stale_evidence_not_blocking(
        self, tmp_path: Path,
    ) -> None:
        # CodeRabbit has a STALE formal review anchored to
        # the prior head. The planner must classify it as
        # OPTIONAL_STALE (NOT a BLOCK / REQUEST) because
        # CodeRabbit is OPTIONAL on repair heads.
        snap = {
            "formal_reviews": [
                {"provider": "coderabbit",
                 "commit_id": PRIOR_HEAD,
                 "submitted_at": "2026-08-19T13:00:00Z",
                 "review_id": "rev-stale-cr"},
            ],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
        }
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={
                "coderabbit": ReviewerPolicy(**{
                    **DEFAULT_POLICY_PROFILES["coderabbit"],
                    "name": "coderabbit",
                }),
            },
            ledger_path=tmp_path / "review_requests",
            phase=PHASE_REPAIR_HEAD,
        )
        # Required+stale on REPAIR_HEAD with phase_required
        # False: planner sees OPTIONAL_STALE -> NOT_NEEDED.
        assert plans["coderabbit"].action == "NOT_NEEDED"


# === Defect C — request accounting repair ===

class TestDefectCRequestAccounting:
    """``_count_active_request_records`` must return
    semantically correct ``(on_current_head, in_pr_lifecycle)``
    counts."""

    def test_zero_records(self, tmp_path: Path) -> None:
        ledger = tmp_path / "review_requests"
        ledger.mkdir()
        on_current, in_pr = _count_active_request_records(
            ledger_path=ledger,
            provider="codex",
            head_sha=CURRENT_HEAD,
            superseded_records=[],
        )
        assert (on_current, in_pr) == (0, 0)

    def test_one_current_head_request(self, tmp_path: Path) -> None:
        ledger = tmp_path / "review_requests"
        ledger.mkdir()
        (ledger / f"codex__{CURRENT_HEAD}.json").write_text(json.dumps({
            "lifecycle": "REQUEST_SENT",
            "requested_at": "2026-08-19T14:30:00Z",
            "request_id": "req-1",
        }))
        on_current, in_pr = _count_active_request_records(
            ledger_path=ledger,
            provider="codex",
            head_sha=CURRENT_HEAD,
            superseded_records=[],
        )
        assert (on_current, in_pr) == (1, 1)

    def test_previous_head_active_record_does_not_count_for_current(
        self, tmp_path: Path,
    ) -> None:
        # Active record on the PRIOR head only; the current
        # head's count must be 0 (different head) while the
        # PR-lifetime count is 1.
        ledger = tmp_path / "review_requests"
        ledger.mkdir()
        (ledger / f"codex__{PRIOR_HEAD}.json").write_text(json.dumps({
            "lifecycle": "REQUEST_SENT",
            "requested_at": "2026-08-19T13:00:00Z",
            "request_id": "req-old",
        }))
        on_current, in_pr = _count_active_request_records(
            ledger_path=ledger,
            provider="codex",
            head_sha=CURRENT_HEAD,
            superseded_records=[],
        )
        assert on_current == 0
        assert in_pr == 1

    def test_superseded_previous_head_record_not_double_counted(
        self, tmp_path: Path,
    ) -> None:
        # Active record on PRIOR head + SUPERSEDED for the
        # same (provider, prior_head) pair. The per-head
        # counter for the current head stays at 0; the PR
        # counter still counts the canonical ``{provider}__{prior_head}.json``
        # because SUPERSEDED records live in a separate file
        # and the helper does NOT subtract them from the
        # PR-lifetime counter (Defect B fix).
        ledger = tmp_path / "review_requests"
        ledger.mkdir()
        (ledger / f"codex__{PRIOR_HEAD}.json").write_text(json.dumps({
            "lifecycle": "REQUEST_SENT",
            "requested_at": "2026-08-19T13:00:00Z",
            "request_id": "req-old",
        }))
        (ledger / f"codex__{PRIOR_HEAD}.superseded.json").write_text(json.dumps({
            "lifecycle": "SUPERSEDED",
            "superseded_by_head": CURRENT_HEAD,
        }))
        on_current, in_pr = _count_active_request_records(
            ledger_path=ledger,
            provider="codex",
            head_sha=CURRENT_HEAD,
            superseded_records=[
                {
                    "provider": "codex",
                    "superseded_head": PRIOR_HEAD,
                    "superseded_by_head": CURRENT_HEAD,
                },
            ],
        )
        assert on_current == 0
        assert in_pr == 1

    def test_multiple_previous_heads(self, tmp_path: Path) -> None:
        # Three prior heads each with an active record; the
        # current head has none. The PR-lifetime counter is
        # 3; the current-head counter is 0.
        ledger = tmp_path / "review_requests"
        ledger.mkdir()
        for h in ("a" * 40, "b" * 40, "c" * 40):
            (ledger / f"codex__{h}.json").write_text(json.dumps({
                "lifecycle": "REQUEST_SENT",
                "requested_at": "2026-08-19T13:00:00Z",
                "request_id": f"req-{h[:6]}",
            }))
        on_current, in_pr = _count_active_request_records(
            ledger_path=ledger,
            provider="codex",
            head_sha="z" * 40,
            superseded_records=[],
        )
        assert on_current == 0
        assert in_pr == 3

    def test_current_head_plus_superseded_histories(
        self, tmp_path: Path,
    ) -> None:
        # Two superseded prior heads + one active on the
        # current head. PR counter = 3; current counter = 1.
        # Use distinct heads so the file names do not
        # collide.
        ledger = tmp_path / "review_requests"
        ledger.mkdir()
        prior_a = "a" * 40
        prior_b = "c" * 40  # distinct from CURRENT_HEAD
        for h in (prior_a, prior_b):
            (ledger / f"codex__{h}.json").write_text(json.dumps({
                "lifecycle": "REQUEST_SENT",
                "requested_at": "2026-08-19T13:00:00Z",
                "request_id": f"req-{h[:6]}",
            }))
            (ledger / f"codex__{h}.superseded.json").write_text(json.dumps({
                "lifecycle": "SUPERSEDED",
                "superseded_by_head": CURRENT_HEAD,
            }))
        (ledger / f"codex__{CURRENT_HEAD}.json").write_text(json.dumps({
            "lifecycle": "REQUEST_SENT",
            "requested_at": "2026-08-19T14:30:00Z",
            "request_id": "req-current",
        }))
        on_current, in_pr = _count_active_request_records(
            ledger_path=ledger,
            provider="codex",
            head_sha=CURRENT_HEAD,
            superseded_records=[
                {"provider": "codex",
                 "superseded_head": prior_a,
                 "superseded_by_head": CURRENT_HEAD},
                {"provider": "codex",
                 "superseded_head": prior_b,
                 "superseded_by_head": CURRENT_HEAD},
            ],
        )
        assert on_current == 1
        assert in_pr == 3


# === End-to-end policy matrix (C23R1 §5) ===

class TestEndToEndPolicyMatrix:
    """§5 of the C23R1 directive requires explicit
    end-to-end policy tests A–F."""

    def test_A_old_codex_review_plus_fresh_exact_head_codex(
        self, tmp_path: Path,
    ) -> None:
        # Old codex review on prior head + fresh exact-head
        # codex review on current head -> Codex FRESH, no
        # request.
        snap = {
            "formal_reviews": [
                {"provider": "codex", "commit_id": PRIOR_HEAD,
                 "submitted_at": "2026-08-19T13:00:00Z",
                 "review_id": "rev-old"},
                {"provider": "codex", "commit_id": CURRENT_HEAD,
                 "submitted_at": "2026-08-19T14:30:00Z",
                 "review_id": "rev-fresh"},
            ],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
        }
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={
                "codex": ReviewerPolicy(**{
                    **DEFAULT_POLICY_PROFILES["codex"],
                    "name": "codex",
                }),
            },
            ledger_path=tmp_path / "review_requests",
            phase=PHASE_REPAIR_HEAD,
        )
        assert plans["codex"].action == "NOT_NEEDED"

    def test_B_initial_pr_no_coderabbit_review(
        self, tmp_path: Path,
    ) -> None:
        # Initial PR, no CodeRabbit review -> CodeRabbit
        # REQUIRED/REQUESTED.
        snap = {
            "formal_reviews": [],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
        }
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={
                "coderabbit": ReviewerPolicy(**{
                    **DEFAULT_POLICY_PROFILES["coderabbit"],
                    "name": "coderabbit",
                }),
            },
            ledger_path=tmp_path / "review_requests",
            phase=PHASE_INITIAL_HEAD,
        )
        assert plans["coderabbit"].action == "REQUEST"

    def test_C_initial_coderabbit_review_consumed_then_repair_head(
        self, tmp_path: Path,
    ) -> None:
        # Initial CodeRabbit review consumed on head A; the
        # worker pushes head B (repair). CodeRabbit is
        # OPTIONAL_STALE / NOT_NEEDED on the repair head and
        # does NOT block readiness.
        snap = {
            "formal_reviews": [
                {"provider": "coderabbit", "commit_id": PRIOR_HEAD,
                 "submitted_at": "2026-08-19T13:00:00Z",
                 "review_id": "rev-cr-stale"},
            ],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
        }
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={
                "coderabbit": ReviewerPolicy(**{
                    **DEFAULT_POLICY_PROFILES["coderabbit"],
                    "name": "coderabbit",
                }),
            },
            ledger_path=tmp_path / "review_requests",
            phase=PHASE_REPAIR_HEAD,
        )
        assert plans["coderabbit"].action == "NOT_NEEDED"

    def test_D_stale_codex_on_repair_head_requests(
        self, tmp_path: Path,
    ) -> None:
        # Codex anchored to prior head on the repair head ->
        # Codex REQUEST, readiness blocks pending Codex.
        snap = {
            "formal_reviews": [
                {"provider": "codex", "commit_id": PRIOR_HEAD,
                 "submitted_at": "2026-08-19T13:00:00Z",
                 "review_id": "rev-codex-stale"},
            ],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
        }
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={
                "codex": ReviewerPolicy(**{
                    **DEFAULT_POLICY_PROFILES["codex"],
                    "name": "codex",
                }),
            },
            ledger_path=tmp_path / "review_requests",
            phase=PHASE_REPAIR_HEAD,
        )
        assert plans["codex"].action == "REQUEST"

    def test_E_fresh_codex_codex_not_needed_coderabbit_optional(
        self, tmp_path: Path,
    ) -> None:
        # Fresh Codex arrives; Codex NOT_NEEDED. CodeRabbit
        # remains optional (no review). Both providers'
        # plans do NOT block.
        snap = {
            "formal_reviews": [
                {"provider": "codex", "commit_id": CURRENT_HEAD,
                 "submitted_at": "2026-08-19T14:30:00Z",
                 "review_id": "rev-codex-fresh"},
            ],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
        }
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={
                "codex": ReviewerPolicy(**{
                    **DEFAULT_POLICY_PROFILES["codex"],
                    "name": "codex",
                }),
                "coderabbit": ReviewerPolicy(**{
                    **DEFAULT_POLICY_PROFILES["coderabbit"],
                    "name": "coderabbit",
                }),
            },
            ledger_path=tmp_path / "review_requests",
            phase=PHASE_REPAIR_HEAD,
        )
        assert plans["codex"].action == "NOT_NEEDED"
        assert plans["coderabbit"].action == "NOT_NEEDED"

    def test_F_explicit_override_codex_required_on_repair_blocks(
        self, tmp_path: Path,
    ) -> None:
        # Explicit override: CodeRabbit phase_required REPAIR
        # = True with budget=0 -> BLOCK (audit's §5 case F).
        snap = {
            "formal_reviews": [],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
        }
        policy = ReviewerPolicy(**{
            **DEFAULT_POLICY_PROFILES["coderabbit"],
            "name": "coderabbit",
            # Override: phase_required REPAIR=True.
            "phase_required": {
                PHASE_INITIAL_HEAD: True,
                PHASE_REPAIR_HEAD: True,
            },
        })
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={"coderabbit": policy},
            ledger_path=tmp_path / "review_requests",
            phase=PHASE_REPAIR_HEAD,
        )
        assert plans["coderabbit"].action == "BLOCK"


# === Phase resolution ===

class TestPhaseResolver:
    """``resolve_phase_from_state_root`` counts
    ``control_plane.repair_pushed`` rows in the per-run
    ``state.json`` journal."""

    def test_no_state_root_returns_initial(self) -> None:
        from autocoder_supervisor.reviewer_policy import (
            resolve_phase_from_state_root,
        )
        phase, last_old, count = resolve_phase_from_state_root(
            state_root=None,
        )
        assert phase == PHASE_INITIAL_HEAD
        assert last_old is None
        assert count == 0

    def test_no_journal_returns_initial(self, tmp_path: Path) -> None:
        phase, last_old, count = resolve_phase_from_state_root(
            state_root=tmp_path,
        )
        assert phase == PHASE_INITIAL_HEAD
        assert count == 0

    def test_no_repair_pushed_returns_initial(self, tmp_path: Path) -> None:
        (tmp_path / "state.json").write_text(json.dumps({
            "current_state": "QUALIFYING_READINESS",
            "journal": [
                {"from": "QUALIFYING_READINESS", "to": "READY_FOR_CANDIDATE",
                 "event": "control_plane.ready_for_candidate",
                 "head_observed": CURRENT_HEAD},
            ],
        }))
        phase, _, count = resolve_phase_from_state_root(state_root=tmp_path)
        assert phase == PHASE_INITIAL_HEAD
        assert count == 0

    def test_one_repair_pushed_returns_repair(self, tmp_path: Path) -> None:
        (tmp_path / "state.json").write_text(json.dumps({
            "current_state": "AWAITING_CI",
            "journal": [
                {"from": "REPAIRING_REVIEW_FINDINGS",
                 "to": "AWAITING_CI",
                 "event": "control_plane.repair_pushed",
                 "head_observed": PRIOR_HEAD,
                 "head_required": CURRENT_HEAD},
            ],
        }))
        phase, last_old, count = resolve_phase_from_state_root(
            state_root=tmp_path,
        )
        assert phase == PHASE_REPAIR_HEAD
        assert last_old == PRIOR_HEAD
        assert count == 1

    def test_explicit_phase_overrides_state_root(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "state.json").write_text(json.dumps({
            "current_state": "AWAITING_CI",
            "journal": [
                {"event": "control_plane.repair_pushed",
                 "head_observed": PRIOR_HEAD},
            ],
        }))
        # The explicit override wins even when the state
        # root would say REPAIR_HEAD.
        assert resolve_phase(
            state_root=tmp_path,
            explicit_phase=PHASE_INITIAL_HEAD,
        ) == PHASE_INITIAL_HEAD


# === Helpers ===

class TestMostRecentHelpers:
    """``_most_recent_review`` and ``_most_recent_comment``
    helpers must order by ``submitted_at`` descending."""

    def test_most_recent_review_orders_by_submitted_at(self) -> None:
        reviews = [
            {"submitted_at": "2026-08-19T10:00:00Z", "review_id": "old"},
            {"submitted_at": "2026-08-19T14:00:00Z", "review_id": "newest"},
            {"submitted_at": "2026-08-19T12:00:00Z", "review_id": "mid"},
        ]
        assert _most_recent_review(reviews)["review_id"] == "newest"

    def test_most_recent_comment_orders_by_created_at(self) -> None:
        comments = [
            {"created_at": "2026-08-19T10:00:00Z", "id": "old"},
            {"created_at": "2026-08-19T14:00:00Z", "id": "newest"},
            {"created_at": "2026-08-19T12:00:00Z", "id": "mid"},
        ]
        assert _most_recent_comment(comments)["id"] == "newest"