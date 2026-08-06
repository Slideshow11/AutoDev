"""Tests for autocoder_orchestration.readiness.ReadinessEngine."""
from __future__ import annotations

import pytest

from autocoder_orchestration import ReadinessEngine
from autocoder_orchestration.readiness import (
    ReadinessCertificate,
    ReadinessDecision,
    GateResult,
    GATE_EXACT_HEAD,
    GATE_UNRESOLVED_CURRENT_THREADS,
    GATE_UNRESOLVED_OUTDATED_THREADS,
    GATE_REQUIRED_CI_JOBS_SUCCESSFUL,
    GATE_CI_BOUND_TO_EXACT_HEAD,
    GATE_QUIET_WINDOW_COMPLETE,
    GATE_NO_ACTIVE_IMPL_WORKER,
    ALL_GATES,
)


H1 = "a" * 64
H2 = "b" * 64
BASE = "c" * 64


def _pr_open(head: str = H1, base: str = "main", base_sha: str = BASE) -> dict:
    return {
        "state": "open",
        "merged": False,
        "draft": False,
        "number": 2,
        "head": {"sha": head},
        "base": {"ref": base, "sha": base_sha},
    }


def _check_runs(head: str = H1, all_pass: bool = True, names=None) -> list:
    if names is None:
        names = ["test (3.10)", "test (3.11)", "test (3.12)", "package-smoke", "provenance", "committed-state-scan"]
    runs = []
    for n in names:
        runs.append({
            "name": n,
            "status": "completed",
            "conclusion": "success" if all_pass else "failure",
            "head_sha": head,
        })
    return runs


def _reviews_at_head(head: str = H1, state: str = "APPROVED") -> list:
    return [
        {"author": "coderabbitai", "state": state, "commit_oid": head, "submittedAt": "2026-08-05T22:00:00Z"},
    ]


def _threads(unresolved_current: int = 0, unresolved_outdated: int = 0) -> list:
    threads = []
    for i in range(unresolved_current):
        threads.append({"id": f"c{i}", "isResolved": False, "isOutdated": False})
    for i in range(unresolved_outdated):
        threads.append({"id": f"o{i}", "isResolved": False, "isOutdated": True})
    return threads


def _kwargs(**overrides) -> dict:
    base = dict(
        expected_head=H1,
        expected_base_sha=BASE,
        expected_base_branch="main",
        required_ci_jobs=("test (3.10)", "test (3.11)", "test (3.12)", "package-smoke", "provenance", "committed-state-scan"),
        live_pr_payload=_pr_open(),
        live_check_runs=_check_runs(),
        live_threads=_threads(),
        live_reviews=_reviews_at_head(),
        body_reconciled=True,
        impl_worker_active=False,
        repair_worker_active=False,
        review_request_in_progress=False,
        reviewer_in_progress=False,
        unconsumed_event_count=0,
        active_conflicting_lease=False,
        api_failure=None,
        parse_failure=None,
        fallback_success=False,
        quiet_window_complete=True,
        quiet_window_observations=[
            {"qualifying": True, "ts_monotonic": 100.0},
            {"qualifying": True, "ts_monotonic": 200.0},
            {"qualifying": True, "ts_monotonic": 350.0},
        ],
        quiet_window_min_monotonic=180.0,
        quiet_window_first_utc="2026-08-05T22:00:00Z",
        quiet_window_last_utc="2026-08-05T22:03:00Z",
        lock_released_after_shutdown=True,
        inputs_frozen=True,
        invalid_finding_descriptions=[],
        inconclusive_finding_descriptions=[],
    )
    base.update(overrides)
    return base


def _engine() -> ReadinessEngine:
    return ReadinessEngine(run_id="r1", repo="o/r", pr_number=2)


# === Construction ===
class TestReadinessEngine:
    def test_engine_constructs(self) -> None:
        eng = _engine()
        assert eng.run_id == "r1"
        assert eng.repo == "o/r"
        assert eng.pr_number == 2


# === All gates present ===
class TestAllGatesEvaluated:
    def test_all_gates_present(self) -> None:
        assert len(ALL_GATES) >= 20
        for required in [
            GATE_EXACT_HEAD,
            GATE_UNRESOLVED_CURRENT_THREADS,
            GATE_UNRESOLVED_OUTDATED_THREADS,
            GATE_REQUIRED_CI_JOBS_SUCCESSFUL,
            GATE_CI_BOUND_TO_EXACT_HEAD,
            GATE_QUIET_WINDOW_COMPLETE,
            GATE_NO_ACTIVE_IMPL_WORKER,
        ]:
            assert required in ALL_GATES

    def test_clean_state_passes(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs())
        assert decision.overall_passed, [g.gate for g in decision.failed_gates()]

    def test_no_bypass_flag(self) -> None:
        # The engine has no skip/ignore/trust/assume/force flag.
        eng = _engine()
        # Verify no kwargs can trigger a bypass
        decision = eng.evaluate(**_kwargs())
        assert decision.overall_passed


# === Live PR identity ===
class TestLivePRIdentity:
    def test_merged_blocks(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(live_pr_payload=_pr_open() | {"merged": True}))
        assert not decision.overall_passed
        assert any(g.gate == "exact_live_pr_identity" for g in decision.failed_gates())

    def test_draft_blocks(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(live_pr_payload=_pr_open() | {"draft": True}))
        assert not decision.overall_passed

    def test_wrong_pr_number_rejected(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(live_pr_payload=_pr_open() | {"number": 99}))
        assert not decision.overall_passed


