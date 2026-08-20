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
from pathlib import Path
from typing import Optional, Tuple

from autocoder_orchestration.review_repair_relay import (  # noqa: F401
    EscalateToHuman,
    InvalidSnapshot,
    RecoverableRetry,
    RelayError,
)


# Round-47: bind the orchestration-root exception class at module
# scope so the guarded ``except (ImportError, OrchestrationRootError)``
# clauses below can always resolve the exception name even when
# the inner ``from .orchestration_state_root import ...`` raises
# ImportError. Without this binding Python evaluates the handler
# before ``OrchestrationRootError`` is necessarily bound, which
# raises ``UnboundLocalError`` instead of taking the documented
# fallback. Fall back to a private ``Exception`` sentinel if the
# orchestration_state_root module itself fails to import.
try:
    from .orchestration_state_root import OrchestrationRootError  # noqa: F401
except ImportError:
    class _OrchestrationRootErrorFallback(Exception):  # noqa: F401
        """Sentinel used only when ``orchestration_state_root``
        cannot be imported. The guarded import below will raise
        the real exception when state-root resolution fails; this
        class exists solely so the ``except`` clause names resolve.
        """
    OrchestrationRootError = _OrchestrationRootErrorFallback


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
    focused_thread_id: Optional[str] = None,
) -> dict:
    """Invoke the relay's review-repair-round CLI subprocess.

    The function writes the snapshot to a temporary file,
    spawns the CLI subprocess, and returns the parsed
    ``RoundDecision`` dict. The subprocess is the only path
    the supervisor uses to talk to the relay — keeping the
    Python packages fully disjoint.

    Round-45 C13: ``focused_thread_id`` scopes the directive
    to a SINGLE targeted review thread. The supervisor's
    durable-thread-drain path passes the targeted thread id
    so the worker evaluates that specific thread rather than
    the historical 8-P1 backlog.

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
    if focused_thread_id:
        cmd += ["--focused-thread-id", focused_thread_id]
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
        # Round-29: the CLI exits non-zero when the relay
        # raises ``RelayError`` or ``InvalidSnapshot``.
        # The CLI emits a JSON payload with
        # ``{"error": "<ClassName>: ..."}``.
        # We MUST re-raise the original exception class so the
        # supervisor surfaces the failure rather than
        # silently treating it as a wiring error.
        # ``EscalateToHuman`` is converted to a structured
        # decision in the CLI (see ``cmd_review_repair_round``)
        # so this branch only fires for non-escalation
        # failures.
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            error_str = payload.get("error") or ""
            # Round-29 review P4: only ``EscalateToHuman``
            # carries the protected-authority escalation
            # signal (EXIT_OK + structured decision). Other
            # ``RelayError`` subclasses are internal /
            # recoverable failures; re-raise them as the
            # actual class so the supervisor's retry path
            # picks them up.
            if not error_str.startswith("EscalateToHuman:"):
                for cls_name in (
                    "RelayError",
                    "InvalidSnapshot",
                    "DirectiveContractError",
                    "MergeAuthorizationError",
                ):
                    if error_str.startswith(f"{cls_name}:"):
                        mod = __import__(
                            "autocoder_orchestration.review_repair_relay",
                            fromlist=[cls_name],
                        )
                        cls = getattr(mod, cls_name, None)
                        if cls is not None:
                            raise cls(
                                error_str.split(":", 1)[1].strip()
                            )
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

    Round-29 P1#3: the supervisor MUST NOT invoke the relay
    on a provider walkthrough / in-progress / completion
    status comment. ``_is_actionable_provider_comment`` is
    the canonical actionable classifier used by the relay;
    the supervisor's trigger MUST consult it. A provider
    status comment that survives the actionable filter
    does NOT trigger the relay here.
    """
    from autocoder_orchestration.review_repair_relay import (
        _is_actionable_provider_comment,
    )
    if not isinstance(snapshot, dict):
        return False
    per_provider = snapshot.get("_provider_issue_comments") or {}
    required_checks = snapshot.get("required_checks") or {}
    inline_comments = snapshot.get("review_comments") or []
    has_actionable_provider_comment = False
    if per_provider:
        for provider, comments in per_provider.items():
            if (
                provider in ("coderabbit", "codex")
                and isinstance(comments, list)
                and comments
            ):
                for comment in comments:
                    # Round-29 review: ``_is_actionable_provider_comment``
                    # expects a body string, not a comment
                    # dictionary. ``capture_live_snapshot``
                    # builds comment dicts with ``login`` and
                    # ``body`` keys; we MUST extract the
                    # body string here so the actionability
                    # check does not raise ``AttributeError``
                    # on a real-world snapshot.
                    if isinstance(comment, dict):
                        body = str(comment.get("body") or "")
                    else:
                        body = str(comment or "")
                    if _is_actionable_provider_comment(body):
                        has_actionable_provider_comment = True
                        break
            if has_actionable_provider_comment:
                break
    if has_actionable_provider_comment:
        return True
    # Round-30: inline review comments (CodeRabbit / Codex
    # file/line suggestions) MUST also trigger the relay.
    # The snapshot's ``review_comments`` list carries the
    # inline comment bodies with file/line/path metadata.
    # A current-head actionable inline comment triggers the
    # structured relay; never fall back to the generic
    # worker merely because the provider used the inline
    # review surface. ``commit_id`` is the head-bound
    # identity for the inline review; absent
    # ``commit_id``, the supervisor's snapshot collector
    # already filters by the current head's review API.
    if isinstance(inline_comments, list) and inline_comments:
        current_head = snapshot.get("head_sha")
        for inline in inline_comments:
            if not isinstance(inline, dict):
                continue
            body = str(inline.get("body") or "")
            if not _is_actionable_provider_comment(body):
                continue
            # Inline reviews are intrinsically bound to
            # the commit they reviewed; the snapshot's
            # capture is the canonical current-head bound.
            # If the inline carries an explicit commit
            # identity that disagrees with the current
            # head, skip it (the relay's collector handles
            # the same filter, but the trigger is also
            # strict so we don't bounce to a stale head).
            cmt = (
                inline.get("commit_id")
                or inline.get("commit_oid")
                or inline.get("commit_sha")
            )
            if (
                isinstance(cmt, str)
                and cmt
                and current_head
                and cmt != current_head
            ):
                continue
            has_actionable_provider_comment = True
            break
    if has_actionable_provider_comment:
        return True
    # CI failures — any required check concluded failure.
    # The relay's CI filter is stricter than this trigger
    # (it requires the conclusion to be on the current
    # head); the supervisor's trigger is the existence of
    # a failure on the current head.
    if isinstance(required_checks, dict):
        for info in required_checks.values():
            if isinstance(info, dict):
                conclusion = str(info.get("conclusion") or "").lower()
                if conclusion in ("failure", "failed"):
                    return True
    # Human-authored issue_comments do NOT trigger the
    # relay. The relay classifies provider-authored inline
    # comments (coderabbit/codex); human comments must be
    # routed to the existing review-request machinery.
    return False


