"""Round-C23 integration tests for the readiness gate
``apply_reviewer_plan`` + ``_evaluate_c23_required_blockers`` +
``evaluate_readiness`` chain.

These tests verify the supervisor-level wiring (not just the
``reviewer_policy`` planner in isolation):
1. ``apply_reviewer_plan`` stamps ``snap["reviewer_plan"]`` on
   the snapshot for the canonical reviewer gate.
2. ``evaluate_readiness`` returns ``required_reviewer_pending``
   when a required reviewer is missing on the current head.
3. The dispatcher function is invoked for ``REQUEST`` actions
   and the dispatch outcome is recorded on the plan entry.
4. The dispatcher is NOT invoked for ``NOT_NEEDED`` /
   ``WAITING_FOR_AUTO`` / ``BLOCK`` actions.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_REPO_ROOT = Path(os.environ.get("REPO_ROOT", str(Path(__file__).resolve().parent.parent)))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Test environment must configure the supervisor's PROVIDERS
# map to include coderabbit (required), codex (required +
# auto-trigger), and sourcery (optional, no auto-trigger).
# The default config only carries coderabbit + codex; we
# override the optional list to include sourcery so the
# dispatcher-invocation test can exercise the negative path.
os.environ.setdefault(
    "AED_REQUIRED_REVIEW_PROVIDERS", "coderabbit,codex"
)
os.environ.setdefault(
    "AED_OPTIONAL_REVIEW_PROVIDERS", "sourcery"
)


from autocoder_supervisor.supervisor import (  # noqa: E402
    _evaluate_c23_required_blockers,
    apply_reviewer_plan,
    evaluate_readiness,
)


# Re-apply the supervisor config so the PROVIDERS map
# includes the three providers we need to exercise the
# dispatcher-invocation contract (codex: required +
# auto-trigger; coderabbit: required + initial-only budget;
# sourcery: optional + no auto-trigger). The default
# bootstrap only registers coderabbit + codex; we
# mutate the module-level PROVIDERS dict and POLICY in
# place (the ``_apply_config`` path doesn't re-build
# either map, by design — it only refreshes runtime
# paths and cadence settings).
#
# IMPORTANT: the bootstrap mutates supervisor-globals; we
# snapshot the original values so the autouse fixture can
# restore them after the test module finishes. Without
# this restore, downstream tests that assume the
# pre-C23 bootstrap state (``required_check_names=[]``,
# codex present but optional, no sourcery entry) see a
# contaminated supervisor and fail spuriously.
_ORIG_PROVIDERS = None
_ORIG_POLICY = None


def _bootstrap_supervisor_with_three_providers() -> None:
    global _ORIG_PROVIDERS, _ORIG_POLICY
    import autocoder_supervisor.supervisor as _sup
    if "sourcery" in _sup.PROVIDERS:
        return
    # Snapshot the originals BEFORE mutating so the
    # module-level teardown can restore them.
    _ORIG_PROVIDERS = dict(_sup.PROVIDERS)
    _ORIG_POLICY = dict(_sup.POLICY)
    # Inject a sourcery provider definition mirroring the
    # C23 directive: optional, auto-runs on pushes (so the
    # supervisor waits for it), no manual trigger.
    from autocoder_supervisor.supervisor import _default_providers
    # Build a sourcery-only provider dict via the same
    # factory the bootstrap uses, then merge it into the
    # existing PROVIDERS map.
    from autocoder_supervisor.config import SupervisorConfig
    cfg = SupervisorConfig.from_dict({
        "schema_version": "aed.autocoder_supervisor.v1",
        "instance_id": "c23-test",
        "state_dir": "/tmp/c23-test-state",
        "working_checkout": "/tmp",
        "log_path": "/tmp/c23-test.log",
        "heartbeat_path": "/tmp/c23-test.hb",
        "lock_path": "/tmp/c23-test.lock",
        "worker_command": ["true"],
        "worker_session_id": "c23-test",
        "worker_session_name": "c23-test",
        "cooldown_seconds": 900,
        "resume_prompt_template": "x",
        "human_boundary": "merge_only",
        "required_review_providers": ["coderabbit", "codex"],
        "optional_review_providers": ["sourcery"],
        "provider_states_are_independent": True,
        "post_codex_recovery_request": False,
        "heartbeat_seconds": 120,
        "quiet_window_seconds": 180,
        "quota_retry_initial_seconds": 3600,
        "quota_retry_backoff_seconds": 21600,
        "quota_backoff_after_retry_count": 2,
    }, reject_user_paths=False)
    sourcery_only = _default_providers(cfg)
    for name, cfg_entry in sourcery_only.items():
        if name not in _sup.PROVIDERS:
            _sup.PROVIDERS[name] = cfg_entry
    # Required check names so the CI gate does not silently
    # fabricate ``CHECKS_GREEN`` semantics.
    _sup.POLICY["required_check_names"] = ["test (3.10)"]


def _restore_supervisor_globals() -> None:
    """Restore the supervisor module-level PROVIDERS and
    POLICY to the pre-bootstrap state. The autouse fixture
    calls this after every test in this module so
    downstream tests see the canonical pre-C23 state."""
    import autocoder_supervisor.supervisor as _sup
    if _ORIG_PROVIDERS is not None:
        _sup.PROVIDERS.clear()
        _sup.PROVIDERS.update(_ORIG_PROVIDERS)
    if _ORIG_POLICY is not None:
        _sup.POLICY.clear()
        _sup.POLICY.update(_ORIG_POLICY)


@pytest.fixture(autouse=True)
def _bootstrap_supervisor_for_each_test() -> None:
    """Bootstrap the supervisor globals for every test in
    this module. The bootstrap is idempotent (it returns
    early when sourcery is already in PROVIDERS), so each
    test re-installs the C23 override before running. A
    teardown step restores the original state to prevent
    contamination of downstream tests."""
    _bootstrap_supervisor_with_three_providers()
    yield
    _restore_supervisor_globals()


CURRENT_HEAD = "a" * 40
OTHER_HEAD = "b" * 40
PRIOR_HEAD = "f" * 40


def _snap(
    *,
    head_sha: str = CURRENT_HEAD,
    formal_reviews: list | None = None,
    providers: dict | None = None,
    issue_comments: list | None = None,
    required_check_names: list | None = None,
) -> dict:
    if required_check_names is None:
        required_check_names = ["test (3.10)"]
    return {
        "head_sha": head_sha,
        "head_match": True,
        "mergeable": True,
        "formal_reviews": formal_reviews or [],
        "issue_comments": issue_comments or [],
        "_provider_issue_comments": {},
        "providers": providers or {},
        "review_threads": {},
        "review_threads_pagination_failed": False,
        "review_threads_pagination_complete": True,
        "review_thread_pagination_failed": False,
        "review_thread_pagination_complete": True,
        "truncated_thread_ids": [],
        "required_checks": {
            name: {
                "conclusion": "success",
                "status": "completed",
            }
            for name in required_check_names
        },
        "operator_logins": [],
        "operator_identity_sources": {},
        "required_check_names": list(required_check_names),
    }


class TestApplyReviewerPlanStampsSnap:
    """``apply_reviewer_plan`` stamps ``snap["reviewer_plan"]``."""

    def test_stamps_plan_with_fresh_provider(self, tmp_path: Path) -> None:
        snap = _snap(
            formal_reviews=[
                {
                    "provider": "codex",
                    "commit_id": CURRENT_HEAD,
                    "submitted_at": "2026-08-19T14:30:00Z",
                    "review_id": "rev-codex",
                },
                {
                    "provider": "coderabbit",
                    "commit_id": CURRENT_HEAD,
                    "submitted_at": "2026-08-19T14:30:00Z",
                    "review_id": "rev-cr",
                },
            ],
        )
        ledger = tmp_path / "review_requests"
        plan = apply_reviewer_plan(
            snap, head_sha=CURRENT_HEAD, ledger_path=ledger,
        )
        assert plan["codex"]["action"] == "NOT_NEEDED"
        assert plan["coderabbit"]["action"] == "NOT_NEEDED"
        assert snap["reviewer_plan"]["codex"]["action"] == "NOT_NEEDED"

    def test_requests_when_no_evidence(
        self, tmp_path: Path,
    ) -> None:
        snap = _snap()
        ledger = tmp_path / "review_requests"
        plan = apply_reviewer_plan(
            snap, head_sha=CURRENT_HEAD, ledger_path=ledger,
        )
        # Both required providers (codex, coderabbit) have
        # no evidence -> both REQUEST.
        assert plan["codex"]["action"] == "REQUEST"
        assert plan["coderabbit"]["action"] == "REQUEST"
        # Sourcery is OPTIONAL + no auto-trigger ->
        # NOT_NEEDED.
        assert plan["sourcery"]["action"] == "NOT_NEEDED"


class TestDispatcherInvocation:
    """The dispatcher is invoked for REQUEST actions only."""

    def test_dispatch_invoked_only_for_request(self, tmp_path: Path) -> None:
        snap = _snap(
            formal_reviews=[
                {
                    "provider": "codex",
                    "commit_id": CURRENT_HEAD,
                    "submitted_at": "2026-08-19T14:30:00Z",
                    "review_id": "rev-codex",
                },
                {
                    "provider": "coderabbit",
                    "commit_id": CURRENT_HEAD,
                    "submitted_at": "2026-08-19T14:30:00Z",
                    "review_id": "rev-cr",
                },
            ],
        )
        ledger = tmp_path / "review_requests"
        dispatcher = MagicMock(return_value=True)
        plan = apply_reviewer_plan(
            snap,
            head_sha=CURRENT_HEAD,
            ledger_path=ledger,
            post_review_request_fn=dispatcher,
        )
        # All required providers fresh; sourcery optional +
        # no auto-trigger -> NOT_NEEDED.
        for provider in ("codex", "coderabbit", "sourcery"):
            assert plan[provider]["action"] == "NOT_NEEDED", (
                f"{provider}: {plan[provider].action!r}"
            )
        # Dispatcher not called.
        dispatcher.assert_not_called()

    def test_dispatch_outcome_recorded(self, tmp_path: Path) -> None:
        snap = _snap()
        ledger = tmp_path / "review_requests"
        dispatcher = MagicMock(return_value=False)
        plan = apply_reviewer_plan(
            snap,
            head_sha=CURRENT_HEAD,
            ledger_path=ledger,
            post_review_request_fn=dispatcher,
        )
        # Both required providers REQUEST; both
        # ``dispatched`` False (mock returned False).
        assert plan["codex"]["dispatched"] is False
        assert plan["coderabbit"]["dispatched"] is False

    def test_dispatcher_invoked_for_required_missing(
        self, tmp_path: Path,
    ) -> None:
        snap = _snap()  # no reviews
        ledger = tmp_path / "review_requests"
        dispatcher = MagicMock(return_value=True)
        apply_reviewer_plan(
            snap,
            head_sha=CURRENT_HEAD,
            ledger_path=ledger,
            post_review_request_fn=dispatcher,
        )
        # Codex + coderabbit (both required + no evidence)
        # trigger the dispatcher.
        assert dispatcher.call_count == 2
        called_providers = {
            c.kwargs["provider"]
            for c in dispatcher.call_args_list
        }
        assert called_providers == {"codex", "coderabbit"}


class TestReadinessGateRequiredReviewers:
    """``evaluate_readiness`` consults ``snap["reviewer_plan"]``
    and fails closed when a required reviewer is missing."""

    def test_no_plan_field_is_no_op(self) -> None:
        # Legacy snapshots that do NOT carry a
        # ``reviewer_plan`` field must continue to behave
        # correctly (the readiness gate returns ready
        # because the provider evidence is clean).
        snap = _snap(formal_reviews=[{
            "provider": "codex",
            "commit_id": CURRENT_HEAD,
            "submitted_at": "2026-08-19T14:30:00Z",
            "review_id": "rev-1",
        }])
        result = evaluate_readiness(snap, head=CURRENT_HEAD)
        # Without a plan stamp the gate does not block on
        # reviewer freshness; the existing CI/thread/blocked
        # branches still apply.
        assert result.get("reason") != "required_reviewer_pending"

    def test_required_reviewer_pending_blocks(self) -> None:
        snap = _snap()
        snap["reviewer_plan"] = {
            "codex": {
                "action": "REQUEST",
                "reason": "no_exact_head_evidence_required_provider",
                "dispatched": True,
            },
        }
        result = evaluate_readiness(snap, head=CURRENT_HEAD)
        assert result["ready"] is False
        assert result["reason"] == "required_reviewer_pending"
        blockers = result["required_reviewer_blockers"]
        assert any(
            b["provider"] == "codex" and b["action"] == "REQUEST"
            for b in blockers
        )

    def test_blocked_budget_blocks_readiness(self) -> None:
        snap = _snap()
        snap["reviewer_plan"] = {
            "codex": {
                "action": "BLOCK",
                "reason": "provider_paused_quota",
            },
        }
        result = evaluate_readiness(snap, head=CURRENT_HEAD)
        assert result["ready"] is False
        assert result["reason"] == "required_reviewer_pending"
        blockers = result["required_reviewer_blockers"]
        assert any(
            b["provider"] == "codex" and b["action"] == "BLOCK"
            for b in blockers
        )

    def test_fresh_provider_does_not_block(self) -> None:
        snap = _snap(formal_reviews=[{
            "provider": "codex",
            "commit_id": CURRENT_HEAD,
            "submitted_at": "2026-08-19T14:30:00Z",
            "review_id": "rev-1",
        }])
        snap["reviewer_plan"] = {
            "codex": {
                "action": "NOT_NEEDED",
                "reason": "fresh_exact_head_review",
            },
        }
        result = evaluate_readiness(snap, head=CURRENT_HEAD)
        assert result["ready"] is True

    def test_stale_provider_blocks(self) -> None:
        # The Codex review is anchored to the PRIOR head;
        # the planner marks it STALE -> REQUEST. The
        # readiness gate must fail closed.
        snap = _snap(formal_reviews=[{
            "provider": "codex",
            "commit_id": PRIOR_HEAD,
            "submitted_at": "2026-08-19T13:00:00Z",
            "review_id": "rev-stale",
        }])
        snap["reviewer_plan"] = {
            "codex": {
                "action": "REQUEST",
                "reason": "freshness_stale",
                "dispatched": True,
            },
        }
        result = evaluate_readiness(snap, head=CURRENT_HEAD)
        assert result["ready"] is False
        assert result["reason"] == "required_reviewer_pending"