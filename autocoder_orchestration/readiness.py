"""Canonical readiness engine.

The readiness engine is the single evaluator that decides whether
the run is ready for a candidate to be built. The controller, the
candidate builder, the status CLI, the strict readiness observer,
the verifier preflight, and the merge-authorization preflight all
invoke this engine.

Readiness is a structured decision with per-gate results. There is
no "skip" or "ignore" flag. A caller that wants to bypass readiness
must explicitly opt out by calling a different code path (which
fails closed at the gate level).
"""
from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


# === Gate IDs ===
GATE_EXACT_LIVE_PR_IDENTITY = "exact_live_pr_identity"
GATE_EXACT_HEAD = "exact_head"
GATE_BASE_BRANCH_AND_SHA = "base_branch_and_sha"
GATE_CLEAN_IMPLEMENTATION_CHECKOUT = "clean_implementation_checkout"
GATE_REQUIRED_CI_JOBS_PRESENT = "required_ci_jobs_present"
GATE_REQUIRED_CI_JOBS_SUCCESSFUL = "required_ci_jobs_successful"
GATE_CI_BOUND_TO_EXACT_HEAD = "ci_jobs_bound_to_exact_head"
GATE_REVIEWER_COMPLETION = "reviewer_completion"
GATE_REVIEWER_BOUND_TO_EXACT_HEAD = "reviewer_bound_to_exact_head"
GATE_UNRESOLVED_CURRENT_THREADS = "unresolved_current_threads"
GATE_UNRESOLVED_OUTDATED_THREADS = "unresolved_outdated_threads"
GATE_REPAIRED_FINDING_STILL_VALID = "repaired_finding_still_valid"
GATE_INCONCLUSIVE_FINDINGS = "inconclusive_findings"
GATE_BODY_RECONCILED = "body_reconciled"
GATE_NO_ACTIVE_IMPL_WORKER = "no_active_implementation_worker"
GATE_NO_ACTIVE_REPAIR_WORKER = "no_active_repair_worker"
GATE_NO_ACTIVE_REVIEW_REQUEST = "no_active_review_request"
GATE_NO_REVIEWER_IN_PROGRESS = "no_reviewer_in_progress"
GATE_NO_UNCONSUMED_EVENTS = "no_unconsumed_events"
GATE_NO_ACTIVE_CONFLICTING_LEASE = "no_active_conflicting_lease"
GATE_NO_API_FAILURE = "no_api_failure"
GATE_NO_PARSE_FAILURE = "no_parse_failure"
GATE_NO_FALLBACK_SUCCESS = "no_fallback_success"
GATE_QUIET_WINDOW_COMPLETE = "quiet_window_complete"
GATE_LOCK_RELEASED_AFTER_SHUTDOWN = "lock_released_after_shutdown"
GATE_INPUTS_FROZEN = "inputs_frozen"

ALL_GATES: Tuple[str, ...] = (
    GATE_EXACT_LIVE_PR_IDENTITY,
    GATE_EXACT_HEAD,
    GATE_BASE_BRANCH_AND_SHA,
    GATE_CLEAN_IMPLEMENTATION_CHECKOUT,
    GATE_REQUIRED_CI_JOBS_PRESENT,
    GATE_REQUIRED_CI_JOBS_SUCCESSFUL,
    GATE_CI_BOUND_TO_EXACT_HEAD,
    GATE_REVIEWER_COMPLETION,
    GATE_REVIEWER_BOUND_TO_EXACT_HEAD,
    GATE_UNRESOLVED_CURRENT_THREADS,
    GATE_UNRESOLVED_OUTDATED_THREADS,
    GATE_REPAIRED_FINDING_STILL_VALID,
    GATE_INCONCLUSIVE_FINDINGS,
    GATE_BODY_RECONCILED,
    GATE_NO_ACTIVE_IMPL_WORKER,
    GATE_NO_ACTIVE_REPAIR_WORKER,
    GATE_NO_ACTIVE_REVIEW_REQUEST,
    GATE_NO_REVIEWER_IN_PROGRESS,
    GATE_NO_UNCONSUMED_EVENTS,
    GATE_NO_ACTIVE_CONFLICTING_LEASE,
    GATE_NO_API_FAILURE,
    GATE_NO_PARSE_FAILURE,
    GATE_NO_FALLBACK_SUCCESS,
    GATE_QUIET_WINDOW_COMPLETE,
    GATE_LOCK_RELEASED_AFTER_SHUTDOWN,
    GATE_INPUTS_FROZEN,
)


