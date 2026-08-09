"""Persistent relay wiring for the supervisor's event loop.

The relay (in ``autocoder_orchestration.review_repair_relay``)
is the bounded loop that drives a single PR through review,
repair, push, CI, review, repair until the head is clean. The
supervisor's main event loop is the persistent daemon that
calls the relay.

This module is the THIN layer that connects the supervisor's
heartbeat to the relay's CLI. The supervisor invokes the
relay-once-CLI subprocess on every new actionable event so
the relay's directive path is consulted AND the next-round
state is durable. The supervisor never imports the
orchestration package directly — the CLI is the process
boundary.

Persistent loop contract
--------------------------

The supervisor's main loop, on every heartbeat that detects a
new actionable event in ``ACTIVE_REPAIR``:

1. Writes the live snapshot to a temporary file.
2. Executes ``autocoder-orchestration review-repair-round``
   with the snapshot file, the canonical head, and the
   canonical state/evidence roots.
3. Reads the returned ``RoundDecision.action``.
4. If ``action == "launch_worker"``: the supervisor's
   existing ``handle_new_events`` machinery launches the
   worker. The directive is already on disk under the
   evidence root; the existing directive bridge picks it
   up automatically.
5. If ``action == "enter_qualifying_readiness"``: the
   supervisor deletes the directive and lets the existing
   readiness machinery evaluate the head.
6. If ``action == "escalate_to_human"``: the supervisor
   treats the round as reported and the controller is
   already in ``BLOCKED`` (the relay CLI drives the
   controller). The supervisor logs the escalation and
   stops launching workers.

The supervisor's existing lease, cooldown, head-mismatch
detection, and snapshot-differs logic are preserved
byte-for-byte. The wiring is a single new branch on the
new-events path.

No merge is performed by the relay. The existing human
merge-authorization boundary is the only path to
``AWAITING_MERGE_AUTHORIZATION``.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional, Tuple


# The path to the relay CLI. The supervisor uses subprocess so
# the supervisor and the relay stay in separate address
# spaces (no Python package cycle). The CLI is the documented
# orchestration entry point.
DEFAULT_RELAY_CLI = "autocoder-orchestration"
DEFAULT_RELAY_SUBCOMMAND = "review-repair-round"


class RelayWiringError(Exception):
    """Raised when the relay wiring cannot complete a round.

    The supervisor catches this exception and falls back to
    the existing ``build_resume_prompt`` path so the
    operator is never blocked by a wiring failure.
    """

    def __init__(self, reason: str, *, returncode: int = -1,
                 stdout: str = "", stderr: str = "") -> None:
        self.reason = reason
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"{reason} (rc={returncode}): {stderr.strip()[:200]}"
        )


def _resolve_cli_executable() -> str:
    """Resolve the relay CLI executable.

    The supervisor's environment may not have the CLI on
    PATH (e.g. inside a venv that the supervisor does not
    use). We accept the explicit ``AED_RELAY_CLI`` env var
    first, then fall back to PATH resolution.
    """
    explicit = os.environ.get("AED_RELAY_CLI")
    if explicit:
        return explicit
    resolved = shutil.which(DEFAULT_RELAY_CLI)
    if resolved:
        return resolved
    # Try the common Python -m invocation as a last resort.
    # The supervisor records this fallback in the log so the
    # operator can see the wiring path.
    return f"{os.environ.get('AED_PYTHON', sys.executable)} -m autocoder_orchestration.cli"


def invoke_relay_round(
    *,
    snapshot: dict,
    head_sha: str,
    state_root: str,
    run_id: str,
    pr_number: int,
    evidence_root: str,
    required_check_names: Tuple[str, ...] = (),
    timeout_seconds: float = 60.0,
) -> dict:
    """Invoke the relay's review-repair-round CLI subprocess.

    The function writes the snapshot to a temporary file,
    spawns the CLI subprocess, and returns the parsed
    ``RoundDecision`` dict. The subprocess is the only path
    the supervisor uses to talk to the relay — keeping the
    Python packages fully disjoint.

    Raises ``RelayWiringError`` on any failure: missing CLI,
    subprocess non-zero exit, malformed JSON output, etc.
    """
    cli = _resolve_cli_executable()
    # Write the snapshot to a temp file in the state's own
    # private directory so the supervisor can be restarted
    # mid-round without losing the input.
    snapshot_path = Path(state_root) / "live_snapshot.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshot_path.write_text(json.dumps(snapshot, sort_keys=True))
    try:
        os.chmod(snapshot_path, 0o600)
    except OSError:
        pass
    # ``_resolve_cli_executable`` may return a shell-style
    # executable (e.g. ``python3 -m autocoder_orchestration.cli``).
    # Split it into argv; the binary path is the first token.
    cli_argv = shlex.split(cli)
    # ``autocoder_orchestration.cli`` declares `--json` on the
    # TOP-LEVEL parser before subcommands. The CLI rejects
    # `--json` after the subcommand; the production
    # invocation must be the top-level form. The build is:
    #   [cli_argv..., --json, <subcommand>, ...args]
    # so argparse sees `--json` before the subcommand.
    cmd = (
        cli_argv
        + ["--json", DEFAULT_RELAY_SUBCOMMAND]
        + [
            "--state-root", state_root,
            "--run-id", run_id,
            "--snapshot-file", str(snapshot_path),
            "--head-sha", head_sha,
            "--evidence-root", evidence_root,
            "--required-check-names", ",".join(required_check_names),
        ]
    )
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RelayWiringError(
            "timeout", stdout=exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            stderr=exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
        ) from exc
    except FileNotFoundError as exc:
        raise RelayWiringError(
            f"cli_not_found: {cli}", stderr=str(exc),
        ) from exc
    if proc.returncode != 0:
        raise RelayWiringError(
            "non_zero_exit", returncode=proc.returncode,
            stdout=proc.stdout, stderr=proc.stderr,
        )
    try:
        decision = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RelayWiringError(
            "invalid_json", stdout=proc.stdout, stderr=proc.stderr,
        ) from exc
    if not isinstance(decision, dict) or "action" not in decision:
        raise RelayWiringError(
            "missing_action_field", stdout=proc.stdout,
        )
    return decision


def should_invoke_relay(snapshot: dict) -> bool:
    """Return True iff the live snapshot has actionable
    evidence that the relay can address.

    The supervisor's existing event-loop already detects new
    actionable events and launches a worker. The relay is
    invoked only when the snapshot has findings the relay
    can address (CodeRabbit review findings, CI failures).
    Quota / paused-provider events fall through to the
    existing review-request path.
    """
    if not isinstance(snapshot, dict):
        return False
    # The supervisor's snapshot shape carries per-provider
    # issue comments and required checks. The relay's
    # ``collect_findings`` is the canonical classifier.
    issue_comments = snapshot.get("issue_comments") or []
    per_provider = snapshot.get("_provider_issue_comments") or {}
    required_checks = snapshot.get("required_checks") or {}
    if per_provider:
        for provider, comments in per_provider.items():
            if (
                provider in ("coderabbit", "codex")
                and isinstance(comments, list)
                and comments
            ):
                return True
    if issue_comments:
        return True
    # CI failures — any required check concluded failure.
    if isinstance(required_checks, dict):
        for info in required_checks.values():
            if isinstance(info, dict):
                conclusion = str(info.get("conclusion") or "").lower()
                if conclusion in ("failure", "failed"):
                    # The relay's CI filter has a stricter
                    # definition; the supervisor's trigger
                    # is the existence of a failure on the
                    # current head.
                    return True
    return False


def mark_head_advanced_public(old_head_sha: str, new_head_sha: str) -> None:
    """Bind the worker push to the orchestration state machine.

    The supervisor calls this when the live PR head
    advances (the worker pushed a new commit). The
    transition fires only on a real head advance, so a
    launch failure leaves the repair state recoverable.

    A subprocess is avoided here because the supervisor
    is already in the supervisor's event loop; spawning
    a subprocess for a single state-machine transition
    would add latency without isolation benefit. The
    helper instantiates a RelayLoop with the supervisor's
    state_root and calls ``mark_head_advanced`` directly.
    """
    from autocoder_orchestration.controller import Controller
    from autocoder_orchestration.review_repair_relay import RelayLoop
    from autocoder_orchestration.store import StateStore
    state_root = os.environ.get("AED_ORCHESTRATION_STATE_ROOT")
    if not state_root:
        state_root = str(
            os.environ.get("AED_STATE_DIR", "/tmp/aed-supervisor-state"),
        )
    evidence_root = os.environ.get("AED_EVIDENCE_ROOT")
    if not evidence_root:
        evidence_root = os.path.join(state_root, "evidence")
    store = StateStore(state_root)
    if not store.read_optional("state.json"):
        # Controller state not initialized yet; nothing
        # to transition. The next round will pick up the
        # new head when the supervisor re-reads the state.
        return
    from autocoder_orchestration.context import RunContext
    ctx_dict = store.read_optional("run_context.json")
    if not ctx_dict:
        return
    ctx = RunContext.from_dict(ctx_dict)
    controller = Controller(
        context=ctx,
        store=store,
    )
    from autocoder_orchestration.review_repair_relay import DirectiveStore
    directive_store = DirectiveStore(
        store=store,
        evidence_root=evidence_root,
    )
    loop = RelayLoop(
        context=ctx,
        store=store,
        directive_store=directive_store,
        controller=controller,
        required_check_names=(),
    )
    loop.mark_head_advanced(old_head_sha, new_head_sha)


def delete_directive_if_present(evidence_root: str) -> None:
    """Delete the canonical directive file when the head is clean.

    The supervisor's directive bridge looks up the directive
    at the canonical evidence root. When the relay signals
    ``enter_qualifying_readiness``, the directive is no
    longer the source of truth — the existing readiness gate
    is. The supervisor removes the directive so the bridge
    falls back to the operator-supplied resume prompt.
    """
    directive = Path(evidence_root) / "directive.json"
    if directive.is_file():
        try:
            directive.unlink()
        except OSError:
            pass


__all__ = [
    "RelayWiringError",
    "DEFAULT_RELAY_CLI",
    "DEFAULT_RELAY_SUBCOMMAND",
    "invoke_relay_round",
    "should_invoke_relay",
    "delete_directive_if_present",
]