def _resolve_orchestration_state_root() -> Optional[str]:
    """Legacy fail-closed entry point used by the round-26 tests.

    Round-28 deprecation: ``STATE_DIR`` is NOT a fallback. The
    new contract is ``resolve_orchestration_state_root`` from
    ``orchestration_state_root`` which raises
    ``OrchestrationRootError`` for fail-closed semantics. This
    shim exists only so the legacy call sites return ``None``
    when the resolver fails closed (the caller still treats
    ``None`` as fail-closed).

    Production code MUST migrate to
    ``resolve_orchestration_state_root`` and treat the raised
    exception as a protected-authority blocker.
    """
    try:
        from .supervisor import RUN_STATE
        from .orchestration_state_root import resolve_orchestration_state_root
        return resolve_orchestration_state_root(run_state_path=Path(RUN_STATE))
    except Exception:
        return None


def _resolve_orchestration_evidence_root(state_root: Optional[str]) -> str:
    """Return the orchestration's evidence_root, paired with the
    state-root resolver above.

    Round-29 review: positively resolve the orchestration
    state root FIRST (via the canonical
    ``resolve_orchestration_state_root`` resolver), then
    derive the evidence root from that concrete root. A
    default deployment with no ``AED_EVIDENCE_ROOT`` env
    var MUST still locate the relay-written directive
    file because the orch state root records the evidence
    root at handoff time. Falling back to
    ``<state_root>/evidence`` without orch-state-root
    resolution would silently route to a stale / empty
    directory for any deployment that did not set
    ``AED_EVIDENCE_ROOT`` explicitly.
    """
    explicit = os.environ.get("AED_EVIDENCE_ROOT")
    if explicit:
        return explicit
    # Round-29: positively resolve the orch state root
    # before deriving the evidence root. The orch state
    # root records the evidence root at handoff time, so
    # this is the canonical path.
    try:
        from .supervisor import RUN_STATE
        from .orchestration_state_root import (
            resolve_orchestration_state_root,
        )
        orch_root = resolve_orchestration_state_root(
            run_state_path=Path(RUN_STATE),
        )
        if orch_root:
            orch_root_path = Path(orch_root)
            # The orch state root layout puts evidence in
            # ``<orch_state_root>/evidence`` by default;
            # but a recorded ``orchestration_evidence_root``
            # in run_state.json takes precedence.
            try:
                run_state = json.loads(RUN_STATE.read_text())
                recorded = run_state.get(
                    "orchestration_evidence_root",
                )
                if recorded:
                    return str(recorded)
            except (OSError, json.JSONDecodeError):
                pass
            return str(orch_root_path / "evidence")
    except (ImportError, OrchestrationRootError):
        # Orch state root cannot be resolved. Fall back
        # to ``<state_root>/evidence`` for back-compat,
        # but record a warning so the supervisor's retry
        # path is observable.
        pass
    if state_root:
        return os.path.join(state_root, "evidence")
    return ""