@dataclass
class GateResult:
    """Per-gate evaluation result."""

    gate: str
    passed: bool
    detail: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "gate": self.gate,
            "passed": self.passed,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


@dataclass
class ReadinessDecision:
    """Output of :class:`ReadinessEngine.evaluate`."""

    run_id: str
    repo: str
    pr_number: Optional[int]
    expected_head: str
    observed_head: Optional[str]
    overall_passed: bool
    gate_results: List[GateResult]
    evaluated_at: str

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "repo": self.repo,
            "pr_number": self.pr_number,
            "expected_head": self.expected_head,
            "observed_head": self.observed_head,
            "overall_passed": self.overall_passed,
            "gate_results": [g.to_dict() for g in self.gate_results],
            "evaluated_at": self.evaluated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ReadinessDecision":
        if not isinstance(payload, dict):
            raise ValueError("decision payload must be a dict")
        return cls(
            run_id=str(payload["run_id"]),
            repo=str(payload["repo"]),
            pr_number=int(payload["pr_number"]) if payload.get("pr_number") is not None else None,
            expected_head=str(payload["expected_head"]),
            observed_head=payload.get("observed_head"),
            overall_passed=bool(payload["overall_passed"]),
            gate_results=[
                GateResult(
                    gate=g["gate"],
                    passed=bool(g["passed"]),
                    detail=str(g.get("detail", "")),
                    evidence=dict(g.get("evidence", {})),
                )
                for g in payload["gate_results"]
            ],
            evaluated_at=str(payload["evaluated_at"]),
        )

    def failed_gates(self) -> List[GateResult]:
        return [g for g in self.gate_results if not g.passed]


@dataclass
class ReadinessCertificate:
    """A signed, versioned decision that binds the run to a specific
    point in time. The candidate builder will refuse to build without
    a valid certificate."""

    decision: ReadinessDecision
    issued_at: str
    expires_at: str
    certificate_id: str
    issuer: str
    schema_version: str = "autocoder.readiness_certificate.v1"

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "certificate_id": self.certificate_id,
            "issuer": self.issuer,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "decision": self.decision.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ReadinessCertificate":
        if payload.get("schema_version") != "autocoder.readiness_certificate.v1":
            raise ValueError("unsupported certificate schema")
        return cls(
            decision=ReadinessDecision.from_dict(payload["decision"]),
            issued_at=str(payload["issued_at"]),
            expires_at=str(payload["expires_at"]),
            certificate_id=str(payload["certificate_id"]),
            issuer=str(payload["issuer"]),
        )

    def is_expired(self, now: str) -> bool:
        return self.expires_at <= now


class ReadOnlyGithubClient:
    """Thin read-only GitHub client used by the readiness engine.

    The client holds a token but only exposes read-only
    operations. Mutation methods are absent by construction.
    """

    def __init__(self, token: str, repo_owner: str, repo_name: str, pr_number: int) -> None:
        if not isinstance(token, str) or not token:
            raise ValueError("token must be a non-empty string")
        if "/" in repo_owner or "/" in repo_name:
            raise ValueError("repo_owner/repo_name must not contain slashes")
        if not isinstance(pr_number, int) or pr_number <= 0:
            raise ValueError("pr_number must be a positive integer")
        self._token = token
        self._repo_owner = repo_owner
        self._repo_name = repo_name
        self._pr_number = pr_number

    def get_pr(self) -> dict:
        """Return the parsed PR payload. Subclasses may override."""
        raise NotImplementedError("subclass must implement get_pr")

    def get_check_runs(self, head_sha: str) -> list:
        """Return the list of check runs for the given head SHA."""
        raise NotImplementedError("subclass must implement get_check_runs")

    def get_review_threads(self) -> list:
        """Return the list of review threads for the PR."""
        raise NotImplementedError("subclass must implement get_review_threads")

    def get_reviews(self) -> list:
        """Return the list of formal reviews for the PR."""
        raise NotImplementedError("subclass must implement get_reviews")


