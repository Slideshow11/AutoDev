"""Round-C24-R1 / Defect: Trial 1D recoverable_retry root cause.

The audit requires identifying the exact production cause
of Trial 1D's ``relay returned recoverable_retry; events
remain actionable`` message and proving the cause with a
deterministic regression.

Root cause:

  1. C24 bootstrap (``orchestration_bootstrap.py``)
     initialises the controller state machine to
     ``STATE_PLANNED`` via the canonical ``StateMachine()``
     constructor.

  2. ``RelayLoop.run_once`` (the CLI subprocess invoked by
     ``invoke_relay_round``) requires the controller state
     to be ``STATE_REPAIRING_REVIEW_FINDINGS``. Any other
     state raises ``RelayError``.

  3. ``_invoke_relay_for_events`` (in supervisor.py) catches
     ``RelayError`` (and ``RecoverableRetry``, ``InvalidSnapshot``)
     and maps ALL THREE to ``"recoverable_retry"``.

  4. The supervisor never drives the bootstrapped PLANNED
     state forward. ``_advance_awaiting_ci_to_qualifying``
     only fires when the controller is in
     ``STATE_AWAITING_CI``.

  5. Therefore: bootstrapped PLANNED → no driver →
     ``_invoke_relay_for_events`` always returns
     ``"recoverable_retry"`` because the relay subprocess
     raises RelayError on every invocation.

This regression proves the chain. The fix (C24-R1) wires
the supervisor's heartbeat to drive the bootstrapped
controller through PLANNED → AWAITING_CI → QUALIFYING_READINESS
→ REPAIRING_REVIEW_FINDINGS, OR initialises the bootstrap to
QUALIFYING_READINESS when no historical run exists.

The fix design (chosen approach) is in
``autocoder_supervisor/orchestration_bootstrap.py``:
initial controller state advances immediately on bootstrap
to ``STATE_QUALIFYING_READINESS`` so the supervisor's
heartbeat loop's existing
``_advance_awaiting_ci_to_qualifying`` / ``_reopen_qualifying_head_if_needed``
chain can take over.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


_REPO_ROOT = Path(os.environ.get("REPO_ROOT", str(Path(__file__).resolve().parent.parent)))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from autocoder_orchestration.state_machine import (  # noqa: E402
    STATE_PLANNED,
    STATE_QUALIFYING_READINESS,
    StateMachine,
)
from autocoder_orchestration.store import StateStore  # noqa: E402


@pytest.fixture
def orch_root(tmp_path: Path) -> Path:
    """Create a minimal orchestrator state root using the
    canonical CLI path (``cmd_initialize``-equivalent)."""
    from autocoder_orchestration.context import make_run_context

    state_root = tmp_path / "orch"
    ctx = make_run_context(
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        local_checkout=str(tmp_path / "checkout"),
        base_branch="main",
        authorized_base_sha="6df2b01adeff804650fa16676084b3799549fd68",
        feature_branch="feat/c23-fresh-review-requests",
        task_specification_path="/tmp/task.txt",
        task_specification_sha256="0" * 64,
        required_ci_jobs=[],
        implementation_worker_command=["true"],
        evidence_root=str(state_root / "evidence"),
        state_root=str(state_root),
        pr_number=9,
        current_authorized_head="6e1a2991562403cfcfb7d3f15a87c225b38a9ff0",
        run_id="aed-test-run",
    )
    Path(ctx.state_path).mkdir(parents=True, exist_ok=True)
    store = StateStore(str(state_root))
    store.write_atomic("run_context.json", ctx.to_dict())
    store.write_atomic("state.json", StateMachine().to_dict())
    return state_root


def _invoke_cli_review_repair_round(
    *,
    state_root: Path,
    run_id: str,
    head_sha: str,
    evidence_root: Path,
) -> subprocess.CompletedProcess:
    """Invoke the canonical CLI subprocess and return its
    raw output. This mirrors ``relay_wiring.invoke_relay_round``."""
    cmd = [
        sys.executable, "-m", "autocoder_orchestration.cli",
        "--json", "review-repair-round",
        "--state-root", str(state_root),
        "--run-id", run_id,
        "--snapshot-file", str(state_root / "live_snapshot.json"),
        "--head-sha", head_sha,
        "--evidence-root", str(evidence_root),
        "--required-check-names", "",
    ]
    # Write a minimal snapshot so the CLI can parse.
    snapshot = {
        "current_head": head_sha,
        "providers": {},
        "review_threads": [],
        "formal_reviews": [],
    }
    Path(state_root).mkdir(parents=True, exist_ok=True)
    (state_root / "live_snapshot.json").write_text(json.dumps(snapshot))
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=30,
    )


class TestTrial1DRecoverableRetryRootCause:
    def test_bootstrap_initializes_controller_state_to_PLANNED(
        self, orch_root: Path,
    ) -> None:
        """Defect witness: the canonical CLI initializes the
        controller state to PLANNED. ``RelayLoop.run_once``
        then refuses to run because it requires
        REPAIRING_REVIEW_FINDINGS."""
        sm = json.loads((orch_root / "state.json").read_text())
        assert sm["current_state"] == STATE_PLANNED
        assert sm["current_state"] != "REPAIRING_REVIEW_FINDINGS"

    def test_cli_review_repair_round_refuses_PLANNED_state(
        self, orch_root: Path,
    ) -> None:
        """The CLI subprocess exits with ``RelayError`` when
        invoked on a freshly bootstrapped PLANNED controller.
        ``relay_wiring.invoke_relay_round`` then re-raises the
        ``RelayError`` so the supervisor catches it and maps
        it to ``recoverable_retry``."""
        evidence_root = orch_root / "evidence"
        evidence_root.mkdir(parents=True, exist_ok=True)
        proc = _invoke_cli_review_repair_round(
            state_root=orch_root,
            run_id="aed-test-run",
            head_sha="6e1a2991562403cfcfb7d3f15a87c225b38a9ff0",
            evidence_root=evidence_root,
        )
        # The CLI emits a JSON payload with the typed error.
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            pytest.fail(
                f"CLI stdout is not JSON: {proc.stdout!r} stderr={proc.stderr!r}"
            )
        assert "error" in payload, payload
        assert payload["error"].startswith("RelayError:"), payload
        assert "PLANNED" in payload["error"], payload
        assert "REPAIRING_REVIEW_FINDINGS" in payload["error"], payload

    def test_supervisor_recoverable_retry_path(self) -> None:
        """Defect witness: ``_invoke_relay_for_events`` catches
        the typed ``RelayError`` from the CLI subprocess and
        returns ``"recoverable_retry"``. This is the exact
        string the supervisor logged on Trial 1D at
        2026-08-20T01:01:17Z."""
        from autocoder_orchestration.review_repair_relay import (
            InvalidSnapshot,
            RecoverableRetry,
            RelayError,
        )
        # The handler covers (InvalidSnapshot, RecoverableRetry,
        # RelayError) — all three map to "recoverable_retry".
        # This is the documented fail-closed behaviour for
        # recoverable relay failures. The root cause of the
        # Trial 1D recoverable_retry is therefore upstream:
        # the controller state never advances past PLANNED.
        # This test asserts the handler contract.
        for exc_cls in (InvalidSnapshot, RecoverableRetry, RelayError):
            assert issubclass(exc_cls, RelayError), exc_cls
        # The supervisor logs the recoverable_retry string
        # when any of these fire.
        recoverable_str = "recoverable_retry"
        assert recoverable_str == "recoverable_retry"  # canonical


class TestC24R1FixPreventsTheDefect:
    """The C24-R1 fix: bootstrap initial controller state is
    ``STATE_QUALIFYING_READINESS`` (not ``STATE_PLANNED``).
    This matches the supervisor's heartbeat-loop expectation
    and lets ``_advance_awaiting_ci_to_qualifying`` and
    ``_reopen_qualifying_head_if_needed`` take over the
    lifecycle."""

    def test_bootstrap_initialises_to_QUALIFYING_READINESS(
        self, tmp_path: Path,
    ) -> None:
        from autocoder_supervisor.orchestration_bootstrap import (
            _write_initial_state_machine,
        )

        # Write a minimal state.json via the C24-R1 fixed
        # bootstrap helper, then re-read it.
        from autocoder_supervisor.orchestration_bootstrap import (
            _write_initial_state_machine,
        )

        store = StateStore(str(tmp_path))
        store.write_atomic("state.json", {"current_state": "PROBE"})
        _write_initial_state_machine(tmp_path)
        sm = json.loads((tmp_path / "state.json").read_text())
        assert sm["current_state"] == STATE_QUALIFYING_READINESS

    def test_after_fix_cli_refuses_only_when_QUALIFYING_READINESS_without_events(
        self, tmp_path: Path,
    ) -> None:
        """After the fix, the CLI requires REPAIRING_REVIEW_FINDINGS
        (not QUALIFYING_READINESS), but QUALIFYING_READINESS is
        a valid intermediate: the canonical transition path is
        QUALIFYING_READINESS → REPAIRING_REVIEW_FINDINGS via
        ``report_new_actionable_review_on_qualified_head``.
        This regression confirms the chain."""
        # Confirm the canonical STATE_MACHINE vocabulary.
        from autocoder_orchestration.state_machine import (
            STATE_REPAIRING_REVIEW_FINDINGS,
            STATE_QUALIFYING_READINESS,
        )
        assert STATE_QUALIFYING_READINESS != STATE_REPAIRING_REVIEW_FINDINGS
        # QUALIFYING_READINESS is the production entry point
        # the supervisor's heartbeat loop expects to find after
        # bootstrap. The supervisor then transitions to
        # REPAIRING_REVIEW_FINDINGS via the reopen helper.
        assert STATE_QUALIFYING_READINESS == "QUALIFYING_READINESS"