"""Round-678 regression tests for the ``--required-check-names``
flag in ``cmd_review_repair_round``.

The defect (CodeRabbit P2): when the supervisor explicitly
passes ``--required-check-names ""`` to express "operator
intentionally wants no required checks", the CLI previously
treated the explicit empty value exactly like the omitted
flag and resurrected ``ctx.required_ci_jobs`` — older
persisted checks re-surfaced and continued to generate
missing/pending findings on heads the operator had meant to
declare clean of required-check obligations.

The fix distinguishes three cases at the CLI parser layer:

  * flag absent (``args.required_check_names is None``):
    fall back to ``ctx.required_ci_jobs``.
  * flag present with explicit empty value (``""``):
    honour the override — do NOT resurrect
    ``ctx.required_ci_jobs``; keep the override empty.
  * flag present with comma-separated names:
    use them verbatim, splitting on ``,`` and dropping empties.

These tests lock in the contract end-to-end via the real
CLI subprocess: a snapshot whose ``required_checks``
omits a check the operator configured as required must
either produce a CI-failure finding (override not in
effect) or must NOT (override in effect). That is the
observable behaviour the supervisor relies on.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest


def _seed_run_context(
    state_root: Path,
    evidence_root: Path,
    head_sha: str,
    *,
    required_ci_jobs: list,
) -> None:
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
        "required_ci_jobs": required_ci_jobs,
        "implementation_worker_command": [],
        "evidence_root": str(evidence_root),
        "state_root": str(state_root),
        "pr_number": 4,
        "current_authorized_head": head_sha,
    }
    with open(state_root / "run_context.json", "w") as f:
        json.dump(ctx, f)
    os.chmod(state_root / "run_context.json", 0o600)
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
    sm_path = state_root / "state.json"
    with open(sm_path, "w") as f:
        json.dump(sm_payload, f)
    os.chmod(sm_path, 0o600)


def _snapshot_clean(head_sha: str) -> dict:
    """A snapshot whose ``required_checks`` does NOT contain
    the configured required checks — i.e. the supervisor
    did not fetch them. If the relay still treats those
    names as required, the decision must be
    ``launch_worker`` (a CI failure finding fires); if the
    relay correctly honours the empty override, the
    decision must be ``enter_qualifying_readiness``.
    """
    return {
        "captured_at": "2026-08-08T00:00:00Z",
        "head_sha": head_sha,
        "head_match": True,
        "mergeable": True,
        "formal_reviews": [],
        "review_threads": {},
        "issue_comments": [],
        # Empty on purpose: the operator's configured required
        # checks are absent, so any required-check list that
        # the CLI applies will surface a CI failure finding.
        "required_checks": {},
        "providers": [],
        "_provider_issue_comments": {},
        "unconsumed_event_ids": [],
    }


def _invoke_cli(
    tmp_path: Path,
    head_sha: str,
    *,
    required_ci_jobs: list,
    argv_extra: list,
) -> dict:
    state_root = tmp_path / "state"
    evidence_root = tmp_path / "evidence"
    snapshot_path = tmp_path / "snapshot.json"
    _seed_run_context(
        state_root, evidence_root, head_sha,
        required_ci_jobs=required_ci_jobs,
    )
    snapshot_path.write_text(json.dumps(_snapshot_clean(head_sha)))

    cli = f"{sys.executable} -m autocoder_orchestration.cli"
    parts = shlex.split(cli) + [
        "--json", "review-repair-round",
        "--state-root", str(state_root),
        "--run-id", "r1",
        "--snapshot-file", str(snapshot_path),
        "--head-sha", head_sha,
        "--evidence-root", str(evidence_root),
    ] + argv_extra

    proc = subprocess_run_safe(parts)
    assert proc.returncode == 0, (
        f"CLI failed: rc={proc.returncode} stderr={proc.stderr[:400]}"
    )
    return json.loads(proc.stdout)


def subprocess_run_safe(parts):
    import subprocess
    return subprocess.run(
        parts, capture_output=True, text=True, timeout=30,
    )


@pytest.fixture
def head_sha() -> str:
    return "a" * 40


class TestRequiredCheckNamesOverrideSemantics:
    """Lock in the three-case contract end-to-end via the CLI."""

    def test_flag_absent_falls_back_to_ctx_required_ci_jobs(
        self, tmp_path: Path, head_sha: str,
    ) -> None:
        """Omitting ``--required-check-names`` MUST fall back to
        the persisted ``ctx.required_ci_jobs`` policy. The
        snapshot omits those checks, so the relay must emit
        a CI failure finding and ``launch_worker``.
        """
        decision = _invoke_cli(
            tmp_path, head_sha,
            required_ci_jobs=["ci-A", "ci-B"],
            argv_extra=[],
        )
        assert decision["action"] == "launch_worker", (
            f"omitted flag must fall back to ctx.required_ci_jobs; "
            f"got action={decision.get('action')!r}"
        )

    def test_explicit_empty_override_does_not_resurrect_ctx(
        self, tmp_path: Path, head_sha: str,
    ) -> None:
        """Passing ``--required-check-names ""`` is the
        operator's explicit declaration that the persisted
        policy should be overridden with an empty set. The
        CLI MUST honour the override and MUST NOT resurrect
        older persisted checks; the head must be considered
        clean of required-check obligations.
        """
        decision = _invoke_cli(
            tmp_path, head_sha,
            required_ci_jobs=["ci-A", "ci-B"],
            argv_extra=["--required-check-names", ""],
        )
        assert decision["action"] == "enter_qualifying_readiness", (
            "explicit empty override must suppress "
            "ctx.required_ci_jobs; got action="
            f"{decision.get('action')!r}"
        )

    def test_explicit_names_pass_through(
        self, tmp_path: Path, head_sha: str,
    ) -> None:
        """Passing ``--required-check-names ci-X,ci-Y`` MUST be
        used verbatim and ignore ``ctx.required_ci_jobs``.
        The snapshot omits ``ci-X`` and ``ci-Y``, so the
        relay must emit a CI failure finding and
        ``launch_worker``.
        """
        decision = _invoke_cli(
            tmp_path, head_sha,
            required_ci_jobs=["ci-A", "ci-B"],
            argv_extra=["--required-check-names", "ci-X,ci-Y"],
        )
        assert decision["action"] == "launch_worker", (
            "explicit names must pass through and override ctx; "
            f"got action={decision.get('action')!r}"
        )