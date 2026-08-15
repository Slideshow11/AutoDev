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
    RecoverableRetry,
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
    provider_surface_complete: bool = True,
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
        "provider_surface_complete": provider_surface_complete,
    }


def _auto_bind_comments(snap: dict) -> None:
    """Test helper: bind every per-provider issue comment to the
    snapshot's ``head_sha`` via ``commit_id``. Production
    captures (round-30+) carry this identity; tests
    pre-dating the bound-import invariant do not. This
    helper brings the legacy tests into the new
    contract.
    """
    current_head = snap.get("head_sha")
    for provider, comments in snap.get("_provider_issue_comments", {}).items():
        for c in comments:
            if isinstance(c, dict) and "commit_id" not in c:
                c["commit_id"] = current_head


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
                {"id": 100, "body": "P1: foo.py:10 looks wrong", "html_url": "x", "commit_id": "a" * 40},
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
                {"id": 1, "body": "P2 trivial", "commit_id": "a" * 40},
                {"id": 2, "body": "P0 stop", "commit_id": "a" * 40},
                {"id": 3, "body": "P1 important", "commit_id": "a" * 40},
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
                {"id": 1, "body": "Add regression test test_foo_returns_bar", "commit_id": "a" * 40},
            ],
        })
        findings = collect_findings(snap)
        assert findings[0].suggested_test == "test_foo_returns_bar"

    def test_p1_priority_high_treated_as_p1(self) -> None:
        snap = _make_snapshot(per_provider={
            "coderabbit": [
                {"id": 1, "body": "priority-high: missing check", "commit_id": "a" * 40},
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

    def test_p0_escalates_when_max_findings_cap_hit(self) -> None:
        """Round-665: a P0_ESCALATE must still escalate when
        the per-directive ``max_findings`` cap would otherwise
        truncate it. The previous three-way partition removed
        P0 from ``p2`` and then capped ``p1 + p2``, silently
        dropping the escalation so a repair directive was
        launched instead of escalating to a human.
        """
        # One P0 plus enough P1/P2 that the cap triggers.
        findings = [
            _make_finding(severity=SEVERITY_P0_ESCALATE, body="P0 critical"),
        ] + [
            _make_finding(
                severity=SEVERITY_P1,
                body=f"p1 finding {i}",
            )
            for i in range(5)
        ] + [
            _make_finding(
                severity=SEVERITY_P2,
                body=f"p2 finding {i}",
            )
            for i in range(5)
        ]
        with pytest.raises(EscalateToHuman) as exc:
            build_directive(
                round_index=42,
                head_sha="a" * 40,
                repo="owner/repo",
                pr_number=4,
                findings=findings,
                coordinator_actor="controller",
                max_findings=3,
            )
        assert "round 42" in str(exc.value)
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
        # Round-591: CI_FAILURE findings appear FIRST in the
        # directive so a worker observes them before any
        # optional review work.
        assert d2.findings[0].severity == SEVERITY_CI_FAILURE
        assert d2.findings[1].severity == SEVERITY_P1

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
                {"id": 1, "body": "🚦 Walkthrough comment.", "commit_id": "a" * 40},
            ],
        })
        findings = collect_findings(snap)
        assert findings == []

    def test_in_progress_marker_is_filtered(self) -> None:
        snap = _make_snapshot(per_provider={
            "coderabbit": [
                {"id": 1, "body": "Review in progress.", "commit_id": "a" * 40},
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
                {"id": 1, "body": "P1: see walkthrough above is stale", "commit_id": "a" * 40},
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
                {"id": 1, "body": "🚦 Walkthrough comment.", "commit_id": "a" * 40},
            ],
        })
        findings = collect_findings(snap)
        assert findings == []

    def test_completion_marker_is_filtered(self) -> None:
        snap = _make_snapshot(per_provider={
            "coderabbit": [
                {"id": 1, "body": "Review completed.", "commit_id": "a" * 40},
            ],
        })
        findings = collect_findings(snap)
        assert findings == []

    def test_actual_finding_is_NOT_filtered(self) -> None:
        # A regular review finding must still be surfaced.
        snap = _make_snapshot(per_provider={
            "coderabbit": [
                {"id": 1, "body": "P1: foo.py:1 broken", "commit_id": "a" * 40},
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
            "coderabbit": [{"id": 1, "body": "P1: foo.py:10 broken", "commit_id": "a" * 40}],
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
            "coderabbit": [{"id": 1, "body": "P0 critical: stops the run", "commit_id": "a" * 40}],
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
            "coderabbit": [{"id": 1, "body": "please force push now", "commit_id": "a" * 40}],
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
            "coderabbit": [{"id": 1, "body": "P1: foo.py:1 broken", "commit_id": "a" * 40}],
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
            "coderabbit": [{"id": 1, "body": "P1: foo.py:1 broken", "commit_id": "a" * 40}],
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
            "coderabbit": [{"id": 1, "body": "P1: foo.py:1 broken", "commit_id": "a" * 40}],
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
            "coderabbit": [{"id": 1, "body": "P1: foo.py:1 broken", "commit_id": "a" * 40}],
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
            "coderabbit": [{"id": 1, "body": "P0 critical", "commit_id": "a" * 40}],
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
        # Round-30: runtime budget is a scheduling boundary,
        # not a protected-authority escalation. The relay
        # MUST raise ``RecoverableRetry`` and the controller
        # MUST NOT enter BLOCKED.
        with pytest.raises(RecoverableRetry):
            loop.run_once(
                _make_snapshot(),
                head_sha="a" * 40,
                repo="owner/repo",
                pr_number=4,
            )
        sm = controller.load_state_machine()
        assert sm is not None
        # Round-30: the controller MUST remain in the current
        # state (REPAIRING_REVIEW_FINDINGS); the slice ended
        # without BLOCKED. The supervisor / scheduler resumes
        # the SAME outstanding work on the next slice.
        assert sm.current_state != "BLOCKED", (
            f"runtime budget MUST NOT escalate to BLOCKED; "
            f"got current_state={sm.current_state!r}"
        )
        # The retry record MUST be persisted.
        retry_path = evidence_root / "round_budget_retry.json"
        assert retry_path.is_file(), (
            f"round-budget retry state MUST be persisted at "
            f"{retry_path}; supervisor reads it on resume"
        )
        import json as _json
        payload = _json.loads(retry_path.read_text())
        assert payload["head_sha"] == "a" * 40
        assert payload["max_rounds"] == 3

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
            "coderabbit": [{"id": 1, "body": "P1: foo.py:1", "commit_id": "a" * 40}],
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
            "coderabbit": [{"id": 1, "body": "P0 critical: stop the run", "commit_id": "a" * 40}],
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
        """Round-30: the max_rounds runtime budget is a
        scheduling boundary, NOT a protected-authority
        escalation. The relay MUST raise ``RecoverableRetry``
        (distinct from ``EscalateToHuman``) and persist the
        retry state. The supervisor / scheduler resumes the
        same outstanding work on the next slice.
        """
        from autocoder_orchestration.review_repair_relay import (
            RecoverableRetry,
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
            "coderabbit": [{"id": 1, "body": "P1: foo.py:1", "commit_id": "a" * 40}],
        })
        with pytest.raises(RecoverableRetry):
            loop.run_once(
                snap, head_sha="a" * 40,
                repo="owner/repo", pr_number=4,
            )
        # The retry state MUST be persisted; the controller
        # MUST NOT be in BLOCKED.
        sm = controller.load_state_machine()
        assert sm is not None
        assert sm.current_state != "BLOCKED", (
            f"runtime budget MUST NOT escalate to BLOCKED; "
            f"got current_state={sm.current_state!r}"
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
                    {"id": 1, "body": "P1: foo.py:1 broken", "commit_id": "a" * 40},
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
        next round is allowed to run again.

        Round-27 contract: the SAME finding remains ACTIVE in
        the ledger and is re-emitted into a new directive on
        every round the worker has not responded with positive
        resolution. qualification is impossible while ACTIVE
        findings exist on the same head.
        """
        from autocoder_orchestration.state_machine import (
            STATE_REPAIRING_REVIEW_FINDINGS,
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
        # The next round on the same head MUST re-emit the
        # finding because DISPATCHED is not consumed. The
        # action is launch_worker (NOT enter_qualifying_readiness —
        # that was the round-26 bug).
        decision2 = loop.run_once(
            snapshot,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
        )
        assert decision2.action == "launch_worker", (
            f"round-27: ACTIVE finding on same head MUST be "
            f"re-emitted; got {decision2.action!r}"
        )
        # The state machine is still REPAIRING_REVIEW_FINDINGS
        # — the controller waits for a positive head advance or
        # operator intervention.
        sm_after2 = controller.load_state_machine()
        assert sm_after2.current_state == STATE_REPAIRING_REVIEW_FINDINGS, (
            f"while ACTIVE findings exist, the state must remain "
            f"in REPAIRING_REVIEW_FINDINGS; got {sm_after2.current_state!r}"
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
        loop, _controller, store, _run_id = self._setup_loop(tmp_path)
        with pytest.raises(ControllerError):
            loop.mark_head_advanced("a" * 40, "not-a-sha")
        # Persisted context MUST still point at A.
        ctx_payload = store.read_optional("run_context.json")
        assert ctx_payload["current_authorized_head"] == "a" * 40
        # And reloading produces a context that still has A.
        ctx = RunContext.from_dict(ctx_payload)
        assert ctx.current_authorized_head == "a" * 40

    def test_persistence_failure_leaves_disk_and_memory_on_old_head(
        self, tmp_path
    ) -> None:
        """Round-27 P1#5 atomicity: a failed rebind persistence
        MUST leave the run in a consistent state on disk AND in
        memory. ``self.context.current_authorized_head`` MUST
        remain on the OLD head; the on-disk ``run_context.json``
        MUST also remain on the OLD head; no state-machine
        transition may fire.

        The injection point is ``save_run_context`` (the hook
        called BEFORE ``self.context`` is mutated). We mock it
        to raise ``OSError`` and assert the post-state.
        """
        from autocoder_orchestration.context import RunContext
        from autocoder_orchestration.state_machine import (
            STATE_AWAITING_CI,
            STATE_REPAIRING_REVIEW_FINDINGS,
        )
        loop, controller, store, _run_id = self._setup_loop(tmp_path)
        # Capture the pre-state.
        original_disk = store.read_optional("run_context.json")
        original_in_memory = controller.context.current_authorized_head
        original_sm = controller.load_state_machine()
        assert original_disk is not None
        assert original_disk["current_authorized_head"] == original_in_memory
        # Inject a save_run_context_for failure. The rebind
        # sequence MUST catch the OSError and leave ``self.context``
        # untouched; in-memory and disk MUST both still be on
        # the OLD head.
        original_save = controller.save_run_context_for
        def failing_save(_context):
            raise OSError("disk full — persistence failed")
        # Replace the method on the instance (round-27 hook).
        controller.save_run_context_for = failing_save  # type: ignore[assignment]
        try:
            with pytest.raises(OSError):
                loop.mark_head_advanced("a" * 40, "b" * 40)
        finally:
            controller.save_run_context_for = original_save  # type: ignore[assignment]
        # In-memory context: still bound to OLD head (a*40).
        assert controller.context.current_authorized_head == "a" * 40, (
            f"on persistence failure, self.context MUST remain "
            f"on the OLD head; got "
            f"{controller.context.current_authorized_head!r}"
        )
        # On-disk context: still bound to OLD head (a*40).
        disk_after = store.read_optional("run_context.json")
        assert disk_after is not None
        assert disk_after["current_authorized_head"] == "a" * 40, (
            f"on persistence failure, disk MUST remain on the OLD "
            f"head; got {disk_after['current_authorized_head']!r}"
        )
        # State machine: still REPAIRING_REVIEW_FINDINGS — the
        # transition did NOT fire because the persistence
        # failed BEFORE ``self.context`` was mutated.
        sm_after = controller.load_state_machine()
        assert sm_after.current_state == STATE_REPAIRING_REVIEW_FINDINGS, (
            f"on persistence failure, state MUST remain in "
            f"REPAIRING_REVIEW_FINDINGS; got {sm_after.current_state!r}"
        )
        # The original (pre-failure) disk content is byte-equal.
        assert disk_after == original_disk, (
            "on persistence failure, disk MUST be byte-equal to "
            "the pre-call content"
        )


class TestFindingLedger:
    """Round-27 P1#1: ``FindingLedger`` enforces the lifecycle
    contract.

    Invariant:
      OBSERVED    -> DISPATCHED -> ACTIVE -> SUPERSEDED  (head advance)
                                   \\-> REPAIRED    (semantic re-eval)

    ``is_fresh(finding)`` returns True iff the finding MUST be
    emitted into the next directive. The rule:

      - No prior entry on the current head -> fresh (first sighting)
      - Prior entry state in {OBSERVED, DISPATCHED, ACTIVE}
        on the current head -> fresh (still unresolved; the
        worker has not yet responded, or the directive was
        emitted but no positive evidence exists that the
        finding is resolved)
      - Prior entry state in {SUPERSEDED, REPAIRED} on the
        current head -> NOT fresh (terminal states)
      - Prior entry is on a DIFFERENT head -> fresh (the head
        advanced; the relay must call
        ``mark_superseded_by_head`` to promote the prior entry
        explicitly, otherwise the prior remains authoritative
        only for its own head)

    Round-27 invariant: a failed worker launch, worker crash,
    worker timeout, no-op worker, or worker that exits without
    pushing MUST leave the same-head finding in DISPATCHED /
    ACTIVE so the next round re-emits it. Round-26 conflated
    DISPATCHED with "consumed"; that contract is now
    explicitly forbidden.
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

    def test_observed_then_dispatched_then_active_keeps_fresh(self, tmp_path) -> None:
        """Round-27: a finding placed in a directive and
        acknowledged by the worker is still ACTIVE on the same
        head. ``is_fresh`` returns True so the next round on
        the same head re-emits it (round-26 bug fix).
        """
        ledger, _store = self._setup(tmp_path)
        f = self._finding()
        ledger.record_observed(f)
        ledger.record_dispatched(f)
        ledger.mark_active(f)
        # Same head, same body -> STILL fresh (active, not resolved).
        assert ledger.is_fresh(self._finding()) is True, (
            "DISPATCHED/ACTIVE MUST NOT suppress the next-round emit; "
            "the worker has not yet responded with positive resolution"
        )

    def test_dispatched_alone_keeps_fresh(self, tmp_path) -> None:
        """Even without ACTIVE promotion, the round-27 ledger
        keeps DISPATCHED fresh so a worker that never even
        acknowledged the directive is re-emitted on the next
        round.
        """
        ledger, _store = self._setup(tmp_path)
        f = self._finding()
        ledger.record_observed(f)
        ledger.record_dispatched(f)
        assert ledger.is_fresh(self._finding()) is True

    def test_observed_alone_keeps_fresh(self, tmp_path) -> None:
        """An OBSERVED entry on the same head is still fresh —
        the directive has not even been built yet.
        """
        ledger, _store = self._setup(tmp_path)
        f = self._finding()
        ledger.record_observed(f)
        assert ledger.is_fresh(self._finding()) is True

    def test_superseded_is_not_fresh(self, tmp_path) -> None:
        """A finding SUPERSEDED on the current head is NOT fresh.
        The head advanced and the relay rewrote the entry to
        SUPERSEDED.
        """
        ledger, _store = self._setup(tmp_path)
        f = self._finding()
        ledger.record_observed(f)
        ledger.record_dispatched(f)
        ledger.mark_active(f)
        promoted = ledger.mark_superseded_by_head("a" * 40)
        assert promoted >= 1
        # Same head + SUPERSEDED -> NOT fresh.
        assert ledger.is_fresh(self._finding()) is False

    def test_repaired_is_not_fresh(self, tmp_path) -> None:
        """A finding REPAIRED on the current head is NOT fresh.
        The repair evidence proves the finding no longer applies.
        """
        ledger, _store = self._setup(tmp_path)
        f = self._finding()
        ledger.record_observed(f)
        ledger.record_dispatched(f)
        ledger.mark_active(f)
        ledger.mark_repaired(f, resolution_evidence="upstream resolved")
        assert ledger.is_fresh(self._finding()) is False

    def test_head_advance_re_emits_same_finding(self, tmp_path) -> None:
        """The worker pushed a new commit (head A -> head B).
        Round-27 contract: the OLD head's findings are NOT
        automatically re-emitted on the new head; the relay
        must first call ``mark_superseded_by_head`` to promote
        them. Before that call, the prior entry is on the
        OLD head and ``is_fresh`` returns True on the NEW
        head (different head_sha).
        """
        from autocoder_orchestration.review_repair_relay import FindingLedger
        from autocoder_orchestration.store import StateStore
        store = StateStore(str(tmp_path / "state"))
        ledger_a = FindingLedger(store, head_sha="a" * 40)
        f = self._finding()
        ledger_a.record_observed(f)
        ledger_a.record_dispatched(f)
        # New head B; same ledger file -> fresh (different head).
        ledger_b = FindingLedger(store, head_sha="b" * 40)
        assert ledger_b.is_fresh(self._finding()) is True

    def test_head_advance_with_supersede_is_fresh_on_new_head(
        self, tmp_path: Path,
    ) -> None:
        """Round-28 P5: a SUPERSEDED row on the OLD head does NOT
        silence a fresh observation of the same finding on the
        NEW head. The user explicitly rejected the round-27
        cross-head shadowing: a stale SUPERSEDED on A is NOT
        positive cross-head resolution evidence, and the
        finding on B remains active.

        A finding whose signature differs IS fresh (different
        body / title / severity is new evidence at the new
        head).
        """
        from autocoder_orchestration.review_repair_relay import FindingLedger
        from autocoder_orchestration.store import StateStore
        store = StateStore(str(tmp_path / "state"))
        # The finding was ACTIVE on the OLD head (the worker
        # had acknowledged it but had not yet resolved it
        # when the head advanced). ``mark_superseded_by_head``
        # is called with the OLD head.
        old_head = "a" * 40
        new_head = "b" * 40
        ledger_a = FindingLedger(store, head_sha=old_head)
        f = self._finding()
        ledger_a.record_observed(f)
        ledger_a.record_dispatched(f)
        ledger_a.mark_active(f)
        promoted = ledger_a.mark_superseded_by_head(old_head)
        assert promoted == 1
        # Round-28 P5: on the new head, the same signature is
        # NOT silenced by the SUPERSEDED row on the OLD head
        # (no positive cross-head resolution evidence). The
        # finding on B is FRESH.
        ledger_b = FindingLedger(store, head_sha=new_head)
        assert ledger_b.is_fresh(self._finding()) is True, (
            f"Round-28 P5: cross-head SUPERSEDED MUST NOT silence "
            f"a fresh observation on the new head."
        )
        # A different signature is fresh too.
        assert ledger_b.is_fresh(self._finding(body="new body")) is True

    def test_body_edit_reopens_finding(self, tmp_path) -> None:
        """A CodeRabbit edit to the same comment MUST reopen
        the finding (different signature) on the same head.
        Round-27: a body edit while SUPERSEDED does not
        reopen; a body edit while ACTIVE does.
        """
        ledger, _store = self._setup(tmp_path)
        f = self._finding()
        ledger.record_observed(f)
        ledger.record_dispatched(f)
        ledger.mark_active(f)
        edited = self._finding(body="Edited body")
        assert ledger.is_fresh(edited) is True

    def test_severity_change_reopens_finding(self, tmp_path) -> None:
        """A CodeRabbit severity change MUST reopen the finding.
        """
        ledger, _store = self._setup(tmp_path)
        f = self._finding(severity="P2")
        ledger.record_observed(f)
        ledger.record_dispatched(f)
        ledger.mark_active(f)
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
        ledger1.record_observed(f)
        ledger1.record_dispatched(f)
        ledger1.mark_active(f)
        # Simulate restart: fresh ledger, same store.
        ledger2 = FindingLedger(store, head_sha="a" * 40)
        # Round-27: ACTIVE on the same head IS fresh (the worker
        # has not yet responded). The next round re-emits.
        assert ledger2.is_fresh(self._finding()) is True

    def test_filter_drops_superseded_and_repaired_on_same_head(
        self, tmp_path: Path,
    ) -> None:
        """``filter_findings_to_current_head`` removes findings
        the ledger has marked SUPERSEDED or REPAIRED on the
        CURRENT head, but keeps ACTIVE findings so the next
        round re-emits them.

        Round-28 P5: cross-head SUPERSEDED on a DIFFERENT head
        does NOT suppress. The test focuses on same-head
        SUPERSEDED + REPAIRED, where the filter MUST drop them
        (terminal on the current head). To test ACTIVE
        survival we add a NEW ACTIVE finding on the new head.
        """
        from autocoder_orchestration.review_repair_relay import (
            filter_findings_to_current_head,
        )
        from autocoder_orchestration.review_repair_relay import FindingLedger
        from autocoder_orchestration.store import StateStore
        store = StateStore(str(tmp_path / "state"))
        head = "a" * 40
        ledger = FindingLedger(store, head_sha=head)
        # Three findings on the SAME head.
        b = self._finding(finding_id="coderabbit:2", comment_id=2)
        c = self._finding(finding_id="coderabbit:3", comment_id=3)
        # ``b`` is REPAIRED on the current head -> terminal.
        ledger.record_observed(b)
        ledger.record_dispatched(b)
        ledger.mark_active(b)
        ledger.mark_repaired(b, resolution_evidence="upstream resolved")
        # ``c`` is SUPERSEDED on the current head -> terminal.
        ledger.record_observed(c)
        ledger.record_dispatched(c)
        ledger.mark_active(c)
        ledger.mark_superseded_by_head(head)
        # Add a fresh ACTIVE finding on the same head. The
        # filter MUST keep it (terminal-on-head does not apply).
        d = self._finding(finding_id="coderabbit:4", comment_id=4, body="active on current head")
        ledger.record_observed(d)
        ledger.record_dispatched(d)
        ledger.mark_active(d)
        out = filter_findings_to_current_head([b, c, d], ledger)
        ids = {f.finding_id for f in out}
        # ``b`` REPAIRED -> dropped. ``c`` SUPERSEDED -> dropped.
        # ``d`` ACTIVE on the current head -> KEPT.
        assert ids == {"coderabbit:4"}, (
            f"only ACTIVE findings on the current head should be "
            f"re-emitted; got {ids}"
        )

    def test_collect_findings_accepts_ledger(self, tmp_path) -> None:
        """``collect_findings`` with a ``ledger`` kwarg MUST
        apply the filter internally. Round-27: a fresh
        finding is still emitted; an ACTIVE finding on the
        head is re-emitted (the worker has not responded).
        """
        from autocoder_orchestration.review_repair_relay import (
            collect_findings, filter_findings_to_current_head,
        )
        ledger, _store = self._setup(tmp_path)
        a = self._finding(finding_id="coderabbit:1", comment_id=1)
        b = self._finding(finding_id="coderabbit:2", comment_id=2)
        # ``a`` recorded as ACTIVE -> kept (re-emit on the next round).
        ledger.record_observed(a)
        ledger.record_dispatched(a)
        ledger.mark_active(a)
        snap = {
            "head_sha": "a" * 40,
            "head_match": True,
            "review_comments": [],
            "issue_comments": [],
            "_provider_issue_comments": {
                "coderabbit": [
                    {"id": 1, "body": "Body", "html_url": "u1", "commit_id": "a" * 40},
                    {"id": 2, "body": "Body", "html_url": "u2", "commit_id": "a" * 40},
                ],
            },
            "required_checks": {},
        }
        out = collect_findings(snap, ledger=ledger)
        ids = {f.finding_id for f in out}
        # ``a`` was ACTIVE -> emitted; ``b`` is first sighting -> emitted.
        assert "coderabbit:1" in ids and "coderabbit:2" in ids, (
            f"both ACTIVE and fresh findings must be emitted; got {ids}"
        )
        # And filter_findings_to_current_head returns both for
        # the same input set.
        out2 = filter_findings_to_current_head([a, b], ledger)
        assert {f.finding_id for f in out2} == {"coderabbit:1", "coderabbit:2"}

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

    def test_mark_superseded_is_idempotent(self, tmp_path) -> None:
        """Calling ``mark_superseded_by_head`` twice for the
        same head MUST NOT stack duplicate SUPERSEDED rows.
        The second call is a no-op.
        """
        from autocoder_orchestration.review_repair_relay import FindingLedger
        from autocoder_orchestration.store import StateStore
        store = StateStore(str(tmp_path / "state"))
        ledger = FindingLedger(store, head_sha="a" * 40)
        f = self._finding()
        ledger.record_observed(f)
        ledger.record_dispatched(f)
        ledger.mark_active(f)
        first = ledger.mark_superseded_by_head("a" * 40)
        second = ledger.mark_superseded_by_head("a" * 40)
        assert first == 1 and second == 0, (
            f"supersede must be idempotent; got first={first}, second={second}"
        )

    def test_invalid_head_sha_raises(self, tmp_path) -> None:
        """``FindingLedger`` rejects non-hex head_sha at
        construction time so a wrong head never silently
        persists a finding on the wrong ledger.
        """
        from autocoder_orchestration.review_repair_relay import (
            DirectiveContractError, FindingLedger,
        )
        from autocoder_orchestration.store import StateStore
        store = StateStore(str(tmp_path / "state"))
        for bad in (None, "", "not-hex", "A" * 40, 1234):
            with pytest.raises((DirectiveContractError, TypeError)):
                FindingLedger(store, head_sha=bad)


class TestFindingLedgerLifecycleInvariant:
    """Round-27 P1#1 user-specified tests: the lifecycle
    contract is enforced at the directive level, not the
    ledger level alone. The relay MUST NOT mark a finding
    consumed merely because a directive was emitted.

    Three scenarios from the user's spec:

      1. Head A has F -> directive created -> worker launch
         fails -> next round on A STILL contains F ->
         qualification is impossible.
      2. Head A has F -> worker launches but pushes nothing
         -> next round on A STILL contains F.
      3. Head A has F -> worker pushes B -> A/F may now be
         treated as superseded subject to fresh B evidence.
    """

    def _setup_loop(self, tmp_path):
        from autocoder_orchestration.controller import Controller
        from autocoder_orchestration.context import make_run_context
        from autocoder_orchestration.review_repair_relay import (
            DirectiveStore, FindingLedger, RelayLoop,
        )
        from autocoder_orchestration.state_machine import (
            StateMachine, STATE_REPAIRING_REVIEW_FINDINGS,
        )
        from autocoder_orchestration.store import StateStore
        store = StateStore(str(tmp_path / "state"))
        ctx = make_run_context(
            run_id="lifecycle-test",
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

    def _snap_with_finding(self, head_sha, finding_id, body):
        return {
            "head_sha": head_sha, "head_match": True,
            "review_comments": [], "issue_comments": [],
            "provider_surface_complete": True,
            "_provider_issue_comments": {
                "coderabbit": [
                    {"id": finding_id, "body": body,
                     "html_url": "u", "commit_id": head_sha},
                ],
            },
            "required_checks": {},
        }

    def test_worker_launch_failure_keeps_finding_active(self, tmp_path) -> None:
        """Scenario 1: Head A has F -> directive created ->
        worker launch fails -> next round on A STILL contains
        F. The relay MUST NOT mark F consumed because a
        directive was emitted.
        """
        loop, store = self._setup_loop(tmp_path)
        snap_a = self._snap_with_finding("a" * 40, 99, "P1 finding")
        d1 = loop.run_once(
            snap_a, head_sha="a" * 40,
            repo="owner/repo", pr_number=4,
        )
        # First round: launch_worker (directive issued).
        assert d1.action == "launch_worker", (
            f"first round must emit directive; got {d1.action!r}"
        )
        # Worker launch "fails" — the controller stays in
        # REPAIRING_REVIEW_FINDINGS; the directive stays on
        # disk; the next round runs on the same head.
        sm = loop.controller.load_state_machine()
        assert sm.current_state == "REPAIRING_REVIEW_FINDINGS"
        # Second round: same head, same body -> the relay MUST
        # still emit F. Round-27 invariant: DISPATCHED does
        # not consume.
        d2 = loop.run_once(
            snap_a, head_sha="a" * 40,
            repo="owner/repo", pr_number=4,
        )
        assert d2.action == "launch_worker", (
            f"worker launch failure MUST leave F in the next "
            f"directive; got {d2.action!r}"
        )
        # And the finding on the same head is ACTIVE/DISPATCHED
        # in the ledger, never SUPERSEDED.
        from autocoder_orchestration.review_repair_relay import FindingLedger
        ledger = FindingLedger(store, head_sha="a" * 40)
        entry = ledger.state_of("coderabbit:99")
        assert entry is not None, (
            "ledger MUST retain the entry; missing entries are "
            "evidence the round-26 'consumed on dispatch' bug"
        )
        assert entry["state"] in ("OBSERVED", "DISPATCHED", "ACTIVE"), (
            f"finding state MUST NOT be SUPERSEDED on a launch "
            f"failure; got {entry['state']!r}"
        )

    def test_worker_noop_keeps_finding_active(self, tmp_path) -> None:
        """Scenario 2: Head A has F -> worker launches but
        pushes nothing (no head advance) -> next round on A
        STILL contains F.
        """
        loop, store = self._setup_loop(tmp_path)
        snap_a = self._snap_with_finding("a" * 40, 99, "P1 finding")
        loop.run_once(
            snap_a, head_sha="a" * 40,
            repo="owner/repo", pr_number=4,
        )
        # "Worker launches but pushes nothing" — the head
        # stays at A; the controller stays in
        # REPAIRING_REVIEW_FINDINGS; the relay's next round
        # sees the same head.
        snap_again = self._snap_with_finding("a" * 40, 99, "P1 finding")
        d2 = loop.run_once(
            snap_again, head_sha="a" * 40,
            repo="owner/repo", pr_number=4,
        )
        assert d2.action == "launch_worker", (
            f"worker that pushes nothing MUST leave F in the "
            f"next directive; got {d2.action!r}"
        )

    def test_head_advance_supersedes_finding_on_old_head_only(
        self, tmp_path: Path,
    ) -> None:
        """Round-28 P5: ``mark_head_advanced`` promotes the prior
        ACTIVE / DISPATCHED entries to SUPERSEDED on the OLD
        head. Round-28 P5 explicitly forbids the round-27
        behavior where the same finding observed on the NEW
        head was silenced by the SUPERSEDED row on the OLD
        head. The ledger MUST remain ACTIVE for B/F until
        positive cross-head resolution evidence arrives.

        Sequence:
          Head A: finding F is ACTIVE
          Worker pushes head B; ``mark_head_advanced`` is called
          ``mark_head_advanced`` promotes A/F to SUPERSEDED
          Reviewer reports identical F on B
          Round-28 P5: B/F is FRESH — the directive MUST
          contain F, qualification is blocked.
        """
        loop, store = self._setup_loop(tmp_path)
        snap_a = self._snap_with_finding("a" * 40, 99, "P1 finding")
        loop.run_once(
            snap_a, head_sha="a" * 40,
            repo="owner/repo", pr_number=4,
        )
        # Worker pushes head B. The supervisor calls
        # ``mark_head_advanced`` which (a) promotes prior
        # ACTIVE / DISPATCHED entries to SUPERSEDED on the OLD
        # head and (b) binds the controller to AWAITING_CI.
        loop.mark_head_advanced("a" * 40, "b" * 40)
        from autocoder_orchestration.review_repair_relay import (
            FindingLedger, FINDING_STATE_SUPERSEDED,
        )
        # The ledger promoted the prior entry to SUPERSEDED
        # on the OLD head.
        ledger_a = FindingLedger(store, head_sha="a" * 40)
        entries = ledger_a.load()
        assert entries["coderabbit:99"]["state"] == FINDING_STATE_SUPERSEDED, (
            f"mark_head_advanced MUST promote prior ACTIVE/DISPATCHED "
            f"entries to SUPERSEDED on the OLD head; got "
            f"{entries['coderabbit:99']['state']!r}"
        )
        # Round-28 P5: on head B with the SAME signature, the
        # finding is FRESH. The cross-head SUPERSEDED on A is
        # NOT positive cross-head resolution evidence.
        ledger_b = FindingLedger(store, head_sha="b" * 40)
        from autocoder_orchestration.review_repair_relay import Finding
        f_b = Finding(
            finding_id="coderabbit:99",
            source="coderabbit",
            severity="P1",
            title="P1 finding",
            body="P1 finding",
            file_path=None, line=None, url=None,
            suggested_test=None, review_id=None,
            comment_id=99, check_name=None,
        )
        assert ledger_b.is_fresh(f_b) is True, (
            "Round-28 P5: on head B with the same body, the "
            "finding MUST be FRESH (no positive cross-head "
            "resolution evidence); the SUPERSEDED row on A "
            "does NOT silence B/F."
        )

    def test_fresh_evidence_at_new_head_reopens(self, tmp_path) -> None:
        """At the new head B, fresh evidence (different
        signature) IS emitted — the SUPERSEDED row at A does
        not silence the new evidence at B.
        """
        loop, store = self._setup_loop(tmp_path)
        snap_a = self._snap_with_finding("a" * 40, 99, "v1 body")
        loop.run_once(
            snap_a, head_sha="a" * 40,
            repo="owner/repo", pr_number=4,
        )
        loop.mark_head_advanced("a" * 40, "b" * 40)
        from autocoder_orchestration.review_repair_relay import Finding, FindingLedger
        ledger_b = FindingLedger(store, head_sha="b" * 40)
        f_b = Finding(
            finding_id="coderabbit:99",
            source="coderabbit",
            severity="P1",
            title="",
            body="v2 body",
            file_path=None, line=None, url=None,
            suggested_test=None, review_id=None,
            comment_id=99, check_name=None,
        )
        assert ledger_b.is_fresh(f_b) is True, (
            "different signature at the new head IS fresh; "
            "the SUPERSEDED row only shadows the original signature"
        )


class TestFindingLedgerPersistsAcrossRounds:
    """End-to-end: ``RelayLoop.run_once`` writes the ledger;
    the next round on the same head sees the finding still
    ACTIVE (round-27 invariant). The directive is issued
    every round the finding remains ACTIVE; only
    SUPERSEDED / REPAIRED transition is terminal.
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

    def test_active_finding_stays_in_directive_each_round(self, tmp_path) -> None:
        """Round-27 invariant: ACTIVE findings remain in the
        directive every round on the same head. The relay
        does NOT mark them consumed merely because a
        directive was emitted. qualification is impossible
        while ACTIVE findings exist.
        """
        loop, store = self._setup_loop(tmp_path)
        snap = {
            "head_sha": "a" * 40, "head_match": True,
            "review_comments": [], "issue_comments": [],
            "_provider_issue_comments": {
                "coderabbit": [
                    {"id": 99, "body": "P1 finding", "html_url": "u99", "commit_id": "a" * 40},
                ],
            },
            "required_checks": {},
        }
        d1 = loop.run_once(
            snap, head_sha="a" * 40, repo="owner/repo", pr_number=4,
        )
        assert d1.action == "launch_worker"
        d2 = loop.run_once(
            snap, head_sha="a" * 40, repo="owner/repo", pr_number=4,
        )
        assert d2.action == "launch_worker", (
            f"round-27 invariant: ACTIVE finding on same head "
            f"MUST be re-emitted; got {d2.action!r}"
        )
        # Round-26 test was: action == "enter_qualifying_readiness".
        # That was the round-26 bug: the ledger marked the
        # finding consumed on dispatch. Round-27 explicitly
        # forbids that.

    def test_body_edit_on_same_head_reopens(self, tmp_path) -> None:
        """A body edit on the same head MUST reopen the finding.
        """
        loop, _store = self._setup_loop(tmp_path)
        snap_v1 = {
            "head_sha": "a" * 40, "head_match": True,
            "review_comments": [], "issue_comments": [],
            "_provider_issue_comments": {
                "coderabbit": [{"id": 99, "body": "v1 body", "html_url": "u", "commit_id": "a" * 40}],
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
                "coderabbit": [{"id": 99, "body": "v2 body", "html_url": "u", "commit_id": "a" * 40}],
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
                "coderabbit": [{"id": 1, "body": "x", "html_url": "u", "commit_id": "a" * 40}],
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
                    {"id": 1, "body": "x", "html_url": "u", "commit_id": "a" * 40},
                    {"id": 2, "body": "y", "html_url": "u2", "commit_id": "a" * 40},
                ],
            },
            "required_checks": {},
        }
        d2 = loop.run_once(
            snap_v2, head_sha="a" * 40, repo="owner/repo", pr_number=4,
        )
        # The new comment must be emitted (different finding_id).
        assert d2.action == "launch_worker"




# ---------------------------------------------------------------------------
# Round-45 C13: focused-thread directive scoping
# ---------------------------------------------------------------------------
#
# These tests reproduce the round-45 defect where the
# supervisor repeatedly dispatched the durable-thread-drain
# event for the same review thread (XpixA) and the worker
# emitted a generic head-level NO-OP because the directive
# did not contain the targeted thread. The relay classified
# the 8 historical P1 findings ALREADY_SATISFIED but never
# produced a per-thread disposition for XpixA, so the drain
# event remained in the runnable queue and was dispatched
# again on the next heartbeat.


def _make_thread_snapshot(thread_id: str, head_sha: str = "f" * 40) -> dict:
    """Build a minimal snapshot carrying one unresolved thread
    bound to ``head_sha``. The thread has a real body and
    path so it is actionable by the durable-thread-drain
    heuristic.
    """
    return {
        "head_sha": head_sha,
        "head_match": True,
        "review_threads": {
            thread_id: {
                "author": "coderabbitai",
                "body": (
                    "_Functional Correctness_ | _Quick win_\n\n"
                    "The current implementation can be simplified. "
                    "Inspect tests/test_autocoder_supervisor.py:165."
                ),
                "commit_oid": head_sha,
                "line": 165,
                "outdated": False,
                "path": "tests/test_autocoder_supervisor.py",
                "resolved": False,
            },
        },
        "review_comments": [],
        "issue_comments": [],
        "_provider_issue_comments": {},
        "required_checks": {},
    }


def test_round45_c13_focused_thread_directive_carries_only_target():
    """Round-45 C13: when ``focused_thread_id`` is set, the
    directive MUST contain exactly one finding whose
    ``finding_id`` is ``"thread:<tid>"``. The historical
    8-P1 backlog MUST NOT appear in the directive, even when
    the snapshot also carries the 8 historical CodeRabbit
    inline comments.
    """
    from autocoder_orchestration.review_repair_relay import (
        collect_findings, build_directive,
    )

    thread_id = "PRRT_kwDOTtyQLc6XpixA"
    head_sha = "f" * 40
    snap = _make_thread_snapshot(thread_id, head_sha)
    # Add historical 8-P1 comments to the snapshot. Without
    # the focused-thread scope, the directive would carry
    # these P1s and skip the targeted P2 thread.
    snap["_provider_issue_comments"] = {
        "coderabbit": [
            {"id": 100 + i, "body": "P1 historical", "commit_id": head_sha}
            for i in range(8)
        ],
    }
    # Test collect_findings directly with the focused thread
    findings = collect_findings(snap, focused_thread_id=thread_id)
    assert len(findings) == 1, (
        f"round-45 C13: focused_thread_id MUST scope "
        f"collector to a single finding; got {len(findings)}"
    )
    assert findings[0].finding_id == f"thread:{thread_id}"
    # Test build_directive enforces the single-finding contract.
    d = build_directive(
        round_index=1,
        head_sha=head_sha,
        repo="Slideshow11/AutoDev",
        pr_number=5,
        findings=findings,
        coordinator_actor="controller",
        target_thread_id=thread_id,
    )
    assert d.target_thread_id == thread_id
    assert len(d.findings) == 1
    assert d.findings[0].finding_id == f"thread:{thread_id}"


def test_round45_c13_focused_thread_directive_violates_contract():
    """Round-45 C13: build_directive MUST reject a directive
    where ``target_thread_id`` is set but ``findings`` is empty
    or contains the wrong thread. The contract enforces that
    the targeted directive is scoped to exactly one finding
    whose ``finding_id`` is ``"thread:<target_thread_id>"``.
    """
    from autocoder_orchestration.review_repair_relay import (
        build_directive, Finding,
        SEVERITY_P1, SEVERITY_P2,
        DirectiveContractError,
    )

    f_wrong = Finding(
        finding_id="thread:OTHER_THREAD",
        source="review_thread",
        severity=SEVERITY_P2,
        title="wrong thread",
        body="x",
        file_path=None,
        line=None,
        url=None,
        suggested_test=None,
        review_id=None,
        comment_id=None,
        check_name=None,
    )
    try:
        build_directive(
            round_index=1,
            head_sha="a" * 40,
            repo="o/r",
            pr_number=5,
            findings=[f_wrong],
            coordinator_actor="controller",
            target_thread_id="PRRT_kwDOTtyQLc6XpixA",
        )
    except DirectiveContractError:
        pass
    else:
        raise AssertionError(
            "round-45 C13: build_directive MUST reject a directive "
            "whose findings[0].finding_id does not match "
            "thread:<target_thread_id>"
        )

    # Empty findings list also rejected (independent of focus).
    try:
        build_directive(
            round_index=1,
            head_sha="a" * 40,
            repo="o/r",
            pr_number=5,
            findings=[],
            coordinator_actor="controller",
            target_thread_id="PRRT_kwDOTtyQLc6XpixA",
        )
    except DirectiveContractError:
        pass
    else:
        raise AssertionError(
            "round-45 C13: build_directive MUST reject empty findings"
        )


def test_round45_c13_prompt_carries_target_thread_id(
    tmp_path,
):
    """Round-45 C13: the worker prompt MUST include the
    targeted thread id so the worker knows to scope its
    investigation. The prompt also MUST carry the round-45
    C13 SCOPING line so the worker does not spend its tool
    budget re-auditing the historical 8-P1 backlog.
    """
    from autocoder_orchestration.review_repair_relay import (
        Finding, SEVERITY_P2, build_directive, build_worker_prompt,
        RoundDecision,
    )
    f = Finding(
        finding_id="thread:PRRT_kwDOTtyQLc6XpixA",
        source="review_thread",
        severity=SEVERITY_P2,
        title="test",
        body="x",
        file_path="tests/test_autocoder_supervisor.py",
        line=165,
        url=None,
        suggested_test=None,
        review_id=None,
        comment_id=None,
        check_name=None,
    )
    d = build_directive(
        round_index=1,
        head_sha="a" * 40,
        repo="o/r",
        pr_number=5,
        findings=[f],
        coordinator_actor="controller",
        target_thread_id="PRRT_kwDOTtyQLc6XpixA",
    )
    decision = RoundDecision(
        action="launch_worker",
        round_index=1,
        head_sha="a" * 40,
        outcome="completed",
        p1_count=0, p2_count=1, ci_failure_count=0,
        escalate_reasons=(),
        directive=d,
        directive_digest="abc",
    )
    prompt = build_worker_prompt(decision)
    assert "PRRT_kwDOTtyQLc6XpixA" in prompt, (
        "round-45 C13: worker prompt MUST include the targeted "
        "thread id so the worker scopes its investigation"
    )
    assert "ROUND-45 C13 SCOPING" in prompt, (
        "round-45 C13: worker prompt MUST include the round-45 "
        "C13 SCOPING line so the worker knows to focus on the "
        "targeted thread rather than re-auditing historical P1s"
    )


def test_round45_c13_no_focused_thread_returns_broad_directive(
    tmp_path,
):
    """Round-45 C13: when ``focused_thread_id`` is NOT set, the
    collector behaves exactly as before: returns the full set
    of historical findings (filtered by current-head rule).
    The directive's ``target_thread_id`` is None.
    """
    from autocoder_orchestration.review_repair_relay import (
        build_directive, collect_findings,
        Finding, SEVERITY_P1,
    )
    head_sha = "f" * 40
    snap = _make_thread_snapshot("PRRT_kwDOTtyQLc6XpixA", head_sha)
    snap["_provider_issue_comments"] = {
        "coderabbit": [
            {"id": 100, "body": "P1 historical", "commit_id": head_sha},
        ],
    }
    # No focused_thread_id — collector returns ALL findings.
    findings = collect_findings(snap)
    assert len(findings) > 1, (
        "round-45 C13: when focused_thread_id is None, the "
        "collector returns the full set of findings"
    )
    # Directive carries no target_thread_id.
    d = build_directive(
        round_index=1,
        head_sha=head_sha,
        repo="o/r",
        pr_number=5,
        findings=findings,
        coordinator_actor="controller",
    )
    assert d.target_thread_id is None


def test_round45_c13_focused_thread_resolves_against_head():
    """Round-45 C13: when the targeted thread has a body and
    a path (actionable content), the current-head binding
    rule does NOT apply. The thread is accepted regardless of
    commit_oid. Only threads with NO body and NO path AND a
    stale commit_oid are excluded.
    """
    from autocoder_orchestration.review_repair_relay import collect_findings

    thread_id = "PRRT_kwDOTtyQLc6XpixA"
    # Thread has a real body and path; commit_oid is stale.
    stale_head = "a" * 40
    live_head = "f" * 40
    snap = _make_thread_snapshot(thread_id, head_sha=stale_head)
    snap["head_sha"] = live_head
    findings = collect_findings(snap, focused_thread_id=thread_id)
    # Body + path present => current-head binding does NOT
    # gate; thread is still actionable.
    assert len(findings) == 1, (
        "round-45 C13: threads with body+path are actionable "
        "even when commit_oid is stale (the snapshot already "
        "filters to the live head's review API)"
    )

    # Now test the stale-head exclusion: no body, no path, stale commit_oid.
    snap_blank = {
        "head_sha": live_head,
        "head_match": True,
        "review_threads": {
            thread_id: {
                "author": "coderabbitai",
                "body": "",
                "commit_oid": stale_head,
                "line": None,
                "outdated": False,
                "path": "",
                "resolved": False,
            },
        },
        "review_comments": [],
        "issue_comments": [],
        "_provider_issue_comments": {},
        "required_checks": {},
    }
    findings = collect_findings(snap_blank, focused_thread_id=thread_id)
    assert findings == [], (
        "round-45 C13: collector MUST exclude threads with no body, "
        "no path, AND a stale commit_oid (no current-head binding)"
    )
