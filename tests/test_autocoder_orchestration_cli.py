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
