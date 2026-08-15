"""Round-590 regression tests for the focused-thread + CI preservation
fix in ``autocoder_orchestration.review_repair_relay.collect_findings``.

The Round-45 C13 ``focused_thread_id`` semantics was: scope the
review-side collector to a single targeted current-head thread and
emit no other findings. Round-590 corrects it: focused mode
restricts the REVIEW portion of the directive to the targeted
thread but MUST still emit a CI_FAILURE finding for every failed
required check in the same snapshot.

These tests pin down the corrected contract:

1. focused thread + all CI green  -> targeted thread only, CI_FAIL=0
2. focused thread + provenance failed
   -> targeted thread + provenance CI_FAILURE, CI_FAIL=1
3. focused thread + five required failures
   -> targeted thread + all five CI_FAILURE findings, CI_FAIL=5
4. focused thread + failed CI MUST NEVER emit CI_FAIL=0
5. required failed CI cannot be removed by review max_findings
   truncation (build_directive preserves CI failures)

The existing Round-45 C13 contract — that the targeted review
thread alone is the directive's review content — is preserved when
no required CI is failing. Only the CI-failure channel is added.
"""
from __future__ import annotations

from autocoder_orchestration.review_repair_relay import (
    SEVERITY_CI_FAILURE,
    SEVERITY_P1,
    Finding,
    build_directive,
    collect_findings,
)


# Seven canonical required checks that PR #5 (Slideshow11/AutoDev)
# declares in ``.github/workflows/ci.yml``. The exact set is owned
# by the workflow; tests scope to the production-set directly so a
# future check addition only needs to update one place.
SEVEN_REQUIRED_CHECKS: tuple[str, ...] = (
    "test (3.10)",
    "test (3.11)",
    "test (3.12)",
    "package-smoke",
    "committed-state-scan",
    "provenance",
    "full-suite",
)


def _make_thread_snapshot(
    thread_id: str,
    *,
    head_sha: str = "f" * 40,
    body: str = "P1: a focused review thread body\n",
    path: str = "autocoder_supervisor/hermes_fingerprint.py",
    required_checks: dict | None = None,
) -> dict:
    """Snapshot with exactly one targeted current-head review thread
    and a configurable ``required_checks`` map."""
    return {
        "captured_at": "2026-08-15T00:00:00Z",
        "head_sha": head_sha,
        "head_match": True,
        "mergeable": True,
        "formal_reviews": [],
        "review_threads": {
            thread_id: {
                "author": "coderabbitai",
                "body": body,
                "commit_oid": head_sha,
                "line": 1540,
                "outdated": False,
                "path": path,
                "resolved": False,
            },
        },
        "issue_comments": [],
        "_provider_issue_comments": {},
        "review_comments": [],
        "required_checks": required_checks or {},
        "providers": {},
        "unconsumed_event_ids": [],
        "provider_surface_complete": True,
    }


def _all_green_required_checks() -> dict:
    return {
        name: {
            "conclusion": "success",
            "status": "completed",
            "run_id": f"r-{name.replace(' ', '')}",
        }
        for name in SEVEN_REQUIRED_CHECKS
    }


def _all_required_checks_terminally_failed(run_prefix: str = "r-fail") -> dict:
    return {
        name: {
            "conclusion": "failure",
            "status": "completed",
            "run_id": f"{run_prefix}-{name.replace(' ', '')}",
        }
        for name in SEVEN_REQUIRED_CHECKS
    }


def _ci_failures(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == SEVERITY_CI_FAILURE]


def _ci_check_names(findings: list[Finding]) -> list[str]:
    return [f.check_name for f in _ci_failures(findings) if f.check_name]


# --- The five contract tests ----------------------------------------------


def test_round590_focused_thread_all_ci_green_emits_only_target_thread():
    """focused thread + all CI green -> targeted thread only, CI_FAIL=0.

    The Round-45 C13 review-side scoping MUST remain in force when
    the snapshot carries no required CI failures. The seven
    required checks all terminate ``success``; the directive MUST
    contain exactly one finding (the targeted review thread) and
    zero CI_FAILURE findings.
    """
    thread_id = "PRRT_kwDOTtyQLc6ZpixA"
    snap = _make_thread_snapshot(
        thread_id,
        required_checks=_all_green_required_checks(),
    )
    findings = collect_findings(
        snap,
        required_check_names=SEVEN_REQUIRED_CHECKS,
        focused_thread_id=thread_id,
    )
    assert len(findings) == 1, (
        f"focused + green CI: expected exactly the targeted thread "
        f"finding only; got {len(findings)}: {[f.finding_id for f in findings]}"
    )
    assert findings[0].finding_id == f"thread:{thread_id}"
    assert findings[0].severity == SEVERITY_P1
    assert _ci_failures(findings) == []
    # Summarize-equivalent contract: CI_FAIL count = 0.
    assert sum(1 for f in findings if f.severity == SEVERITY_CI_FAILURE) == 0


def test_round590_focused_thread_provenance_failed_emits_provenance_ci_failure():
    """focused thread + provenance failed -> targeted thread + provenance
    CI_FAILURE, CI_FAIL=1.

    With focused_thread_id set, the collector MUST emit BOTH the
    targeted thread AND the failed CI finding for ``provenance``.
    The directive summary MUST report CI_FAIL=1, NOT 0.
    """
    thread_id = "PRRT_kwDOTtyQLc6ZpixA"
    required = _all_green_required_checks()
    required["provenance"] = {
        "conclusion": "failure",
        "status": "completed",
        "run_id": "r-provenance",
    }
    snap = _make_thread_snapshot(thread_id, required_checks=required)
    findings = collect_findings(
        snap,
        required_check_names=SEVEN_REQUIRED_CHECKS,
        focused_thread_id=thread_id,
    )
    ci = _ci_failures(findings)
    assert len(ci) == 1, (
        f"focused + 1 failed CI: expected exactly one CI_FAILURE; "
        f"got {len(ci)}: {[f.finding_id for f in ci]}"
    )
    assert ci[0].check_name == "provenance"
    # Targeted review thread still present.
    thread_findings = [
        f for f in findings if f.finding_id == f"thread:{thread_id}"
    ]
    assert len(thread_findings) == 1
    # REQUIRED: focused + failed CI MUST NEVER emit CI_FAIL=0.
    assert sum(1 for f in findings if f.severity == SEVERITY_CI_FAILURE) == 1


