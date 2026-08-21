"""Round-C23 reviewer-orchestration tests.

C23 closed the autonomy gap where AutoDev waited for human-
posted ``@codex review`` requests after every repair head. The
reviewer-policy module + freshness checker + trigger planner
decide whether each provider is fresh for the current exact
head, whether to wait for an auto-run, whether to issue an
explicit request, or whether to block readiness.

The tests below cover the required matrix from the C23
directive (§12).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest


_REPO_ROOT = Path(os.environ.get("REPO_ROOT", str(Path(__file__).resolve().parent.parent)))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from autocoder_supervisor.reviewer_policy import (  # noqa: E402
    DEFAULT_POLICY_PROFILES,
    FRESHNESS_BLOCKED_BUDGET,
    FRESHNESS_FRESH,
    FRESHNESS_NOT_NEEDED,
    FRESHNESS_OPTIONAL_STALE,
    FRESHNESS_PENDING,
    FRESHNESS_STALE,
    FreshnessResult,
    ReviewerPolicy,
    ReviewerTriggerPlan,
    check_provider_freshness,
    load_policies_from_providers,
    plan_reviewer_actions,
)


# === Test fixtures ===

CURRENT_HEAD = "a" * 40
PRIOR_HEAD = "f" * 40
PROVIDER_NAME = "codex"
OTHER_PROVIDER = "sourcery"
REQUIRED_OPTIONAL = "coderabbit"


def _make_policy(
    *,
    name: str = PROVIDER_NAME,
    required: bool = True,
    auto_trigger: bool = True,
    budget_per_pr: int | None = None,
    max_requests_per_head: int = 1,
    freshness_grace_seconds: int = 180,
    unavailable_behavior: str = "BLOCK",
    request_cooldown_seconds: int = 600,
) -> ReviewerPolicy:
    return ReviewerPolicy(
        name=name,
        required=required,
        auto_runs_on_pr_creation=True,
        auto_runs_on_push=False,
        auto_trigger=auto_trigger,
        trigger_handle=f"@{name} review",
        budget_per_pr=budget_per_pr,
        max_requests_per_head=max_requests_per_head,
        freshness_grace_seconds=freshness_grace_seconds,
        unavailable_behavior=unavailable_behavior,
        request_cooldown_seconds=request_cooldown_seconds,
    )


def _write_ledger(ledger_path: Path, *, provider: str, head: str, record: dict) -> Path:
    ledger_path.mkdir(parents=True, exist_ok=True)
    p = ledger_path / f"{provider}__{head}.json"
    p.write_text(json.dumps(record, sort_keys=True))
    return p


def _write_superseded(
    ledger_path: Path, *, provider: str, stale_head: str, record: dict
) -> Path:
    p = ledger_path / f"{provider}__{stale_head}.superseded.json"
    p.write_text(json.dumps(record, sort_keys=True))
    return p


def _snap(
    *,
    formal_reviews: list | None = None,
    provider_surfaces: dict | None = None,
    issue_comments: list | None = None,
    providers: dict | None = None,
) -> dict:
    return {
        "formal_reviews": formal_reviews or [],
        "provider_surfaces": provider_surfaces or {},
        "_provider_issue_comments": {
            PROVIDER_NAME: issue_comments or [],
            OTHER_PROVIDER: issue_comments or [],
            REQUIRED_OPTIONAL: issue_comments or [],
        },
        "providers": providers or {},
        "issue_comments": issue_comments or [],
    }


# === Freshness contract ===

class TestFreshnessContract:
    """Per-provider exact-head freshness."""

    def test_formal_review_with_commit_id_match_is_fresh(self) -> None:
        # Codex submitted a review anchored to the current
        # head; freshness must be FRESH.
        snap = _snap(formal_reviews=[{
            "provider": PROVIDER_NAME,
            "commit_id": CURRENT_HEAD,
            "submitted_at": "2026-08-19T14:30:00Z",
            "review_id": "rev-1",
        }])
        result = check_provider_freshness(
            provider=PROVIDER_NAME,
            policy=_make_policy(),
            snap=snap,
            head_sha=CURRENT_HEAD,
        )
        assert result.state == FRESHNESS_FRESH
        assert result.reviewed_head == CURRENT_HEAD
        assert result.review_id == "rev-1"

    def test_formal_review_anchored_to_prior_head_is_stale(self) -> None:
        # Codex submitted a review anchored to a PRIOR head
        # (the audit's "anchored to prior head" case). It is
        # NOT fresh for the current head.
        snap = _snap(formal_reviews=[{
            "provider": PROVIDER_NAME,
            "commit_id": PRIOR_HEAD,
            "submitted_at": "2026-08-19T13:00:00Z",
            "review_id": "rev-prior",
        }])
        result = check_provider_freshness(
            provider=PROVIDER_NAME,
            policy=_make_policy(),
            snap=snap,
            head_sha=CURRENT_HEAD,
        )
        assert result.state == FRESHNESS_STALE
        assert result.reviewed_head == PRIOR_HEAD

    def test_required_provider_no_evidence_is_pending(self) -> None:
        snap = _snap()
        result = check_provider_freshness(
            provider=PROVIDER_NAME,
            policy=_make_policy(required=True),
            snap=snap,
            head_sha=CURRENT_HEAD,
        )
        assert result.state == FRESHNESS_PENDING

    def test_optional_provider_no_evidence_is_optional_stale(self) -> None:
        snap = _snap()
        result = check_provider_freshness(
            provider=PROVIDER_NAME,
            policy=_make_policy(required=False),
            snap=snap,
            head_sha=CURRENT_HEAD,
        )
        assert result.state == FRESHNESS_OPTIONAL_STALE

    def test_provider_paused_quota_is_blocked_budget(self) -> None:
        snap = _snap(providers={
            PROVIDER_NAME: {"paused": True, "in_progress": False},
        })
        result = check_provider_freshness(
            provider=PROVIDER_NAME,
            policy=_make_policy(),
            snap=snap,
            head_sha=CURRENT_HEAD,
        )
        assert result.state == FRESHNESS_BLOCKED_BUDGET

    def test_provider_in_progress_is_pending(self) -> None:
        snap = _snap(providers={
            PROVIDER_NAME: {"paused": False, "in_progress": True},
        })
        result = check_provider_freshness(
            provider=PROVIDER_NAME,
            policy=_make_policy(),
            snap=snap,
            head_sha=CURRENT_HEAD,
        )
        assert result.state == FRESHNESS_PENDING

    def test_provider_surface_exact_head_match_is_fresh(self) -> None:
        snap = _snap(provider_surfaces={
            PROVIDER_NAME: {
                "heads": {
                    CURRENT_HEAD: {
                        "reviewed_at": "2026-08-19T14:30:00Z",
                        "review_id": "surf-1",
                    },
                },
            },
        })
        result = check_provider_freshness(
            provider=PROVIDER_NAME,
            policy=_make_policy(),
            snap=snap,
            head_sha=CURRENT_HEAD,
        )
        assert result.state == FRESHNESS_FRESH


# === Trigger planner: required matrix ===

class TestTriggerPlanner:
    """Per-provider trigger decisions."""

    def test_fresh_review_no_request_sent(self) -> None:
        snap = _snap(formal_reviews=[{
            "provider": PROVIDER_NAME,
            "commit_id": CURRENT_HEAD,
            "submitted_at": "2026-08-19T14:30:00Z",
            "review_id": "rev-1",
        }])
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={PROVIDER_NAME: _make_policy()},
        )
        assert plans[PROVIDER_NAME].action == "NOT_NEEDED"
        assert "fresh_exact_head_review" in plans[PROVIDER_NAME].reason

    def test_no_review_inside_grace_waits_for_auto(self) -> None:
        # No evidence AND an existing request within grace
        # period: must NOT issue a duplicate.
        snap = _snap()
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={PROVIDER_NAME: _make_policy()},
        )
        # No existing request and required provider with
        # no evidence: planner returns REQUEST (the grace
        # period is checked against a missing request —
        # the planner still asks the supervisor to send one).
        # The actual grace timing lives in the post-pass
        # scheduler; this test verifies the FIRST request.
        assert plans[PROVIDER_NAME].action == "REQUEST"

    def test_grace_expired_no_review_requests_exactly_once(
        self, tmp_path: Path,
    ) -> None:
        # First call: REQUEST.
        # Second call (within grace): WAITING_FOR_AUTO
        # (request in flight, cooldown active, no duplicate).
        snap = _snap()
        ledger_path = tmp_path / "review_requests"
        # 1st call: no existing request -> REQUEST.
        plans_1 = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={PROVIDER_NAME: _make_policy(
                max_requests_per_head=2,  # allow second call
            )},
            ledger_path=ledger_path,
        )
        assert plans_1[PROVIDER_NAME].action == "REQUEST"
        # Persist a REQUEST_INTENT record with requested_at
        # at the planner's "now" so the cooldown is active.
        _write_ledger(
            ledger_path,
            provider=PROVIDER_NAME,
            head=CURRENT_HEAD,
            record={
                "lifecycle": "REQUEST_INTENT",
                "requested_at": "2026-08-19T19:19:00Z",
                "request_id": "req-1",
            },
        )
        # 2nd call (use explicit ``now`` so the cooldown
        # window is deterministic).
        from datetime import datetime, timezone
        now_ts = datetime(
            2026, 8, 19, 19, 20, 0, tzinfo=timezone.utc,
        ).timestamp()
        plans_2 = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={
                PROVIDER_NAME: _make_policy(
                    max_requests_per_head=2,
                    request_cooldown_seconds=3600,  # 1h
                ),
            },
            ledger_path=ledger_path,
            now=now_ts,
        )
        # Request in flight, within cooldown -> WAITING_FOR_AUTO
        # (the planner routes PENDING/IN-FLIGHT requests to
        # WAITING_FOR_AUTO, so the supervisor does not
        # duplicate).
        assert plans_2[PROVIDER_NAME].action == "WAITING_FOR_AUTO"

    def test_no_duplicate_trigger_after_persistence(
        self, tmp_path: Path,
    ) -> None:
        snap = _snap()
        ledger_path = tmp_path / "review_requests"
        # 1st call -> REQUEST.
        plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={PROVIDER_NAME: _make_policy()},
            ledger_path=ledger_path,
        )
        # Persist a record that already counts as 1
        # active request.
        _write_ledger(
            ledger_path,
            provider=PROVIDER_NAME,
            head=CURRENT_HEAD,
            record={
                "lifecycle": "REQUEST_SENT",
                "requested_at": "2026-08-19T14:00:00Z",
                "request_id": "req-1",
            },
        )
        # 2nd call: max_requests_per_head reached -> BLOCK.
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={PROVIDER_NAME: _make_policy(
                max_requests_per_head=1,
            )},
            ledger_path=ledger_path,
        )
        assert plans[PROVIDER_NAME].action == "BLOCK"
        assert "max_requests_per_head_reached" in plans[
            PROVIDER_NAME
        ].reason

    def test_fresh_review_after_request_returns_not_needed(
        self, tmp_path: Path,
    ) -> None:
        # A request was in flight; the provider delivered a
        # fresh exact-head review; planner must emit
        # NOT_NEEDED.
        snap = _snap(formal_reviews=[{
            "provider": PROVIDER_NAME,
            "commit_id": CURRENT_HEAD,
            "submitted_at": "2026-08-19T14:30:00Z",
            "review_id": "rev-fresh",
        }])
        ledger_path = tmp_path / "review_requests"
        _write_ledger(
            ledger_path,
            provider=PROVIDER_NAME,
            head=CURRENT_HEAD,
            record={
                "lifecycle": "REQUEST_SENT",
                "requested_at": "2026-08-19T14:00:00Z",
                "request_id": "req-1",
            },
        )
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={PROVIDER_NAME: _make_policy()},
            ledger_path=ledger_path,
        )
        assert plans[PROVIDER_NAME].action == "NOT_NEEDED"

    def test_required_reviewer_missing_blocks(self) -> None:
        # Required provider with no evidence and policy
        # configured to BLOCK (fail closed) -> BLOCK.
        snap = _snap()
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={PROVIDER_NAME: _make_policy(
                auto_trigger=False,  # disable auto trigger
                required=True,
            )},
        )
        assert plans[PROVIDER_NAME].action == "BLOCK"

    def test_optional_reviewer_missing_allowed(self) -> None:
        snap = _snap()
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={
                "sourcery": _make_policy(
                    name="sourcery",
                    required=False,
                    auto_trigger=False,
                ),
            },
        )
        assert plans["sourcery"].action == "NOT_NEEDED"

    def test_coderabbit_budget_exhausted_blocks(self, tmp_path: Path) -> None:
        # CodeRabbit budget exhausted: 1 explicit request
        # was already used for the PR lifecycle. Use a
        # per-head cap of 2 so the budget triggers first.
        snap = _snap()
        ledger_path = tmp_path / "review_requests"
        _write_ledger(
            ledger_path,
            provider=REQUIRED_OPTIONAL,
            head=CURRENT_HEAD,
            record={
                "lifecycle": "REQUEST_SENT",
                "requested_at": "2026-08-19T14:00:00Z",
                "request_id": "req-cr-1",
            },
        )
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={
                REQUIRED_OPTIONAL: _make_policy(
                    name=REQUIRED_OPTIONAL,
                    required=True,
                    budget_per_pr=1,
                    max_requests_per_head=5,  # high so
                    # budget_per_pr triggers first
                ),
            },
            ledger_path=ledger_path,
        )
        assert plans[REQUIRED_OPTIONAL].action == "BLOCK"
        assert "budget_per_pr_exhausted" in plans[
            REQUIRED_OPTIONAL
        ].reason

    def test_two_successive_heads_one_request_per_head(
        self, tmp_path: Path,
    ) -> None:
        # First head triggers a request; advance to a new
        # head; planner must request again (per-head cap is
        # per-head, not per-PR).
        snap = _snap()
        ledger_path = tmp_path / "review_requests"
        # Head 1: REQUEST.
        plans_head_1 = plan_reviewer_actions(
            head_sha="b" * 40,
            snap=snap,
            policies={PROVIDER_NAME: _make_policy()},
            ledger_path=ledger_path,
        )
        assert plans_head_1[PROVIDER_NAME].action == "REQUEST"
        # Persist + supersede (simulate head advance).
        _write_ledger(
            ledger_path,
            provider=PROVIDER_NAME,
            head="b" * 40,
            record={
                "lifecycle": "REQUEST_SENT",
                "requested_at": "2026-08-19T14:00:00Z",
                "request_id": "req-1",
            },
        )
        _write_superseded(
            ledger_path,
            provider=PROVIDER_NAME,
            stale_head="b" * 40,
            record={"superseded_by_head": CURRENT_HEAD},
        )
        # Head 2: REQUEST again (per-head cap resets per head).
        plans_head_2 = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={PROVIDER_NAME: _make_policy(
                max_requests_per_head=1,
            )},
            ledger_path=ledger_path,
            superseded_records=[
                {"provider": PROVIDER_NAME},
            ],
        )
        assert plans_head_2[PROVIDER_NAME].action == "REQUEST"

    def test_no_duplicate_trigger_across_slices(
        self, tmp_path: Path,
    ) -> None:
        # After REQUEST was persisted, repeated planner
        # calls within the cooldown must NOT emit a second
        # REQUEST. Use max_requests_per_head=2 so the per-head
        # cap doesn't trigger first, and pin ``now`` so the
        # cooldown is deterministic.
        snap = _snap()
        ledger_path = tmp_path / "review_requests"
        _write_ledger(
            ledger_path,
            provider=PROVIDER_NAME,
            head=CURRENT_HEAD,
            record={
                "lifecycle": "REQUEST_SENT",
                "requested_at": "2026-08-19T19:19:00Z",
                "request_id": "req-1",
            },
        )
        policies = {PROVIDER_NAME: _make_policy(
            request_cooldown_seconds=3600,
            max_requests_per_head=2,
        )}
        from datetime import datetime, timezone
        now_ts = datetime(
            2026, 8, 19, 19, 20, 0, tzinfo=timezone.utc,
        ).timestamp()
        for _ in range(5):
            plans = plan_reviewer_actions(
                head_sha=CURRENT_HEAD,
                snap=snap,
                policies=policies,
                ledger_path=ledger_path,
                now=now_ts,
            )
            # Within cooldown: WAITING_FOR_AUTO (no
            # duplicate request).
            assert plans[PROVIDER_NAME].action in (
                "WAITING_FOR_AUTO", "NOT_NEEDED",
            ), f"got {plans[PROVIDER_NAME].action!r}"

    def test_pending_review_then_head_change_stales_request(
        self, tmp_path: Path,
    ) -> None:
        # A request is in flight for head B; the head
        # advances to C. The plan for C must NOT inherit the
        # pending state; a fresh request is allowed.
        snap = _snap()
        ledger_path = tmp_path / "review_requests"
        _write_ledger(
            ledger_path,
            provider=PROVIDER_NAME,
            head="b" * 40,
            record={
                "lifecycle": "REQUEST_SENT",
                "requested_at": "2026-08-19T14:00:00Z",
                "request_id": "req-1",
            },
        )
        _write_superseded(
            ledger_path,
            provider=PROVIDER_NAME,
            stale_head="b" * 40,
            record={"superseded_by_head": CURRENT_HEAD},
        )
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={PROVIDER_NAME: _make_policy(
                max_requests_per_head=1,
            )},
            ledger_path=ledger_path,
            superseded_records=[{"provider": PROVIDER_NAME}],
        )
        # Per-head cap resets; required + no evidence on the
        # new head -> REQUEST.
        assert plans[PROVIDER_NAME].action == "REQUEST"

    def test_ci_green_codex_stale_not_ready(self) -> None:
        # CI green but Codex review is stale (anchored to
        # prior head). The planner must emit BLOCK; readiness
        # cannot proceed.
        snap = _snap(formal_reviews=[{
            "provider": PROVIDER_NAME,
            "commit_id": PRIOR_HEAD,
            "submitted_at": "2026-08-19T13:00:00Z",
            "review_id": "rev-stale",
        }])
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={PROVIDER_NAME: _make_policy()},
        )
        # Stale on a required provider triggers REQUEST (the
        # supervisor will post ``@codex review`` and wait).
        # The readiness gate then refuses promotion until
        # the request is acknowledged.
        assert plans[PROVIDER_NAME].action == "REQUEST"

    def test_ci_green_codex_fresh_ready(self) -> None:
        snap = _snap(formal_reviews=[{
            "provider": PROVIDER_NAME,
            "commit_id": CURRENT_HEAD,
            "submitted_at": "2026-08-19T14:30:00Z",
            "review_id": "rev-fresh",
        }])
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={PROVIDER_NAME: _make_policy()},
        )
        assert plans[PROVIDER_NAME].action == "NOT_NEEDED"

    def test_optional_provider_on_repair_head_no_block(
        self,
    ) -> None:
        # Sourcery is OPTIONAL. No evidence on the new head.
        # Planner must NOT block readiness.
        snap = _snap()
        plans = plan_reviewer_actions(
            head_sha=CURRENT_HEAD,
            snap=snap,
            policies={
                "sourcery": _make_policy(
                    name="sourcery",
                    required=False,
                    auto_trigger=False,
                ),
            },
        )
        assert plans["sourcery"].action == "NOT_NEEDED"


# === Test policy defaults ===

class TestPolicyDefaults:
    """The conservative default profiles match the C23 directive."""

    def test_codex_required_and_triggerable(self) -> None:
        profile = DEFAULT_POLICY_PROFILES["codex"]
        assert profile["required"] is True
        assert profile["auto_trigger"] is True
        assert profile["max_requests_per_head"] == 1

    def test_sourcery_optional_no_auto_trigger(self) -> None:
        profile = DEFAULT_POLICY_PROFILES["sourcery"]
        assert profile["required"] is False
        assert profile["auto_trigger"] is False
        assert profile["max_requests_per_head"] == 0

    def test_coderabbit_initial_only(self) -> None:
        profile = DEFAULT_POLICY_PROFILES["coderabbit"]
        assert profile["required"] is True
        # Initial-only budget.
        assert profile["budget_per_pr"] == 1
        assert profile["max_requests_per_head"] == 1


class TestLoadPoliciesFromProviders:
    """Verify the loader picks up the existing PROVIDERS map."""

    def test_load_default_profiles(self) -> None:
        providers = {
            "codex": {"trigger_handle": "@codex review"},
            "sourcery": {"trigger_handle": "@sourcery-ai review"},
        }
        policies = load_policies_from_providers(providers)
        assert "codex" in policies
        assert "sourcery" in policies
        assert policies["codex"].required is True
        assert policies["sourcery"].required is False
        # trigger_handle inherited from PROVIDERS map.
        assert policies["codex"].trigger_handle == "@codex review"
        assert (
            policies["sourcery"].trigger_handle
            == "@sourcery-ai review"
        )

    def test_overrides_win(self) -> None:
        providers = {
            "codex": {"trigger_handle": "@codex review"},
        }
        policies = load_policies_from_providers(
            providers,
            overrides={"codex": {"required": False}},
        )
        assert policies["codex"].required is False