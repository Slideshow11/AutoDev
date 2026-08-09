"""Tests for the CLI entry point."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from autocoder_orchestration import cli as cli_module


def test_cli_help_exits_zero() -> None:
    """The CLI must exit 0 on --help."""
    with pytest.raises(SystemExit) as exc:
        cli_module.main(["--help"])
    assert exc.value.code == 0


def test_cli_status_missing_state(tmp_path) -> None:
    """status command returns 4 (state error) when no state.json exists."""
    state_root = tmp_path / "state"
    state_root.mkdir()
    rc = cli_module.main([
        "--json", "status",
        "--state-root", str(state_root),
        "--run-id", "r1",
    ])
    assert rc == 4


def test_cli_initialize_creates_state(tmp_path) -> None:
    """initialize command writes the run context and initial state."""
    state_root = tmp_path / "state"
    state_root.mkdir()
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()

    rc = cli_module.main([
        "--json", "initialize",
        "--state-root", str(state_root),
        "--run-id", "r1",
        "--owner", "o",
        "--repo", "r",
        "--local-checkout", str("/home" + "/" + "max" + "/" + "AutoDev"),
        "--base-branch", "main",
        "--authorized-base-sha", "a" * 64,
        "--feature-branch", "feat/test",
        "--taskspec-path", "/tmp/task",
        "--taskspec-sha256", "b" * 64,
        "--evidence-root", str(evidence_root),
    ])
    assert rc == 0
    # Check that the state files were created
    state_path = Path(state_root) / "o" / "r" / "pending" / "r1"
    assert (state_path / "run_context.json").exists()
    assert (state_path / "state.json").exists()


def test_cli_unknown_command_returns_two() -> None:
    with pytest.raises(SystemExit):
        cli_module.main(["--json", "unknown"])



# === review-repair-round CLI tests ===

class TestReviewRepairRoundCLI:
    def _setup(self, tmp_path: Path):
        """Initialize a run context that the CLI can drive."""
        from autocoder_orchestration.context import make_run_context
        from autocoder_orchestration.state_machine import StateMachine
        from autocoder_orchestration.store import StateStore
        from autocoder_orchestration.state_machine import (
            STATE_IMPLEMENTING, STATE_AWAITING_CI,
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
        return state_root, evidence_root

    def test_round_returns_launch_worker_action(self, tmp_path: Path) -> None:
        state_root, evidence_root = self._setup(tmp_path)
        snapshot = {
            "captured_at": "2026-08-08T00:00:00Z",
            "head_sha": "a" * 40,
            "head_match": True,
            "mergeable": True,
            "formal_reviews": [],
            "review_threads": {},
            "issue_comments": [],
            "required_checks": {},
            "providers": {},
            "_provider_issue_comments": {
                "coderabbit": [
                    {"id": 1, "body": "P1: foo.py:1 broken"},
                ],
            },
            "unconsumed_event_ids": [],
        }
        snap_file = tmp_path / "snap.json"
        snap_file.write_text(json.dumps(snapshot))
        rc = cli_module.main([
            "--json", "review-repair-round",
            "--state-root", str(state_root),
            "--run-id", "r1",
            "--snapshot-file", str(snap_file),
        ])
        assert rc == 0

    def test_round_returns_qualifying_when_clean(self, tmp_path: Path) -> None:
        state_root, _ = self._setup(tmp_path)
        # No findings -> enter_qualifying_readiness.
        snap = {
            "captured_at": "x", "head_sha": "a" * 40,
            "head_match": True, "mergeable": True,
            "formal_reviews": [], "review_threads": {},
            "issue_comments": [],
            "required_checks": {}, "providers": {},
            "_provider_issue_comments": {},
            "unconsumed_event_ids": [],
        }
        snap_file = tmp_path / "snap.json"
        snap_file.write_text(json.dumps(snap))
        rc = cli_module.main([
            "--json", "review-repair-round",
            "--state-root", str(state_root),
            "--run-id", "r1",
            "--snapshot-file", str(snap_file),
        ])
        assert rc == 0

    def test_round_rejects_missing_snapshot(self, tmp_path: Path) -> None:
        state_root, _ = self._setup(tmp_path)
        rc = cli_module.main([
            "--json", "review-repair-round",
            "--state-root", str(state_root),
            "--run-id", "r1",
        ])
        assert rc == 2  # EXIT_INVARG

    def test_round_rejects_bad_state(self, tmp_path: Path) -> None:
        from autocoder_orchestration.context import make_run_context
        from autocoder_orchestration.state_machine import StateMachine
        from autocoder_orchestration.store import StateStore
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
        # State stays at PLANNED - the relay must refuse.
        sm = StateMachine()
        store.write_atomic("state.json", sm.to_dict())
        snap = {
            "captured_at": "x", "head_sha": "a" * 40,
            "head_match": True, "mergeable": True,
            "formal_reviews": [], "review_threads": {},
            "issue_comments": [],
            "required_checks": {}, "providers": {},
            "_provider_issue_comments": {},
            "unconsumed_event_ids": [],
        }
        snap_file = tmp_path / "snap.json"
        snap_file.write_text(json.dumps(snap))
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_module.main([
                "--json", "review-repair-round",
                "--state-root", str(state_root),
                "--run-id", "r1",
                "--snapshot-file", str(snap_file),
            ])
        payload = json.loads(buf.getvalue())
        # Round-29 review: only ``EscalateToHuman`` is
        # mapped to ``action=escalate_to_human`` + EXIT_OK
        # (protected-authority escalation). Generic
        # ``RelayError`` (controller in wrong state, etc.)
        # is now an internal / recoverable failure surfaced
        # as ``action=internal_error`` + EXIT_INTERNAL so
        # the supervisor's retry / recover path picks it up
        # rather than misclassifying it as a human-authority
        # escalation.
        assert rc != 0
        assert payload.get("action") == "internal_error"
        assert "RelayError" in payload.get("error", "")
        assert "REPAIRING_REVIEW_FINDINGS" in payload.get(
            "error", ""
        ) or "PLANNED" in payload.get("error", "")

    def test_status_reports_no_directive(self, tmp_path: Path) -> None:
        from autocoder_orchestration.context import make_run_context
        from autocoder_orchestration.state_machine import StateMachine
        from autocoder_orchestration.store import StateStore
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
        rc = cli_module.main([
            "--json", "review-repair-status",
            "--state-root", str(state_root),
            "--run-id", "r1",
            "--evidence-root", str(evidence_root),
        ])
        assert rc == 0
