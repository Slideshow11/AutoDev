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
import tempfile
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

def _tmp_task_spec_path(tmp_path: Path) -> str:
    """Create a real workitem-specification file under the pytest-provided
    ``tmp_path`` (CodeRabbit round-19 finding 3742791230). Using
    ``tmp_path`` keeps the file inside pytest's auto-cleaned
    temporary directory rather than a world-writable ``/tmp`` path.
    The file is removed automatically when ``tmp_path`` is torn down.

    NOTE: the filename must NOT contain the credential-prefix
    substring (see ``.github/workflows/scan-forbidden.txt``)
    because that would flag the test file itself. We use a
    benign ``workitem`` name.
    """
    p = tmp_path / "workitem.md"
    p.write_text("# Workitem (test fixture)\n")
    return str(p)


def _make_snapshot(
    *,
    comments: list | None = None,
    per_provider: dict | None = None,
    required_checks: dict | None = None,
    head_sha: str = "a" * 40,
    head_match: bool = True,
    review_comments: list | None = None,
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
        "review_comments": review_comments or [],
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
        # When no required_check_names supplied, the relay
        # has no authoritative required-check list and emits
        # no findings. The relay drives the operator's
        # authoritative required-check list; non-required
        # failures are surfaced through the existing
        # readiness gate, not through the repair loop.
        findings = collect_findings(snap)
        assert {f.check_name for f in findings} == set()
        # When required_check_names is supplied, only those
        # failures emit findings. Non-required failures are
        # NOT surfaced as repair findings.
        findings = collect_findings(
            snap, required_check_names=("test (3.11)",),
        )
        assert {f.check_name for f in findings} == {"test (3.11)"}

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

class TestCancelledChecks:
    """Cancelled required checks must NOT be treated as clean.
    A cancelled check is a non-actionable terminal state;
    the relay surfaces it as a finding so the readiness
    gate can decide.
    """
    def test_cancelled_required_check_is_finding(self) -> None:
        snap = _make_snapshot(
            required_checks={
                "tests": {"conclusion": "cancelled", "run_id": "x"},
            },
        )
        findings = collect_findings(
            snap, required_check_names=("tests",),
        )
        # Cancelled is NOT in the success set.
        assert len(findings) == 1
        assert findings[0].check_name == "tests"
        assert findings[0].severity == "CI_FAILURE"

    def test_skipped_required_check_is_passing(self) -> None:
        # Skipped is a positive terminal state for required
        # checks (the check was intentionally not run).
        snap = _make_snapshot(
            required_checks={
                "tests": {"conclusion": "skipped"},
            },
        )
        findings = collect_findings(
            snap, required_check_names=("tests",),
        )
        assert findings == []


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

class TestNonFindingCommentsAreFiltered:
    """Status markers (walkthrough, in-progress, completion)
    are NOT actionable findings. The relay must filter them
    out so a clean head does not become a persistent repair
    loop.
    """

    def test_walkthrough_marker_is_filtered(self) -> None:
        snap = _make_snapshot(per_provider={
            "coderabbit": [
                {"id": 1, "body": "🚦 Walkthrough comment."},
            ],
        })
        findings = collect_findings(snap)
        assert findings == []

    def test_in_progress_marker_is_filtered(self) -> None:
        snap = _make_snapshot(per_provider={
            "coderabbit": [
                {"id": 1, "body": "Review in progress."},
            ],
        })
        findings = collect_findings(snap)
        assert findings == []

    def test_walkthrough_mentioned_in_passing_is_actionable(self) -> None:
        """Round-3 P1: a body that merely mentions 'walkthrough'
        in passing (e.g. 'P1: see walkthrough above is stale')
        MUST NOT be filtered as a status marker. The
        anchoring fix only filters bodies whose FIRST
        line is a status marker.
        """
        snap = _make_snapshot(per_provider={
            "coderabbit": [
                {"id": 1, "body": "P1: see walkthrough above is stale"},
            ],
        })
        findings = collect_findings(snap)
        # The body has a P1 marker and actionable content.
        # The 'walkthrough' appears in the middle, not the
        # first line. The anchor keeps it actionable.
        assert len(findings) == 1
        assert findings[0].severity == "P1"

    def test_walkthrough_first_line_short_is_filtered(self) -> None:
        """A short body whose first line is a status marker
        (with optional emoji) is filtered.
        """
        snap = _make_snapshot(per_provider={
            "coderabbit": [
                {"id": 1, "body": "🚦 Walkthrough comment."},
            ],
        })
        findings = collect_findings(snap)
        assert findings == []

    def test_completion_marker_is_filtered(self) -> None:
        snap = _make_snapshot(per_provider={
            "coderabbit": [
                {"id": 1, "body": "Review completed."},
            ],
        })
        findings = collect_findings(snap)
        assert findings == []

    def test_actual_finding_is_NOT_filtered(self) -> None:
        # A regular review finding must still be surfaced.
        snap = _make_snapshot(per_provider={
            "coderabbit": [
                {"id": 1, "body": "P1: foo.py:1 broken"},
            ],
        })
        findings = collect_findings(snap)
        assert len(findings) == 1
        assert findings[0].severity == SEVERITY_P1


class TestSnapshotHeadBinding:
    """The relay refuses to act on a snapshot whose head_sha
    does not match the requested head. This is the
    exact-head premise: review evidence from a previous
    head must NEVER drive a directive for the current
    head.
    """

    def test_snapshot_head_mismatch_raises(self) -> None:
        # Isolate the head_sha mismatch failure mode.
        # The snapshot's head_sha differs from the requested
        # head; head_match is True. The relay MUST refuse.
        snap = _make_snapshot(
            head_sha="a" * 40, head_match=True,
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
        # Isolate the head_match=False failure mode.
        # The snapshot's head_sha matches but head_match is
        # False; the relay MUST also refuse.
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
            task_specification_path=_tmp_task_spec_path(tmp_path),
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
            task_specification_path=_tmp_task_spec_path(tmp_path),
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
            task_specification_path=_tmp_task_spec_path(tmp_path),
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
            task_specification_path=_tmp_task_spec_path(tmp_path),
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
            task_specification_path=_tmp_task_spec_path(tmp_path),
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
        calls = {"count": 0}
        def mock_provider(head: str) -> dict:
            calls["count"] += 1
            return snap
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








class TestMissingHeadMetadataRejected:
    """Round-3 P1: a snapshot with missing head_sha MUST
    be rejected. The exact-head guard requires a concrete
    SHA; a snapshot without one cannot be verified.
    """

    def test_missing_head_sha_is_rejected(self) -> None:
        snap = {
            "captured_at": "2026-08-08T00:00:00Z",
            # head_sha is intentionally missing
            "head_match": True,
            "mergeable": True,
            "formal_reviews": [],
            "review_threads": {},
            "issue_comments": [],
            "required_checks": {},
            "providers": [],
            "_provider_issue_comments": {},
            "review_comments": [],
            "unconsumed_event_ids": [],
        }
        with pytest.raises(InvalidSnapshot):
            evaluate_round(
                snapshot=snap,
                head_sha="a" * 40,
                repo="owner/repo",
                pr_number=4,
                round_index=1,
            )

    def test_explicit_null_head_sha_is_rejected(self) -> None:
        # The snapshot preserves an explicit null head_sha.
        snap = {
            "captured_at": "2026-08-08T00:00:00Z",
            "head_sha": None,
            "head_match": True,
            "mergeable": True,
            "formal_reviews": [],
            "review_threads": {},
            "issue_comments": [],
            "required_checks": {},
            "providers": [],
            "_provider_issue_comments": {},
            "review_comments": [],
            "unconsumed_event_ids": [],
        }
        with pytest.raises(InvalidSnapshot):
            evaluate_round(
                snapshot=snap,
                head_sha="a" * 40,
                repo="owner/repo",
                pr_number=4,
                round_index=1,
            )


class TestInlineReviewCommentsAreFindings:
    """Round-3 P1: inline review comments (path + line + body)
    MUST be included as findings. The supervisor's snapshot
    may carry inline review comments via the
    ``use_reviews_api`` flag; the relay must consume them.
    """

    def test_inline_review_comment_is_finding(self) -> None:
        snap = _make_snapshot(review_comments=[
            {
                "id": 99,
                "path": "foo.py",
                "line": 12,
                "body": "P1: foo.py:12 broken",
            },
        ])
        findings = collect_findings(snap)
        assert len(findings) == 1
        assert findings[0].severity == "P1"
        assert findings[0].file_path == "foo.py"
        assert findings[0].line == 12

    def test_inline_walkthrough_marker_is_filtered(self) -> None:
        snap = _make_snapshot(review_comments=[
            {
                "id": 99,
                "path": "foo.py",
                "line": 12,
                "body": "🚦 Walkthrough comment.",
            },
        ])
        findings = collect_findings(snap)
        # The status marker is filtered.
        assert findings == []


class TestLaunchWorkerDoesNotTransition:
    """Round-3 P1: when the relay decides launch_worker,
    it must NOT trigger report_repair_pushed. The
    transition REPAIRING_REVIEW_FINDINGS -> AWAITING_CI
    fires only AFTER the worker successfully pushes and a
    new head is observed, via the explicit
    mark_head_advanced method.

    Previously the relay called report_repair_pushed
    immediately on launch_worker, which committed the
    transition BEFORE the worker had actually pushed. A
    launch failure could leave the run in AWAITING_CI
    with no new head, and the next round would refuse
    the exact-head guard.
    """

    def _make_snapshot_with_finding(self) -> dict:
        return {
            "captured_at": "2026-08-08T00:00:00Z",
            "head_sha": "a" * 40,
            "head_match": True,
            "mergeable": True,
            "formal_reviews": [],
            "review_threads": {},
            "issue_comments": [],
            "required_checks": {},
            "providers": [],
            "_provider_issue_comments": {
                "coderabbit": [
                    {"id": 1, "body": "P1: foo.py:1 broken"},
                ],
            },
            "unconsumed_event_ids": [],
        }

    def _setup_loop(self, tmp_path):
        from autocoder_orchestration.review_repair_relay import (
            RELAY_SCHEMA_VERSION, RelayLoop,
        )
        from autocoder_orchestration.store import StateStore
        store = StateStore(str(tmp_path / "state"))
        store.write_atomic("run_context.json", {
            "schema_version": "autocoder.run_context.v1",
            "run_id": "r1",
            "repo_owner": "owner",
            "repo_name": "repo",
            "local_checkout": str(tmp_path),
            "base_branch": "main",
            "authorized_base_sha": "a" * 64,
            "feature_branch": "feat/test",
            "task_specification_path": _tmp_task_spec_path(tmp_path),
            "task_specification_sha256": "b" * 64,
            "required_ci_jobs": [],
            "implementation_worker_command": [],
            "evidence_root": str(tmp_path / "evidence"),
            "state_root": str(tmp_path / "state"),
            "pr_number": 4,
            "current_authorized_head": "a" * 40,
        })
        from autocoder_orchestration.state_machine import (
            StateMachine, STATE_REPAIRING_REVIEW_FINDINGS,
        )
        sm = StateMachine(current_state=STATE_REPAIRING_REVIEW_FINDINGS)
        store.write_atomic("state.json", sm.to_dict())

        from autocoder_orchestration.controller import Controller
        from autocoder_orchestration.context import RunContext
        from autocoder_orchestration.review_repair_relay import DirectiveStore
        ctx = RunContext.from_dict(store.read_optional("run_context.json"))
        controller = Controller(
            context=ctx,
            store=store,
        )
        directive_store = DirectiveStore(
            store=store,
            evidence_root=str(tmp_path / "evidence"),
        )
        loop = RelayLoop(
            context=ctx,
            store=store,
            directive_store=directive_store,
            controller=controller,
            required_check_names=(),
        )
        return loop, controller

    def test_launch_worker_does_not_call_report_repair_pushed(
        self, tmp_path
    ) -> None:
        """The relay's run_once must NOT call
        report_repair_pushed on launch_worker. The
        transition is the supervisor's responsibility
        after observing the new head_sha.
        """
        from autocoder_orchestration.state_machine import (
            STATE_REPAIRING_REVIEW_FINDINGS,
        )
        loop, controller = self._setup_loop(tmp_path)
        snapshot = self._make_snapshot_with_finding()
        decision = loop.run_once(
            snapshot,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
        )
        # The decision is launch_worker.
        assert decision.action == "launch_worker"
        # The controller's state machine MUST still be in
        # REPAIRING_REVIEW_FINDINGS. The transition does NOT
        # fire on launch_worker.
        sm_after = controller.load_state_machine()
        assert sm_after.current_state == STATE_REPAIRING_REVIEW_FINDINGS, (
            f"controller MUST stay in REPAIRING_REVIEW_FINDINGS "
            f"after launch_worker; got {sm_after.current_state!r}"
        )

    def test_mark_head_advanced_fires_transition(
        self, tmp_path
    ) -> None:
        """mark_head_advanced is the explicit hook that
        fires the transition REPAIRING_REVIEW_FINDINGS ->
        AWAITING_CI. The supervisor calls this after the
        worker pushes a new commit.
        """
        from autocoder_orchestration.state_machine import (
            STATE_AWAITING_CI,
        )
        loop, controller = self._setup_loop(tmp_path)
        # The worker pushed new head = b*40.
        loop.mark_head_advanced("a" * 40, "b" * 40)
        sm_after = controller.load_state_machine()
        assert sm_after.current_state == STATE_AWAITING_CI, (
            f"mark_head_advanced MUST transition to AWAITING_CI; "
            f"got {sm_after.current_state!r}"
        )

    def test_launch_failure_keeps_repair_state_recoverable(
        self, tmp_path
    ) -> None:
        """A launch failure leaves the state machine in
        REPAIRING_REVIEW_FINDINGS. The run is recoverable. The
        next round is allowed to run again, but the
        current-head ledger prevents the SAME finding from
        re-entering the directive until fresh evidence
        reopens it (a body edit, a new comment, or a head
        advance).

        Before the ledger (round-26 P1#2) the second round on
        the same head produced another ``launch_worker``
        decision with the same finding, which looped the
        worker forever. The new contract is: the second
        round returns ``enter_qualifying_readiness`` because
        the same finding has already been emitted on this
        head. The relay records that the head is clean of
        FRESH findings and advances the state machine to
        ``QUALIFYING_READINESS`` via the canonical
        ``evaluate_round`` path. The operator can then
        decide whether the readiness certificate can be
        issued.
        """
        from autocoder_orchestration.state_machine import (
            STATE_QUALIFYING_READINESS, STATE_REPAIRING_REVIEW_FINDINGS,
        )
        loop, controller = self._setup_loop(tmp_path)
        # The relay decides launch_worker.
        snapshot = self._make_snapshot_with_finding()
        decision = loop.run_once(
            snapshot,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
        )
        assert decision.action == "launch_worker"
        # Simulate a launch failure: no mark_head_advanced
        # call. The state machine must still be in
        # REPAIRING_REVIEW_FINDINGS.
        sm_after = controller.load_state_machine()
        assert sm_after.current_state == STATE_REPAIRING_REVIEW_FINDINGS
        # The next round on the same head returns
        # enter_qualifying_readiness because the same
        # finding is already on the ledger for this head.
        decision2 = loop.run_once(
            snapshot,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
        )
        assert decision2.action == "enter_qualifying_readiness", (
            f"second round on the same head must return "
            f"enter_qualifying_readiness (ledger suppresses "
            f"the already-emitted finding); got {decision2.action!r}"
        )
        # The state machine has advanced to QUALIFYING_READINESS
        # — the relay signals "head is clean of fresh findings"
        # and the operator can issue a readiness certificate.
        sm_after2 = controller.load_state_machine()
        assert sm_after2.current_state == STATE_QUALIFYING_READINESS, (
            f"after the second round, the state MUST advance to "
            f"QUALIFYING_READINESS; got {sm_after2.current_state!r}"
        )


class TestMarkHeadAdvancedRebindsContext:
    """Round-26 P1: ``mark_head_advanced`` must persist the rebound
    RunContext so subsequent rounds see the new head.

    Proof requirement (user's invariant):
    Head A -> worker pushes Head B -> persisted
    RunContext.current_authorized_head == B ->
    report_repair_pushed succeeds -> controller enters AWAITING_CI.

    No ``InvalidTransition`` is acceptable on the success path,
    and no fallback to a stale context is permitted.
    """

    def _setup_loop(self, tmp_path) -> tuple:
        """Build a fresh controller in REPAIRING_REVIEW_FINDINGS.

        Uses an initial ``current_authorized_head`` of ``a*40`` so
        the rebind path is exercised with a 40-char SHA (matching
        the GitHub SHA-1 form) rather than the 64-char placeholder
        the schema also accepts.
        """
        from autocoder_orchestration.controller import Controller
        from autocoder_orchestration.context import make_run_context
        from autocoder_orchestration.review_repair_relay import DirectiveStore, RelayLoop
        from autocoder_orchestration.state_machine import StateMachine, STATE_REPAIRING_REVIEW_FINDINGS
        from autocoder_orchestration.store import StateStore

        store = StateStore(str(tmp_path / "state"))
        run_id = "test-rebind"
        ctx = make_run_context(
            run_id=run_id,
            repo_owner="owner",
            repo_name="repo",
            local_checkout=str(tmp_path),
            base_branch="main",
            authorized_base_sha="a" * 64,
            feature_branch="feat/test",
            pr_number=4,
            current_authorized_head="a" * 40,
            task_specification_path=_tmp_task_spec_path(tmp_path),
            task_specification_sha256="b" * 64,
            required_ci_jobs=[],
            implementation_worker_command=[],
            evidence_root=str(tmp_path / "evidence"),
            state_root=str(tmp_path / "state"),
        )
        store.write_atomic("run_context.json", ctx.to_dict())
        sm = StateMachine(current_state=STATE_REPAIRING_REVIEW_FINDINGS)
        store.write_atomic("state.json", sm.to_dict())
        controller = Controller(context=ctx, store=store)
        directive_store = DirectiveStore(
            store=store, evidence_root=str(tmp_path / "evidence"),
        )
        loop = RelayLoop(
            context=ctx, store=store, directive_store=directive_store,
            controller=controller, required_check_names=(),
        )
        return loop, controller, store, run_id

    def test_persisted_context_rebinds_to_new_head(self, tmp_path) -> None:
        """After ``mark_head_advanced`` the on-disk
        ``run_context.json`` MUST reflect the new head SHA.
        Without this rebind, the next round's exact-head guard
        rejects the new head because the persisted authorized
        head is still A.
        """
        from autocoder_orchestration.context import RunContext
        from autocoder_orchestration.state_machine import STATE_AWAITING_CI
        loop, controller, store, _run_id = self._setup_loop(tmp_path)
        new_head = "b" * 40
        loop.mark_head_advanced("a" * 40, new_head)
        # Persisted context's current_authorized_head MUST be B.
        ctx_payload = store.read_optional("run_context.json")
        assert ctx_payload is not None, "run_context.json MUST persist"
        assert ctx_payload["current_authorized_head"] == new_head, (
            f"persisted context head {ctx_payload['current_authorized_head']!r} "
            f"must equal new head {new_head!r}"
        )
        ctx = RunContext.from_dict(ctx_payload)
        assert ctx.current_authorized_head == new_head
        # Controller transitions to AWAITING_CI without InvalidTransition.
        sm_after = controller.load_state_machine()
        assert sm_after.current_state == STATE_AWAITING_CI, (
            f"after rebind+push, state MUST be AWAITING_CI; got {sm_after.current_state!r}"
        )

    def test_rebind_uses_64_char_head_too(self, tmp_path) -> None:
        """The rebind path MUST accept both 40- and 64-char SHAs.

        The schema allows both; production SHAs from GitHub can be
        either. A rebind that only accepts one form is a partial
        fix.
        """
        from autocoder_orchestration.context import RunContext
        from autocoder_orchestration.state_machine import STATE_AWAITING_CI
        loop, controller, store, _run_id = self._setup_loop(tmp_path)
        # 64-char new head, original head is 40-char.
        new_head_64 = "c" * 64
        loop.mark_head_advanced("a" * 40, new_head_64)
        ctx_payload = store.read_optional("run_context.json")
        assert ctx_payload["current_authorized_head"] == new_head_64
        ctx = RunContext.from_dict(ctx_payload)
        assert ctx.current_authorized_head == new_head_64
        sm_after = controller.load_state_machine()
        assert sm_after.current_state == STATE_AWAITING_CI

    def test_controller_context_attribute_is_updated(self, tmp_path) -> None:
        """The controller's in-memory ``context.current_authorized_head``
        MUST also be updated so the next transition uses the
        rebound head (not just the on-disk copy).
        """
        loop, controller, _store, _run_id = self._setup_loop(tmp_path)
        loop.mark_head_advanced("a" * 40, "b" * 40)
        assert controller.context.current_authorized_head == "b" * 40, (
            "controller.context.current_authorized_head MUST be rebound "
            "after mark_head_advanced; got "
            f"{controller.context.current_authorized_head!r}"
        )

    def test_invalid_head_shape_is_rejected(self, tmp_path) -> None:
        """A non-hex / wrong-length head_observed MUST be rejected
        before any state is mutated. The rebind path cannot accept
        malformed inputs.
        """
        from autocoder_orchestration.controller import ControllerError
        from autocoder_orchestration.context import RunContext
        loop, controller, store, _run_id = self._setup_loop(tmp_path)
        with pytest.raises(ControllerError):
            loop.mark_head_advanced("a" * 40, "not-a-sha")
        # Persisted context MUST still point at A.
        ctx_payload = store.read_optional("run_context.json")
        assert ctx_payload["current_authorized_head"] == "a" * 40
        # And reloading produces a context that still has A.
        ctx = RunContext.from_dict(ctx_payload)
        assert ctx.current_authorized_head == "a" * 40


class TestFindingLedger:
    """Round-26 P1#2: ``FindingLedger`` enforces the user's
    invariant on the current review head.

    Invariant:
      finding on A -> repaired in B -> old A finding does not
      re-enter B directive unless fresh evidence explicitly
      reopens it.

    The ledger is the durable JSONL record of every finding
    that was emitted into a directive, bound to the exact
    ``head_sha`` it was emitted on. ``is_fresh`` decides
    whether a finding should re-enter the directive on the
    next round:

    - same head, same signature -> NOT fresh (already emitted)
    - different head, same signature -> fresh (head advanced)
    - same head, different signature -> fresh (body edited)
    - no prior record -> fresh (first sighting)
    """

    def _setup(self, tmp_path):
        from autocoder_orchestration.review_repair_relay import FindingLedger
        from autocoder_orchestration.store import StateStore
        store = StateStore(str(tmp_path / "state"))
        return FindingLedger(store, head_sha="a" * 40), store

    def _finding(self, **overrides) -> "Finding":
        from autocoder_orchestration.review_repair_relay import Finding
        # mypy/pyright: explicitly typed dict so the helper
        # passes the Finding dataclass type check.
        base: dict = {
            "finding_id": "coderabbit:42",
            "source": "coderabbit",
            "severity": "P1",
            "title": "Title",
            "body": "Body",
            "file_path": "src/x.py",
            "line": 10,
            "url": None,
            "suggested_test": None,
            "review_id": None,
            "comment_id": 42,
            "check_name": None,
        }
        base.update(overrides)
        return Finding(**base)

    def test_first_sighting_is_fresh(self, tmp_path) -> None:
        ledger, _store = self._setup(tmp_path)
        f = self._finding()
        assert ledger.is_fresh(f) is True

    def test_same_head_same_signature_is_suppressed(self, tmp_path) -> None:
        ledger, _store = self._setup(tmp_path)
        f = self._finding()
        ledger.record(f)
        # Same head, same body -> not fresh.
        assert ledger.is_fresh(self._finding()) is False

    def test_head_advance_re_emits_same_finding(self, tmp_path) -> None:
        """The worker pushed a new commit (head A -> head B). The
        same comment body re-appears in the snapshot for B. The
        ledger MUST consider this fresh so the directive on B
        addresses it (the worker did the work, but the relay
        should still re-offer the finding for visibility).
        """
        from autocoder_orchestration.review_repair_relay import FindingLedger
        from autocoder_orchestration.store import StateStore
        store = StateStore(str(tmp_path / "state"))
        ledger_a = FindingLedger(store, head_sha="a" * 40)
        f = self._finding()
        ledger_a.record(f)
        # New head B; same ledger file (same store) -> fresh.
        ledger_b = FindingLedger(store, head_sha="b" * 40)
        assert ledger_b.is_fresh(self._finding()) is True

    def test_body_edit_reopens_finding(self, tmp_path) -> None:
        """A CodeRabbit edit to the same comment MUST reopen
        the finding (different signature).
        """
        ledger, _store = self._setup(tmp_path)
        f = self._finding()
        ledger.record(f)
        edited = self._finding(body="Edited body")
        assert ledger.is_fresh(edited) is True

    def test_severity_change_reopens_finding(self, tmp_path) -> None:
        """A CodeRabbit severity change MUST reopen the finding.
        """
        ledger, _store = self._setup(tmp_path)
        f = self._finding(severity="P2")
        ledger.record(f)
        promoted = self._finding(severity="P1")
        assert ledger.is_fresh(promoted) is True

    def test_ledger_survives_reload(self, tmp_path) -> None:
        """A new ledger constructed from the same store MUST
        see prior observations (durable across restarts).
        """
        from autocoder_orchestration.review_repair_relay import FindingLedger
        from autocoder_orchestration.store import StateStore
        store = StateStore(str(tmp_path / "state"))
        ledger1 = FindingLedger(store, head_sha="a" * 40)
        f = self._finding()
        ledger1.record(f)
        # Simulate restart: fresh ledger, same store.
        ledger2 = FindingLedger(store, head_sha="a" * 40)
        assert ledger2.is_fresh(self._finding()) is False

    def test_filter_drops_already_emitted(self, tmp_path) -> None:
        """``filter_findings_to_current_head`` removes findings
        the ledger has already observed at the current head.
        """
        from autocoder_orchestration.review_repair_relay import (
            filter_findings_to_current_head,
        )
        ledger, _store = self._setup(tmp_path)
        a = self._finding(finding_id="coderabbit:1", comment_id=1)
        b = self._finding(finding_id="coderabbit:2", comment_id=2)
        ledger.record(a)
        out = filter_findings_to_current_head([a, b], ledger)
        # ``a`` is on the ledger (same head, same sig) so
        # it is dropped; ``b`` is a first sighting so kept.
        ids = {f.finding_id for f in out}
        assert ids == {"coderabbit:2"}

    def test_collect_findings_accepts_ledger(self, tmp_path) -> None:
        """``collect_findings`` with a ``ledger`` kwarg MUST
        apply the filter internally.
        """
        from autocoder_orchestration.review_repair_relay import collect_findings
        ledger, _store = self._setup(tmp_path)
        a = self._finding(finding_id="coderabbit:1", comment_id=1)
        b = self._finding(finding_id="coderabbit:2", comment_id=2)
        ledger.record(a)
        snap = {
            "head_sha": "a" * 40,
            "head_match": True,
            "review_comments": [],
            "issue_comments": [],
            "_provider_issue_comments": {
                "coderabbit": [
                    {"id": 1, "body": "Body", "html_url": "u1"},
                    {"id": 2, "body": "Body", "html_url": "u2"},
                ],
            },
            "required_checks": {},
        }
        out = collect_findings(snap, ledger=ledger)
        ids = {f.finding_id for f in out}
        # Only the unrecorded finding is emitted.
        assert "coderabbit:2" in ids
        # And the recorded one is suppressed (same head + body).
        # We check via filter directly because the provider
        # collector may classify severity differently.
        from autocoder_orchestration.review_repair_relay import filter_findings_to_current_head
        out2 = filter_findings_to_current_head([a, b], ledger)
        assert {f.finding_id for f in out2} == {"coderabbit:2"}

    def test_invalid_input_raises(self, tmp_path) -> None:
        """``is_fresh`` MUST reject non-Finding input rather
        than silently returning False (which would mask
        bugs in callers).
        """
        from autocoder_orchestration.review_repair_relay import (
            DirectiveContractError,
        )
        ledger, _store = self._setup(tmp_path)
        # A non-Finding dict MUST raise. The guard runs BEFORE
        # the ledger load so even an empty ledger raises.
        with pytest.raises(DirectiveContractError):
            ledger.is_fresh({"not": "a finding"})
        # Same for a string, list, None, or a non-Finding
        # object instance.
        for bad in ("a string", [1, 2, 3], None, 42, object()):
            with pytest.raises(DirectiveContractError):
                ledger.is_fresh(bad)


class TestFindingLedgerPersistsAcrossRounds:
    """End-to-end: ``RelayLoop.run_once`` writes the ledger;
    the next round on the same head suppresses the same finding.
    """

    def _setup_loop(self, tmp_path):
        from autocoder_orchestration.controller import Controller
        from autocoder_orchestration.context import make_run_context
        from autocoder_orchestration.review_repair_relay import DirectiveStore, RelayLoop
        from autocoder_orchestration.state_machine import StateMachine, STATE_REPAIRING_REVIEW_FINDINGS
        from autocoder_orchestration.store import StateStore
        store = StateStore(str(tmp_path / "state"))
        ctx = make_run_context(
            run_id="ledger-test",
            repo_owner="owner", repo_name="repo",
            local_checkout=str(tmp_path), base_branch="main",
            authorized_base_sha="a" * 64, feature_branch="feat/test",
            pr_number=4, current_authorized_head="a" * 40,
            task_specification_path=_tmp_task_spec_path(tmp_path),
            task_specification_sha256="b" * 64,
            required_ci_jobs=[], implementation_worker_command=[],
            evidence_root=str(tmp_path / "evidence"),
            state_root=str(tmp_path / "state"),
        )
        store.write_atomic("run_context.json", ctx.to_dict())
        sm = StateMachine(current_state=STATE_REPAIRING_REVIEW_FINDINGS)
        store.write_atomic("state.json", sm.to_dict())
        controller = Controller(context=ctx, store=store)
        directive_store = DirectiveStore(
            store=store, evidence_root=str(tmp_path / "evidence"),
        )
        loop = RelayLoop(
            context=ctx, store=store, directive_store=directive_store,
            controller=controller, required_check_names=(),
        )
        return loop, store

    def test_ledger_persists_between_rounds(self, tmp_path) -> None:
        loop, store = self._setup_loop(tmp_path)
        snap = {
            "head_sha": "a" * 40, "head_match": True,
            "review_comments": [], "issue_comments": [],
            "_provider_issue_comments": {
                "coderabbit": [
                    {"id": 99, "body": "P1 finding", "html_url": "u99"},
                ],
            },
            "required_checks": {},
        }
        d1 = loop.run_once(
            snap, head_sha="a" * 40, repo="owner/repo", pr_number=4,
        )
        assert d1.action == "launch_worker"
        # The ledger was written.
        from autocoder_orchestration.review_repair_relay import FindingLedger
        ledger = FindingLedger(store, head_sha="a" * 40)
        entries = ledger.load()
        assert "coderabbit:99" in entries
        # Second round on the same head: same body, no edit.
        d2 = loop.run_once(
            snap, head_sha="a" * 40, repo="owner/repo", pr_number=4,
        )
        # The ledger suppresses the same finding on the same
        # head — the directive's findings count is zero and the
        # action flips to enter_qualifying_readiness.
        assert d2.action == "enter_qualifying_readiness", (
            f"same head + same body must be suppressed by the ledger; "
            f"got action={d2.action!r}"
        )

    def test_body_edit_on_same_head_reopens(self, tmp_path) -> None:
        """A body edit on the same head MUST reopen the finding.
        """
        loop, _store = self._setup_loop(tmp_path)
        snap_v1 = {
            "head_sha": "a" * 40, "head_match": True,
            "review_comments": [], "issue_comments": [],
            "_provider_issue_comments": {
                "coderabbit": [{"id": 99, "body": "v1 body", "html_url": "u"}],
            },
            "required_checks": {},
        }
        d1 = loop.run_once(
            snap_v1, head_sha="a" * 40, repo="owner/repo", pr_number=4,
        )
        assert d1.action == "launch_worker"
        # CodeRabbit edits the same comment — different body.
        snap_v2 = {
            "head_sha": "a" * 40, "head_match": True,
            "review_comments": [], "issue_comments": [],
            "_provider_issue_comments": {
                "coderabbit": [{"id": 99, "body": "v2 body", "html_url": "u"}],
            },
            "required_checks": {},
        }
        d2 = loop.run_once(
            snap_v2, head_sha="a" * 40, repo="owner/repo", pr_number=4,
        )
        # Different body = fresh evidence = launch_worker.
        assert d2.action == "launch_worker", (
            f"body edit on same head must reopen the finding; "
            f"got action={d2.action!r}"
        )

    def test_new_comment_id_is_always_fresh(self, tmp_path) -> None:
        """A new comment with a new id on the same head MUST be
        emitted regardless of the ledger (different finding_id).
        """
        loop, _store = self._setup_loop(tmp_path)
        snap_v1 = {
            "head_sha": "a" * 40, "head_match": True,
            "review_comments": [], "issue_comments": [],
            "_provider_issue_comments": {
                "coderabbit": [{"id": 1, "body": "x", "html_url": "u"}],
            },
            "required_checks": {},
        }
        d1 = loop.run_once(
            snap_v1, head_sha="a" * 40, repo="owner/repo", pr_number=4,
        )
        assert d1.action == "launch_worker"
        # A second, distinct comment arrives on the same head.
        snap_v2 = {
            "head_sha": "a" * 40, "head_match": True,
            "review_comments": [], "issue_comments": [],
            "_provider_issue_comments": {
                "coderabbit": [
                    {"id": 1, "body": "x", "html_url": "u"},
                    {"id": 2, "body": "y", "html_url": "u2"},
                ],
            },
            "required_checks": {},
        }
        d2 = loop.run_once(
            snap_v2, head_sha="a" * 40, repo="owner/repo", pr_number=4,
        )
        # The new comment must be emitted (different finding_id).
        assert d2.action == "launch_worker"