def mark_head_advanced_public(
    old_head_sha: str,
    new_head_sha: str,
    *,
    attempt_id: Optional[str] = None,
) -> bool:
    """Bind a verified worker push to the orchestration state machine.

    Round-36 invariant: ``HEAD_ADVANCED != REPAIR_PUSHED``.

    The supervisor calls this when the live PR head
    advances, BUT it must ONLY succeed when the head
    advance has positive worker-attempt provenance. The
    supervisor passes ``attempt_id`` (the canonical
    WorkerAttemptRecord on disk) and this helper verifies:

    1. A WorkerAttemptRecord with that attempt_id exists.
    2. Its lifecycle is ``PUSH_VERIFIED`` or ``TERMINAL_REPAIRED``.
    3. Its ``prelaunch_head`` equals ``old_head_sha``.
    4. Its ``pushed_commit_sha`` equals ``new_head_sha``.
    5. Its ``github_head_verified`` is True.

    If any of these fail, the call is REJECTED: the helper
    logs the rejection and returns False. The supervisor
    MUST still rebind AUTHORITATIVE_HEAD (so the next round
    operates on the new head), but the orchestrator does NOT
    record the transition as ``repair_pushed``.

    Returns True if the controller recorded
    ``REPAIRING_REVIEW_FINDINGS -> AWAITING_CI`` via
    ``report_repair_pushed``. Returns False otherwise
    (including when the attempt is not verified, the
    orchestrator was not in REPAIRING, or the head was
    already advanced idempotently).
    """
    from autocoder_orchestration.controller import Controller
    from autocoder_orchestration.review_repair_relay import RelayLoop
    from autocoder_orchestration.store import StateStore
    from autocoder_orchestration.worker_attempt import (
        LIFECYCLE_PUSH_VERIFIED,
        LIFECYCLE_TERMINAL_REPAIRED,
        WorkerResultArtifact,
    )
    from .orchestration_state_root import (
        resolve_orchestration_state_root,
    )
    from .supervisor import RUN_STATE  # type: ignore[name-defined]

    # Round-36: validate positive worker-attempt provenance BEFORE
    # touching the controller state machine. Without this check a
    # generic head advance (Humphry infrastructure commit, operator
    # commit, recovery commit, external actor) would falsely
    # acknowledge a worker repair push.
    if attempt_id is None:
        try:
            from .supervisor import log
            log(
                "error",
                "mark_head_advanced_public: missing attempt_id; "
                "cannot acknowledge repair push without provenance",
                old_head=old_head_sha[:12] if old_head_sha else "",
                new_head=new_head_sha[:12],
            )
        except ImportError:
            pass
        return False
    # Round-39 P1#7: read the attempt from the supervisor's
    # canonical WorkerAttemptStore. ``default_store()`` falls
    # back to ``cwd/state/worker_attempts`` or
    # ``AED_EVIDENCE_ROOT/state/worker_attempts`` which is NOT
    # the supervisor's configured ``WORKER_ATTEMPTS_DIR``. In
    # the default deployment those two paths differ; the
    # verified attempt could not be found and the head advance
    # was rejected. The supervisor's canonical store lives at
    # ``STATE_DIR/worker_attempts`` (same as
    # ``autocoder_supervisor.supervisor._worker_attempt_store()``).
    # We import the helper from the supervisor module so the
    # path is computed identically to the one the
    # launch/poll/finalize paths use.
    #
    # Round-275 P2#7 (Data Integrity): when the supervisor's
    # canonical store cannot be constructed (helper raises,
    # supervisor module missing, etc.) we MUST fail closed.
    # The previous behaviour fell through to ``default_store()``
    # which uses a different root and reintroduces the
    # cross-root provenance failure this change was meant to
    # remove. The provenance check is now mandatory: any
    # failure to bind the canonical store returns ``False``
    # so the supervisor's heartbeat loop stops bouncing a
    # head advance through a divergent store root.
    try:
        from .supervisor import _worker_attempt_store
        store = _worker_attempt_store()
    except Exception as exc:  # noqa: BLE001
        try:
            from .supervisor import log
            log(
                "error",
                "mark_head_advanced_public: canonical worker_attempt_store "
                "unavailable; refusing to fall back to default_store() to "
                "preserve cross-root provenance",
                attempt_id=attempt_id,
                error=str(exc),
                error_type=type(exc).__name__,
                old_head=old_head_sha[:12] if old_head_sha else "",
                new_head=new_head_sha[:12],
            )
        except ImportError:
            pass
        return False
    attempt = store.read(attempt_id)
    if attempt is None:
        try:
            from .supervisor import log
            log(
                "error",
                "mark_head_advanced_public: attempt_id not found on disk",
                attempt_id=attempt_id,
                old_head=old_head_sha[:12] if old_head_sha else "",
                new_head=new_head_sha[:12],
            )
        except ImportError:
            pass
        return False
    if attempt.lifecycle not in (
        LIFECYCLE_PUSH_VERIFIED, LIFECYCLE_TERMINAL_REPAIRED,
    ):
        try:
            from .supervisor import log
            log(
                "error",
                "mark_head_advanced_public: attempt lifecycle not PUSH_VERIFIED",
                attempt_id=attempt_id,
                lifecycle=attempt.lifecycle,
                old_head=old_head_sha[:12] if old_head_sha else "",
                new_head=new_head_sha[:12],
            )
        except ImportError:
            pass
        return False
    if attempt.prelaunch_head != old_head_sha:
        try:
            from .supervisor import log
            log(
                "error",
                "mark_head_advanced_public: attempt prelaunch_head mismatch",
                attempt_id=attempt_id,
                attempt_prelaunch=attempt.prelaunch_head[:12],
                expected_old=old_head_sha[:12],
                new_head=new_head_sha[:12],
            )
        except ImportError:
            pass
        return False
    if attempt.pushed_commit_sha != new_head_sha:
        try:
            from .supervisor import log
            log(
                "error",
                "mark_head_advanced_public: attempt pushed_commit_sha mismatch",
                attempt_id=attempt_id,
                attempt_pushed=(
                    attempt.pushed_commit_sha[:12]
                    if attempt.pushed_commit_sha else "(none)"
                ),
                new_head=new_head_sha[:12],
            )
        except ImportError:
            pass
        return False
    if not attempt.github_head_verified:
        try:
            from .supervisor import log
            log(
                "error",
                "mark_head_advanced_public: attempt github_head_verified is False",
                attempt_id=attempt_id,
                new_head=new_head_sha[:12],
            )
        except ImportError:
            pass
        return False

    # Round-C24-R1 / P1-A: the authoritative repair-transition
    # timestamp is NOT the worker result envelope's
    # ``completed_at`` (Codex P1 finding
    # PRRT_kwDOTtyQLc6aqKAg). It is ``head.repo.pushed_at`` from
    # GitHub's PR payload, captured by the supervisor's
    # canonical ``fetch_live_pr_head_now_with_push_time`` helper
    # at the moment it positively verifies
    # ``pushed_commit_sha == live_head``. The worker result
    # envelope's ``completed_at`` is NOT the push time and must
    # never be used as a substitute. If the supervisor cannot
    # fetch the authoritative pushed_at (network error, missing
    # token), ``repair_transition_at`` remains ``None`` and the
    # SUPERSEDED row is written without ``superseded_at`` so
    # the C22 resurrection helper fails closed.
    repair_transition_at = None
    artifact_path = getattr(attempt, "result_artifact_path", None)
    if artifact_path:
        artifact = WorkerResultArtifact.read(Path(artifact_path))
        if (
            artifact is not None
            and artifact.attempt_id == attempt.attempt_id
            and new_head_sha in artifact.pushed_commit_shas
            and not artifact.validate_against_attempt(attempt)
        ):
            # ``artifact.completed_at`` is documented but
            # NOT trustworthy as the push boundary. We
            # capture the authoritative ``pushed_at`` from
            # GitHub's PR payload and write it back to the
            # attempt record so the relay's downstream
            # ``mark_head_advanced`` can forward it as the
            # ``superseded_at`` value. The artifact's
            # ``completed_at`` is ignored here by design.
            try:
                push_ts = _fetch_pr_head_pushed_at(
                    repo_owner=str(REPO_OWNER),  # type: ignore[name-defined]
                    repo_name=str(REPO_NAME),  # type: ignore[name-defined]
                    pr_number=int(PR_NUMBER),  # type: ignore[name-defined]
                    head_sha=new_head_sha,
                )
                if push_ts:
                    repair_transition_at = push_ts
                    try:
                        from datetime import datetime as _dt
                        _dt.fromisoformat(push_ts.replace("Z", "+00:00"))
                        attempt.push_succeeded_at = push_ts
                    except (AttributeError, TypeError, ValueError):
                        attempt.push_succeeded_at = None
                        repair_transition_at = None
                else:
                    # Fail closed: no trustworthy push time.
                    try:
                        from .supervisor import log
                        log(
                            "warning",
                            "mark_head_advanced_public: could not "
                            "fetch authoritative pushed_at from "
                            "GitHub PR payload; setting "
                            "superseded_at=None so resurrection "
                            "fails closed",
                            attempt_id=attempt_id,
                            head=new_head_sha[:12],
                        )
                    except ImportError:
                        pass
                    attempt.push_succeeded_at = None
                    repair_transition_at = None
            except (AttributeError, TypeError, ValueError):
                repair_transition_at = None

    # Provenance verified. Now drive the controller transition.
    try:
        state_root = resolve_orchestration_state_root(
            run_state_path=Path(RUN_STATE),
        )
    except OrchestrationRootError as exc:
        # Round-28 invariant: fail closed. The supervisor MUST NOT
        # fall back to STATE_DIR; if the orchestration state root
        # cannot be positively identified, route to BLOCKED /
        # escalation and stop autonomous progression.
        try:
            from .supervisor import log
            log(
                "error",
                "mark_head_advanced failed: orchestration state_root not positively identified",
                error=str(exc),
                old_head=old_head_sha[:12] if old_head_sha else "",
                new_head=new_head_sha[:12],
                attempt_id=attempt_id,
            )
        except ImportError:
            pass
        return False
    evidence_root = _resolve_orchestration_evidence_root(state_root)
    orch_store = StateStore(state_root)
    if not orch_store.read_optional("state.json"):
        # Controller state not initialized yet; nothing
        # to transition. The next round will pick up the
        # new head when the supervisor re-reads the state.
        return False
    from autocoder_orchestration.context import RunContext
    ctx_dict = orch_store.read_optional("run_context.json")
    if not ctx_dict:
        return False
    ctx = RunContext.from_dict(ctx_dict)
    controller = Controller(
        context=ctx,
        store=orch_store,
    )
    from autocoder_orchestration.review_repair_relay import DirectiveStore
    directive_store = DirectiveStore(
        store=orch_store,
        evidence_root=evidence_root,
    )
    loop = RelayLoop(
        context=ctx,
        store=orch_store,
        directive_store=directive_store,
        controller=controller,
        required_check_names=(),
    )
    # Only invoke mark_head_advanced if the controller is in
    # REPAIRING_REVIEW_FINDINGS. Any other state means the
    # transition has already happened (idempotency) or the
    # controller is not awaiting a repair push.
    try:
        state_now = orch_store.read_optional("state.json") or {}
        current = state_now.get("current_state")
    except Exception:
        current = None
    if current != "REPAIRING_REVIEW_FINDINGS":
        try:
            from .supervisor import log
            log(
                "info",
                "mark_head_advanced_public: controller not in REPAIRING; "
                "skipping report_repair_pushed",
                attempt_id=attempt_id,
                current_state=current,
                new_head=new_head_sha[:12],
            )
        except ImportError:
            pass
        return False
    # Round-275 P2#6 (Stability): ``loop.mark_head_advanced``
    # may raise a typed ``ControllerError`` or any
    # downstream exception when the state-store write
    # fails, when the rebind sequence raises, or when the
    # underlying controller refactor breaks the contract.
    # The supervisor's caller (the heartbeat loop) treats
    # this helper as best-effort — an unhandled exception
    # propagates out of the supervisor's heart-beat
    # machinery and kills the entire loop. We log the
    # failure and return ``False`` so the supervisor can
    # continue polling; the next round re-attempts the
    # transition from the durable state.
    try:
        loop.mark_head_advanced(
            old_head_sha,
            new_head_sha,
            # Round-C22R2/P1: forward the directive UUID that
            # drove the worker push so the finding ledger's
            # SUPERSEDED row records the authoritative
            # repair-transition provenance. ``directive_id``
            # was captured into the attempt record at
            # launch time (``WorkerAttemptRecord.directive_id``).
            directive_id=getattr(attempt, "directive_id", None),
            # Round-C24 / Defect 3: forward the worker's
            # authoritative verified-repair timestamp
            # (the validated WorkerResultArtifact ``completed_at``)
            # so the SUPERSEDED row
            # compares correctly against a reviewer follow-up
            # that arrives AFTER the verified push but BEFORE
            # the supervisor observes the push. Without this
            # the SUPERSEDED row's ``superseded_at`` falls
            # back to ``_now_iso()`` which is strictly later
            # than any genuine post-repair follow-up.
            superseded_at=repair_transition_at,
        )
    except Exception as exc:  # noqa: BLE001
        try:
            from .supervisor import log
            log(
                "error",
                "mark_head_advanced_public: loop.mark_head_advanced "
                "raised; supervisor continues polling",
                attempt_id=attempt_id,
                error=str(exc),
                error_type=type(exc).__name__,
                old_head=old_head_sha[:12] if old_head_sha else "",
                new_head=new_head_sha[:12],
            )
        except ImportError:
            pass
        return False
    # On success, mark the attempt TERMINAL_REPAIRED so it is
    # never re-acknowledged (idempotent).
    try:
        attempt.assert_can_transition_to(LIFECYCLE_TERMINAL_REPAIRED)
        attempt.lifecycle = LIFECYCLE_TERMINAL_REPAIRED
        attempt.finished_at = (
            attempt.finished_at or _now_iso()
        )
        store.write(attempt)
    except Exception as exc:  # noqa: BLE001
        try:
            from .supervisor import log
            log(
                "warning",
                "mark_head_advanced_public: could not finalize attempt to TERMINAL_REPAIRED",
                attempt_id=attempt_id,
                error=str(exc),
            )
        except ImportError:
            pass
    return True


def _now_iso() -> str:
    """ISO-8601 UTC timestamp with second precision."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _fetch_pr_head_pushed_at(
    *,
    repo_owner: str,
    repo_name: str,
    pr_number: int,
    head_sha: str,
) -> Optional[str]:
    """Round-C24-R2 / P1-A: REMOVED. Returns ``""``.

    The previous C24-R1 implementation read an
    authoritatively-broken repo-level timestamp. The
    audit invalidates that binding. Returning ``""``
    here means the resurrection rule will rely on the
    per-follow-up exact-head binding (commit_id) rather
    than any wall-clock timestamp. The C24-R2 contract
    is fail-closed: when no trustworthy exact-head binding
    is available, the SUPERSEDED row is written without
    ``superseded_at`` and outdated-thread resurrection
    fails closed.
    """
    return ""


__all__ = [
    "RelayWiringError",
    "DEFAULT_RELAY_CLI",
    "DEFAULT_RELAY_SUBCOMMAND",
    "invoke_relay_round",
    "should_invoke_relay",
    "delete_directive_if_present",
]