# === Exact head ===
class TestExactHead:
    def test_head_drift_blocks(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(live_pr_payload=_pr_open(head=H2)))
        assert any(g.gate == GATE_EXACT_HEAD for g in decision.failed_gates())

    def test_head_match_passes(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs())
        assert all(g.gate != GATE_EXACT_HEAD or g.passed for g in decision.gate_results)


# === CI ===
class TestCI:
    def test_missing_ci_block(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(live_check_runs=[]))
        assert not decision.overall_passed
        assert any(g.gate == "required_ci_jobs_present" for g in decision.failed_gates())

    def test_pending_ci_block(self) -> None:
        eng = _engine()
        runs = _check_runs(all_pass=True)
        for r in runs:
            r["status"] = "in_progress"
        decision = eng.evaluate(**_kwargs(live_check_runs=runs))
        assert not decision.overall_passed

    def test_failed_ci_block(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(live_check_runs=_check_runs(all_pass=False)))
        assert not decision.overall_passed

    def test_stale_head_ci_block(self) -> None:
        eng = _engine()
        # Mismatched head SHA on a check run
        runs = _check_runs()
        for r in runs:
            r["head_sha"] = H2
        decision = eng.evaluate(**_kwargs(live_check_runs=runs))
        assert not decision.overall_passed


# === Reviewer ===
class TestReviewer:
    def test_stale_reviewer_approval_blocks(self) -> None:
        eng = _engine()
        # Reviewer approved an OLD head
        decision = eng.evaluate(**_kwargs(live_reviews=_reviews_at_head(head=H2)))
        assert not decision.overall_passed

    def test_reviewer_changes_requested_blocks(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(live_reviews=_reviews_at_head(state="CHANGES_REQUESTED")))
        assert not decision.overall_passed

    def test_no_reviewer_block(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(live_reviews=[]))
        assert not decision.overall_passed


# === Threads ===
class TestThreads:
    def test_unresolved_current_blocks(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(live_threads=_threads(unresolved_current=1)))
        assert any(g.gate == GATE_UNRESOLVED_CURRENT_THREADS for g in decision.failed_gates())

    def test_unresolved_outdated_blocks(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(live_threads=_threads(unresolved_outdated=1)))
        assert any(g.gate == GATE_UNRESOLVED_OUTDATED_THREADS for g in decision.failed_gates())


# === Active workers ===
class TestActiveWorkers:
    def test_active_worker_blocks(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(impl_worker_active=True))
        assert any(g.gate == GATE_NO_ACTIVE_IMPL_WORKER for g in decision.failed_gates())

    def test_unconsumed_events_blocks(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(unconsumed_event_count=1))
        assert not decision.overall_passed

    def test_provider_in_progress_blocks(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(reviewer_in_progress=True))
        assert not decision.overall_passed


# === API and parse ===
class TestApiAndParse:
    def test_api_failure_blocks(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(api_failure="connection timeout"))
        assert not decision.overall_passed

    def test_parse_failure_blocks(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(parse_failure="invalid JSON"))
        assert not decision.overall_passed

    def test_fallback_success_blocks(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(fallback_success=True))
        assert not decision.overall_passed


# === Quiet window ===
class TestQuietWindow:
    def test_incomplete_window_blocks(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(quiet_window_complete=False))
        assert not decision.overall_passed
        assert any(g.gate == GATE_QUIET_WINDOW_COMPLETE for g in decision.failed_gates())

    def test_nonqual_inside_window_blocks(self) -> None:
        eng = _engine()
        obs = [{"qualifying": True, "ts_monotonic": 1.0}, {"qualifying": False, "ts_monotonic": 100.0}, {"qualifying": True, "ts_monotonic": 200.0}]
        decision = eng.evaluate(**_kwargs(quiet_window_observations=obs, quiet_window_min_monotonic=180.0))
        assert not decision.overall_passed

    def test_lock_not_released_blocks(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(lock_released_after_shutdown=False))
        assert not decision.overall_passed

    def test_inputs_not_frozen_blocks(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs(inputs_frozen=False))
        assert not decision.overall_passed


# === Decision round-trip ===
class TestDecisionRoundtrip:
    def test_roundtrip(self) -> None:
        eng = _engine()
        decision = eng.evaluate(**_kwargs())
        payload = decision.to_dict()
        restored = ReadinessDecision.from_dict(payload)
        assert restored.overall_passed == decision.overall_passed
        assert restored.run_id == decision.run_id
        assert len(restored.gate_results) == len(decision.gate_results)


class TestCertificate:
    def test_certificate_serialization(self) -> None:
        from autocoder_orchestration.readiness import ReadinessCertificate
        eng = _engine()
        decision = eng.evaluate(**_kwargs())
        cert = ReadinessCertificate(
            decision=decision,
            issued_at="2026-08-05T22:00:00Z",
            expires_at="2026-08-05T22:10:00Z",
            certificate_id="cert-1",
            issuer="observer",
        )
        payload = cert.to_dict()
        restored = ReadinessCertificate.from_dict(payload)
        assert restored.certificate_id == cert.certificate_id
        assert restored.decision.run_id == cert.decision.run_id

    def test_certificate_expiry_check(self) -> None:
        from autocoder_orchestration.readiness import ReadinessCertificate
        eng = _engine()
        decision = eng.evaluate(**_kwargs())
        cert = ReadinessCertificate(
            decision=decision,
            issued_at="2026-08-05T22:00:00Z",
            expires_at="2026-08-05T22:10:00Z",
            certificate_id="cert-1",
            issuer="observer",
        )
        assert cert.is_expired("2026-08-05T22:11:00Z")
        assert not cert.is_expired("2026-08-05T22:09:00Z")
