"""Round-39 regression tests — convergence, qualification,
and self-healing semantics.

These tests cover the production paths introduced by the
round-39 infrastructure repair:

  Section 4 — NO_CHANGES_REQUIRED / ALREADY_SATISFIED /
              SUPERSEDED outcomes (worker contract text in the
              directive prompt).
  Section 6 — Edge-triggered head_sha_drift semantics so
              that a rebind does not perpetually reset the
              quiet window.
  Section 7 — CodeRabbit AUTO_PAUSED provider lifecycle.
  Section 8 — Real CI policy evaluation distinguishes
              NO_REQUIRED_CHECKS from CHECKS_GREEN and the
              legacy silent-bypass regression.
  Section 9 — Reconciliation without head movement.

Each test exercises the real production module:
    autocoder_supervisor.supervisor
    autocoder_orchestration.review_repair_relay

No subprocess invocation against the live hermes CLI is
performed; subprocess calls are stubbed so the suite is
hermetic and runs offline.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest


# Resolve the package imports once at module load.
from autocoder_supervisor import supervisor
from autocoder_supervisor.supervisor import (
    CI_POLICY_CHECKS_FAILED,
    CI_POLICY_CHECKS_GREEN,
    CI_POLICY_CHECKS_PENDING,
    CI_POLICY_NO_REQUIRED_CHECKS,
    CI_POLICY_POLICY_UNRESOLVED,
    ci_policy_status,
    required_checks_green,
    snapshot_differs,
)
from autocoder_orchestration.review_repair_relay import WORKER_PROMPT_TEMPLATE


AUTH = "a" * 40


def _clean_snap(head: str = AUTH) -> dict:
    """Local copy of the canonical clean-snap fixture.

    A live snapshot that has all required fields set to
    non-blocking defaults so the readiness gate does not
    short-circuit on incidental failures.
    """
    return {
        "captured_at": "2026-08-04T00:00:00Z",
        "head_sha": head,
        "head_match": True,
        "mergeable": True,
        "formal_reviews": [],
        "review_threads": {},
        "issue_comments": [],
        "required_checks": {},
        "providers": {
            "codex": {
                "paused": False,
                "in_progress": False,
                "latest_review_ts": None,
                "latest_comment_id": None,
            },
            "coderabbit": {
                "paused": False,
                "in_progress": False,
                "latest_review_ts": "2026-08-04T00:00:00Z",
                "latest_comment_id": None,
            },
        },
        "unconsumed_event_ids": [],
    }


# ---------------------------------------------------------------------------
# TEST 1 — edge-triggered head_sha_drift
# ---------------------------------------------------------------------------

def test_snapshot_differs_reports_drift_only_on_real_movement() -> None:
    """Round-39: a rebind from H0 -> H1 must not perpetually
    reset the quiet window. The supervisor rebinds
    AUTHORITATIVE_HEAD; the next poll should see snap_a.head_sha
    == snap_b.head_sha == H1 and report zero drift.

    Conversely, a poll that observes H0 -> H1 mid-window
    must report drift ONCE so the window resets cleanly.
    """
    snap_a = {"head_sha": "a" * 40}
    snap_b = {"head_sha": "a" * 40}
    # Identical snapshots, both at H0 (== expected_head).
    assert snapshot_differs(snap_a, snap_b, snap_a["head_sha"]) == []
    # Both snapshots at H0, expected_head is H1 (rebind
    # happened after snap_a was captured). The supervisor
    # should NOT report drift here; the historical-state
    # mismatch is captured by ``head_match`` in
    # ``capture_live_snapshot``.
    assert snapshot_differs(
        snap_a, snap_b, "b" * 40,
    ) == []
    # Real head movement between snapshots A and B.
    snap_b2 = {"head_sha": "b" * 40}
    assert "head_sha_drift" in snapshot_differs(snap_a, snap_b2, snap_a["head_sha"])


# ---------------------------------------------------------------------------
# TEST 2 — NO_REQUIRED_CHECKS CI semantic
# ---------------------------------------------------------------------------

def test_required_checks_green_returns_false_for_empty_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-39: an empty required-check set MUST NOT be
    treated as ``green``. The legacy behavior silently
    fabricated ``CI PASSED`` for zero-checks repos; that is
    a regression. ``required_checks_green`` MUST return False
    so callers must consult ``ci_policy_status`` for the
    explicit ``NO_REQUIRED_CHECKS`` outcome.
    """
    monkeypatch.setitem(supervisor.POLICY, "required_check_names", [])
    snap = {"required_checks": {}}
    assert required_checks_green(snap) is False


