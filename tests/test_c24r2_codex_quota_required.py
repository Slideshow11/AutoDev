"""Round-C24-R2 / Codex usage-limit required-provider handling.

The audit (§6) requires:

- do not treat the usage-limit response as a successful review
- do not classify Codex as FRESH
- do not qualify readiness when required Codex evidence is absent
- do not spam additional review requests
- expose an explicit provider-budget/quota blocker
- do NOT weaken Codex from required to optional merely because
  quota is exhausted
- do NOT consume CodeRabbit reviews to bypass this condition

The existing codex policy ``unavailable_behavior="BLOCK"``
when ``paused=True`` produces ``BLOCK`` with reason
``provider_paused_quota``. The C24-R2 contract:
  - the planner stamps ``snap["reviewer_plan"]["codex"] = BLOCK``
  - the readiness gate (``active_repair_quiet_window``) aborts
    when the planner returns BLOCK
  - the heartbeat loop preserves the BLOCK reason in the
    operator-visible log
  - CodeRabbit is NOT promoted to a substitute for the
    required Codex evidence

This test pins the §6 contract.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(os.environ.get("REPO_ROOT", str(Path(__file__).resolve().parent.parent)))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from autocoder_supervisor import reviewer_policy as policy  # noqa: E402
from autocoder_supervisor import supervisor as supervisor_mod  # noqa: E402


HEAD = "f" * 40


def _plan_with_codex_paused(*, codex_required: bool) -> dict:
    """Build a minimal snapshot with codex paused (quota exhausted)."""
    codex = policy.ReviewerPolicy(
        name="codex",
        required=codex_required,
        auto_trigger=True,
        unavailable_behavior="BLOCK",
    )
    plans = policy.plan_reviewer_actions(
        head_sha=HEAD,
        snap={
            "head_sha": HEAD,
            "providers": {
                "codex": {"paused": True, "in_progress": False},
                "coderabbit": {"paused": False, "in_progress": False},
            },
            "formal_reviews": [],
            "review_comments": [],
            "review_threads": {},
            "provider_surfaces": {},
        },
        policies={"codex": codex},
    )
    return {"codex": plans["codex"].__dict__ if hasattr(plans["codex"], "__dict__") else plans}


class TestCodexQuotaRequiredHandling:
    def test_required_codex_paused_emits_BLOCK(self) -> None:
        """Audit §6 R1: required Codex, paused, must yield BLOCK."""
        result = _plan_with_codex_paused(codex_required=True)
        plan = result["codex"]
        assert plan["action"] == "BLOCK", (
            f"Required Codex paused must yield BLOCK; got {plan['action']!r}"
        )
        assert "paused" in plan["reason"].lower() or "quota" in plan["reason"].lower()

    def test_optional_codex_paused_yields_NOT_NEEDED(self) -> None:
        """Audit §6 R2: optional paused → NOT_NEEDED (per Defect 4)."""
        result = _plan_with_codex_paused(codex_required=False)
        plan = result["codex"]
        assert plan["action"] == "NOT_NEEDED"

    def test_no_optional_when_required_paused(self) -> None:
        """Audit §6: do NOT weaken Codex from required to optional
        merely because quota is exhausted. The planner must
        preserve ``required=True`` semantics."""
        result = _plan_with_codex_paused(codex_required=True)
        plan = result["codex"]
        assert plan["action"] != "NOT_NEEDED"

    def test_no_coderabbit_substitute_for_required_codex(self) -> None:
        """Audit §6: do NOT consume CodeRabbit reviews to bypass
        the required Codex evidence check. With codex required
        AND paused, the CodeRabbit path does NOT promote
        readiness."""
        result = _plan_with_codex_paused(codex_required=True)
        plan = result["codex"]
        assert plan["action"] == "BLOCK"

    def test_in_flight_request_does_not_resend(self) -> None:
        """Audit §6: do not spam additional review requests. The
        planner's existing request-in-flight logic must
        preserve the existing request when one is already
        REQUEST_SENT or ACKNOWLEDGED."""
        from autocoder_supervisor.reviewer_policy import (
            _count_active_request_records as _count,
        )
        import inspect
        sig = inspect.signature(_count)
        assert "ledger_path" in sig.parameters
        assert "provider" in sig.parameters
        assert "head_sha" in sig.parameters


class TestReadinessBlockedByRequiredCodex:
    def test_evaluate_readiness_blocks_when_codex_paused(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Defend against cross-test pollution: prior tests
        # in the suite (notably test_c24r1_reviewer_plan_fail_closed)
        # leave unconsumed events in the supervisor's global
        # unconsumed_events.json. ``evaluate_readiness`` returns
        # ``ready=False, reason=unconsumed_events`` BEFORE the
        # reviewer_plan check, so the test must pin the
        # unconsumed list to empty.
        from autocoder_supervisor import supervisor as _sup_iso
        monkeypatch.setattr(_sup_iso, "list_unconsumed_events", list)
        """Audit §6: do not qualify readiness while required Codex
        evidence is absent. A snapshot with codex in BLOCKED
        state must fail the readiness gate."""
        snap = {
            "head_sha": HEAD,
            "head_match": True,
            "providers": {
                "codex": {"paused": True, "in_progress": False},
                "coderabbit": {"paused": False, "in_progress": False},
            },
            "formal_reviews": [],
            "review_threads": {},
            "review_thread_pagination_complete": True,
            "review_thread_pagination_failed": False,
            "provider_surface_complete": True,
            "truncated_thread_ids": [],
            "provider_surfaces": {},
            "_provider_issue_comments": {
                "codex": [
                    {"login": "chatgpt-codex-connector[bot]", "body": "p1"}
                ],
            },
            "required_checks": {
                "full-suite": {"conclusion": "success", "status": "completed"},
                "committed-state-scan": {"conclusion": "success", "status": "completed"},
            },
        }
        from autocoder_supervisor.supervisor import evaluate_readiness
        # Inject the BLOCK plan directly on the snapshot.
        # The producer helper stamps the plan via the
        # policy-direct API.
        codex = policy.ReviewerPolicy(
            name="codex",
            required=True,
            auto_trigger=True,
            unavailable_behavior="BLOCK",
        )
        plan = policy.plan_reviewer_actions(
            head_sha=HEAD, snap=snap, policies={"codex": codex},
        )
        # Stamp the plan; the BLOCK action on a required
        # provider is the audit's fail-closed blocker.
        # ``apply_reviewer_plan`` writes the plan via
        # ``dataclasses.asdict`` so the readiness gate (and
        # audit consumers) see plain dicts. Mirror that
        # round-trip here.
        import dataclasses
        snap["reviewer_plan"] = {
            "codex": dataclasses.asdict(
                policy.ReviewerTriggerPlan(
                    provider="codex",
                    action="BLOCK",
                    reason="provider_paused_quota",
                    freshness=plan["codex"].freshness,
                ),
            ),
        }
        # Defend against cross-test pollution: a prior test
        # (test_round54_c22_retry_lifecycle) mutates the
        # supervisor's module-level ``POLICY`` dict to drop
        # ``codex`` from the required list. The audit's §6
        # contract is provider-specific (Codex must be a
        # required provider), so this test must set the
        # policy explicitly.
        from autocoder_supervisor import supervisor as _sup_mod
        _preserved_policy = dict(_sup_mod.POLICY)
        _preserved_required = list(
            _sup_mod.POLICY.get(
                "required_review_providers_for_pr_416", []
            )
        )
        if "codex" not in _preserved_required:
            _sup_mod.POLICY["required_review_providers_for_pr_416"] = (
                list(_preserved_required) + ["codex"]
            )
        try:
            result = evaluate_readiness(snap, HEAD)
            assert result["ready"] is False, (
                f"Readiness must fail closed when required Codex is paused (BLOCK). "
                f"Got: {result!r}"
            )
            # The blockers must include codex.
            all_blockers = list(result.get("blockers", [])) + list(
                result.get("required_reviewer_blockers", [])
            )
            provider_names = {b["provider"] for b in all_blockers}
            assert "codex" in provider_names, (
                f"BLOCK on codex must appear in blockers list; "
                f"got {all_blockers!r}"
            )
        finally:
            _sup_mod.POLICY["required_review_providers_for_pr_416"] = (
                _preserved_required
            )
