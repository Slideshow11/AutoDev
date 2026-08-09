"""Production-path behavioral tests for the relay wiring.

These tests do NOT mock away argparse. They construct the
real CLI invocation produced by ``invoke_relay_round`` and
run it through the actual ``autocoder_orchestration.cli``
argparse parser. The goal is to prove that the supervisor's
production invocation actually executes the relay, not just
that the wiring builds a syntactically plausible argv.

The round-1 integration defect was exactly this: the
wiring built ``autocoder-orchestration review-repair-round
--json``, but ``--json`` is a top-level flag. argparse
rejected the invocation with rc=2 and the supervisor fell
back to the legacy generic-worker path. These tests catch
the same shape of regression.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


from autocoder_orchestration.review_repair_relay import (
    RELAY_SCHEMA_VERSION,
    ReviewDirective,
    build_directive,
)
from autocoder_supervisor.relay_wiring import (
    DEFAULT_RELAY_CLI,
    DEFAULT_RELAY_SUBCOMMAND,
    RelayWiringError,
    invoke_relay_round,
)


def _write_directive_with_digest(target: Path, directive: dict) -> dict:
    """Write a directive with a valid _sha256 sidecar value."""
    canonical_fields = {k: v for k, v in directive.items() if k != "_sha256"}
    canonical = json.dumps(
        canonical_fields, sort_keys=True, separators=(",", ":"),
    )
    digest = __import__("hashlib").sha256(
        canonical.encode("utf-8"),
    ).hexdigest()
    payload = dict(directive)
    payload["_sha256"] = digest
    target.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def _init_run_context(state_root: Path, evidence_root: Path, head_sha: str) -> None:
    """Write a real ``run_context.json`` and state machine to
    ``state_root`` so the relay CLI loads them via the
    StateStore.

    The relay's ``cmd_review_repair_round`` reads
    ``run_context.json`` directly and refuses to run when
    one is absent. The state machine MUST be in
    REPAIRING_REVIEW_FINDINGS for the relay to accept the
    round. The context fields mirror the production layout
    (PR 4, base main, head <a*40>).
    """
    state_root.mkdir(parents=True, exist_ok=True)
    evidence_root.mkdir(parents=True, exist_ok=True)
    ctx = {
        "schema_version": "autocoder.run_context.v1",
        "run_id": "r1",
        "repo_owner": "owner",
        "repo_name": "repo",
        "local_checkout": str(state_root),
        "base_branch": "main",
        "authorized_base_sha": "a" * 64,
        "feature_branch": "feat/test",
        "task_specification_path": "/tmp/task",
        "task_specification_sha256": "b" * 64,
        "required_ci_jobs": [],
        "implementation_worker_command": [],
        "evidence_root": str(evidence_root),
        "state_root": str(state_root),
        "pr_number": 4,
        "current_authorized_head": head_sha,
    }
    with open(state_root / "run_context.json", "w") as f:
        json.dump(ctx, f)
    os.chmod(state_root / "run_context.json", 0o600)
    # Initialize the state machine in REPAIRING_REVIEW_FINDINGS.
    sm_path = state_root / "state.json"
    sm_payload = {
        "schema_version": "autocoder.state_machine.v1",
        "current_state": "REPAIRING_REVIEW_FINDINGS",
        "revision": 1,
        "expected_revision": 0,
        "head_observed": head_sha,
        "transitions": [],
        "journal": [],
        "evidence": {},
    }
    with open(sm_path, "w") as f:
        json.dump(sm_payload, f)
    os.chmod(state_root / "run_context.json", 0o600)
    os.chmod(sm_path, 0o600)


def _snapshot_clean(head_sha: str) -> dict:
    """A snapshot bound to the head that has no findings."""
    return {
        "captured_at": "2026-08-08T00:00:00Z",
        "head_sha": head_sha,
        "head_match": True,
        "mergeable": True,
        "formal_reviews": [],
        "review_threads": {},
        "issue_comments": [],
        "required_checks": {},
        "providers": [],
        "_provider_issue_comments": {},
        "unconsumed_event_ids": [],
    }


def _snapshot_with_finding(head_sha: str) -> dict:
    """A snapshot with one actionable CodeRabbit finding."""
    return {
        "captured_at": "2026-08-08T00:00:00Z",
        "head_sha": head_sha,
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


class TestInvokeRelayRoundProductionPath:
    """The relay wiring's subprocess invocation must reach the
    real CLI parser. The argv shape is fixed: the binary
    path, then ``--json`` (a top-level flag), then the
    subcommand, then the subcommand's args.

    The defect that motivated this test class was the
    argv ``autocoder-orchestration review-repair-round --json``,
    which places ``--json`` AFTER the subcommand and is
    rejected by argparse.
    """

    def _init(self, tmp_path: Path, head_sha: str):
        state_root = tmp_path / "state"
        evidence_root = tmp_path / "evidence"
        _init_run_context(state_root, evidence_root, head_sha)
        return state_root, evidence_root

    def test_invoke_clean_head_returns_qualifying_action(
        self, tmp_path: Path,
    ) -> None:
        """A clean head returns ``enter_qualifying_readiness``."""
        head_sha = "a" * 40
        state_root, evidence_root = self._init(tmp_path, head_sha)
        snapshot = _snapshot_clean(head_sha)
        decision = invoke_relay_round(
            snapshot=snapshot,
            head_sha=head_sha,
            state_root=str(state_root),
            run_id="r1",
            pr_number=4,
            evidence_root=str(evidence_root),
            required_check_names=(),
        )
        assert decision["action"] == "enter_qualifying_readiness"
        assert decision["head_sha"] == head_sha

    def test_invoke_finding_returns_launch_worker_action(
        self, tmp_path: Path,
    ) -> None:
        """A actionable finding returns ``launch_worker`` and
        persists the directive to the canonical evidence root.
        """
        head_sha = "a" * 40
        state_root, evidence_root = self._init(tmp_path, head_sha)
        snapshot = _snapshot_with_finding(head_sha)
        decision = invoke_relay_round(
            snapshot=snapshot,
            head_sha=head_sha,
            state_root=str(state_root),
            run_id="r1",
            pr_number=4,
            evidence_root=str(evidence_root),
            required_check_names=(),
        )
        assert decision["action"] == "launch_worker"
        assert decision["head_sha"] == head_sha
        # The directive must be on disk for the bridge to pick it up.
        directive_path = evidence_root / "directive.json"
        assert directive_path.is_file()

    def test_invoke_via_real_cli_subprocess(
        self, tmp_path: Path,
    ) -> None:
        """Spawn the real CLI binary directly with the same
        argv the wiring produces. This is the round-1
        regression test that proves the production argv
        is accepted by argparse.
        """
        head_sha = "a" * 40
        state_root, evidence_root = self._init(tmp_path, head_sha)
        # Pre-stage the snapshot file the same way the wiring
        # would write it.
        snapshot_path = state_root / "live_snapshot.json"
        snapshot = _snapshot_clean(head_sha)
        snapshot_path.write_text(json.dumps(snapshot, sort_keys=True))
        os.chmod(snapshot_path, 0o600)
        # Build the production argv explicitly. The shell
        # form is the universal fallback when the entry
        # console_script is not installed on PATH.
        cli = f"{sys.executable} -m autocoder_orchestration.cli"
        parts = shlex.split(cli) + [
            "--json", "review-repair-round",
            "--state-root", str(state_root),
            "--run-id", "r1",
            "--snapshot-file", str(snapshot_path),
            "--head-sha", head_sha,
            "--evidence-root", str(evidence_root),
            "--required-check-names", "",
        ]
        proc = subprocess.run(
            parts, capture_output=True, text=True, timeout=30,
        )
        # The exact defect: --json placed after the
        # subcommand. Verify the CLI does NOT return the
        # argparse error.
        assert "unrecognized arguments" not in proc.stderr, (
            f"CLI rejected argv with --json after subcommand: "
            f"rc={proc.returncode} stderr={proc.stderr[:400]}"
        )
        # Parse the JSON output and verify the action.
        assert proc.returncode == 0, (
            f"CLI failed: rc={proc.returncode} stderr={proc.stderr[:400]}"
        )
        decision = json.loads(proc.stdout)
        assert decision["action"] == "enter_qualifying_readiness"

    def test_invoke_returns_escalation_when_p0_present(
        self, tmp_path: Path,
    ) -> None:
        """A P0 finding causes the relay to block the run;
        the CLI surfaces this as a structured
        ``action == escalate_to_human`` decision with
        populated ``escalate_reasons``.
        """
        head_sha = "a" * 40
        state_root, evidence_root = self._init(tmp_path, head_sha)
        snapshot = {
            "captured_at": "2026-08-08T00:00:00Z",
            "head_sha": head_sha,
            "head_match": True,
            "mergeable": True,
            "formal_reviews": [],
            "review_threads": {},
            "issue_comments": [],
            "required_checks": {},
            "providers": [],
            "_provider_issue_comments": {
                "coderabbit": [
                    {"id": 1, "body": "P0: critical: stop the run"},
                ],
            },
            "unconsumed_event_ids": [],
        }
        decision = invoke_relay_round(
            snapshot=snapshot,
            head_sha=head_sha,
            state_root=str(state_root),
            run_id="r1",
            pr_number=4,
            evidence_root=str(evidence_root),
            required_check_names=(),
        )
        assert decision["action"] == "escalate_to_human"
        assert len(decision["escalate_reasons"]) >= 1

    def test_invoke_rejects_stale_snapshot(
        self, tmp_path: Path,
    ) -> None:
        """A snapshot whose head_sha differs from the
        requested head is rejected with ``InvalidSnapshot``;
        the CLI surfaces this as a non-zero exit.
        """
        head_sha = "a" * 40
        stale_head = "b" * 40
        state_root, evidence_root = self._init(tmp_path, head_sha)
        snapshot = _snapshot_clean(stale_head)
        with pytest.raises(RelayWiringError) as exc:
            invoke_relay_round(
                snapshot=snapshot,
                head_sha=head_sha,
                state_root=str(state_root),
                run_id="r1",
                pr_number=4,
                evidence_root=str(evidence_root),
                required_check_names=(),
            )
        # A stale snapshot is rejected (non-zero exit). The
        # CLI maps InvalidSnapshot to either EXIT_STATE (4)
        # or EXIT_INTERNAL (5) depending on the trap.
        assert exc.value.returncode != 0, (
            f"stale snapshot must be rejected with non-zero exit, "
            f"got rc={exc.value.returncode}"
        )


class TestInvokeRelayRoundArgvShape:
    """The argv shape produced by ``invoke_relay_round`` is
    the supervisor's production contract. Verify it matches
    what the real CLI parser accepts.
    """

    def test_argv_starts_with_binary_then_json_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Capture the argv that subprocess.run receives.
        captured: dict = {}
        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            class _R:
                returncode = 0
                stdout = json.dumps({"action": "enter_qualifying_readiness"})
                stderr = ""
            return _R()
        monkeypatch.setattr(
            "autocoder_supervisor.relay_wiring.subprocess.run",
            fake_run,
        )
        # Pin the cli to a single-token binary so the
        # shell-split path is not taken.
        monkeypatch.setattr(
            "autocoder_supervisor.relay_wiring._resolve_cli_executable",
            lambda: "/usr/bin/false",
        )
        head_sha = "a" * 40
        state_root = tmp_path / "state"
        evidence_root = tmp_path / "evidence"
        state_root.mkdir()
        evidence_root.mkdir()
        invoke_relay_round(
            snapshot=_snapshot_clean(head_sha),
            head_sha=head_sha,
            state_root=str(state_root),
            run_id="r1",
            pr_number=4,
            evidence_root=str(evidence_root),
            required_check_names=(),
        )
        cmd = captured["cmd"]
        # The defect would be: cmd == ["/usr/bin/false",
        # "review-repair-round", ..., "--json"]. The fix:
        # cmd == ["/usr/bin/false", "--json",
        # "review-repair-round", ...].
        assert cmd[0] == "/usr/bin/false"
        assert cmd[1] == "--json", (
            f"--json must be at index 1 (top-level), got: {cmd[:3]}"
        )
        assert cmd[2] == DEFAULT_RELAY_SUBCOMMAND, (
            f"subcommand must be at index 2, got: {cmd[:3]}"
        )
        # No --json after the subcommand.
        sub_idx = cmd.index(DEFAULT_RELAY_SUBCOMMAND)
        assert "--json" not in cmd[sub_idx + 1:], (
            f"--json must not appear after subcommand: {cmd[sub_idx + 1:]}"
        )

    def test_argv_with_shell_split_binary_also_correct(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ``AED_RELAY_CLI`` env var may carry a shell
        command like ``python -m autocoder_orchestration.cli``.
        The wiring must still produce an argv with
        ``--json`` BEFORE the subcommand.
        """
        captured: dict = {}
        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            class _R:
                returncode = 0
                stdout = json.dumps({"action": "enter_qualifying_readiness"})
                stderr = ""
            return _R()
        monkeypatch.setattr(
            "autocoder_supervisor.relay_wiring.subprocess.run",
            fake_run,
        )
        monkeypatch.setattr(
            "autocoder_supervisor.relay_wiring._resolve_cli_executable",
            lambda: "/usr/bin/python3 -m autocoder_orchestration.cli",
        )
        head_sha = "a" * 40
        state_root = tmp_path / "state"
        evidence_root = tmp_path / "evidence"
        state_root.mkdir()
        evidence_root.mkdir()
        invoke_relay_round(
            snapshot=_snapshot_clean(head_sha),
            head_sha=head_sha,
            state_root=str(state_root),
            run_id="r1",
            pr_number=4,
            evidence_root=str(evidence_root),
            required_check_names=(),
        )
        cmd = captured["cmd"]
        # The first 4 entries are the shell-split binary.
        assert cmd[:4] == [
            "/usr/bin/python3", "-m", "autocoder_orchestration.cli", "--json",
        ], (
            f"--json must follow the shell-split binary, got: {cmd[:5]}"
        )
        assert cmd[4] == DEFAULT_RELAY_SUBCOMMAND
        # No --json after the subcommand.
        sub_idx = cmd.index(DEFAULT_RELAY_SUBCOMMAND)
        assert "--json" not in cmd[sub_idx + 1:]
