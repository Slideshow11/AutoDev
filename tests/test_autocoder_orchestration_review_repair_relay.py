"""Tests for the autonomous review/repair relay v1.

The relay is the bounded loop that drives a single PR through
review finding collection, directive construction, and pump-back
into the supervisor until the head is clean. The tests cover
the three pure-data components:

1. ``collect_findings`` — snapshot → normalized findings list.
2. ``build_directive`` — findings → ReviewDirective.
3. ``ReviewDirective`` / ``Finding`` round-tripping.

The loop driver (``run_relay``) is a thin coordinator that calls
into the existing state machine and supervisor primitives; it is
exercised by the integration tests in
``test_autocoder_orchestration_relay_loop.py`` (added in a
follow-up commit).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from autocoder_orchestration.review_repair_relay import (
    ALL_SEVERITIES,
    DEFAULT_MAX_ROUNDS,
    DirectiveContractError,
    DirectiveStore,
    EscalateToHuman,
    Finding,
    InvalidSnapshot,
    RELAY_SCHEMA_VERSION,
    ReviewDirective,
    RelayLoop,
    RelayError,
    RoundDecision,
    RoundTranscript,
    SEVERITY_CI_FAILURE,
    SEVERITY_P0_ESCALATE,
    SEVERITY_P1,
    SEVERITY_P2,
    build_directive,
    build_worker_prompt,
    collect_findings,
    evaluate_round,
    heads_equal,
    relay_state_for_outcome,
)


# === Helpers ===

def _make_snapshot(
    *,
    comments: list | None = None,
    per_provider: dict | None = None,
    required_checks: dict | None = None,
    head_sha: str = "a" * 40,
    head_match: bool = True,
) -> dict:
    return {
        "captured_at": "2026-08-08T00:00:00Z",
        "head_sha": head_sha,
        "head_match": head_match,
        "mergeable": True,
        "formal_reviews": [],
        "review_threads": {},
        "issue_comments": comments or [],
        "required_checks": required_checks or {},
        "providers": {},
        "_provider_issue_comments": per_provider or {},
        "unconsumed_event_ids": [],
    }


def _make_finding(
    *,
    severity: str = SEVERITY_P1,
    body: str = "see path.py:42",
    source: str = "coderabbit",
    file_path: str | None = None,
    line: int | None = None,
) -> Finding:
    return Finding(
        finding_id=f"test:{source}:{body[:8]}",
        source=source,
        severity=severity,
        title=body.splitlines()[0][:120],
        body=body,
        file_path=file_path,
        line=line,
        url=None,
        suggested_test=None,
        review_id=None,
        comment_id=None,
        check_name=None,
    )


# === collect_findings tests ===

class TestCollectFindings:
    def test_empty_snapshot_returns_empty_list(self) -> None:
        snap = _make_snapshot()
        assert collect_findings(snap) == []

    def test_collects_from_provider_issue_comments(self) -> None:
        snap = _make_snapshot(per_provider={
            "coderabbit": [
                {"id": 100, "body": "P1: foo.py:10 looks wrong", "html_url": "x"},
            ],
        })
        findings = collect_findings(snap)
        assert len(findings) == 1
        f = findings[0]
        assert f.source == "coderabbit"
        assert f.severity == SEVERITY_P1
        assert f.finding_id == "coderabbit:100"
        assert f.file_path == "foo.py"
        assert f.line == 10
        assert f.comment_id == 100

    def test_falls_back_to_unfiltered_issue_comments(self) -> None:
        snap = _make_snapshot(comments=[
            {
                "id": 200,
                "user": {"login": "coderabbitai[bot]"},
                "body": "P2 minor: bar.py:5 nit",
            },
        ])
        findings = collect_findings(snap)
        assert len(findings) == 1
        assert findings[0].file_path == "bar.py"
        assert findings[0].severity == SEVERITY_P2

    def test_ignores_non_coderabbit_comments_in_unfiltered_fallback(self) -> None:
        snap = _make_snapshot(comments=[
            {"id": 1, "user": {"login": "alice"}, "body": "P1: a.py:1"},
            {"id": 2, "user": {"login": "coderabbitai[bot]"}, "body": "P2: b.py:1"},
        ])
        findings = collect_findings(snap)
        assert len(findings) == 1
        assert findings[0].finding_id == "coderabbit:2"

    def test_collects_ci_failures(self) -> None:
        snap = _make_snapshot(required_checks={
            "test (3.11)": {"conclusion": "failure", "status": "completed", "run_id": "r1"},
            "validator": {"conclusion": "success", "status": "completed", "run_id": "r2"},
        })
        findings = collect_findings(snap, required_check_names=("test (3.11)", "validator"))
        ci = [f for f in findings if f.severity == SEVERITY_CI_FAILURE]
        assert len(ci) == 1
        assert ci[0].check_name == "test (3.11)"
        assert ci[0].source == "ci"

    def test_ci_finding_emitted_only_for_required_checks(self) -> None:
        snap = _make_snapshot(required_checks={
            "test (3.11)": {"conclusion": "failure", "run_id": "x"},
            "extra": {"conclusion": "failure", "run_id": "y"},
        })
        # When no required_check_names supplied, the collector
        # surfaces both required and non-required failures for
        # visibility.
        findings = collect_findings(snap)
        assert {f.check_name for f in findings} == {
            "test (3.11)",
            "extra",
        }
        # When required_check_names is supplied, exact failures
        # are emitted for the required set, and non-required
        # failures are also surfaced (extra visibility).
        findings = collect_findings(
            snap, required_check_names=("test (3.11)",),
        )
        assert {f.check_name for f in findings} == {"test (3.11)", "extra"}

    def test_in_progress_ci_check_does_not_emit_finding(self) -> None:
        snap = _make_snapshot(required_checks={
            "test (3.11)": {"conclusion": None, "status": "in_progress", "run_id": "x"},
        })
        assert collect_findings(snap) == []

    def test_findings_sorted_severity_p0_first(self) -> None:
        snap = _make_snapshot(per_provider={
            "coderabbit": [
                {"id": 1, "body": "P2 trivial"},
                {"id": 2, "body": "P0 stop"},
                {"id": 3, "body": "P1 important"},
            ],
        })
        findings = collect_findings(snap)
        assert [f.severity for f in findings] == [
            SEVERITY_P0_ESCALATE, SEVERITY_P1, SEVERITY_P2,
        ]

    def test_invalid_snapshot_raises(self) -> None:
        with pytest.raises(InvalidSnapshot):
            collect_findings("not a dict")  # type: ignore[arg-type]

    def test_suggested_test_extracted(self) -> None:
        snap = _make_snapshot(per_provider={
            "coderabbit": [
                {"id": 1, "body": "Add regression test test_foo_returns_bar"},
            ],
        })
        findings = collect_findings(snap)
        assert findings[0].suggested_test == "test_foo_returns_bar"

    def test_p1_priority_high_treated_as_p1(self) -> None:
        snap = _make_snapshot(per_provider={
            "coderabbit": [
                {"id": 1, "body": "priority-high: missing check"},
            ],
        })
        findings = collect_findings(snap)
        assert findings[0].severity == SEVERITY_P1


# === build_directive tests ===

class TestRequiredCheckMissing:
    """If the operator names required checks, they MUST be
    present in the snapshot and successful before the relay
    calls the head clean. Missing or pending required checks
    emit CI_FAILURE findings.
    """

    def test_missing_required_check_is_finding(self) -> None:
        snap = _make_snapshot(
            required_checks={"test (3.11)": {"conclusion": "success"}},
        )
        findings = collect_findings(
            snap, required_check_names=("test (3.11)", "lint"),
        )
        # The lint check is missing -> finding.
        ck_names = {f.check_name for f in findings}
        assert "lint" in ck_names
        # The successful test (3.11) is not a finding.
        assert "test (3.11)" not in ck_names

    def test_pending_required_check_is_finding(self) -> None:
        snap = _make_snapshot(
            required_checks={
                "tests": {"conclusion": "", "status": "in_progress"},
            },
        )
        findings = collect_findings(
            snap, required_check_names=("tests",),
        )
        assert len(findings) == 1
        assert findings[0].check_name == "tests"
        assert "in-progress" in findings[0].body or "pending" in findings[0].body

    def test_successful_required_check_is_passing(self) -> None:
        snap = _make_snapshot(
            required_checks={
                "test (3.11)": {"conclusion": "success"},
                "lint": {"conclusion": "success"},
            },
        )
        findings = collect_findings(
            snap, required_check_names=("test (3.11)", "lint"),
        )
        # All checks are present and successful -> no findings.
        assert findings == []


class TestBuildDirective:
    def test_builds_for_p1_findings(self) -> None:
        findings = [_make_finding(severity=SEVERITY_P1, body="a.py:1 broken")]
        d = build_directive(
            round_index=0,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
            findings=findings,
            coordinator_actor="controller",
        )
        assert d.schema_version == RELAY_SCHEMA_VERSION
        assert d.round_index == 0
        assert d.head_sha == "a" * 40
        assert d.summary == "1 findings: P1=1, P2=0, CI_FAIL=0"
        assert len(d.findings) == 1

    def test_p0_escalates(self) -> None:
        findings = [_make_finding(severity=SEVERITY_P0_ESCALATE, body="P0 critical")]
        with pytest.raises(EscalateToHuman) as exc:
            build_directive(
                round_index=0,
                head_sha="a" * 40,
                repo="owner/repo",
                pr_number=4,
                findings=findings,
                coordinator_actor="controller",
            )
        assert "round 0" in str(exc.value)
        assert "P0" in str(exc.value)

    def test_escalation_keyword_blocks(self) -> None:
        findings = [_make_finding(severity=SEVERITY_P1, body="force push the branch")]
        with pytest.raises(EscalateToHuman) as exc:
            build_directive(
                round_index=2,
                head_sha="a" * 40,
                repo="owner/repo",
                pr_number=4,
                findings=findings,
                coordinator_actor="controller",
            )
        assert "force push" in str(exc.value)

    def test_each_escalation_keyword_blocks(self) -> None:
        for kw in (
            "force push", "rewrite history", "delete branch",
            "disable tests", "skip ci", "merge pr", "close pr",
            "bypass guard", "ignore gate",
        ):
            findings = [_make_finding(severity=SEVERITY_P1, body=f"please {kw} now")]
            with pytest.raises(EscalateToHuman):
                build_directive(
                    round_index=0,
                    head_sha="a" * 40,
                    repo="owner/repo",
                    pr_number=4,
                    findings=findings,
                    coordinator_actor="controller",
                )

    def test_no_findings_rejected(self) -> None:
        with pytest.raises(DirectiveContractError):
            build_directive(
                round_index=0,
                head_sha="a" * 40,
                repo="owner/repo",
                pr_number=4,
                findings=[],
                coordinator_actor="controller",
            )

    def test_invalid_head_sha_rejected(self) -> None:
        with pytest.raises(DirectiveContractError):
            build_directive(
                round_index=0,
                head_sha="not-a-sha",
                repo="owner/repo",
                pr_number=4,
                findings=[_make_finding()],
                coordinator_actor="controller",
            )

    def test_directive_sha256_is_deterministic(self) -> None:
        import dataclasses
        kwargs = dict(
            round_index=0,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
            findings=[_make_finding(body="same body")],
            coordinator_actor="controller",
        )
        d1 = build_directive(**kwargs)
        d2 = build_directive(**kwargs)
        # directive_id is a uuid so we cannot compare full dicts
        # for equality; the SHA-256 must be deterministic as long
        # as the directive_id is fixed.
        d1 = dataclasses.replace(d1, directive_id="fixed")
        d2 = dataclasses.replace(d2, directive_id="fixed")
        assert d1.compute_sha256() == d2.compute_sha256()


# === ReviewDirective round-trip ===

class TestReviewDirective:
    def test_to_from_dict_roundtrip(self) -> None:
        findings = (
            _make_finding(severity=SEVERITY_P1, body="a.py:1"),
            _make_finding(severity=SEVERITY_CI_FAILURE, body="ci broken"),
        )
        d = build_directive(
            round_index=3,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
            findings=list(findings),
            coordinator_actor="controller",
        )
        d2 = ReviewDirective.from_dict(d.to_dict())
        assert d2.head_sha == d.head_sha
        assert d2.round_index == d.round_index
        assert d2.repo == d.repo
        assert d2.pr_number == d.pr_number
        assert len(d2.findings) == 2
        assert d2.findings[0].severity == SEVERITY_P1
        assert d2.findings[1].severity == SEVERITY_CI_FAILURE

    def test_unknown_severity_rejected(self) -> None:
        with pytest.raises(DirectiveContractError):
            build_directive(
                round_index=0,
                head_sha="a" * 40,
                repo="owner/repo",
                pr_number=4,
                findings=[_make_finding(severity="P99")],
                coordinator_actor="controller",
            )

    def test_round_index_negative_rejected(self) -> None:
        with pytest.raises(DirectiveContractError):
            build_directive(
                round_index=-1,
                head_sha="a" * 40,
                repo="owner/repo",
                pr_number=4,
                findings=[_make_finding()],
                coordinator_actor="controller",
            )


# === RoundTranscript round-trip ===

class TestRoundTranscript:
    def test_to_from_dict_roundtrip(self) -> None:
        ts = RoundTranscript(
            schema_version=RELAY_SCHEMA_VERSION,
            round_index=2,
            head_sha_before="a" * 40,
            head_sha_after="b" * 40,
            directive_id="d1",
            started_at="2026-08-08T00:00:00Z",
            ended_at="2026-08-08T00:01:00Z",
            outcome="completed",
            p1_count=2,
            p2_count=1,
            ci_failure_count=1,
            escalate_reasons=("none",),
        )
        ts2 = RoundTranscript.from_dict(ts.to_dict())
        assert ts2.round_index == 2
        assert ts2.outcome == "completed"
        assert ts2.escalate_reasons == ("none",)

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(DirectiveContractError):
            RoundTranscript.from_dict({"round_index": 1})


# === DirectiveStore ===

class TestDirectiveStore:
    def test_write_then_read_directive(self, tmp_path: Path) -> None:
        from autocoder_orchestration.store import StateStore
        state_root = tmp_path / "state"
        evidence_root = tmp_path / "evidence"
        state_root.mkdir()
        evidence_root.mkdir()
        store = StateStore(str(state_root))
        ds = DirectiveStore(store, str(evidence_root))
        findings = [_make_finding(body="a.py:1")]
        d = build_directive(
            round_index=0,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
            findings=findings,
            coordinator_actor="controller",
        )
        digest = ds.write_directive(d)
        assert len(digest) == 64
        d2 = ds.read_directive()
        assert d2 is not None
        assert d2.head_sha == d.head_sha
        assert d2.findings[0].body == "a.py:1"

    def test_append_transcript_round_count(self, tmp_path: Path) -> None:
        from autocoder_orchestration.store import StateStore
        state_root = tmp_path / "state"
        evidence_root = tmp_path / "evidence"
        state_root.mkdir()
        evidence_root.mkdir()
        store = StateStore(str(state_root))
        ds = DirectiveStore(store, str(evidence_root))
        for i in range(3):
            ds.append_transcript(RoundTranscript(
                schema_version=RELAY_SCHEMA_VERSION,
                round_index=i,
                head_sha_before="a" * 40,
                head_sha_after="b" * 40,
                directive_id=f"d{i}",
                started_at="2026-08-08T00:00:00Z",
                ended_at="2026-08-08T00:01:00Z",
                outcome="completed",
                p1_count=0,
                p2_count=0,
                ci_failure_count=0,
                escalate_reasons=(),
            ))
        assert ds.last_round_index() == 2
        # All entries are valid RoundTranscripts.
        entries = ds.read_transcript()
        assert len(entries) == 3
        assert [e.round_index for e in entries] == [0, 1, 2]


# === heads_equal ===

class TestHeadsEqual:
    def test_equal(self) -> None:
        assert heads_equal("a" * 40, "a" * 40)
        assert heads_equal("A" * 40, "a" * 40)

    def test_not_equal(self) -> None:
        assert not heads_equal("a" * 40, "b" * 40)

    def test_none(self) -> None:
        assert not heads_equal(None, "a" * 40)
        assert not heads_equal("a" * 40, None)
        assert not heads_equal(None, None)


# === relay_state_for_outcome ===

class TestRelayStateForOutcome:
    def test_completed_returns_awaiting_ci(self) -> None:
        state, actor = relay_state_for_outcome("completed")
        assert state == "AWAITING_CI"
        assert actor == "implementation_worker"

    def test_ready_returns_qualifying_readiness(self) -> None:
        state, actor = relay_state_for_outcome("ready")
        assert state == "QUALIFYING_READINESS"
        assert actor == "controller"

    def test_unknown_returns_repairing(self) -> None:
        state, actor = relay_state_for_outcome("unknown")
        assert state == "REPAIRING_REVIEW_FINDINGS"
        assert actor == "controller"


# === Constants ===

class TestConstants:
    def test_severities_are_documented(self) -> None:
        assert SEVERITY_P0_ESCALATE in ALL_SEVERITIES
        assert SEVERITY_P1 in ALL_SEVERITIES
        assert SEVERITY_P2 in ALL_SEVERITIES
        assert SEVERITY_CI_FAILURE in ALL_SEVERITIES

    def test_max_rounds_is_bounded(self) -> None:
        # The default ceiling must be a small positive integer; the
        # operator can lower it but never raise it (the lower bound
        # is the lease-timeout, not the round count).
        assert 1 <= DEFAULT_MAX_ROUNDS <= 50


# === evaluate_round tests ===

class TestSnapshotHeadBinding:
    """The relay refuses to act on a snapshot whose head_sha
    does not match the requested head. This is the
    exact-head premise: review evidence from a previous
    head must NEVER drive a directive for the current
    head.
    """

    def test_snapshot_head_mismatch_raises(self) -> None:
        snap = _make_snapshot(
            head_sha="a" * 40, head_match=False,
        )
        with pytest.raises(InvalidSnapshot):
            evaluate_round(
                snapshot=snap,
                head_sha="b" * 40,
                repo="owner/repo",
                pr_number=4,
                round_index=0,
            )

    def test_snapshot_head_match_false_raises(self) -> None:
        # Snapshot's head_sha matches but head_match is False;
        # the relay must still refuse.
        snap = _make_snapshot(
            head_sha="a" * 40, head_match=False,
        )
        with pytest.raises(InvalidSnapshot):
            evaluate_round(
                snapshot=snap,
                head_sha="a" * 40,
                repo="owner/repo",
                pr_number=4,
                round_index=0,
            )

    def test_snapshot_head_match_true_passes(self) -> None:
        snap = _make_snapshot(
            head_sha="a" * 40, head_match=True,
        )
        # No findings -> enter_qualifying_readiness.
        d = evaluate_round(
            snapshot=snap,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
            round_index=0,
        )
        assert d.action == "enter_qualifying_readiness"


class TestEvaluateRound:
    def test_empty_snapshot_returns_qualifying_action(self) -> None:
        d = evaluate_round(
            snapshot=_make_snapshot(),
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
            round_index=0,
        )
        assert d.action == "enter_qualifying_readiness"
        assert d.outcome == "ready"
        assert d.directive is None
        assert d.directive_digest is None

    def test_p1_findings_returns_launch_worker(self) -> None:
        snap = _make_snapshot(per_provider={
            "coderabbit": [{"id": 1, "body": "P1: foo.py:10 broken"}],
        })
        d = evaluate_round(
            snapshot=snap,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
            round_index=0,
        )
        assert d.action == "launch_worker"
        assert d.outcome == "completed"
        assert d.directive is not None
        assert d.directive.head_sha == "a" * 40
        assert d.p1_count == 1
        assert d.p2_count == 0

    def test_p0_escalates_with_reasons(self) -> None:
        snap = _make_snapshot(per_provider={
            "coderabbit": [{"id": 1, "body": "P0 critical: stops the run"}],
        })
        d = evaluate_round(
            snapshot=snap,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
            round_index=0,
        )
        assert d.action == "escalate_to_human"
        assert d.outcome == "escalated"
        assert d.directive is None
        assert len(d.escalate_reasons) == 1
        assert "P0" in d.escalate_reasons[0]

    def test_escalation_keyword_escalates(self) -> None:
        snap = _make_snapshot(per_provider={
            "coderabbit": [{"id": 1, "body": "please force push now"}],
        })
        d = evaluate_round(
            snapshot=snap,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
            round_index=0,
        )
        assert d.action == "escalate_to_human"
        assert "force push" in d.escalate_reasons[0]

    def test_invalid_head_sha_raises(self) -> None:
        with pytest.raises(DirectiveContractError):
            evaluate_round(
                snapshot=_make_snapshot(),
                head_sha="not-a-sha",
                repo="owner/repo",
                pr_number=4,
                round_index=0,
            )

    def test_invalid_snapshot_raises(self) -> None:
        with pytest.raises(InvalidSnapshot):
            evaluate_round(
                snapshot="not a dict",  # type: ignore[arg-type]
                head_sha="a" * 40,
                repo="owner/repo",
                pr_number=4,
                round_index=0,
            )

    def test_directive_store_persists_directive(self, tmp_path: Path) -> None:
        from autocoder_orchestration.store import StateStore
        state_root = tmp_path / "state"
        evidence_root = tmp_path / "evidence"
        state_root.mkdir()
        evidence_root.mkdir()
        store = StateStore(str(state_root))
        ds = DirectiveStore(store, str(evidence_root))
        snap = _make_snapshot(per_provider={
            "coderabbit": [{"id": 1, "body": "P1: foo.py:1 broken"}],
        })
        d = evaluate_round(
            snapshot=snap,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
            round_index=0,
            directive_store=ds,
        )
        assert d.directive_digest is not None
        assert len(d.directive_digest) == 64
        round_tripped = ds.read_directive()
        assert round_tripped is not None
        assert round_tripped.directive_id == d.directive.directive_id  # type: ignore[union-attr]

    def test_ci_finding_drives_action(self) -> None:
        snap = _make_snapshot(required_checks={
            "test (3.11)": {"conclusion": "failure", "run_id": "r"},
        })
        d = evaluate_round(
            snapshot=snap,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
            round_index=0,
            required_check_names=("test (3.11)",),
        )
        assert d.action == "launch_worker"
        assert d.ci_failure_count == 1
        assert any(
            f.severity == SEVERITY_CI_FAILURE for f in d.directive.findings  # type: ignore[union-attr]
        )


# === build_worker_prompt tests ===

class TestBuildWorkerPrompt:
    def test_prompt_contains_directive_id_and_sha(self) -> None:
        snap = _make_snapshot(per_provider={
            "coderabbit": [{"id": 1, "body": "P1: foo.py:1 broken"}],
        })
        d = evaluate_round(
            snapshot=snap,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
            round_index=2,
        )
        prompt = build_worker_prompt(d)
        assert d.directive is not None
        assert d.directive.directive_id in prompt
        assert d.directive.compute_sha256() in prompt
        assert "round 2" in prompt
        assert "PR 4" in prompt
        assert "owner/repo" in prompt
        # The directive JSON is embedded verbatim (pretty-printed).
        directive_json = json.dumps(d.directive.to_dict(), indent=2, sort_keys=True)
        assert directive_json in prompt

    def test_prompt_deterministic_for_same_decision(self) -> None:
        import dataclasses
        snap = _make_snapshot(per_provider={
            "coderabbit": [{"id": 1, "body": "P1: foo.py:1 broken"}],
        })
        d = evaluate_round(
            snapshot=snap,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
            round_index=3,
        )
        d_pinned = dataclasses.replace(
            d,
            directive=dataclasses.replace(d.directive, directive_id="fixed"),  # type: ignore[arg-type]
        )
        assert build_worker_prompt(d_pinned) == build_worker_prompt(d_pinned)

    def test_prompt_requires_directive(self) -> None:
        d = RoundDecision(
            action="enter_qualifying_readiness",
            round_index=0,
            head_sha="a" * 40,
            outcome="ready",
            p1_count=0,
            p2_count=0,
            ci_failure_count=0,
            escalate_reasons=(),
            directive=None,
            directive_digest=None,
        )
        with pytest.raises(DirectiveContractError):
            build_worker_prompt(d)


# === RelayLoop tests ===

class TestRelayLoop:
    def _setup(self, tmp_path: Path):
        """Create a minimal state store + controller + relay loop."""
        from autocoder_orchestration.store import StateStore
        from autocoder_orchestration.context import make_run_context
        from autocoder_orchestration.controller import Controller
        from autocoder_orchestration.state_machine import (
            StateMachine,
            STATE_IMPLEMENTING,
            STATE_AWAITING_CI,
            STATE_REPAIRING_REVIEW_FINDINGS,
        )
        from autocoder_orchestration.context import ACTOR_CONTROLLER, ACTOR_IMPL_WORKER

        state_root = tmp_path / "state"
        evidence_root = tmp_path / "evidence"
        state_root.mkdir()
        evidence_root.mkdir()
        store = StateStore(str(state_root))
        ctx = make_run_context(
            repo_owner="owner",
            repo_name="repo",
            local_checkout=str(tmp_path),
            base_branch="main",
            authorized_base_sha="a" * 64,
            feature_branch="feat/test",
            task_specification_path="/tmp/task",
            task_specification_sha256="b" * 64,
            required_ci_jobs=[],
            implementation_worker_command=[],
            evidence_root=str(evidence_root),
            state_root=str(state_root),
            pr_number=4,
            current_authorized_head="a" * 40,
        )
        store.write_atomic("run_context.json", ctx.to_dict())
        sm = StateMachine()
        store.write_atomic("state.json", sm.to_dict())
        sm = sm.transition(STATE_IMPLEMENTING, ACTOR_CONTROLLER)
        store.write_atomic("state.json", sm.to_dict())
        sm = sm.transition(STATE_AWAITING_CI, ACTOR_IMPL_WORKER)
        store.write_atomic("state.json", sm.to_dict())
        sm = sm.transition(STATE_REPAIRING_REVIEW_FINDINGS, ACTOR_CONTROLLER)
        store.write_atomic("state.json", sm.to_dict())
        controller = Controller(ctx, store)
        ds = DirectiveStore(store, str(evidence_root))
        return ctx, store, controller, ds

    def test_run_once_launch_worker(self, tmp_path: Path) -> None:
        ctx, store, controller, ds = self._setup(tmp_path)
        loop = RelayLoop(
            context=ctx, store=store,
            directive_store=ds, controller=controller,
        )
        snap = _make_snapshot(per_provider={
            "coderabbit": [{"id": 1, "body": "P1: foo.py:1 broken"}],
        })
        decision = loop.run_once(
            snap, head_sha="a" * 40, repo="owner/repo", pr_number=4,
        )
        assert decision.action == "launch_worker"
        assert ds.last_round_index() == 0

    def test_run_once_enter_qualifying_when_clean(self, tmp_path: Path) -> None:
        ctx, store, controller, ds = self._setup(tmp_path)
        loop = RelayLoop(
            context=ctx, store=store,
            directive_store=ds, controller=controller,
        )
        decision = loop.run_once(
            _make_snapshot(),
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
        )
        assert decision.action == "enter_qualifying_readiness"
        assert ds.last_round_index() == 0

    def test_run_once_escalates_to_blocked(self, tmp_path: Path) -> None:
        ctx, store, controller, ds = self._setup(tmp_path)
        loop = RelayLoop(
            context=ctx, store=store,
            directive_store=ds, controller=controller,
        )
        snap = _make_snapshot(per_provider={
            "coderabbit": [{"id": 1, "body": "P0 critical"}],
        })
        decision = loop.run_once(
            snap, head_sha="a" * 40, repo="owner/repo", pr_number=4,
        )
        assert decision.action == "escalate_to_human"
        sm = controller.load_state_machine()
        assert sm is not None
        assert sm.current_state == "BLOCKED"

    def test_run_once_refuses_wrong_state(self, tmp_path: Path) -> None:
        from autocoder_orchestration.store import StateStore
        from autocoder_orchestration.context import make_run_context
        from autocoder_orchestration.controller import Controller
        from autocoder_orchestration.state_machine import (
            StateMachine,
            STATE_PLANNED,
        )

        state_root = tmp_path / "state"
        evidence_root = tmp_path / "evidence"
        state_root.mkdir()
        evidence_root.mkdir()
        store = StateStore(str(state_root))
        ctx = make_run_context(
            repo_owner="owner",
            repo_name="repo",
            local_checkout=str(tmp_path),
            base_branch="main",
            authorized_base_sha="a" * 64,
            feature_branch="feat/test",
            task_specification_path="/tmp/task",
            task_specification_sha256="b" * 64,
            required_ci_jobs=[],
            implementation_worker_command=[],
            evidence_root=str(evidence_root),
            state_root=str(state_root),
            pr_number=4,
            current_authorized_head="a" * 40,
        )
        store.write_atomic("run_context.json", ctx.to_dict())
        sm = StateMachine()
        store.write_atomic("state.json", sm.to_dict())
        controller = Controller(ctx, store)
        ds = DirectiveStore(store, str(evidence_root))
        loop = RelayLoop(
            context=ctx, store=store,
            directive_store=ds, controller=controller,
        )
        with pytest.raises(RelayError):
            loop.run_once(
                _make_snapshot(), head_sha="a" * 40,
                repo="owner/repo", pr_number=4,
            )

    def test_run_once_blocks_after_max_rounds(self, tmp_path: Path) -> None:
        from autocoder_orchestration.store import StateStore
        from autocoder_orchestration.context import make_run_context
        from autocoder_orchestration.controller import Controller
        from autocoder_orchestration.state_machine import (
            StateMachine,
            STATE_IMPLEMENTING, STATE_AWAITING_CI,
            STATE_REPAIRING_REVIEW_FINDINGS,
        )
        from autocoder_orchestration.context import ACTOR_CONTROLLER, ACTOR_IMPL_WORKER

        state_root = tmp_path / "state"
        evidence_root = tmp_path / "evidence"
        state_root.mkdir()
        evidence_root.mkdir()
        store = StateStore(str(state_root))
        ctx = make_run_context(
            repo_owner="owner",
            repo_name="repo",
            local_checkout=str(tmp_path),
            base_branch="main",
            authorized_base_sha="a" * 64,
            feature_branch="feat/test",
            task_specification_path="/tmp/task",
            task_specification_sha256="b" * 64,
            required_ci_jobs=[],
            implementation_worker_command=[],
            evidence_root=str(evidence_root),
            state_root=str(state_root),
            pr_number=4,
            current_authorized_head="a" * 40,
        )
        store.write_atomic("run_context.json", ctx.to_dict())
        sm = StateMachine()
        sm = sm.transition(STATE_IMPLEMENTING, ACTOR_CONTROLLER)
        sm = sm.transition(STATE_AWAITING_CI, ACTOR_IMPL_WORKER)
        sm = sm.transition(STATE_REPAIRING_REVIEW_FINDINGS, ACTOR_CONTROLLER)
        store.write_atomic("state.json", sm.to_dict())
        controller = Controller(ctx, store)
        ds = DirectiveStore(store, str(evidence_root))
        # Seed 3 transcript entries so the next round_index == max_rounds.
        for i in range(3):
            ds.append_transcript(RoundTranscript(
                schema_version=RELAY_SCHEMA_VERSION,
                round_index=i,
                head_sha_before="a" * 40,
                head_sha_after="a" * 40,
                directive_id=f"d{i}",
                started_at="2026-08-08T00:00:00Z",
                ended_at="2026-08-08T00:01:00Z",
                outcome="completed",
                p1_count=1,
                p2_count=0,
                ci_failure_count=0,
                escalate_reasons=(),
            ))
        loop = RelayLoop(
            context=ctx, store=store,
            directive_store=ds, controller=controller,
            max_rounds=3,
        )
        with pytest.raises(EscalateToHuman):
            loop.run_once(
                _make_snapshot(),
                head_sha="a" * 40,
                repo="owner/repo",
                pr_number=4,
            )
        sm = controller.load_state_machine()
        assert sm is not None
        assert sm.current_state == "BLOCKED"

    def test_head_clean_helper(self, tmp_path: Path) -> None:
        from autocoder_orchestration.store import StateStore
        from autocoder_orchestration.context import make_run_context
        from autocoder_orchestration.controller import Controller
        state_root = tmp_path / "state"
        evidence_root = tmp_path / "evidence"
        state_root.mkdir()
        evidence_root.mkdir()
        store = StateStore(str(state_root))
        ctx = make_run_context(
            repo_owner="owner",
            repo_name="repo",
            local_checkout=str(tmp_path),
            base_branch="main",
            authorized_base_sha="a" * 64,
            feature_branch="feat/test",
            task_specification_path="/tmp/task",
            task_specification_sha256="b" * 64,
            required_ci_jobs=[],
            implementation_worker_command=[],
            evidence_root=str(evidence_root),
            state_root=str(state_root),
            pr_number=4,
            current_authorized_head="a" * 40,
        )
        controller = Controller(ctx, store)
        ds = DirectiveStore(store, str(evidence_root))
        loop = RelayLoop(
            context=ctx, store=store,
            directive_store=ds, controller=controller,
        )
        assert loop.head_clean(_make_snapshot())
        assert not loop.head_clean(_make_snapshot(per_provider={
            "coderabbit": [{"id": 1, "body": "P1: foo.py:1"}],
        }))



# === Round-2 escalation-path tests ===

class TestRunUntilHeadAdvances:
    """The relay's persistent loop driver.

    The supervisor's event loop calls
    ``loop.run_until_head_advances`` to drive the relay through
    as many rounds as the head advances. The loop halts only
    on protected-authority escalation or when the head is
    clean.
    """

    def _setup(self, tmp_path: Path):
        from autocoder_orchestration.store import StateStore
        from autocoder_orchestration.context import make_run_context
        from autocoder_orchestration.controller import Controller
        from autocoder_orchestration.state_machine import (
            StateMachine,
            STATE_IMPLEMENTING,
            STATE_AWAITING_CI,
            STATE_REPAIRING_REVIEW_FINDINGS,
        )
        from autocoder_orchestration.context import (
            ACTOR_CONTROLLER, ACTOR_IMPL_WORKER,
        )

        state_root = tmp_path / "state"
        evidence_root = tmp_path / "evidence"
        state_root.mkdir()
        evidence_root.mkdir()
        store = StateStore(str(state_root))
        ctx = make_run_context(
            repo_owner="owner",
            repo_name="repo",
            local_checkout=str(tmp_path),
            base_branch="main",
            authorized_base_sha="a" * 64,
            feature_branch="feat/test",
            task_specification_path="/tmp/task",
            task_specification_sha256="b" * 64,
            required_ci_jobs=[],
            implementation_worker_command=[],
            evidence_root=str(evidence_root),
            state_root=str(state_root),
            pr_number=4,
            current_authorized_head="a" * 40,
        )
        store.write_atomic("run_context.json", ctx.to_dict())
        sm = StateMachine()
        sm = sm.transition(STATE_IMPLEMENTING, ACTOR_CONTROLLER)
        sm = sm.transition(STATE_AWAITING_CI, ACTOR_IMPL_WORKER)
        sm = sm.transition(STATE_REPAIRING_REVIEW_FINDINGS, ACTOR_CONTROLLER)
        store.write_atomic("state.json", sm.to_dict())
        controller = Controller(ctx, store)
        ds = DirectiveStore(store, str(evidence_root))
        return ctx, store, controller, ds

    def test_escalate_raises_escalate_to_human(self, tmp_path: Path) -> None:
        """A P0 round must raise EscalateToHuman, not a
        non-exception (RoundDecision is not a BaseException).
        """
        from autocoder_orchestration.review_repair_relay import (
            EscalateToHuman,
        )
        ctx, store, controller, ds = self._setup(tmp_path)
        loop = RelayLoop(
            context=ctx, store=store,
            directive_store=ds, controller=controller,
        )
        snap = _make_snapshot(per_provider={
            "coderabbit": [{"id": 1, "body": "P0 critical: stop the run"}],
        })
        # Wire the loop's _await_head_advance to mock a
        # worker that pushes a new head.
        calls = {"count": 0}
        def mock_provider(head: str) -> dict:
            calls["count"] += 1
            return snap
        def mock_advance(head_sha: str) -> Optional[str]:
            # The first call returns None (no advance yet),
            # then we return a new head to continue the loop.
            if calls["count"] < 2:
                return None
            return None
        with pytest.raises(EscalateToHuman):
            loop.run_until_head_advances(
                mock_provider, head_sha="a" * 40,
                repo="owner/repo", pr_number=4,
                on_action=lambda d: None,
            )

    def test_enter_qualifying_returns_when_head_clean(self, tmp_path: Path) -> None:
        """A clean head returns the decision without
        raising.
        """
        ctx, store, controller, ds = self._setup(tmp_path)
        loop = RelayLoop(
            context=ctx, store=store,
            directive_store=ds, controller=controller,
        )
        snap = _make_snapshot()  # clean
        decision = loop.run_until_head_advances(
            lambda head: snap,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
            on_action=lambda d: None,
        )
        assert decision.action == "enter_qualifying_readiness"

    def test_safety_net_blocks_after_consecutive_rounds(self, tmp_path: Path) -> None:
        """The max_rounds safety net triggers when the same
        head produces N consecutive rounds without
        advancement.
        """
        from autocoder_orchestration.review_repair_relay import (
            EscalateToHuman,
        )
        ctx, store, controller, ds = self._setup(tmp_path)
        loop = RelayLoop(
            context=ctx, store=store,
            directive_store=ds, controller=controller,
            max_rounds=2,
        )
        # Seed 2 transcript entries with the same head and
        # completed outcome.
        for _ in range(2):
            ds.append_transcript(RoundTranscript(
                schema_version=RELAY_SCHEMA_VERSION,
                round_index=0,
                head_sha_before="a" * 40,
                head_sha_after="a" * 40,
                directive_id="d",
                started_at="2026-08-08T00:00:00Z",
                ended_at="2026-08-08T00:01:00Z",
                outcome="completed",
                p1_count=1,
                p2_count=0,
                ci_failure_count=0,
                escalate_reasons=(),
            ))
        snap = _make_snapshot(per_provider={
            "coderabbit": [{"id": 1, "body": "P1: foo.py:1"}],
        })
        with pytest.raises(EscalateToHuman):
            loop.run_once(
                snap, head_sha="a" * 40,
                repo="owner/repo", pr_number=4,
            )