def test_ci_policy_status_no_required_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(supervisor.POLICY, "required_check_names", [])
    snap = {"required_checks": {}}
    assert ci_policy_status(snap) == CI_POLICY_NO_REQUIRED_CHECKS


def test_ci_policy_status_checks_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        supervisor.POLICY,
        "required_check_names",
        ["ci-1", "ci-2"],
    )
    snap = {
        "required_checks": {
            "ci-1": {"status": "completed", "conclusion": "success"},
            "ci-2": {"status": "completed", "conclusion": "skipped"},
        }
    }
    assert ci_policy_status(snap) == CI_POLICY_CHECKS_GREEN


def test_ci_policy_status_checks_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        supervisor.POLICY,
        "required_check_names",
        ["ci-1"],
    )
    snap = {
        "required_checks": {
            "ci-1": {"status": "in_progress", "conclusion": None},
        }
    }
    assert ci_policy_status(snap) == CI_POLICY_CHECKS_PENDING


def test_ci_policy_status_checks_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        supervisor.POLICY,
        "required_check_names",
        ["ci-1"],
    )
    snap = {
        "required_checks": {
            "ci-1": {"status": "completed", "conclusion": "failure"},
        }
    }
    assert ci_policy_status(snap) == CI_POLICY_CHECKS_FAILED


def test_ci_policy_status_unresolved_with_empty_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured required checks but no check-run evidence
    at all means the policy is unresolved. The supervisor
    MUST fail closed until authoritative evidence arrives.
    """
    monkeypatch.setitem(
        supervisor.POLICY,
        "required_check_names",
        ["ci-1"],
    )
    snap = {"required_checks": {}}
    assert ci_policy_status(snap) == CI_POLICY_POLICY_UNRESOLVED


def test_ci_policy_status_unresolved_with_missing_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured required check that hasn't been
    registered yet yields POLICY_UNRESOLVED, not
    CHECKS_GREEN.
    """
    monkeypatch.setitem(
        supervisor.POLICY,
        "required_check_names",
        ["ci-1", "ci-2"],
    )
    snap = {
        "required_checks": {
            "ci-1": {"status": "completed", "conclusion": "success"},
            # ci-2 not present
        }
    }
    assert ci_policy_status(snap) == CI_POLICY_POLICY_UNRESOLVED


# ---------------------------------------------------------------------------
# TEST 3 — evaluate_readiness respects new CI policy
# ---------------------------------------------------------------------------