# === Readiness Engine ===
class ReadinessEngine:
    """Canonical readiness evaluator.

    The engine is read-only. It does not write any state.
    """

    def __init__(self, *, run_id: str, repo: str, pr_number: Optional[int]) -> None:
        self.run_id = run_id
        self.repo = repo
        self.pr_number = pr_number

    def evaluate(
        self,
        *,
        expected_head: str,
        expected_base_sha: str,
        expected_base_branch: str,
        required_ci_jobs: Tuple[str, ...],
        live_pr_payload: Dict[str, Any],
        live_check_runs: List[Dict[str, Any]],
        live_threads: List[Dict[str, Any]],
        live_reviews: List[Dict[str, Any]],
        body_reconciled: bool,
        impl_worker_active: bool,
        repair_worker_active: bool,
        review_request_in_progress: bool,
        reviewer_in_progress: bool,
        unconsumed_event_count: int,
        active_conflicting_lease: bool,
        api_failure: Optional[str],
        parse_failure: Optional[str],
        fallback_success: bool,
        quiet_window_complete: bool,
        quiet_window_observations: List[Dict[str, Any]],
        quiet_window_min_monotonic: float,
        quiet_window_first_utc: Optional[str],
        quiet_window_last_utc: Optional[str],
        lock_released_after_shutdown: bool,
        inputs_frozen: bool,
        invalid_finding_descriptions: List[str],
        inconclusive_finding_descriptions: List[str],
    ) -> ReadinessDecision:
        results: List[GateResult] = []

        # Identity
        results.append(self._gate_live_pr_identity(live_pr_payload))
        results.append(self._gate_exact_head(live_pr_payload, expected_head))
        results.append(self._gate_base(live_pr_payload, expected_base_branch, expected_base_sha))

        # Find live head
        observed_head = (
            live_pr_payload.get("head", {}).get("sha")
            if isinstance(live_pr_payload, dict)
            else None
        )

        # Checkout
        results.append(self._gate_clean_checkout(impl_worker_active, repair_worker_active))

        # CI
        results.append(self._gate_ci_jobs_present(live_check_runs, required_ci_jobs))
        results.append(self._gate_ci_jobs_successful(live_check_runs, required_ci_jobs))
        results.append(self._gate_ci_bound_to_head(live_check_runs, expected_head))

        # Reviewer
        results.append(self._gate_reviewer_completion(live_reviews, expected_head))
        results.append(self._gate_reviewer_bound_to_head(live_reviews, expected_head))

        # Threads
        results.append(self._gate_unresolved_current(live_threads))
        results.append(self._gate_unresolved_outdated(live_threads))
        results.append(self._gate_repaired_finding_still_valid(invalid_finding_descriptions))
        results.append(self._gate_inconclusive_findings(inconclusive_finding_descriptions))

        # Body
        results.append(self._gate_body_reconciled(body_reconciled))

        # Activity
        results.append(self._gate_no_impl_worker(impl_worker_active))
        results.append(self._gate_no_repair_worker(repair_worker_active))
        results.append(self._gate_no_review_request(review_request_in_progress))
        results.append(self._gate_no_reviewer_in_progress(reviewer_in_progress))
        results.append(self._gate_no_unconsumed_events(unconsumed_event_count))
        results.append(self._gate_no_active_conflicting_lease(active_conflicting_lease))

        # API / parse
        results.append(self._gate_no_api_failure(api_failure))
        results.append(self._gate_no_parse_failure(parse_failure))
        results.append(self._gate_no_fallback_success(fallback_success))

        # Quiet window
        results.append(self._gate_quiet_window(
            quiet_window_complete,
            quiet_window_observations,
            quiet_window_min_monotonic,
            quiet_window_first_utc,
            quiet_window_last_utc,
        ))

        # Shutdown and freeze
        results.append(self._gate_lock_released_after_shutdown(lock_released_after_shutdown))
        results.append(self._gate_inputs_frozen(inputs_frozen))

        overall_passed = all(g.passed for g in results)
        return ReadinessDecision(
            run_id=self.run_id,
            repo=self.repo,
            pr_number=self.pr_number,
            expected_head=expected_head,
            observed_head=observed_head,
            overall_passed=overall_passed,
            gate_results=results,
            evaluated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    # === Gate implementations ===
    def _gate_live_pr_identity(self, pr: dict) -> GateResult:
        if not isinstance(pr, dict):
            return GateResult(GATE_EXACT_LIVE_PR_IDENTITY, False, "live PR payload is not a dict")
        state = pr.get("state")
        merged = pr.get("merged", False)
        draft = pr.get("draft", False)
        if state != "open":
            return GateResult(GATE_EXACT_LIVE_PR_IDENTITY, False, f"PR state is {state!r}, expected 'open'")
        if merged:
            return GateResult(GATE_EXACT_LIVE_PR_IDENTITY, False, "PR is already merged")
        if draft:
            return GateResult(GATE_EXACT_LIVE_PR_IDENTITY, False, "PR is a draft")
        if self.pr_number is not None and pr.get("number") != self.pr_number:
            return GateResult(
                GATE_EXACT_LIVE_PR_IDENTITY,
                False,
                f"PR number mismatch: caller expected {self.pr_number}, "
                f"live PR is {pr.get('number')}",
            )
        return GateResult(
            GATE_EXACT_LIVE_PR_IDENTITY,
            True,
            "PR is open, unmerged, not draft, and number matches",
            evidence={"pr_number": pr.get("number")},
        )

    def _gate_exact_head(self, pr: dict, expected_head: str) -> GateResult:
        if not isinstance(pr, dict):
            return GateResult(GATE_EXACT_HEAD, False, "live PR payload is not a dict")
        head = pr.get("head", {}).get("sha")
        if head != expected_head:
            return GateResult(
                GATE_EXACT_HEAD,
                False,
                f"head drift: expected {expected_head!r}, live {head!r}",
                evidence={"expected_head": expected_head, "observed_head": head},
            )
        return GateResult(
            GATE_EXACT_HEAD,
            True,
            "PR head matches expected head exactly",
            evidence={"head_sha": head},
        )

    def _gate_base(self, pr: dict, expected_base: str, expected_base_sha: str) -> GateResult:
        if not isinstance(pr, dict):
            return GateResult(GATE_BASE_BRANCH_AND_SHA, False, "live PR payload is not a dict")
        base = pr.get("base", {})
        if base.get("ref") != expected_base:
            return GateResult(
                GATE_BASE_BRANCH_AND_SHA,
                False,
                f"base branch {base.get('ref')!r} != expected {expected_base!r}",
                evidence={"expected_base": expected_base, "observed_base": base.get("ref")},
            )
        if base.get("sha") != expected_base_sha:
            return GateResult(
                GATE_BASE_BRANCH_AND_SHA,
                False,
                f"base SHA {base.get('sha')!r} != expected {expected_base_sha!r}",
            )
        return GateResult(
            GATE_BASE_BRANCH_AND_SHA,
            True,
            "base branch and SHA match",
            evidence={"base_ref": base.get("ref"), "base_sha": base.get("sha")},
        )

    def _gate_clean_checkout(self, impl_worker_active: bool, repair_worker_active: bool) -> GateResult:
        ok = not impl_worker_active and not repair_worker_active
        return GateResult(
            GATE_CLEAN_IMPLEMENTATION_CHECKOUT,
            ok,
            "no active implementation or repair worker" if ok else "active worker detected",
            evidence={
                "impl_worker_active": impl_worker_active,
                "repair_worker_active": repair_worker_active,
            },
        )

    def _gate_ci_jobs_present(self, runs: list, required: Tuple[str, ...]) -> GateResult:
        if not isinstance(runs, list):
            return GateResult(GATE_REQUIRED_CI_JOBS_PRESENT, False, "check_runs is not a list")
        by_name = {r.get("name"): r for r in runs if isinstance(r, dict)}
        missing = [name for name in required if name not in by_name]
        ok = not missing
        return GateResult(
            GATE_REQUIRED_CI_JOBS_PRESENT,
            ok,
            "all required CI jobs present" if ok else "missing required CI jobs",
            evidence={"missing": missing, "required": list(required)},
        )

    def _gate_ci_jobs_successful(self, runs: list, required: Tuple[str, ...]) -> GateResult:
        if not isinstance(runs, list):
            return GateResult(GATE_REQUIRED_CI_JOBS_SUCCESSFUL, False, "check_runs is not a list")
        by_name = {r.get("name"): r for r in runs if isinstance(r, dict)}
        failed = []
        for name in required:
            r = by_name.get(name)
            if not r:
                continue
            status = r.get("status")
            conclusion = r.get("conclusion")
            if status != "completed" or conclusion != "success":
                failed.append({"name": name, "status": status, "conclusion": conclusion})
        ok = not failed
        return GateResult(
            GATE_REQUIRED_CI_JOBS_SUCCESSFUL,
            ok,
            "all required CI jobs successful" if ok else "some required CI jobs failed",
            evidence={"failed": failed},
        )

    def _gate_ci_bound_to_head(self, runs: list, expected_head: str) -> GateResult:
        if not isinstance(runs, list):
            return GateResult(GATE_CI_BOUND_TO_EXACT_HEAD, False, "check_runs is not a list")
        mismatched = [r.get("name") for r in runs if isinstance(r, dict) and r.get("head_sha") != expected_head]
        ok = not mismatched
        return GateResult(
            GATE_CI_BOUND_TO_EXACT_HEAD,
            ok,
            "all CI jobs bound to exact head" if ok else "stale-head CI jobs detected",
            evidence={"expected_head": expected_head, "mismatched": mismatched},
        )

    def _gate_reviewer_completion(self, reviews: list, expected_head: str) -> GateResult:
        if not isinstance(reviews, list):
            return GateResult(GATE_REVIEWER_COMPLETION, False, "reviews is not a list")
        for r in reviews:
            if not isinstance(r, dict):
                continue
            if r.get("author") != "coderabbitai":
                continue
            if r.get("commit_oid") != expected_head:
                continue
            # Found exact-head review
            state = r.get("state")
            if state == "APPROVED":
                return GateResult(
                    GATE_REVIEWER_COMPLETION,
                    True,
                    "exact-head CodeRabbit review is APPROVED",
                    evidence={"review_state": state},
                )
            return GateResult(
                GATE_REVIEWER_COMPLETION,
                False,
                f"exact-head CodeRabbit review is {state!r}, expected APPROVED",
                evidence={"review_state": state},
            )
        return GateResult(
            GATE_REVIEWER_COMPLETION,
            False,
            "no exact-head CodeRabbit review found",
            evidence={"expected_head": expected_head},
        )

    def _gate_reviewer_bound_to_head(self, reviews: list, expected_head: str) -> GateResult:
        if not isinstance(reviews, list):
            return GateResult(GATE_REVIEWER_BOUND_TO_EXACT_HEAD, False, "reviews is not a list")
        for r in reviews:
            if not isinstance(r, dict):
                continue
            if r.get("author") == "coderabbitai" and r.get("commit_oid") == expected_head:
                return GateResult(
                    GATE_REVIEWER_BOUND_TO_EXACT_HEAD,
                    True,
                    "exact-head CodeRabbit review present",
                    evidence={"commit_oid": r.get("commit_oid")},
                )
        return GateResult(
            GATE_REVIEWER_BOUND_TO_EXACT_HEAD,
            False,
            "no review on exact head",
            evidence={"expected_head": expected_head},
        )

    def _gate_unresolved_current(self, threads: list) -> GateResult:
        unresolved = [
            t for t in threads
            if isinstance(t, dict) and not t.get("isResolved") and not t.get("isOutdated")
        ]
        ok = len(unresolved) == 0
        return GateResult(
            GATE_UNRESOLVED_CURRENT_THREADS,
            ok,
            "no unresolved current threads" if ok else f"{len(unresolved)} unresolved current threads",
            evidence={"count": len(unresolved)},
        )

    def _gate_unresolved_outdated(self, threads: list) -> GateResult:
        unresolved = [
            t for t in threads
            if isinstance(t, dict) and not t.get("isResolved") and t.get("isOutdated")
        ]
        ok = len(unresolved) == 0
        return GateResult(
            GATE_UNRESOLVED_OUTDATED_THREADS,
            ok,
            "no unresolved outdated threads" if ok else f"{len(unresolved)} unresolved outdated threads",
            evidence={"count": len(unresolved)},
        )

    def _gate_repaired_finding_still_valid(self, descriptions: list) -> GateResult:
        ok = len(descriptions) == 0
        return GateResult(
            GATE_REPAIRED_FINDING_STILL_VALID,
            ok,
            "no repaired-but-still-valid findings" if ok else f"{len(descriptions)} repaired findings still valid",
            evidence={"descriptions": list(descriptions)},
        )

    def _gate_inconclusive_findings(self, descriptions: list) -> GateResult:
        ok = len(descriptions) == 0
        return GateResult(
            GATE_INCONCLUSIVE_FINDINGS,
            ok,
            "no inconclusive findings" if ok else f"{len(descriptions)} inconclusive findings",
            evidence={"descriptions": list(descriptions)},
        )

    def _gate_body_reconciled(self, body_reconciled: bool) -> GateResult:
        return GateResult(
            GATE_BODY_RECONCILED,
            body_reconciled,
            "PR body reconciled" if body_reconciled else "PR body not yet reconciled",
            evidence={"reconciled": body_reconciled},
        )

    def _gate_no_impl_worker(self, active: bool) -> GateResult:
        return GateResult(
            GATE_NO_ACTIVE_IMPL_WORKER,
            not active,
            "no active implementation worker" if not active else "implementation worker active",
            evidence={"active": active},
        )

    def _gate_no_repair_worker(self, active: bool) -> GateResult:
        return GateResult(
            GATE_NO_ACTIVE_REPAIR_WORKER,
            not active,
            "no active repair worker" if not active else "repair worker active",
            evidence={"active": active},
        )

    def _gate_no_review_request(self, in_progress: bool) -> GateResult:
        return GateResult(
            GATE_NO_ACTIVE_REVIEW_REQUEST,
            not in_progress,
            "no active review request" if not in_progress else "review request in progress",
            evidence={"in_progress": in_progress},
        )

    def _gate_no_reviewer_in_progress(self, in_progress: bool) -> GateResult:
        return GateResult(
            GATE_NO_REVIEWER_IN_PROGRESS,
            not in_progress,
            "no reviewer in progress" if not in_progress else "reviewer in progress",
            evidence={"in_progress": in_progress},
        )

    def _gate_no_unconsumed_events(self, count: int) -> GateResult:
        return GateResult(
            GATE_NO_UNCONSUMED_EVENTS,
            count == 0,
            "no unconsumed events" if count == 0 else f"{count} unconsumed events",
            evidence={"count": count},
        )

    def _gate_no_active_conflicting_lease(self, active: bool) -> GateResult:
        return GateResult(
            GATE_NO_ACTIVE_CONFLICTING_LEASE,
            not active,
            "no active conflicting lease" if not active else "active conflicting lease",
            evidence={"active": active},
        )

    def _gate_no_api_failure(self, failure: Optional[str]) -> GateResult:
        ok = failure is None
        return GateResult(
            GATE_NO_API_FAILURE,
            ok,
            "no API failure" if ok else f"API failure: {failure}",
            evidence={"failure": failure},
        )

    def _gate_no_parse_failure(self, failure: Optional[str]) -> GateResult:
        ok = failure is None
        return GateResult(
            GATE_NO_PARSE_FAILURE,
            ok,
            "no parse failure" if ok else f"parse failure: {failure}",
            evidence={"failure": failure},
        )

    def _gate_no_fallback_success(self, fallback: bool) -> GateResult:
        return GateResult(
            GATE_NO_FALLBACK_SUCCESS,
            not fallback,
            "no fallback-derived success" if not fallback else "fallback-derived success detected",
            evidence={"fallback": fallback},
        )

    def _gate_quiet_window(
        self,
        complete: bool,
        observations: list,
        min_monotonic: float,
        first_utc: Optional[str],
        last_utc: Optional[str],
    ) -> GateResult:
        if not complete:
            return GateResult(
                GATE_QUIET_WINDOW_COMPLETE,
                False,
                "strict readiness window not complete",
                evidence={"complete": False},
            )
        # Every observation in the interval must be qualifying
        nonqual_inside = []
        for o in observations:
            if not isinstance(o, dict):
                continue
            if not o.get("qualifying"):
                nonqual_inside.append(o)
        ok = len(nonqual_inside) == 0
        return GateResult(
            GATE_QUIET_WINDOW_COMPLETE,
            ok,
            "quiet window complete with no non-qualifying observation"
            if ok
            else f"quiet window complete but {len(nonqual_inside)} non-qualifying observations inside",
            evidence={
                "complete": complete,
                "first_utc": first_utc,
                "last_utc": last_utc,
                "min_monotonic": min_monotonic,
                "nonqual_inside_count": len(nonqual_inside),
            },
        )

    def _gate_lock_released_after_shutdown(self, released: bool) -> GateResult:
        return GateResult(
            GATE_LOCK_RELEASED_AFTER_SHUTDOWN,
            released,
            "lock released after shutdown" if released else "lock not released",
            evidence={"released": released},
        )

    def _gate_inputs_frozen(self, frozen: bool) -> GateResult:
        return GateResult(
            GATE_INPUTS_FROZEN,
            frozen,
            "inputs frozen" if frozen else "inputs not frozen",
            evidence={"frozen": frozen},
        )