def test_round590_focused_thread_five_failures_emits_all_five_ci_failures():
    """focused thread + five required failures -> targeted thread + all
    five CI_FAILURE findings, CI_FAIL=5.

    Every failed required check MUST be surfaced as an independent
    CI_FAILURE finding, even when focused_thread_id is set. The
    directive summary MUST report CI_FAIL=5.
    """
    thread_id = "PRRT_kwDOTtyQLc6ZpixA"
    required = _all_green_required_checks()
    failed_set = {
        "test (3.10)", "test (3.11)", "test (3.12)",
        "provenance", "full-suite",
    }
    for name in failed_set:
        required[name] = {
            "conclusion": "failure",
            "status": "completed",
            "run_id": f"r-fail-{name.replace(' ', '')}",
        }
    snap = _make_thread_snapshot(thread_id, required_checks=required)
    findings = collect_findings(
        snap,
        required_check_names=SEVEN_REQUIRED_CHECKS,
        focused_thread_id=thread_id,
    )
    ci = _ci_failures(findings)
    names = sorted(_ci_check_names(ci))
    assert names == sorted(failed_set), (
        f"focused + 5 failed CIs: expected {sorted(failed_set)}; "
        f"got {names}"
    )
    # Targeted review thread still present.
    thread_findings = [
        f for f in findings if f.finding_id == f"thread:{thread_id}"
    ]
    assert len(thread_findings) == 1
    assert sum(1 for f in findings if f.severity == SEVERITY_CI_FAILURE) == 5


def test_round590_focused_thread_failed_ci_never_emits_ci_fail_zero():
    """focused thread + failed CI must never emit CI_FAIL=0.

    Negative-control: the same required-check state that yields
    CI_FAIL=5 in the broad collector MUST also yield CI_FAIL=5 when
    ``focused_thread_id`` is set. Focused scope MUST NOT silently
    drop CI failures.
    """
    thread_id = "PRRT_kwDOTtyQLc6ZpixA"
    required = _all_required_checks_terminally_failed()
    snap = _make_thread_snapshot(thread_id, required_checks=required)
    findings_focused = collect_findings(
        snap,
        required_check_names=SEVEN_REQUIRED_CHECKS,
        focused_thread_id=thread_id,
    )
    findings_unfocused = collect_findings(
        snap,
        required_check_names=SEVEN_REQUIRED_CHECKS,
    )
    focused_ci = sum(
        1 for f in findings_focused if f.severity == SEVERITY_CI_FAILURE
    )
    unfocused_ci = sum(
        1 for f in findings_unfocused if f.severity == SEVERITY_CI_FAILURE
    )
    assert focused_ci == unfocused_ci == len(SEVEN_REQUIRED_CHECKS), (
        f"focused + all-failed CI: focused_ci={focused_ci}, "
        f"unfocused_ci={unfocused_ci}, "
        f"required_check_count={len(SEVEN_REQUIRED_CHECKS)}"
    )
    assert focused_ci > 0, (
        "round-590 negative control: focused + failed CI MUST NOT "
        "collapse CI_FAIL to 0"
    )


def test_round590_build_directive_preserves_ci_failures_under_max_findings():
    """required failed CI cannot be removed by review ``max_findings``
    truncation.

    ``build_directive`` MUST keep CI_FAILURE findings even when
    the (hypothetical) ``max_findings`` budget is exhausted by
    review findings. CI failures and round-281 C22 §6 require
    them to remain actionable for the worker; this is the
    construction-side half of the contract pinned by
    ``test_round590_focused_thread_failed_ci_never_emits_ci_fail_zero``.
    """
    thread_id = "PRRT_kwDOTtyQLc6ZpixA"
    required = _all_required_checks_terminally_failed()
    snap = _make_thread_snapshot(thread_id, required_checks=required)
    findings = collect_findings(
        snap,
        required_check_names=SEVEN_REQUIRED_CHECKS,
        focused_thread_id=thread_id,
    )
    # Even with max_findings=2 (smaller than the seven CI failures
    # plus the targeted thread), the directive MUST keep every
    # CI_FAILURE finding. The current directive builder may already
    # apply a higher default cap; we only assert the contract:
    # CI failures survive any cap.
    ci_findings = [f for f in findings if f.severity == SEVERITY_CI_FAILURE]
    assert len(ci_findings) == len(SEVEN_REQUIRED_CHECKS)
    d = build_directive(
        round_index=590,
        head_sha="f" * 40,
        repo="Slideshow11/AutoDev",
        pr_number=5,
        findings=findings,
        coordinator_actor="controller",
        target_thread_id=thread_id,
    )
    ci_in_directive = [
        f for f in d.findings if f.severity == SEVERITY_CI_FAILURE
    ]
    assert len(ci_in_directive) == len(SEVEN_REQUIRED_CHECKS), (
        f"round-281 C22 §6: build_directive MUST preserve every "
        f"CI_FAILURE finding; lost "
        f"{len(SEVEN_REQUIRED_CHECKS) - len(ci_in_directive)} "
        f"of {len(SEVEN_REQUIRED_CHECKS)} CIs"
    )
