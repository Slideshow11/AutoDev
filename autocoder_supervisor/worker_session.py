"""Round-38: durable Hermes worker session lifecycle contract.

Problem the previous round could not solve:
    A configured ``AED_SESSION_ID`` (or any session bound to a
    lease) may not exist in the user's ``~/.hermes/state.db``.
    A blind ``hermes chat --resume <missing-id>`` invocation
    fails immediately, the worker process dies, and the lease
    blocks any new dispatch for many minutes while the
    supervisor's heartbeat loop waits on the now-zombie PID.

Contract implemented here:
    - ``resolve_worker_session`` examines the durable record
      and the configured session, and returns a *canonical*
      worker session identity that the supervisor MUST use
      for the current dispatch.
    - If the persisted worker session no longer exists, a fresh
      isolated session is created through the supported
      ``hermes chat -q "..."`` mechanism (no ``--resume``), its
      returned ``session_id`` is captured from stdout, and the
      lease / attempt are rebound to it.
    - Session creation is scoped to a single attempt and is
      NEVER allowed to adopt an arbitrary unrelated session.
    - Persisted session identity takes precedence over the
      stale env var so supervisor restart does not regress.

Scope limits:
    - No token material is logged at any point.
    - No process is killed by this module.
    - No retry / backoff logic; that's the caller's job.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


# The hermes CLI prints "session_id: <id>" as the final
# stdout line when a quiet, single-shot chat completes. We
# anchor the regex on a line-start marker to avoid picking
# up incidental text.
_SESSION_ID_LINE_RE = re.compile(
    r"(?m)^session_id:\s*([A-Za-z0-9_]+)\s*$"
)

# hermes exits with rc=2 when --resume references a session
# the store does not have.
_SESSION_NOT_FOUND_RE = re.compile(
    r"^Session not found:\s*(\S+)\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class SessionResolution:
    """Outcome of a session-resolution call.

    Attributes:
        session_id: The canonical session id the supervisor
            MUST pass to ``hermes chat --resume`` for the
            current attempt. Never None when ``ok`` is True.
        was_replaced: True when the originally requested
            session was missing and a fresh session was
            created instead.
        reason: Diagnostic string safe to log.
        persisted_path: Where the new session identity was
            persisted (or None when no replacement happened).
    """

    session_id: str
    was_replaced: bool
    reason: str
    persisted_path: Optional[str]


def _read_persisted_session(persist_path: Path) -> Optional[str]:
    """Read the durable session id for this attempt.

    Returns None when the file is absent or corrupt.
    """
    try:
        text = persist_path.read_text(encoding="utf-8")
    except (OSError, TypeError):
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    sid = str(data.get("session_id") or "").strip()
    return sid or None


def _write_persisted_session(
    persist_path: Path,
    *,
    session_id: str,
    attempt_id: str,
    pr_number: int,
    repo_owner: str,
    repo_name: str,
    feature_branch: str,
    replacement_reason: str,
) -> None:
    """Persist the actual session used by an attempt.

    Layout is intentionally minimal and explicit so a future
    reviewer can read it without inferring semantics.
    """
    payload = {
        "schema_version": "autocoder.worker_session.v1",
        "attempt_id": attempt_id,
        "session_id": session_id,
        "pr_number": pr_number,
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "feature_branch": feature_branch,
        "replacement_reason": replacement_reason,
        "persisted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    persist_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = persist_path.with_suffix(persist_path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(persist_path)


def _hermes_session_exists(
    hermes_bin: str,
    session_id: str,
    *,
    timeout: float = 15.0,
) -> bool:
    """Return True iff ``session_id`` exists in the user's
    hermes state.db.

    We use ``hermes sessions list --limit <N>`` because the CLI
    is the supported, version-stable inspection path. We
    parse the human-readable table rather than touching the
    SQLite store directly so the contract survives hermes
    schema bumps.
    """
    try:
        proc = subprocess.run(  # noqa: S602
            [hermes_bin, "sessions", "list", "--limit", "200"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    # The id column is the LAST whitespace-separated token on
    # the row. Match it on its own to avoid false positives.
    return any(
        line.split() and line.split()[-1] == session_id
        for line in proc.stdout.splitlines()
        if not line.startswith(("─", "Title"))
    )


def _create_fresh_session(
    hermes_bin: str,
    *,
    seed_prompt: str,
    cwd: Optional[Path] = None,
    timeout: float = 90.0,
) -> str:
    """Start an isolated fresh hermes chat and return its id.

    Round-38 forensic note: hermes emits ``session_id: <id>``
    to **stderr** (not stdout), as a separate ``session_id:``
    line that follows the session-init banner. The actual
    chat response goes to stdout. We must therefore scan
    stderr for the id, then surface it from stdout.

    The prompt is intentionally short: we just need a session
    shell. The worker will receive the real repair directive
    via the lease and re-resume this session, so this call
    must NOT pre-consume prompt-cache budget on the real task.
    """
    proc = subprocess.run(  # noqa: S602
        [
            hermes_bin,
            "chat",
            "-q",
            seed_prompt,
            "--no-restore-cwd",
            "--accept-hooks",
            "--yolo",
            "-Q",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd is not None else None,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"fresh hermes session creation failed: rc={proc.returncode} "
            f"stderr={proc.stderr.strip()[:200]}"
        )
    # Round-38 hermes emits ``session_id: <id>`` on STDERR
    # after the session-init banner. Search both streams so
    # the contract survives a future hermes move of the
    # marker to stdout.
    for source in (proc.stderr, proc.stdout):
        match = _SESSION_ID_LINE_RE.search(source or "")
        if match:
            return match.group(1)
    raise RuntimeError(
        "fresh hermes session did not return session_id; "
        f"stdout head={(proc.stdout or '')[:200]!r} "
        f"stderr head={(proc.stderr or '')[:200]!r}"
    )


def resolve_worker_session(
    *,
    hermes_bin: str,
    configured_session_id: str,
    persist_path: Path,
    attempt_id: str,
    pr_number: int,
    repo_owner: str,
    repo_name: str,
    feature_branch: str,
    workspace_cwd: Optional[Path] = None,
    seed_prompt: str = "init",
) -> SessionResolution:
    """Resolve the worker session id for the current attempt.

    Resolution order:
        1. Persisted session id from a prior attempt on this
           claim. If it still exists, return it as-is.
        2. The configured session id from env. If it exists,
           return it (and persist it for restart resilience).
        3. Otherwise, create a fresh isolated hermes session,
           persist it, and return it.

    The persisted file is written ONLY for cases (2) and (3)
    so we never overwrite a still-valid session id.
    """
    # 1. Persisted identity wins.
    persisted = _read_persisted_session(persist_path)
    if persisted:
        if _hermes_session_exists(hermes_bin, persisted):
            return SessionResolution(
                session_id=persisted,
                was_replaced=False,
                reason="persisted session exists; resuming",
                persisted_path=None,
            )
        # Persisted identity is stale; we will replace it.

    # 2. Configured session is the operator's bootstrap truth.
    if configured_session_id and _hermes_session_exists(
        hermes_bin,
        configured_session_id,
    ):
        _write_persisted_session(
            persist_path,
            session_id=configured_session_id,
            attempt_id=attempt_id,
            pr_number=pr_number,
            repo_owner=repo_owner,
            repo_name=repo_name,
            feature_branch=feature_branch,
            replacement_reason="bootstrap_persistence",
        )
        return SessionResolution(
            session_id=configured_session_id,
            was_replaced=False,
            reason="configured session exists; persisted for restart",
            persisted_path=str(persist_path),
        )

    # 3. Fall back to fresh-session creation.
    reason = (
        "configured session missing"
        if configured_session_id
        else "no configured session"
    )
    fresh_id = _create_fresh_session(
        hermes_bin,
        seed_prompt=seed_prompt,
        cwd=workspace_cwd,
    )
    _write_persisted_session(
        persist_path,
        session_id=fresh_id,
        attempt_id=attempt_id,
        pr_number=pr_number,
        repo_owner=repo_owner,
        repo_name=repo_name,
        feature_branch=feature_branch,
        replacement_reason=reason,
    )
    return SessionResolution(
        session_id=fresh_id,
        was_replaced=True,
        reason=f"{reason}; created fresh session {fresh_id[:12]}",
        persisted_path=str(persist_path),
    )


def probe_session_missing(
    hermes_bin: str,
    session_id: str,
    *,
    timeout: float = 10.0,
) -> bool:
    """Return True iff ``hermes chat --resume <session_id>``
    definitively reports the session is missing.

    This is a cheaper alternative to ``_hermes_session_exists``
    when we only need to know whether the resume path will
    fail (vs. having to introspect the entire session table).
    """
    try:
        proc = subprocess.run(  # noqa: S602
            [
                hermes_bin,
                "chat",
                "-q",
                "probe",
                "--resume",
                session_id,
                "--no-restore-cwd",
                "--accept-hooks",
                "--yolo",
                "-Q",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if _SESSION_NOT_FOUND_RE.search(proc.stderr or ""):
        return True
    return proc.returncode != 0 and "not found" in (proc.stderr or "").lower()


# ---------------------------------------------------------------------------
# Round-38: zombie / exited-child detection helpers
# ---------------------------------------------------------------------------


def _read_proc_state(pid: int) -> Optional[str]:
    """Return the single-letter ``State:`` field from
    ``/proc/<pid>/status``, or None if the proc is gone.
    """
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return None
    for line in text.splitlines():
        if line.startswith("State:"):
            # "State:  X (label)"
            parts = line.split()
            if len(parts) >= 2:
                return parts[1]
    return None


def reap_child_now(pid: int) -> Tuple[Optional[int], Optional[int]]:
    """Reap a child process if it has exited.

    Uses ``os.waitid(P_PID, pid, WNOHANG)`` which works for
    zombies (the OS still owns the PID and a wait will
    succeed). For processes that are NOT this supervisor's
    child (e.g. after a restart that lost the parent link),
    the call raises ``ChildProcessError``; we fall back to
    inspecting ``/proc/<pid>/status`` so the recovery still
    recognises a zombie even across the restart boundary.

    Returns ``(exit_code, signal)``. Both may be None when
    the child is alive.
    """
    # First: try the standard waitid on the current parent.
    try:
        result = os.waitid(os.P_PID, pid, os.WNOHANG)
    except ChildProcessError:
        result = None
    except OSError:
        result = None
    status = getattr(result, "si_status", None) if result else None
    if status is not None:
        try:
            if os.WIFEXITED(status):
                return (os.WEXITSTATUS(status), None)
            if os.WIFSIGNALED(status):
                return (None, os.WTERMSIG(status))
        except (AttributeError, OSError):
            pass

    # Fallback: read /proc/<pid>/status. If the process is
    # now a zombie OR no longer exists, treat it as exited.
    state = _read_proc_state(pid)
    if state is None:
        return (None, None)  # proc file gone; caller decides
    if state == "Z":
        # Zombies exist until reaped; the OS has already
        # delivered the exit. Try one more waitid; if it
        # still fails, surface the zombie state with a
        # synthetic exit code so the caller can finalise.
        try:
            os.waitid(os.P_PID, pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
        return (-1, None)
    return (None, None)


def child_is_zombie(pid: int) -> bool:
    """Return True if ``/proc/<pid>/status`` shows State Z."""
    return _read_proc_state(pid) == "Z"


def child_is_gone(pid: int) -> bool:
    """Return True if no ``/proc/<pid>`` exists at all."""
    try:
        Path(f"/proc/{pid}").stat()
    except (OSError, FileNotFoundError):
        return True
    return False