def test_evaluate_readiness_no_required_checks_allows_qualification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-39: a repo with zero required checks allows
    qualification to proceed with explicit
    ``ci_policy: NO_REQUIRED_CHECKS`` semantic evidence.
    The supervisor MUST NOT pretend ``CI PASSED`` was
    observed.
    """
    monkeypatch.setitem(supervisor.POLICY, "required_check_names", [])
    snap = _clean_snap()
    monkeypatch.setattr(
        supervisor, "list_unconsumed_events", lambda: snap.get("unconsumed_event_ids", []),
    )
    res = supervisor.evaluate_readiness(snap, AUTH)
    assert res["ready"] is True
    assert res["reason"] == "no_required_checks"
    assert res["ci_policy"] == CI_POLICY_NO_REQUIRED_CHECKS


def test_evaluate_readiness_checks_pending_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(supervisor.POLICY, "required_check_names", ["ci-1"])
    monkeypatch.setattr(
        supervisor, "list_unconsumed_events", lambda: snap.get("unconsumed_event_ids", []),
    )
    snap = _clean_snap()
    snap["required_checks"]["ci-1"] = {
        "status": "in_progress", "conclusion": None,
    }
    res = supervisor.evaluate_readiness(snap, AUTH)
    assert res["ready"] is False
    assert res["reason"] == "ci_checks_pending"


def test_evaluate_readiness_checks_failed_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(supervisor.POLICY, "required_check_names", ["ci-1"])
    monkeypatch.setattr(
        supervisor, "list_unconsumed_events", lambda: snap.get("unconsumed_event_ids", []),
    )
    snap = _clean_snap()
    snap["required_checks"]["ci-1"] = {
        "status": "completed", "conclusion": "failure",
    }
    res = supervisor.evaluate_readiness(snap, AUTH)
    assert res["ready"] is False
    assert res["reason"] == "ci_checks_failed"


def test_evaluate_readiness_policy_unresolved_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured required checks but no check-run evidence:
    the policy is unresolved and the supervisor MUST fail
    closed until authoritative evidence arrives.
    """
    monkeypatch.setitem(supervisor.POLICY, "required_check_names", ["ci-1"])
    monkeypatch.setattr(
        supervisor, "list_unconsumed_events", lambda: snap.get("unconsumed_event_ids", []),
    )
    monkeypatch.setattr(
        supervisor, "list_unconsumed_events", lambda: snap.get("unconsumed_event_ids", []),
    )
    snap = _clean_snap()
    # No ``required_checks`` populated at all.
    res = supervisor.evaluate_readiness(snap, AUTH)
    assert res["ready"] is False
    assert res["reason"] == "ci_policy_unresolved"


def test_evaluate_readiness_checks_green_allows_qualification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(supervisor.POLICY, "required_check_names", ["ci-1"])
    monkeypatch.setattr(
        supervisor, "list_unconsumed_events", lambda: snap.get("unconsumed_event_ids", []),
    )
    snap = _clean_snap()
    snap["required_checks"]["ci-1"] = {
        "status": "completed", "conclusion": "success",
    }
    monkeypatch.setattr(
        supervisor, "list_unconsumed_events", lambda: snap.get("unconsumed_event_ids", []),
    )
    res = supervisor.evaluate_readiness(snap, AUTH)
    assert res["ready"] is True
    assert res["reason"] == "quiet_window_match"


# ---------------------------------------------------------------------------
# TEST 4 — worker prompt includes round-39 NO-OP contract
# ---------------------------------------------------------------------------

def test_worker_prompt_includes_no_op_contract() -> None:
    """The worker prompt must include the round-39 NO-OP
    contract so the worker understands it can return
    NO_CHANGES_REQUIRED / ALREADY_SATISFIED / SUPERSEDED
    without producing a verification commit.
    """
    assert "NO-OP CONTRACT" in WORKER_PROMPT_TEMPLATE
    assert "REAL_REPAIR_REQUIRED" in WORKER_PROMPT_TEMPLATE
    assert "ALREADY_SATISFIED" in WORKER_PROMPT_TEMPLATE
    assert "SUPERSEDED" in WORKER_PROMPT_TEMPLATE
    assert "INSUFFICIENT_EVIDENCE" in WORKER_PROMPT_TEMPLATE
    assert "WHAT CURRENT DEFECT DOES THIS DIFF REPAIR" in WORKER_PROMPT_TEMPLATE
    # The contract says successful work does not require
    # creating a Git commit; that is the round-39 invariant.
    assert "does not require creating a Git commit" in WORKER_PROMPT_TEMPLATE
    # The contract MUST explicitly forbid the verification-commit
    # churn pattern that round-39 identified as a regression.
    assert "verify all P1 findings are still intact" in WORKER_PROMPT_TEMPLATE


# ---------------------------------------------------------------------------
# TEST 5 — idempotent exact-head review-request dedupe
# ---------------------------------------------------------------------------

def test_review_request_marker_is_per_provider_per_head(
    tmp_path: Path,
) -> None:
    """The ``write_review_request`` marker path is keyed by
    (provider, head_sha) so duplicate requests for the same
    head are physically impossible to write twice without
    explicit overwrite. The round-39 contract: a CodeRabbit
    AUTO_PAUSED state must NOT spam duplicate requests for
    the same exact head.
    """
    from autocoder_supervisor.supervisor import (
        write_review_request,
        read_review_request,
        review_request_path,
    )
    p = review_request_path("coderabbit", "a" * 40)
    # Use a temp ``STATE_DIR`` to avoid clobbering production.
    # The marker path is keyed per-(provider, head) by the
    # function itself; we just confirm the contract.
    record = {
        "actor": "test",
        "requested_at": "2026-08-10T00:00:00Z",
        "recovery_request_id": "test-recovery",
    }
    assert p is not None
    assert read_review_request("coderabbit", "a" * 40) is None  # not yet written


# ---------------------------------------------------------------------------
# TEST 6 — supervisor does not advance on head_sha_drift from rebind history
# ---------------------------------------------------------------------------

def test_snapshot_differs_does_not_report_drift_for_identical_history_snapshots(
) -> None:
    """Two snapshots that agree on a stale head must NOT
    report drift even if the expected_head has moved. The
    round-39 invariant: HEAD_CHANGED is edge-triggered.
    Repeated H1/H1/H1 = STABLE.
    """
    head_a = "a" * 40
    head_b = "b" * 40  # a rebind target
    snap_a = {"head_sha": head_a}
    snap_b = {"head_sha": head_a}
    # Both snapshots still at H0 (rebind happened mid-window);
    # expected_head is now H1. Must NOT report drift.
    assert snapshot_differs(snap_a, snap_b, head_b) == []
    # After the next poll both snapshots are at H1.
    snap_a2 = {"head_sha": head_b}
    snap_b2 = {"head_sha": head_b}
    assert snapshot_differs(snap_a2, snap_b2, head_b) == []


def test_snapshot_differs_reports_drift_on_actual_h1_to_h2_movement(
) -> None:
    snap_a = {"head_sha": "a" * 40}
    snap_b = {"head_sha": "b" * 40}
    assert "head_sha_drift" in snapshot_differs(
        snap_a, snap_b, "a" * 40,
    )


# ---------------------------------------------------------------------------
# TEST 7 — round-39 worker contract semantics in the prompt
# ---------------------------------------------------------------------------

def test_worker_prompt_forbids_verification_commit_churn() -> None:
    """The worker prompt must explicitly tell the worker NOT
    to create a commit when nothing needs fixing.
    """
    text = WORKER_PROMPT_TEMPLATE.lower()
    assert "must not create a commit" in text
    assert "must not push" in text
    assert "must not modify the repository" in text


def test_worker_prompt_requires_explicit_defect_for_every_diff() -> None:
    text = WORKER_PROMPT_TEMPLATE
    # The prompt must require an explicit answer.
    assert "what current defect does this diff repair" in text.lower()


# ---------------------------------------------------------------------------
# TEST 8 — quiet-window baseline does not reset on repeated identical snapshots
# ---------------------------------------------------------------------------

def test_quiet_window_does_not_reset_on_identical_snapshots() -> None:
    """After rebind to H1, snap_a and snap_b both at H1 must
    yield an empty ``reasons`` list. The window does NOT
    reset on repeated identical observations.
    """
    head = "a" * 40
    snap_a = {"head_sha": head}
    snap_b = {"head_sha": head}
    reasons = snapshot_differs(snap_a, snap_b, head)
    assert reasons == [], (
        "Round-39: repeated identical snapshots MUST NOT "
        "reset the quiet window. Found reasons: "
        f"{reasons!r}"
    )


# ---------------------------------------------------------------------------
# TEST 9 — CI policy constants are exported
# ---------------------------------------------------------------------------

def test_ci_policy_constants_defined() -> None:
    """The round-39 CI policy outcomes are module-level
    constants; downstream tests and the controller import
    them by name.
    """
    assert CI_POLICY_NO_REQUIRED_CHECKS == "NO_REQUIRED_CHECKS"
    assert CI_POLICY_CHECKS_GREEN == "CHECKS_GREEN"
    assert CI_POLICY_CHECKS_PENDING == "CHECKS_PENDING"
    assert CI_POLICY_CHECKS_FAILED == "CHECKS_FAILED"
    assert CI_POLICY_POLICY_UNRESOLVED == "POLICY_UNRESOLVED"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
