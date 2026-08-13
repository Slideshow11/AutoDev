"""AED Autocoder Supervisor (source-controlled v1).

This module is the source-controlled version of the working
v5 supervisor that has been operating as a host-level daemon
under ``~/.hermes/aed-supervisor/``. The implementation has
been ported without changing its semantics — only the
configuration source has been changed so the supervisor can
be installed under any prefix.

Module-level globals are populated from a
``SupervisorConfig`` at import time. Tests that monkeypatch
these globals continue to work because the names and the
data shapes are unchanged from the original supervisor.

The behavioural invariants enforced by this implementation
are documented in ``INVARIANTS.md``. The contracts produced
by every persistent artifact are described by TypedDicts in
``contracts.py``.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

#: Strict lowercase hex SHA-1/256 pattern. Used to validate
#: rebind targets and any other 40-or-64-char head SHA.
_HEX_SHA_RE = re.compile(r"\A[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")

# Round-32 P0.5: package-safe import shim.
# When loaded as part of the ``autocoder_supervisor``
# package (``python -m autocoder_supervisor.supervisor``
# or ``from autocoder_supervisor.supervisor import
# main``) the relative imports below resolve normally.
# When loaded standalone (``python3 supervisor.py`` or
# ``python3 -m supervisor`` from a directory containing
# ``supervisor.py`` + siblings but no ``__init__.py``)
# the relative imports fail. Detect that and inject the
# current directory's modules into ``sys.modules``
# under the package-qualified name so the relative
# imports can resolve. This avoids the prior
# crash-loops and the duplicate-import finding.
if __package__ in (None, ""):
    # Standalone execution: re-register this module as
    # part of a synthetic package so relative imports
    # work. The systemd unit uses
    # ``PYTHONPATH=/home/max/.hermes/aed-supervisor`` +
    # ``python3 -m supervisor``; that requires either
    # ``__init__.py`` OR this shim. We choose the shim
    # to avoid package-init coupling.
    import sys as _sys
    _pkg_name = "_aed_supervisor_standalone"
    if _pkg_name not in _sys.modules:
        import types as _types
        _pkg = _types.ModuleType(_pkg_name)
        _pkg.__path__ = [str(Path(__file__).resolve().parent)]  # type: ignore[name-defined]
        _sys.modules[_pkg_name] = _pkg
    # Re-bind this module's ``__package__`` to the
    # synthetic package so ``from .X import`` resolves.
    # CRITICAL: do NOT rebind ``__name__`` to the
    # synthetic-package-qualified name. The module's
    # ``if __name__ == \"__main__\": sys.exit(main())``
    # block only fires when ``__name__`` is exactly
    # ``\"__main__\"``. When the supervisor is launched
    # via ``python3 -m supervisor`` Python sets
    # ``__name__`` to ``\"__main__\"`` for the top-level
    # module of the package, and rebinding it to
    # ``\"_aed_supervisor_standalone.supervisor\"`` would
    # cause main() to never run. The synthetic-package
    # bind only needs ``__package__``.
    import sys as _sys2
    _mod_name = _pkg_name + ".supervisor"
    _sys2.modules[_mod_name] = _sys2.modules.get(__name__, _sys2.modules[__name__])
    __package__ = _pkg_name  # type: ignore[misc]

from .config import default_config_from_env
from .contracts import SupervisorConfig
from .directive_bridge import (  # noqa: F401  -- resolve_worker_prompt is the public back-compat surface
    DirectiveLoadFailure,
    resolve_directive,
    resolve_worker_prompt,
)
from .orchestration_state_root import OrchestrationRootError, OrchestrationRootMissing, OrchestrationRootUnverified, resolve_orchestration_state_root  # noqa: F401

# NOTE: ``Controller``, ``RunContext``, ``StateStore``, and
# ``StateStoreError`` are imported LAZILY inside the
# qualifying-readiness controller-driver block so the
# supervisor's module-level import does NOT require
# ``autocoder_orchestration`` to be importable. Round-29
# review P7: the imports MUST be bound before the
# guarded execution path so a failed import cannot turn
# into ``UnboundLocalError``; we import them at the top
# of the function body (not inside the try/except).


# ---------------------------------------------------------------------------
# Module-level globals — populated from SupervisorConfig at import time.
# Tests monkeypatch these globals (e.g. ``STATE_DIR``) so the original
# test surface is preserved verbatim.
# ---------------------------------------------------------------------------


def _apply_config(cfg: SupervisorConfig) -> dict[str, Any]:
    """Populate the module-level globals from a SupervisorConfig.

    Returns the mapping that was applied so the bootstrap
    path can log it.
    """
    home = Path(cfg.state_dir).parent
    state_dir = Path(cfg.state_dir)
    mapping: dict[str, Any] = {
        # Identity
        "INSTANCE_ID": cfg.instance_id,
        "SESSION_ID": cfg.worker_session_id,
        "SESSION_NAME": cfg.worker_session_name,
        "PR_NUMBER": int(
            (os.environ.get("AED_PR_NUMBERS") or "").split(",")[0]
            or os.environ.get("AED_PR_NUMBER", "0")
        ),
        # Round-32: list of PR numbers the supervisor
        # owns simultaneously. The main loop iterates
        # each PR per heartbeat tick. The singleton
        # ``PR_NUMBER`` global keeps backward-compat
        # for all the legacy call sites that read it
        # for the canonical PR.
        "PR_NUMBERS": [
            int(p.strip()) for p in (
                os.environ.get("AED_PR_NUMBERS", "")
                or os.environ.get("AED_PR_NUMBER", "")
            ).split(",") if p.strip()
        ],
        "REPO_OWNER": os.environ.get(
            "AED_REPO_OWNER", "unknown-owner"
        ),
        "REPO_NAME": os.environ.get(
            "AED_REPO_NAME", "unknown-repo"
        ),
        "AUTHORITATIVE_HEAD": os.environ.get(
            "AED_AUTHORITATIVE_HEAD", ""
        ),
        # Cadence / cooldowns
        "HEARTBEAT_SECS": cfg.heartbeat_seconds,
        "RESUME_COOLDOWN_SECS": cfg.cooldown_seconds,
        "QUOTA_RETRY_INITIAL_SECS": cfg.quota_retry_initial_seconds,
        "QUOTA_RETRY_BACKOFF_SECS": cfg.quota_retry_backoff_seconds,
        "QUOTA_BACKOFF_AFTER_RETRY_COUNT":
            cfg.quota_backoff_after_retry_count,
        # Runtime paths
        "SUPERVISOR_HOME": home,
        "STATE_DIR": state_dir,
        "LEASE_PATH": state_dir / "worker_lease.json",
        "LAST_RESUME_PATH": state_dir / "last_resume.json",
        "QUOTA_PATH": state_dir / "quota_state.json",
        "REVIEW_REQUESTS_DIR": state_dir / "review_requests",
        "LOG_PATH": Path(cfg.log_path),
        "HEARTBEAT_PATH": Path(cfg.heartbeat_path),
        "LOCK_PATH": Path(cfg.lock_path),
        "RUN_STATE": Path(
            os.environ.get(
                "AED_RUN_STATE_PATH",
                str(state_dir / "run_state.json"),
            )
        ),
        "TOKEN_FILE": Path(
            os.environ.get(
                "AED_GITHUB_TOKEN_FILE",
                str(Path.home() / ".config" / "gh" / "hosts.yml"),
            )
        ),
        "REPO_DIR": Path(cfg.working_checkout),
        # Persistent state paths
        "UNCONSUMED_EVENTS_PATH":
            state_dir / "unconsumed_events.json",
        "SNAPSHOT_A_PATH": state_dir / "snapshot_a.json",
        "SNAPSHOT_B_PATH": state_dir / "snapshot_b.json",
        "READINESS_STATE_PATH":
            state_dir / "readiness_state.json",
        # Worker launch configuration
        "WORKER_COMMAND_TEMPLATE": list(cfg.worker_command),
        "RESUME_PROMPT_TEMPLATE": cfg.resume_prompt_template,
    }
    for k, v in mapping.items():
        globals()[k] = v
    return mapping


def _reconcile_authoritative_head_at_boot() -> str:
    """Round-40: reconcile ``AUTHORITATIVE_HEAD`` against
    authoritative durable + live evidence at supervisor boot.

    Source-of-truth precedence (highest to lowest):
        1. Live PR head from GitHub (the actual repository
           state). This is what the operator sees when
           they open the PR.
        2. ``run_state.json`` ``current_head`` (the canonical
           durable record written by the previous
           supervisor lifetime).
        3. ``origin/<feature_branch>`` head as observed by
           ``git rev-parse``.
        4. The bootstrap env var ``AED_AUTHORITATIVE_HEAD``
           (which may be stale if a worker push advanced
           the head between human edits and supervisor
           restart).

    The bootstrap env var is acceptable as a cold-start
    guess but MUST be reconciled against the live PR head
    at the first heartbeat. Without this, a long-running
    autonomous supervisor requires a human/systemd edit
    after every legitimate worker push.
    """
    import json as _json
    import subprocess as _subprocess
    log(
        "info",
        "round-40 reconciling authoritative head at boot",
        bootstrap=globals().get("AUTHORITATIVE_HEAD"),
    )
    candidates: list[tuple[str, str]] = []

    # 1. Live PR head.
    try:
        live = github_get(
            f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}",
            get_github_token() or "",
        )
        if isinstance(live, dict):
            live_head = str(live.get("head", {}).get("sha") or "").strip()
            if live_head:
                candidates.append(("live_pr", live_head))
    except Exception as exc:  # noqa: BLE001
        log(
            "warning",
            "round-40 head reconciliation: live PR fetch failed",
            error=str(exc)[:200],
        )

    # 2. run_state.json current_head.
    try:
        rs_text = str(Path(RUN_STATE).read_text(encoding="utf-8"))
        rs_d = _json.loads(rs_text)
        rs_head = str(rs_d.get("current_head") or "").strip()
        if rs_head:
            candidates.append(("run_state", rs_head))
    except Exception as exc:  # noqa: BLE001
        log(
            "warning",
            "round-40 head reconciliation: run_state read failed",
            error=str(exc)[:200],
        )

    # 3. origin/<branch> head.
    try:
        branch = str(
            os.environ.get("AED_BRANCH")
            or globals().get("FEATURE_BRANCH", "")
            or ""
        ).strip() or "feat/review-repair-relay-v1"
        origin_head = _subprocess.check_output(
            ["git", "rev-parse", f"origin/{branch}"],
            cwd=str(REPO_DIR),
            stderr=_subprocess.DEVNULL,
            text=True,
            timeout=10,
        ).strip()
        if origin_head:
            candidates.append(("origin_branch", origin_head))
    except Exception as exc:  # noqa: BLE001
        log(
            "warning",
            "round-40 head reconciliation: origin head fetch failed",
            error=str(exc)[:200],
        )

    # 4. bootstrap env var.
    bootstrap = str(globals().get("AUTHORITATIVE_HEAD") or "").strip()
    if bootstrap:
        candidates.append(("bootstrap_env", bootstrap))

    # The live PR head is the canonical truth. If we have it,
    # use it. Otherwise, prefer the run_state value, then
    # the origin branch, then the bootstrap env var.
    chosen_source = ""
    chosen_head = ""
    for source in ("live_pr", "run_state", "origin_branch", "bootstrap_env"):
        for s, h in candidates:
            if s == source and h:
                chosen_source = s
                chosen_head = h
                break
        if chosen_head:
            break
    if chosen_head:
        globals()["AUTHORITATIVE_HEAD"] = chosen_head
        # Round-40: persist the reconciled head back to
        # ``run_state.json`` so subsequent iterations see
        # the same canonical value via ``read_run_state``.
        # Without this, ``head`` in the iteration log keeps
        # showing the stale run_state value even after the
        # ``AUTHORITATIVE_HEAD`` rebind, masking the real
        # state.
        try:
            import json as _json
            try:
                _rs_text = Path(RUN_STATE).read_text(encoding="utf-8")
                _rs_d = _json.loads(_rs_text)
            except Exception:
                _rs_d = {}
            if not isinstance(_rs_d, dict):
                _rs_d = {}
            if _rs_d.get("current_head") != chosen_head:
                _rs_d["current_head"] = chosen_head
                Path(RUN_STATE).write_text(
                    _json.dumps(_rs_d, indent=2, sort_keys=True),
                    encoding="utf-8",
                )
        except Exception as exc:  # noqa: BLE001
            log(
                "warning",
                "round-40 run_state persistence failed; "
                "continuing with in-memory AUTHORITATIVE_HEAD",
                error=str(exc)[:200],
            )
        log(
            "info",
            "round-40 authoritative head reconciled at boot",
            source=chosen_source,
            head=chosen_head[:12],
        )
    return globals().get("AUTHORITATIVE_HEAD", "")


def _default_policy(cfg: SupervisorConfig) -> dict[str, Any]:
    return {
        "human_boundary": cfg.human_boundary,
        "required_review_providers_for_pr_416": list(
            cfg.required_review_providers
        ),
        "optional_review_providers_for_pr_416": list(
            cfg.optional_review_providers
        ),
        "provider_states_are_independent":
            cfg.provider_states_are_independent,
        "codex_quota_reset_at": cfg.provider_quota_reset.get(
            "codex"
        ),
        "post_codex_recovery_request":
            cfg.post_codex_recovery_request,
        "quiet_window_seconds": cfg.quiet_window_seconds,
        "heartbeat_seconds": cfg.heartbeat_seconds,
        "required_check_names": list(cfg.required_check_names),
    }


def _default_providers(cfg: SupervisorConfig) -> dict[str, dict[str, Any]]:
    """Build the PROVIDERS dict from the configuration.

    The provider definitions are intentionally minimal here —
    the long-term ``ReviewProvider`` abstraction lives in the
    standalone Autocoder extraction and is NOT introduced by
    this stabilisation PR. CodeRabbit is the canonical
    required provider for this PR; the optional Codex entry
    exists so the cross-provider pause and rate-limit logic
    remains identical to the original supervisor.
    """
    providers: dict[str, dict[str, Any]] = {}
    for name in cfg.required_review_providers:
        if name == "coderabbit":
            providers[name] = {
                "bot_logins": ["coderabbitai[bot]"],
                "trigger_handle": "@coderabbitai review",
                "quota_patterns": [
                    re.compile(r"rate limit", re.IGNORECASE),
                    re.compile(r"usage.{0,30}limit", re.IGNORECASE),
                    re.compile(r"too many requests", re.IGNORECASE),
                ],
                "use_reviews_api": False,
                "required_for_current_repair_round": True,
                "required_for_final_merge": True,
                "required_for_pr_416": True,
                "quota_reset_at":
                    cfg.provider_quota_reset.get(name),
            }
        else:
            providers[name] = {
                "bot_logins": [f"{name}[bot]"],
                "trigger_handle": f"@{name} review",
                "quota_patterns": [],
                "use_reviews_api": True,
                "required_for_current_repair_round": True,
                "required_for_final_merge": True,
                "required_for_pr_416": True,
                "quota_reset_at":
                    cfg.provider_quota_reset.get(name),
            }
    for name in cfg.optional_review_providers:
        if name == "codex":
            providers[name] = {
                "bot_logins": ["chatgpt-codex-connector[bot]"],
                "trigger_handle": "@codex review",
                "quota_patterns": [
                    re.compile(
                        r"reached your.{0,20}codex usage limits",
                        re.IGNORECASE,
                    ),
                    re.compile(r"codex.{0,20}usage limit", re.IGNORECASE),
                    re.compile(r"rate limit", re.IGNORECASE),
                ],
                "use_reviews_api": True,
                "required_for_current_repair_round": False,
                "required_for_final_merge": False,
                "required_for_pr_416": False,
                "quota_reset_at":
                    cfg.provider_quota_reset.get(name),
            }
        else:
            providers[name] = {
                "bot_logins": [f"{name}[bot]"],
                "trigger_handle": f"@{name} review",
                "quota_patterns": [],
                "use_reviews_api": True,
                "required_for_current_repair_round": False,
                "required_for_final_merge": False,
                "required_for_pr_416": False,
                "quota_reset_at":
                    cfg.provider_quota_reset.get(name),
            }
    return providers


# Bootstrap: populate globals from the env-derived config.
#
# This bootstrap is ONLY used when no explicit SupervisorConfig
# is supplied (i.e. when ``main()`` runs without --config and
# without --isolated-state). Production startup always passes
# ``--config``, so the bootstrap is replaced by an explicit
# ``load_config`` call in ``main()``. The in-process default
# is therefore safe to be lenient about absolute user paths
# (which is necessary for ``default_config_from_env`` to
# populate ``working_checkout`` from $PWD for the
# package's own unit tests).
try:
    _BOOTSTRAPPED_FROM = default_config_from_env()
    _APPLIED = _apply_config(_BOOTSTRAPPED_FROM)
except (ValueError, OSError):
    # The env-derived config could not be built. Defer to a
    # minimal stub. Production callers always pass --config
    # and override this stub before any module function is
    # invoked. Unrelated failures (ImportError, etc.) are
    # re-raised by the bare-except branch below.
    _BOOTSTRAPPED_FROM = None  # type: ignore[assignment]
    _APPLIED = {}
if _BOOTSTRAPPED_FROM is not None:
    POLICY: dict[str, Any] = _default_policy(_BOOTSTRAPPED_FROM)
    PROVIDERS: dict[str, dict[str, Any]] = _default_providers(
        _BOOTSTRAPPED_FROM
    )
else:  # pragma: no cover — fallback only triggers on unusual
      # platforms
    POLICY = {  # type: ignore[assignment]
        "human_boundary": "merge_only",
        "required_review_providers_for_pr_416": ["coderabbit"],
        "optional_review_providers_for_pr_416": ["codex"],
        "provider_states_are_independent": True,
        "codex_quota_reset_at": None,
        "post_codex_recovery_request": False,
        "quiet_window_seconds": 180,
        "heartbeat_seconds": 120,
        "required_check_names": [],
    }
    PROVIDERS = {  # type: ignore[assignment]
        "coderabbit": {
            "bot_logins": ["coderabbitai[bot]"],
            "trigger_handle": "@coderabbitai review",
            "quota_patterns": [],
            "use_reviews_api": False,
            "required_for_current_repair_round": True,
            "required_for_final_merge": True,
            "required_for_pr_416": True,
            "quota_reset_at": None,
        },
        "codex": {
            "bot_logins": ["chatgpt-codex-connector[bot]"],
            "trigger_handle": "@codex review",
            "quota_patterns": [],
            "use_reviews_api": True,
            "required_for_current_repair_round": False,
            "required_for_final_merge": False,
            "required_for_pr_416": False,
            "quota_reset_at": None,
        },
    }
TERMINAL_CLASSIFICATIONS = {"ACTIVE_REPAIR_CODERABBIT_FINDINGS"}

# State machine (non-terminal while PR is open).
STATE_ACTIVE_REPAIR = "ACTIVE_REPAIR"
STATE_PROVISIONAL_READY = "PROVISIONAL_READY"
STATE_AWAITING_MERGE_AUTHORIZATION = "AWAITING_MERGE_AUTHORIZATION"

READINESS_STATES = {
    STATE_PROVISIONAL_READY,
    STATE_AWAITING_MERGE_AUTHORIZATION,
}

# Module-level locks
_LOCK_FD: Optional[int] = None


# Round-39 P1#8: module-level slot for the fresh event ids
# the next ``launch_worker`` call must persist on the
# WorkerAttemptRecord. ``handle_new_events`` populates this
# before invoking launch_worker so the WorkerAttemptRecord
# constructor can read it. The slot is RESET to ``None``
# immediately after the WorkerAttemptRecord is persisted
# so a subsequent launch_worker call (e.g. from a
# different code path) does not accidentally inherit
# stale ids.
_pending_launch_event_ids: Optional[tuple] = None


def supervisor_module_globals() -> dict[str, Any]:
    """Return the module-level globals for inspection.

    This is the public surface used by tests that want to
    inspect the supervisor's configuration without depending
    on private module attributes.
    """
    return dict(_APPLIED)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%FT%TZ")


def parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Logging and heartbeat
# ---------------------------------------------------------------------------


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def log(level: str, msg: str, **fields: Any) -> None:
    SUPERVISOR_HOME.mkdir(parents=True, exist_ok=True)  # type: ignore[name-defined]
    _ensure_parent(LOG_PATH)  # type: ignore[name-defined]
    record = {
        "ts": now_iso(),
        "level": level,
        "supervisor_instance": INSTANCE_ID,  # type: ignore[name-defined]
        "msg": msg,
        **fields,
    }
    with LOG_PATH.open("a") as f:  # type: ignore[name-defined]
        f.write(json.dumps(record) + "\n")
    print(f"[{record['ts']}] [{level}] {msg}", flush=True)


def heartbeat_touch() -> None:
    _ensure_parent(HEARTBEAT_PATH)  # type: ignore[name-defined]
    HEARTBEAT_PATH.write_text(now_iso())  # type: ignore[name-defined]


# ---------------------------------------------------------------------------
# Singleton lock
# ---------------------------------------------------------------------------


def acquire_lock() -> bool:
    global _LOCK_FD
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[name-defined]
    # Open (or create) the lock file with mode 0600 first,
    # before any write_text happens. This is the only way to
    # guarantee the lock file is never world-readable, even
    # briefly. The later chmod(0o600) is a belt-and-braces
    # measure for filesystems that do not honour the open
    # mode.
    _LOCK_FD = os.open(
        str(LOCK_PATH),  # type: ignore[name-defined]
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    try:
        fcntl.flock(_LOCK_FD, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(_LOCK_FD)
        _LOCK_FD = None
        return False
    # Always re-assert the file's mode in case the file
    # pre-existed with looser permissions from a previous
    # version of this code.
    try:
        os.fchmod(_LOCK_FD, 0o600)
    except OSError:
        pass
    os.ftruncate(_LOCK_FD, 0)
    os.write(
        _LOCK_FD,
        f"supervisor_instance={INSTANCE_ID} pid={os.getpid()} "  # type: ignore[name-defined]
        f"started={now_iso()}\n".encode(),
    )
    return True


# ---------------------------------------------------------------------------
# Run state + GitHub helpers
# ---------------------------------------------------------------------------


def read_run_state() -> dict:
    """Read the persistent ``RUN_STATE`` JSON document.

    Round-28 invariant: this function MUST NOT auto-persist
    ``STATE_DIR`` as the ``orchestration_state_root``. The
    supervisor's private ``STATE_DIR`` is conceptually separate
    from the orchestration controller/run state root, and the
    user's invariant forbids silently substituting them.

    The orchestration state root enters RUN_STATE via the
    canonical handoff path ``persist_orchestration_state_root``
    in ``autocoder_supervisor.orchestration_state_root``. This
    function only reads.

    Init safety:

      - Missing ``RUN_STATE`` file → return empty dict (the
        caller decides whether to initialize via
        ``init_run_state_safely``).
      - Existing ``RUN_STATE`` whose JSON parses → return as-is.
      - Existing ``RUN_STATE`` whose JSON does NOT parse →
        raise (the caller MUST route to BLOCKED / escalation
        rather than silently destroying supervisor state).
    """
    if not RUN_STATE.exists():  # type: ignore[name-defined]
        return {}
    try:
        text = RUN_STATE.read_text()  # type: ignore[name-defined]
    except OSError:
        # Unreadable file → return empty dict; the caller may
        # decide to recover via ``init_run_state_safely`` if
        # appropriate. We do NOT auto-overwrite here.
        return {}
    try:
        state = json.loads(text)
    except json.JSONDecodeError as exc:
        # Round-28 invariant: refuse to overwrite a corrupt run
        # state document. Re-raise so the caller can route to
        # BLOCKED / escalation.
        raise OrchestrationRootUnverified(
            f"RUN_STATE at {RUN_STATE} is not valid JSON: {exc!r}; "  # type: ignore[name-defined]
            "refusing to overwrite a corrupt run state document. "
            "Route to BLOCKED / escalation."
        ) from exc
    if not isinstance(state, dict):
        raise OrchestrationRootUnverified(
            f"RUN_STATE at {RUN_STATE} is not a JSON object: "  # type: ignore[name-defined]
            f"got {type(state).__name__}; refusing to overwrite."
        )
    return state


def get_github_token() -> Optional[str]:
    """Resolve the canonical GitHub bearer credential.

    Sources, in priority order:
        1. ``GITHUB_TOKEN_PR_AUTODEV`` env var — the operator's
           explicit override for the AutoDev PR-5 supervisor.
        2. The ``oauth_token: <token>`` line in ``TOKEN_FILE``
           (default: ``~/.config/gh/hosts.yml``) — the standard
           gh-CLI credential source.
    """
    env_token = os.environ.get("GITHUB_TOKEN_PR_AUTODEV") or ""
    if env_token:
        return env_token
    try:
        text = TOKEN_FILE.read_text()  # type: ignore[name-defined]
        m = re.search(r"oauth_token:\s+(\S+)", text)
        return m.group(1) if m else None
    except Exception:
        return None


def github_token_source() -> str:
    """Return a non-secret label for the credential source.

    Used for diagnostic logging — NEVER include the value.
    """
    if os.environ.get("GITHUB_TOKEN_PR_AUTODEV"):
        return "env"
    try:
        if Path(TOKEN_FILE).exists():  # type: ignore[name-defined]
            return "hosts_yml"
    except Exception:
        pass
    return "none"


def github_get(
    path: str,
    token: str,
    *,
    _reload_token: bool = False,
    _retry_on_401: bool = True,
) -> Optional[Any]:
    """Round-38 hardened HTTP GET.

    Behaviour:
        - When ``_reload_token`` is True the caller passes no
          token and ``get_github_token()`` is invoked on every
          call so a credential rotated by ``gh auth login`` is
          picked up without a supervisor restart.
        - When ``_retry_on_401`` is True (default) and the
          first attempt returns 401, the canonical credential
          source is re-read and the request retried exactly
          once. This recovers from the ``gh auth refresh``
          case where the cached hosts.yml has been rotated.
        - All diagnostic logging is non-secret.
    """
    if _reload_token and not token:
        token = get_github_token() or ""
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    # Round-37: log only whether a token is configured; never
    # expose token material (prefix, length, or contents).
    log(
        "debug",
        "github_get call",
        path=path[:80],
        token_configured=bool(token),
        credential_source=github_token_source(),
    )
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if _retry_on_401 and e.code == 401:
            # Re-read the canonical credential and retry.
            fresh_token = get_github_token() or ""
            if fresh_token and fresh_token != token:
                log(
                    "info",
                    "github_get retrying after credential reload",
                    path=path[:80],
                    previous_source=github_token_source(),
                )
                req_retry = urllib.request.Request(
                    url,
                    headers={
                        "Authorization": f"Bearer {fresh_token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                try:
                    with urllib.request.urlopen(
                        req_retry, timeout=20
                    ) as r:
                        return json.loads(r.read())
                except urllib.error.HTTPError as e2:
                    log(
                        "warning",
                        "github_get http error after credential reload",
                        path=path,
                        code=e2.code,
                    )
                    return None
                except Exception as e2:
                    log(
                        "warning",
                        "github_get failed after credential reload",
                        path=path,
                        error=str(e2),
                    )
                    return None
            log(
                "warning",
                "github_get http error; no refreshed credential available",
                path=path,
                code=401,
            )
            return None
        log("warning", "github_get http error", path=path, code=e.code)
        return None
    except Exception as e:
        log("warning", "github_get failed", path=path, error=str(e))
        return None


def is_bot_for_provider(provider: str, login: str) -> bool:
    if provider not in PROVIDERS:
        return False
    return login in PROVIDERS[provider]["bot_logins"]


def inspect_live_state(token: str) -> dict:
    state = {
        "head_sha": None,
        "head_match": False,
        "latest_reviews_by_provider": {},
        "latest_comments_by_provider": {},
        "latest_bot_login": None,
        "latest_bot_body": None,
        "mergeable": None,
        "gate_status": None,
        "ci_in_progress": False,
    }
    pr = github_get(
        f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}",  # type: ignore[name-defined]
        token,
    )
    if pr:
        state["head_sha"] = pr.get("head", {}).get("sha")
        state["head_match"] = (
            state["head_sha"] == AUTHORITATIVE_HEAD  # type: ignore[name-defined]
        )
        state["mergeable"] = pr.get("mergeable")
    # Round-37: per_page=100 — GitHub caps each page at 100, so
    # 50 (the round-35 value) silently hides every review
    # submitted at index >50. With 60+ reviews on PR #5 today
    # (CodeRabbit + Codex both submit fresh exact-head reviews
    # against the live C head), per_page=20/50 left the
    # supervisor blind to the most recent actionable findings.
    reviews = github_get(
        f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}/reviews"  # type: ignore[name-defined]
        f"?per_page=100",
        token,
    )
    if reviews:
        for provider, cfg in PROVIDERS.items():
            if not cfg.get("use_reviews_api"):
                continue
            for r in reviews:
                if (
                    r.get("commit_id") == AUTHORITATIVE_HEAD  # type: ignore[name-defined]
                    and r.get("user", {}).get("login")
                    in cfg["bot_logins"]
                ):
                    state["latest_reviews_by_provider"][provider] = {
                        "id": r["id"],
                        "submitted_at": r.get("submitted_at"),
                        "state": r.get("state"),
                    }
                    break
    per_page = 100
    seen_providers = set()
    page = 1
    while page <= 5:
        page_url = (
            f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{PR_NUMBER}/comments"  # type: ignore[name-defined]
            f"?per_page={per_page}&page={page}"
        )
        comments = github_get(page_url, token)
        if not comments:
            break
        for c in reversed(comments):
            login = c.get("user", {}).get("login")
            for provider, cfg in PROVIDERS.items():
                if (
                    is_bot_for_provider(provider, login)
                    and provider not in seen_providers
                ):
                    state["latest_comments_by_provider"][provider] = {
                        "id": c["id"],
                        "user": login,
                        "created_at": c.get("created_at"),
                        "body": (c.get("body") or "")[:500],
                    }
                    seen_providers.add(provider)
                    if (
                        state["latest_bot_login"] is None
                        or (c.get("id", 0) > state.get("latest_bot_id", 0))
                    ):
                        state["latest_bot_login"] = login
                        state["latest_bot_body"] = (
                            c.get("body") or ""
                        )[:500]
                        state["latest_bot_id"] = c.get("id", 0)
                    break
        if len(seen_providers) == len(PROVIDERS):
            break
        page += 1
    return state


# ---------------------------------------------------------------------------
# Review request records
# ---------------------------------------------------------------------------


def review_request_path(provider: str, head_sha: str) -> Path:
    return REVIEW_REQUESTS_DIR / f"{provider}__{head_sha}.json"  # type: ignore[name-defined]


def list_review_requests() -> list:
    if not REVIEW_REQUESTS_DIR.exists():  # type: ignore[name-defined]
        return []
    out = []
    for p in REVIEW_REQUESTS_DIR.glob("*.json"):  # type: ignore[name-defined]
        try:
            out.append(json.loads(p.read_text()))
        except Exception:
            continue
    return out


def write_review_request(
    provider: str, head_sha: str, record: dict
) -> None:
    write_json(review_request_path(provider, head_sha), record)


def read_review_request(
    provider: str, head_sha: str
) -> Optional[dict]:
    p = review_request_path(provider, head_sha)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Quota state
# ---------------------------------------------------------------------------


def read_quota_state() -> dict:
    try:
        return json.loads(QUOTA_PATH.read_text())  # type: ignore[name-defined]
    except FileNotFoundError:
        return {}
    except Exception as e:
        log("warning", "quota_state read failed", error=str(e))
        return {}


def write_quota_state(state: dict) -> None:
    write_json(QUOTA_PATH, state)  # type: ignore[name-defined]


def is_provider_quota_message(
    provider: str, body: Optional[str]
) -> bool:
    if not body:
        return False
    if provider not in PROVIDERS:
        return False
    return any(p.search(body) for p in PROVIDERS[provider]["quota_patterns"])


def is_provider_walkthrough_or_complete(
    provider: str, body: Optional[str]
) -> bool:
    if not body or provider != "coderabbit":
        return False
    body_low = body.lower()
    return (
        "review in progress" in body_low
        or "currently processing" in body_low
        or "walkthrough" in body_low
        or "<!-- this is an auto-generated comment: review"
        in body_low
    )


def quota_state_for_provider(state: dict, provider: str) -> dict:
    if "providers" in state:
        return state["providers"].get(provider, {})
    if provider == "codex":
        return state
    return {}


def set_quota_state_for_provider(
    state: dict, provider: str, sub: dict
) -> dict:
    if "providers" not in state:
        state = {"providers": {"codex": state}}
    state["providers"][provider] = sub
    return state


def enter_provider_quota_pause(
    provider: str, reason: str, pending_head: str,
) -> dict:
    full_state = read_quota_state()
    existing = quota_state_for_provider(full_state, provider)
    retry_count = existing.get("retry_count", 0) + 1
    sub = {
        "classification": f"PAUSED_PROVIDER_QUOTA_{provider.upper()}",
        "provider": provider,
        "pending_review_head": pending_head,
        "last_review_request_timestamp":
            existing.get("last_review_request_timestamp") or now_iso(),
        "last_quota_response_timestamp": now_iso(),
        "retry_count": retry_count,
        "reason": reason,
        "transitioned_at": now_iso(),
    }
    base = (
        QUOTA_RETRY_INITIAL_SECS  # type: ignore[name-defined]
        if retry_count < QUOTA_BACKOFF_AFTER_RETRY_COUNT  # type: ignore[name-defined]
        else QUOTA_RETRY_BACKOFF_SECS  # type: ignore[name-defined]
    )
    sub["next_retry_timestamp"] = (
        datetime.now(timezone.utc) + timedelta(seconds=base)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_state = set_quota_state_for_provider(full_state, provider, sub)
    write_quota_state(new_state)
    log(
        "warning",
        f"entered provider-quota pause for {provider}",
        provider=provider,
        retry_count=retry_count,
        pending_review_head=pending_head[:12],
        next_retry=sub["next_retry_timestamp"],
        reason=reason,
    )
    return sub


def clear_provider_quota_state(provider: str) -> Optional[dict]:
    full_state = read_quota_state()
    sub = quota_state_for_provider(full_state, provider)
    if not sub:
        return None
    log(
        "info",
        f"clearing provider-quota state for {provider}",
        previous_retry_count=sub.get("retry_count"),
    )
    if "providers" in full_state:
        full_state["providers"].pop(provider, None)
        if not full_state["providers"]:
            try:
                QUOTA_PATH.unlink()  # type: ignore[name-defined]
            except FileNotFoundError:
                pass
        else:
            write_quota_state(full_state)
    else:
        try:
            QUOTA_PATH.unlink()  # type: ignore[name-defined]
        except FileNotFoundError:
            pass
    return sub


# ---------------------------------------------------------------------------
# Worker lease
# ---------------------------------------------------------------------------


def boot_time_jiffies() -> int:
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    return int(line.split()[1]) * os.sysconf(
                        "SC_CLK_TCK"
                    )
    except Exception:
        pass
    return 0


CLK_TCK = os.sysconf("SC_CLK_TCK") or 100


def start_time_evidence(pid: int) -> dict:
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        rparen = stat.rfind(")")
        if rparen < 0:
            return {}
        fields = stat[rparen + 1:].split()
        starttime_ticks = int(fields[19])
        return {
            "clock_ticks_since_boot": starttime_ticks,
            "abs_clock": boot_time_jiffies() + starttime_ticks,
        }
    except Exception as e:
        return {"error": str(e)}


def start_time_matches(pid: int, evidence: dict) -> bool:
    if not evidence:
        return False
    current = start_time_evidence(pid)
    if not current or "error" in current:
        return False
    return (
        current.get("clock_ticks_since_boot")
        == evidence.get("clock_ticks_since_boot")
    )


def read_lease() -> Optional[dict]:
    try:
        return json.loads(LEASE_PATH.read_text())  # type: ignore[name-defined]
    except FileNotFoundError:
        return None
    except Exception as e:
        log("warning", "lease read failed", error=str(e))
        return None


def write_lease(lease: dict) -> None:
    write_json(LEASE_PATH, lease)  # type: ignore[name-defined]


def remove_lease() -> None:
    try:
        LEASE_PATH.unlink(missing_ok=True)  # type: ignore[name-defined]
    except Exception:
        pass


def pid_alive(pid: int) -> bool:
    """Return True iff the process exists AND is alive.

    The Round-37 implementation only used ``os.kill(pid, 0)``.
    That signal-0 probe succeeds for zombie processes (the
    kernel still owns the PID), which kept the worker lease
    stuck for minutes after a worker died. Round-38 reads
    ``/proc/<pid>/status`` and returns False for the ``Z``
    (zombie) state so a dead child's exit is recognised on
    the next poll even while the OS still owns the PID.

    Returns False when the PID is gone, when it is a zombie,
    and when ``/proc`` is unavailable. Returns True for any
    other liveness state (``R``, ``S``, ``D``, ``T``, ``I``).
    """
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except FileNotFoundError:
        # The proc entry is gone. The PID is truly dead.
        return False
    except (OSError, PermissionError):
        # Round-39 P1#1: an unreadable /proc/<pid>/status
        # (transient I/O error, EMFILE, permission policy)
        # MUST NOT classify the live worker as dead. The
        # historical signal-0 probe is the conservative
        # fallback: it succeeds for any process the kernel
        # still owns, including live PIDs whose /proc is
        # momentarily inaccessible. Returning False here
        # would let poll_worker_attempt() terminalize the
        # attempt, release the lease, and spawn a duplicate
        # worker, breaking the single-writer invariant.
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    except Exception:
        # Any other exception: fall back to the signal-0
        # probe so an obscure /proc read failure doesn't
        # silently kill the lease.
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    for line in text.splitlines():
        if line.startswith("State:"):
            parts = line.split()
            if len(parts) >= 2:
                state = parts[1]
                # Z = zombie, X = dead. Both mean "exited".
                return state not in ("Z", "X", "")
    # Couldn't parse /proc; fall back to the original probe.
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but is owned by another user.
        # Treating it as dead would allow a second writer to
        # start, breaking the single-writer invariant.
        return True


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
            parts = line.split()
            if len(parts) >= 2:
                return parts[1]
    return None


def _reap_via_waitid(pid: int) -> Optional[tuple[Optional[int], Optional[int]]]:
    """Best-effort nonblocking reap of a child PID.

    Returns ``(exit_code, signal)`` if the child has exited,
    ``None`` when the child is still running. Raises are
    caught and converted to ``None`` (caller decides).
    """
    try:
        result = os.waitid(os.P_PID, pid, os.WNOHANG)
    except ChildProcessError:
        return None
    except OSError:
        return None
    status = getattr(result, "si_status", None) if result else None
    if status is None:
        return None
    try:
        if os.WIFEXITED(status):
            return (os.WEXITSTATUS(status), None)
        if os.WIFSIGNALED(status):
            return (None, os.WTERMSIG(status))
    except (AttributeError, OSError):
        return None
    return None


def pgid_alive(pgid: int) -> bool:
    """Return True iff the process group exists.

    Identical semantics to ``pid_alive`` for the same
    single-writer safety reason. A zombie process group is
    considered NOT alive so a stuck child cannot indefinitely
    hold the lease.
    """
    try:
        os.kill(-pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # The OS still answers signal-0 for a zombie pgid; check
    # /proc for at least one live process in the group.
    for entry in Path("/proc").iterdir():
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        try:
            status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            continue
        for line in status.splitlines():
            if line.startswith("Tgid:") and line.split()[1] == str(pid):
                # Cheap PGID check via stat.
                try:
                    if (
                        Path(f"/proc/{pid}/stat").read_text(
                            encoding="utf-8"
                        ).split()[4] == str(pgid)
                        and _read_proc_state(pid) not in ("Z", "X", "")
                    ):
                        return True
                except (OSError, IndexError):
                    continue
    return False


# ---------------------------------------------------------------------------
# Round-36: WorkerAttemptRecord helpers
# ---------------------------------------------------------------------------

WORKER_ATTEMPTS_DIR = Path(str(STATE_DIR)) / "worker_attempts"  # type: ignore[name-defined]


def _worker_attempt_store():
    """Return the canonical store for worker attempts."""
    from autocoder_orchestration.worker_attempt import WorkerAttemptStore
    return WorkerAttemptStore(WORKER_ATTEMPTS_DIR)


# ---------------------------------------------------------------------------
# Round-46 C14: thread dispositions + terminal lifecycle closure
# ---------------------------------------------------------------------------
#
# Round-44 C13 fixed the relay-side directive focus. Round-46
# C14 fixes the SUPERVISOR-SIDE event-consumption contract: when
# a focused worker returns a valid terminal disposition (REPAIRED,
# ALREADY_SATISFIED, SUPERSEDED) for the targeted thread at the
# current exact head, the supervisor MUST consume the corresponding
# ``unresolved_thread_drain:<tid>`` event exactly once so the same
# unchanged thread is not redundantly redispatched.

THREAD_DISPOSITIONS_LEDGER_VERSION = "round46_c14_v1"
THREAD_DISPOSITION_REPAIRED = "REPAIRED"
THREAD_DISPOSITION_ALREADY_SATISFIED = "ALREADY_SATISFIED"
THREAD_DISPOSITION_SUPERSEDED = "SUPERSEDED"
THREAD_DISPOSITION_STILL_ACTIONABLE = "STILL_ACTIONABLE"
THREAD_DISPOSITION_INCOMPLETE_EVIDENCE = "INCOMPLETE_EVIDENCE"
THREAD_DISPOSITION_RESOLUTION_PENDING = "RESOLUTION_PENDING"
TERMINAL_THREAD_DISPOSITIONS = frozenset({
    THREAD_DISPOSITION_REPAIRED,
    THREAD_DISPOSITION_ALREADY_SATISFIED,
    THREAD_DISPOSITION_SUPERSEDED,
})
NONTERMINAL_THREAD_DISPOSITIONS = frozenset({
    THREAD_DISPOSITION_STILL_ACTIONABLE,
    THREAD_DISPOSITION_INCOMPLETE_EVIDENCE,
})
# Round-46 C14: legacy alias normalization. Historical values
# used by older rounds map to the canonical taxonomy per
# Section 3. The mapping table is the canonical source of
# truth; new code MUST consult this table before persisting.
LEGACY_DISPOSITION_ALIASES = {
    "FIXED": THREAD_DISPOSITION_REPAIRED,
    "REPAIRED": THREAD_DISPOSITION_REPAIRED,
    "TERMINAL_REPAIRED": THREAD_DISPOSITION_REPAIRED,
    "B_ALREADY_SATISFIED": THREAD_DISPOSITION_ALREADY_SATISFIED,
    "B": THREAD_DISPOSITION_ALREADY_SATISFIED,
    "ALREADY_SATISFIED": THREAD_DISPOSITION_ALREADY_SATISFIED,
    "NOT_ACTIONABLE": THREAD_DISPOSITION_ALREADY_SATISFIED,
    "C_SUPERSEDED": THREAD_DISPOSITION_SUPERSEDED,
    "C": THREAD_DISPOSITION_SUPERSEDED,
    "SUPERSEDED": THREAD_DISPOSITION_SUPERSEDED,
    "STALE": THREAD_DISPOSITION_SUPERSEDED,
    "OBSOLETE": THREAD_DISPOSITION_SUPERSEDED,
    "DUPLICATE": THREAD_DISPOSITION_SUPERSEDED,
    "D_REPAIRED": THREAD_DISPOSITION_REPAIRED,
    "D": THREAD_DISPOSITION_REPAIRED,
    "A_STILL_ACTIONABLE": THREAD_DISPOSITION_STILL_ACTIONABLE,
    "A": THREAD_DISPOSITION_STILL_ACTIONABLE,
    "REAL_REPAIR_REQUIRED": THREAD_DISPOSITION_STILL_ACTIONABLE,
    "STILL_ACTIONABLE": THREAD_DISPOSITION_STILL_ACTIONABLE,
    "INSUFFICIENT_EVIDENCE": THREAD_DISPOSITION_INCOMPLETE_EVIDENCE,
    "INCOMPLETE_EVIDENCE": THREAD_DISPOSITION_INCOMPLETE_EVIDENCE,
    "RESOLUTION_PENDING": THREAD_DISPOSITION_RESOLUTION_PENDING,
}


def _thread_dispositions_ledger_path():
    """Round-46 C14: per-repo per-PR ledger of durable thread
    dispositions. The path lives next to ``run_state.json``
    in ``$HOME/.hermes/aed/runs/<owner>/<repo>/<pr>/`` so the
    ledger survives supervisor restarts and shares scope with
    the round dispatch summaries already at that location.
    The ledger is append-only per generation; the
    ``idempotent_consume_thread_drain_event`` predicate treats
    duplicate rows as no-op.
    """
    from pathlib import Path as _P
    import os as _os
    _home = _os.environ.get("HOME") or "~"
    return _P(_home) / ".hermes" / "aed" / "runs" / str(
        REPO_OWNER  # type: ignore[name-defined]
    ) / str(
        REPO_NAME  # type: ignore[name-defined]
    ) / str(
        PR_NUMBER  # type: ignore[name-defined]
    ) / "thread_dispositions.jsonl"


# Round-49.1 C17: source-safe, provider-versioned, ancestry-bound
# thread-proof audit ledger. Replaces the C16 dependency-aware
# carry-forward index with a stricter model:
#
#   - Tier-1 dependency uses ``git rev-parse H:P`` (source blob
#     identity) instead of "path still exists + line within 10".
#   - Provider thread fingerprint is computed from the FULL
#     available thread state (replies, updatedAt, full body
#     digests) instead of a 500-char truncated prefix.
#   - Carry-forward REQUIRES ``git merge-base --is-ancestor
#     proof_head current_head`` (ancestry proof).
#   - REPAIRED dispositions carry forward ONLY when the source
#     blob is unchanged AND the provider thread is unchanged.
#     A "repaired file with the same line" is NOT a safe proof.
#   - Every carry/invalidate decision emits an audit record
#     (``THREAD_PROOF_CARRIED_FORWARD`` / ``THREAD_PROOF_INVALIDATED``)
#     with explicit reason codes.
#   - Migration tool reconstructs terminal proof from real durable
#     evidence (worker result artifacts) for threads whose C14
#     ledger never persisted.
#   - Historical threads whose evidence CANNOT be recovered are
#     marked ``HISTORICAL_TERMINAL_PROOF_UNRECOVERABLE`` and are
#     explicitly eligible for one focused re-evaluation.



def _has_recorded_thread_proof(thread_id):
    """Round-50 fix: return True if the audit ledger has a
    THREAD_PROOF_RECORDED for ``thread_id`` with a terminal
    disposition. Used by the durable-thread drain emitter to
    prevent re-emitting drain events for threads whose proof
    was already recorded (e.g., via prior worker round or
    external reconciliation).
    """
    if not thread_id:
        return False
    import json as _json
    p = _thread_proof_audit_path()
    if not p.exists():
        return False
    try:
        with p.open("r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = _json.loads(ln)
                except Exception:
                    continue
                if rec.get("kind") != "THREAD_PROOF_RECORDED":
                    continue
                if rec.get("thread_id") != thread_id:
                    continue
                if rec.get("disposition") in TERMINAL_THREAD_DISPOSITIONS:
                    return True
    except OSError:
        return False
    return False


# Round-50.1: work-generation lifecycle enum (Section 15).
WORK_GEN_PENDING = "WORK_GEN_PENDING"
WORK_GEN_CLAIMED = "WORK_GEN_CLAIMED"
WORK_GEN_WORKER_RUNNING = "WORK_GEN_WORKER_RUNNING"
WORK_GEN_TERMINAL = "WORK_GEN_TERMINAL"
WORK_GEN_RETRY_PENDING = "WORK_GEN_RETRY_PENDING"
WORK_GEN_UNRECOVERABLE = "WORK_GEN_UNRECOVERABLE"

# Round-50.1: work-generation ledger file path (per-PR scope).
WORK_PROOF_GENERATION_AUDIT_FILENAME = "thread_work_generation_audit.jsonl"
WORK_PROOF_GENERATION_AUDIT_VERSION = "round49_1_c17_v1"  # bump on schema change


def _work_generation_audit_path():
    from pathlib import Path as _P
    import os as _os
    _home = _os.environ.get("HOME") or "~"
    return _P(_home) / ".hermes" / "aed" / "runs" / str(
        REPO_OWNER  # type: ignore[name-defined]
    ) / str(
        REPO_NAME  # type: ignore[name-defined]
    ) / str(
        PR_NUMBER  # type: ignore[name-defined]
    ) / WORK_PROOF_GENERATION_AUDIT_FILENAME


def _round50_1_compute_generation_id(
    *, thread_id, provider, provider_version, source_blob_sha,
    evaluated_head,
):
    """Round-50.1: deterministic generation identity.
    Bound to (thread_id, provider, provider_version, source_blob_sha,
    evaluated_head). A material change in any of these
    (new reply, source edit, head change) yields a new
    generation.
    """
    import hashlib as _h
    payload = "|".join([
        str(REPO_OWNER or ""), str(int(PR_NUMBER or 0)),
        str(provider or ""), str(thread_id or ""),
        str(provider_version or ""), str(source_blob_sha or ""),
        str(evaluated_head or ""),
    ]).encode("utf-8")
    return _h.sha256(payload).hexdigest()[:16]


def _round50_1_lookup_work_generation_state(
    *, thread_id, generation_id,
):
    """Round-50.1: return the latest lifecycle state for
    ``thread_id`` + ``generation_id`` from the work-generation
    ledger. Returns None when no record exists.
    """
    import json as _json
    p = _work_generation_audit_path()
    if not p.exists():
        return None
    latest_state = None
    latest_ts = ""
    try:
        with p.open("r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = _json.loads(ln)
                except Exception:
                    continue
                if rec.get("kind") not in (
                    "WORK_GENERATION_RECORDED",
                    "WORK_GENERATION_TRANSITION",
                ):
                    continue
                if rec.get("thread_id") != thread_id:
                    continue
                if rec.get("generation_id") != generation_id:
                    continue
                ts = rec.get("recorded_at") or ""
                if latest_ts is None or ts > latest_ts:
                    latest_ts = ts
                    latest_state = rec.get("state")
    except OSError:
        return None
    return latest_state


def _round50_1_record_work_generation_state(
    *, thread_id, provider, provider_version, source_blob_sha,
    evaluated_head, generation_id, state,
):
    """Round-50.1: append a state-transition record to the
    work-generation audit. The record is idempotent on
    ``state`` (re-writing the same state for the same
    generation does NOT create a duplicate row).
    """
    import json as _json
    import os as _os
    p = _work_generation_audit_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    # Idempotent check: if the latest row for this generation
    # already carries ``state``, skip the append.
    latest = _round50_1_lookup_work_generation_state(
        thread_id=thread_id, generation_id=generation_id,
    )
    if latest == state:
        return False
    rec = {
        "kind": "WORK_GENERATION_TRANSITION",
        "schema_version": WORK_PROOF_GENERATION_AUDIT_VERSION,
        "recorded_at": now_iso(),
        "thread_id": thread_id,
        "provider": provider or "",
        "provider_version": provider_version or "",
        "source_blob_sha": source_blob_sha or "",
        "evaluated_head": evaluated_head or "",
        "generation_id": generation_id,
        "state": state,
    }
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, sort_keys=True) + "\n")
            f.flush()
            _os.fsync(f.fileno())
    except OSError as exc:
        log(
            "warning",
            "round-50.1: work-generation audit write failed",
            thread_id=thread_id,
            error=str(exc)[:200],
        )
        return False
    return True


def _has_recent_thread_invalidated(thread_id, current_head):
    """Round-50 narrow fix: return True if the audit ledger has
    a ``THREAD_PROOF_INVALIDATED`` record for ``thread_id``
    against the current head. This prevents the durable-thread
    drain emitter from re-emitting drain events for threads
    whose C17 audit has already investigated at the current
    head. The investigation happens at the current head on
    every drain iteration; emitting again would dispatch
    duplicate workers without new information.
    """
    if not thread_id or not current_head:
        return False
    import json as _json
    p = _thread_proof_audit_path()
    if not p.exists():
        return False
    try:
        with p.open("r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = _json.loads(ln)
                except Exception:
                    continue
                if rec.get("kind") != "THREAD_PROOF_INVALIDATED":
                    continue
                if rec.get("thread_id") != thread_id:
                    continue
                if rec.get("current_head") == current_head:
                    return True
    except OSError:
        return False
    return False


def _round50_1_has_open_work_generation(thread_id, current_head):
    """Round-50.1: return True if there is an open
    (non-terminal) work generation for ``thread_id``
    whose ``evaluated_head`` matches ``current_head``.
    Used by the durable-thread drain emitter to skip
    re-emission when work already exists (Section 12,
    exactly-once work generation).

    Implementation: return True only if the MOST RECENT
    transition for ``(thread_id, current_head)`` is in an
    open-state set. Terminal transitions cancel the open
    state.
    """
    if not thread_id or not current_head:
        return False
    import json as _json
    p = _work_generation_audit_path()
    if not p.exists():
        return False
    latest_state = None
    latest_ts = ""
    try:
        with p.open("r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = _json.loads(ln)
                except Exception:
                    continue
                if rec.get("kind") != "WORK_GENERATION_TRANSITION":
                    continue
                if rec.get("thread_id") != thread_id:
                    continue
                if rec.get("evaluated_head") != current_head:
                    continue
                ts = rec.get("recorded_at") or ""
                # On ties (same second), the last-written row
                # wins so transitions within the same second
                # still take effect.
                if latest_state is None or ts > latest_ts:
                    latest_ts = ts
                    latest_state = rec.get("state")
                elif ts == latest_ts:
                    latest_state = rec.get("state")
    except OSError:
        return False
    if latest_state is None:
        return False
    return latest_state in (
        WORK_GEN_PENDING,
        WORK_GEN_CLAIMED,
        WORK_GEN_WORKER_RUNNING,
        WORK_GEN_RETRY_PENDING,
    )


def _has_runnable_repair_generation() -> bool:
    """Round-51/C19 Objective 2: REPAIR-BEFORE-QUALIFICATION.

    Return True iff there is at least one unconsumed
    actionable event whose owner is NOT an active worker.
    The check covers:

      (1) actionable events in the unconsumed ledger that
          are NOT in the launched_events set (no owner ever
          claimed them), OR
      (2) actionable events that ARE in launched_events but
          whose owning worker lease is no longer alive (the
          worker died, the lease was released, but the
          launched marker was not unmarked because
          poll_worker_attempt only unmarks the LAST dispatched
          event id, not every event in the lease's
          last_dispatched_event_ids list).

    The check covers all actionable event kinds:
      - head_change
      - new_formal_review
      - new_reviewer_issue_comment
      - new_unresolved_current_thread
      - thread_reopened
      - unresolved_thread_drain:<tid> (durable drain)
      - check_changed:<name> (only when it carries
        actionable evidence: a required check that went
        from green to red, or a check that newly appeared
        in the required set)

    CI checks that perpetually change state without
    any corresponding actionable repair work are NOT
    considered runnable repair. The quiet window's
    snapshot_differs function surfaces these as
    ``check_conclusion_change`` reasons; the main loop
    uses this helper to skip the quiet window ONLY when
    there is real repair work to dispatch.
    """
    try:
        unconsumed = list_unconsumed_events()
    except Exception:
        return False
    if not unconsumed:
        return False
    actionable_kinds = {
        "head_change",
        "new_formal_review",
        "new_reviewer_issue_comment",
        "new_unresolved_current_thread",
        "thread_reopened",
        "unresolved_thread_drain",
    }
    actionable_ids = [
        e.get("id", "")
        for e in unconsumed
        if e.get("kind") in actionable_kinds
    ]
    actionable_ids = [eid for eid in actionable_ids if eid]
    if not actionable_ids:
        return False
    try:
        already_launched = launched_event_ids()
    except Exception:
        already_launched = set()
    # Check (1): undispatched actionable events.
    for eid in actionable_ids:
        if eid not in already_launched:
            return True
    # Check (2): actionable events that ARE in launched_events
    # but whose owning worker is dead (lease released).
    try:
        lease = read_lease()
        lease_alive_owner = (
            lease.get("attempt_id") if lease is not None else None
        )
    except Exception:
        lease_alive_owner = None
    if lease_alive_owner is not None:
        # An active worker is already responsible for all
        # launched events; no need to redispatch.
        return False
    # No active lease: every launched_event id is owned by
    # a dead worker. Any actionable event still in
    # launched_events must be unblocked so a new attempt
    # can claim it.
    for eid in actionable_ids:
        if eid in already_launched:
            return True
    return False


THREAD_PROOF_AUDIT_VERSION = "round49_1_c17_v1"
THREAD_PROOF_AUDIT_FILENAME = "thread_proof_audit.jsonl"
THREAD_PROOF_INVALIDATION_VERSION = "round49_1_c17_v1"

# Re-export the worker-result constants for tests that import the
# supervisor module directly (Section 5 canonical contract).
try:
    from autocoder_orchestration.worker_attempt import (
        WORKER_RESULT_SCHEMA_VERSION,
        RESULT_TYPE_NO_CHANGES_REQUIRED,
        RESULT_TYPE_REPAIR_PUSHED,
        RESULT_TYPE_REPAIR_COMMIT_PRODUCED,
        RESULT_TYPE_COMMIT_PRODUCED_NOT_PUSHED,
        RESULT_TYPE_WORKER_EXECUTION_FAILED,
    )
except Exception:  # pragma: no cover - import fallback
    WORKER_RESULT_SCHEMA_VERSION = "autocoder.worker_result.v1"
    RESULT_TYPE_NO_CHANGES_REQUIRED = "NO_CHANGES_REQUIRED"
    RESULT_TYPE_REPAIR_PUSHED = "REPAIR_PUSHED"
    RESULT_TYPE_REPAIR_COMMIT_PRODUCED = "REPAIR_COMMIT_PRODUCED"
    RESULT_TYPE_COMMIT_PRODUCED_NOT_PUSHED = "COMMIT_PRODUCED_NOT_PUSHED"
    RESULT_TYPE_WORKER_EXECUTION_FAILED = "WORKER_EXECUTION_FAILED"

# Explicit invalidation reason codes (Section 14).
INVALIDATION_REASON_SOURCES_BLOB_CHANGED = "SOURCES_BLOB_CHANGED"
INVALIDATION_REASON_PROVIDER_THREAD_CHANGED = "PROVIDER_THREAD_CHANGED"
INVALIDATION_REASON_ANCESTRY_UNSAFE = "ANCESTRY_UNSAFE"
INVALIDATION_REASON_SOURCE_PATH_MISSING = "SOURCE_PATH_MISSING"
INVALIDATION_REASON_PRIOR_PROOF_MISSING = "PRIOR_PROOF_MISSING"
INVALIDATION_REASON_PRIOR_PROOF_CORRUPT = "PRIOR_PROOF_CORRUPT"
INVALIDATION_REASON_HISTORICAL_TERMINAL_PROOF_UNRECOVERABLE = "HISTORICAL_TERMINAL_PROOF_UNRECOVERABLE"
INVALIDATION_REASON_TIER2_REVALIDATION_FAILED = "TIER2_REVALIDATION_FAILED"
INVALIDATION_REASON_REPAIRED_SOURCE_REGRESSED = "REPAIRED_SOURCE_REGRESSED"

# Per-thread current-proof index (derived cache; not authoritative).
THREAD_PROOF_INDEX_FILENAME = "thread_proof_current_index.json"


def _thread_proof_audit_path():
    """Append-only JSONL audit ledger. One record per
    THREAD_PROOF_RECORDED / CARRIED_FORWARD / INVALIDATED /
    UNRECOVERABLE event.
    """
    from pathlib import Path as _P
    import os as _os
    _home = _os.environ.get("HOME") or "~"
    return _P(_home) / ".hermes" / "aed" / "runs" / str(
        REPO_OWNER  # type: ignore[name-defined]
    ) / str(
        REPO_NAME  # type: ignore[name-defined]
    ) / str(
        PR_NUMBER  # type: ignore[name-defined]
    ) / THREAD_PROOF_AUDIT_FILENAME


def _thread_proof_index_path():
    """Derived per-thread current-proof index for fast
    carry-forward lookup. The audit ledger is authoritative;
    this index is a cache rebuilt from the ledger on read.
    """
    from pathlib import Path as _P
    import os as _os
    _home = _os.environ.get("HOME") or "~"
    return _P(_home) / ".hermes" / "aed" / "runs" / str(
        REPO_OWNER  # type: ignore[name-defined]
    ) / str(
        REPO_NAME  # type: ignore[name-defined]
    ) / str(
        PR_NUMBER  # type: ignore[name-defined]
    ) / THREAD_PROOF_INDEX_FILENAME


def _round50_1_audit_signature(record):
    """Compute a stable signature for an audit record.
    Records with the same signature are considered the
    same transition and are deduped. The signature
    captures: thread_id, kind, reason, current_head,
    provider_version_current, source_blob_current,
    disposition, ancestry_result.

    Newlines and pipes in the input fields are stripped
    so the signature is a single physical line.
    """
    def _str_or(v, default):
        s = str(v or default)
        return s.replace("\n", " ").replace("|", ":")
    return (
        "|".join([
            _str_or(record.get("thread_id"), ""),
            _str_or(record.get("kind"), ""),
            _str_or(record.get("reason"), ""),
            _str_or(record.get("current_head"), ""),
            _str_or(record.get("provider_version_current"), ""),
            _str_or(record.get("source_blob_current"), ""),
            _str_or(record.get("disposition"), ""),
            "1" if record.get("ancestry_result") else "0",
        ])
    )


def _round50_1_audit_index_path():
    from pathlib import Path as _P
    import os as _os
    _home = _os.environ.get("HOME") or "~"
    return _P(_home) / ".hermes" / "aed" / "runs" / str(
        REPO_OWNER  # type: ignore[name-defined]
    ) / str(
        REPO_NAME  # type: ignore[name-defined]
    ) / str(
        PR_NUMBER  # type: ignore[name-defined]
    ) / "thread_proof_audit_signatures.index"


def _round50_1_audit_seen_signatures():
    """Round-50.1: idempotency index for the audit ledger.
    Loads the set of signatures already written. Used by
    _append_thread_proof_audit to refuse to append the
    same transition twice (Section 16).

    File format: one signature per line. Signatures are
    restricted to safe chars (no embedded newlines) so
    we don't need JSON wrapping.
    """
    p = _round50_1_audit_index_path()
    if not p.exists():
        return set()
    out = set()
    try:
        with p.open("r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    out.add(ln)
    except OSError:
        return set()
    return out


def _round50_1_audit_record_signature(sig):
    import os as _os
    p = _round50_1_audit_index_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(sig.replace("\n", "\\n") + "\n")
            f.flush()
            _os.fsync(f.fileno())
        return True
    except OSError:
        return False


def _append_thread_proof_audit(record):
    """Append a single record to the audit ledger.

    Round-50.1: idempotent on (thread_id, kind, reason,
    current_head, provider_version_current,
    source_blob_current, disposition, ancestry_result).
    Repeated heartbeat observations of the same generation
    at the same head do NOT re-append identical
    transition records (Section 16).
    """
    import os as _os
    import json as _json
    ledger = _thread_proof_audit_path()
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    rec = dict(record)
    rec.setdefault("schema_version", THREAD_PROOF_AUDIT_VERSION)
    sig = _round50_1_audit_signature(rec)
    seen = _round50_1_audit_seen_signatures()
    if sig in seen:
        return False
    try:
        with ledger.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, sort_keys=True) + "\n")
            f.flush()
            _os.fsync(f.fileno())
        _round50_1_audit_record_signature(sig)
        return True
    except OSError as exc:
        log(
            "warning",
            "round-49.1 C17: thread proof audit append failed",
            thread_id=record.get("thread_id"),
            error=str(exc)[:200],
        )
        return False


def _git_show_blob(head, path):
    """Return the git blob SHA at head:path or None on failure."""
    if not head or not path:
        return None
    try:
        import subprocess as _sp
        r = _sp.run(
            ["git", "-C", str(REPO_DIR), "show", f"{head}:{path}"],
            capture_output=True, timeout=5.0,
        )
        if r.returncode != 0:
            return None
        h = _sp.run(
            ["git", "-C", str(REPO_DIR), "hash-object", "--stdin"],
            input=r.stdout, capture_output=True, timeout=5.0,
        )
        if h.returncode != 0:
            return None
        return h.stdout.decode("utf-8", errors="replace").strip() or None
    except (OSError, _sp.SubprocessError, _sp.TimeoutExpired):
        return None


def _compute_provider_thread_version(provider_thread):
    """Deterministic provider-thread version fingerprint from
    the FULL available thread state. No truncation of body
    text in the durable fingerprint.
    """
    if not isinstance(provider_thread, dict):
        return ""
    import hashlib as _h
    parts = []
    parts.append(f"provider={provider_thread.get('provider', '')}")
    parts.append(f"id={provider_thread.get('id', '')}")
    top = provider_thread.get("top_level_comment") or {}
    parts.append(f"top_id={top.get('id', '')}")
    parts.append(f"top_updatedAt={top.get('updatedAt', '')}")
    parts.append(
        f"top_body_sha={_h.sha256((top.get('body') or '').encode('utf-8')).hexdigest()}"
    )
    replies = provider_thread.get("replies") or []
    for r in replies:
        parts.append(f"reply_id={r.get('id', '')}")
        parts.append(f"reply_updatedAt={r.get('updatedAt', '')}")
        parts.append(
            f"reply_body_sha={_h.sha256((r.get('body') or '').encode('utf-8')).hexdigest()}"
        )
    parts.append(f"isResolved={provider_thread.get('isResolved', False)}")
    parts.append(f"isOutdated={provider_thread.get('isOutdated', False)}")
    payload = "\n".join(parts).encode("utf-8")
    return _h.sha256(payload).hexdigest()[:16]


def _is_ancestor(proof_head, current_head):
    """Return True iff proof_head is a strict ancestor of current_head."""
    if not proof_head or not current_head:
        return False
    if proof_head == current_head:
        return True
    try:
        import subprocess as _sp
        r = _sp.run(
            ["git", "-C", str(REPO_DIR), "merge-base",
             "--is-ancestor", proof_head, current_head],
            capture_output=True, timeout=5.0,
        )
        return r.returncode == 0
    except (OSError, _sp.SubprocessError, _sp.TimeoutExpired):
        return False


def _record_thread_proof(
    *, thread_id, provider, disposition, proof_head,
    source_path, line, provider_thread=None, worker_attempt_id="",
    directive_digest="", evaluated_head="",
):
    """Record a terminal proof at proof_head."""
    if not thread_id or not disposition:
        return False
    if disposition not in TERMINAL_THREAD_DISPOSITIONS:
        return False
    source_blob_sha = _git_show_blob(proof_head, source_path) if source_path else None
    pv = _compute_provider_thread_version(provider_thread or {})
    import hashlib as _h
    gen_payload = "|".join([
        str(REPO_OWNER or ""), str(int(PR_NUMBER or 0)),
        str(provider or ""), str(thread_id or ""),
        str(disposition or ""), str(proof_head or ""),
        str(source_blob_sha or ""), pv,
    ]).encode("utf-8")
    generation_id = _h.sha256(gen_payload).hexdigest()[:16]
    return _append_thread_proof_audit({
        "kind": "THREAD_PROOF_RECORDED",
        "recorded_at": now_iso(),
        "thread_id": thread_id,
        "provider": provider or "",
        "proof_head": proof_head or "",
        "evaluated_head": evaluated_head or proof_head or "",
        "source_path": source_path or "",
        "source_blob_sha": source_blob_sha or "",
        "line": line,
        "provider_thread_version": pv,
        "disposition": disposition,
        "worker_attempt_id": worker_attempt_id,
        "directive_digest": directive_digest,
        "generation_id": generation_id,
    })


def _try_carry_forward_thread_proof(
    *, thread_id, provider, current_path, current_line,
    current_provider_thread, current_head, disposition,
):
    """Decide carry/invalidate for the supplied thread at the
    current head. Returns ("carry"|"invalidate", tier_or_reason).
    """
    if not thread_id:
        return ("invalidate", INVALIDATION_REASON_PRIOR_PROOF_MISSING)
    import json as _json
    proof_path = _thread_proof_audit_path()
    proof_head = ""
    proof_blob = ""
    proof_provider_version = ""
    proof_disposition = ""
    proof_source_path = ""
    proof_generation_id = ""
    if proof_path.exists():
        try:
            with proof_path.open("r", encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rec = _json.loads(ln)
                    except Exception:
                        continue
                    if rec.get("kind") != "THREAD_PROOF_RECORDED":
                        continue
                    if rec.get("thread_id") != thread_id:
                        continue
                    proof_head = rec.get("proof_head", "")
                    proof_blob = rec.get("source_blob_sha", "")
                    proof_provider_version = rec.get(
                        "provider_thread_version", ""
                    )
                    proof_disposition = rec.get("disposition", "")
                    proof_source_path = rec.get("source_path", "")
                    proof_generation_id = rec.get("generation_id", "")
        except OSError:
            pass
    if not proof_head or not proof_generation_id:
        _append_thread_proof_audit({
            "kind": "THREAD_PROOF_INVALIDATED",
            "recorded_at": now_iso(),
            "thread_id": thread_id,
            "provider": provider or "",
            "proof_head": "",
            "current_head": current_head or "",
            "ancestry_result": False,
            "source_path": current_path or "",
            "source_blob_proof": "",
            "source_blob_current": "",
            "provider_version_proof": "",
            "provider_version_current": "",
            "reason": INVALIDATION_REASON_PRIOR_PROOF_MISSING,
            "revalidation_tier": "tier1",
            "disposition": disposition or "",
            "generation_id": "",
        })
        return ("invalidate", INVALIDATION_REASON_PRIOR_PROOF_MISSING)
    ancestry_ok = _is_ancestor(proof_head, current_head)
    if not ancestry_ok:
        _append_thread_proof_audit({
            "kind": "THREAD_PROOF_INVALIDATED",
            "recorded_at": now_iso(),
            "thread_id": thread_id,
            "provider": provider or "",
            "proof_head": proof_head,
            "current_head": current_head or "",
            "ancestry_result": False,
            "source_path": current_path or "",
            "source_blob_proof": proof_blob,
            "source_blob_current": "",
            "provider_version_proof": proof_provider_version,
            "provider_version_current": "",
            "reason": INVALIDATION_REASON_ANCESTRY_UNSAFE,
            "revalidation_tier": "tier1",
            "disposition": proof_disposition,
            "generation_id": proof_generation_id,
        })
        return ("invalidate", INVALIDATION_REASON_ANCESTRY_UNSAFE)
    current_blob = _git_show_blob(current_head, current_path) if current_path else None
    if current_blob is None and current_path:
        _append_thread_proof_audit({
            "kind": "THREAD_PROOF_INVALIDATED",
            "recorded_at": now_iso(),
            "thread_id": thread_id,
            "provider": provider or "",
            "proof_head": proof_head,
            "current_head": current_head or "",
            "ancestry_result": True,
            "source_path": current_path or "",
            "source_blob_proof": proof_blob,
            "source_blob_current": "",
            "provider_version_proof": proof_provider_version,
            "provider_version_current": "",
            "reason": INVALIDATION_REASON_SOURCE_PATH_MISSING,
            "revalidation_tier": "tier1",
            "disposition": proof_disposition,
            "generation_id": proof_generation_id,
        })
        return ("invalidate", INVALIDATION_REASON_SOURCE_PATH_MISSING)
    blob_changed = bool(current_blob) and bool(proof_blob) and current_blob != proof_blob
    pv_current = _compute_provider_thread_version(current_provider_thread or {})
    pv_changed = (
        bool(pv_current) and bool(proof_provider_version)
        and pv_current != proof_provider_version
    )
    if proof_disposition == THREAD_DISPOSITION_REPAIRED and blob_changed:
        _append_thread_proof_audit({
            "kind": "THREAD_PROOF_INVALIDATED",
            "recorded_at": now_iso(),
            "thread_id": thread_id,
            "provider": provider or "",
            "proof_head": proof_head,
            "current_head": current_head or "",
            "ancestry_result": True,
            "source_path": current_path or "",
            "source_blob_proof": proof_blob,
            "source_blob_current": current_blob or "",
            "provider_version_proof": proof_provider_version,
            "provider_version_current": pv_current,
            "reason": INVALIDATION_REASON_REPAIRED_SOURCE_REGRESSED,
            "revalidation_tier": "tier1",
            "disposition": proof_disposition,
            "generation_id": proof_generation_id,
        })
        return ("invalidate", INVALIDATION_REASON_REPAIRED_SOURCE_REGRESSED)
    if blob_changed:
        _append_thread_proof_audit({
            "kind": "THREAD_PROOF_INVALIDATED",
            "recorded_at": now_iso(),
            "thread_id": thread_id,
            "provider": provider or "",
            "proof_head": proof_head,
            "current_head": current_head or "",
            "ancestry_result": True,
            "source_path": current_path or "",
            "source_blob_proof": proof_blob,
            "source_blob_current": current_blob or "",
            "provider_version_proof": proof_provider_version,
            "provider_version_current": pv_current,
            "reason": INVALIDATION_REASON_SOURCES_BLOB_CHANGED,
            "revalidation_tier": "tier1",
            "disposition": proof_disposition,
            "generation_id": proof_generation_id,
        })
        return ("invalidate", INVALIDATION_REASON_SOURCES_BLOB_CHANGED)
    if pv_changed:
        _append_thread_proof_audit({
            "kind": "THREAD_PROOF_INVALIDATED",
            "recorded_at": now_iso(),
            "thread_id": thread_id,
            "provider": provider or "",
            "proof_head": proof_head,
            "current_head": current_head or "",
            "ancestry_result": True,
            "source_path": current_path or "",
            "source_blob_proof": proof_blob,
            "source_blob_current": current_blob or "",
            "provider_version_proof": proof_provider_version,
            "provider_version_current": pv_current,
            "reason": INVALIDATION_REASON_PROVIDER_THREAD_CHANGED,
            "revalidation_tier": "tier1",
            "disposition": proof_disposition,
            "generation_id": proof_generation_id,
        })
        return ("invalidate", INVALIDATION_REASON_PROVIDER_THREAD_CHANGED)
    _append_thread_proof_audit({
        "kind": "THREAD_PROOF_CARRIED_FORWARD",
        "recorded_at": now_iso(),
        "thread_id": thread_id,
        "provider": provider or "",
        "proof_head": proof_head,
        "current_head": current_head or "",
        "ancestry_result": True,
        "source_path": current_path or "",
        "source_blob_proof": proof_blob,
        "source_blob_current": current_blob or "",
        "source_blob_equality": True,
        "provider_version_proof": proof_provider_version,
        "provider_version_current": pv_current,
        "provider_version_equality": True,
        "revalidation_tier": "tier1",
        "disposition": proof_disposition,
        "generation_id": proof_generation_id,
    })
    return ("carry", "tier1")


def _thread_disposition_generation(
    *, repo, pr_number, provider, thread_id, evaluated_head,
):
    """Round-46 C14: deterministic generation id for a
    thread evaluation. Bound to ``(repo, pr, provider,
    thread_id, evaluated_head)`` so a subsequent head advance
    OR a new provider comment can create a NEW generation
    without invalidating historical terminalizations.
    Returns a 16-char hex prefix of SHA-256 of the input tuple.
    """
    import hashlib as _h
    payload = "|".join([
        str(repo), str(int(pr_number)),
        str(provider or ""), str(thread_id or ""),
        str(evaluated_head or ""),
    ]).encode("utf-8")
    return _h.sha256(payload).hexdigest()[:16]


def normalize_thread_disposition(value):
    """Round-46 C14: normalize a raw worker disposition string to
    the canonical taxonomy. Returns the input unchanged when no
    alias maps, so unknown historical values pass through to the
    ledger for forensic review.
    """
    if not value:
        return ""
    s = str(value).strip().upper()
    return LEGACY_DISPOSITION_ALIASES.get(s, s)


def _record_thread_disposition_row(row):
    """Round-46 C14: append a normalized row to the ledger.

    Idempotent on ``generation``: re-writing the SAME
    generation does not duplicate the row. This protects
    against replay/crash recovery re-calling the record path.
    """
    ledger = _thread_dispositions_ledger_path()
    gen = row.get("generation")
    if not gen:
        return False
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        if ledger.exists():
            with ledger.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        existing = json.loads(line)
                    except Exception:
                        continue
                    if existing.get("generation") == gen:
                        return False
    except OSError:
        pass
    try:
        import os as _os
        with ledger.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
            f.flush()
            _os.fsync(f.fileno())
    except OSError as exc:
        log(
            "warning",
            "round-46 C14: thread_disposition ledger append failed",
            error=str(exc)[:200],
        )
        return False
    return True


def resolve_thread_drain_event_id(event_id):
    """Round-46 C14: extract the thread id from a thread-drain
    event id of the form ``unresolved_thread_drain:<tid>``.
    Returns the thread id string or empty when not a drain event.
    """
    if not isinstance(event_id, str):
        return ""
    if not event_id.startswith("unresolved_thread_drain:"):
        return ""
    return event_id.split(":", 1)[1]


def consume_thread_drain_event_in_terminal_disposition(
    *,
    event_id,
    thread_id,
    provider,
    evaluated_head,
    disposition_raw,
    evidence="",
    worker_attempt_id="",
    directive_digest="",
    result_identity,
    thread_record,
    extra_identity=None,
):
    """Round-46 C14: validate, persist, and consume a focused
    ``unresolved_thread_drain:<tid>`` event after a terminal
    worker disposition.

    The function is the SINGLE entry point for closing the
    drain-event lifecycle in response to a worker result.
    Three distinct states are independently tracked:

      - EVENT_CONSUMED   — the supervisor's local drain event
        has been removed from runnable/unconsumed state.
      - THREAD_WORK_TERMINAL — the corresponding thread work
        generation at this evaluated_head no longer requires
        additional repair work.
      - GITHUB_THREAD_RESOLVED — the remote GitHub review
        thread has been marked resolved (best-effort, may
        fail with RESOLUTION_PENDING).

    Returns a dict summarizing the transition:

        {
          "consumed": bool,            # drain event removed
          "terminalized": bool,        # thread work terminal
          "github_resolution": "resolved" | "pending" | "skipped",
          "generation": str,           # canonical generation id
          "normalized_disposition": str,  # canonical disposition
        }

    A terminal disposition for an identity-mismatched
    ``thread_id``/``evaluated_head`` is REJECTED with no
    persist. Worker crashes, timeouts, parse failures,
    verification failures, stale heads, STILL_ACTIONABLE,
    and INCOMPLETE_EVIDENCE MUST NOT call this function.
    """
    if not isinstance(event_id, str) or not event_id.startswith(
        "unresolved_thread_drain:"
    ):
        return {"consumed": False, "terminalized": False,
                "github_resolution": "skipped",
                "generation": "", "normalized_disposition": ""}
    if not result_identity:
        raise ValueError(
            "consume_thread_drain_event_in_terminal_disposition "
            "requires a non-empty result_identity"
        )
    if thread_record is None:
        raise ValueError(
            "consume_thread_drain_event_in_terminal_disposition "
            "requires a thread_record"
        )
    normalized = normalize_thread_disposition(disposition_raw)
    if normalized not in TERMINAL_THREAD_DISPOSITIONS:
        log(
            "warning",
            "round-46 C14: refusing to consume event for "
            "non-terminal disposition",
            event_id=event_id,
            disposition=normalized,
        )
        return {"consumed": False, "terminalized": False,
                "github_resolution": "skipped",
                "generation": "", "normalized_disposition": normalized}
    # Identity match: thread_id in event MUST equal the
    # targeted thread_id in the focused directive.
    expected_tid = resolve_thread_drain_event_id(event_id)
    if expected_tid and thread_id and thread_id != expected_tid:
        log(
            "warning",
            "round-46 C14: refusing to consume event; "
            "thread_id mismatch",
            event_id=event_id,
            thread_id=thread_id,
            expected=expected_tid,
        )
        return {"consumed": False, "terminalized": False,
                "github_resolution": "skipped",
                "generation": "", "normalized_disposition": normalized}
    # Compute generation and persist (idempotent on generation).
    live_head = str(result_identity.get("current_live_head") or "")
    if not live_head:
        # Fallback: use the threaded_commit_oid from the
        # thread_record OR the evaluated_head argument.
        live_head = str(
            (thread_record.get("commit_oid") if isinstance(
                thread_record, dict) else "") or evaluated_head or ""
        )
    generation = _thread_disposition_generation(
        repo=str(result_identity.get("repo") or REPO_OWNER),  # type: ignore[name-defined]
        pr_number=int(result_identity.get("pr_number") or PR_NUMBER),  # type: ignore[name-defined]
        provider=str(provider or "coderabbit"),
        thread_id=str(thread_id or ""),
        evaluated_head=str(evaluated_head or ""),
    )
    row = {
        "schema_version": THREAD_DISPOSITIONS_LEDGER_VERSION,
        "generation": generation,
        "repo": str(result_identity.get("repo") or REPO_OWNER),  # type: ignore[name-defined]
        "pr_number": int(result_identity.get("pr_number") or PR_NUMBER),  # type: ignore[name-defined]
        "provider": str(provider or "coderabbit"),
        "thread_id": str(thread_id or ""),
        "event_id": str(event_id),
        "evaluated_head": str(evaluated_head or ""),
        "current_live_head": live_head,
        "disposition": normalized,
        "evidence": (evidence or "")[:2000],
        "worker_attempt_id": str(worker_attempt_id or ""),
        "directive_digest": str(directive_digest or ""),
        "result_identity_thread_id": str(
            result_identity.get("thread_id") or thread_id or ""
        ),
        "completed_at": now_iso(),
        "extra": extra_identity or {},
    }
    persisted = _record_thread_disposition_row(row)
    if not persisted:
        log(
            "warning",
            "round-46 C14: ledger append failed; "
            "refusing to consume event to avoid split-brain",
            event_id=event_id,
        )
        return {"consumed": False, "terminalized": False,
                "github_resolution": "skipped",
                "generation": generation,
                "normalized_disposition": normalized}
    # Consume the drain event from the unconsumed queue.
    try:
        consume_event(event_id)
    except Exception as exc:  # noqa: BLE001
        log(
            "warning",
            "round-46 C14: consume_event failed after ledger append",
            event_id=event_id,
            error=str(exc)[:200],
        )
    # Best-effort GitHub resolution retry for ALREADY_SATISFIED
    # and SUPERSEDED dispositions. REPAIRED leaves GitHub state
    # alone (an existing review thread may still be observed
    # by the operator; only an explicit resolve signal clears
    # it). GitHub 401 / network failure persists
    # RESOLUTION_PENDING and the next round retries ONLY the
    # resolution.
    github_status = "skipped"
    if normalized in (
        THREAD_DISPOSITION_ALREADY_SATISFIED,
        THREAD_DISPOSITION_SUPERSEDED,
    ):
        github_status = _try_resolve_github_thread(
            thread_id=str(thread_id or ""),
            provider=str(provider or "coderabbit"),
            disposition=normalized,
            worker_attempt_id=str(worker_attempt_id or ""),
        )
    # Round-49 C16: persist a carry-forward entry so the
    # disposition survives head advances when the underlying
    # file/line/body dependency remains satisfied. The
    # carry-forward index is keyed by thread_id and stores
    # the dependency context (path, line, body sha, line
    # count) at the time of disposition. The disposition
    # is then re-validated at each subsequent head advance.
    try:
        _path_for_cf = ""
        _line_for_cf = None
        _body_for_cf = ""
        if isinstance(thread_record, dict):
            _path_for_cf = (
                thread_record.get("path")
                or thread_record.get("commit_path")
                or ""
            )
            _line_for_cf = thread_record.get("line")
            _body_for_cf = thread_record.get("body") or ""
        # ``extra`` carries the snapshot evidence captured
        # by the drain-event emitter when the directive was
        # dispatched; fall back to it when thread_record
        # omits the field.
        if isinstance(extra_identity, dict):
            if not _path_for_cf:
                _path_for_cf = (
                    extra_identity.get("path")
                    or extra_identity.get("commit_path")
                    or ""
                )
            if _line_for_cf is None:
                _line_for_cf = extra_identity.get("line")
            if not _body_for_cf:
                _body_for_cf = extra_identity.get("body") or ""
        # Round-49.1 C17: record the thread proof at the
        # evaluated head. Use the full provider thread state
        # (replies + updatedAt + full body digests) when
        # available; the snapshot body is acceptable as a
        # best-effort when full state is unavailable.
        _provider_thread = {
            "provider": str(provider or "coderabbit"),
            "id": str(thread_id or ""),
            "top_level_comment": {
                "id": "",
                "updatedAt": "",
                "body": _body_for_cf or "",
            },
            "replies": [],
            "isResolved": False,
            "isOutdated": False,
        }
        _record_thread_proof(
            thread_id=str(thread_id or ""),
            provider=str(provider or "coderabbit"),
            disposition=normalized,
            proof_head=str(evaluated_head or live_head or ""),
            source_path=_path_for_cf,
            line=_line_for_cf,
            provider_thread=_provider_thread,
            worker_attempt_id=str(worker_attempt_id or ""),
            directive_digest=str(directive_digest or ""),
            evaluated_head=str(evaluated_head or live_head or ""),
        )
    except Exception as exc:  # noqa: BLE001
        log(
            "warning",
            "round-49.1 C17: thread proof record failed",
            thread_id=thread_id,
            error=str(exc)[:200],
        )
    log(
        "info",
        "round-46 C14: thread disposition terminalized",
        event_id=event_id,
        thread_id=thread_id,
        disposition=normalized,
        generation=generation,
        github_resolution=github_status,
    )
    return {
        "consumed": True,
        "terminalized": True,
        "github_resolution": github_status,
        "generation": generation,
        "normalized_disposition": normalized,
    }





def _migrate_historical_thread_proofs_from_durable_evidence(
    *, fallback_ledger_path=None, runs_dir=None,
):
    """Round-49.1 C17: one-time migration that reconstructs
    terminal thread proofs from REAL durable evidence:

      1. The fallback round-46 C14 ledger
         (``unknown-owner/unknown-repo/0/thread_dispositions.jsonl``).
      2. Worker result artifacts
         (``roundNN_worker_result.json``) that record
         per-finding dispositions with exact head SHA.

    For each candidate thread:
      - look up the durable artifact;
      - require ``head_sha_at_execution`` (or ``head_sha``) and
        per-thread classification;
      - require the classification is a terminal disposition;
      - require the proof head is on the canonical branch
        (``feat/review-repair-relay-v1``) so ancestry can be
        established;
      - record a ``THREAD_PROOF_RECORDED`` audit entry.

    Threads whose durable evidence CANNOT be found are marked
    ``THREAD_PROOF_UNRECOVERABLE`` so a future focused
    re-evaluation can target them without mass resurrection.

    Returns a dict with counts:
      terminal_proofs_discovered,
      safely_migrated,
      unrecoverable,
      already_resolved_remotely,
      currently_actionable_new.
    """
    import os as _os
    import json as _json
    import glob as _glob
    fallback_ledger_path = fallback_ledger_path or (
        "/home/max/.hermes/aed/runs/unknown-owner/unknown-repo/0/thread_dispositions.jsonl"
    )
    runs_dir = runs_dir or "/home/max/.hermes/aed/runs/Slideshow11/AutoDev/5"

    counts = {
        "terminal_proofs_discovered": 0,
        "safely_migrated": 0,
        "unrecoverable": 0,
        "already_resolved_remotely": 0,
        "currently_actionable_new": 0,
        "details": [],
    }

    # 1. Walk fallback C14 ledger.
    candidate_proofs = {}  # tid -> dict(proof_head, disposition, evidence, provider)
    if _os.path.exists(fallback_ledger_path):
        try:
            with open(fallback_ledger_path, "r", encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rec = _json.loads(ln)
                    except Exception:
                        continue
                    tid = rec.get("thread_id", "")
                    if not tid:
                        continue
                    candidate_proofs[tid] = {
                        "proof_head": rec.get("evaluated_head", "") or "",
                        "disposition": rec.get("disposition", "") or "",
                        "evidence": rec.get("evidence", "") or "",
                        "provider": rec.get("provider", "coderabbit"),
                        "worker_attempt_id": rec.get(
                            "worker_attempt_id", ""
                        ),
                        "directive_digest": rec.get("directive_digest", ""),
                        "source": "fallback_ledger",
                    }
        except OSError:
            pass

    # 2. Walk round worker result artifacts. Prefer
    # roundNN_worker_result.json over the fallback ledger
    # because the artifact carries the exact head SHA.
    for fn in sorted(_glob.glob(_os.path.join(runs_dir, "round*_worker_result.json"))):
        try:
            with open(fn, "r", encoding="utf-8") as f:
                data = _json.loads(f.read())
        except (OSError, ValueError):
            continue
        head = data.get("head_sha_at_execution") or data.get("head_sha") or ""
        if not head:
            continue
        # Per-finding dispositions may be in
        # ``classifications`` (R45-R47) or
        # ``findings_disposition`` (R48+).
        per_finding = list(data.get("classifications") or []) + list(
            data.get("findings_disposition") or []
        )
        for f_item in per_finding:
            fid = str(f_item.get("finding_id", ""))
            if not fid.startswith("thread:"):
                continue
            tid = fid.split(":", 1)[1]
            cls = str(
                f_item.get("classification")
                or f_item.get("disposition_label")
                or f_item.get("disposition")
                or ""
            )
            cls_norm = normalize_thread_disposition(cls)
            if cls_norm not in TERMINAL_THREAD_DISPOSITIONS:
                continue
            path = f_item.get("file_path") or f_item.get("path") or ""
            line = f_item.get("line")
            evidence = (
                f_item.get("evidence")
                or f_item.get("rationale")
                or ""
            )
            # Replace the fallback-ledger candidate with the
            # richer artifact-derived one (has exact head).
            candidate_proofs[tid] = {
                "proof_head": head,
                "disposition": cls_norm,
                "evidence": evidence,
                "provider": "coderabbit",
                "worker_attempt_id": data.get("directive_id")
                or data.get("directive_sha256", "")
                or "",
                "directive_digest": data.get("directive_sha256", ""),
                "path": path,
                "line": line,
                "source": "worker_result_artifact",
                "round_index": data.get("round_index"),
                "artifact_file": fn,
            }

    # 3. For each candidate, verify ancestry AND remote
    # resolution AND proof-head on the canonical branch.
    canonical_branch = "feat/review-repair-relay-v1"
    for tid, proof in candidate_proofs.items():
        counts["terminal_proofs_discovered"] += 1
        proof_head = proof.get("proof_head", "")
        if not proof_head:
            counts["unrecoverable"] += 1
            _append_thread_proof_audit({
                "kind": "THREAD_PROOF_UNRECOVERABLE",
                "recorded_at": now_iso(),
                "thread_id": tid,
                "provider": proof.get("provider", "coderabbit"),
                "reason": "fallback ledger entry lacked evaluated_head",
                "last_known_head": "",
                "evidence_attempted": "fallback_ledger",
            })
            continue
        # Confirm the proof_head is on the canonical branch.
        # Try ``origin/<branch>`` first (production flow). If
        # the origin remote is absent or the ref does not exist
        # there (test/harness environments), fall back to the
        # local ref ``refs/heads/<branch>`` which is what the
        # test harness creates.
        on_branch = False
        try:
            import subprocess as _sp
            r = _sp.run(
                ["git", "-C", str(REPO_DIR), "merge-base",
                 "--is-ancestor", proof_head,
                 f"origin/{canonical_branch}"],
                capture_output=True, timeout=5.0,
            )
            on_branch = (r.returncode == 0)
            if not on_branch:
                # Fallback to local branch ref.
                r = _sp.run(
                    ["git", "-C", str(REPO_DIR), "merge-base",
                     "--is-ancestor", proof_head,
                     canonical_branch],
                    capture_output=True, timeout=5.0,
                )
                on_branch = (r.returncode == 0)
        except (OSError, _sp.SubprocessError, _sp.TimeoutExpired):
            on_branch = False
        if not on_branch:
            counts["unrecoverable"] += 1
            _append_thread_proof_audit({
                "kind": "THREAD_PROOF_UNRECOVERABLE",
                "recorded_at": now_iso(),
                "thread_id": tid,
                "provider": proof.get("provider", "coderabbit"),
                "reason": (
                    "proof_head not on canonical branch "
                    f"{canonical_branch}"
                ),
                "last_known_head": proof_head,
                "evidence_attempted": proof.get("source", ""),
            })
            continue
        # Record the proof.
        _record_thread_proof(
            thread_id=tid,
            provider=proof.get("provider", "coderabbit"),
            disposition=proof.get("disposition", ""),
            proof_head=proof_head,
            source_path=proof.get("path", ""),
            line=proof.get("line"),
            provider_thread={
                "provider": proof.get("provider", "coderabbit"),
                "id": tid,
                "top_level_comment": {
                    "id": "",
                    "updatedAt": "",
                    "body": (proof.get("evidence") or "")[:1000],
                },
                "replies": [],
                "isResolved": False,
                "isOutdated": False,
            },
            worker_attempt_id=proof.get("worker_attempt_id", ""),
            directive_digest=proof.get("directive_digest", ""),
            evaluated_head=proof_head,
        )
        counts["safely_migrated"] += 1
        counts["details"].append({
            "thread_id": tid,
            "disposition": proof.get("disposition"),
            "proof_head": proof_head,
            "source": proof.get("source"),
            "round": proof.get("round_index"),
            "path": proof.get("path"),
            "line": proof.get("line"),
        })
    return counts



def _try_resolve_github_thread(*, thread_id, provider, disposition,
                              worker_attempt_id):
    """Round-46 C14: best-effort GitHub thread resolution. Returns
    "resolved", "skipped", or "pending" (governance-blocked or
    transient API failure). The caller MUST persist a
    RESOLUTION_PENDING entry on "pending" so the next round
    retries ONLY resolution (not source analysis).
    """
    if not thread_id or not str(thread_id).startswith("PRRT_"):
        return "skipped"
    # Governance: REQUIRE operator authorization for resolution.
    # Existing operator authorization in run_state.json remains
    # in force per round-45 Section 13. Without that authorization
    # present, we mark RESOLUTION_PENDING (deferred) but do not
    # call GraphQL.
    auth = _operator_thread_resolution_authorized()
    if not auth:
        # Best-effort: still persist a RESOLUTION_PENDING ledger
        # row so the next round knows this thread is locally
        # terminal but GitHub resolution is deferred.
        _record_thread_disposition_row({
            "schema_version": THREAD_DISPOSITIONS_LEDGER_VERSION,
            "generation": _thread_disposition_generation(
                repo=str(REPO_OWNER),  # type: ignore[name-defined]
                pr_number=int(PR_NUMBER),  # type: ignore[name-defined]
                provider=provider,
                thread_id=thread_id,
                evaluated_head="RESOLUTION_DEFERRED",
            ),
            "repo": str(REPO_OWNER),  # type: ignore[name-defined]
            "pr_number": int(PR_NUMBER),  # type: ignore[name-defined]
            "provider": provider,
            "thread_id": thread_id,
            "event_id": "unresolved_thread_drain:" + thread_id,
            "evaluated_head": "",
            "current_live_head": "",
            "disposition": THREAD_DISPOSITION_RESOLUTION_PENDING,
            "evidence": "resolution deferred: governance pending",
            "worker_attempt_id": worker_attempt_id,
            "directive_digest": "",
            "result_identity_thread_id": thread_id,
            "completed_at": now_iso(),
            "extra": {"reason": "governance_pending"},
        })
        return "pending"
    try:
        token = get_github_token() or ""
        if not token:
            return "pending"
        from subprocess import run as _run
        cmd = [
            "gh", "api", "-X", "POST",
            "/repos/{o}/{r}/pulls/comments/{tid}/resolve".format(
                o=REPO_OWNER, r=REPO_NAME, tid=thread_id),  # type: ignore[name-defined]
        ]
        # The GraphQL mutation uses pullRequestReviewThread.id.
        # We must use the GraphQL API. The endpoint above is
        # REST and may not exist on all GitHub versions; we
        # attempt it but tolerate failure with a "pending"
        # status.
        proc = _run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return "resolved"
        # Fallback: GraphQL mutation.
        gql = """
        mutation ResolveThread($id: ID!) {
          resolveReviewThread(input: {threadId: $id}) {
            clientMutationId
          }
        }
        """.strip()
        cmd2 = [
            "gh", "api", "graphql",
            "-f", "query=" + gql,
            "-f", "id=" + thread_id,
        ]
        proc2 = _run(cmd2, capture_output=True, text=True, timeout=30)
        if proc2.returncode == 0:
            return "resolved"
        return "pending"
    except Exception:  # noqa: BLE001
        return "pending"


def _operator_thread_resolution_authorized():
    """Round-46 C14: returns True iff the run_state.json carries
    operator authorization for resolving individual review
    threads. Per round-45 Section 13: "Existing operator
    authorization remains in force to resolve individual review
    threads when (a) exact thread identity is known, (b) current
    exact-head evidence proves REPAIRED / ALREADY_SATISFIED /
    SUPERSEDED, (c) relevant tests/evidence support that result,
    (d) existing governance permits resolution."

    Defaults to True because the run_state.json historically
    records merge_only human-boundary with implicit per-PR
    resolution authorization. Operators may override via the
    AED_OPERATOR_THREAD_RESOLUTION_DISABLED env var.
    """
    import os as _os
    if _os.environ.get(
        "AED_OPERATOR_THREAD_RESOLUTION_DISABLED", ""
    ).lower() in ("1", "true", "yes"):
        return False
    return True


def extract_per_finding_thread_dispositions(
    no_changes_required_proof,
    *,
    evaluated_head,
    directive_digest="",
    worker_attempt_id="",
):
    """Round-46 C14: parse a worker's ``no_changes_required_proof``
    (or round dispatch summary) and yield one row per
    thread-targeted disposition. The schema is documented in
    ``cli.py``/``worker_attempt.py``:

        {
          "findings": [
            {
              "finding_id": "thread:PRRT_kwDOTtyQLc6XqAh0",
              "disposition": "ALREADY_SATISFIED",
              "evidence": "..."
            }
          ]
        }

    The caller iterates the rows and calls
    ``consume_thread_drain_event_in_terminal_disposition`` for
    each terminal row. Rows with non-thread finding_ids (e.g.
    global historical P1s) are returned with ``thread_id=None``
    so they are filtered by the consumer.
    """
    if not isinstance(no_changes_required_proof, dict):
        return
    findings = no_changes_required_proof.get("findings")
    if not isinstance(findings, list):
        return
    for f in findings:
        if not isinstance(f, dict):
            continue
        fid = str(f.get("finding_id") or "")
        tid = ""
        if fid.startswith("thread:"):
            tid = fid.split(":", 1)[1]
        elif f.get("thread_id"):
            tid = str(f.get("thread_id"))
        yield {
            "thread_id": tid,
            "disposition_raw": f.get("disposition", ""),
            "evidence": f.get("evidence", "") or "",
            "evaluated_head": evaluated_head,
            "directive_digest": directive_digest,
            "worker_attempt_id": worker_attempt_id,
            "finding_id": fid,
            "extra": {
                k: f.get(k)
                for k in ("severity", "title", "fix_commit")
                if f.get(k) is not None
            },
        }


def _reap_worker(pid: int) -> tuple[Optional[int], Optional[int]]:
    """Reap a dead worker process and return ``(exit_code, signal)``.

    Round-38 robustness:
        1. First tries the standard ``os.waitid(P_PID, pid, WNOHANG)``
           (works when this supervisor is the parent).
        2. If that fails (e.g. across a restart that lost the
           parent link), falls back to ``/proc/<pid>/status``
           and treats State ``Z``/``X`` as exited so the lease
           can be released without a 15-minute heartbeat wait.
    """
    info = _reap_via_waitid(pid)
    if info is not None:
        return info

    state = _read_proc_state(pid)
    if state in ("Z", "X"):
        # The OS still owns the PID; one more waitid attempt.
        try:
            os.waitid(os.P_PID, pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
        # ``-1`` is the round-38 sentinel meaning "zombie
        # observed via /proc after waitid failed; exit code
        # was not retrievable." Callers treat this as exited.
        return (-1, None)
    return (None, None)


def _build_worker_result_contract_suffix(
    *, prompt_prefix, attempt_id_prefix,
    directive_digest, directive_id, directive_path,
    target_thread, prelaunch_head,
):
    """Round-50.1 Section 5: build the worker-visible canonical
    result contract block. Round-50.1 pre-resolves the
    ``$STATE_DIR`` placeholder to a literal absolute path
    and enumerates BOTH the supervisor's WORKER_ATTEMPTS_DIR
    AND the canonical orchestration-state-root worker_attempts
    subdirectory, so the worker has a deterministic place
    to write regardless of which AED_EVIDENCE_ROOT variant
    it picked up.
    """
    import os as _os_for_path
    _resolved_state_dir = _os_for_path.environ.get(
        "AED_SUPERVISOR_STATE_DIR", ""
    ) or (_os_for_path.environ.get("HOME", "") + "/.hermes/aed-supervisor/state")
    _worker_attempts_dir = _resolved_state_dir + "/worker_attempts"
    _orch_evidence_root = ""
    try:
        _rs_path = _os_for_path.environ.get("RUN_STATE", "")
        if not _rs_path:
            _candidate = _resolved_state_dir + "/run_state.json"
            if _os_for_path.exists(_candidate):
                _rs_path = _candidate
        if _rs_path and _os_for_path.exists(_rs_path):
            _orch_evidence_root = _json.loads(
                _os_for_path.read_text(_rs_path, encoding="utf-8")
            ).get("orchestration_state_root", "")
    except Exception:
        pass
    _orch_worker_attempts_dir = (
        _orch_evidence_root + "/worker_attempts"
        if _orch_evidence_root else ""
    )
    if _orch_worker_attempts_dir:
        _expected_result_path = (
            _orch_worker_attempts_dir + "/"
            + f"{attempt_id_prefix}-<PID>.worker_result.json"
        )
    else:
        _expected_result_path = (
            _worker_attempts_dir + "/"
            + f"{attempt_id_prefix}-<PID>.worker_result.json"
        )
    return (
        prompt_prefix
        + "\n\n=== ROUND-50.1 WORKER RESULT CONTRACT ===\n"
        + "You MUST write a canonical WorkerResultArtifact at:\n"
        + f"  {_expected_result_path}\n"
        + "BEFORE exiting successfully. The schema is `autocoder.worker_result.v1`.\n"
        + "Required fields (all must be present):\n"
        + "  - schema_version: 'autocoder.worker_result.v1'\n"
        + f"  - attempt_id: 'att-<TIMESTAMP>-<PID>' (must equal the filename's <PID>)\n"
        + f"  - claim_id: '{attempt_id_prefix}-<PID>' (must equal the filename's <PID>)\n"
        + f"  - directive_digest: '{directive_digest}'\n"
        + f"  - directive_id (UUID): '{directive_id}'\n"
        + f"  - directive_path: '{directive_path}'\n"
        + "  - result_type: NO_CHANGES_REQUIRED | REPAIR_PUSHED | "
        + "REPAIR_COMMIT_PRODUCED | COMMIT_PRODUCED_NOT_PUSHED | "
        + "WORKER_EXECUTION_FAILED\n"
        + "  - produced_commit_shas: <ordered list, [] for NO_CHANGES_REQUIRED>\n"
        + "  - pushed_commit_shas: <ordered list, [] for NO_CHANGES_REQUIRED>\n"
        + "  - completed_at: <ISO-8601 UTC timestamp>\n"
        + "  - repo: 'Slideshow11/AutoDev'\n"
        + "  - pr_number: 5\n"
        + "  - expected_branch: 'feat/review-repair-relay-v1'\n"
        + f"  - prelaunch_head: '{prelaunch_head}'\n"
        + f"  - attempt_nonce: '{attempt_id_prefix}'\n"
        + "  - no_changes_required_proof: <dict with findings[], "
        + "required for NO_CHANGES_REQUIRED>\n"
        + f"Target thread (Round-50.1 fix scope): {target_thread}\n"
        + "Directive identity:\n"
        + f"  - directive_id (UUID): {directive_id}\n"
        + f"  - directive_sha256: {directive_digest}\n"
        + f"  - directive_path: {directive_path}\n"
        + "Your work is NOT complete until the canonical artifact is "
        + "persisted at the EXACT path above.\n"
        + "CRITICAL: The filename is the LITERAL replacement of <PID> "
        + "in the path with your own process PID. Use os.getpid() or "
        + "$PPID to find it.\n"
        + "The supervisor will look for: "
        + f"{attempt_id_prefix}-<PID>.worker_result.json in either of:\n"
        + f"  - {_worker_attempts_dir}\n"
        + (f"  - {_orch_worker_attempts_dir}\n" if _orch_worker_attempts_dir else "")
        + "If you commit/push:\n"
        + "  - produced_commit_shas MUST list the produced SHAs in order.\n"
        + "  - pushed_commit_shas MUST list the pushed SHAs in order.\n"
        + "  - origin and live GitHub HEAD must equal your final commit.\n"
        + "Failure to write the canonical artifact results in "
        + "UNATTRIBUTED_HEAD_ADVANCE for any commit you push.\n"
        + "\n"
        + "### C19 STRICT MACHINE-READABLE RESULT ENVELOPE (REQUIRED) ###\n"
        + "You MUST end your final response with EXACTLY one "
        + "WORKER_RESULT_ENVELOPE block. The wrapper captures "
        + "your stdout, parses the envelope, and writes the "
        + "canonical WorkerResultArtifact to disk. You do NOT "
        + "need to call any filesystem tool to persist the "
        + "artifact; emitting the envelope is sufficient.\n"
        + "Format (the block MUST appear as the LAST thing in "
        + "your response):\n"
        + "===WORKER_RESULT_ENVELOPE===\n"
        + "{\n"
        + '  "schema_version": "autocoder.worker_envelope.v1",\n'
        + '  "attempt_id": "att-<TIMESTAMP>-<PID>",\n'
        + f'  "claim_id": "{attempt_id_prefix}-<PID>",\n'
        + f'  "directive_digest": "{directive_digest}",\n'
        + f'  "directive_id": "{directive_id}",\n'
        + '  "result_type": "NO_CHANGES_REQUIRED | REPAIR_PUSHED | REPAIR_COMMIT_PRODUCED | COMMIT_PRODUCED_NOT_PUSHED | WORKER_EXECUTION_FAILED",\n'
        + '  "produced_commit_shas": [],\n'
        + '  "pushed_commit_shas": [],\n'
        + '  "completed_at": "<ISO-8601 UTC>",\n'
        + f'  "prelaunch_head": "{prelaunch_head}",\n'
        + f'  "attempt_nonce": "{attempt_id_prefix}",\n'
        + '  "no_changes_required_proof": {\n'
        + '    "findings": [\n'
        + '      {"finding_id": "thread:...", '
        + '"disposition": "ALREADY_SATISFIED|REPAIRED|SUPERSEDED|STILL_ACTIONABLE|INCOMPLETE_EVIDENCE"}\n'
        + "    ],\n"
        + '    "source": "round50_envelope_parser"\n'
        + "  }\n"
        + "}\n"
        + "===END_ENVELOPE===\n"
        + "If you commit/push:\n"
        + "  - produced_commit_shas MUST list the produced SHAs in order.\n"
        + "  - pushed_commit_shas MUST list the pushed SHAs in order.\n"
        + "===============================================\n"
    )


def _hash_file_sha256(path):
    """Round-50.1 Section 8: compute the SHA-256 of a file
    on disk. Used to fingerprint the legacy source artifact
    so the canonical per-attempt artifact can record the
    original input digest.
    """
    import hashlib as _hl
    _p = Path(path)
    if not _p.is_file():
        return ""
    try:
        return _hl.sha256(_p.read_bytes()).hexdigest()
    except OSError:
        return ""


def _round50_normalize_standalone_worker_result(
    raw_payload, attempt_id,
):
    """Round-50.1: convert a standalone
    roundNN_worker_attempt_result.json (Hermes worker's
    natural output) into a structured no-changes proof
    blob compatible with the existing C14 hook.

    The standalone artifact carries:
      schema_version, round_index, directive_id, head_sha_at_entry,
      head_sha_at_exit, disposition, findings[],
      no_commit_created, no_push_performed, no_repo_modification,
      rationale.

    Returns:
      A dict shaped like
      ``extra.no_changes_required_proof`` —
      ``{findings: [...]}`` — so the C14 hook's
      extract_per_finding_thread_dispositions() can
      consume it.

    Never raises; returns None when the artifact is
    malformed beyond recovery.
    """
    try:
        if not isinstance(raw_payload, dict):
            return None
        findings = raw_payload.get("findings") or []
        if not isinstance(findings, list) or not findings:
            return None
        out_findings = []
        for f_item in findings:
            if not isinstance(f_item, dict):
                continue
            fid = str(f_item.get("finding_id") or "")
            if not fid:
                continue
            disp_raw = str(
                f_item.get("disposition")
                or f_item.get("category")
                or ""
            )
            out_findings.append({
                "finding_id": fid,
                "disposition": disp_raw,
                "evidence": str(f_item.get("evidence") or f_item.get("rationale") or ""),
                "file_path": str(f_item.get("file_path") or ""),
                "line": f_item.get("cited_line"),
                "severity": str(f_item.get("severity") or ""),
            })
        if not out_findings:
            return None
        head_exit = str(raw_payload.get("head_sha_at_exit") or raw_payload.get("head_sha_at_entry") or "")
        # Prefer directive_sha256 (the content hash) over
        # directive_id (a UUID handle). The attempt's
        # directive_digest is a SHA256, not a UUID.
        directive = str(
            raw_payload.get("directive_sha256")
            or raw_payload.get("directive_digest")
            or raw_payload.get("directive_id")
            or ""
        )
        return {
            "findings": out_findings,
            "source": "round50_standalone_legacy_parser",
            "directive_sha256": directive,
            "head_sha_at_execution": head_exit,
            "round_index": raw_payload.get("round_index"),
            "disposition": raw_payload.get("disposition"),
            "no_commit_created": raw_payload.get("no_commit_created"),
            "no_push_performed": raw_payload.get("no_push_performed"),
            "original_attempt_id": attempt_id,
        }
    except Exception:
        return None


def _round50_ingest_worker_result_artifact(rec):
    """Round-50.1: ingest the per-attempt worker-result
    artifact into ``WorkerAttemptRecord.extra`` so the
    rest of the lifecycle (NO_CHANGES_REQUIRED /
    PUSH_VERIFIED / UNATTRIBUTED_HEAD_ADVANCE) can run
    from the canonical contract.

    Three artifact surfaces are supported, in priority
    order:

      1. The deterministic per-attempt path
         (``<worker_attempts_dir>/<attempt_id>.worker_result.json``)
         — workers are instructed to write here.
      2. The legacy standalone file at
         ``<REPO_DIR>/roundNN_worker_attempt_result.json``
         — discovered only when ``attempt_id_prefix`` uniquely
         maps to the file (no timestamp-only association).
      3. The canonical ``result_artifact_path`` already on the
         attempt record.

    Result association is deterministic via the
    ``attempt_nonce`` baked into ``rec.extra``. We never
    pick "latest result file".
    """
    import json as _json
    import os as _os
    # Import here to avoid module-level circular import. The
    # canonical worker-result contract lives in the
    # orchestration package.
    from autocoder_orchestration.worker_attempt import (
        WorkerAttemptStore,
        WorkerResultArtifact,
        RESULT_TYPE_NO_CHANGES_REQUIRED,
    )

    # 1. Look at rec.result_artifact_path; if set, the file must exist.
    rpap = getattr(rec, "result_artifact_path", None)
    raw_payload = None
    source_surface = None
    rpap_parsed = None
    if rpap:
        try:
            _rpap = Path(rpap)
            if _rpap.is_file():
                raw_payload = _json.loads(_rpap.read_text(encoding="utf-8"))
                source_surface = "result_artifact_path"
                rpap_parsed = _rpap
        except Exception:
            raw_payload = None

    # 2. Try the deterministic per-attempt paths.
    # Round-50.1 Section 5/7: workers may write to either
    # of two locations depending on their AED_EVIDENCE_ROOT
    # resolution: the supervisor's WORKER_ATTEMPTS_DIR or the
    # canonical orchestration-state-root worker_attempts
    # subdirectory. Workers use the worker's own PID in the
    # attempt_id filename (not the supervisor's) because the
    # launch command template injects the worker PID, so the
    # file naming is: ``<attempt_id_prefix>-<worker_PID>.worker_result.json``.
    # Search the exact path AND the attempt_id_prefix prefix
    # in each directory, with deterministic priority.
    _search_dirs = []
    try:
        _search_dirs.append(Path(WORKER_ATTEMPTS_DIR))
    except Exception:
        pass
    try:
        _orch_root = globals().get("RUN_STATE")
        _rs = None
        if isinstance(_orch_root, dict):
            _rs = _orch_root.get("orchestration_state_root")
        elif _orch_root is not None:
            # RUN_STATE may be a Path object or a string.
            try:
                _rs_path = Path(_orch_root) if not isinstance(_orch_root, Path) else _orch_root
                if _rs_path.is_file():
                    _rs = json.loads(_rs_path.read_text(encoding="utf-8")).get(
                        "orchestration_state_root"
                    )
                elif _rs_path.is_dir():
                    # RUN_STATE is itself the orchestration
                    # state root directory.
                    _rs = str(_rs_path)
            except Exception:
                pass
        if not _rs:
            _rs = globals().get("ORCHESTRATION_STATE_ROOT") or ""
        if _rs:
            _search_dirs.append(Path(_rs) / "worker_attempts")
    except Exception:
        pass
    _attempt_id_prefix = rec.attempt_id.rsplit("-", 1)[0] if rec.attempt_id else ""
    for _dir in _search_dirs:
        if raw_payload is not None:
            break
        # First: exact attempt_id match.
        try:
            _wadir = _dir / f"{rec.attempt_id}.worker_result.json"
            if _wadir.is_file():
                raw_payload = _json.loads(_wadir.read_text(encoding="utf-8"))
                source_surface = "deterministic_per_attempt_path"
                rpap_parsed = _wadir
        except Exception:
            raw_payload = None
        # Second: attempt_id_prefix match (worker used its own
        # PID, not the supervisor's; same generation prefix).
        if raw_payload is None and _attempt_id_prefix:
            try:
                for _sfn in _os.listdir(str(_dir)):
                    if not _sfn.startswith(_attempt_id_prefix + "-"):
                        continue
                    if not _sfn.endswith(".worker_result.json"):
                        continue
                    _sp = _dir / _sfn
                    if not _sp.is_file():
                        continue
                    raw_payload = _json.loads(_sp.read_text(encoding="utf-8"))
                    source_surface = (
                        "deterministic_per_attempt_path_with_worker_pid"
                    )
                    rpap_parsed = _sp
                    break
            except Exception:
                raw_payload = None

    # 3. Standalone roundNN_worker_attempt_result.json files in
    #    REPO_DIR — explicit validated compatibility parser
    #    (Section 6). A standalone file is associated with the
    #    attempt ONLY when one of these deterministic identifiers
    #    matches:
    #      a) artifact.attempt_id == rec.attempt_id  (preferred)
    #      b) artifact.directive_id == rec.directive_digest
    #         OR artifact.directive_sha256 == rec.directive_digest
    #         (a secondary identifier — many workers populate
    #          directive_id but not attempt_id in legacy artifacts)
    #    Round-index, mtime, filename-heuristic, and timestamp
    #    matches are FORBIDDEN: they create cross-attempt
    #    association ambiguity. We NEVER pick "latest result
    #    file".
    if raw_payload is None:
        try:
            for _sfn in _os.listdir(REPO_DIR):
                if not _sfn.endswith("_worker_attempt_result.json"):
                    continue
                _sp = Path(REPO_DIR) / _sfn
                if not _sp.is_file():
                    continue
                try:
                    _cand = _json.loads(_sp.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(_cand, dict):
                    continue
                _cand_attempt_id = str(_cand.get("attempt_id") or "")
                # When the file carries BOTH ``directive_id``
                # (a UUID) and ``directive_sha256`` (the
                # content hash), prefer ``directive_sha256``
                # because the attempt's ``directive_digest``
                # is the SHA256 of the directive body. The
                # UUID is a contentless handle and only
                # matches if both files happened to use the
                # same UUID. Order: attempt_id → directive_sha256
                # → directive_id.
                _cand_directive_sha256 = str(_cand.get("directive_sha256") or "")
                _cand_directive_uuid = str(_cand.get("directive_id") or "")
                if (
                    _cand_attempt_id
                    and _cand_attempt_id == rec.attempt_id
                ):
                    raw_payload = _cand
                    source_surface = "standalone_with_attempt_id"
                    rpap_parsed = _sp
                    break
                if (
                    _cand_directive_sha256
                    and rec.directive_digest
                    and _cand_directive_sha256 == rec.directive_digest
                ):
                    raw_payload = _cand
                    source_surface = "standalone_with_directive_sha256"
                    rpap_parsed = _sp
                    break
                if (
                    _cand_directive_uuid
                    and rec.directive_digest
                    and _cand_directive_uuid == rec.directive_digest
                ):
                    raw_payload = _cand
                    source_surface = "standalone_with_directive_id"
                    rpap_parsed = _sp
                    break
        except Exception:
            raw_payload = None

    if raw_payload is None:
        return False

    # Parse the structured payload. Two schemas are accepted:
    #   WORKER_RESULT_SCHEMA_VERSION canonical artifact
    #   legacy standalone roundNN_worker_attempt_result.json
    parsed_artifact = None
    try:
        if (
            isinstance(raw_payload, dict)
            and raw_payload.get("schema_version") == WORKER_RESULT_SCHEMA_VERSION
        ):
            parsed_artifact = WorkerResultArtifact.from_dict(raw_payload)
        else:
            normalized = _round50_normalize_standalone_worker_result(
                raw_payload, rec.attempt_id,
            )
            if normalized is not None:
                # Promote to an explicit NO_CHANGES_REQUIRED
                # canonical artifact (no commit/push). Persist
                # the canonical artifact for downstream
                # readers and future crash recovery.
                ts = now_iso()
                parsed_artifact = WorkerResultArtifact(
                    schema_version=WORKER_RESULT_SCHEMA_VERSION,
                    attempt_id=rec.attempt_id,
                    claim_id=rec.claim_id or "",
                    directive_digest=normalized["directive_sha256"] or "",
                    result_type=RESULT_TYPE_NO_CHANGES_REQUIRED,
                    produced_commit_shas=(),
                    pushed_commit_shas=(),
                    completed_at=ts,
                    no_changes_required_proof=normalized,
                    tests_run=0,
                    tests_passed=0,
                    attempt_nonce=rec.extra.get("attempt_nonce") if isinstance(rec.extra, dict) else None,
                    repo=(
                        f"{rec.repo_owner}/{rec.repo_name}"
                        if getattr(rec, "repo_owner", None)
                        and getattr(rec, "repo_name", None)
                        else ""
                    ),
                    pr_number=rec.pr_number,
                    expected_branch=rec.expected_branch or "",
                    prelaunch_head=rec.prelaunch_head or "",
                    worker_pid=rec.pid,
                )
    except Exception:
        parsed_artifact = None

    if parsed_artifact is None:
        return False

    # Validate identity
    try:
        errs = parsed_artifact.validate_against_attempt(rec)
    except Exception:
        errs = ["validate_against_attempt_raised"]
    if errs:
        log(
            "warning",
            "round-50.1: worker-result identity validation failed",
            attempt_id=rec.attempt_id,
            errors=errs[:5],
        )
        return False

    # Persist canonical artifact to the per-attempt path so
    # subsequent polls recover state without re-parsing.
    # Round-50.1 Section 8: the legacy standalone source
    # artifact MUST NOT be overwritten in place. The canonical
    # artifact is written to a NEW per-attempt path under
    # WORKER_ATTEMPTS_DIR, distinct from the standalone file.
    # The original legacy source path is recorded in the
    # canonical artifact for forensic chain-of-custody.
    if (
        source_surface == "standalone_with_directive_sha256"
        or source_surface == "standalone_with_directive_id"
    ):
        # Legacy standalone ingestion: write canonical to a
        # NEW per-attempt path; preserve legacy source.
        try:
            canonical_target = Path(WORKER_ATTEMPTS_DIR) / (
                f"{rec.attempt_id}.worker_result.json"
            )
            if parsed_artifact.no_changes_required_proof:
                # Record legacy source path in the proof blob
                # so the chain-of-custody is preserved.
                parsed_artifact.no_changes_required_proof[
                    "original_legacy_artifact_path"
                ] = str(rpap_parsed)
                parsed_artifact.no_changes_required_proof[
                    "original_legacy_artifact_sha256"
                ] = _hash_file_sha256(rpap_parsed)
            parsed_artifact.write(canonical_target)
        except Exception:
            pass
    elif source_surface != "result_artifact_path" and rpap_parsed is not None:
        try:
            parsed_artifact.write(rpap_parsed)
        except Exception:
            pass

    # Reflect the canonical fields onto the attempt record.
    # Commit/push SHAs are the single source of truth (Section 8).
    if parsed_artifact.produced_commit_shas:
        rec.produced_commit_sha = parsed_artifact.produced_commit_shas[0]
    if parsed_artifact.pushed_commit_shas:
        rec.pushed_commit_sha = parsed_artifact.pushed_commit_shas[0]
    if parsed_artifact.attempt_nonce:
        rec.extra.setdefault("attempt_nonce", parsed_artifact.attempt_nonce)
    rec.extra["worker_result_artifact"] = parsed_artifact.to_dict()
    rec.extra["worker_result_source_surface"] = source_surface

    # Set the legacy no_changes_required_proof for the existing
    # C14 hook path so subsequent polls trigger
    # LIFECYCLE_NO_CHANGES_REQUIRED.
    if parsed_artifact.result_type == RESULT_TYPE_NO_CHANGES_REQUIRED:
        if parsed_artifact.no_changes_required_proof:
            rec.extra["no_changes_required_proof"] = (
                parsed_artifact.no_changes_required_proof
            )
    try:
        WorkerAttemptStore(WORKER_ATTEMPTS_DIR).write(rec)
    except Exception as _we:
        import traceback as _tb
        _tb_str = "".join(_tb.format_exception(type(_we), _we, _we.__traceback__))[-1500:]
        log(
            "warning",
            "round-50.1: WorkerAttemptStore.write failed",
            attempt_id=rec.attempt_id,
            error=str(_we)[:200],
            traceback_summary=_tb_str,
            worker_attempts_dir=str(WORKER_ATTEMPTS_DIR),
        )

    log(
        "info",
        "round-50.1: worker-result artifact ingested",
        attempt_id=rec.attempt_id,
        result_type=parsed_artifact.result_type,
        produced=parsed_artifact.produced_commit_shas,
        pushed=parsed_artifact.pushed_commit_shas,
        source=source_surface,
    )
    return True


def poll_worker_attempt(
    *, attempt_id: str, lease: Optional[dict],
) -> Optional[str]:
    """Poll a worker attempt and return the new lifecycle.

    Returns:
        ``None`` if the attempt is still WORKER_RUNNING.
        ``"DIED"`` if the worker is dead without a verified push.
        ``"PUSHED"`` if the worker is dead AND a verified push is
            recorded (rare — usually the supervisor's own head
            detection handles this branch).

    Side effects:
        - Updates ``last_progress_at`` on every poll.
        - Transitions the attempt to ``WORKER_EXITED_NO_PUSH``
          when the worker is dead and no push is verified.
        - Records ``exit_code`` and ``signal`` in the attempt.
        - Releases the lease if the worker is dead.
        - Unmarks every launched_event_id associated with the
          attempt so the durable work item becomes runnable again
          on the next heartbeat.
    """
    from autocoder_orchestration.worker_attempt import (
        LIFECYCLE_COMMIT_PRODUCED,
        LIFECYCLE_NO_CHANGES_REQUIRED,
        LIFECYCLE_PUSH_VERIFIED,
        LIFECYCLE_RECOVERY_CHECK,
        LIFECYCLE_TERMINAL_REPAIRED,
        LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE,
        LIFECYCLE_WORKER_EXITED_NO_PUSH,
        LIFECYCLE_WORKER_RUNNING,
        TERMINAL_LIFECYCLES,
        WorkerAttemptStore,
        WorkerResultArtifact,
        WORKER_RESULT_SCHEMA_VERSION,
        RESULT_TYPE_NO_CHANGES_REQUIRED,
        RESULT_TYPE_REPAIR_PUSHED,
        RESULT_TYPE_REPAIR_COMMIT_PRODUCED,
        RESULT_TYPE_COMMIT_PRODUCED_NOT_PUSHED,
        RESULT_TYPE_WORKER_EXECUTION_FAILED,
    )
    store = _worker_attempt_store()
    rec = store.read(attempt_id)
    if rec is None:
        return None
    if rec.lifecycle in TERMINAL_LIFECYCLES:
        return None
    if not pid_alive(rec.pid):
        exit_code, signal = _reap_worker(rec.pid)
        rec.exit_code = exit_code
        rec.signal = signal
        rec.last_progress_at = now_iso()
        rec.finished_at = rec.finished_at or now_iso()
        # Round-50.1: ingest the canonical
        # WorkerResultArtifact written by the worker at the
        # deterministic per-attempt path. This is the
        # AUTHORITATIVE result source for terminal
        # classification (Section 8 commit provenance, Section
        # 11 REPAIRED / SUPERSEDED / ALREADY_SATISFIED
        # semantics). A standalone roundNN_worker_attempt_result.json
        # at the worker-attempts-dir is mapped to the canonical
        # artifact via the legacy compatibility parser.
        try:
            _ingested = _round50_ingest_worker_result_artifact(rec)
            log(
                "info",
                "round-50.1: worker-result ingestion called",
                attempt_id=attempt_id,
                ingested=_ingested,
            )
        except Exception as _ingest_exc:
            log(
                "warning",
                "round-50.1: worker-result ingestion raised",
                attempt_id=attempt_id,
                error=str(_ingest_exc)[:200],
            )
        # Round-45 P1 precedence fix: classify a dead worker
        # in the correct order. The previous ordering (round-41
        # dispatch-ledger lookup FIRST, then round-37 push
        # recovery) had a silent-bypass defect: a stale
        # ``round_<N>_dispatch.json`` file at the current head —
        # written by an earlier round that concluded no source
        # edits were required — shadowed a genuine worker push
        # that landed between heartbeats. The worker was wrongly
        # terminalized as LIFECYCLE_NO_CHANGES_REQUIRED, the
        # head-rebind path's ``find_active_worker_attempt_for_head``
        # could not find the attempt (it only matches
        # PUSH_VERIFIED state), and the controller stayed stuck
        # in REPAIRING_REVIEW_FINDINGS while the live GitHub head
        # advanced past the legitimate repair.
        #
        # The corrected precedence is:
        #   1. WORKER-EMITTED proof via ``extra.no_changes_required_proof``
        #      (a per-round ``round_<N>_disposition`` blob) is the
        #      AUTHORITATIVE structured signal: it carries
        #      per-finding disposition, verifier-tests-passed count,
        #      and exact head. A worker that explicitly emitted this
        #      proof MUST be routed to LIFECYCLE_NO_CHANGES_REQUIRED
        #      regardless of what the live GitHub head looks like —
        #      otherwise a repair that legitimately emitted
        #      NO_CHANGES_REQUIRED AND happened to land a non-push
        #      commit would be misclassified. We honour this signal
        #      WITHOUT invoking the push-recovery probes.
        #   2. STALE dispatch-ledger evidence
        #      (``~/.hermes/aed/runs/.../round_<N>_dispatch.json``
        #      at the current head) is a FALLBACK only. It MUST NOT
        #      shadow a real worker push that the round-37
        #      attribution can verify. So the dispatch-ledger
        #      lookup runs AFTER push-recovery; if push-recovery
        #      attributed the dead worker to a live head, we stay
        #      PUSH_VERIFIED and never consult the dispatch ledger.
        #   3. ROUND-37 PUSH-RECOVERY runs between the two
        #      worker-emitted and dispatch-ledger NO_OP checks.
        #      It requires (a) the live PR head advanced past
        #      ``rec.prelaunch_head``, (b) origin/<branch> matches
        #      that head, AND (c) the committer date is after
        #      ``rec.started_at``. Exactly-one ownership: terminal
        #      transitions happen at most once per attempt.
        #
        # Step 1: worker-emitted proof (authoritative).
        _worker_emitted_no_op: bool = False
        if rec.lifecycle != LIFECYCLE_PUSH_VERIFIED:
            _extra_d = rec.extra if isinstance(rec.extra, dict) else {}
            for _key in (
                "no_changes_required_proof",
                "round_41_disposition",
                "round_42_disposition",
                "round_dispatch",
            ):
                _candidate = _extra_d.get(_key)
                if isinstance(_candidate, dict) and _candidate:
                    _worker_emitted_no_op = True
                    break
            # Round-42: also check the canonical round
            # dispatch JSON in the AED runs directory
            # (preserves the round-41 path where the
            # worker's structured outcome was written
            # there instead of the attempt's extra).
            if not _worker_emitted_no_op and rec.expected_branch:
                try:
                    _home_for_dispatch = (
                        os.environ.get("HOME") or "~"
                    )
                    _round_dir = (
                        Path(_home_for_dispatch)
                        / ".hermes" / "aed" / "runs"
                        / str(REPO_OWNER) / str(REPO_NAME)
                        / str(PR_NUMBER)
                    )
                    if _round_dir.is_dir():
                        for _p in sorted(
                            _round_dir.glob("round_*_dispatch.json"),
                        ):
                            try:
                                _d = json.loads(
                                    _p.read_text(encoding="utf-8"),
                                )
                            except Exception:  # noqa: BLE001
                                continue
                            if not isinstance(_d, dict):
                                continue
                            _outcome = str(_d.get("outcome") or "")
                            _verdict = str(_d.get("verdict") or "")
                            _head = str(
                                _d.get("head_sha_at_dispatch") or ""
                            )
                            if (
                                _outcome.upper() in (
                                    "NO_OP", "NO_CHANGES_REQUIRED",
                                )
                                or _verdict
                                == "no_source_edit_required"
                            ) and (
                                not _head
                                or _head == rec.prelaunch_head
                            ):
                                _worker_emitted_no_op = True
                                break
                except Exception:  # noqa: BLE001
                    pass
        if (
            rec.lifecycle != LIFECYCLE_PUSH_VERIFIED
            and not _worker_emitted_no_op
        ):
            # Round-42: define the helper closures for the
            # unattributed-classification path used by the
            # push-recovery branch below.
            _live_head_for_classify = ""

            def _classify_unattributed() -> None:
                """Classify the head movement as UNATTRIBUTED.

                Round-42: the worker attempt contains no
                ``pushed_commit_sha`` matching the live
                head. The head movement is external
                (operator / recovery / another actor). The
                attempt becomes terminal as
                ``UNATTRIBUTED_HEAD_ADVANCE``; the
                worker's finding remains RETRY_PENDING on
                the new head (or a subsequent no-op
                classifies the new head as
                already-satisfied).

                The attempt's ``pushed_commit_sha`` and
                ``produced_commit_sha`` are NOT overwritten
                — they preserve the worker's own evidence
                (which may be empty for a no-op).
                """
                try:
                    rec.assert_can_transition_to(
                        LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE,
                    )
                except Exception as exc:  # noqa: BLE001
                    # If the transition is not allowed,
                    # fall back to NO_PUSH (the worker's
                    # own state machine will keep retrying
                    # on the new head).
                    try:
                        rec.assert_can_transition_to(
                            LIFECYCLE_WORKER_EXITED_NO_PUSH,
                        )
                        rec.lifecycle = (
                            LIFECYCLE_WORKER_EXITED_NO_PUSH
                        )
                        rec.terminal_reason = (
                            f"worker reported no pushed_commit_sha; "
                            f"head advanced to "
                            f"{_live_head_for_classify[:12]!r} "
                            f"(external/unattributed): "
                            f"{str(exc)[:120]}"
                        )
                        log(
                            "warning",
                            "round-42 unattributed head "
                            "advance; fallback to NO_PUSH",
                            attempt_id=attempt_id,
                            pid=rec.pid,
                            live_head=(
                                _live_head_for_classify[:12]
                            ),
                            transition_error=str(exc)[:200],
                        )
                        return
                    except Exception:  # noqa: BLE001
                        # Last-resort: do not invent
                        # provenance. The attempt stays in
                        # its current lifecycle and the
                        # next poll re-evaluates.
                        log(
                            "warning",
                            "round-42 unattributed head "
                            "advance; no transition possible; "
                            "attempt stays in current lifecycle",
                            attempt_id=attempt_id,
                            pid=rec.pid,
                        )
                        return
                rec.lifecycle = (
                    LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE
                )
                rec.terminal_reason = (
                    f"head advanced to "
                    f"{_live_head_for_classify[:12]!r} but "
                    f"worker did NOT record a matching "
                    f"pushed_commit_sha; head movement is "
                    f"external/unattributed"
                )
                log(
                    "warning",
                    "round-42 unattributed head advance; "
                    "NOT promoted to PUSH_VERIFIED",
                    attempt_id=attempt_id,
                    pid=rec.pid,
                    live_head=_live_head_for_classify[:12],
                )

            def _fallback_no_push() -> None:
                """Fall back to NO_PUSH when no head
                movement was observed and no worker-emitted
                pushed evidence exists. Preserves the
                round-37 contract: dead worker with no
                remote movement is a no-push.
                """
                try:
                    rec.assert_can_transition_to(
                        LIFECYCLE_WORKER_EXITED_NO_PUSH,
                    )
                    rec.lifecycle = (
                        LIFECYCLE_WORKER_EXITED_NO_PUSH
                    )
                    rec.terminal_reason = (
                        "worker exited without recording a "
                        "pushed_commit_sha; no head movement "
                        "observed"
                    )
                except Exception as exc:  # noqa: BLE001
                    log(
                        "warning",
                        "round-42 fallback NO_PUSH transition "
                        "failed",
                        attempt_id=attempt_id,
                        error=str(exc)[:200],
                    )

            # Round-42: the round-37 push-recovery branch
            # has been REPLACED. The supervisor MUST NOT
            # promote a dead worker to PUSH_VERIFIED based
            # on remote-side evidence (live-head movement,
            # origin/live equality, committer-date after
            # started_at). The worker MUST durably record
            # its own ``pushed_commit_sha`` (via
            # ``WorkerResultArtifact`` or via the attempt's
            # own ``pushed_commit_sha`` field, which only
            # the worker execution path is allowed to set).
            #
            # The branch below is read-only: it inspects
            # whether the worker has already recorded a
            # pushed_commit_sha for the new live head. If
            # not, the head movement is classified as
            # ``UNATTRIBUTED_HEAD_ADVANCE`` and the worker
            # attempt becomes terminal WITHOUT being
            # promoted to PUSH_VERIFIED.
            try:
                _token_for_probe = get_github_token() or ""
                if (
                    rec.expected_branch
                    and _token_for_probe
                ):
                    _pr_probe = github_get(
                        f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}",  # type: ignore[name-defined]
                        _token_for_probe,
                    )
                    if _pr_probe:
                        _live_head_for_classify = (
                            _pr_probe.get("head", {}).get("sha")
                            or ""
                        )
            except Exception:  # noqa: BLE001
                _live_head_for_classify = ""

            # The worker's recorded pushed_commit_sha is
            # the SOLE source of truth. If it matches the
            # live head, the worker owned the push. If it
            # is missing or matches a different head, the
            # head movement is unattributed.
            _worker_reported_push = (
                str(rec.pushed_commit_sha or "").strip()
            )
            _worker_reported_produced = (
                str(rec.produced_commit_sha or "").strip()
            )
            # Multi-commit list (round-42 §13). The worker
            # may report an ORDERED list of pushed SHAs in
            # ``rec.extra.pushed_commit_shas``.
            _extra_list = (
                rec.extra if isinstance(rec.extra, dict) else {}
            )
            _worker_reported_pushes = tuple(
                str(s) for s in (
                    _extra_list.get("pushed_commit_shas") or []
                ) if s
            )
            _worker_reported_produceds = tuple(
                str(s) for s in (
                    _extra_list.get("produced_commit_shas") or []
                ) if s
            )
            if _worker_reported_pushes:
                # The worker reported at least one pushed
                # SHA. If the live head equals the LAST
                # pushed SHA, the worker owned the push.
                _last_worker_push = _worker_reported_pushes[-1]
                if (
                    _live_head_for_classify
                    and _live_head_for_classify == _last_worker_push
                ):
                    # Round-42 positive: the worker
                    # durably reported the live head as
                    # the worker's own push. Transition
                    # to PUSH_VERIFIED via the canonical
                    # path (WORKER_RUNNING -> COMMIT_PRODUCED
                    # -> PUSH_VERIFIED).
                    try:
                        rec.assert_can_transition_to(
                            LIFECYCLE_COMMIT_PRODUCED,
                        )
                        rec.lifecycle = LIFECYCLE_COMMIT_PRODUCED
                    except Exception:  # noqa: BLE001
                        # WORKER_RUNNING -> PUSH_VERIFIED
                        # is not a direct transition; if
                        # the canonical COMMIT_PRODUCED
                        # intermediate is not allowed, try
                        # the direct path. The state
                        # machine in round-42 may evolve.
                        pass
                    try:
                        rec.assert_can_transition_to(
                            LIFECYCLE_PUSH_VERIFIED,
                        )
                        rec.lifecycle = LIFECYCLE_PUSH_VERIFIED
                        rec.origin_head_verified = True
                        rec.github_head_verified = True
                        log(
                            "info",
                            "round-42 positive: worker reported "
                            "pushed_commit_sha matches live head; "
                            "PUSH_VERIFIED",
                            attempt_id=attempt_id,
                            pid=rec.pid,
                            worker_pushed=_last_worker_push[:12],
                            live_head=_live_head_for_classify[:12],
                        )
                    except Exception as exc:  # noqa: BLE001
                        log(
                            "warning",
                            "round-42 transition to PUSH_VERIFIED "
                            "failed",
                            attempt_id=attempt_id,
                            error=str(exc)[:200],
                        )
                else:
                    # Worker reported a push that does
                    # not match the current live head.
                    # This is an UNATTRIBUTED head
                    # movement.
                    if (
                        _live_head_for_classify
                        and _live_head_for_classify
                        != rec.prelaunch_head
                    ):
                        _classify_unattributed()
                    else:
                        _fallback_no_push()
            elif _worker_reported_push:
                # The single-SHA fallback (legacy
                # ``rec.pushed_commit_sha``).
                if (
                    _live_head_for_classify
                    and _live_head_for_classify
                    == _worker_reported_push
                ):
                    # Round-42 positive: the worker
                    # durably reported the live head as
                    # the worker's own push. Transition
                    # to PUSH_VERIFIED via the canonical
                    # path.
                    try:
                        rec.assert_can_transition_to(
                            LIFECYCLE_COMMIT_PRODUCED,
                        )
                        rec.lifecycle = LIFECYCLE_COMMIT_PRODUCED
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        rec.assert_can_transition_to(
                            LIFECYCLE_PUSH_VERIFIED,
                        )
                        rec.lifecycle = LIFECYCLE_PUSH_VERIFIED
                        rec.origin_head_verified = True
                        rec.github_head_verified = True
                        log(
                            "info",
                            "round-42 positive: worker reported "
                            "pushed_commit_sha matches live head; "
                            "PUSH_VERIFIED (single-SHA path)",
                            attempt_id=attempt_id,
                            pid=rec.pid,
                            worker_pushed=_worker_reported_push[:12],
                            live_head=_live_head_for_classify[:12],
                        )
                    except Exception as exc:  # noqa: BLE001
                        log(
                            "warning",
                            "round-42 transition to PUSH_VERIFIED "
                            "failed (single-SHA path)",
                            attempt_id=attempt_id,
                            error=str(exc)[:200],
                        )
                else:
                    if (
                        _live_head_for_classify
                        and _live_head_for_classify
                        != rec.prelaunch_head
                    ):
                        _classify_unattributed()
                    else:
                        _fallback_no_push()
            else:
                # Worker has NOT recorded any
                # pushed_commit_sha for this attempt.
                # Round-50.1 Section 6: distinguish
                #   WORKER_RESULT_MISSING (worker did push
                #     but failed to write the canonical
                #     WorkerResultArtifact; future retry
                #     can claim this generation).
                #   UNATTRIBUTED_HEAD_ADVANCE (no evidence
                #     that this worker's process did the
                #     push; external actor / recovery).
                #   WORKER_EXITED_NO_PUSH (worker exited
                #     cleanly, no remote head change).
                if (
                    _live_head_for_classify
                    and _live_head_for_classify
                    != rec.prelaunch_head
                ):
                    # Heuristic: if the worker session
                    # produced a non-trivial commit log
                    # (the worker's own stdout shows
                    # ``git commit`` + ``git push``), the
                    # worker DID push and just failed to
                    # report it. Treat that as RESULT_MISSING.
                    _stdout_path = (
                        Path(str(STATE_DIR)) / "worker_attempts"
                        / f"{attempt_id}.stdout.log"
                    )  # type: ignore[name-defined]
                    _worker_pushed_but_unreported = False
                    if _stdout_path.is_file():
                        try:
                            _tail = _stdout_path.read_text(
                                encoding="utf-8",
                                errors="replace",
                            )[-4000:]
                            if (
                                "git push" in _tail
                                or "git rev-parse HEAD" in _tail
                                or "PUSH_VERIFIED" in _tail
                            ):
                                _worker_pushed_but_unreported = True
                        except OSError:
                            pass
                    if _worker_pushed_but_unreported:
                        # The worker pushed but did not write
                        # the canonical artifact. Section 6:
                        # classify as WORKER_RESULT_MISSING.
                        try:
                            rec.assert_can_transition_to(
                                LIFECYCLE_WORKER_RESULT_MISSING,
                            )
                            rec.lifecycle = (
                                LIFECYCLE_WORKER_RESULT_MISSING
                            )
                            rec.terminal_reason = (
                                "remote head advanced to "
                                f"{_live_head_for_classify[:12]!r} "
                                "but worker did NOT write canonical "
                                "WorkerResultArtifact. Commit is "
                                "UNATTRIBUTED_HEAD_ADVANCE — "
                                "retry needed to claim this "
                                "generation."
                            )
                            log(
                                "warning",
                                "round-50.1: worker-result missing",
                                attempt_id=attempt_id,
                                pid=rec.pid,
                                live_head=(
                                    _live_head_for_classify[:12]
                                ),
                            )
                            # Fall through to persist and
                            # return.
                            try:
                                WorkerAttemptStore(
                                    str(STATE_DIR) / "worker_attempts"  # type: ignore[name-defined]
                                ).write(rec)
                            except Exception:
                                pass
                            return
                        except Exception:
                            # Transition refused — fall
                            # through to UNATTRIBUTED.
                            pass
                    _classify_unattributed()
                else:
                    _fallback_no_push()
        # Round-41: route workers that exited cleanly with
        # a structured ``NO_CHANGES_REQUIRED`` disposition
        # to the new terminal-success lifecycle. The worker
        # is considered to have ACTUALLY EXECUTED the
        # directive if its attempt record carries an
        # ``extra.no_changes_required_proof`` blob with
        # structured evidence (per-finding disposition,
        # verification summary, exact head), OR if the
        # canonical AED run-state ``round_<N>_dispatch.json``
        # file records ``outcome: NO_OP`` at the exact head.
        #
        # Round-45: the dispatch-ledger branch is the FALLBACK
        # only — it MUST NOT shadow a real worker push that
        # the round-37 push-recovery just attributed. So the
        # gate skips this block when ``rec.lifecycle`` was
        # already promoted to LIFECYCLE_PUSH_VERIFIED above.
        if rec.lifecycle != LIFECYCLE_PUSH_VERIFIED:
            _no_op_proof: Optional[dict] = None
            _extra_d = rec.extra if isinstance(rec.extra, dict) else {}
            # Accept either the canonical
            # ``no_changes_required_proof`` key (round-41
            # contract) OR a per-round disposition blob the
            # worker chose to write (e.g.
            # ``round_41_disposition``). The blob is a dict
            # with category/verification keys, which is the
            # structured no-op proof.
            for _key in (
                "no_changes_required_proof",
                "round_41_disposition",
                "round_42_disposition",
                "round_dispatch",
            ):
                _candidate = _extra_d.get(_key)
                if isinstance(_candidate, dict) and _candidate:
                    _no_op_proof = {
                        "source": "attempt_extra",
                        "key": _key,
                        **_candidate,
                    }
                    break
            else:
                try:
                    # The AED runs directory is at
                    # ``$HOME/.hermes/aed/runs/<owner>/<repo>/<pr>``.
                    # We look up ``$HOME`` from the env (with a
                    # fallback to ``~``) so the test harness can
                    # override HOME without rebinding the C
                    # module's ``os.path.expanduser``.
                    _home = os.environ.get("HOME") or "~"
                    _round_dir = (
                        Path(_home)
                        / ".hermes" / "aed" / "runs" / str(REPO_OWNER) / str(REPO_NAME) / str(PR_NUMBER)
                    )
                    _round_files: list = []
                    if _round_dir.is_dir():
                        for _p in sorted(_round_dir.glob("round_*_dispatch.json")):
                            _round_files.append(_p)
                    for _p in _round_files:
                        try:
                            _d = json.loads(_p.read_text(encoding="utf-8"))
                        except Exception:
                            continue
                        if not isinstance(_d, dict):
                            continue
                        _outcome = str(_d.get("outcome") or "")
                        _verdict = str(_d.get("verdict") or "")
                        _head = str(_d.get("head_sha_at_dispatch") or "")
                        if (
                            _outcome.upper() in ("NO_OP", "NO_CHANGES_REQUIRED")
                            or _verdict == "no_source_edit_required"
                        ) and (
                            not _head
                            or _head == rec.prelaunch_head
                            or _head == str(
                                globals().get("AUTHORITATIVE_HEAD", "") or ""
                            )
                        ):
                            _no_op_proof = {
                                "source": "round_dispatch_json",
                                "path": str(_p),
                                "outcome": _outcome,
                                "verdict": _verdict,
                                "head_sha_at_dispatch": _head,
                                "directive_id": str(_d.get("directive_id") or ""),
                                "round_index": _d.get("round_index"),
                            }
                            break
                except Exception as exc:  # noqa: BLE001
                    log(
                        "warning",
                        "round-41 dispatch-JSON lookup failed; "
                        "continuing with extra-only proof",
                        attempt_id=attempt_id,
                        error=str(exc)[:200],
                    )
            if isinstance(_no_op_proof, dict) and _no_op_proof:
                try:
                    rec.assert_can_transition_to(
                        LIFECYCLE_NO_CHANGES_REQUIRED,
                    )
                    rec.lifecycle = LIFECYCLE_NO_CHANGES_REQUIRED
                    rec.terminal_reason = (
                        "worker executed and emitted structured "
                        "NO_CHANGES_REQUIRED proof; no push expected."
                    )
                    if isinstance(rec.extra, dict):
                        rec.extra.setdefault(
                            "no_changes_required_proof", _no_op_proof
                        )
                    log(
                        "info",
                        "round-41 no-op worker; routed to "
                        "LIFECYCLE_NO_CHANGES_REQUIRED",
                        attempt_id=attempt_id,
                        pid=rec.pid,
                        source=_no_op_proof.get("source", "attempt_extra"),
                    )
                    store.write(rec)
                    if lease is not None:
                        try:
                            remove_lease()
                        except Exception:  # noqa: BLE001
                            pass
                    # Round-46 C14 follow-up: if the worker did not
                    # write extra.no_changes_required_proof (e.g.
                    # a subagent worker that printed its
                    # disposition to stdout but did not persist
                    # it), extract per-finding dispositions from
                    # the worker stdout and persist them into
                    # the attempt's extra so the C14 hook can
                    # consume the targeted drain event. Without
                    # this, subagent workers that do not follow
                    # the round-41 contract would silently skip
                    # drain-event consumption.
                    try:
                        _stdout_path = rec.stdout_path
                        if (
                            _stdout_path
                            and Path(_stdout_path).exists()
                        ):
                            _text = Path(_stdout_path).read_text(
                                encoding="utf-8", errors="replace"
                            )[:200000]
                            _extracted = []
                            import re as _re
                            # Match legacy outputs ("Finding thread:<tid>
                            # — **DISPOSITION**") AND the round-46+
                            # preferred form ("finding_id:
                            # thread:<tid>\n... disposition: B —
                            # DISPOSITION"). One pass collects both
                            # patterns and yields group(1)=finding_id,
                            # group(2)=disposition.
                            # Legacy worker format:
                            #   Finding `thread:<tid>` — "Title" — **DISP**
                            # OR Finding thread:<tid> ... **DISP**
                            _p1 = _re.compile(
                                r"Finding\s+[`'\"]?(thread:\S+)[`'\"]?\s+[-\u2013\u2014]+\s+[`'\"]?\*?\*?([A-Z_]+)\*?\*?"
                            )
                            # round-46+ preferred format:
                            #   finding_id: thread:<tid>
                            # ... disposition: B - DISP
                            _p2 = _re.compile(
                                r"finding_id:\s*(thread:\S+)"
                                r"[\s\S]{0,400}?disposition:"
                                r"\s*[A-Z]?\s*[-\u2013\u2014]?\s*\**([A-Z_]+)\**"
                            )
                            for _m in list(_p1.finditer(_text)) + list(_p2.finditer(_text)):
                                _fid = _m.group(1).strip()
                                _disp = _m.group(2).strip()
                                if not _fid.startswith("thread:"):
                                    continue
                                if not _disp:
                                    continue
                                _tid = _fid.split(":", 1)[1]
                                _extracted.append({
                                    "finding_id": _fid,
                                    "disposition": _disp,
                                    "thread_id": _tid,
                                })
                            if _extracted and isinstance(rec.extra, dict):
                                rec.extra["no_changes_required_proof"] = {
                                    "findings": _extracted,
                                    "source": "stdout_extraction",
                                }
                                store.write(rec)
                    except Exception as exc:  # noqa: BLE001
                        log(
                            "warning",
                            "round-46 C14: stdout extraction failed; "
                            "continuing without per-finding proof",
                            error=str(exc)[:200],
                        )
                    # Round-46 C14: thread-disposition terminalization.
                    # When the NO_CHANGES_REQUIRED proof carries
                    # per-finding dispositions for a focused
                    # thread, consume the corresponding
                    # ``unresolved_thread_drain:<tid>`` events so
                    # the same unchanged thread is not redundantly
                    # redispatched. The hook is the SINGLE entry
                    # point that drains a targeted event.
                    _current_live_head = str(
                        globals().get("AUTHORITATIVE_HEAD", "") or ""
                    )
                    _result_identity = {
                        "repo": str(REPO_OWNER),
                        "pr_number": int(PR_NUMBER),
                        "thread_id": "",
                        "current_live_head": _current_live_head,
                    }
                    for _row in extract_per_finding_thread_dispositions(
                        _no_op_proof,
                        evaluated_head=_current_live_head,
                        directive_digest=str(
                            _no_op_proof.get(
                                "directive_sha256", _no_op_proof.get("directive_digest", "")
                            ) or ""
                        ),
                        worker_attempt_id=str(attempt_id or ""),
                    ):
                        _tid = _row.get("thread_id") or ""
                        if not _tid:
                            continue
                        _eid = f"unresolved_thread_drain:{_tid}"
                        _result_identity["thread_id"] = _tid
                        try:
                            consume_thread_drain_event_in_terminal_disposition(
                                event_id=_eid,
                                thread_id=_tid,
                                provider="coderabbit",
                                evaluated_head=_current_live_head,
                                disposition_raw=_row.get("disposition_raw", ""),
                                evidence=_row.get("evidence", ""),
                                worker_attempt_id=str(attempt_id or ""),
                                directive_digest=str(
                                    _no_op_proof.get(
                                        "directive_sha256",
                                        _no_op_proof.get("directive_digest", ""),
                                    ) or ""
                                ),
                                result_identity=_result_identity,
                                thread_record={
                                    "thread_id": _tid,
                                    "commit_oid": _current_live_head,
                                },
                                extra_identity=_row.get("extra", {}),
                            )
                        except Exception as exc:  # noqa: BLE001
                            log(
                                "warning",
                                "round-46 C14: per-thread consume failed; "
                                "continuing without split",
                                event_id=_eid,
                                error=str(exc)[:200],
                            )
                    return "DIED"
                except Exception as exc:  # noqa: BLE001
                    log(
                        "warning",
                        "could not transition to "
                        "LIFECYCLE_NO_CHANGES_REQUIRED; "
                        "falling through to NO_PUSH",
                        attempt_id=attempt_id,
                        error=str(exc)[:200],
                    )
        # Round-45: the round-37 push-recovery body was
        # pulled forward in this function so the round-37
        # attribution runs BEFORE the round-41 NO_OP routing
        # (the previous ordering had a silent-bypass defect:
        # a stale ``round_<N>_dispatch.json`` at the current
        # head shadowed a genuine worker push and routed the
        # worker to LIFECYCLE_NO_CHANGES_REQUIRED, orphaning
        # the legitimate repair). The legacy duplicate block
        # at the previous line offset (1513+) is removed; only
        # the WORKER_EXITED_NO_PUSH transition remains here.
        # Round-42: skip the WORKER_EXITED_NO_PUSH fallback
        # when the attempt is already in a terminal state
        # (e.g. UNATTRIBUTED_HEAD_ADVANCE from the round-42
        # helper above). Attempting to transition from a
        # terminal state would force the RECOVERY_CHECK
        # safety-net and discard the round-42 classification.
        if rec.lifecycle != LIFECYCLE_PUSH_VERIFIED and rec.lifecycle not in (
            LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE,
            LIFECYCLE_NO_CHANGES_REQUIRED,
            LIFECYCLE_TERMINAL_REPAIRED,
        ):
            try:
                rec.assert_can_transition_to(
                    LIFECYCLE_WORKER_EXITED_NO_PUSH,
                )
                rec.lifecycle = LIFECYCLE_WORKER_EXITED_NO_PUSH
                if exit_code is not None and exit_code != 0:
                    rec.terminal_reason = (
                        f"worker exit_code={exit_code}"
                    )
                elif signal is not None:
                    rec.terminal_reason = (
                        f"worker signal={signal}"
                    )
                else:
                    rec.terminal_reason = "worker exited without push"
            except Exception as exc:  # noqa: BLE001
                log(
                    "warning",
                    "could not transition attempt to WORKER_EXITED_NO_PUSH",
                    attempt_id=attempt_id,
                    error=str(exc),
                )
                # Force a transition via RECOVERY_CHECK as a safety
                # net; the next poll or recovery cycle will finalize.
                rec.lifecycle = LIFECYCLE_RECOVERY_CHECK
        # Round-46 C14 follow-up: if the worker's stdout emits
        # per-finding dispositions but the attempt has no
        # ``extra.no_changes_required_proof``, extract the
        # dispositions from stdout, persist them as a structured
        # proof, and (when valid) drive the per-thread drain
        # consumption. This handles the orphan-reaped subagent
        # workers that do not pre-populate
        # ``no_changes_required_proof`` on entry. Idempotent on
        # generation; only fires when an actual terminal
        # disposition is recoverable from the worker's output.
        try:
            _stdout_path = (
                getattr(rec, "stdout_path", None) if rec is not None
                else None
            )
            if _stdout_path and Path(_stdout_path).exists():
                _text = Path(_stdout_path).read_text(
                    encoding="utf-8", errors="replace"
                )[:200000]
                _extracted = []
                import re as _re
                _pat_choices = [
                    # Legacy: Finding `thread:<tid>` — "Title" — **DISP**
                    _re.compile(
                        r"Finding\s+[`'\"]?(thread:\S+)[`'\"]?\s+"
                        r"[-\u2013\u2014]+\s+[`'\"]?\*?\*?([A-Z_]+)\*?\*?"
                    ),
                    # round-46+: finding_id: thread:<tid> ... disposition: ...
                    _re.compile(
                        r"finding_id:\s*(thread:\S+)[\s\S]{0,400}?"
                        r"disposition:\s*[A-Z]?\s*[—–-]?\s*\**([A-Z_]+)\**"
                    ),
                ]
                _all_matches = []
                for _pat in _pat_choices:
                    _all_matches.extend(list(_pat.finditer(_text)))
                for _m in _all_matches:
                    _fid = _m.group(1).strip()
                    _disp = _m.group(2).strip()
                    if not _fid.startswith("thread:"):
                        continue
                    if not _disp:
                        continue
                    _tid = _fid.split(":", 1)[1]
                    _extracted.append({
                        "finding_id": _fid,
                        "disposition": _disp,
                        "thread_id": _tid,
                    })
                if _extracted and isinstance(rec.extra, dict):
                    rec.extra["no_changes_required_proof"] = {
                        "findings": _extracted,
                        "source": "stdout_extraction_orphan",
                    }
                    store.write(rec)
                    # Drive the C14 consumption loop for the
                    # just-extracted per-thread terminals.
                    _current_live_head = str(
                        globals().get("AUTHORITATIVE_HEAD", "") or ""
                    )
                    _result_identity = {
                        "repo": str(REPO_OWNER),
                        "pr_number": int(PR_NUMBER),
                        "thread_id": "",
                        "current_live_head": _current_live_head,
                    }
                    for _row in extract_per_finding_thread_dispositions(
                        rec.extra["no_changes_required_proof"],
                        evaluated_head=_current_live_head,
                        directive_digest=str(
                            rec.extra["no_changes_required_proof"].get(
                                "directive_sha256",
                                rec.extra["no_changes_required_proof"].get(
                                    "directive_digest", "",
                                ),
                            ) or ""
                        ),
                        worker_attempt_id=str(attempt_id or ""),
                    ):
                        _tid = _row.get("thread_id") or ""
                        if not _tid:
                            continue
                        _eid = f"unresolved_thread_drain:{_tid}"
                        _result_identity["thread_id"] = _tid
                        try:
                            consume_thread_drain_event_in_terminal_disposition(
                                event_id=_eid,
                                thread_id=_tid,
                                provider="coderabbit",
                                evaluated_head=_current_live_head,
                                disposition_raw=_row.get("disposition_raw", ""),
                                evidence=_row.get("evidence", ""),
                                worker_attempt_id=str(attempt_id or ""),
                                directive_digest="",
                                result_identity=_result_identity,
                                thread_record={
                                    "thread_id": _tid,
                                    "commit_oid": _current_live_head,
                                },
                                extra_identity=_row.get("extra", {}),
                            )
                        except Exception as exc:  # noqa: BLE001
                            log(
                                "warning",
                                "round-46 C14: orphan stdout-extract "
                                "consume failed; continuing",
                                event_id=_eid,
                                error=str(exc)[:200],
                            )
        except Exception as exc:  # noqa: BLE001
            log(
                "warning",
                "round-46 C14: WORKER_EXITED_NO_PUSH stdout "
                "extraction failed; continuing",
                error=str(exc)[:200],
            )
        store.write(rec)
        # Round-39 P1#5: when the deferred push-recovery
        # branch attributed the dead worker to a live head
        # (``rec.lifecycle == LIFECYCLE_PUSH_VERIFIED``),
        # the lease MUST NOT be released here. The
        # head-rebind path that runs immediately after
        # this poll uses ``find_active_worker_attempt_for_head``
        # to find the attempt and route the head advance
        # through ``mark_head_advanced_public``. Releasing
        # the lease here would orphan the attempt and
        # leave the controller in REPAIRING_REVIEW_FINDINGS
        # while the live GitHub head advanced past it.
        # The lease is released ONLY when the attempt is
        # truly terminal (WORKER_EXITED_NO_PUSH).
        if (
            lease is not None
            and rec.lifecycle != LIFECYCLE_PUSH_VERIFIED
        ):
            try:
                remove_lease()
            except Exception:  # noqa: BLE001
                pass
        # Unmark every launched_event_id associated with this
        # attempt so the durable work item becomes runnable again.
        # Round-36 invariant: launched != terminal.
        # Round-39 P1#5: only unmark when the attempt is
        # truly terminal. A PUSH_VERIFIED attempt owns
        # its events; the head-rebind path will mark
        # them consumed once the controller transitions
        # to AWAITING_CI.
        if (
            lease is not None
            and rec.lifecycle != LIFECYCLE_PUSH_VERIFIED
        ):
            for eid in (
                lease.get("last_dispatched_event_id") or ""
            ).split(","):
                eid = eid.strip()
                if eid:
                    try:
                        unmark_event_launched(eid)
                    except Exception:  # noqa: BLE001
                        pass
        log(
            "warning",
            "worker attempt finalized",
            attempt_id=attempt_id,
            pid=rec.pid,
            lifecycle=rec.lifecycle,
            exit_code=exit_code,
            signal=signal,
            terminal_reason=rec.terminal_reason,
        )
        return "DIED"
    # Worker still alive — bump heartbeat.
    rec.last_progress_at = now_iso()
    try:
        store.write(rec)
    except Exception:  # noqa: BLE001
        pass
    return None


def reconcile_orphaned_worker_attempts(*, work_dir=None) -> int:
    """Round-52/C20 §3, §6: reconcile WORKER_RUNNING WorkerAttemptRecords
    that the supervisor no longer has a live lease for. Idempotent.

    Each heartbeat, the supervisor's main loop calls
    ``poll_worker_attempt`` for the attempt recorded in the current
    lease. If the lease is gone but a ``WORKER_RUNNING`` record
    remains (e.g. the supervisor was restarted, the lease was
    removed by an out-of-band path, or the lease was written by a
    prior incarnation), this function scans the worker_attempts
    directory, runs the canonical C18 ingestion for each
    ``WORKER_RUNNING`` record whose pid is dead, and then drives
    the post-poll finalization chain:

      REPAIR_PUSHED
        -> verify_push_against_attempt (origin + GitHub)
        -> finalize_worker_attempt_pushed (PUSH_VERIFIED)
        -> mark_head_advanced_public (controller transition)
        -> consume_thread_drain_event_in_terminal_disposition
        -> resolveReviewThread (remote)

      NO_CHANGES_REQUIRED
        -> finalize_worker_attempt_pushed (LIFECYCLE_NO_CHANGES_REQUIRED)
        -> consume_thread_drain_event_in_terminal_disposition
        -> resolveReviewThread (remote)

      WORKER_EXECUTION_FAILED / other
        -> WORKER_EXITED_NO_PUSH (default)

    The function is idempotent: re-running it after a crash leaves
    records in their already-terminal state.

    Returns the number of records that transitioned from
    WORKER_RUNNING to a terminal lifecycle in this call.
    """
    from autocoder_orchestration.worker_attempt import (
        LIFECYCLE_WORKER_RUNNING,
        LIFECYCLE_PUSH_VERIFIED,
        LIFECYCLE_NO_CHANGES_REQUIRED,
        LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE,
        LIFECYCLE_WORKER_EXITED_NO_PUSH,
        LIFECYCLE_TERMINAL_REPAIRED,
        TERMINAL_LIFECYCLES,
        WorkerAttemptStore,
    )
    # Round-52/C20: ``work_dir`` is the orchestration state root
    # (the parent directory containing ``worker_attempts/``).
    # When None, use the supervisor's WORKER_ATTEMPTS_DIR
    # directly. The reconciliation scans WA_DIR for attempt
    # records whose lease has been lost.
    if work_dir is not None:
        _wa_candidate = Path(work_dir) / "worker_attempts"
        _dir = _wa_candidate if _wa_candidate.exists() else Path(work_dir)
    else:
        _dir = WORKER_ATTEMPTS_DIR
    if not _dir.exists():
        return 0
    transitions = 0
    # Round-52/C20: match only attempt records, NOT canonical
    # artifacts or log files. Artifact files end in
    # .worker_result.json and are written alongside the
    # attempt record.
    for _path in _dir.glob("att-*.json"):
        if _path.name.endswith(".worker_result.json"):
            continue
        if not _path.name.endswith(".json"):
            continue
        try:
            _d = json.loads(_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        _rec_lifecycle = _d.get("lifecycle")
        if _rec_lifecycle != LIFECYCLE_WORKER_RUNNING:
            continue
        if _rec_lifecycle in TERMINAL_LIFECYCLES:
            continue
        # Try to read via the store; fallback to raw json
        _attempt_id = _d.get("attempt_id") or _path.stem
        _pid = _d.get("pid")
        # Worker must be dead (process not alive).
        if _pid and pid_alive(_pid):
            continue
        # Run the canonical C18 ingestion on the on-disk
        # artifact to populate rec.extra with worker_result_artifact
        # and the produced/pushed commit SHAs.
        try:
            from autocoder_orchestration.worker_attempt import (
                WorkerAttemptRecord,
            )
            _rec_obj = WorkerAttemptRecord(
                schema_version=_d.get("schema_version", "autocoder.worker_attempt.v1"),
                attempt_id=_attempt_id,
                claim_id=_d.get("claim_id", ""),
                repo_owner=_d.get("repo_owner", ""),
                repo_name=_d.get("repo_name", ""),
                pr_number=int(_d.get("pr_number", 5)),
                event_ids=tuple(_d.get("event_ids") or ()),
                finding_ids=tuple(_d.get("finding_ids") or ()),
                directive_digest=_d.get("directive_digest", ""),
                directive_path=_d.get("directive_path", ""),
                prelaunch_head=_d.get("prelaunch_head", ""),
                expected_branch=_d.get("expected_branch", "feat/review-repair-relay-v1"),
                pid=int(_pid) if _pid else 0,
                lease_id=_d.get("lease_id", ""),
                started_at=_d.get("started_at", now_iso()),
                last_progress_at=_d.get("last_progress_at", now_iso()),
                finished_at=_d.get("finished_at"),
                lifecycle=_d.get("lifecycle", LIFECYCLE_WORKER_RUNNING),
                attempt_count=int(_d.get("attempt_count", 1)),
                stdout_path=_d.get("stdout_path"),
                stderr_path=_d.get("stderr_path"),
                exit_code=_d.get("exit_code"),
                signal=_d.get("signal"),
                result_artifact_path=_d.get("result_artifact_path"),
                produced_commit_sha=_d.get("produced_commit_sha"),
                pushed_commit_sha=_d.get("pushed_commit_sha"),
                origin_head_verified=bool(_d.get("origin_head_verified", False)),
                github_head_verified=bool(_d.get("github_head_verified", False)),
                terminal_reason=_d.get("terminal_reason"),
                extra=_d.get("extra") or {},
            )
        except Exception as _re_exc:
            log("warning", "round-52: failed to reconstruct WorkerAttemptRecord", error=str(_re_exc)[:200])
            continue
        # Set finished_at if missing
        if not _rec_obj.finished_at:
            _rec_obj.finished_at = now_iso()
        # Run the canonical C18 ingestion to ingest the artifact.
        try:
            _ingested = _round50_ingest_worker_result_artifact(_rec_obj)
        except Exception as _ie:
            _ingested = False
            log(
                "warning",
                "round-52: orphan worker artifact ingestion raised",
                attempt_id=_attempt_id,
                error=str(_ie)[:200],
            )
        # Re-load the artifact data into the rec after ingestion
        if _rec_obj.produced_commit_sha is None and _rec_obj.extra:
            _wra = _rec_obj.extra.get("worker_result_artifact") or {}
            _psha = _wra.get("produced_commit_shas") or []
            _ppusha = _wra.get("pushed_commit_shas") or []
            if _psha:
                _rec_obj.produced_commit_sha = _psha[0]
            if _ppusha:
                _rec_obj.pushed_commit_sha = _ppusha[0]
        # Classify the lifecycle based on the worker's reported result.
        _new_lifecycle = LIFECYCLE_WORKER_EXITED_NO_PUSH
        _wra = (_rec_obj.extra or {}).get("worker_result_artifact") or {}
        _result_type = _wra.get("result_type")
        # Round-52/C20 §13: if the worker's disposition is
        # INCOMPLETE_EVIDENCE, the round-39 contract requires
        # the attempt to be a no-op, the thread to remain
        # actionable, and the durable work item to be retried
        # on the next heartbeat (the worker ran out of tool
        # budget). We persist the attempt as NO_CHANGES_REQUIRED
        # so the lifecycle is terminal, but we DO NOT consume
        # the drain event or attempt remote resolution — the
        # next round will re-dispatch a fresh worker.
        if _result_type == "NO_CHANGES_REQUIRED":
            _findings = (_wra.get("no_changes_required_proof") or {}).get("findings") or []
            _all_incomplete = (
                _findings
                and all(
                    (f.get("disposition") or "").upper() == "INCOMPLETE_EVIDENCE"
                    for f in _findings
                )
            )
            if _all_incomplete:
                # Treat as STILL_ACTIONABLE: terminalize the
                # attempt (the worker really did no work) but
                # leave the drain event and thread untouched so
                # the next heartbeat re-dispatches.
                try:
                    _rec_obj.lifecycle = "WORKER_EXITED_NO_PUSH"
                    _rec_obj.terminal_reason = (
                        "round-52: worker emitted INCOMPLETE_EVIDENCE "
                        "for every finding; thread remains actionable, "
                        "no remote resolution attempted"
                    )
                    WorkerAttemptStore(_dir).write(_rec_obj)
                    _new_lifecycle = "WORKER_EXITED_NO_PUSH"
                    # Skip the post-finalization drain-consume
                    # and remote-resolution block below.
                    transitions += 1
                    log(
                        "warning",
                        "round-52: orphan worker INCOMPLETE_EVIDENCE; "
                        "drain event preserved for re-dispatch",
                        attempt_id=_attempt_id,
                        pid=_pid,
                    )
                    continue
                except Exception as _iie:
                    log(
                        "warning",
                        "round-52: INCOMPLETE_EVIDENCE persist failed",
                        attempt_id=_attempt_id,
                        error=str(_iie)[:200],
                    )
        if _result_type == "REPAIR_PUSHED" and _rec_obj.pushed_commit_sha:
            # Verify the pushed SHA is on origin/<branch>.
            _v = verify_push_against_attempt(
                attempt_id=_attempt_id,
                new_head_sha=_rec_obj.pushed_commit_sha,
            )
            if _v and _v.get("github_head_verified"):
                try:
                    finalize_worker_attempt_pushed(
                        attempt_id=_attempt_id,
                        pushed_commit_sha=_rec_obj.pushed_commit_sha,
                        produced_commit_sha=(
                            _v.get("produced_commit_sha")
                            or _rec_obj.pushed_commit_sha
                        ),
                        origin_head_verified=True,
                        github_head_verified=True,
                    )
                    # Re-read the attempt record after the
                    # finalization helper has persisted the
                    # new lifecycle and pushed/produced SHAs.
                    _after = WorkerAttemptStore(_dir).read(_attempt_id)
                    if _after is not None:
                        _rec_obj = _after
                    _new_lifecycle = LIFECYCLE_PUSH_VERIFIED
                except Exception as _fe:
                    log(
                        "warning",
                        "round-52: finalize_worker_attempt_pushed failed",
                        attempt_id=_attempt_id,
                        error=str(_fe)[:200],
                    )
                    _new_lifecycle = LIFECYCLE_WORKER_EXITED_NO_PUSH
            else:
                _new_lifecycle = LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE
                try:
                    _rec_obj.lifecycle = LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE
                    _rec_obj.terminal_reason = (
                        "round-52: worker reported pushed SHA but "
                        "origin/live do not contain it"
                    )
                except Exception:
                    pass
        elif _result_type == "NO_CHANGES_REQUIRED":
            # The worker explicitly emitted no-op proof.
            try:
                _rec_obj.lifecycle = LIFECYCLE_NO_CHANGES_REQUIRED
                _rec_obj.terminal_reason = (
                    "round-52 orphan recovery: worker emitted "
                    "structured NO_CHANGES_REQUIRED proof"
                )
                _new_lifecycle = LIFECYCLE_NO_CHANGES_REQUIRED
            except Exception as _nce:
                log(
                    "warning",
                    "round-52: NO_CHANGES_REQUIRED finalization failed",
                    attempt_id=_attempt_id,
                    error=str(_nce)[:200],
                )
        # Persist the worker attempt record AFTER all lifecycle
        # transitions so the on-disk JSON reflects PUSH_VERIFIED,
        # NO_CHANGES_REQUIRED, or UNATTRIBUTED_HEAD_ADVANCE.
        try:
            WorkerAttemptStore(_dir).write(_rec_obj)
        except Exception as _we:
            log(
                "warning",
                "round-52: orphan worker attempt write failed",
                attempt_id=_attempt_id,
                error=str(_we)[:200],
            )
        transitions += 1
        log(
            "warning",
            "round-52: orphan worker reconciled",
            attempt_id=_attempt_id,
            pid=_pid,
            ingested=_ingested,
            lifecycle=_new_lifecycle,
            result_type=_result_type,
        )
        # Post-finalization: consume the drain event, attempt
        # remote thread resolution. These are best-effort and
        # must NOT block the next iteration.
        try:
            _new_head = ""
            try:
                _new_head = (_rec_obj.pushed_commit_sha or
                             _wra.get("prelaunch_head") or
                             _rec_obj.prelaunch_head)
            except Exception:
                _new_head = _rec_obj.prelaunch_head
            # Round-53/C21 §3, §7, §8: per-finding thread
            # disposition is the canonical terminality gate.
            # Every finding carries its own disposition. The
            # attempt's overall terminality is the WORST
            # disposition across all findings: a single
            # NONTERMINAL finding leaves the thread actionable
            # even if other findings are TERMINAL. The
            # supervisor must NOT consume the drain event,
            # write a terminal proof, or call
            # resolveReviewThread for any NONTERMINAL thread.
            _candidate_thread_dispositions = []
            # For both NO_CHANGES_REQUIRED and REPAIR_PUSHED,
            # the artifact's no_changes_required_proof.findings
            # may carry per-finding dispositions. Read it
            # unconditionally. The C22 §4 contracted-thread check
            # (below) ensures the artifact's claims are
            # cross-checked against the durable work item.
            try:
                _findings = (
                    (_wra.get("no_changes_required_proof") or {}).get("findings")
                    or []
                )
            except Exception:
                _findings = []
            for _f in _findings:
                _fid = (_f or {}).get("finding_id", "")
                if _fid.startswith("thread:"):
                    _gtid = _fid[len("thread:"):]
                    _disp = str(
                        (_f or {}).get("disposition") or ""
                    ).upper()
                    _candidate_thread_dispositions.append(
                        (_gtid, _disp)
                    )
            # Default disposition for the consume/resolve cycle.
            # Per §7, REPAIR_PUSHED implies REPAIRED;
            # NO_CHANGES_REQUIRED defaults to ALREADY_SATISFIED.
            if _result_type == "REPAIR_PUSHED":
                _default_disp = THREAD_DISPOSITION_REPAIRED
            else:
                _default_disp = THREAD_DISPOSITION_ALREADY_SATISFIED
            # Round-54/C22 §2, §4: for REPAIR_PUSHED, the contracted
            # thread identity MUST come from the durable work item
            # (WorkerAttemptRecord.finding_ids) AND/OR the
            # artifact's per-finding dispositions. The worker's
            # finding_id claim is EVIDENCE, not AUTHORITY over
            # thread ownership. If both sources agree, the thread
            # identity is confirmed. If they disagree, the
            # attempt is treated as RESULT_IDENTITY_MISMATCH
            # and the terminalization is BLOCKED.
            if _result_type == "REPAIR_PUSHED":
                _contracted_tids = []
                try:
                    _fid_tids = list(
                        getattr(_rec_obj, "finding_ids", None) or ()
                    )
                    for _ctid in _fid_tids:
                        if isinstance(_ctid, str) and _ctid:
                            _contracted_tids.append(_ctid)
                except Exception as _ce:
                    log(
                        "warning",
                        "round-54: C22 could not read contracted "
                        "finding_ids; treating REPAIR_PUSHED thread "
                        "as unidentifiable",
                        attempt_id=_attempt_id,
                        error=str(_ce)[:200],
                    )
                # Cross-check the worker's artifact claims. If the
                # artifact's ncrp.findings specify a thread_id that
                # is not in the contracted set, RESULT_IDENTITY_MISMATCH.
                _worker_reported_tids = {
                    _tid for (_tid, _disp) in _candidate_thread_dispositions
                }
                if _contracted_tids:
                    _mismatch = _worker_reported_tids - set(_contracted_tids)
                    if _mismatch:
                        log(
                            "warning",
                            "round-54: C22 RESULT_IDENTITY_MISMATCH; "
                            "blocking terminalization; thread set must match contracted set",
                            attempt_id=_attempt_id,
                            mismatch=sorted(_mismatch),
                            contracted=sorted(_contracted_tids),
                            reported=sorted(_worker_reported_tids),
                        )
                        # Drop worker-reported threads not in contract.
                        _candidate_thread_dispositions = [
                            (t, d) for (t, d) in _candidate_thread_dispositions
                            if t not in _mismatch
                        ]
                    # Ensure each contracted thread is in the map.
                    for _ctid in _contracted_tids:
                        if not any(
                            t == _ctid
                            for (t, _d) in _candidate_thread_dispositions
                        ):
                            _candidate_thread_dispositions.append(
                                (_ctid, _default_disp)
                            )
            # Round-54/C22 §2, §3, §7, §15: source terminality
            # MUST durably precede remote resolution. Per
            # C22 §2, the per-thread normalized disposition
            # is built explicitly (no implicit default, no
            # unbound variable). Per C22 §3, the consume
            # helper must succeed before resolveReviewThread
            # becomes eligible; an exception in source
            # terminalization MUST NOT silently fall through
            # to remote resolution. Per C22 §15, attempt
            # terminality is separate from thread-source
            # terminality; a successful PUSH_VERIFIED with
            # an unproven source terminality is NOT eligible
            # to resolve.
            #
            # Build the per-thread terminal map: each entry is
            # (thread_id, disposition_raw, terminalized_flag).
            # The terminalized flag is False until the consume
            # helper returns success. Only terminalized threads
            # are eligible for resolveReviewThread.
            _terminal_map = []  # list[(tid, disp, terminalized)]
            _nonterminal_tids = set()
            for (_tid, _disp) in _candidate_thread_dispositions:
                if _disp in NONTERMINAL_THREAD_DISPOSITIONS:
                    _nonterminal_tids.add(_tid)
                elif _disp in TERMINAL_THREAD_DISPOSITIONS:
                    _terminal_map.append(
                        (_tid, _disp, False)
                    )
                else:
                    # Unknown disposition. Fail closed:
                    # treat as nonterminal.
                    log(
                        "warning",
                        "round-54: C22 unknown thread disposition; "
                        "treating as NONTERMINAL",
                        attempt_id=_attempt_id,
                        thread_id=_tid,
                        disposition=_disp,
                    )
                    _nonterminal_tids.add(_tid)
            if _nonterminal_tids:
                log(
                    "warning",
                    "round-54: C22 worker attempt has NONTERMINAL finding(s); "
                    "drain events for those threads preserved for re-dispatch",
                    attempt_id=_attempt_id,
                    nonterminal_tids=sorted(_nonterminal_tids),
                    terminal_thread_count=len(_terminal_map),
                )
            # Per §11, provider identity MUST come from the
            # durable work item, NOT a hardcoded "coderabbit".
            _recorded_provider = _infer_provider_from_attempt(_rec_obj)
            # Per C22 §2, §3: each consume call has its own
            # outcome tracking. A failure in the consume helper
            # leaves the thread in FINALIZATION_RETRY_PENDING
            # and blocks remote resolution. We do NOT swallow
            # the exception; we record the per-thread terminal
            # flag explicitly.
            for _idx, (_tid, _disp, _term) in enumerate(_terminal_map):
                # C14: write a terminal proof so the
                # durable-thread drain emitter stops
                # re-emitting the drain event.
                _term = False
                try:
                    consume_thread_drain_event_in_terminal_disposition(
                        thread_id=_tid,
                        event_id=f"unresolved_thread_drain:{_tid}",
                        provider=_recorded_provider,
                        evaluated_head=_new_head,
                        disposition_raw=_disp,  # per-thread normalized
                        evidence=str(_candidate_thread_dispositions)[:200],
                        worker_attempt_id=_attempt_id,
                        directive_digest=_rec_obj.directive_digest,
                        result_identity={
                            "repo": f"{_rec_obj.repo_owner}/{_rec_obj.repo_name}",
                            "pr_number": _rec_obj.pr_number,
                            "thread_id": _tid,
                            "current_live_head": _new_head,
                        },
                        thread_record={
                            "thread_id": _tid,
                            "commit_oid": _new_head,
                        },
                        extra_identity={"source": "round54_c22_orphan_recovery"},
                    )
                    # Per C22 §3: the consume helper returns
                    # the durable write status. We mark the
                    # thread terminalized ONLY on success.
                    _term = True
                except Exception as _cexc:
                    # Per C22 §3: the consume helper raised.
                    # The thread stays in FINALIZATION_RETRY_PENDING.
                    # Remote resolution MUST NOT run for this
                    # thread. We log the failure for restart
                    # recovery.
                    log(
                        "warning",
                        "round-54: C22 source terminalization raised; "
                        "thread remains FINALIZATION_RETRY_PENDING; "
                        "remote resolution BLOCKED for this thread",
                        attempt_id=_attempt_id,
                        thread_id=_tid,
                        error=type(_cexc).__name__,
                        error_msg=str(_cexc)[:200],
                    )
                # Update the terminalized flag in place.
                _terminal_map[_idx] = (_tid, _disp, _term)
            # Per C22 §2, §3, §15: resolveReviewThread runs
            # only for threads whose consume succeeded.
            # The broad except Exception: pass is removed.
            for (_tid, _disp, _term) in _terminal_map:
                if not _term:
                    # Source terminalization did not durably
                    # succeed. Block remote resolution for
                    # this thread.
                    continue
                if _new_lifecycle not in (
                    LIFECYCLE_PUSH_VERIFIED,
                    LIFECYCLE_NO_CHANGES_REQUIRED,
                ):
                    continue
                try:
                    resolveReviewThread(
                        attempt_id=_attempt_id,
                        lifecycle=_new_lifecycle,
                        thread_id=_tid,
                        head_sha=_new_head,
                    )
                except Exception as _rexc:
                    log(
                        "warning",
                        "round-54: C22 resolveReviewThread raised; "
                        "thread remains RESOLUTION_PENDING; durable retry",
                        attempt_id=_attempt_id,
                        thread_id=_tid,
                        error=type(_rexc).__name__,
                        error_msg=str(_rexc)[:200],
                    )
        except Exception as _pf_exc:
            log(
                "warning",
                "round-52: post-finalization cleanup raised",
                attempt_id=_attempt_id,
                error=str(_pf_exc)[:200],
            )
    # Round-53/C21 §8: the C20 broad retry-scan of
    # historical finalized attempts is removed. Resolution
    # now lives in a per-thread lifecycle: a terminal
    # thread work item writes RESOLUTION_PENDING once and
    # resolveReviewThread is called exactly once. Re-running
    # this loop on every heartbeat floods the durable
    # audit ledger with redundant resolve attempts and
    # bypasses the per-thread terminality gate. The
    # resolveReviewThread function is itself idempotent at
    # the GitHub layer (calling it on an already-resolved
    # thread returns immediately), so the durable lifecycle
    # is the only authoritative state.

    return transitions


def _infer_provider_from_attempt(rec) -> str:
    """Round-53/C21 §11: provider identity MUST come from the
    durable work item. Returns the provider string the supervisor
    should record when consuming drain events and resolving
    threads for this attempt. Returns the empty string when
    the durable record does not prove a provider relationship.

    Preference order (most authoritative first):
      1. WorkerAttemptRecord.finding_ids (each finding carries
         its source provider in the durable work item, in
         the form finding:PROVIDER:thread:...)
      2. The dispatch event's provider field
      3. The extra.provider / extra.source_provider fields
      4. Empty string (no claim)

    The empty-string return is the FAIL-CLOSED default. It tells
    the supervisor to skip consume-thread-drain and
    resolveReviewThread for this attempt rather than
    fabricating a provider relationship.
    """
    try:
        if hasattr(rec, "finding_ids") and rec.finding_ids:
            for fid in rec.finding_ids:
                if isinstance(fid, str) and fid.startswith("finding:"):
                    parts = fid.split(":", 2)
                    if len(parts) >= 2 and parts[1] not in ("", "unknown"):
                        return parts[1]
        extra = getattr(rec, "extra", None) or {}
        if isinstance(extra, dict):
            recorded = extra.get("provider") or extra.get("source_provider")
            if isinstance(recorded, str) and recorded.strip():
                return recorded.strip()
    except Exception:
        pass
    return ""


def _github_graphql(query: str, variables: dict, *,
                    _reload_token: bool = True) -> Optional[dict]:
    """Round-54/C22 §9, §10: in-process authenticated GraphQL
    transport. Uses the supervisor's existing
    GITHUB_TOKEN_PR_AUTODEV credential source. No shell
    lookup, no manual systemd PATH requirement, no
    subprocess invocation of `gh`. Returns the parsed JSON
    `data` field on success, None on failure. The caller
    is responsible for inspecting the response for errors.
    """
    try:
        _token = get_github_token() if _reload_token else os.environ.get("GITHUB_TOKEN_PR_AUTODEV", "")
    except Exception:
        _token = os.environ.get("GITHUB_TOKEN_PR_AUTODEV", "")
    if not _token:
        return None
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={
            "Authorization": f"Bearer {_token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            _payload = json.loads(r.read())
        if not isinstance(_payload, dict):
            return None
        if _payload.get("errors"):
            return None
        return _payload.get("data")
    except Exception:
        return None


def resolveReviewThread(*, attempt_id: str, lifecycle: str,
                       thread_id: str = "", head_sha: str = "") -> None:
    """Round-54/C22 §9, §15: resolve a GitHub review thread.
    Uses the in-process authenticated GraphQL transport.
    May be a no-op when ``thread_id`` is empty. Failures are
    logged and the thread remains RESOLUTION_PENDING for
    durable retry on a future heartbeat.
    """
    if not thread_id:
        return
    _mutation = (
        "mutation ResolveThread($id: ID!) {"
        " resolveReviewThread(input: {threadId: $id}) {"
        " clientMutationId } }"
    )
    _data = _github_graphql(_mutation, {"id": thread_id})
    if _data is not None:
        log(
            "info",
            "round-54: C22 thread resolved via in-process graphql",
            thread_id=thread_id,
            attempt_id=attempt_id,
            lifecycle=lifecycle,
        )
    else:
        log(
            "warning",
            "round-54: C22 in-process graphql resolveReviewThread failed; "
            "thread remains RESOLUTION_PENDING for durable retry",
            thread_id=thread_id,
            attempt_id=attempt_id,
            lifecycle=lifecycle,
        )


def finalize_worker_attempt_pushed(
    *,
    attempt_id: str,
    pushed_commit_sha: str,
    produced_commit_sha: str,
    origin_head_verified: bool,
    github_head_verified: bool,
) -> bool:
    """Mark an attempt as ``COMMIT_PRODUCED -> PUSH_VERIFIED``.

    Called by the supervisor when an active worker attempt is
    positively associated with a verified push. This is the
    ONLY path that authorizes ``mark_head_advanced_public`` to
    succeed.

    Returns True if the transition succeeded, False otherwise.
    """
    from autocoder_orchestration.worker_attempt import (
        LIFECYCLE_COMMIT_PRODUCED,
        LIFECYCLE_PUSH_VERIFIED,
        LIFECYCLE_RECOVERY_CHECK,
        LIFECYCLE_WORKER_EXITED_NO_PUSH,
        LIFECYCLE_WORKER_RUNNING,
    )
    store = _worker_attempt_store()
    rec = store.read(attempt_id)
    if rec is None:
        return False
    # Idempotent: if already PUSH_VERIFIED, return True.
    if rec.lifecycle == LIFECYCLE_PUSH_VERIFIED:
        return True
    # Cannot mark pushed if the worker already died without push.
    if rec.lifecycle == LIFECYCLE_WORKER_EXITED_NO_PUSH:
        return False
    # RECOVERY_CHECK can move to PUSH_VERIFIED if evidence supports it.
    target_lifecycle = LIFECYCLE_PUSH_VERIFIED
    if rec.lifecycle == LIFECYCLE_WORKER_RUNNING:
        # Two-step: WORKER_RUNNING -> COMMIT_PRODUCED -> PUSH_VERIFIED
        try:
            rec.assert_can_transition_to(LIFECYCLE_COMMIT_PRODUCED)
            rec.lifecycle = LIFECYCLE_COMMIT_PRODUCED
            store.write(rec)
        except Exception:  # noqa: BLE001
            # Already advanced; continue.
            pass
    try:
        rec.assert_can_transition_to(target_lifecycle)
    except Exception as exc:  # noqa: BLE001
        log(
            "warning",
            "finalize_worker_attempt_pushed: cannot transition",
            attempt_id=attempt_id,
            lifecycle=rec.lifecycle,
            target=target_lifecycle,
            error=str(exc),
        )
        return False
    rec.lifecycle = target_lifecycle
    rec.produced_commit_sha = produced_commit_sha or rec.produced_commit_sha
    rec.pushed_commit_sha = pushed_commit_sha or rec.pushed_commit_sha
    rec.origin_head_verified = bool(origin_head_verified)
    rec.github_head_verified = bool(github_head_verified)
    rec.last_progress_at = now_iso()
    rec.finished_at = rec.finished_at or now_iso()
    store.write(rec)
    log(
        "info",
        "worker attempt PUSH_VERIFIED",
        attempt_id=attempt_id,
        pushed_commit_sha=(pushed_commit_sha or "")[:12],
        github_head_verified=bool(github_head_verified),
    )
    return True


def find_active_worker_attempt_for_head(head_sha: str) -> Optional[dict]:
    """Return the active worker attempt whose prelaunch_head is ``head_sha``.

    Used by the head-rebind path: if an active attempt is associated
    with the rebinding head advance, the supervisor may attempt to
    call ``mark_head_advanced_public(..., attempt_id=...)``. Otherwise
    the head advance is treated as unrelated / manual and only
    AUTHORITATIVE_HEAD is rebound.
    """
    from autocoder_orchestration.worker_attempt import (
        LIFECYCLE_COMMIT_PRODUCED,
        LIFECYCLE_PUSH_VERIFIED,
        LIFECYCLE_WORKER_RUNNING,
        TERMINAL_LIFECYCLES,
    )
    store = _worker_attempt_store()
    for rec in store.list_active():
        if rec.prelaunch_head == head_sha and rec.lifecycle in (
            LIFECYCLE_WORKER_RUNNING,
            LIFECYCLE_COMMIT_PRODUCED,
            LIFECYCLE_PUSH_VERIFIED,
        ):
            return rec.to_dict()
    # Also inspect terminal-failed attempts for head provenance
    # (the supervisor may still rebind; the attempt cannot ack).
    return None


def verify_push_against_attempt(
    *, attempt_id: str, new_head_sha: str,
    store: Optional["WorkerAttemptStore"] = None,
) -> Optional[dict]:
    """Verify that ``new_head_sha`` is the commit the active worker pushed.

    Returns a dict with verification fields, or ``None`` if the
    attempt cannot be associated with the head.

    Round-42 invariant: the SOLE source of truth for worker
    commit ownership is the worker's own recorded
    ``produced_commit_sha`` / ``pushed_commit_sha`` (or
    ``extra.produced_commit_shas`` /
    ``extra.pushed_commit_shas`` for multi-commit chains).

    Round-42 / Section 8: the following are diagnostic
    only and MUST NOT be sufficient for promotion:

      - committer_date > started_at
      - origin/<branch> == new_head_sha
      - prelaunch_head != new_head_sha

    The verifier therefore:
      1. Returns ``True`` ONLY if the worker's recorded
         ``pushed_commit_sha`` (or final element of
         ``pushed_commit_shas``) equals ``new_head_sha``.
      2. Returns ``True`` ONLY if the worker's recorded
         ``produced_commit_sha`` (or final element of
         ``produced_commit_shas``) equals ``new_head_sha``
         AND the origin branch points to it AND the commit
         date is after the worker started (defence in depth).
      3. Returns ``None`` for both ``github_head_verified``
         and ``origin_head_verified`` when the worker has
         NOT recorded the commit. The head-rebind path then
         treats the advance as external and skips
         ``mark_head_advanced_public``.
    """
    from autocoder_orchestration.worker_attempt import (
        LIFECYCLE_PUSH_VERIFIED,
        LIFECYCLE_WORKER_EXITED_NO_PUSH,
        WorkerAttemptStore,
    )
    from typing import Optional  # noqa: F401
    if store is None:
        store = _worker_attempt_store()
    rec = store.read(attempt_id)
    if rec is None:
        return None
    if rec.lifecycle == LIFECYCLE_WORKER_EXITED_NO_PUSH:
        return None
    out: dict = {
        "attempt_id": attempt_id,
        "prelaunch_head": rec.prelaunch_head,
        "produced_commit_sha": rec.produced_commit_sha,
        "pushed_commit_sha": rec.pushed_commit_sha,
        "github_head_verified": False,
        "origin_head_verified": False,
    }
    # Round-42: collect worker-emitted SHAs (multi-commit
    # list fallback to single-SHA fields).
    _extra = rec.extra if isinstance(rec.extra, dict) else {}
    _pushed_list = tuple(
        str(s) for s in (_extra.get("pushed_commit_shas") or []) if s
    )
    _produced_list = tuple(
        str(s) for s in (_extra.get("produced_commit_shas") or []) if s
    )
    _worker_pushed = (
        _pushed_list[-1]
        if _pushed_list
        else str(rec.pushed_commit_sha or "").strip()
    )
    _worker_produced = (
        _produced_list[-1]
        if _produced_list
        else str(rec.produced_commit_sha or "").strip()
    )
    # Direct: the worker's recorded pushed SHA matches.
    if _worker_pushed and _worker_pushed == new_head_sha:
        out["github_head_verified"] = True
        out["origin_head_verified"] = True
        return out
    # Indirect: the worker's recorded produced SHA matches
    # AND origin/<branch> points to the same SHA. (Worker
    # created the commit; the push happened but the
    # worker did not record the push — still a positive
    # ownership signal because the producer is the
    # worker, not an external actor.)
    if _worker_produced and _worker_produced == new_head_sha:
        if rec.expected_branch:
            try:
                origin_head = subprocess.check_output(
                    ["git", "rev-parse",
                     f"origin/{rec.expected_branch}"],
                    cwd=str(REPO_DIR),  # type: ignore[name-defined]
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=10,
                ).strip()
                if origin_head == new_head_sha:
                    out["origin_head_verified"] = True
                    out["github_head_verified"] = True
                    return out
            except (subprocess.CalledProcessError,
                    subprocess.TimeoutExpired, OSError):
                pass
        return out
    # Round-42: the worker has NOT recorded the commit. The
    # head movement is unattributed. ``committer_date >
    # started_at`` alone is INSUFFICIENT (round-42 §8).
    # ``origin == new_head_sha`` alone is INSUFFICIENT.
    # ``prelaunch_head != new_head_sha`` alone is
    # INSUFFICIENT. The verifier returns ``None`` for
    # both flags; the head-rebind path treats the advance
    # as external and skips ``mark_head_advanced_public``.
    return out


def _git_committer_iso(sha: str) -> tuple[bool, Optional[Any]]:
    """Return ``(ok, parsed_datetime)`` for a commit SHA.

    Round-31 P1#6: ``verify_push_against_attempt`` uses
    this to require worker-specific proof before
    attributing a remote head to the attempt. The check
    uses ``git log -1 --format=%cI <sha>`` (committer
    date, ISO 8601 strict) so the verifier can compare
    against ``rec.started_at``.

    Returns ``(False, None)`` on any subprocess error,
    missing commit, or unparseable date so callers
    conservatively treat the head as external.
    """
    try:
        out = subprocess.check_output(
            ["git", "log", "-1", "--format=%cI", str(sha or "")],
            cwd=str(REPO_DIR),  # type: ignore[name-defined]
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        ).strip()
    except (subprocess.CalledProcessError,
            subprocess.TimeoutExpired, OSError, ValueError):
        return (False, None)
    if not out:
        return (False, None)
    parsed = parse_iso(out)
    if parsed is None:
        return (False, None)
    return (True, parsed)


def pid_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return (
                f.read()
                .decode("utf-8", errors="replace")
                .replace("\0", " ")
                .strip()
            )
    except Exception:
        return ""


def lease_alive(lease: dict) -> Optional[dict]:
    pid = lease.get("pid")
    pgid = lease.get("pgid")
    if not pid or not pgid:
        return None
    if not pid_alive(pid):
        return None
    if not pgid_alive(pgid):
        return None
    if not start_time_matches(pid, lease.get("start_time_evidence", {})):
        return None
    cmdline = pid_cmdline(pid)
    # Round-38: a worker may have been launched against a
    # resolved session id that differs from the configured
    # ``SESSION_ID`` env bootstrap (e.g. when the configured
    # session was missing and a fresh isolated session was
    # created). The lease carries the actual session id; use
    # that as the authoritative match.
    lease_session_id = str(
        lease.get("session_id") or SESSION_ID or ""  # type: ignore[name-defined]
    )
    if lease_session_id not in cmdline:
        return None
    if "hermes chat" not in cmdline:
        return None
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except Exception:
        return None
    # The worker must have been launched from the configured
    # working_checkout exactly. We compare against the resolved
    # REPO_DIR (no basename fallback) so that operators using
    # multiple checkouts under different names cannot
    # accidentally inherit each other's leases. Both sides
    # are resolved with os.path.realpath so symlinks and
    # ``..`` components do not produce a spurious mismatch.
    repo_dir = os.path.realpath(str(REPO_DIR)).rstrip("/")  # type: ignore[name-defined]
    cwd_resolved = os.path.realpath(cwd).rstrip("/")
    if cwd_resolved != repo_dir:
        return None
    lease["heartbeat_at"] = now_iso()
    return lease


def write_cooldown() -> None:
    """Persist the cooldown timestamp atomically.

    The file is plain text (an ISO timestamp) so we reuse
    ``write_json`` for the atomic rename + 0600 chmod path.
    """
    write_json(LAST_RESUME_PATH, {"ts": now_iso()})  # type: ignore[name-defined]


# Round-30: per-provider cooldown-recovery ledger. The
# production function records each recovery attempt with
# the requested-at timestamp and the cooldown window; a
# second call within the window returns ``noop`` rather
# than re-issuing the request. The ledger is durable so a
# process restart can resume the same provider recovery.
DEFAULT_PROVIDER_COOLDOWN_SECS = 600  # 10 minutes


def _provider_cooldown_ledger_path(evidence_root: Any) -> Path:
    """Canonical path to the per-provider cooldown ledger."""
    return Path(str(evidence_root)) / "provider_cooldown.json"


def recover_provider_cooldown(
    provider: str, evidence_root: Any,
) -> dict:
    """Round-30/31: production provider pause/cooldown
    recovery.

    The function persists a per-provider request ledger
    under ``<evidence_root>/provider_cooldown.json``.
    Multiple calls in succession are idempotent: a
    duplicate request within the cooldown window returns
    ``{"action": "noop", ...}`` rather than re-issuing
    the request.

    Round-31: the function MUST actually issue the
    provider recovery request through the real
    provider-request seam. The recovery action uses
    GitHub's issue-comment endpoint to request a fresh
    review from the configured provider (e.g.
    ``@coderabbitai review``). The ledger records the
    recovery-request identity + timestamp so duplicate
    spam is impossible.

    Returns a structured dict with ``action``,
    ``provider``, ``requested_at``, and ``cooldown_until``.
    """
    ledger_path = _provider_cooldown_ledger_path(evidence_root)
    now = now_iso()
    existing: dict = {}
    if ledger_path.exists():
        try:
            existing = json.loads(ledger_path.read_text()) or {}
        except (OSError, json.JSONDecodeError):
            existing = {}
    if not isinstance(existing, dict):
        existing = {}
    last = existing.get(provider) or {}
    last_ts = last.get("requested_at") or ""
    cooldown_secs = DEFAULT_PROVIDER_COOLDOWN_SECS
    if last_ts:
        # Round-31: previously, ``datetime.fromisoformat``
        # raised on the very first call because the
        # ledger was missing (``last_ts`` was empty);
        # the bug was the ``return`` short-circuit
        # leaving the ``last_dt`` name bound to None,
        # which caused a NameError on subsequent
        # ``last_dt + timedelta(...)``. The fix is to
        # parse the timestamp ONLY when ``last_ts`` is
        # present and to fall through to the
        # ``resumed`` branch on any parse error so a
        # fresh request is issued.
        try:
            from datetime import datetime, timedelta, timezone
            # The ``Z`` suffix is the canonical UTC marker;
            # ``fromisoformat`` accepts it on 3.11+.
            normalized = last_ts.replace("Z", "+00:00")
            last_dt = datetime.fromisoformat(normalized)
            now_dt = datetime.fromisoformat(
                now.replace("Z", "+00:00"),
            )
            if (now_dt - last_dt) < timedelta(seconds=cooldown_secs):
                return {
                    "action": "noop",
                    "provider": provider,
                    "requested_at": last_ts,
                    "cooldown_until": (
                        last_dt + timedelta(seconds=cooldown_secs)
                    ).isoformat(),
                }
        except Exception:  # noqa: BLE001
            # Parse error: fall through to ``resumed``.
            pass
    # Round-31: issue the provider recovery request.
    # In production this calls the real provider-request
    # seam (e.g. ``gh pr comment --body '@coderabbitai
    # review'``). In a CI / test environment the call
    # is best-effort; the ledger records the attempt.
    recovery_request_id = (
        f"recovery-{provider}-{now.replace(':', '').replace('-', '').replace('.', '').replace('+', '').replace('Z', '')}"
    )
    existing[provider] = {
        "requested_at": now,
        "cooldown_secs": cooldown_secs,
        "recovery_request_id": recovery_request_id,
        "attempt_count": int(last.get("attempt_count", 0)) + 1,
    }
    # Round-32: actually invoke the canonical
    # provider-request seam (the same one the
    # supervisor's quiet-window loop uses). This makes
    # ``recover_provider_cooldown`` an ACTUAL recovery
    # request rather than a passive ledger write. The
    # result is captured in the ledger so duplicate
    # requests within the cooldown window are
    # rejected by the early-return branch above.
    real_request_ok: Optional[bool] = None
    real_request_error: Optional[str] = None
    try:
        # The provider-request seam is the canonical
        # ``write_review_request`` / ``post_review_request``
        # path used by the quiet-window loop. We reuse
        # ``write_review_request`` which writes the
        # durable request marker; ``post_review_request``
        # performs the actual ``gh pr comment`` call.
        # Either may fail (network / auth) — the ledger
        # records the attempt either way.
        # Round-44 C12: the recovery path MUST bind the
        # request head to the live PR head, NOT to the
        # cached ``AUTHORITATIVE_HEAD`` global. The
        # in-memory ``AUTHORITATIVE_HEAD`` may be polluted
        # by test runs (the supervisor process also hosts
        # the test suite); using it here is exactly the
        # path that posted ``(current head 686c76756014)``
        # while live PR head was C11. Re-fetch live PR
        # head NOW so the recovery request honors the
        # exact-head invariant at send time. If the live
        # head cannot be fetched, refuse the recovery send
        # rather than guessing from cached state.
        head_sha_now = fetch_live_pr_head_now()
        if not head_sha_now:
            head_sha_now = (
                str(AUTHORITATIVE_HEAD)  # type: ignore[name-defined]
                if "AUTHORITATIVE_HEAD" in globals()
                else ""
            )
            log(
                "warning",
                "recover_provider_cooldown: live PR head "
                "unavailable; falling back to cached "
                "AUTHORITATIVE_HEAD. The recovery send "
                "may fail closed in post_review_request "
                "if the cached head is also stale.",
                provider=provider,
                cached_head=head_sha_now[:12],
            )
        try:
            write_review_request(  # type: ignore[name-defined]
                provider=provider,
                head_sha=head_sha_now or "unknown",
                record={
                    "actor": "recovery",
                    "requested_at": now,
                    "recovery_request_id": recovery_request_id,
                    "lifecycle": "REQUEST_INTENT",
                    "request_head": head_sha_now or "unknown",
                },
            )
        except Exception as e:  # noqa: BLE001 - defensive
            real_request_error = f"write_review_request: {e}"
        else:
            try:
                real_request_ok = bool(
                    post_review_request(  # type: ignore[name-defined]
                        provider=provider,
                        head_sha=head_sha_now or "unknown",
                    )
                )
            except Exception as e:  # noqa: BLE001 - defensive
                real_request_error = (
                    real_request_error or f"post_review_request: {e}"
                )
    except Exception:  # noqa: BLE001
        # Module not available / supervisor globals not
        # bound: best-effort no-op.
        pass
    existing[provider]["request_invoked"] = (
        real_request_ok is True
    )
    if real_request_error is not None:
        existing[provider]["request_error"] = real_request_error
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write.
        tmp_path = ledger_path.with_suffix(
            ledger_path.suffix + ".tmp",
        )
        tmp_path.write_text(
            json.dumps(existing, sort_keys=True),
        )
        tmp_path.replace(ledger_path)
    except OSError:
        log(
            "warning",
            "provider cooldown ledger persistence failed",
            provider=provider,
            error=str(ledger_path),
        )
    # Compute the cooldown-until timestamp.
    from datetime import datetime, timedelta
    try:
        now_dt = datetime.fromisoformat(
            now.replace("Z", "+00:00"),
        )
        cooldown_until_dt = now_dt + timedelta(seconds=cooldown_secs)
        cooldown_until = cooldown_until_dt.isoformat()
    except Exception:  # noqa: BLE001
        cooldown_until = now
    # Round-32: ``action`` reports the real outcome.
    # ``resumed`` requires ``request_invoked`` True;
    # otherwise we report ``recoverable_retry`` so
    # the supervisor / scheduler retries with backoff
    # rather than falsely claiming recovery.
    final_action = (
        "resumed" if existing[provider].get("request_invoked")
        else "recoverable_retry"
    )
    return {
        "action": final_action,
        "provider": provider,
        "requested_at": now,
        "cooldown_until": cooldown_until,
        "recovery_request_id": recovery_request_id,
        "request_invoked": existing[provider].get("request_invoked"),
        "request_error": real_request_error,
    }


def read_cooldown() -> Optional[str]:
    """Read the persisted cooldown timestamp.

    The file is JSON with an envelope ``{"ts": <ISO>}``;
    legacy plain-text cooldown files are still accepted.
    """
    try:
        text = LAST_RESUME_PATH.read_text().strip()  # type: ignore[name-defined]
    except FileNotFoundError:
        return None
    except Exception:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "ts" in data:
            return str(data["ts"])
    except Exception:
        pass
    # Legacy plain-text format: the file content was a bare
    # ISO timestamp.
    return text


def cooldown_active() -> bool:
    ts = read_cooldown()
    if not ts:
        return False
    try:
        last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return False
    elapsed = (
        datetime.now(timezone.utc) - last
    ).total_seconds()
    return elapsed < RESUME_COOLDOWN_SECS  # type: ignore[name-defined]


def build_resume_prompt(rs: dict, live: dict) -> str:
    # Substitute into the operator-supplied resume prompt
    # template. ``str.format`` raises KeyError for
    # unknown placeholders and ValueError for unmatched
    # ``{`` or ``}``. Catching both here lets callers
    # treat template errors like any other launch failure
    # instead of terminating the daemon.
    try:
        return RESUME_PROMPT_TEMPLATE.format(  # type: ignore[name-defined]
            pr_number=PR_NUMBER,  # type: ignore[name-defined]
            repo_owner=REPO_OWNER,  # type: ignore[name-defined]
            repo_name=REPO_NAME,  # type: ignore[name-defined]
            branch=os.environ.get(
                "AED_BRANCH", "feat/controller-run-identity-and-locking"
            ),
            head=AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
            session_id=SESSION_ID,  # type: ignore[name-defined]
        )
    except (KeyError, ValueError) as exc:
        log(
            "error",
            "resume prompt template substitution failed",
            error=str(exc),
        )
        raise


def _resolve_hermes_bin() -> Optional[str]:
    """Locate the hermes CLI binary via PATH.

    Returns the absolute path or ``None`` if the binary is
    not on PATH. Any exception (e.g. ``which`` not installed)
    is treated as "not found".
    """
    try:
        return subprocess.check_output(
            ["which", "hermes"], text=True, timeout=10
        ).strip() or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None


def _parse_github_remote_identity(remote_url: str) -> dict:
    """Parse a GitHub ``origin`` URL into canonical segments.

    Round-31 P1#8: the round-37 identity guard used a
    case-insensitive substring match against the expected
    ``{owner}/{repo}`` string. That match was unsafe — any URL
    containing the substring ``slideshow11-autodev`` (e.g.
    ``git@github.com:evil/Slideshow11-AutoDev.git``) was
    accepted even though both the owner and the repository
    name were different. A misspelled owner like
    ``slideshow12/autoDev`` also slipped past the match.

    This helper parses the URL and returns the canonical
    ``{owner, repo}`` segments for EXACT comparison. It
    handles the three common forms:

        * SSH:   ``git@github.com:<owner>/<repo>.git``
        * HTTPS: ``https://github.com/<owner>/<repo>.git``
        * HTTPS (no .git): ``https://github.com/<owner>/<repo>``

    Non-GitHub remotes (e.g. ``git@gitlab.com:foo/bar.git``)
    are accepted ONLY if the host matches the expected
    GitHub host exactly — otherwise the segments are
    empty so the identity guard refuses the launch.

    Returns a dict ``{"owner": str, "repo": str}`` with the
    canonical (case-preserving) owner and repo. Either
    field is ``""`` when the URL cannot be parsed into a
    recognized GitHub form.
    """
    out = {"owner": "", "repo": ""}
    if not isinstance(remote_url, str) or not remote_url.strip():
        return out
    raw = remote_url.strip()
    host = ""
    path = ""
    if raw.startswith("git@"):
        # ``git@github.com:owner/repo.git`` — note the colon,
        # not a slash, separates host from path.
        try:
            _rest = raw.split("@", 1)[1]
            _host, _path = _rest.split(":", 1)
        except ValueError:
            return out
        host = _host.strip()
        path = _path.strip()
    elif "://" in raw:
        # ``https://github.com/owner/repo.git``
        try:
            _scheme, _rest = raw.split("://", 1)
        except ValueError:
            return out
        _rest = _rest.lstrip("/")
        if "/" not in _rest:
            return out
        _host, _path = _rest.split("/", 1)
        host = _host.strip()
        path = _path.strip()
    else:
        # Unrecognised form (e.g. local file path or a
        # relative ref). The identity guard rejects.
        return out
    # Accept only GitHub. ``github.com`` is the canonical
    # host; enterprise deployments (``*.ghe.com``) are
    # out of scope for this round.
    if host.lower() != "github.com":
        return out
    # Strip the optional ``.git`` suffix and any trailing
    # path segments (e.g. ``/issues``); the canonical repo
    # is the first path segment after the host.
    path = path.lstrip("/")
    if not path:
        return out
    segments = [
        seg for seg in path.split("/") if seg
    ]
    if len(segments) < 2:
        return out
    owner = segments[0].strip()
    repo = segments[1].strip()
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not owner or not repo:
        return out
    out["owner"] = owner
    out["repo"] = repo
    return out


def launch_worker(rs: dict, live: dict) -> Optional[dict]:
    # Round-37 fix (repository identity guard): before any
    # worker subprocess is spawned, the supervisor MUST
    # verify the local working directory is actually the
    # repo / PR / branch it claims to be repairing. Without
    # this guard, a stale cwd from a previous round (or
    # operator drift) could lead the worker into another
    # repository entirely. The guard persists a failed
    # attempt record and returns ``None`` without spawning.
    _expected_repo = (
        f"{REPO_OWNER}/{REPO_NAME}"  # type: ignore[name-defined]
    )
    _expected_pr = int(PR_NUMBER)  # type: ignore[name-defined]
    try:
        _expected_branch = str(
            os.environ.get("AED_BRANCH") or ""  # type: ignore[name-defined]
        )
    except Exception:
        _expected_branch = ""
    # Round-37 escape hatch: the identity guard is a SAFETY
    # device, not a gate. Tests that import the supervisor
    # directly without configuring REPO_DIR / REPO_OWNER /
    # REPO_NAME globals MUST still be able to exercise the
    # downstream behavior (Popen, lease, directive bridge).
    # Setting ``AED_SKIP_IDENTITY_GUARD=1`` lets the operator
    # or test runner opt out of the guard when the
    # environment cannot satisfy it. The guard is NEVER
    # bypassed in production: the systemd supervisor never
    # sets this env var.
    import os as _os
    if _os.environ.get("AED_SKIP_IDENTITY_GUARD") == "1":
        log(
            "warning",
            "round-37 identity guard skipped (test affordance)",
            expected_repo=_expected_repo,
        )
        _skip_guard = True
    else:
        _skip_guard = False
    if not _skip_guard:
        try:
            from json import loads as _rs_json_loads
            _rs_text = str(
                Path(RUN_STATE).read_text(encoding="utf-8")  # type: ignore[name-defined]
            )
            _rs_d = _rs_json_loads(_rs_text)
            if isinstance(_rs_d, dict):
                _expected_branch = str(
                    _rs_d.get("feature_branch") or _expected_branch
                )
        except Exception:
            pass
        try:
            _remote_out = subprocess.run(  # noqa: S602
                [
                    "git",
                    "-C",
                    str(REPO_DIR),  # type: ignore[name-defined]
                    "remote",
                    "get-url",
                    "origin",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            _remote_url = _remote_out.stdout.strip()
            # Round-31 P1#8: the previous substring match was
            # unsafe — ``git@github.com:evil/Slideshow11-AutoDev.git``
            # contains the substring ``slideshow11-autodev`` even
            # though the owner and repository are BOTH different
            # from the expected identity. A misspelled or
            # hyphen-substituted owner like ``slideshow12/autodev``
            # would also slip past ``Slideshow11/AutoDev.lower()``.
            # Parse the GitHub remote URL into its canonical
            # ``{owner}/{repo}`` segments and compare them
            # EXACTLY (case-insensitive on owner, exact on repo).
            _expected_owner = (
                str(REPO_OWNER)  # type: ignore[name-defined]
            ).strip()
            _expected_repo_name = (
                str(REPO_NAME)  # type: ignore[name-defined]
            ).strip()
            _remote_identity = (
                _parse_github_remote_identity(_remote_url)
            )
            _remote_owner = (
                str(_remote_identity.get("owner") or "").strip()
            )
            _remote_repo = (
                str(_remote_identity.get("repo") or "").strip()
            )
            if (
                not _remote_owner
                or not _remote_repo
                or _remote_owner.lower() != _expected_owner.lower()
                or _remote_repo != _expected_repo_name
            ):
                log(
                    "error",
                    "round-31 identity guard rejected launch: "
                    "git remote does not match expected repo",
                    expected_repo=_expected_repo,
                    expected_owner=_expected_owner,
                    expected_repo_name=_expected_repo_name,
                    actual_remote=_remote_url,
                    parsed_remote_owner=_remote_owner,
                    parsed_remote_repo=_remote_repo,
                    expected_pr=_expected_pr,
                    expected_branch=_expected_branch,
                )
                return None
            _branch_out = subprocess.run(  # noqa: S602
                [
                    "git",
                    "-C",
                    str(REPO_DIR),  # type: ignore[name-defined]
                    "rev-parse",
                    "--abbrev-ref",
                    "HEAD",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            _actual_branch = _branch_out.stdout.strip()
            if (
                _expected_branch
                and _actual_branch != _expected_branch
            ):
                log(
                    "error",
                    "round-37 identity guard rejected launch: "
                    "current branch does not match expected",
                    expected_branch=_expected_branch,
                    actual_branch=_actual_branch,
                    expected_pr=_expected_pr,
                )
                return None
        except Exception as exc:
            # If git probes fail, refuse to launch — better to
            # log a clear diagnostic than to mutate the wrong
            # repository.
            log(
                "error",
                "round-37 identity guard rejected launch: git "
                "probe failed",
                error=str(exc),
                expected_repo=_expected_repo,
                expected_branch=_expected_branch,
            )
            return None
    # The relay (autocoder_orchestration.review_repair_relay)
    # writes a canonical directive.json to the evidence root. When
    # one is present, the supervisor uses the relay-built prompt
    # instead of the operator-supplied resume_prompt_template.
    # The bridge is a no-op when the directive is absent; the
    # existing build_resume_prompt path is preserved byte-for-byte.
    directive_prompt: Optional[str] = None
    resolved_directive = None
    expected_head = AUTHORITATIVE_HEAD  # type: ignore[name-defined]
    # Round-29 P1#20: pass the resolved evidence root so the
    # bridge finds the canonical directive written by the
    # relay. The resolved root comes from
    # ``_resolve_orchestration_evidence_root`` (relay-side)
    # or ``run_state.json``; passing it through the bridge
    # ensures the bridge searches the canonical directory
    # even when the operator did not set ``AED_EVIDENCE_ROOT``.
    evidence_root_override: Optional[str] = None
    try:
        from .relay_wiring import _resolve_orchestration_evidence_root
        try:
            evidence_root_override = _resolve_orchestration_evidence_root(
                None  # type: ignore[arg-type]
            ) or None
        except Exception:
            evidence_root_override = None
    except ImportError:
        pass
    try:
        resolved_directive = resolve_directive(
            expected_head=expected_head,
            evidence_root_override=evidence_root_override,
        )
    except DirectiveLoadFailure as exc:
        # The directive is malformed (digest mismatch,
        # head_mismatch, schema-invalid, etc.). This is a
        # CRITICAL integrity failure: the publish-side
        # artifact does not match the consume-side
        # contract. The supervisor MUST NOT silently fall
        # back to the operator-supplied resume prompt —
        # a stale or corrupt directive could push a wrong
        # repair. Log an explicit integrity error,
        # delete the corrupt directive, and return None
        # so the launch is aborted. The operator MUST
        # inspect the supervisor log and the directive
        # artifact.
        log(
            "error",
            "directive_bridge rejected directive; refusing to launch worker",
            reason=exc.reason,
            path=str(exc.path) if exc.path else "",
            severity="critical_integrity",
        )
        # Delete the corrupt directive so the next round
        # regenerates a clean one.
        if exc.path:
            try:
                Path(str(exc.path)).unlink()
                log(
                    "warning",
                    "deleted corrupt directive; relay will regenerate next round",
                    path=str(exc.path),
                )
            except OSError as unlink_exc:
                log(
                    "warning",
                    "could not delete corrupt directive",
                    path=str(exc.path),
                    error=str(unlink_exc),
                )
        return None
    if resolved_directive is not None:
        directive_prompt = resolved_directive.prompt
        log(
            "info",
            "using relay-directive prompt instead of resume_prompt_template",
            directive_path=str(resolved_directive.path),
            directive_sha256=resolved_directive.directive_sha256,
        )
    if directive_prompt is not None:
        prompt = directive_prompt
    else:
        try:
            prompt = build_resume_prompt(rs, live)
        except (KeyError, ValueError) as exc:
            # ``build_resume_prompt`` already logs and re-raises;
            # the supervisor surfaces a regular launch failure
            # instead of terminating the daemon.
            log(
                "error",
                "invalid resume_prompt_template",
                error=str(exc),
            )
            return None
    # Resolve the hermes binary: prefer AED_HERMES_BIN, then
    # the configured worker_command (whose first element is
    # the binary path), then `which hermes`.
    configured_cmd = list(WORKER_COMMAND_TEMPLATE)  # type: ignore[name-defined]
    hermes_bin = (
        os.environ.get("AED_HERMES_BIN")
        or (configured_cmd[0] if configured_cmd else None)
        or _resolve_hermes_bin()
    )
    if not hermes_bin:
        log(
            "error",
            "could not resolve hermes binary; "
            "set AED_HERMES_BIN or configure worker_command[0]",
        )
        return None
    # Round-38: the attempt_id_prefix is computed early so the
    # session-resolution helper can use a stable persist path
    # derived from the same prefix the worker-attempt record
    # will use.
    attempt_id_prefix = "att-" + now_iso().replace(":", "").replace("-", "")
    # Round-50.1 Section 5: append the worker-visible canonical
    # result contract to the prompt so the worker has every
    # identifier it needs in its actual input (not just in a
    # side file the worker never reads). The directive_sha256
    # is preferred over the directive_id UUID for the
    # directive_digest field (Section 9).
    _directive_digest_for_prompt = (
        getattr(resolved_directive, "directive_sha256", "")
        or ""
        if resolved_directive is not None
        else ""
    )
    _directive_id_for_prompt = (
        getattr(resolved_directive, "directive_id", "")
        or ""
        if resolved_directive is not None
        else ""
    )
    _directive_path_for_prompt = (
        str(getattr(resolved_directive, "path", "") or "")
        if resolved_directive is not None
        else ""
    )
    if not _directive_digest_for_prompt:
        try:
            from .directive_bridge import resolve_directive as _rd
            _rd_obj = _rd(expected_head=str(live.get("head_sha", "")))
            if _rd_obj is not None:
                _directive_digest_for_prompt = (
                    getattr(_rd_obj, "directive_sha256", "") or ""
                )
                _directive_id_for_prompt = (
                    getattr(_rd_obj, "directive_id", "") or ""
                )
                _directive_path_for_prompt = (
                    str(getattr(_rd_obj, "path", "") or "")
                )
        except Exception:
            pass
    _target_thread = ""
    for _f in (rs.get("findings") or []):
        if isinstance(_f, dict):
            _target_thread = _f.get("finding_id") or ""
            break
    prompt = _build_worker_result_contract_suffix(
        prompt_prefix=prompt,
        attempt_id_prefix=attempt_id_prefix,
        directive_digest=_directive_digest_for_prompt,
        directive_id=_directive_id_for_prompt,
        directive_path=_directive_path_for_prompt,
        target_thread=_target_thread,
        prelaunch_head=str(live.get("head_sha", "")),
    )
    # Round-40: compute the canonical pending event ids ONCE,
    # before any branch consumes them. The previous design
    # referenced a local ``_pending_event_ids`` inside the
    # WorkerAttemptRecord constructor (line ~2740) BEFORE
    # assigning it (line ~2799), causing UnboundLocalError
    # whenever the constructor path ran ahead of the
    # assignment. The fix: compute the immutable value at
    # the very top of ``launch_worker``, then reference it
    # everywhere downstream. No branch may depend on
    # accidental control-flow initialization.
    try:
        pending_event_ids = tuple(
            globals().get("_pending_launch_event_ids") or ()
        )
    except Exception:
        pending_event_ids = ()
    # Round-38: resolve the actual worker session id BEFORE
    # building the launch command. If the configured/persisted
    # session no longer exists in the hermes state store, the
    # resolution helper creates a fresh isolated session via
    # the supported ``hermes chat -q "..."`` mechanism and
    # persists it for restart resilience. The launch command
    # below then uses the resolved session id, NOT the stale
    # SESSION_ID env value. This prevents the recurring
    # ``Session not found: 20260810_023800_pr5`` worker-death
    # cycle observed in Round 37.
    _resolved_session_id = str(SESSION_ID or "")  # type: ignore[name-defined]
    _resolved_was_replaced = False
    _resolved_persist_path = (
        Path(str(STATE_DIR))  # type: ignore[name-defined]
        / "worker_sessions"
        / f"session-{attempt_id_prefix}.json"
    )
    try:
        from .worker_session import resolve_worker_session
        _session_resolution = resolve_worker_session(
            hermes_bin=hermes_bin,
            configured_session_id=_resolved_session_id,
            persist_path=_resolved_persist_path,
            attempt_id=attempt_id_prefix,
            pr_number=int(PR_NUMBER),  # type: ignore[name-defined]
            repo_owner=str(REPO_OWNER),  # type: ignore[name-defined]
            repo_name=str(REPO_NAME),  # type: ignore[name-defined]
            feature_branch=str(
                os.environ.get("AED_BRANCH") or ""
            ),
            workspace_cwd=Path(str(REPO_DIR)),  # type: ignore[name-defined]
        )
        _resolved_session_id = _session_resolution.session_id
        _resolved_was_replaced = _session_resolution.was_replaced
        log(
            "info",
            "round-38 worker session resolved",
            attempt_id=attempt_id_prefix,
            session_id=_resolved_session_id[:12] + "...",
            was_replaced=_resolved_was_replaced,
            reason=_session_resolution.reason,
        )
    except Exception as exc:
        # Round-40 invariant: a session resolution failure
        # MUST NOT strand the loop, BUT it MUST NOT fall back
        # to a session that has already been classified
        # SESSION_MISSING (the operator's configured bootstrap
        # id is a recurring source of "Session not found"
        # worker deaths).
        #
        # Behavior:
        #   * If the configured session id is already
        #     classified SESSION_MISSING in the durable
        #     registry, return None so the work is released
        #     back to the heartbeat for another dispatch cycle.
        #   * Otherwise, log the failure and continue with the
        #     configured id (the round-38 fallback path), so
        #     transient resolution failures (e.g. hermes
        #     subprocess timeout on a fresh-session create)
        #     do not permanently strand the loop. The next
        #     dispatch cycle will retry resolution.
        from .worker_session import (
            is_session_marked_missing as _is_session_marked_missing,
        )
        persist_dir = _resolved_persist_path.parent.parent
        if (
            _resolved_session_id
            and _is_session_marked_missing(
                _resolved_session_id, state_dir=persist_dir
            )
        ):
            log(
                "warning",
                "round-40 session resolution failed; configured "
                "session is in SESSION_MISSING registry; NOT "
                "launching worker",
                attempt_id=attempt_id_prefix,
                error=str(exc)[:200],
                action="release_to_retry",
            )
            return None
        log(
            "warning",
            "round-40 session resolution failed; using configured "
            "id (legacy fallback path)",
            attempt_id=attempt_id_prefix,
            error=str(exc)[:200],
        )
    # Build the launch command by substituting {prompt} and
    # {session_id} into the configured template (if any) and
    # appending the standard flags. The configured template
    # is honoured so the operator can override individual
    # flags; the post-substitution flags are appended if they
    # are not already present.
    if configured_cmd:
        try:
            cmd = [
                part.format(
                    prompt=prompt,
                    session_id=_resolved_session_id,
                )
                for part in configured_cmd
            ]
        except (KeyError, IndexError, ValueError) as exc:
            # The configured worker_command template contains
            # an unknown placeholder or unmatched brace. Log
            # the failure and return ``None`` so the launch
            # fails like any other launch failure rather than
            # terminating the daemon.
            log(
                "error",
                "invalid worker_command template",
                error=str(exc),
            )
            return None
        # The configured template provides the launch
        # arguments; do not append the standard flags if they
        # are already there.
        standard_tail = (
            "--no-restore-cwd", "--accept-hooks", "--yolo", "-Q",
        )
        if not any(t in cmd for t in standard_tail):
            cmd.extend(standard_tail)
        # If the configured command did not include the
        # hermes binary path (e.g. it was just `["hermes",
        # "chat", ...]`), substitute the resolved path.
        if cmd[0] in ("hermes", "hermes-chat"):
            cmd[0] = hermes_bin
    else:
        cmd = [
            hermes_bin,
            "chat",
            "-q",
            prompt,
            "--resume",
            _resolved_session_id,
            "--no-restore-cwd",
            "--accept-hooks",
            "--yolo",
            "-Q",
        ]
    log(
        "info",
        "launching worker in own process group",
        cmd_len=len(cmd),
    )
    # Round-36: capture worker stdout/stderr to durable log files
    # instead of DEVNULL so death mode (exit code, signal, stderr)
    # is observable after restart.
    worker_attempts_dir = Path(str(STATE_DIR)) / "worker_attempts"  # type: ignore[name-defined]
    worker_attempts_dir.mkdir(parents=True, exist_ok=True)
    # Round-51/C19: wrap the worker command in a Python wrapper
    # that captures the strict machine-readable result envelope
    # and writes the canonical WorkerResultArtifact. This
    # removes the worker's final filesystem tool-call dependency:
    # the worker only needs to emit the envelope text and the
    # wrapper handles persistence. The original hermes cmd is
    # passed after "--" as positional args.
    try:
        from .aed_worker_wrapper import (
            _resolve_wrapper_argv as _resolve_wrapper,
        )
        # directive_digest/directive_id/directive_path are
        # extracted from the resolved_directive BEFORE
        # entering the WorkerAttemptRecord construction so
        # the wrapper kwargs are populated correctly.
        _early_directive_digest = (
            getattr(resolved_directive, "directive_sha256", "")
            or ""
            if resolved_directive is not None else ""
        )
        _early_directive_id = (
            getattr(resolved_directive, "directive_id", "")
            or ""
            if resolved_directive is not None else ""
        )
        _early_directive_path = (
            str(getattr(resolved_directive, "path", "") or "")
            if resolved_directive is not None else ""
        )
        _state_dir_str = str(STATE_DIR)  # type: ignore[name-defined]
        _early_claim_id = (
            _early_directive_id
            or f"lease-{_resolved_session_id}"
        )
        _wrapper_kwargs = {
            "attempt_id": attempt_id_prefix,  # actual attempt_id filled in after Popen
            "directive_digest": _early_directive_digest,
            "directive_id": _early_directive_id,
            "directive_path": _early_directive_path,
            "claim_id": _early_claim_id,
            "prelaunch_head": str(live.get("head_sha", "")),
            "result_artifact_path": str(
                worker_attempts_dir
                / f"{attempt_id_prefix}-<PID>.worker_result.json"
            ),
            "stdout_log_path": str(
                worker_attempts_dir / f"{attempt_id_prefix}.stdout.log"
            ),
            "expected_branch": "feat/review-repair-relay-v1",
            "pr_number": int(PR_NUMBER),  # type: ignore[name-defined]
            "repo": (
                f"{REPO_OWNER}/{REPO_NAME}"  # type: ignore[name-defined]
            ),
            "cwd": str(REPO_DIR),  # type: ignore[name-defined]
        }
        # The wrapper also writes a copy under the orch dir
        # when the orch path is resolvable.
        try:
            _rs_path = Path(_state_dir_str) / "run_state.json"
            if _rs_path.is_file():
                _rs = json.loads(
                    _rs_path.read_text(encoding="utf-8")
                )
                _orch_root = _rs.get("orchestration_state_root", "")
                if _orch_root:
                    _wrapper_kwargs["orch_result_artifact_path"] = (
                        str(
                            Path(_orch_root) / "worker_attempts"
                            / f"{attempt_id_prefix}-<PID>.worker_result.json"
                        )
                    )
        except Exception:
            pass
        cmd = _resolve_wrapper(_wrapper_kwargs, cmd)
    except Exception as _we:
        log(
            "warning",
            "round-51: wrapper resolution failed; using direct launch",
            attempt_id=attempt_id_prefix,
            error=str(_we)[:200],
        )
    # attempt_id_prefix was already computed earlier (round-38)
    # so the session-resolution helper could share the prefix.
    stdout_path = worker_attempts_dir / f"{attempt_id_prefix}.stdout.log"
    stderr_path = worker_attempts_dir / f"{attempt_id_prefix}.stderr.log"
    # Round-54/C22 §12: dirty worktree pre-launch guard.
    # Inspect the shared production checkout for unexpected
    # tracked changes OR unexpected untracked paths in the
    # source tree. Runtime state (heartbeat, logs, leases,
    # worker attempts) lives outside the repository, so the
    # guard filters paths that are runtime artifacts.
    _c22_runtime_state_subdirs = (
        "autocoder_supervisor/state/",
        "autocoder_supervisor/logs/",
        ".ruff_cache/",
        "__pycache__/",
        ".pytest_cache/",
    )
    try:
        _gs = subprocess.run(
            ["git", "-C", str(REPO_DIR), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True, timeout=10,
        )
        _dirty = []
        for _line in (_gs.stdout or "").splitlines():
            if not _line.strip():
                continue
            # git status --porcelain format: XY <path>
            _path = _line[3:].strip().strip('"')
            if any(_path.startswith(s) for s in _c22_runtime_state_subdirs):
                continue
            # Allow explicit .gitignore patterns (round-52
            # operator reports) and known runtime artifacts
            if _path.startswith(".hermes_"):
                continue
            if _path.startswith("/tmp/"):
                continue
            _dirty.append(_path)
        if _dirty:
            log(
                "error",
                "round-54: C22 WORKER_LAUNCH_BLOCKED_DIRTY_TREE; "
                "aborting worker launch; production checkout has "
                "unexpected tracked changes or untracked source paths",
                attempt_id=attempt_id_prefix,
                dirty_paths=_dirty[:20],
                dirty_count=len(_dirty),
            )
            try:
                stdout_fh.close()
                stderr_fh.close()
            except Exception:
                pass
            return None
    except Exception as _ge:
        # If git status itself fails, fail closed. The
        # supervisor must NEVER launch a worker into a
        # checkout whose tree integrity it cannot verify.
        log(
            "error",
            "round-54: C22 WORKER_LAUNCH_BLOCKED_DIRTY_TREE; "
            "git status check raised; aborting worker launch",
            attempt_id=attempt_id_prefix,
            error=str(_ge)[:200],
        )
        return None
    try:
        stdout_fh = stdout_path.open("wb", buffering=0)
        stderr_fh = stderr_path.open("wb", buffering=0)
    except OSError as exc:
        log(
            "error",
            "could not open worker log files; aborting launch",
            error=str(exc),
            attempt_id=attempt_id_prefix,
        )
        return None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_DIR),  # type: ignore[name-defined]
            stdout=stdout_fh,
            stderr=stderr_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        log("error", "worker launch failed", error=str(e))
        try:
            stdout_fh.close()
            stderr_fh.close()
        except Exception:
            pass
        return None
    # Round-37 fix: subprocess.Popen duplicates the parent file
    # descriptors into the child. The child now owns them; the
    # parent MUST close its copies so repeated repair rounds
    # do not exhaust the supervisor's open-file-descriptor
    # budget. The log paths remain bound to the child's
    # descriptors via the rename below.
    try:
        stdout_fh.close()
        stderr_fh.close()
    except Exception:
        pass
    attempt_id = f"{attempt_id_prefix}-{proc.pid}"
    # Rename the log files now that we know the pid.
    final_stdout_path = worker_attempts_dir / f"{attempt_id}.stdout.log"
    final_stderr_path = worker_attempts_dir / f"{attempt_id}.stderr.log"
    try:
        if stdout_path.exists() and stdout_path != final_stdout_path:
            stdout_path.rename(final_stdout_path)
        if stderr_path.exists() and stderr_path != final_stderr_path:
            stderr_path.rename(final_stderr_path)
        stdout_path = final_stdout_path
        stderr_path = final_stderr_path
    except OSError:
        # The rename is best-effort; the attempt_id will still match.
        pass
    evidence = start_time_evidence(proc.pid)
    if "error" in evidence:
        log(
            "warning",
            "could not capture start-time evidence",
            error=evidence["error"],
        )

    # Round-36: persist a canonical WorkerAttemptRecord BEFORE
    # returning. The record is the durable causal contract that
    # connects FINDING -> WORKER -> COMMIT -> PUSH -> LIVE GITHUB
    # HEAD. mark_head_advanced_public requires a PUSH_VERIFIED
    # record before acknowledging a worker repair push.
    try:
        from autocoder_orchestration.worker_attempt import (
            LIFECYCLE_WORKER_RESULT_MISSING,
            LIFECYCLE_WORKER_RUNNING,
            WorkerAttemptRecord,
            WorkerAttemptStore,
        )
        directive_digest = ""
        directive_path = ""
        directive_id = ""
        if resolved_directive is not None:
            directive_digest = (
                getattr(resolved_directive, "directive_sha256", "")
                or ""
            )
            directive_path = str(
                getattr(resolved_directive, "path", "") or ""
            )
            directive_id = (
                getattr(resolved_directive, "directive_id", "")
                or ""
            )
        # Round-37 fix: ``RUN_STATE`` is a Path (NOT a dict),
        # so ``isinstance(RUN_STATE, dict)`` was always False
        # and every WorkerAttemptRecord was created with an
        # empty ``expected_branch``. With an empty
        # ``expected_branch`` the ``verify_push_against_attempt``
        # path skipped the ``origin/<branch>`` check entirely
        # and every successful worker push failed provenance
        # validation, leaving the controller permanently stuck
        # in REPAIRING_REVIEW_FINDINGS while the live GitHub
        # head advanced past it.
        # Read the actual run_state.json (the dict) for the
        # canonical ``feature_branch``. Fall back to the
        # ``feature_branch`` argument the relay passes, then to
        # ``BRANCH`` (the supervisor's configured default).
        expected_branch = ""
        try:
            from json import loads as _json_loads
            _run_state_text = ""
            try:
                _run_state_text = str(
                    Path(RUN_STATE).read_text(  # type: ignore[name-defined]
                        encoding="utf-8",
                    )
                )
            except (OSError, TypeError):
                _run_state_text = ""
            if _run_state_text:
                try:
                    _rs_dict = _json_loads(_run_state_text)
                except Exception:
                    _rs_dict = {}
                if isinstance(_rs_dict, dict):
                    expected_branch = str(
                        _rs_dict.get("feature_branch") or ""
                    )
            if not expected_branch:
                expected_branch = str(
                    getattr(resolved_directive, "feature_branch", "")
                    or ""
                )
            if not expected_branch:
                expected_branch = str(
                    os.environ.get("AED_BRANCH") or ""  # type: ignore[name-defined]
                )
        except Exception:
            expected_branch = str(
                os.environ.get("AED_BRANCH") or ""  # type: ignore[name-defined]
            )
        attempt = WorkerAttemptRecord(
            schema_version="autocoder.worker_attempt.v1",
            attempt_id=attempt_id,
            claim_id=(
                directive_id
                or f"lease-{_resolved_session_id}"
            ),
            repo_owner=str(REPO_OWNER),  # type: ignore[name-defined]
            repo_name=str(REPO_NAME),  # type: ignore[name-defined]
            pr_number=int(PR_NUMBER),  # type: ignore[name-defined]
            # Round-39 P1#8: populate ``event_ids`` from the
            # pending-launch slot populated by
            # ``handle_new_events``. The previous initializer
            # was always ``()`` so dead-worker recovery could
            # not unmark the real launched events.
            event_ids=tuple(pending_event_ids),
            finding_ids=(),
            directive_digest=directive_digest,
            directive_path=directive_path,
            prelaunch_head=AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
            expected_branch=expected_branch,
            pid=proc.pid,
            lease_id=attempt_id,
            started_at=now_iso(),
            last_progress_at=now_iso(),
            finished_at=None,
            lifecycle=LIFECYCLE_WORKER_RUNNING,
            attempt_count=1,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            exit_code=None,
            signal=None,
            # Round-50.1: deterministic per-attempt result
            # artifact path is recorded here so the
            # worker's standalone result is auto-ingested by
            # the per-attempt normalizer in poll_worker_attempt
            # rather than discovered by global "latest
            # result file" heuristics.
            result_artifact_path=str(
                worker_attempts_dir
                / f"{attempt_id}.worker_result.json"
            ),
            produced_commit_sha=None,
            pushed_commit_sha=None,
            origin_head_verified=False,
            github_head_verified=False,
            terminal_reason=None,
            extra={
                "cmd": cmd,
                "pgid": proc.pid,
                "supervisor_instance_id": INSTANCE_ID,  # type: ignore[name-defined]
                # Round-50.1: tell the worker the exact
                # path where it MUST write the canonical
                # WorkerResultArtifact. This makes result
                # association deterministic and removes
                # the round-50 "latest result file" heuristic.
                "expected_result_artifact_path": str(
                    worker_attempts_dir
                    / f"{attempt_id}.worker_result.json"
                ),
                "attempt_nonce": attempt_id,
            },
        )
        WorkerAttemptStore(worker_attempts_dir).write(attempt)
        log(
            "info",
            "worker attempt persisted",
            attempt_id=attempt_id,
            pid=proc.pid,
        )
    except Exception as exc:  # noqa: BLE001
        # Round-40 invariant: if WorkerAttemptRecord
        # persistence fails, the supervisor MUST NOT
        # acknowledge ownership of the worker. The
        # worker process is already running; we
        # deliberately do NOT write the lease (so a
        # subsequent heartbeat sees no phantom active
        # worker) and we terminate the spawned process
        # so it does not become a zombie that blocks
        # future dispatches. The events remain
        # actionable so the next heartbeat can retry
        # the durable dispatch.
        log(
            "error",
            "could not persist worker attempt record; "
            "launch cannot be acknowledged; terminating "
            "orphan worker",
            attempt_id=attempt_id,
            error=str(exc),
        )
        try:
            import os as _os
            _os.killpg(int(proc.pid), 15)  # SIGTERM
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        return None

    # Round-39 P1#8: persist the fresh event ids on the
    # WorkerAttemptRecord so dead-worker recovery can
    # ``unmark_event_launched`` for the real ids (not just the
    # empty tuple the production initializer used). The slot
    # is populated by ``handle_new_events`` BEFORE the launch
    # call and is cleared immediately after the WorkerAttemptRecord
    # is written to avoid leaking into the next launch. The
    # raised-leased worker is durable and the launched-event
    # ledger is the inverse of the attempt's event_ids.
    # Round-40: ``pending_event_ids`` is the immutable value
    # computed at the top of ``launch_worker``. The lease
    # below uses the SAME canonical value, NOT a fresh
    # assignment that could drift from the
    # WorkerAttemptRecord above.
    lease = {
        "supervisor_instance_id": INSTANCE_ID,  # type: ignore[name-defined]
        "run_id": f"PR-{PR_NUMBER}",  # type: ignore[name-defined]
        "pr_number": PR_NUMBER,  # type: ignore[name-defined]
        # Round-38: record the actual session id used to launch
        # this worker. ``SESSION_ID`` is only the configured
        # bootstrap; the resolved session is what was actually
        # passed to ``hermes chat --resume``. Persisting the
        # resolved value here keeps the lease authoritative
        # even after a fresh-session replacement.
        "session_id": _resolved_session_id,
        "session_id_configured": str(SESSION_ID or ""),  # type: ignore[name-defined]
        "session_id_was_replaced": _resolved_was_replaced,
        "session_name": SESSION_NAME,  # type: ignore[name-defined]
        "authoritative_head_at_launch":
            AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
        "pid": proc.pid,
        "pgid": proc.pid,
        "start_time_evidence": evidence,
        "launched_at": now_iso(),
        "heartbeat_at": now_iso(),
        "cmd": cmd,
        # Round-36: round-trip the attempt id so subsequent
        # operations (dead-worker recovery, head-rebind check)
        # can locate the canonical record on disk.
        "attempt_id": attempt_id,
        # Round-39 P1#8: round-trip the event ids so the
        # dead-worker recovery path can unmark the correct
        # launched events. The WorkerAttemptRecord is the
        # durable source of truth; the lease is the
        # secondary path used by ``poll_worker_attempt``
        # BEFORE the attempt record is finalized.
        "last_dispatched_event_id": ",".join(pending_event_ids),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }
    write_lease(lease)
    write_cooldown()
    log(
        "info", "worker launched", pid=proc.pid, pgid=proc.pid,
        attempt_id=attempt_id,
    )
    return lease


def fetch_live_pr_head_now() -> str:
    """Round-44 C12: fetch the live PR head immediately
    before any provider-request send. This is the ONLY
    authoritative source for ``head_sha`` at send time.

    The in-memory ``AUTHORITATIVE_HEAD`` global may be
    polluted by tests (test runs share the supervisor
    process), by stale ``bootstrap_env`` values, or by
    a failed head reconciliation. Re-fetching live PR
    head here establishes the canonical current head
    and prevents sending requests bound to stale
    historical SHAs.

    Returns the empty string on any failure (network,
    auth, rate-limit). Callers MUST treat an empty
    return as ``unknown`` and fail closed rather than
    guessing from cached state.
    """
    try:
        token = get_github_token() or ""
        if not token:
            return ""
        live = github_get(
            f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}",  # type: ignore[name-defined]
            token,
        )
        if isinstance(live, dict):
            head = str(live.get("head", {}).get("sha") or "").strip()
            if head:
                return head
    except Exception as exc:  # noqa: BLE001
        log(
            "warning",
            "fetch_live_pr_head_now: live PR fetch failed; "
            "refusing to send provider request without verified head",
            error=str(exc)[:200],
        )
    return ""


def mark_review_request_superseded(
    provider: str,
    stale_head: str,
    superseded_by_head: str,
    *,
    reason: str,
) -> None:
    """Round-44 C12: mark a stale ``provider__head`` request
    marker as ``SUPERSEDED`` for current qualification
    purposes. Preserves the historical file as audit
    evidence (does NOT delete it) but adds a sibling
    ``provider__head.superseded.json`` ledger entry so
    downstream qualification paths can prove the
    exact-head invariant was honored.
    """
    if not stale_head:
        return
    p = REVIEW_REQUESTS_DIR / f"{provider}__{stale_head}.superseded.json"  # type: ignore[name-defined]
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "actor": "round44_c12",
            "lifecycle": "SUPERSEDED",
            "provider": provider,
            "stale_head": stale_head,
            "superseded_by_head": superseded_by_head,
            "reason": reason,
            "superseded_at": now_iso(),
        }
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True))
        tmp.replace(p)
        log(
            "warning",
            "round-44 C12: marked stale review request SUPERSEDED",
            provider=provider,
            stale_head=stale_head[:12],
            superseded_by_head=superseded_by_head[:12],
            reason=reason,
        )
    except OSError as exc:
        log(
            "warning",
            "round-44 C12: superseded ledger write failed",
            provider=provider,
            error=str(exc)[:200],
        )


def post_review_request(provider: str, head_sha: str) -> bool:
    """Round-44 C12: send the provider review request
    ONLY after re-verifying the live PR head matches
    the requested exact head. If they disagree, refuse
    to send and mark any prior request for the stale
    head as ``SUPERSEDED`` so it cannot satisfy
    qualification for the current head.

    Lifecycle: ``REQUEST_INTENT`` (caller wrote the
    request file) → ``post_review_request`` re-checks
    live head → on match, ``REQUEST_SENT`` → caller
    transitions to ``ACKNOWLEDGED`` → ``REVIEW_COMPLETE``.
    On head mismatch the request is NOT sent and any
    prior request for the stale head is marked
    ``SUPERSEDED``.
    """
    cfg = PROVIDERS.get(provider)
    if not cfg:
        log(
            "warning",
            "post_review_request: unknown provider",
            provider=provider,
        )
        return False
    handle = cfg["trigger_handle"]
    # Round-44 C12: exact-head invariant. The request
    # head must equal the live PR head AT SEND TIME.
    # Cached ``AUTHORITATIVE_HEAD`` or any historical
    # value MUST NOT independently determine the
    # request head. Re-fetch live PR head now; if the
    # requested head disagrees, refuse to send and mark
    # the prior request file as SUPERSEDED.
    live_head = fetch_live_pr_head_now()
    if not live_head:
        log(
            "warning",
            "post_review_request: refusing to send; live PR "
            "head unavailable for exact-head verification",
            provider=provider,
            requested_head=head_sha[:12],
        )
        return False
    if head_sha != live_head:
        log(
            "warning",
            "post_review_request: refusing to send; requested "
            "head is NOT the live PR head",
            provider=provider,
            requested_head=head_sha[:12],
            live_head=live_head[:12],
        )
        # Mark any prior request for the requested head as
        # SUPERSEDED. Preserve the original file as audit
        # evidence; write a sibling ``.superseded.json``
        # so downstream qualification can recognize it.
        mark_review_request_superseded(
            provider,
            head_sha,
            live_head,
            reason=(
                "live_head_mismatch_at_send_time; "
                "qualification rebinds to live_head"
            ),
        )
        return False
    # Persist request intent bound to the EXACT live head.
    try:
        write_review_request(  # type: ignore[name-defined]
            provider=provider,
            head_sha=live_head,
            record={
                "actor": "post_review_request",
                "requested_at": now_iso(),
                "lifecycle": "REQUEST_INTENT",
                "request_head": live_head,
            },
        )
    except Exception as exc:  # noqa: BLE001
        log(
            "warning",
            "post_review_request: write_review_request failed",
            provider=provider,
            error=str(exc)[:200],
        )
        return False
    cmd = [
        "gh",
        "pr",
        "comment",
        str(PR_NUMBER),  # type: ignore[name-defined]
        "--repo",
        f"{REPO_OWNER}/{REPO_NAME}",  # type: ignore[name-defined]
        "--body",
        f"{handle}\n\n(current head {live_head[:12]})",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )
    except Exception as e:
        log("error", "gh pr comment failed", error=str(e))
        return False
    if proc.returncode != 0:
        log(
            "warning",
            "gh pr comment non-zero exit",
            stderr=proc.stderr[:300],
        )
        return False
    log(
        "info",
        "round-44 C12 posted",
        provider=provider,
        head=live_head[:12],
        handle=handle,
    )
    return True


def update_quota_last_request(provider: str) -> None:
    full_state = read_quota_state()
    sub = quota_state_for_provider(full_state, provider)
    if not sub:
        return
    sub["last_review_request_timestamp"] = now_iso()
    base = (
        QUOTA_RETRY_INITIAL_SECS  # type: ignore[name-defined]
        if sub.get("retry_count", 0) < QUOTA_BACKOFF_AFTER_RETRY_COUNT  # type: ignore[name-defined]
        else QUOTA_RETRY_BACKOFF_SECS  # type: ignore[name-defined]
    )
    sub["next_retry_timestamp"] = (
        datetime.now(timezone.utc) + timedelta(seconds=base)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_state = set_quota_state_for_provider(full_state, provider, sub)
    write_quota_state(new_state)


# ---------------------------------------------------------------------------
# Per-provider correlation and quota handling
# ---------------------------------------------------------------------------


def process_provider_quotas(live: dict) -> dict:
    statuses: dict[str, str] = {}
    for provider in PROVIDERS:
        latest = live.get(
            "latest_comments_by_provider", {}
        ).get(provider)
        body = latest.get("body") if latest else None
        if is_provider_quota_message(provider, body):
            existing = quota_state_for_provider(
                read_quota_state(), provider
            )
            if not existing:
                enter_provider_quota_pause(
                    provider,
                    reason=f"{provider} usage/quota message",
                    pending_head=AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
                )
            statuses[provider] = "paused"
        else:
            existing = quota_state_for_provider(
                read_quota_state(), provider
            )
            if existing:
                log(
                    "info",
                    f"{provider} quota cleared; resuming normal schedule",
                )
                clear_provider_quota_state(provider)
            statuses[provider] = (
                "clear" if existing else "unknown"
            )
    return statuses


def handle_paused_providers(
    live: dict, statuses: dict
) -> tuple[bool, list]:
    """Round-44 C12: post a single review-request retry
    per paused provider per heartbeat, but ONLY for the
    live PR head. Any prior request bound to a stale
    head is marked ``SUPERSEDED`` before the live-head
    request is sent.

    Lifecycle: ``REQUEST_INTENT`` (via
    ``post_review_request``) → ``REQUEST_SENT`` (the
    ``gh pr comment`` subprocess succeeded) →
    ``ACKNOWLEDGED`` (CodeRabbit ack observed on next
    snapshot) → ``REVIEW_COMPLETE``. A request bound to
    H0 becomes ``SUPERSEDED`` the moment live PR head
    advances to H1; downstream qualification paths read
    the ``.superseded.json`` ledger to reject stale
    evidence.
    """
    any_paused = False
    paused = []
    now = datetime.now(timezone.utc)
    # Round-44 C12: re-fetch the live PR head NOW so the
    # paused-handler cannot use a stale snapshot's
    # ``head_sha`` field. The exact-head invariant
    # requires the request head to equal the live PR head
    # at send time; ``live.get("head_sha")`` may be
    # several minutes stale if the snapshot is cached.
    live_pr_head = fetch_live_pr_head_now() or live.get("head_sha") or ""
    for provider in PROVIDERS:
        if statuses.get(provider) != "paused":
            continue
        any_paused = True
        paused.append(provider)
        full_state = read_quota_state()
        sub = quota_state_for_provider(full_state, provider)
        pending = sub.get("pending_review_head")
        # Round-44 C12: rebind ``pending_review_head`` to
        # the live PR head, NOT to ``AUTHORITATIVE_HEAD``.
        # ``AUTHORITATIVE_HEAD`` may be polluted by test
        # runs (the supervisor process also hosts the
        # test suite) or by stale bootstrap values; only
        # the live PR head is canonical at send time.
        target_head = live_pr_head or AUTHORITATIVE_HEAD  # type: ignore[name-defined]
        if pending != target_head:
            # Mark any prior request file for ``pending``
            # as SUPERSEDED so it cannot satisfy
            # qualification for the current head.
            if pending and pending != live_pr_head:
                mark_review_request_superseded(
                    provider,
                    pending,
                    target_head,
                    reason="pending_review_head_stale_at_paused_handler",
                )
            sub["pending_review_head"] = target_head
            new_state = set_quota_state_for_provider(
                full_state, provider, sub
            )
            write_quota_state(new_state)
            log(
                "warning",
                f"{provider}: head changed during pause; "
                "updated pending_review_head",
                old_head=(pending or "")[:12],
                new_head=target_head[:12],
            )
            continue
        next_retry = parse_iso(sub.get("next_retry_timestamp"))
        if next_retry is None or now >= next_retry:
            cfg = PROVIDERS.get(provider, {})
            reset_at = parse_iso(cfg.get("quota_reset_at"))
            if reset_at is not None and now < reset_at:
                next_check = now + timedelta(hours=24)
                sub["next_retry_timestamp"] = next_check.strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                new_state = set_quota_state_for_provider(
                    full_state, provider, sub
                )
                write_quota_state(new_state)
                log(
                    "info",
                    f"{provider}: low-frequency availability check "
                    "scheduled; no review request posted before "
                    "quota_reset_at",
                    reset_at=cfg["quota_reset_at"],
                )
                continue
            head_for_request = sub.get("pending_review_head") or target_head
            # Round-44 C12: dedupe by ``provider + head``. A
            # pending request file for the same provider +
            # head means the request was already persisted;
            # do not send a duplicate ``@coderabbitai
            # review`` for the same head.
            existing_req = read_review_request(  # type: ignore[name-defined]
                provider, head_for_request
            )
            if existing_req and existing_req.get("lifecycle") not in (
                "SUPERSEDED",
            ):
                log(
                    "info",
                    f"{provider}: review request for head already "
                    "persisted; skipping duplicate send",
                    head=head_for_request[:12],
                )
                continue
            live_head = live.get("head_sha")
            if (
                live_head == head_for_request
                and not live.get(
                    "latest_reviews_by_provider", {}
                ).get(provider)
                and not (
                    live.get(
                        "latest_comments_by_provider", {}
                    ).get(provider, {}).get("body")
                    and is_provider_walkthrough_or_complete(
                        provider,
                        live[
                            "latest_comments_by_provider"
                        ][provider]["body"],
                    )
                )
            ):
                if post_review_request(provider, head_for_request):
                    update_quota_last_request(provider)
                    log(
                        "info",
                        f"{provider}: retry posted single review "
                        "request after backoff",
                        retry_count=sub.get("retry_count", 0),
                        head=head_for_request[:12],
                    )
            else:
                log(
                    "info",
                    f"{provider}: live head or review state invalid "
                    "for new request",
                    live_head=(live_head or "")[:12],
                )
    return any_paused, paused


def collect_provider_surfaces(
    provider: str, head_sha: str, token: str
) -> dict:
    surfaces = {
        "provider": provider,
        "head_sha": head_sha,
        "reviews": [],
        "issue_comments": [],
        "review_comments": [],
        "check_runs": [],
    }
    cfg = PROVIDERS[provider]
    bot_logins = cfg["bot_logins"]
    # Round-135 P1: ``github_get`` swallows HTTP/network errors
    # and returns ``None``. Without propagating that failure,
    # ``capture_live_snapshot`` would record an empty surfaces
    # dict while leaving ``provider_surface_complete=True``,
    # letting the relay silently treat a provider outage as a
    # clean head. Track per-request failure so the snapshot
    # collector can fail-closed at the call site.
    api_failure: Optional[str] = None
    if cfg.get("use_reviews_api"):
        # Round-37: per_page=100 (was 50). GitHub caps pages at
        # 100; a 50-cap silently hides every review submitted
        # at index >50, which on PR #5 today is exactly the
        # fresh exact-head CodeRabbit CHANGES_REQUESTED.
        reviews = github_get(
            f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}/reviews"  # type: ignore[name-defined]
            f"?per_page=100",
            token,
        )
        if reviews is None:
            # Round-135 P1: propagate the outage. The snapshot
            # loop will mark ``provider_surface_complete=False``
            # and the relay will refuse to enter readiness.
            api_failure = "reviews_api_unreachable"
        if reviews:
            for r in reviews:
                if (
                    r.get("commit_id") == head_sha
                    and r.get("user", {}).get("login") in bot_logins
                ):
                    surfaces["reviews"].append({
                        "id": r["id"],
                        "submitted_at": r.get("submitted_at"),
                        "state": r.get("state"),
                        "body": (r.get("body") or "")[:500],
                    })
    per_page = 100
    seen_ids = set()
    # Round-31: real current-head binding for provider
    # issue comments. GitHub's `/issues/{N}/comments`
    # endpoint does NOT include a ``commit_id`` field;
    # stamping every historical comment with the current
    # ``head_sha`` manufactures provenance (CodeRabbit
    # concern). The canonical binding is the latest
    # formal review cycle's commit OID for the same
    # provider on the same head, recorded in a
    # durable per-head provider-review-cycle ledger.
    #
    # When the latest formal review's commit OID is the
    # current head (the common case), the comment is
    # bound to ``head_sha``. When no formal review
    # exists for the head (no review cycle active), the
    # comment is bound to ``None`` and the relay's
    # ``collect_findings`` filter (which requires a
    # commit_id matching ``snapshot.head_sha``) will
    # naturally skip it. Historical issue-comment
    # chatter therefore cannot reappear as a head-B
    # finding merely because ``capture_live_snapshot``
    # ran on B; it must be re-reported on B through a
    # current-head surface (fresh formal review, fresh
    # inline review, fresh thread) to become active.
    latest_review_commit_oid: Optional[str] = None
    for review in surfaces.get("reviews", []):
        # The formal-review filter at line 1552 already
        # bounds reviews to ``commit_id == head_sha``,
        # so the latest review in this list is the
        # canonical head-bound review identity.
        latest_review_commit_oid = head_sha
    # Round-140 P1: track the most-recent bot-authored
    # issue comment id for this provider across all pages.
    # GitHub returns ``/issues/{N}/comments`` newest-first
    # within each page; iterating ``reversed(comments)``
    # gives chronological order per page, so the LAST
    # bot-authored comment we encounter (highest ``cid``)
    # is the freshest in the current review cycle.
    #
    # Binding is computed in a SECOND pass after the
    # pagination loop completes, so only the actual
    # freshest comment carries a non-None ``commit_id``
    # / ``review_cycle``. Earlier comments are left
    # unbound (None) and the relay's filter rejects them.
    latest_provider_cid: Optional[int] = None
    for page in range(1, 6):
        comments = github_get(
            f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{PR_NUMBER}/comments"  # type: ignore[name-defined]
            f"?per_page={per_page}&page={page}",
            token,
        )
        if not comments:
            break
        for c in reversed(comments):
            cid = c.get("id")
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            if c.get("user", {}).get("login") in bot_logins:
                # Round-31: durable per-head provider-review
                # ledger. Record the cycle identity alongside
                # the comment so a head-A comment cannot
                # be replayed on a head-B capture.
                comment_commit_id = (
                    latest_review_commit_oid
                    if surfaces.get("reviews")
                    else None
                )
                # Track the freshest bot-authored comment
                # for this provider across all pages. ``cid``
                # is monotonic for issue comments, so the
                # highest one we observe wins.
                if (
                    isinstance(cid, int)
                    and (
                        latest_provider_cid is None
                        or cid > latest_provider_cid
                    )
                ):
                    latest_provider_cid = cid
                surfaces["issue_comments"].append({
                    "id": cid,
                    "user": c["user"]["login"],
                    "created_at": c.get("created_at"),
                    "body": (c.get("body") or "")[:500],
                    # Round-32 / Round-140 P1: the
                    # ``commit_id`` field on the comment
                    # is always absent from the production
                    # ``/issues/{PR}/comments`` endpoint
                    # (documented at lines 7206-7208). The
                    # previous gate required
                    # ``c.get("commit_id") == head_sha``
                    # which was unreachable. Round-140
                    # rebinds using evidence THIS endpoint
                    # DOES expose: the freshest bot-authored
                    # issue comment id for this provider
                    # (``latest_provider_cid``) AND a
                    # head-bound review cycle for this
                    # provider (``surfaces["reviews"]``
                    # non-empty).
                    #
                    # ``commit_id`` and ``review_cycle``
                    # are left ``None`` here so the
                    # second pass below can stamp ONLY the
                    # freshest comment.
                    "commit_id": None,
                    "review_cycle": None,
                })
    # Round-140 P1: second-pass binding for issue
    # comments. The pagination loop above records every
    # bot-authored comment with ``commit_id=None`` /
    # ``review_cycle=None``; this pass stamps ONLY the
    # comment whose ``id`` equals ``latest_provider_cid``
    # (the highest bot-authored cid observed, which is
    # the freshest comment in the current review cycle).
    # All other comments remain unbound, preserving the
    # round-31 invariant that head-A chatter cannot
    # reappear on head-B.
    if (
        latest_provider_cid is not None
        and surfaces.get("reviews")
    ):
        for entry in surfaces["issue_comments"]:
            if entry.get("id") == latest_provider_cid:
                entry["commit_id"] = head_sha
                entry["review_cycle"] = (
                    f"{provider}:{head_sha}:{latest_provider_cid}"
                )
                break

    if cfg.get("use_reviews_api"):
        for review in surfaces["reviews"]:
            inline = github_get(
                f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}"  # type: ignore[name-defined]
                f"/reviews/{review['id']}/comments",
                token,
            )
            if inline:
                for c in inline:
                    surfaces["review_comments"].append({
                        "id": c.get("id"),
                        "path": c.get("path"),
                        "line": c.get("line"),
                        "body": (c.get("body") or "")[:500],
                    })
    # Round-135 P1: if the canonical provider API was
    # unreachable, the collected surfaces are NOT
    # authoritative. Surface the failure to the snapshot
    # loop (which catches this exception and marks the
    # snapshot incomplete) so the relay refuses to enter
    # qualifying-readiness on a silent outage.
    if api_failure:
        raise RuntimeError(
            f"collect_provider_surfaces[{provider}] "
            f"api_failure={api_failure}; snapshot evidence "
            f"is incomplete"
        )
    return surfaces


def correlate_provider_review(
    provider: str,
    head_sha: str,
    surfaces: dict,
    request_record: dict,
    token: str = "",
) -> dict:
    result = {
        "provider": provider,
        "requested_head": head_sha,
        "covers_requested_head": False,
        "responses_after_request": 0,
        "latest_response_timestamp": None,
        "walkthrough_present": False,
        "review_present": False,
        "rate_limited": False,
        "stale": False,
    }
    if not request_record:
        return result
    request_ts = parse_iso(request_record.get("requested_at"))
    request_head = request_record.get("head_sha")
    for c in surfaces["issue_comments"]:
        ts = parse_iso(c.get("created_at"))
        # Fail closed: a missing or unparseable timestamp on
        # either side of the comparison excludes the comment
        # from response coverage. Treating a missing
        # timestamp as "within the window" defeats the
        # timestamp-based review-response coverage and
        # lets stale comments or unparseable records count
        # as responses to the request.
        if request_ts is None or ts is None:
            within_request_window = False
        else:
            within_request_window = (
                ts >= request_ts
                or (
                    abs((ts - request_ts).total_seconds()) <= 300
                    and "in progress" in (c.get("body") or "").lower()
                )
            )
        if within_request_window:
            result["responses_after_request"] += 1
            if result["latest_response_timestamp"] is None or (
                ts and parse_iso(
                    result["latest_response_timestamp"]
                ) and ts > parse_iso(
                    result["latest_response_timestamp"]
                )
            ):
                result["latest_response_timestamp"] = c.get(
                    "created_at"
                )
            body = (c.get("body") or "")
            if "reached your" in body.lower() and "usage" in body.lower():
                result["rate_limited"] = True
            if provider == "coderabbit":
                if (
                    "walkthrough" in body.lower()
                    or "review" in body.lower()
                ):
                    result["walkthrough_present"] = True
    for r in surfaces["reviews"]:
        ts = parse_iso(r.get("submitted_at"))
        # Fail closed: a missing timestamp on either side
        # excludes the review from response coverage.
        if request_ts is not None and ts is not None and ts >= request_ts:
            result["review_present"] = True
            if result["latest_response_timestamp"] is None or (
                ts and parse_iso(
                    result["latest_response_timestamp"]
                ) and ts > parse_iso(
                    result["latest_response_timestamp"]
                )
            ):
                result["latest_response_timestamp"] = r.get(
                    "submitted_at"
                )
    # Pass the actual token; the previous empty-string token
    # silently defeated the stale-head guard for any request
    # made before the supervisor had read its token. Without a
    # valid token, github_get returns None and we conservatively
    # treat the response as not stale.
    live_head = github_get(
        f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}",  # type: ignore[name-defined]
        token,
    )
    # A missing or malformed live-head response is treated
    # as STALE so the supervisor refuses to authorize a
    # review whose head it cannot confirm. The covers_
    # requested_head gate stays closed in that case. This is
    # the fail-closed behaviour that prevents a transient
    # GitHub API outage from being silently treated as
    # "no review yet".
    if live_head is None or not isinstance(live_head, dict):
        result["stale"] = True
    elif (
        live_head.get("head", {}).get("sha") != head_sha
    ):
        result["stale"] = True
    result["covers_requested_head"] = (
        not result["stale"]
        and (
            result["walkthrough_present"]
            or result["review_present"]
            or result["rate_limited"]
            or result["responses_after_request"] > 0
        )
    )
    return result


def compute_globally_paused(
    providers: dict[str, dict[str, Any]],
    paused_providers: list[str],
) -> bool:
    """Decide whether the run is globally paused.

    A run is globally paused only when every provider eligible
    for the current repair round is paused. A single paused
    optional provider must NEVER globally pause the run when
    at least one required provider is still available.

    This helper is the canonical implementation; tests should
    call it rather than re-implementing the rule.
    """
    paused_set = set(paused_providers)
    providers_required_for_current_round = [
        p for p, cfg in providers.items()
        if cfg.get("required_for_current_repair_round", False)
        and p not in paused_set
    ]
    return (
        bool(paused_providers)
        and len(providers_required_for_current_round) == 0
    )


def resume_if_eligible(rs: dict, live: dict) -> str:
    cls = (
        rs.get("round103_resume", {}).get("resume_classification") or ""
    )
    if cls in TERMINAL_CLASSIFICATIONS:
        return "stop"
    statuses = process_provider_quotas(live)
    any_paused, paused = handle_paused_providers(live, statuses)
    globally_paused = (
        any_paused
        and compute_globally_paused(PROVIDERS, paused)
    )
    if globally_paused:
        run = read_run_state()
        run.setdefault("round103_resume", {})["paused_providers"] = paused
        run["round103_resume"]["resume_classification"] = (
            "PAUSED_ALL_REVIEW_PROVIDERS"
        )
        write_json(RUN_STATE, run)  # type: ignore[name-defined]
        return "quota_paused"
    lease = read_lease()
    if lease:
        refreshed = lease_alive(lease)
        if refreshed is not None:
            write_lease(refreshed)
            return "worker_alive"
        log("info", "revoking stale lease", pid=lease.get("pid"))
        remove_lease()
    # Round-38: derive the lease session id from the most
    # recent durable record. The supervisor may have launched
    # the worker with a resolved session id that differs from
    # ``SESSION_ID`` (e.g. when the configured session was
    # missing and a fresh isolated session was created).
    lease_session_id = str(
        (lease or {}).get("session_id")
        or SESSION_ID
        or ""  # type: ignore[name-defined]
    )
    for pid_dir in os.listdir("/proc"):
        if not pid_dir.isdigit():
            continue
        pid = int(pid_dir)
        try:
            cmdline = pid_cmdline(pid)
        except Exception:
            continue
        if "hermes chat" in cmdline and lease_session_id in cmdline:
            try:
                my_pgid = os.getpgid(pid)
            except Exception:
                continue
            evidence = start_time_evidence(pid)
            new_lease = {
                "supervisor_instance_id": INSTANCE_ID,  # type: ignore[name-defined]
                "run_id": f"PR-{PR_NUMBER}",  # type: ignore[name-defined]
                "pr_number": PR_NUMBER,  # type: ignore[name-defined]
                "session_id": lease_session_id,
                "session_name": SESSION_NAME,  # type: ignore[name-defined]
                "authoritative_head_at_launch":
                    AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
                "pid": pid,
                "pgid": my_pgid,
                "start_time_evidence": evidence,
                "launched_at": now_iso(),
                "heartbeat_at": now_iso(),
                "cmd": [
                    "hermes",
                    "chat",
                    "--resume",
                    lease_session_id,
                ],
            }
            write_lease(new_lease)
            log(
                "info",
                "adopted orphan worker",
                pid=pid,
                pgid=my_pgid,
            )
            return "worker_alive"
    actionable = False
    token = get_github_token()
    request_records = list_review_requests()
    surfaces_by_provider = {}
    active_provider = None
    paused_set = set(paused)
    for provider in PROVIDERS:
        if provider in paused_set:
            continue
        req = None
        for r in request_records:
            if (
                r.get("provider") == provider
                and r.get("head_sha") == AUTHORITATIVE_HEAD  # type: ignore[name-defined]
            ):
                req = r
                break
        if not req:
            continue
        surfaces = collect_provider_surfaces(
            provider, AUTHORITATIVE_HEAD, token or ""  # type: ignore[name-defined]
        )
        surfaces_by_provider[provider] = surfaces
        corr = correlate_provider_review(
            provider, AUTHORITATIVE_HEAD, surfaces, req,  # type: ignore[name-defined]
            token=token or "",
        )
        if (
            corr["covers_requested_head"]
            and not corr["rate_limited"]
            and not corr["stale"]
        ):
            actionable = True
            active_provider = provider
            log(
                "info",
                f"{provider}: actionable review correlated to AUTH head",
                responses_after_request=corr[
                    "responses_after_request"
                ],
                walkthrough=corr["walkthrough_present"],
                review=corr["review_present"],
            )
            break
    if not actionable:
        return "skip"
    if cooldown_active():
        return "skip"
    new_lease = launch_worker(rs, live)
    if new_lease:
        return "resume"
    return "skip"


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except Exception as e:
        log("warning", "read_json failed", path=str(path), error=str(e))
        return {}


def write_json(path: Path, data: dict) -> None:
    """Atomically write JSON to ``path`` with restrictive permissions.

    The atomic write is performed by writing to a sibling
    temporary file and renaming it into place. The temp
    file is opened with mode 0o600 so it is never
    world-readable, even briefly. The parent directory is
    created with mode 0o700 if it does not yet exist.
    """
    # Track whether the parent directory pre-existed. mkdir's
    # mode argument is masked by the process umask; we rely on
    # the explicit os.chmod call below to enforce 0o700 on
    # newly-created parents only.
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not parent_existed:
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    tmp = path.with_suffix(".tmp")
    # Open the temp file with mode 0o600 before writing so
    # the file is never created with the umask-permissive
    # 0644 mode that Path.write_text would otherwise use.
    fd = os.open(
        str(tmp),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2, sort_keys=True))
    except Exception:
        # On any failure, close and remove the temp file
        # so we never leave a partially-written 0600 file
        # in the state directory.
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    os.replace(tmp, path)
    # Belt-and-braces: enforce 0600 on the renamed path
    # too. Some filesystems ignore chmod on the source of a
    # rename and only honour it on the destination.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Event tracking
# ---------------------------------------------------------------------------


def write_unconsumed_event(event: dict) -> None:
    existing = read_json(UNCONSUMED_EVENTS_PATH)  # type: ignore[name-defined]
    events = existing.get("events", [])
    seen_ids = {e.get("id") for e in events}
    eid = event.get("id")
    if eid and eid in seen_ids:
        return
    events.append(event)
    write_json(UNCONSUMED_EVENTS_PATH, {"events": events})  # type: ignore[name-defined]


def list_unconsumed_events() -> list:
    return read_json(UNCONSUMED_EVENTS_PATH).get("events", [])  # type: ignore[name-defined]


def consume_event(event_id: str) -> None:
    data = read_json(UNCONSUMED_EVENTS_PATH)  # type: ignore[name-defined]
    remaining = [
        e for e in data.get("events", []) if e.get("id") != event_id
    ]
    write_json(UNCONSUMED_EVENTS_PATH, {"events": remaining})  # type: ignore[name-defined]


def launched_event_ids() -> set:
    return set(
        read_json(STATE_DIR / "launched_events.json").get("ids", [])  # type: ignore[name-defined]
    )


def mark_event_launched(event_id: str) -> None:
    ids = launched_event_ids()
    ids.add(event_id)
    write_json(
        STATE_DIR / "launched_events.json",  # type: ignore[name-defined]
        {"ids": sorted(ids)},
    )


# Round-33 P1#1: cooldown-deferred event ledger.
# Events that arrive while ``cooldown_active()`` is True are
# persisted to ``unconsumed_events.json`` but the dispatch is
# skipped (line 4575). The quiet-window post-loop clear wipes
# pre-existing unconsumed events, which would silently lose
# these cooldown-deferred events. Track them in a SEPARATE
# cooldown-deferred ledger so the post-loop clear can preserve
# them, and the same event dispatches automatically when
# cooldown expires (exactly one ownership transition).
_COOLDOWN_DEFERRED_PATH = STATE_DIR / "cooldown_deferred_events.json"  # type: ignore[name-defined]


def _mark_cooldown_deferred(events: list) -> None:
    """Record that ``events`` were deferred during cooldown.

    Idempotent: events already marked are not re-added. The
    record is removed when the event is actually dispatched
    (see ``_consume_cooldown_deferred``).
    """
    if not events:
        return
    try:
        existing = read_json(_COOLDOWN_DEFERRED_PATH)
    except Exception:  # noqa: BLE001
        existing = {}
    deferred = list(existing.get("ids", []))
    seen = set(deferred)
    for e in events:
        eid = e.get("id") if isinstance(e, dict) else None
        if eid and eid not in seen:
            deferred.append(eid)
            seen.add(eid)
    if not deferred:
        return
    write_json(
        _COOLDOWN_DEFERRED_PATH,
        {
            "ids": deferred,
            "last_deferred_at": now_iso(),
        },
    )


def _consume_cooldown_deferred(event_ids: Iterable[str]) -> None:
    """Remove ``event_ids`` from the cooldown-deferred ledger.

    Called from ``handle_new_events`` after a successful
    dispatch so the event does not appear as still-deferred
    on later heartbeats. Exactly-one ownership: the event
    transitions PENDING -> dispatched, never twice.
    """
    if not event_ids:
        return
    try:
        existing = read_json(_COOLDOWN_DEFERRED_PATH)
    except Exception:  # noqa: BLE001
        return
    deferred = list(existing.get("ids", []))
    consume_set = set(event_ids)
    remaining = [eid for eid in deferred if eid not in consume_set]
    write_json(
        _COOLDOWN_DEFERRED_PATH,
        {
            "ids": remaining,
            "last_deferred_at": existing.get("last_deferred_at"),
        },
    )


def _cooldown_deferred_ids() -> set:
    """Return the set of event ids currently cooldown-deferred.

    Used by the quiet-window post-loop clear to PRESERVE
    cooldown-deferred events (they MUST NOT be wiped).
    """
    try:
        existing = read_json(_COOLDOWN_DEFERRED_PATH)
    except Exception:  # noqa: BLE001
        return set()
    return set(existing.get("ids", []))


def _replay_cooldown_deferred_if_any() -> None:
    """Replay events that were deferred during cooldown.

    Round-39 P1#2: when cooldown expires, this helper
    re-emits the deferred events into the unconsumed-events
    ledger so the next snapshot delta (or the explicit
    ``handle_new_events`` invocation) routes them to
    ``_invoke_relay_for_events``. The deferred ledger is
    cleared once the event payload is replayed.

    Exactly-one ownership: PENDING -> dispatched, never
    twice. An event already in ``launched_events.json``
    is considered already-dispatched and is NOT re-emitted
    (the next ``handle_new_events`` short-circuits the
    ``already`` set).
    """
    deferred_ids = _cooldown_deferred_ids()
    if not deferred_ids:
        return
    # Read the existing unconsumed ledger so we can merge
    # without losing any events that arrived concurrently.
    try:
        existing = read_json(UNCONSUMED_EVENTS_PATH)  # type: ignore[name-defined]
    except Exception:  # noqa: BLE001
        existing = {}
    existing_events = list(existing.get("events", []) or [])
    existing_ids = {e.get("id") for e in existing_events if isinstance(e, dict)}
    # Read the launched-events list so we don't re-emit an
    # event that was already dispatched (idempotency).
    launched = launched_event_ids()
    replayed: list = []
    for eid in list(deferred_ids):
        if eid in launched:
            # Already dispatched; just remove from the
            # deferred ledger so it doesn't accumulate.
            continue
        if eid in existing_ids:
            # Already in the unconsumed ledger; the next
            # ``run_iteration_v5`` will see it via the
            # snapshot delta. Drop the deferred entry.
            continue
        # Original payload is unknown (we only stored
        # ids in the deferred ledger). Synthesize a
        # minimal event dict so the relay's payload
        # classification still works; the relay's
        # ``should_invoke_relay`` consults the live
        # snapshot, not the event payload, so this
        # placeholder is sufficient.
        existing_events.append(
            {"id": eid, "kind": "replayed_cooldown_deferred"}
        )
        replayed.append(eid)
    if not replayed:
        # All deferred ids were already dispatched or
        # already in the unconsumed ledger. Drop them
        # from the deferred ledger so they don't
        # accumulate; the next iteration sees the
        # replayed events via the existing paths.
        _consume_cooldown_deferred(
            [eid for eid in deferred_ids if eid not in launched]
        )
        return
    try:
        write_json(
            UNCONSUMED_EVENTS_PATH,  # type: ignore[name-defined]
            {"events": existing_events},
        )
    except Exception as exc:  # noqa: BLE001
        log(
            "warning",
            "cooldown-deferred replay failed",
            error=str(exc),
        )
        return
    # Clear the deferred ledger for the ids we replayed.
    _consume_cooldown_deferred(replayed)
    log(
        "info",
        "cooldown-deferred events replayed",
        replayed=len(replayed),
    )


def unmark_event_launched(event_id: str) -> None:
    ids = launched_event_ids()
    if event_id in ids:
        ids.remove(event_id)
        write_json(
            STATE_DIR / "launched_events.json",  # type: ignore[name-defined]
            {"ids": sorted(ids)},
        )


# ---------------------------------------------------------------------------
# Readiness state + snapshots
# ---------------------------------------------------------------------------


def read_readiness_state() -> dict:
    return read_json(READINESS_STATE_PATH)  # type: ignore[name-defined]


def write_readiness_state(state: dict) -> None:
    write_json(READINESS_STATE_PATH, state)  # type: ignore[name-defined]


def read_snapshot(slot: str) -> dict:
    if slot == "A":
        return read_json(SNAPSHOT_A_PATH)  # type: ignore[name-defined]
    if slot == "B":
        return read_json(SNAPSHOT_B_PATH)  # type: ignore[name-defined]
    return {}


def write_snapshot(slot: str, snap: dict) -> None:
    if slot == "A":
        write_json(SNAPSHOT_A_PATH, snap)  # type: ignore[name-defined]
    elif slot == "B":
        write_json(SNAPSHOT_B_PATH, snap)  # type: ignore[name-defined]


def safe_github_get(path: str, token: str) -> Optional[Any]:
    return github_get(path, token)


def capture_live_snapshot(rs: dict, token: str) -> dict:
    snap = {
        "captured_at": now_iso(),
        "head_sha": None,
        "head_match": False,
        "formal_reviews": [],
        "review_threads": {},
        "issue_comments": [],
        "required_checks": {},
        "providers": {},
        "_provider_issue_comments": {},
        "unconsumed_event_ids": [
            e.get("id") for e in list_unconsumed_events()
        ],
        # Round-29 review: ``provider_surfaces`` MUST be
        # populated by the snapshot collector; downstream
        # consumers (relay, directive classifier) need the
        # current inline review comments per provider.
        "provider_surfaces": {},
        "review_comments": [],
    }
    pr = safe_github_get(
        f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}",  # type: ignore[name-defined]
        token,
    )
    if pr:
        snap["head_sha"] = pr.get("head", {}).get("sha")
        snap["head_match"] = (
            snap["head_sha"] == AUTHORITATIVE_HEAD  # type: ignore[name-defined]
        )
        snap["mergeable"] = pr.get("mergeable")
    revs = safe_github_get(
        # Round-37: per_page=100 (was 20). GitHub caps each page
        # at 100, and PR #5 has accumulated 60+ reviews, so the
        # previous 20-cap left capture_live_snapshot blind to
        # every review at index >20 — including the live C3
        # CodeRabbit CHANGES_REQUESTED on head 20024c8 that the
        # supervisor was supposed to handle.
        f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}/reviews?per_page=100",  # type: ignore[name-defined]
        token,
    )
    if revs:
        for r in revs:
            login = r.get("user", {}).get("login") or ""
            provider = None
            for p, cfg in PROVIDERS.items():
                if login in cfg.get("bot_logins", []):
                    provider = p
                    break
            snap["formal_reviews"].append({
                "id": r.get("id"),
                "submitted_at": r.get("submitted_at"),
                "commit_id": r.get("commit_id"),
                "provider": provider,
                "login": login,
            })
    # Round-29 review: actually call ``collect_provider_surfaces``
    # so the snapshot carries the inline review comments per
    # provider. Without this step the relay's
    # ``_collect_review_findings`` would never see file/line
    # suggestions and the directive builder would be silent
    # on actionable provider findings.
    # Round-30: when ``collect_provider_surfaces`` fails
    # the snapshot's evidence is INCOMPLETE. Persist
    # ``provider_surface_complete = False`` with the
    # failure identity so the relay evaluation can refuse
    # to enter qualifying-readiness (the readiness path
    # MUST NOT be entered with incomplete evidence).
    snap["provider_surface_complete"] = True
    snap["provider_surface_failures"] = {}
    for provider_name in PROVIDERS:
        try:
            surfaces = collect_provider_surfaces(
                provider_name,
                AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
                token or "",
            )
            snap["provider_surfaces"][provider_name] = surfaces
            for inline in (
                surfaces.get("review_comments", []) if isinstance(
                    surfaces, dict,
                ) else []
            ):
                snap["review_comments"].append(inline)
        except Exception as exc:  # noqa: BLE001 - defensive
            # Round-30: provider API failure is INCOMPLETE
            # evidence. Mark the snapshot as such so the
            # relay evaluation refuses to promote
            # readiness and routes to a recoverable retry.
            snap["provider_surface_complete"] = False
            snap["provider_surface_failures"][provider_name] = str(exc)
            log(
                "error",
                "collect_provider_surfaces failed; "
                "provider_surface_complete=false; relay will "
                "return recoverable retry, NOT readiness",
                provider=provider_name,
                error=str(exc),
            )
    all_threads: list[tuple[str, bool, bool, dict]] = []
    cursor = None
    pagination_failed: bool = False
    pagination_complete: bool = False
    for _ in range(6):
        vars_data = {
            "owner": REPO_OWNER,  # type: ignore[name-defined]
            "name": REPO_NAME,  # type: ignore[name-defined]
            "number": int(PR_NUMBER),  # type: ignore[name-defined]
        }
        if cursor:
            vars_data["cursor"] = cursor
        # Use GraphQL variables rather than concatenating
        # owner / name / number into the query document. This
        # is the GitHub-recommended pattern and prevents
        # accidental injection of operator-controlled values
        # into the GraphQL parser.
        # Round-35: include the thread's path and comment
        # body so the relay can classify actionable
        # findings. The previous query omitted ``comments``
        # and ``path`` so the thread inventory only carried
        # resolved/outdated flags, not the actual finding
        # content. That made every thread effectively
        # blank to the relay and the head looked "clean"
        # even when actionable review material existed on
        # the current head.
        query = (
            "query($owner: String!, $name: String!, "
            "$number: Int!, $cursor: String) "
            "{ repository(owner: $owner, name: $name) "
            "{ pullRequest(number: $number) "
            "{ reviewThreads(first: 100, after: $cursor) "
            "{ pageInfo { hasNextPage endCursor } "
            "nodes { id isResolved isOutdated path "
            "comments(first: 25) { nodes { "
            "body author { login } databaseId "
            "path line commit { oid } } } } } } } }"
        )
        payload = json.dumps({
            "query": query,
            "variables": vars_data,
        }).encode()
        req = urllib.request.Request(
            "https://api.github.com/graphql",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read())
        except Exception:
            # Page error. The inventory is INCOMPLETE.
            # Mark the pagination as failed so the
            # snapshot records an explicit
            # thread_pagination_failed=True flag.
            pagination_failed = True
            break
        # Guard against GraphQL errors where the root response
        # is null or a list, ``data`` is null, or any intermediate
        # value is a non-dict. Each step coerces the possibly-null
        # intermediate value to an empty dict before the next
        # ``.get``, so a partial response (errors, null repository,
        # etc.) does not crash.
        root = d if isinstance(d, dict) else {}
        # Round-116 P1: a partial GraphQL response with root-level
        # ``errors`` (e.g. rate-limit, auth failure, schema
        # mismatch) MUST mark pagination as FAILED. The previous
        # code coerced the missing ``data`` to an empty dict and
        # then walked the rest of the loop with all fields
        # falsey, which eventually tripped the
        # ``if not pinfo.get("hasNextPage")`` branch and recorded
        # ``pagination_complete=True`` with an empty thread
        # list. ``evaluate_readiness()`` would then see neither
        # ``pagination_failed`` nor any unresolved threads and
        # promote readiness on a false-clean signal. Reject the
        # page up front so the snapshot is explicitly incomplete.
        if not isinstance(d, dict) or root.get("errors"):
            pagination_failed = True
            break
        data_obj = root.get("data")
        data = data_obj if isinstance(data_obj, dict) else {}
        repo_obj = data.get("repository")
        repo = repo_obj if isinstance(repo_obj, dict) else {}
        pr_obj = repo.get("pullRequest")
        pr_gql = pr_obj if isinstance(pr_obj, dict) else {}
        threads_obj = pr_gql.get("reviewThreads")
        # Round-116 P1: ``reviewThreads`` may be missing or null
        # on a well-formed but partial response (e.g. the PR
        # was just closed, the field was deprecated, or the
        # field was excluded by an alias). If we cannot
        # confirm we received a real reviewThreads object, the
        # inventory is INCOMPLETE — do not coerce to an empty
        # dict and walk to the falsey-hasNextPage branch.
        if not isinstance(threads_obj, dict):
            pagination_failed = True
            break
        threads = threads_obj
        nodes_obj = threads.get("nodes")
        nodes = nodes_obj if isinstance(nodes_obj, list) else []
        for tn in nodes:
            if not isinstance(tn, dict):
                # Skip malformed node entries (e.g. a stray
                # string or null from a partial response).
                continue
            node_id = tn.get("id")
            if not isinstance(node_id, str):
                continue
            # Round-35: capture the path and the first
            # actionable comment body so downstream
            # collectors can classify the thread as a real
            # finding instead of treating it as a blank
            # unresolved thread.
            comments_obj = tn.get("comments")
            comments_nodes = (
                comments_obj.get("nodes", [])
                if isinstance(comments_obj, dict) else []
            )
            first_comment: dict = {}
            if comments_nodes and isinstance(comments_nodes[0], dict):
                first_comment = comments_nodes[0]
            thread_path = (
                tn.get("path")
                or first_comment.get("path")
                or ""
            )
            thread_line = first_comment.get("line") if first_comment else None
            commit_obj = first_comment.get("commit") if first_comment else None
            thread_commit_oid = (
                commit_obj.get("oid") if isinstance(commit_obj, dict) else None
            )
            thread_body = (
                first_comment.get("body") if first_comment else ""
            )
            author_obj = (
                first_comment.get("author") if first_comment else None
            )
            thread_author = (
                author_obj.get("login") if isinstance(author_obj, dict) else None
            )
            all_threads.append((
                node_id,
                bool(tn.get("isResolved")),
                bool(tn.get("isOutdated")),
                {
                    "path": thread_path,
                    "line": thread_line,
                    "body": (thread_body or "")[:500],
                    "commit_oid": thread_commit_oid,
                    "author": thread_author,
                    "comment_count": len(comments_nodes),
                },
            ))
        page_info_obj = threads.get("pageInfo")
        # Round-116 P1: a missing/null ``pageInfo`` means we did
        # NOT receive a well-formed page. Treat as pagination
        # FAILED rather than walking to the falsey-hasNextPage
        # branch (which would record ``pagination_complete=True``
        # with whatever partial nodes we happened to collect).
        if not isinstance(page_info_obj, dict):
            pagination_failed = True
            break
        pinfo = page_info_obj
        if not pinfo.get("hasNextPage"):
            pagination_complete = True
            break
        # If hasNextPage is set but endCursor is missing,
        # the inventory is incomplete: fail closed.
        if not pinfo.get("endCursor"):
            pagination_failed = True
            break
        cursor = pinfo.get("endCursor")
    snap["review_threads"] = {
        tid: {
            "resolved": r,
            "outdated": o,
            # Round-35: thread evidence is part of the
            # durable thread record. The drain logic uses
            # path/line/body to confirm current-head
            # actionable binding before dispatch.
            **evidence,
        }
        for (tid, r, o, evidence) in all_threads
    }
    # Record the pagination status. When pagination failed,
    # the readiness gate MUST treat the inventory as
    # incomplete (not "no unresolved threads").
    snap["review_threads_pagination_failed"] = pagination_failed
    snap["review_threads_pagination_complete"] = pagination_complete
    page = 1
    while page <= 5:
        ic = safe_github_get(
            f"/repos/{REPO_OWNER}/{REPO_NAME}/issues/{PR_NUMBER}/comments"  # type: ignore[name-defined]
            f"?per_page=100&page={page}",
            token,
        )
        if not ic:
            break
        for c in ic:
            snap["issue_comments"].append({
                "id": c.get("id"),
                "created_at": c.get("created_at"),
                "login": c.get("user", {}).get("login") or "",
                "body": (c.get("body") or "")[:500],
            })
        page += 1
        if len(ic) < 100:
            break
    # Build the per-provider issue-comment index so the
    # ``in_progress`` computation can scan only the comments
    # authored by each provider's bot accounts.
    snap.setdefault("_provider_issue_comments", {})  # type: ignore[arg-type]
    for provider, cfg in PROVIDERS.items():
        bot_logins = set(cfg.get("bot_logins", []))
        snap["_provider_issue_comments"][provider] = [
            c for c in snap["issue_comments"]
            if c.get("login") in bot_logins
        ]
    # Round-29 review: ``provider_surfaces`` + ``review_comments``
    # are populated ABOVE (right after the formal-reviews loop)
    # via the actual ``collect_provider_surfaces`` call. The
    # previous round's loop here was a no-op (read-only against
    # an empty ``provider_surfaces`` dict). We keep a sanity
    # check here so a future refactor that drops the upper
    # population fails loudly rather than silently emitting an
    # empty snapshot.
    if not snap.get("provider_surfaces"):
        log(
            "warning",
            "capture_live_snapshot: provider_surfaces is empty; "
            "review_comments will be empty too",
        )
    # Deduplicate review_comments by ``id`` so the relay
    # doesn't double-count when a provider surfaces the same
    # comment across multiple paths.
    seen_ids = set()
    deduped_review_comments = []
    for inline in snap.get("review_comments", []) or []:
        if isinstance(inline, dict):
            cid = inline.get("id")
        else:
            cid = None
        if cid is not None and cid in seen_ids:
            continue
        if cid is not None:
            seen_ids.add(cid)
        deduped_review_comments.append(inline)
    snap["review_comments"] = deduped_review_comments
    cr = safe_github_get(
        f"/repos/{REPO_OWNER}/{REPO_NAME}/commits/{snap['head_sha'] or ''}/check-runs",  # type: ignore[name-defined]
        token,
    )
    if cr and isinstance(cr, dict):
        for c in cr.get("check_runs", []):
            snap["required_checks"][c.get("name")] = {
                "conclusion": c.get("conclusion"),
                "status": c.get("status"),
                "run_id": (c.get("html_url") or ""),
            }
    quota = read_quota_state().get("providers", {}) or {}
    for p in PROVIDERS:
        recent_review_ts = max(
            [
                r.get("submitted_at") or ""
                for r in snap["formal_reviews"]
                if r.get("provider") == p
            ],
            default=None,
        )
        latest_comment_id = max(
            [
                c.get("id", 0)
                for c in snap["issue_comments"]
                if c.get("login")
                in PROVIDERS[p].get("bot_logins", [])
            ],
            default=None,
        )
        # Terminal review evidence (a formal review record
        # or an issue comment with a completed-walkthrough
        # marker) overrides any stale "in progress" comment
        # that may be hanging around from an earlier round.
        recent_review_ts_str = recent_review_ts or ""
        latest_comment_id_int = latest_comment_id or 0
        # Find the latest issue comment authored by this
        # provider. If it predates the most recent formal
        # review, the provider is in a terminal state and
        # any "in progress" comment is stale.
        latest_comment_ts = max(
            (
                c.get("created_at") or ""
                for c in (
                    snap.get("_provider_issue_comments", {}).get(p, [])
                )
            ),
            default="",
        )
        # Only consider "in progress" comments newer than
        # the most recent formal review (terminal evidence
        # overrides).
        def _ts(s: str) -> str:
            return s or ""
        in_progress = any(
            "in progress" in (
                c.get("body", "").lower()
                if isinstance(c, dict) else ""
            )
            and (
                recent_review_ts_str is None
                or _ts(c.get("created_at") or "")
                > recent_review_ts_str
            )
            for c in (
                snap.get("_provider_issue_comments", {}).get(p, [])
            )
        )
        # An explicit "completed" or "review complete"
        # comment after the latest review record forces
        # in_progress = False (terminal evidence overrides
        # any leftover "in progress" wording).
        if in_progress and any(
            (
                "review completed" in (
                    c.get("body", "").lower()
                    if isinstance(c, dict) else ""
                )
                or "<!-- this is an auto-generated comment: review"
                in (
                    c.get("body", "").lower()
                    if isinstance(c, dict) else ""
                )
            )
            and _ts(c.get("created_at") or "") >= recent_review_ts_str
            for c in (
                snap.get("_provider_issue_comments", {}).get(p, [])
            )
        ):
            in_progress = False
        snap["providers"][p] = {
            "paused": bool(quota.get(p)),
            "in_progress": in_progress,
            "latest_review_ts": recent_review_ts,
            "latest_comment_id": latest_comment_id,
        }
    return snap


def snapshot_differs(a: dict, b: dict, expected_head: str) -> list:
    """Return the list of reasons two snapshots differ.

    Round-39 semantic fix (Section 6):

        A head_sha_drift reset MUST be edge-triggered by an
        ACTUAL new head movement between snapshots A and B,
        not by the perpetual condition that a stored snapshot
        was taken before the supervisor rebounded to the
        current authoritative head.

        Concretely:
          H0 -> verified worker pushes H1 -> AUTHORITATIVE_HEAD rebinds H1
                -> snap A captured at H1, snap B captured at H1
                -> a.head_sha == b.head_sha == H1 == expected_head
                -> no drift; window stable.
            vs
          H0 -> snap A captured at H0, snap B captured at H1
                -> a.head_sha != b.head_sha
                -> real drift; window reset.

        The old comparison ``a.head_sha != expected_head`` falsely
        reported drift forever after every rebind until the next
        snap_a capture at the new head. After hundreds of polls,
        the quiet window never elapsed and AutoDev could not
        qualify.

    Returns:
        list of reason strings; empty list means snapshots match.
    """
    reasons: list[str] = []
    if not a or not b:
        return ["snapshot_empty"]
    a_head = a.get("head_sha")
    b_head = b.get("head_sha")
    # Round-39 edge-triggered head_sha_drift (Section 6):
    # Report drift ONLY when an actual head movement is observed
    # between snapshots A and B. The previous implementation
    # compared each snapshot against ``expected_head`` and reported
    # drift whenever either snapshot was taken before a supervisor
    # rebind. After the rebind, both snapshots would carry the
    # new head on subsequent polls, but until then the window
    # reset forever. The new rule is:
    #   drift = "the head moved between snapshots" (edge-triggered).
    # The historical-state mismatch (snap_a taken before rebind,
    # expected_head already on the new head) is captured by
    # ``head_match`` in ``capture_live_snapshot`` and surfaced
    # via ``revoke_readiness``; it is NOT a quiet-window reset
    # cause.
    if a_head and b_head and a_head != b_head:
        reasons.append("head_sha_drift")
    if a.get("formal_reviews") != b.get("formal_reviews"):
        reasons.append("formal_review_change")
    a_ids = {c.get("id") for c in a.get("issue_comments", [])}
    b_ids = {c.get("id") for c in b.get("issue_comments", [])}
    if a_ids != b_ids:
        reasons.append("issue_comment_change")
    if a.get("review_threads") != b.get("review_threads"):
        reasons.append("thread_state_change")
    if a.get("required_checks") != b.get("required_checks"):
        reasons.append("check_conclusion_change")
    if a.get("providers") != b.get("providers"):
        reasons.append("provider_state_change")
    if a.get("unconsumed_event_ids") != b.get(
        "unconsumed_event_ids"
    ):
        reasons.append("unconsumed_event_change")
    return reasons


def threads_block_readiness(snap: dict) -> list:
    blockers = []
    for tid, s in snap.get("review_threads", {}).items():
        if (not s.get("resolved")) and (not s.get("outdated")):
            blockers.append({"thread_id": tid, **s})
    return blockers


def required_checks_green(snap: dict) -> bool:
    """All required GitHub checks for the PR must have a SUCCESS
    or SKIPPED conclusion.

    A check that has not been registered yet (the entry is
    absent from the snapshot) is treated as NOT green. A
    check whose status is queued/in_progress is also NOT
    green. The only green states are ``success``, ``skipped``,
    ``neutral``.

    Round-39 invariant: this function returns ``True`` ONLY
    when at least one required check exists AND every
    required check is green. An empty required-check set
    returns ``False`` here so callers must consult
    ``ci_policy_status`` to distinguish the
    ``NO_REQUIRED_CHECKS`` case from the
    ``CHECKS_GREEN`` case. The legacy behavior of
    interpreting zero required checks as "green" is a
    silent CI-bypass regression; round-39 forbids it.
    """
    required = set(POLICY.get("required_check_names") or [])
    if not required:
        return False
    for name in required:
        info = snap.get("required_checks", {}).get(name)
        # An absent entry means the check has not yet been
        # registered; fail closed rather than skipping.
        if info is None:
            return False
        status = info.get("status")
        # GitHub check-run status must be exactly "completed".
        # The other "success", "skipped", "neutral" tokens are
        # check-run CONCLUSION values, not status values.
        if status != "completed":
            return False
        c = info.get("conclusion")
        # The conclusion may be success / skipped / neutral.
        if c not in ("success", "skipped", "neutral"):
            return False
    return True


# Round-39 CI policy outcomes. Distinguishes:
#   NO_REQUIRED_CHECKS  — repo has zero required checks
#                          configured; qualification can
#                          proceed with explicit semantic
#                          evidence.
#   CHECKS_GREEN         — at least one required check
#                          exists and all are green.
#   CHECKS_PENDING       — at least one required check is
#                          registered but not yet completed.
#   CHECKS_FAILED        — at least one required check
#                          completed with failure.
#   POLICY_UNRESOLVED    — the supervisor cannot determine
#                          the policy (orchestration root
#                          unresolved, run_state.json corrupt,
#                          etc.). Fail closed.
CI_POLICY_NO_REQUIRED_CHECKS = "NO_REQUIRED_CHECKS"
CI_POLICY_CHECKS_GREEN = "CHECKS_GREEN"
CI_POLICY_CHECKS_PENDING = "CHECKS_PENDING"
CI_POLICY_CHECKS_FAILED = "CHECKS_FAILED"
CI_POLICY_POLICY_UNRESOLVED = "POLICY_UNRESOLVED"


def ci_policy_status(snap: dict) -> str:
    """Resolve the canonical CI policy outcome for the live head.

    Round-39 (Section 8): distinguish the
    ``NO_REQUIRED_CHECKS`` case from ``CHECKS_GREEN``. The
    legacy behavior of treating an empty required-check
    list as "green" was a silent CI-bypass regression;
    callers must consult this function for the explicit
    semantic outcome.
    """
    required = list(POLICY.get("required_check_names") or [])
    if not required:
        return CI_POLICY_NO_REQUIRED_CHECKS
    # The check_run evidence must actually be present in
    # the snapshot. An empty ``required_checks`` dict with
    # a non-empty required list means "checks configured
    # but none have run yet"; that's POLICY_UNRESOLVED
    # until authoritative evidence arrives.
    rc = snap.get("required_checks") or {}
    if not rc:
        return CI_POLICY_POLICY_UNRESOLVED
    pending = False
    missing = False
    for name in required:
        info = rc.get(name)
        if info is None:
            # Configured check hasn't been registered yet;
            # we lack authoritative evidence about its
            # conclusion. Mark the run POLICY_UNRESOLVED so
            # the caller cannot fabricate ``CHECKS_GREEN``
            # without seeing real evidence.
            missing = True
            continue
        status = info.get("status")
        if status != "completed":
            pending = True
            continue
        c = info.get("conclusion")
        if c in ("failure", "timed_out", "cancelled", "action_required"):
            return CI_POLICY_CHECKS_FAILED
    if missing:
        return CI_POLICY_POLICY_UNRESOLVED
    if pending:
        return CI_POLICY_CHECKS_PENDING
    return CI_POLICY_CHECKS_GREEN


def any_required_provider_in_progress(snap: dict) -> bool:
    for p in POLICY["required_review_providers_for_pr_416"]:
        info = snap.get("providers", {}).get(p, {})
        if info.get("in_progress"):
            return True
    return False


def evaluate_readiness(
    snap: dict, head: Optional[str] = None,
) -> dict:
    h = head or AUTHORITATIVE_HEAD  # type: ignore[name-defined]
    if not snap or snap.get("head_sha") != h:
        return {"ready": False, "reason": "head_mismatch"}
    # Fail-closed: if the review-threads pagination failed
    # (page error, missing endCursor, or >10 pages), the
    # thread inventory is INCOMPLETE. The readiness gate
    # MUST treat incomplete as "not ready". An empty
    # review_threads dict from a failed pagination is a
    # false-clean signal: the gate MUST NOT promote
    # readiness on it.
    if snap.get("review_threads_pagination_failed"):
        return {
            "ready": False,
            "reason": "thread_pagination_failed",
            "pagination_complete": snap.get(
                "review_threads_pagination_complete"
            ),
        }
    # Round-117 P1: fail closed when provider surface
    # collection did not complete. ``capture_live_snapshot``
    # sets ``provider_surface_complete = False`` whenever
    # ``collect_provider_surfaces`` raises for any
    # required provider, but the readiness gate must NOT
    # treat an incomplete-evidence snapshot as ready.
    # Without this check, two stable incomplete snapshots
    # inside the quiet-window polling would otherwise
    # promote readiness despite the provider evidence
    # being untrustworthy.
    if snap.get("provider_surface_complete") is False:
        return {
            "ready": False,
            "reason": "provider_surface_incomplete",
            "provider_surface_failures": snap.get(
                "provider_surface_failures", {}
            ),
        }
    blockers = threads_block_readiness(snap)
    if blockers:
        return {
            "ready": False,
            "reason": "unresolved_threads",
            "blockers": blockers,
        }
    if list_unconsumed_events():
        return {"ready": False, "reason": "unconsumed_events"}
    # Round-39: distinguish CI policy outcomes so a repo
    # with zero required checks does not silently fabricate
    # "checks green" semantics. The empty-required-checks
    # case is an explicit semantic state (``NO_REQUIRED_CHECKS``)
    # that allows qualification.
    ci_state = ci_policy_status(snap)
    if ci_state == CI_POLICY_CHECKS_PENDING:
        return {
            "ready": False,
            "reason": "ci_checks_pending",
        }
    if ci_state == CI_POLICY_CHECKS_FAILED:
        return {
            "ready": False,
            "reason": "ci_checks_failed",
        }
    if ci_state == CI_POLICY_POLICY_UNRESOLVED:
        return {
            "ready": False,
            "reason": "ci_policy_unresolved",
        }
    # CI_POLICY_CHECKS_GREEN and CI_POLICY_NO_REQUIRED_CHECKS
    # both allow qualification to proceed (with explicit
    # evidence for the NO_REQUIRED_CHECKS case).
    if any_required_provider_in_progress(snap):
        return {
            "ready": False,
            "reason": "required_provider_in_progress",
        }
    if ci_state == CI_POLICY_NO_REQUIRED_CHECKS:
        return {
            "ready": True,
            "reason": "no_required_checks",
            "ci_policy": ci_state,
        }
    return {"ready": True, "reason": "quiet_window_match"}


def detect_new_actionable_events(
    prev_snap: dict, new_snap: dict
) -> list:
    events = []
    if not prev_snap:
        return events
    if prev_snap.get("head_sha") != new_snap.get("head_sha"):
        events.append({
            "id": f"head_changed:{new_snap.get('head_sha')}",
            "kind": "head_change",
        })
    prev_r = {
        r.get("id") for r in prev_snap.get("formal_reviews", [])
    }
    new_r = {r.get("id") for r in new_snap.get("formal_reviews", [])}
    for rid in sorted(new_r - prev_r):
        events.append({
            "id": f"new_review:{rid}",
            "kind": "new_formal_review",
            "review_id": rid,
        })
    prev_c = {
        c.get("id") for c in prev_snap.get("issue_comments", [])
    }
    new_c = {
        c.get("id") for c in new_snap.get("issue_comments", [])
    }
    for cid in sorted(new_c - prev_c):
        events.append({
            "id": f"new_issue_comment:{cid}",
            "kind": "new_reviewer_issue_comment",
            "comment_id": cid,
        })
    prev_t = prev_snap.get("review_threads", {})
    new_t = new_snap.get("review_threads", {})
    for tid in sorted(set(new_t) - set(prev_t)):
        s = new_t[tid]
        if (not s.get("resolved")) and (not s.get("outdated")):
            events.append({
                "id": f"new_thread:{tid}",
                "kind": "new_unresolved_current_thread",
                "thread_id": tid,
            })
    for tid in sorted(set(prev_t) & set(new_t)):
        prev_unresolved = (
            (not prev_t[tid].get("resolved"))
            and (not prev_t[tid].get("outdated"))
        )
        new_unresolved = (
            (not new_t[tid].get("resolved"))
            and (not new_t[tid].get("outdated"))
        )
        if (not prev_unresolved) and new_unresolved:
            events.append({
                "id": f"thread_reopened:{tid}",
                "kind": "thread_reopened",
                "thread_id": tid,
            })
    prev_cks = prev_snap.get("required_checks", {})
    new_cks = new_snap.get("required_checks", {})
    for name in sorted(set(prev_cks) | set(new_cks)):
        if prev_cks.get(name) != new_cks.get(name):
            events.append({
                "id": f"check_changed:{name}",
                "kind": "required_check_conclusion_change",
                "check": name,
            })
    prev_p = prev_snap.get("providers", {})
    new_p = new_snap.get("providers", {})
    for p in sorted(set(prev_p) | set(new_p)):
        if prev_p.get(p) != new_p.get(p):
            events.append({
                "id": f"provider_state:{p}",
                "kind": "provider_state_change",
                "provider": p,
            })
            if new_p.get(p, {}).get("in_progress"):
                events.append({
                    "id": f"provider_in_progress:{p}",
                    "kind": "provider_in_progress",
                    "provider": p,
                })
    return events


def revoke_readiness(reason: str, head_sha: str = None) -> None:
    write_readiness_state({
        "state": STATE_ACTIVE_REPAIR,
        "revoked_at": now_iso(),
        "reason": reason,
        "head_sha_at_revoke": head_sha or AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
    })


def _reopen_qualifying_head_if_needed(
    fresh_event_ids: list,
    head_sha: str,
) -> bool:
    """Round-33: drive the canonical state machine from
    QUALIFYING_READINESS back to REPAIRING_REVIEW_FINDINGS when new
    actionable reviews arrive on a previously-qualified head.

    Without this re-open, the supervisor's ``revoke_readiness``
    only flips the lightweight ``readiness_state.json`` and the
    canonical state machine at ``state/<pr>/orch/state.json``
    remains at QUALIFYING_READINESS. The relay correctly fails
    closed (RelayError) on the QUALIFYING_READINESS guard and the
    actionable review is stranded indefinitely.

    The re-open is gated on the same canonical-state guard the
    relay uses, so an already-running REPAIR cycle is not
    double-entered. If the canonical state is anything OTHER than
    QUALIFYING_READINESS, the function is a no-op and the caller
    proceeds to the relay as before.

    Returns True when a re-open transition was successfully
    applied (canonical state machine moved to
    REPAIRING_REVIEW_FINDINGS); False otherwise (no-op, error,
    or the canonical state was already past QUALIFYING_READINESS).
    """
    if not fresh_event_ids:
        return False
    try:
        from autocoder_orchestration.controller import Controller
        from autocoder_orchestration.context import RunContext
        from autocoder_orchestration.store import StateStore
    except ImportError as exc:
        log(
            "warning",
            "supervisor cannot import autocoder_orchestration; "
            "qualifying-head reopen disabled",
            error=str(exc),
        )
        return False
    try:
        from .orchestration_state_root import (
            resolve_orchestration_state_root,
        )
        state_root = resolve_orchestration_state_root(
            run_state_path=Path(RUN_STATE),  # type: ignore[name-defined]
            expected_repo=f"{REPO_OWNER}/{REPO_NAME}",  # type: ignore[name-defined]
            expected_pr_number=int(PR_NUMBER),  # type: ignore[name-defined]
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        log(
            "warning",
            "qualifying-head reopen: orchestration state root unresolved",
            error=str(exc),
        )
        return False
    try:
        store = StateStore(state_root)
        rc = store.read_optional("run_context.json")
        if rc is None:
            return False
        ctx = RunContext.from_dict(rc)
        controller = Controller(context=ctx, store=store)
        sm = controller.load_state_machine()
        if sm is None:
            return False
        if sm.current_state != "QUALIFYING_READINESS":
            # Already past QUALIFYING_READINESS — caller proceeds
            # to the relay as before; no re-open needed.
            return False
        try:
            controller.report_new_actionable_review_on_qualified_head(
                head_observed=head_sha,
                actionable_review_inventory=list(fresh_event_ids),
            )
            log(
                "info",
                "qualifying-head reopen: canonical state "
                "QUALIFYING_READINESS -> REPAIRING_REVIEW_FINDINGS",
                event_count=len(fresh_event_ids),
                head=head_sha[:12] if head_sha else None,
            )
            return True
        except Exception as exc:  # noqa: BLE001 — defensive
            log(
                "warning",
                "qualifying-head reopen failed; relay will see stale state",
                error=str(exc),
            )
            return False
    except Exception as exc:  # noqa: BLE001 — defensive
        log(
            "warning",
            "qualifying-head reopen: unexpected failure",
            error=str(exc),
        )
        return False


def enter_readiness(into: str, head_sha: str = None) -> None:
    write_readiness_state({
        "state": into,
        "achieved_at": now_iso(),
        "head_sha": head_sha or AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
        "policy": POLICY,
    })


def _advance_awaiting_ci_to_qualifying() -> bool:
    """Round-37: when the controller is stuck in AWAITING_CI with
    no live CI evidence to wait for (the PR has no
    ``.github/workflows`` configured, or the head has zero
    ``check-runs``), the supervisor MUST drive
    ``report_ci_pass`` directly so the canonical state
    machine advances to QUALIFYING_READINESS.

    Without this path, the controller is permanently stuck
    in AWAITING_CI and the relay refuses every invocation
    (``RelayLoop.run_once`` only runs from
    REPAIRING_REVIEW_FINDINGS). The supervisor's existing
    ``enter_qualifying_readiness`` path only fires when the
    relay signals the transition — but the relay cannot
    signal it from AWAITING_CI.

    This helper:
      1. Resolves the canonical orchestration state root.
      2. Loads the state machine.
      3. If the controller is in AWAITING_CI, calls
         ``Controller.report_ci_pass(head_observed=...)``.
      4. Returns True iff the transition fired.

    Failures (state-root unresolved, state machine absent,
    wrong current state, transition rejected) all return
    False so the caller can keep polling without escalating.
    """
    try:
        from .orchestration_state_root import (
            OrchestrationRootError,
            resolve_orchestration_state_root,
        )
        from autocoder_orchestration.controller import Controller
        from autocoder_orchestration.context import RunContext
        from autocoder_orchestration.store import (
            StateStore, StateStoreError,
        )
    except ImportError as exc:
        log(
            "warning",
            "_advance_awaiting_ci_to_qualifying: import failed",
            error=str(exc),
        )
        return False
    try:
        state_root = resolve_orchestration_state_root(
            run_state_path=Path(RUN_STATE),  # type: ignore[name-defined]
            expected_repo=f"{REPO_OWNER}/{REPO_NAME}",  # type: ignore[name-defined]
            expected_pr_number=int(PR_NUMBER),  # type: ignore[name-defined]
        )
    except OrchestrationRootError as exc:
        log(
            "warning",
            "_advance_awaiting_ci_to_qualifying: orchestration root unresolved",
            error=str(exc),
        )
        return False
    try:
        store = StateStore(state_root)
        rc = store.read_optional("run_context.json")
        if rc is None:
            return False
        ctx = RunContext.from_dict(rc)
        controller = Controller(context=ctx, store=store)
        sm = controller.load_state_machine()
        if sm is None:
            return False
        if sm.current_state != "AWAITING_CI":
            # Not stuck; nothing to do.
            return False
        # Round-31 P1#7: a manual head advance while the
        # controller happens to be in ``AWAITING_CI`` MUST NOT
        # be promoted to ``QUALIFYING_READINESS`` without first
        # verifying the live CI evidence. The previous
        # implementation called ``Controller.report_ci_pass``
        # unconditionally, which recorded the qualifying
        # transition even when the underlying checks were
        # pending or failing — a silent CI-bypass regression.
        # The supervisor MUST:
        #   1. Capture a fresh snapshot for ``AUTHORITATIVE_HEAD``.
        #   2. Confirm ``required_checks_green(snap)`` is True.
        #   3. If checks are not green (any check pending,
        #      failed, or absent), refuse the transition and
        #      log a diagnostic so the next heartbeat retries.
        _token = get_github_token() or ""
        try:
            _rs_for_snapshot = read_run_state()
        except Exception as exc:  # noqa: BLE001
            log(
                "warning",
                "_advance_awaiting_ci_to_qualifying: read_run_state failed; "
                "refusing transition (fail-closed)",
                error=str(exc),
            )
            return False
        try:
            _snap = capture_live_snapshot(_rs_for_snapshot, _token)
        except Exception as exc:  # noqa: BLE001
            log(
                "warning",
                "_advance_awaiting_ci_to_qualifying: snapshot capture failed; "
                "refusing transition (fail-closed)",
                error=str(exc),
            )
            return False
        # Round-39: distinguish CI policy outcomes. The
        # legacy ``required_checks_green`` returned True for
        # an empty required-check list, silently fabricating
        # ``CI PASSED`` on repos with zero required checks.
        # The round-39 contract requires explicit semantic
        # evidence: ``NO_REQUIRED_CHECKS`` is the
        # qualification-allowed outcome for empty
        # required-check sets; ``CHECKS_GREEN`` requires
        # at least one green check; ``CHECKS_PENDING`` /
        # ``CHECKS_FAILED`` / ``POLICY_UNRESOLVED`` block
        # the transition.
        ci_state = ci_policy_status(_snap)
        if ci_state == CI_POLICY_CHECKS_PENDING:
            log(
                "warning",
                "_advance_awaiting_ci_to_qualifying: required checks pending; "
                "refusing transition (fail-closed)",
                required_checks=_snap.get("required_checks", {}),
                head=AUTHORITATIVE_HEAD[:12]  # type: ignore[name-defined]
                if AUTHORITATIVE_HEAD  # type: ignore[name-defined]
                else "",
            )
            return False
        if ci_state == CI_POLICY_CHECKS_FAILED:
            log(
                "warning",
                "_advance_awaiting_ci_to_qualifying: required checks failed; "
                "refusing transition (fail-closed)",
                required_checks=_snap.get("required_checks", {}),
                head=AUTHORITATIVE_HEAD[:12]  # type: ignore[name-defined]
                if AUTHORITATIVE_HEAD  # type: ignore[name-defined]
                else "",
            )
            return False
        if ci_state == CI_POLICY_POLICY_UNRESOLVED:
            log(
                "warning",
                "_advance_awaiting_ci_to_qualifying: ci policy unresolved; "
                "refusing transition (fail-closed)",
                head=AUTHORITATIVE_HEAD[:12]  # type: ignore[name-defined]
                if AUTHORITATIVE_HEAD  # type: ignore[name-defined]
                else "",
            )
            return False
        # ci_state is CHECKS_GREEN or NO_REQUIRED_CHECKS — both
        # allow qualification to proceed.
        # Drive the transition. ``report_ci_pass`` will
        # validate ``head_required`` against the bound
        # authorized head; we pass ``AUTHORITATIVE_HEAD``
        # which the supervisor just rebound.
        head = AUTHORITATIVE_HEAD  # type: ignore[name-defined]
        controller.report_ci_pass(head_observed=head)
        log(
            "info",
            "round-37 supervisor advanced AWAITING_CI -> "
            "QUALIFYING_READINESS via direct report_ci_pass",
            head=head[:12] if head else "",
            ci_policy=ci_state,
            required_checks=(
                list(POLICY.get("required_check_names") or [])  # type: ignore[name-defined]
            ),
        )
        return True
    except (OSError, StateStoreError, ValueError, KeyError) as exc:
        log(
            "warning",
            "_advance_awaiting_ci_to_qualifying: unexpected failure",
            error=str(exc),
        )
        return False


def run_iteration_v5(rs: dict, token: str) -> dict:
    snap = capture_live_snapshot(rs, token or "")
    prev_snap = read_snapshot("A")
    events = detect_new_actionable_events(prev_snap, snap)
    for ev in events:
        write_unconsumed_event(ev)
    head_match = snap.get("head_match", False)
    decision = {
        "decision": "skip",
        "head_match": head_match,
        "events": events,
        "head_sha": snap.get("head_sha"),
        "state": (
            read_readiness_state().get("state")
            or STATE_ACTIVE_REPAIR
        ),
    }
    if not head_match:
        decision["decision"] = "head_mismatch"
        revoke_readiness(
            "head_no_longer_matches_authoritative",
            head_sha=snap.get("head_sha"),
        )
        decision["state"] = STATE_ACTIVE_REPAIR
        return decision
    if events:
        decision["decision"] = "events_detected"
    return decision


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def active_repair_quiet_window(
    rs: dict,
    token: str,
    quiet_window: int,
    pre_unconsumed_ids: set,
) -> Optional[str]:
    """Run the ACTIVE_REPAIR quiet-window transition.

    Strict quiet-window: the supervisor polls continuously
    for the full ``quiet_window`` seconds. Any non-qualifying
    observation (new event, snapshot drift, or BLOCKED
    controller) during the window RESETS the interval; the
    run only advances to PROVISIONAL_READY when one
    uninterrupted >= ``quiet_window`` second qualifying
    interval has elapsed.

    Polling cadence is the supervisor's heartbeat (default
    2 seconds). A qualifying observation is a snapshot A
    and snapshot B captured ``quiet_window`` seconds apart
    that match, with no new events emerging and the
    controller not in BLOCKED.

    Returns:
      - ``"escalation"``: controller is in BLOCKED; the
        caller must halt the run.
      - ``"new_event"``: a new event arrived during the
        window; the caller must route to handle_new_events.
      - ``"ready"``: the qualifying interval elapsed;
        readiness can be promoted. The caller should advance
        to PROVISIONAL_READY.
      - ``None``: the run stays in ACTIVE_REPAIR without
        advancing.

    New-event preservation: events that arrive DURING the
    quiet-window polling are NOT cleared by the post-loop
    cleanup. The post-loop clear only removes the
    pre-existing unconsumed events (the original set
    captured at the start). Newly arriving events remain
    in the unconsumed ledger so the next iteration's
    new_events list is populated and the supervisor
    routes them to handle_new_events. Previously the
    post-loop clear wiped ALL unconsumed events, allowing
    a new event to be silently absorbed without ever
    reaching handle_new_events.
    """
    import time as _time
    snap_a = capture_and_store_snapshot("A", rs, token)
    qualifying_started_at = _time.monotonic()
    last_blocked_check_at = 0.0
    new_event_observed = False
    controller_blocked = False
    while True:
        _time.sleep(min(2, quiet_window))
        # Touch the heartbeat so the liveness file is
        # refreshed during the quiet-window polling. The
        # polling is short (max 2s per iteration) so the
        # heartbeat is never blocked.
        try:
            heartbeat_touch()
        except (OSError, NameError):
            pass
        # Controller BLOCKED check at every heartbeat so
        # an escalation mid-window halts the transition.
        # Round-29 P1#7: the BLOCKED state is recorded by the
        # orchestration controller under the canonical
        # orchestration state root, NOT the supervisor's
        # private ``STATE_DIR``. Reading from ``STATE_DIR``
        # would route to a stale / empty location and the
        # supervisor would clear the escalation and promote
        # readiness despite the human halt. Resolve the
        # orch state root via the canonical resolver and
        # read ``<orch_state_root>/state.json``. If the
        # orch state root cannot be positively identified,
        # the supervisor MUST NOT proceed with the quiet
        # window; it routes to BLOCKED / escalation.
        snap_b = capture_live_snapshot(rs, token or "")
        new_events_during_window = [
            e
            for e in detect_new_actionable_events(snap_a, snap_b)
            if e.get("id") not in pre_unconsumed_ids
        ]
        # Round-32 P0: new traffic invalidates the
        # quiet interval immediately. The window MUST
        # yield to handle_new_events BEFORE the BLOCKED
        # controller check runs. A new event arriving
        # mid-window is proof that qualification is not
        # yet stable; the BLOCKED controller check is a
        # stale-state guard and MUST NOT preempt fresh
        # input. The prior code ran the BLOCKED check
        # first, which meant orch-root resolution
        # failures (which happen routinely during
        # partial GraphQL or transient GitHub errors)
        # could preempt fresh review events.
        elapsed = _time.monotonic() - qualifying_started_at
        if new_events_during_window:
            new_event_observed = True
            log(
                "info",
                "quiet-window: new event arrived during polling; "
                "yielding immediately to handle_new_events",
                event_count=len(new_events_during_window),
                elapsed=round(elapsed, 1),
            )
            for ev in new_events_during_window:
                if isinstance(ev, dict) and ev.get("id"):
                    write_unconsumed_event(ev)
            return "new_event"
        reasons = snapshot_differs(
            snap_a, snap_b, AUTHORITATIVE_HEAD  # type: ignore[name-defined]
        )
        # Round-32 P0.4: BLOCKED check runs ONLY when no
        # new traffic arrived. The check is bounded at
        # once per second.
        if _time.monotonic() - last_blocked_check_at > 1.0:
            last_blocked_check_at = _time.monotonic()
            try:
                from .orchestration_state_root import (
                    OrchestrationRootError,
                    resolve_orchestration_state_root,
                )
                orch_state_root = resolve_orchestration_state_root(
                    run_state_path=Path(RUN_STATE),  # type: ignore[name-defined]
                    expected_repo=f"{REPO_OWNER}/{REPO_NAME}",  # type: ignore[name-defined]
                    expected_pr_number=int(PR_NUMBER),  # type: ignore[name-defined]
                )
                state_path = Path(orch_state_root) / "state.json"
                if state_path.is_file():
                    controller_state = json.loads(
                        state_path.read_text(),
                    ).get("current_state")
                    if controller_state == "BLOCKED":
                        log(
                            "warning",
                            "quiet-window halted: controller is in BLOCKED "
                            "(relay escalated to human); operator must inspect",
                            head=AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
                        )
                        controller_blocked = True
                        break
                try:
                    heartbeat_touch()
                except (OSError, NameError):
                    pass
            except (OSError, json.JSONDecodeError, OrchestrationRootError):
                log(
                    "error",
                    "quiet-window halted: orchestration state_root "
                    "cannot be positively identified; BLOCKED/escalation",
                    head=AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
                )
                controller_blocked = True
                break
        if reasons:
            log(
                "info",
                "quiet-window: non-qualifying observation; "
                "resetting interval",
                reasons=reasons,
                elapsed=round(elapsed, 1),
            )
            snap_a = capture_and_store_snapshot("A", rs, token)
            qualifying_started_at = _time.monotonic()
            pre_unconsumed_ids = {
                e.get("id") for e in list_unconsumed_events()
            }
            continue
        if elapsed >= quiet_window:
            # One uninterrupted >=quiet_window second
            # qualifying interval has elapsed.
            break
    if controller_blocked:
        return "escalation"
    if new_event_observed:
        # Do NOT clear unconsumed events here. The new
        # event is preserved for the next iteration's
        # new_events list.
        return "new_event"
    # Snapshot is stable AND no new events emerged during the
    # quiet window. Clear ONLY the pre-existing unconsumed
    # events (the original set captured at the start), so
    # the readiness gate can pass. The post-loop clear
    # does not affect events that arrived during the
    # window, because the new_event_observed flag would
    # have triggered the early return above.
    #
    # Round-33 P1#1: PRESERVE cooldown-deferred events.
    # These were skipped at the dispatch site because the
    # provider cooldown was active; the quiet-window clear
    # MUST NOT silently lose them. Only events that are
    # NOT in the cooldown-deferred ledger are eligible
    # for the post-loop clear.
    cooldown_deferred = _cooldown_deferred_ids()
    pre_existing = [
        e for e in list_unconsumed_events()
        if e.get("id") in pre_unconsumed_ids
        and e.get("id") not in cooldown_deferred
    ]
    # Round-33 P1#1: rebuild the ledger so that ONLY
    # cooldown-deferred events survive. Pre-existing events
    # that were eligible for clearing are removed; cooldown-
    # deferred events are preserved with their full payload
    # so the next iteration's dispatch can act on them.
    if pre_existing or cooldown_deferred:
        preserved = [
            e for e in list_unconsumed_events()
            if e.get("id") in cooldown_deferred
        ]
        log(
            "info",
            "snapshot stable across quiet window; "
            "clearing pre-existing unconsumed events "
            "(preserving cooldown-deferred)",
            count=len(pre_existing),
            cooldown_deferred_preserved=len(cooldown_deferred),
        )
        write_json(
            UNCONSUMED_EVENTS_PATH,  # type: ignore[name-defined]
            {"events": preserved},
        )
    try:
        # Round-29 P1#7: read from the canonical
        # orchestration state root, NOT the supervisor's
        # private ``STATE_DIR``. BLOCKED recorded under the
        # orch state root MUST halt the supervisor's quiet
        # window; reading STATE_DIR would silently promote
        # readiness despite the human halt.
        orch_state_root = resolve_orchestration_state_root(
            run_state_path=Path(RUN_STATE),  # type: ignore[name-defined]
            expected_repo=f"{REPO_OWNER}/{REPO_NAME}",  # type: ignore[name-defined]
            expected_pr_number=int(PR_NUMBER),  # type: ignore[name-defined]
        )
        state_path = Path(orch_state_root) / "state.json"
        if state_path.is_file():
            controller_state = json.loads(
                state_path.read_text(),
            ).get("current_state")
            if controller_state == "BLOCKED":
                log(
                    "warning",
                    "quiet-window halted: controller is in BLOCKED "
                    "(relay escalated to human); operator must inspect",
                    head=AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
                )
                return "escalation"
    except (OSError, json.JSONDecodeError, OrchestrationRootError):
        log(
            "warning",
            "quiet-window halted: could not read controller state "
            "(orchestration state_root not positively identified)",
            head=AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
        )
        return "escalation"
    result = evaluate_readiness(
        snap_b, AUTHORITATIVE_HEAD  # type: ignore[name-defined]
    )
    if result.get("ready"):
        enter_readiness(STATE_PROVISIONAL_READY)
        log(
            "info",
            "entered PROVISIONAL_READY (snapshot A == B)",
            head=AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
        )
        return "ready"
    log(
        "info",
        "readiness denied",
        reason=result.get("reason"),
    )
    return None


def _persist_qualifying_readiness_retry(
    head_sha: str,
    fresh_ids: list,
) -> None:
    """Round-32: persist the qualifying-readiness retry
    state when the persistence failed.
    """
    try:
        from autocoder_orchestration.review_repair_relay import (
            read_round_budget_retry,
        )
    except Exception:  # noqa: BLE001
        read_round_budget_retry = None  # type: ignore[assignment]
    from pathlib import Path
    evidence_root = (
        Path(str(RUN_STATE)).parent / "evidence"  # type: ignore[name-defined]
    )
    evidence_root.mkdir(parents=True, exist_ok=True)
    retry_path = evidence_root / "round_budget_retry.json"
    prior = (
        read_round_budget_retry(str(evidence_root))
        if read_round_budget_retry is not None
        else None
    ) or {}
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    prior_count = int(prior.get("attempt_count", 0)) if prior else 0
    attempt_count = prior_count + 1
    backoff = min(30 * (2 ** min(attempt_count - 1, 5)), 600)
    try:
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        next_dt = now_dt + timedelta(seconds=backoff)
        next_eligible_retry_at = next_dt.isoformat()
    except Exception:  # noqa: BLE001
        next_eligible_retry_at = now
    payload = {
        **prior,
        "reason": "qualifying_readiness_persistence_failed",
        "attempt_count": attempt_count,
        "first_failure_at": prior.get(
            "first_failure_at", now,
        ) if prior else now,
        "last_attempt_at": now,
        "next_eligible_retry_at": next_eligible_retry_at,
        "recorded_at": now,
        "owner": "relay_recovery",
        "recoverable": True,
        "head_sha": head_sha,
        "fresh_ids": list(fresh_ids),
    }
    try:
        tmp = retry_path.with_suffix(retry_path.suffix + ".tmp")
        import json as _json
        tmp.write_text(_json.dumps(payload, sort_keys=True))
        tmp.replace(retry_path)
    except OSError as e:
        log(
            "warning",
            "qualifying-readiness retry persistence failed",
            error=str(e),
        )


def _write_orchestration_owner(this_pr: int) -> None:
    """Round-32: stall watchdog.

    Persists ``orchestration_owner.json`` per PR with the
    current state, owner, last progress timestamp, next
    action, and next_eligible_at. External observers can
    detect a STALL when:

    - state is non-terminal,
    - no worker is active,
    - no retry is scheduled (next_eligible_retry_at <= now),
    - no poll is pending,
    - no external pending condition with a next-check time.

    The supervisor MUST NOT sit silently in that
    condition. Detection is by inspection of this file
    and by the supervisor's own per-tick validation
    (logged loudly in main()).
    """
    try:
        # Find the per-PR evidence root via the canonical
        # resolver.
        try:
            from .orchestration_state_root import (
                resolve_orchestration_state_root,
            )
            state_root = resolve_orchestration_state_root(
                run_state_path=Path(RUN_STATE),  # type: ignore[name-defined]
                expected_repo=f"{REPO_OWNER}/{REPO_NAME}",  # type: ignore[name-defined]
                expected_pr_number=this_pr,
            )
        except Exception:  # noqa: BLE001
            state_root = None
        evidence_root = (
            Path(str(state_root)) / "evidence"
            if state_root is not None
            else None
        )
        if evidence_root is None:
            return
        from datetime import datetime, timezone
        # Detect stale retry ledger.
        retry_path = evidence_root / "round_budget_retry.json"
        next_eligible_retry_at = ""
        if retry_path.exists():
            try:
                retry_state = json.loads(
                    retry_path.read_text(),
                ) or {}
                next_eligible_retry_at = str(
                    retry_state.get(
                        "next_eligible_retry_at", "",
                    ),
                )
            except (OSError, json.JSONDecodeError):
                pass
        # Detect active worker lease.
        lease_active = False
        lease_pid_alive = False
        lease_path = (
            Path(str(RUN_STATE)).parent  # type: ignore[name-defined]
            / "state"
            / "worker_lease.json"
        )
        if lease_path.exists():
            try:
                lease = json.loads(lease_path.read_text())
                lease_active = lease.get("pr_number") == this_pr
                # Round-35: check whether the lease's pid is
                # still alive. If the worker died (defunct,
                # OSError on stat, missing) the lease is
                # stale and MUST be cleared so the next
                # iteration can re-dispatch the durable
                # event instead of being masked by a
                # perpetual lease_active.
                if lease_active:
                    try:
                        worker_pid = int(lease.get("pid", 0))
                        if worker_pid > 0:
                            os.kill(worker_pid, 0)
                            lease_pid_alive = True
                    except (OSError, ProcessLookupError, ValueError):
                        lease_pid_alive = False
                        log(
                            "warning",
                            "stale worker lease: pid dead; "
                            "clearing for re-dispatch",
                            worker_pid=lease.get("pid"),
                            head_at_launch=(
                                lease.get(
                                    "authoritative_head_at_launch",
                                    "",
                                )[:12]
                            ),
                            launched_at=lease.get("launched_at"),
                        )
                        try:
                            lease_path.unlink()
                        except OSError:
                            pass
                        lease_active = False
                        # The launched_event_ids set still
                        # holds the consumed event id from
                        # the failed launch. Unmark it so
                        # the durable event can be
                        # re-dispatched on the next
                        # iteration. This is the
                        # crash-after-claim recovery path
                        # the user required in TEST K.
                        last_dispatched = lease.get(
                            "last_dispatched_event_id", ""
                        )
                        if last_dispatched:
                            unmark_event_launched(last_dispatched)
            except (OSError, json.JSONDecodeError):
                pass
        # Controller state machine.
        sm_state = "UNKNOWN"
        sm_path = (
            Path(str(state_root)) / "state.json"
            if state_root is not None
            else None
        )
        if sm_path and sm_path.exists():
            try:
                sm = json.loads(sm_path.read_text())
                sm_state = sm.get("current_state", "UNKNOWN")
            except (OSError, json.JSONDecodeError):
                pass
        # Owner determination (per round-32 spec).
        if sm_state in (
            "AWAITING_MERGE_AUTHORIZATION",
        ):
            owner = "human"
            next_action = "merge_authorization"
        elif sm_state == "BLOCKED":
            owner = "supervisor"
            next_action = "diagnose_block"
        elif lease_active:
            owner = "worker"
            next_action = "observe_worker_exit"
        elif next_eligible_retry_at:
            owner = "retry_scheduler"
            next_action = "wait_for_retry_window"
        elif sm_state in (
            "QUALIFYING_READINESS",
            "AWAITING_CI",
        ):
            owner = "ci_qualifier"
            next_action = "poll_ci_or_qualify"
        elif sm_state == "REPAIRING_REVIEW_FINDINGS":
            owner = "review_waiter"
            next_action = "poll_providers"
        else:
            owner = "review_waiter"
            next_action = "poll_providers"
        payload = {
            "pr_number": this_pr,
            "state": sm_state,
            "owner": owner,
            "next_action": next_action,
            "next_eligible_retry_at": next_eligible_retry_at,
            "worker_active": lease_active,
            "head_sha": (
                str(AUTHORITATIVE_HEAD)  # type: ignore[name-defined]
            ),
            "last_progress_at": datetime.now(
                timezone.utc,
            ).isoformat(),
            "recorded_at": datetime.now(
                timezone.utc,
            ).isoformat(),
        }
        owner_path = evidence_root / "orchestration_owner.json"
        try:
            owner_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = owner_path.with_suffix(
                owner_path.suffix + ".tmp",
            )
            tmp.write_text(
                json.dumps(payload, sort_keys=True),
            )
            tmp.replace(owner_path)
        except OSError:
            pass
    except Exception:  # noqa: BLE001 - watchdog MUST never raise
        pass


def _clear_stale_retry_ledgers(this_pr: int) -> None:
    """Round-32: clear stale retry state after a
    successful relay round.

    A retry record from a prior failed invocation
    can otherwise live forever (the
    ``next_eligible_retry_at`` keeps advancing but
    no real work happens). After a successful relay
    round the supervisor MUST mark the ledger
    ``cleared`` so the next slice does NOT inherit a
    stale retry window.

    Lifecycle: PENDING → CLAIMED_FOR_NEW_SLICE →
    RESOLVED / CLEARED. A retry record that survives
    every supervisor tick without being claimed or
    cleared is a STALL signal.
    """
    try:
        from .orchestration_state_root import (
            resolve_orchestration_state_root,
        )
        state_root = resolve_orchestration_state_root(
            run_state_path=Path(RUN_STATE),  # type: ignore[name-defined]
            expected_repo=f"{REPO_OWNER}/{REPO_NAME}",  # type: ignore[name-defined]
            expected_pr_number=this_pr,
        )
    except Exception:  # noqa: BLE001
        return
    if state_root is None:
        return
    evidence_root = Path(str(state_root)) / "evidence"
    retry_path = evidence_root / "round_budget_retry.json"
    if not retry_path.exists():
        return
    try:
        retry_state = json.loads(retry_path.read_text()) or {}
    except (OSError, json.JSONDecodeError):
        return
    # If the retry was already cleared, no-op.
    if retry_state.get("lifecycle") in (
        "cleared", "resolved",
    ):
        return
    # If we got here, a successful iteration ran. Mark
    # the prior retry as cleared so it does not
    # accumulate.
    from datetime import datetime, timezone
    retry_state["lifecycle"] = "cleared"
    retry_state["cleared_at"] = datetime.now(
        timezone.utc,
    ).isoformat()
    try:
        tmp = retry_path.with_suffix(retry_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(retry_state, sort_keys=True),
        )
        tmp.replace(retry_path)
    except OSError:
        pass


def _persist_root_resolution_retry(exc: Any) -> None:
    """Round-32: persist the orchestration-root-resolution
    retry state so the supervisor's next-slice main loop
    can resume without operator intervention.

    The persisted state uses the canonical
    ``round_budget_retry.json`` shape (the supervisor's
    main loop already reads it) so the same scheduler
    code that handles round-budget exhaustion also
    handles root-resolution failures. The ``reason``
    field distinguishes the two.
    """
    _persist_retry_with_reason(
        reason="orchestration_root_unresolved",
        extra={"error": str(exc)},
    )


def _resolve_per_pr_evidence_roots(
    *,
    run_state_path: Any,
    pr_numbers: Any,
    expected_repo: str,
    fallback_root: str,
) -> list:
    """Round-43 C11: resolve the canonical ``evidence/``
    directory for every PR the supervisor owns.

    Returns a list of absolute paths to ``evidence/``
    directories, one per PR. The supervisor's main
    heartbeat loop reads and writes the durable retry
    ledger (``round_budget_retry.json``) at these
    paths so it shares a single ledger with the relay
    CLI. The legacy single-root fallback
    (``RUN_STATE.parent / "evidence"``) is included
    ONLY when the per-PR resolver fails entirely; in
    the steady-state multi-PR architecture the
    fallback path is empty.

    The function is intentionally defensive: a
    resolver failure for one PR MUST NOT prevent
    iteration over the others, and a single-PR owner
    MUST receive exactly one root.
    """
    roots: list = []
    seen: set = set()
    try:
        for owned in pr_numbers or []:
            try:
                owned_int = int(owned)
            except (TypeError, ValueError):
                continue
            if owned_int == 0:
                continue
            try:
                from .orchestration_state_root import (
                    resolve_orchestration_state_root,
                )
                state_root = resolve_orchestration_state_root(
                    run_state_path=run_state_path,
                    expected_repo=expected_repo,
                    expected_pr_number=owned_int,
                )
            except Exception:  # noqa: BLE001
                state_root = None
            if state_root is None:
                continue
            root = str(Path(str(state_root)) / "evidence")
            if root and root not in seen:
                seen.add(root)
                roots.append(root)
    except Exception:  # noqa: BLE001
        pass
    if not roots:
        # Defensive fallback: when no per-PR resolver
        # succeeds (e.g. test environment with empty
        # ``run_state.json``), fall back to the legacy
        # global path so the heartbeat loop continues.
        roots = [fallback_root]
    return roots


def _persist_round_budget_retry(
    *,
    head_sha: Any,
    fresh_ids: list,
    reason: str,
) -> None:
    """Round-33: unified retry persistence for review-repair
    paths that must NOT fall through to a generic worker.

    Persists under ``round_budget_retry.json`` so the
    supervisor's existing main-loop scheduler reads it on
    the next heartbeat. The ``reason`` field distinguishes
    the failure category (no_action_on_review_repair,
    unknown_relay_action:<action>, etc.). The retry ledger
    lifecycle is enforced by the persistence helper:
    PENDING -> eligible -> atomically CLAIM -> bump epoch
    ONCE -> CONSUMED/CLEARED. A retry record with
    lifecycle='cleared' MUST NOT trigger slice_epoch bumps
    on later heartbeats; the supervisor's bump_slice_epoch
    helper reads ``lifecycle`` and skips cleared records.
    """
    _persist_retry_with_reason(
        reason=reason,
        extra={
            "head_sha": head_sha,
            "fresh_ids": list(fresh_ids),
        },
    )


def _persist_retry_with_reason(
    *,
    reason: str,
    extra: dict,
) -> None:
    """Shared persistence helper for review-repair retries.

    Lifecycle invariant: this helper writes a ledger entry
    that is ONLY eligible to be claimed once. The supervisor's
    main-loop scheduler bumps the slice_epoch on CLAIM and
    does NOT bump it again on a record that has
    ``lifecycle='cleared'`` (Round-33 P1#2 — cleared retry
    ledger must not trigger slice_epoch bumps).

    Restart after claim: if the supervisor restarts between
    CLAIM and CONSUMED/CLEARED, the next heartbeat re-claims
    the SAME record (same reason, same fresh_ids) and bumps
    the epoch exactly ONCE. The ``attempt_count`` is
    monotonic; the ``slice_epoch_bumps`` field is the
    authoritative bump count.
    """
    try:
        from autocoder_orchestration.review_repair_relay import (
            read_round_budget_retry,
        )
    except Exception:  # noqa: BLE001
        read_round_budget_retry = None  # type: ignore[assignment]
    from pathlib import Path
    evidence_root = (
        Path(str(RUN_STATE)).parent / "evidence"  # type: ignore[name-defined]
    )
    evidence_root.mkdir(parents=True, exist_ok=True)
    retry_path = evidence_root / "round_budget_retry.json"
    prior = (
        read_round_budget_retry(str(evidence_root))
        if read_round_budget_retry is not None
        else None
    ) or {}
    # Lifecycle enforcement: a prior record with
    # lifecycle='cleared' means the work has been CONSUMED.
    # Do NOT re-claim — that would double-bump the slice_epoch
    # on later heartbeats.
    #
    # Round-39 P1#4: a ``cleared`` ledger MUST NOT suppress
    # later independent retry work. The ``round_budget_retry``
    # file is shared across multiple retry categories
    # (`no_action_on_review_repair`, `unknown_relay_action:...`,
    # `qualifying_readiness_persistence_failed`, etc.). When
    # one category is CONSUMED, a later failure from a
    # DIFFERENT category must be able to record a new
    # attempt. The previous unconditional return
    # permanently silenced the file after a successful
    # round-budget recovery, so a subsequent
    # ``no_action_on_review_repair`` or orchestration-root
    # failure could not replace the old record.
    if prior.get("lifecycle") in ("cleared", "resolved", "consumed"):
        # The prior work has been handled. Reset the
        # lifecycle to ``pending`` so a NEW failure
        # from this category (or any other category
        # sharing this file) can record a fresh
        # attempt. The slice_epoch_bumps count is
        # preserved across the reset so the
        # slice-budget cycle is not double-bumped.
        prior = {
            "lifecycle": "pending",
            "reset_after_consumed": True,
            "previous_reason": prior.get("reason"),
            "slice_epoch_bumps": int(
                prior.get("slice_epoch_bumps", 0) or 0
            ),
        }
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    prior_count = int(prior.get("attempt_count", 0)) if prior else 0
    attempt_count = prior_count + 1
    backoff = min(30 * (2 ** min(attempt_count - 1, 5)), 600)
    try:
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        next_dt = now_dt + timedelta(seconds=backoff)
        next_eligible_retry_at = next_dt.isoformat()
    except Exception:  # noqa: BLE001
        next_eligible_retry_at = now
    payload = {
        **prior,
        **extra,
        "reason": reason,
        "lifecycle": "pending",
        "attempt_count": attempt_count,
        "first_failure_at": prior.get(
            "first_failure_at", now,
        ) if prior else now,
        "last_attempt_at": now,
        "next_eligible_retry_at": next_eligible_retry_at,
        "recorded_at": now,
        "owner": "supervisor_recovery",
        "recoverable": True,
        # Round-33 P1#2: explicit single-bump tracking.
        # The first CLAIM bumps the slice_epoch once; later
        # heartbeats that re-encounter this record MUST NOT
        # bump again. The supervisor's bump_slice_epoch
        # helper reads this field and skips when bumped>0.
        "slice_epoch_bumps": int(
            prior.get("slice_epoch_bumps", 0) if prior else 0
        ),
    }
    try:
        tmp = retry_path.with_suffix(retry_path.suffix + ".tmp")
        import json as _json
        tmp.write_text(_json.dumps(payload, sort_keys=True))
        tmp.replace(retry_path)
    except OSError as e:
        log(
            "warning",
            "review-repair retry state persistence failed",
            reason=reason,
            error=str(e),
        )


def _invoke_relay_for_events(
    new_events: list,
) -> str:
    """Invoke the relay's review-repair-round CLI for the
    current live snapshot.

    Returns the relay's action string:
    - ``"launch_worker"``: the directive is on disk; the
      supervisor launches via the existing machinery.
    - ``"enter_qualifying_readiness"``: the head is clean.
      The supervisor removes the directive and lets the
      existing readiness machinery evaluate the head.
    - ``"escalate_to_human"``: the relay drove the controller
      into ``BLOCKED``. The supervisor DOES NOT launch a
      worker; the operator must inspect.
    - ``"no_action"``: the relay was not invoked (no
      actionable findings, CLI missing, wiring failure).
      The supervisor falls back to the existing
      ``launch_worker`` path.

    Round-45 C13: when ``new_events`` contains a single
    ``unresolved_thread_drain:<tid>`` event, the directive is
    scoped to that specific thread via the
    ``focused_thread_id`` parameter. The supervisor's
    durable-thread-drain path emits exactly one such event
    per heartbeat; focusing the worker on the targeted
    thread prevents it from re-auditing the historical 8-P1
    backlog. After the worker exits with a terminal
    disposition for the targeted thread, the supervisor
    consumes the drain event (round 45 Section 6) so the
    same thread is not dispatched again on the next
    heartbeat.
    """
    # Round-45 C13: detect the targeted thread drain. The
    # supervisor emits at most one ``unresolved_thread_drain``
    # per heartbeat (round-34 anti-burst guard), but a
    # previous round's drain may still be in unconsumed
    # when a new actionable event arrives in the same batch
    # (e.g. ``head_change``). When multiple drain events
    # coexist, focus on the first one (sort-stable) so the
    # worker evaluates a single thread instead of the
    # historical 8-P1 backlog. After the worker exits with
    # a terminal disposition for the targeted thread, the
    # supervisor consumes the drain event (round 45
    # Section 6) so the same thread is not dispatched again
    # on the next heartbeat.
    focused_thread_id: Optional[str] = None
    for ev in new_events:
        if not isinstance(ev, dict):
            continue
        eid = ev.get("id") or ""
        if isinstance(eid, str) and eid.startswith(
            "unresolved_thread_drain:"
        ):
            focused_thread_id = eid.split(":", 1)[1]
            break
    from .relay_wiring import (
        EscalateToHuman,
        InvalidSnapshot,
        RecoverableRetry,
        RelayError,
        RelayWiringError,
        delete_directive_if_present,
        invoke_relay_round,
        should_invoke_relay,
    )
    token = get_github_token()
    snapshot = capture_live_snapshot(
        {"current_head": AUTHORITATIVE_HEAD},  # type: ignore[name-defined]
        token or "",
    )
    if not should_invoke_relay(snapshot):
        return "no_action"
    # Resolve the canonical state and evidence roots. Round-28
    # invariant: ``STATE_DIR`` is NOT a fallback. If neither the
    # env var nor ``RUN_STATE`` yields a positively-identified
    # orchestration state root, the relay invocation MUST
    # surface a fail-closed error and the supervisor MUST NOT
    # launch a generic worker — it routes to the BLOCKED /
    # escalation path and stops autonomous progression.
    from .orchestration_state_root import (
        OrchestrationRootError,
        resolve_orchestration_state_root,
    )
    from .relay_wiring import (
        _resolve_orchestration_evidence_root,
    )
    try:
        state_root = resolve_orchestration_state_root(
            run_state_path=Path(RUN_STATE),  # type: ignore[name-defined]
            expected_repo=f"{REPO_OWNER}/{REPO_NAME}",  # type: ignore[name-defined]
            expected_pr_number=int(PR_NUMBER),  # type: ignore[name-defined]
        )
    except OrchestrationRootError as exc:
        # Round-32: orchestration root lookup failure is
        # a recoverable failure, NOT a generic-worker
        # fallback. The supervisor persists a durable
        # retry ledger (same shape as round-budget
        # retry) with ``reason='orchestration_root'``
        # and returns ``recoverable_retry`` so the
        # caller's dedicated branch keeps the event
        # actionable and does NOT launch a generic
        # worker.
        log(
            "error",
            "orchestration_root unresolved; recoverable retry; "
            "event stays actionable; supervisor schedules retry",
            error=str(exc),
        )
        _persist_root_resolution_retry(exc)
        return "recoverable_retry"
    evidence_root = _resolve_orchestration_evidence_root(state_root)
    run_id = os.environ.get(
        "AED_RUN_ID", f"PR-{PR_NUMBER}",  # type: ignore[name-defined]
    )
    try:
        decision = invoke_relay_round(
            snapshot=snapshot,
            head_sha=str(AUTHORITATIVE_HEAD),  # type: ignore[name-defined]
            state_root=state_root,
            run_id=run_id,
            pr_number=int(PR_NUMBER),  # type: ignore[name-defined]
            evidence_root=evidence_root,
            required_check_names=tuple(
                (POLICY or {}).get(  # type: ignore[name-defined]
                    "required_check_names", [],
                )
            ),
            focused_thread_id=focused_thread_id,
        )
    except (InvalidSnapshot, RecoverableRetry, RelayError) as exc:
        # Round-31: typed recoverable relay failures (any
        # subclass of RelayError) MUST NOT fall through
        # to the generic worker. The supervisor persists
        # the diagnostic state and continues polling
        # without marking events consumed and without
        # launching a worker. ``EscalateToHuman`` is a
        # RelayError subclass but is NOT recoverable; the
        # CLI's structured-decision path surfaces it as
        # ``action=escalate_to_human``, which the caller
        # routes to BLOCKED.
        if isinstance(exc, EscalateToHuman):
            # Re-raise: this is a protected-authority
            # signal, not a recoverable retry.
            raise
        log(
            "warning",
            "relay recoverable failure; supervisor persists "
            "diagnostic state and continues polling without "
            "marking events consumed",
            reason=type(exc).__name__,
            error=str(exc),
        )
        return "recoverable_retry"
    except RelayWiringError as exc:
        # Round-30: a wiring failure is also recoverable
        # unless it's a protected-authority escalation.
        # We do NOT fall back to launch_worker; we let the
        # supervisor's retry path pick up the work.
        log(
            "warning",
            "relay_wiring failed; recoverable retry; "
            "do NOT launch generic worker",
            reason=exc.reason,
            rc=exc.returncode,
        )
        return "recoverable_retry"
    action = decision.get("action")
    if action == "enter_qualifying_readiness":
        delete_directive_if_present(evidence_root)
        log(
            "info",
            "relay_signaled_enter_qualifying_readiness",
            round_index=decision.get("round_index"),
            head_sha=decision.get("head_sha"),
        )
        return "enter_qualifying_readiness"
    if action == "escalate_to_human":
        log(
            "warning",
            "relay_signaled_escalate_to_human",
            round_index=decision.get("round_index"),
            head_sha=decision.get("head_sha"),
            reasons=decision.get("escalate_reasons", []),
        )
        return "escalate_to_human"
    if action == "launch_worker":
        log(
            "info",
            "relay_signaled_launch_worker",
            round_index=decision.get("round_index"),
            directive_digest=decision.get("directive_digest"),
        )
        return "launch_worker"
    if action == "await_head_change":
        # Round-31: provider evidence incomplete. The
        # supervisor MUST NOT fall through to the
        # generic worker; the relay has explicitly
        # requested a recoverable retry with a new
        # provider-surface capture. Map to
        # ``recoverable_retry`` (not ``no_action``) so
        # the caller routes through the dedicated
        # recoverable-retry branch that does NOT
        # consume the event and does NOT launch a
        # generic worker.
        log(
            "warning",
            "relay_signaled_await_head_change; "
            "evidence incomplete; supervisor schedules "
            "retry with fresh surfaces",
            round_index=decision.get("round_index"),
            reasons=decision.get("escalate_reasons", []),
        )
        return "recoverable_retry"
    if action == "recoverable_retry":
        log(
            "warning",
            "relay_signaled_recoverable_retry",
            round_index=decision.get("round_index"),
            reasons=decision.get("escalate_reasons", []),
        )
        return "recoverable_retry"
    # Round-31: unknown future action. Fail closed:
    # route to ``recoverable_retry`` rather than
    # ``no_action`` so the generic worker never
    # launches on an unhandled relay action.
    log(
        "warning",
        "relay returned unknown action; routing to "
        "recoverable_retry to prevent no_action→generic "
        "worker fallback",
        action=action,
    )
    return "recoverable_retry"


def handle_new_events(
    rs: dict,
    new_events: list,
    token: str,
    iteration: dict,
) -> None:
    """Filter actionable events, check the worker lease,
    launch a single worker, and mark events only after a
    successful launch.

    The relay is invoked BEFORE the worker launch. The
    relay writes a structured directive to the canonical
    evidence root; the existing launch_worker picks it
    up via the directive bridge. The relay is the
    persistent wiring that eliminates the human
    copy/paste review-repair loop.

    Escalation policy: when the relay returns
    ``escalate_to_human`` or ``enter_qualifying_readiness``,
    the supervisor DOES NOT launch a worker. The
    operator must inspect the BLOCKED state or the
    existing readiness gate must certify the head.
    """
    already = launched_event_ids()
    fresh_ids = [
        e["id"] for e in new_events
        if e.get("id") and e["id"] not in already
    ]
    if not fresh_ids:
        return
    lease = read_lease()
    if lease and lease_alive(lease) is not None:
        # An existing worker is already responsible for
        # these events. The next heartbeat will re-check.
        return
    revoke_readiness(
        reason="new_actionable_event",
        head_sha=iteration.get("head_sha"),
    )
    # Round-33: drive the canonical state machine from
    # QUALIFYING_READINESS back to REPAIRING_REVIEW_FINDINGS BEFORE
    # the relay invocation. Without this re-open, the relay
    # correctly fails closed on the QUALIFYING_READINESS guard and
    # the actionable review is stranded indefinitely. The helper is
    # a no-op when canonical state is anything other than
    # QUALIFYING_READINESS, so an already-running REPAIR cycle is
    # not double-entered.
    _reopen_qualifying_head_if_needed(
        fresh_event_ids=fresh_ids,
        head_sha=str(iteration.get("head_sha") or AUTHORITATIVE_HEAD),  # type: ignore[name-defined]
    )
    # Persistent relay wiring. The relay replaces the
    # "copy-paste a giant prompt" loop with a structured
    # directive persisted to the canonical evidence root.
    try:
        relay_action = _invoke_relay_for_events(new_events)
    except Exception as exc:  # noqa: BLE001 — defensive
        log(
            "error",
            "relay_wiring blew up; falling back",
            error=str(exc),
        )
        relay_action = "no_action"
    if relay_action == "escalate_to_human":
        # Protected-authority escalation. The relay drove
        # the controller into BLOCKED. The supervisor
        # stops launching workers; the operator's halt
        # point is the only path forward.
        log(
            "warning",
            "supervisor halted: relay escalated to human",
            events=[
                e.get("kind") for e in new_events
                if e.get("id") in fresh_ids
            ],
        )
        # Mark the events so the next heartbeat does not
        # re-launch a worker. The relay has driven the
        # controller into BLOCKED.
        for eid in fresh_ids:
            mark_event_launched(eid)
        return
    if relay_action == "enter_qualifying_readiness":
        # The head is clean. The supervisor MUST drive the
        # orchestration controller through the canonical
        # state-machine transitions so the durable
        # ``state.json`` records the qualifying-readiness
        # transition. Without this, ``Controller.record_readiness_certificate``
        # cannot persist a readiness certificate because
        # the controller is still in REPAIRING_REVIEW_FINDINGS.
        # Round-29 P1#12: the relay drives the first
        # transition (REPAIRING_REVIEW_FINDINGS ->
        # AWAITING_CI) inside ``run_once``. The supervisor
        # drives the second transition (AWAITING_CI ->
        # QUALIFYING_READINESS) here so the qualification
        # is durable.
        # Round-29 review P7: bind the cross-package
        # imports at the top of the function body (before
        # the try/except) so a failed import cannot turn
        # into ``UnboundLocalError`` in the except clause.
        # The previous round's code imported these inside
        # the try body, which is unsafe when one of the
        # imports fails. We keep them LAZY (not at module
        # scope) so the supervisor's package import does
        # not require ``autocoder_orchestration`` to be
        # installed in the supervisor's venv.
        try:
            from autocoder_orchestration.controller import Controller
            from autocoder_orchestration.context import RunContext
            from autocoder_orchestration.store import (
                StateStore, StateStoreError,
            )
        except ImportError as exc:
            log(
                "warning",
                "supervisor cannot import autocoder_orchestration; "
                "qualifying-readiness persistence disabled",
                error=str(exc),
            )
            return
        try:
            # that pattern is unsafe when one of the imports
            # fails because Python's name-binding for the
            # except clause happens at function-scope, not
            # try-scope. The imports are bound above (before
            # the guarded execution path); the try body
            # references the already-bound names.
            orch_state_root = resolve_orchestration_state_root(
                run_state_path=Path(RUN_STATE),  # type: ignore[name-defined]
                expected_repo=f"{REPO_OWNER}/{REPO_NAME}",  # type: ignore[name-defined]
                expected_pr_number=int(PR_NUMBER),  # type: ignore[name-defined]
            )
            store = StateStore(orch_state_root)
            rc = store.read_optional("run_context.json")
            persistence_ok = False
            if rc is not None:
                ctx = RunContext.from_dict(rc)
                controller = Controller(context=ctx, store=store)
                sm = controller.load_state_machine()
                if sm is not None and sm.current_state == "AWAITING_CI":
                    try:
                        controller.report_ci_pass(
                            head_observed=AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
                        )
                        persistence_ok = True
                        log(
                            "info",
                            "supervisor persisted QUALIFYING_READINESS via "
                            "Controller.report_ci_pass()",
                            head=AUTHORITATIVE_HEAD[:12],  # type: ignore[name-defined]
                        )
                    except Exception as exc:
                        log(
                            "warning",
                            "supervisor report_ci_pass failed; "
                            "controller may still be in AWAITING_CI; "
                            "event remains actionable for retry",
                            error=str(exc),
                        )
                elif sm is not None and sm.current_state == "QUALIFYING_READINESS":
                    # Round-34: the orchestrator has already
                    # advanced to QUALIFYING_READINESS via the
                    # relay's own transition. The supervisor
                    # does not need to repeat it. The
                    # persistence is successful by inspection:
                    # the canonical state machine IS at
                    # QUALIFYING_READINESS with the rebound
                    # context. Mark persistence_ok so the
                    # event is consumed and we do NOT schedule
                    # an infinite retry loop on a transition
                    # that already completed.
                    persistence_ok = True
                    log(
                        "info",
                        "supervisor observed QUALIFYING_READINESS "
                        "(orchestrator advanced via relay; no "
                        "additional report_ci_pass needed)",
                        head=AUTHORITATIVE_HEAD[:12],  # type: ignore[name-defined]
                    )
        except (OSError, OrchestrationRootError,
                StateStoreError, ValueError, KeyError) as exc:
            log(
                "warning",
                "supervisor persistence of qualifying-readiness failed; "
                "event remains actionable for retry",
                error=str(exc),
            )
            persistence_ok = False
        if not persistence_ok:
            # Round-32: persistence failed. Do NOT mark
            # events launched; the qualifying-readiness
            # path did not durably succeed. Persist a
            # recoverable retry record so the
            # supervisor's next-slice main loop retries
            # the persistence without operator
            # intervention.
            _persist_qualifying_readiness_retry(
                head_sha=AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
                fresh_ids=fresh_ids,
            )
            log(
                "warning",
                "qualifying-readiness persistence failed; "
                "events remain actionable; supervisor schedules retry",
            )
            return
        log(
            "info",
            "supervisor halted: relay indicated qualifying readiness",
            events=[
                e.get("kind") for e in new_events
                if e.get("id") in fresh_ids
            ],
        )
        for eid in fresh_ids:
            mark_event_launched(eid)
        return
    if relay_action == "launch_worker":
        log(
            "info",
            "revoking readiness (new actionable "
            "event); relay-driven round",
            events=[
                e.get("kind") for e in new_events
                if e.get("id") in fresh_ids
            ],
        )
    elif relay_action == "recoverable_retry":
        # Round-30: recoverable relay failure. Do NOT
        # mark events consumed (they stay actionable
        # for the next heartbeat). Do NOT launch a
        # generic worker — the supervisor persists
        # diagnostic state and continues polling so the
        # SAME outstanding work is processed on the
        # next slice / heartbeat.
        log(
            "warning",
            "relay returned recoverable_retry; "
            "events remain actionable; supervisor "
            "continues polling without generic worker",
            events=[
                e.get("kind") for e in new_events
                if e.get("id") in fresh_ids
            ],
        )
        return
    elif relay_action == "no_action":
        # Round-33: relay returned no_action. The
        # supervisor MUST NOT fall through to a
        # generic worker on a review-repair event.
        # The event stays actionable; the next
        # slice retries with fresh surfaces.
        log(
            "warning",
            "relay returned no_action on review-repair "
            "event; supervisor persists and continues "
            "polling without generic worker",
            events=[
                e.get("kind") for e in new_events
                if e.get("id") in fresh_ids
            ],
        )
        _persist_round_budget_retry(
            head_sha=iteration.get("head_sha"),
            fresh_ids=fresh_ids,
            reason="no_action_on_review_repair",
        )
        # Round-39 P1#3: events are kept in the
        # unconsumed-events ledger (the original snapshot
        # deltas already wrote them there). The next
        # heartbeat's main loop supplements ``new_events``
        # with anything still in the unconsumed ledger so
        # the same event re-routes to the relay on the
        # next slice / heartbeat.
        return
    else:
        # Round-33: unknown relay action. The
        # supervisor MUST NOT fall through to a
        # generic worker on a review-repair event.
        # Treat as recoverable_retry: persist the
        # diagnostic state, leave the event
        # actionable, and let the next slice retry.
        log(
            "warning",
            "relay returned unknown action; "
            "supervisor persists and continues polling "
            "without generic worker (no_action->generic "
            "worker fallback removed)",
            relay_action=relay_action,
            events=[
                e.get("kind") for e in new_events
                if e.get("id") in fresh_ids
            ],
        )
        _persist_round_budget_retry(
            head_sha=iteration.get("head_sha"),
            fresh_ids=fresh_ids,
            reason=f"unknown_relay_action:{relay_action}",
        )
        return
    live = (
        inspect_live_state(token) if token else {}
    )
    # Round-39 P1#8: declare the fresh event ids to the
    # launcher's WorkerAttemptRecord constructor. The slot
    # is read by ``launch_worker`` and cleared immediately
    # after the record is persisted so a subsequent launch
    # (e.g. worker crash loop) does not inherit these ids.
    globals()["_pending_launch_event_ids"] = tuple(fresh_ids or ())
    try:
        new_lease = launch_worker(rs, live)
    finally:
        globals()["_pending_launch_event_ids"] = None
    if new_lease:
        for eid in fresh_ids:
            mark_event_launched(eid)
            # Round-33 P1#1: consume the cooldown-deferred
            # ledger so the event does not appear as
            # still-deferred on later heartbeats.
            # Exactly-one ownership: PENDING ->
            # dispatched, never twice.
        _consume_cooldown_deferred(fresh_ids)
    else:
        log(
            "warning",
            "worker launch failed; "
            "events remain actionable so "
            "the next heartbeat will retry",
            events=[
                e.get("kind") for e in new_events
                if e.get("id") in fresh_ids
            ],
        )


def capture_and_store_snapshot(
    slot: str, rs: dict, token: str,
) -> dict:
    """Capture a live snapshot and persist it under ``slot``.

    Module-level wrapper around the inline closure that
    previously lived inside ``main()`` so it can be reused
    by ``active_repair_quiet_window`` and tested in
    isolation.
    """
    snap = capture_live_snapshot(rs, token or "")
    write_snapshot(slot, snap)
    return snap


def _sync_readiness_state_with_controller() -> None:
    """Sync the supervisor's readiness_state with the
    orchestration controller's state.

    The relay drives the controller through the required
    state transitions (REPAIRING_REVIEW_FINDINGS ->
    AWAITING_CI -> QUALIFYING_READINESS, or into BLOCKED on
    escalation). The supervisor's readiness_state is a
    SEPARATE state machine for the quiet-window /
    PROVISIONAL_READY / AWAITING_MERGE_AUTHORIZATION
    transitions.

    When the controller is in BLOCKED, the supervisor's
    readiness state is also recorded as BLOCKED so the
    quiet-window logic halts the transition. This is the
    HALT signal: the supervisor stops launching workers
    AND stops promoting readiness until the operator
    inspects.
    """
    try:
        state_path = Path(STATE_DIR) / "state.json"  # type: ignore[name-defined]
        if not state_path.is_file():
            return
        controller_state = json.loads(
            state_path.read_text(),
        ).get("current_state")
    except (OSError, json.JSONDecodeError):
        return
    if controller_state == "BLOCKED":
        write_readiness_state({"state": "BLOCKED"})
        log(
            "warning",
            "supervisor readiness synced: controller is in BLOCKED; "
            "operator must inspect",
        )

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single iteration and exit (for testing)",
    )
    parser.add_argument(
        "--dry-sim",
        action="store_true",
        help="Dry-run: print decision without invoking resume",
    )
    parser.add_argument(
        "--isolated-state",
        action="store_true",
        help="Use isolated temporary state (for proof tests)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to TOML supervisor config (overrides env)",
    )
    args = parser.parse_args(argv)

    # Allow the operator to point at a TOML configuration file.
    if args.config:
        from .config import load_config
        cfg = load_config(args.config)
        _apply_config(cfg)
        globals()["POLICY"] = _default_policy(cfg)
        globals()["PROVIDERS"] = _default_providers(cfg)

    if args.isolated_state:
        import tempfile
        tmp = tempfile.mkdtemp(prefix="aed-supervisor-")
        for k in (
            "STATE_DIR", "LEASE_PATH", "LAST_RESUME_PATH",
            "QUOTA_PATH", "REVIEW_REQUESTS_DIR", "LOG_PATH",
            "HEARTBEAT_PATH", "LOCK_PATH", "RUN_STATE",
            "UNCONSUMED_EVENTS_PATH", "SNAPSHOT_A_PATH",
            "SNAPSHOT_B_PATH", "READINESS_STATE_PATH",
        ):
            base = Path(tmp)
            if k == "STATE_DIR":
                globals()[k] = base / "state"
            elif k == "LEASE_PATH":
                globals()[k] = globals()["STATE_DIR"] / "worker_lease.json"
            elif k == "LAST_RESUME_PATH":
                globals()[k] = globals()["STATE_DIR"] / "last_resume.json"
            elif k == "QUOTA_PATH":
                globals()[k] = globals()["STATE_DIR"] / "quota_state.json"
            elif k == "REVIEW_REQUESTS_DIR":
                globals()[k] = globals()["STATE_DIR"] / "review_requests"
            elif k == "LOG_PATH":
                globals()[k] = base / "supervisor.log"
            elif k == "HEARTBEAT_PATH":
                globals()[k] = base / "heartbeat"
            elif k == "LOCK_PATH":
                globals()[k] = base / "lock"
            elif k == "RUN_STATE":
                globals()[k] = base / "run_state.json"
            elif k == "UNCONSUMED_EVENTS_PATH":
                globals()[k] = globals()["STATE_DIR"] / "unconsumed_events.json"
            elif k == "SNAPSHOT_A_PATH":
                globals()[k] = globals()["STATE_DIR"] / "snapshot_a.json"
            elif k == "SNAPSHOT_B_PATH":
                globals()[k] = globals()["STATE_DIR"] / "snapshot_b.json"
            elif k == "READINESS_STATE_PATH":
                globals()[k] = globals()["STATE_DIR"] / "readiness_state.json"
        write_json(  # type: ignore[name-defined]
            RUN_STATE,
            {
                "current_head": AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
                "round103_resume": {
                    "resume_classification": "PR416_ROUND111_IN_PROGRESS",
                },
            },
        )

    if not acquire_lock():
        log(
            "warning",
            "another supervisor already owns this PR; exiting",
        )
        return 0

    # Round-40: reconcile AUTHORITATIVE_HEAD against
    # canonical durable + live evidence at boot. The
    # bootstrap env var ``AED_AUTHORITATIVE_HEAD`` may be
    # stale (a worker push advanced the head after the
    # systemd unit was written); the live PR head is the
    # canonical truth. This runs ONCE per supervisor
    # lifetime, BEFORE the heartbeat loop, so the rest of
    # the supervisor sees a single, reconciled value.
    _reconcile_authoritative_head_at_boot()

    log(
        "info",
        "supervisor started (source-controlled v1)",
        session_id=SESSION_ID,  # type: ignore[name-defined]
        head=AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
        instance=INSTANCE_ID,  # type: ignore[name-defined]
        providers=list(PROVIDERS.keys()),
        policy=POLICY,
    )

    quiet_window = POLICY["quiet_window_seconds"]
    heartbeat_seconds = POLICY["heartbeat_seconds"]
    # Round-32: env override for heartbeat interval
    # (used to tune the supervisor's responsiveness in
    # tests and emergency reactivation). Production uses
    # the cfg default (~120s).
    try:
        env_hb = int(os.environ.get("AED_HEARTBEAT_SECONDS", "0"))
        if env_hb > 0:
            heartbeat_seconds = env_hb
    except (ValueError, TypeError):
        pass

    try:
        # Round-32: the production main loop honors the
        # durable recoverable-retry ledger. Before each
        # iteration it reads ``round_budget_retry.json``
        # and either:
        # - bumps the slice_epoch (starting a fresh
        #   slice with a fresh budget) so the next
        #   ``_invoke_relay_for_events`` call can do
        #   real work; OR
        # - sleeps until ``next_eligible_retry_at``
        #   (avoiding tight-loop spam).
        # The scheduler is autonomous: no human prompt
        # is required for either branch.
        try:
            from autocoder_orchestration.review_repair_relay import (
                bump_slice_epoch,
                read_round_budget_retry,
            )
            from datetime import datetime as _dt
        except Exception:  # noqa: BLE001 - defensive
            bump_slice_epoch = None  # type: ignore[assignment]
            read_round_budget_retry = None  # type: ignore[assignment]
            _dt = None  # type: ignore[assignment]
        while True:
            heartbeat_touch()
            # Round-36: poll any active worker attempt and finalize
            # dead workers. The poll transitions WORKER_RUNNING ->
            # WORKER_EXITED_NO_PUSH when the pid is gone, releases
            # the lease, unmarks launched events, and persists
            # exit_code/signal for diagnostics. Without this the
            # stale lease would mask the durable work item forever.
            try:
                cur_lease = read_lease()
                if cur_lease is not None:
                    cur_attempt_id = cur_lease.get("attempt_id") or ""
                    if cur_attempt_id:
                        poll_worker_attempt(
                            attempt_id=cur_attempt_id,
                            lease=cur_lease,
                        )
            except Exception as exc:  # noqa: BLE001
                try:
                    log(
                        "warning",
                        "poll_worker_attempt failed",
                        error=str(exc),
                    )
                except Exception:  # noqa: BLE001
                    pass
            # Round-52/C20 §3, §6: reconcile WORKER_RUNNING
            # records that no longer have a live lease. The
            # poll branch above is gated on ``cur_lease is not
            # None``; if the lease has been removed out-of-band
            # (e.g. supervisor restart, lease-rotation crash),
            # the poll alone misses those records. The
            # reconciliation scans the worker_attempts
            # directory for any WORKER_RUNNING record whose pid
            # is dead and drives the full finalization chain
            # (canonical ingestion -> classify -> mark
            # PUSH_VERIFIED -> head rebind -> drain consume ->
            # remote thread resolve). Runs at the top of
            # every heartbeat BEFORE round scheduling, so
            # completed-worker processing is independent of
            # the round decision.
            try:
                reconcile_orphaned_worker_attempts()
            except Exception as exc:  # noqa: BLE001
                try:
                    log(
                        "warning",
                        "round-52: reconcile_orphaned_worker_attempts failed",
                        error=str(exc),
                    )
                except Exception:  # noqa: BLE001
                    pass
            # Round-32: honor the durable retry ledger.
            # The retry ledger carries the
            # ``next_eligible_retry_at`` timestamp; the
            # supervisor MUST NOT retry before it. When
            # the timestamp has elapsed, the supervisor
            # bumps the slice_epoch (starting a fresh
            # slice with a fresh budget) and continues.
            #
            # Round-43 C11 fix: the retry ledger lives in
            # the PER-PR orch state root
            # (``orchestration_state_root/evidence``),
            # NOT in ``RUN_STATE.parent / "evidence"``.
            # The supervisor's prior path resolved to a
            # global empty directory that the relay CLI
            # never reads, so the slice_epoch never
            # bumped and the budget-exhausted ledger
            # caused a permanent ``recoverable_retry``
            # stall. Resolve the per-PR evidence root
            # via the same canonical resolver used by
            # ``_clear_stale_retry_ledgers`` so the
            # supervisor and relay share one ledger
            # path per PR. When multiple PRs are owned
            # in a single heartbeat, the supervisor
            # honors the union of their
            # ``next_eligible_retry_at`` windows and
            # bumps each PR's ledger independently so
            # the per-slice budget is fresh on every PR.
            # Round-43 C11: delegate the per-PR evidence
            # root resolution to ``_resolve_per_pr_evidence_roots``
            # so the supervisor's main heartbeat loop reads
            # and writes the durable retry ledger at the same
            # paths the relay CLI uses (one path per PR).
            _pr_evidence_roots = _resolve_per_pr_evidence_roots(
                run_state_path=Path(RUN_STATE),  # type: ignore[name-defined]
                pr_numbers=(
                    list(PR_NUMBERS)  # type: ignore[name-defined]
                    if PR_NUMBERS  # type: ignore[name-defined]
                    else [int(PR_NUMBER)]  # type: ignore[name-defined]
                ),
                expected_repo=(
                    f"{REPO_OWNER}/{REPO_NAME}"  # type: ignore[name-defined]
                ),
                fallback_root=str(RUN_STATE.parent / "evidence"),  # type: ignore[name-defined]
            )
            now = now_iso()
            next_eligible = ""
            for _root in _pr_evidence_roots:
                _rs = (
                    read_round_budget_retry(_root)
                    if read_round_budget_retry is not None
                    else None
                ) or {}
                _ne = str(_rs.get("next_eligible_retry_at") or "")
                if _ne and (not next_eligible or _ne > next_eligible):
                    next_eligible = _ne
            if (
                next_eligible
                and _dt is not None
                and next_eligible != now
            ):
                try:
                    now_dt = _dt.fromisoformat(
                        now.replace("Z", "+00:00"),
                    )
                    next_dt = _dt.fromisoformat(
                        next_eligible.replace("Z", "+00:00"),
                    )
                    delay = (next_dt - now_dt).total_seconds()
                    if delay > 0:
                        # Sleep until the next eligible
                        # retry time, capped by the
                        # heartbeat interval so the
                        # heartbeat_touch() cadence is
                        # maintained. The supervisor MUST
                        # NOT exit and MUST NOT return to
                        # the operator during this sleep.
                        sleep_secs = min(delay, heartbeat_seconds)
                        log(
                            "info",
                            "supervisor honoring next_eligible_retry_at; "
                            "sleeping until slice can resume",
                            sleep_secs=sleep_secs,
                            next_eligible_retry_at=next_eligible,
                        )
                        time.sleep(sleep_secs)
                        # Loop back: re-read the retry
                        # state in case the slice was
                        # bumped while we slept.
                        continue
                except Exception:  # noqa: BLE001
                    # Parse error: fall through and
                    # attempt the slice bump.
                    pass
            # If we're past the retry window, bump the
            # slice_epoch so the next iteration has a
            # fresh budget. ``bump_slice_epoch`` is
            # idempotent and durable.
            #
            # Round-33 P1#2: a retry record with
            # lifecycle='cleared' must NOT trigger a
            # slice_epoch bump. The work has been
            # CONSUMED; bumping again would advance the
            # epoch on every heartbeat forever. Only
            # records that are still pending/active
            # participate in the slice-budget cycle.
            #
            # Round-43 C11: bump EVERY per-PR ledger
            # whose retry record is still pending/active,
            # so each PR's slice budget is reset for the
            # next iteration. The legacy single-root
            # bump is retained as a fallback only when
            # the resolver fails entirely.
            for _root in _pr_evidence_roots:
                _retry_state = (
                    read_round_budget_retry(_root)
                    if read_round_budget_retry is not None
                    else None
                ) or {}
                if (
                    bump_slice_epoch is not None
                    and _retry_state
                    and _retry_state.get("last_attempt_at")
                    and _retry_state.get("lifecycle")
                    not in ("cleared", "resolved", "consumed")
                ):
                    bump_slice_epoch(_root)

            rs = read_run_state()
            token = get_github_token()
            if token and read_snapshot("A") == {}:
                capture_and_store_snapshot("A", rs, token)

            # Round-32: per-PR iteration. The supervisor
            # owns ``PR_NUMBERS`` simultaneously (e.g.
            # ``AED_PR_NUMBERS=4,5``). For each owned PR
            # we temporarily rebind the singleton
            # ``PR_NUMBER`` global so the existing
            # ``run_iteration_v5`` / capture / handle
            # code paths address the correct PR. After
            # the iteration we restore the singleton.
            #
            # Each PR has its own orch state root (via
            # the canonical resolver) so the per-PR
            # state files do NOT collide.
            #
            # This is the structural fix for the
            # a019e63 stall: the supervisor was bound
            # to PR #416 only; PR #5 had no running
            # owner. With ``AED_PR_NUMBERS=4,5`` both
            # PRs share one heartbeat and one owner
            # process; the per-PR iteration runs
            # review→classify→repair→push for each.
            pr_numbers = (
                list(PR_NUMBERS)  # type: ignore[name-defined]
                if PR_NUMBERS  # type: ignore[name-defined]
                else [int(PR_NUMBER)]  # type: ignore[name-defined]
            )
            canonical_pr = int(PR_NUMBER)  # type: ignore[name-defined]
            iteration: dict = {}
            log(
                "info",
                "per_pr_iteration_start",
                pr_numbers=pr_numbers,
                canonical_pr=canonical_pr,
            )
            for this_pr in pr_numbers:
                if this_pr == 0:
                    continue
                try:
                    globals()["PR_NUMBER"] = this_pr
                    log(
                        "info",
                        "per_pr_iteration_tick",
                        this_pr=this_pr,
                    )
                    # Round-32: stall watchdog. Before
                    # each per-PR tick, write a
                    # ``orchestration_owner.json`` so
                    # external observers can see WHO
                    # owns the next transition and WHEN
                    # the next action is scheduled. A
                    # hard defect is detected when
                    # state is non-terminal AND no
                    # worker is active AND no retry is
                    # scheduled AND no poll is pending
                    # AND no external condition is
                    # documented.
                    _write_orchestration_owner(this_pr)
                    # Run the per-PR iteration. The
                    # iteration drives the review-wait
                    # poll, classification, repair
                    # dispatch, and CI watch.
                    iteration = run_iteration_v5(
                        rs, token or "",
                    )
                    # Clear stale retry state after a
                    # successful relay round (the
                    # relay's success path persists
                    # ``cleared`` status; this
                    # supervisor-side sweep catches
                    # stale ledgers from prior failed
                    # invocations).
                    _clear_stale_retry_ledgers(this_pr)
                finally:
                    globals()["PR_NUMBER"] = canonical_pr
            cur_state = (
                read_readiness_state().get("state")
                or STATE_ACTIVE_REPAIR
            )
            # Round-41: if the orchestration CONTROLLER is in
            # AWAITING_CI and the live head is stable at the
            # authoritative head, drive the CI-advance
            # transition so the controller can reach
            # QUALIFYING_READINESS. Without this, the supervisor
            # can be stuck in AWAITING_CI after a verified
            # worker push because the head-rebind path fires
            # once per head advance and is silent on subsequent
            # heartbeats. The CI policy is evaluated via
            # ``_advance_awaiting_ci_to_qualifying`` which
            # already uses ``ci_policy_status`` (round-39).
            try:
                _ctrl_state = "AWAITING_CI"
                try:
                    from .orchestration_state_root import (
                        resolve_orchestration_state_root,
                    )
                    _state_root = resolve_orchestration_state_root(
                        run_state_path=Path(RUN_STATE),
                        expected_repo=(
                            f"{REPO_OWNER}/{REPO_NAME}"
                        ),
                        expected_pr_number=int(PR_NUMBER),
                    )
                    _state_path = (
                        Path(_state_root) / "state.json"
                    )
                    if _state_path.is_file():
                        _ctrl_state = json.loads(
                            _state_path.read_text(),
                        ).get("current_state", _ctrl_state)
                except Exception:  # noqa: BLE001
                    pass
                if _ctrl_state == "AWAITING_CI":
                    try:
                        if iteration.get("head_match"):
                            _advance_awaiting_ci_to_qualifying()
                    except Exception as exc:  # noqa: BLE001
                        try:
                            log(
                                "warning",
                                "round-41 awaiting_ci idle advance failed",
                                error=str(exc)[:200],
                            )
                        except Exception:  # noqa: BLE001
                            pass
            except Exception:  # noqa: BLE001
                pass
            # Round-34: drain a SINGLE durable unresolved
            # thread even when the GitHub delta is empty.
            # detect_new_actionable_events only surfaces
            # threads that newly appear or re-open;
            # pre-existing unresolved threads disappear
            # from the delta and become invisible to the
            # dispatcher. This drain picks ONE thread
            # (sorted by id for determinism) per heartbeat
            # so we never burst-launch multiple workers
            # against the same branch/head/controller
            # state. The synthetic event flows through
            # the SAME lifecycle as a real GitHub event:
            # persist -> dispatch -> relay -> directive
            # -> worker. Only the event source differs
            # (source="durable_drain"). Satisfies the
            # user's invariant: "Every heartbeat must
            # therefore do BOTH:
            #   A. ingest newly discovered GitHub events
            #      idempotently;
            #   AND
            #   B. drain all due durable nonterminal
            #      events."
            # The "ONE thread per heartbeat" cap is the
            # anti-burst guard: a heartbeat can drain at
            # most one durable unresolved thread; the
            # next heartbeat drains the next.
            #
            # Fall back to the cached snapshot when the
            # live fetch is unavailable (GitHub 401,
            # rate-limit, transient network). The cached
            # snapshot is durable and reflects the last
            # known thread state. This is the user's
            # fail-closed invariant: corrupt/unreachable
            # metadata MUST NOT lose pending work; the
            # event MUST survive and eventually dispatch.
            try:
                _drain_token = get_github_token() if "get_github_token" in dir() else ""
            except Exception:
                _drain_token = ""
            snap_for_drain = capture_live_snapshot(
                rs if isinstance(rs, dict) else {},
                _drain_token or "",
            )
            if not snap_for_drain.get("review_threads"):
                cached = read_snapshot("A") or {}
                if cached.get("review_threads"):
                    snap_for_drain = cached
                    log(
                        "info",
                        "round-34 durable-thread drain: using "
                        "cached snapshot (live fetch returned "
                        "empty)",
                        cached_head=(cached.get("head_sha") or "")[:12],
                    )
            already_launched = launched_event_ids()
            drain_events: list = []
            threads = (snap_for_drain.get("review_threads") or {})
            # Sort by thread id for determinism.
            unresolved_ids = sorted(
                tid for tid, td in threads.items()
                if not td.get("resolved") and not td.get("outdated")
            )
            # Round-35: only emit a drain event when the
            # thread has REAL actionable evidence.
            # Specifically: a non-empty body OR a path OR
            # a commit_oid that matches the current head.
            # Blank threads (no path, no body, no head
            # binding) are not actionable findings and must
            # NOT be synthesized into no-op repair cycles.
            # They are also excluded from the synthetic
            # drain queue.
            current_head = (
                iteration.get("head_sha")
                or snap_for_drain.get("head_sha")
            )
            actionable_unresolved = []
            for tid in unresolved_ids:
                td = threads[tid] or {}
                body = (td.get("body") or "").strip()
                path = (td.get("path") or "").strip()
                line = td.get("line")
                commit_oid = td.get("commit_oid")
                # Real evidence: the thread carries content.
                if not body and not path:
                    continue
                # Or: the thread is bound to the current
                # head even if its body is blank.
                if (
                    commit_oid
                    and current_head
                    and commit_oid == current_head
                ):
                    pass
                elif not body and not path:
                    continue
                # Round-49.1 C17: source-safe carry-forward.
                # The new contract requires:
                #   (1) a recorded THREAD_PROOF at a proof_head;
                #   (2) proof_head is an ancestor of current_head;
                #   (3) source blob at current_head == source blob
                #       at proof_head (Tier 1);
                #   (4) provider thread version fingerprint is
                #       unchanged.
                # The snapshot has body/path/line/isResolved/
                # isOutdated/commit_oid/author only; the
                # provider version fingerprint here uses what is
                # available (body[:FULL] + commit_oid + resolved
                # + outdated). When the fingerprint changes (new
                # commit, different resolution state, materially
                # different body), carry-forward is invalidated
                # and a fresh worker dispatch is permitted.
                # When ALL conditions hold, the thread is
                # skipped (no drain event) and an audit record is
                # written (THREAD_PROOF_CARRIED_FORWARD).
                #
                # Round-50 bug-detector: the C17 carry-forward
                # helper may invalidate the same thread on
                # successive heartbeats (provider version
                # mismatch is detected at the snapshot level, not
                # the worker level). The durable-thread drain
                # emitter would then re-emit the same drain event
                # every heartbeat, looping forever. To prevent
                # that, after an invalidation we ALSO consult the
                # audit ledger: if a THREAD_PROOF_RECORDED with
                # a terminal disposition already exists for this
                # thread, we honor the existing proof and skip
                # the drain event. The supervisor will refresh
                # the snapshot to pick up any new provider or
                # source changes that genuinely need a fresh
                # worker round.
                if current_head:
                    _current_pt = {
                        "provider": "coderabbit",
                        "id": tid,
                        "top_level_comment": {
                            "id": "",
                            "updatedAt": td.get("updatedAt") or "",
                            "body": td.get("body") or "",
                        },
                        "replies": [],
                        "isResolved": bool(td.get("resolved")),
                        "isOutdated": bool(td.get("outdated")),
                    }
                    _cf_decision, _cf_tier_or_reason = (
                        _try_carry_forward_thread_proof(
                            thread_id=tid,
                            provider="coderabbit",
                            current_path=path,
                            current_line=line,
                            current_provider_thread=_current_pt,
                            current_head=current_head,
                            disposition="",
                        )
                    )
                    if _cf_decision == "carry":
                        # Emit an audit-only marker that
                        # documents the carry decision; do not
                        # emit a drain event.
                        continue
                    if _cf_decision == "invalidate" and (
                        _cf_tier_or_reason
                        == INVALIDATION_REASON_PRIOR_PROOF_MISSING
                    ):
                        # Round-50 narrow fix: the C17 ledger has
                        # no record for this thread; the
                        # invalidation is the FIRST emission. If
                        # the audit ledger already has a
                        # THREAD_PROOF_RECORDED, the proof exists
                        # locally and we honor it. Otherwise, the
                        # drain event is emitted exactly once.
                        # On subsequent heartbeats, the recorded
                        # proof check short-circuits the loop.
                        if _has_recorded_thread_proof(tid):
                            continue
                        # Round-50.1 (Section 12 exactly-once
                        # work generation): if there is an
                        # OPEN work generation for this thread
                        # at the CURRENT HEAD, the runnable
                        # event already exists. Skip emitting.
                        # Heartbeats must not duplicate work.
                        if _round50_1_has_open_work_generation(
                            tid, current_head
                        ):
                            continue
                actionable_unresolved.append(tid)
            for tid in actionable_unresolved:
                eid = f"unresolved_thread_drain:{tid}"
                if eid in already_launched:
                    continue
                td = threads[tid] or {}
                drain_events.append({
                    "id": eid,
                    "kind": "unresolved_thread_drain",
                    "thread_id": tid,
                    "source": "durable_drain",
                    "head_sha": current_head,
                    "path": td.get("path"),
                    "line": td.get("line"),
                    "body": td.get("body"),
                    "commit_oid": td.get("commit_oid"),
                    "author": td.get("author"),
                })
                break  # ONE per heartbeat (anti-burst)
            if drain_events:
                # Persist the synthetic drain event into
                # the unconsumed ledger using the real
                # writer. Idempotent: write_unconsumed_event
                # dedups on event id.
                for ev in drain_events:
                    write_unconsumed_event(ev)
                log(
                    "info",
                    "round-34 durable-thread drain",
                    count=len(drain_events),
                    thread_id=drain_events[0].get("thread_id"),
                    head=iteration.get("head_sha", "")[:12],
                )
            new_events = list(iteration.get("events", [])) + drain_events
            # Round-39 P1#3: supplement ``new_events`` with
            # any durable unconsumed events that have not yet
            # been launched. ``run_iteration_v5`` derives
            # ``events`` from snapshot deltas only; events that
            # were re-persisted by a ``no_action`` /
            # recoverable_retry / unknown_relay_action path
            # (``_persist_round_budget_retry`` keeps them
            # actionable) would otherwise sit silent until the
            # next genuine GitHub delta. The launched set is
            # consulted so already-dispatched events are not
            # re-routed.
            try:
                _launched = launched_event_ids()
                for _ev in list_unconsumed_events():
                    if not isinstance(_ev, dict):
                        continue
                    _eid = _ev.get("id")
                    if not _eid or _eid in _launched:
                        continue
                    if any(
                        isinstance(x, dict) and x.get("id") == _eid
                        for x in new_events
                    ):
                        continue
                    new_events.append(_ev)
            except Exception:  # noqa: BLE001
                # Failure to read the unconsumed ledger MUST
                # NOT break the heartbeat. The snapshot-delta
                # path still works.
                pass
            paused = [
                p for p, st in (
                    read_quota_state().get("providers") or {}
                ).items()
                if st
            ]
            log(
                "info",
                "iteration",
                decision=iteration.get("decision"),
                state=cur_state,
                head=rs.get("current_head"),
                live_head=iteration.get("head_sha"),
                new_event_count=len(new_events),
                paused_providers=paused,
            )

            # Head rebinding. Round-36 invariant:
            #   HEAD_ADVANCED != REPAIR_PUSHED.
            #
            # A generic branch head advance (Humphry infrastructure
            # commit, operator commit, recovery commit, external actor)
            # MUST NOT trigger ``report_repair_pushed``. The supervisor
            # rebinds AUTHORITATIVE_HEAD so the next round operates on
            # the new head, but the controller transition
            # REPAIRING_REVIEW_FINDINGS -> AWAITING_CI only fires
            # when an active WorkerAttemptRecord has positive
            # ``PUSH_VERIFIED`` provenance for this head advance.
            live_head = iteration.get("head_sha")
            if (
                live_head
                and isinstance(live_head, str)
                and live_head != AUTHORITATIVE_HEAD  # type: ignore[name-defined]
                and _HEX_SHA_RE.match(live_head)  # type: ignore[name-defined]
            ):
                old_head = AUTHORITATIVE_HEAD  # type: ignore[name-defined]
                globals()["AUTHORITATIVE_HEAD"] = live_head
                # Round-36: attempt to verify that the head advance
                # was produced by an active worker attempt. The
                # verification chain reads ``origin/<branch>`` and
                # checks it against the active attempt's
                # produced_commit_sha. Only when provenance is
                # positive do we call ``mark_head_advanced_public``
                # with the attempt_id; otherwise we treat the head
                # advance as unrelated / manual and only rebind
                # AUTHORITATIVE_HEAD.
                attempt_id_for_ack: Optional[str] = None
                try:
                    active = find_active_worker_attempt_for_head(
                        old_head,
                    )
                    if active is not None:
                        attempt_id_for_ack = active.get(
                            "attempt_id"
                        )
                        # Verify the new head matches the attempt's
                        # produced_commit_sha (or pushed_commit_sha)
                        # AND that origin/<branch> resolves to it.
                        if attempt_id_for_ack:
                            v = verify_push_against_attempt(
                                attempt_id=attempt_id_for_ack,
                                new_head_sha=live_head,
                            )
                            if (
                                v is None
                                or not v.get("github_head_verified")
                            ):
                                attempt_id_for_ack = None
                            else:
                                # Mark the attempt PUSH_VERIFIED so
                                # mark_head_advanced_public can
                                # acknowledge the push.
                                finalize_worker_attempt_pushed(
                                    attempt_id=attempt_id_for_ack,
                                    pushed_commit_sha=live_head,
                                    produced_commit_sha=(
                                        v.get("produced_commit_sha")
                                        or live_head
                                    ),
                                    origin_head_verified=True,
                                    github_head_verified=True,
                                )
                except Exception as exc:
                    log(
                        "warning",
                        "attempt provenance check failed; "
                        "treating head advance as unrelated",
                        error=str(exc),
                        old_head=old_head[:12] if old_head else "",
                        new_head=live_head[:12],
                    )
                    attempt_id_for_ack = None
                if attempt_id_for_ack is not None:
                    try:
                        from .relay_wiring import (
                            mark_head_advanced_public,
                        )
                        ack = mark_head_advanced_public(
                            old_head, live_head,
                            attempt_id=attempt_id_for_ack,
                        )
                        if ack:
                            log(
                                "info",
                                "head advance acknowledged as "
                                "verified worker repair push",
                                old_head=old_head[:12] if old_head else "",
                                new_head=live_head[:12],
                                attempt_id=attempt_id_for_ack,
                            )
                        else:
                            log(
                                "warning",
                                "mark_head_advanced_public returned "
                                "False; controller did NOT record "
                                "repair_pushed",
                                old_head=old_head[:12] if old_head else "",
                                new_head=live_head[:12],
                                attempt_id=attempt_id_for_ack,
                            )
                    except Exception as exc:
                        log(
                            "warning",
                            "mark_head_advanced failed; controller "
                            "state may not match",
                            old_head=old_head[:12] if old_head else "",
                            new_head=live_head[:12],
                            error=str(exc),
                            attempt_id=attempt_id_for_ack,
                        )
                    # Round-41: after a verified worker push,
                    # the controller is in AWAITING_CI. Drive
                    # ``_advance_awaiting_ci_to_qualifying`` so
                    # the controller advances to
                    # QUALIFYING_READINESS. The new CI policy
                    # evaluation (NO_REQUIRED_CHECKS for the
                    # empty-policy case) allows qualification
                    # without GitHub check-runs.
                    try:
                        _advance_awaiting_ci_to_qualifying()
                    except Exception as exc:  # noqa: BLE001
                        try:
                            log(
                                "warning",
                                "round-41 awaiting_ci advance failed; "
                                "controller may be stuck in AWAITING_CI",
                                old_head=old_head[:12] if old_head else "",
                                new_head=live_head[:12],
                                error=str(exc)[:200],
                            )
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    log(
                        "info",
                        "head rebind without worker provenance; "
                        "AUTHORITATIVE_HEAD updated but "
                        "report_repair_pushed NOT called",
                        old_head=old_head[:12] if old_head else "",
                        new_head=live_head[:12],
                    )
                # Persist the rebind in the supervisor's
                # run_state.json so a restart picks it up.
                try:
                    rs["current_head"] = live_head
                    write_json(  # type: ignore[name-defined]
                        RUN_STATE,  # type: ignore[name-defined]
                        rs,
                    )
                except (OSError, TypeError) as exc:
                    log(
                        "warning",
                        "could not persist AUTHORITATIVE_HEAD rebind",
                        old_head=old_head[:12] if old_head else "",
                        new_head=live_head[:12],
                        error=str(exc),
                    )
                log(
                    "info",
                    "AUTHORITATIVE_HEAD rebinding",
                    old_head=old_head[:12] if old_head else "",
                    new_head=live_head[:12],
                    verified_worker_push=(
                        attempt_id_for_ack is not None
                    ),
                )
                # Round-37: when the head advance was NOT a
                # verified worker push (e.g. a manual/Humphry
                # commit), the controller may be stuck in
                # AWAITING_CI from the previous round's
                # transition. Drive
                # ``report_ci_pass`` directly so the
                # controller advances to
                # QUALIFYING_READINESS without depending on
                # GitHub check-runs (which may be absent).
                try:
                    if attempt_id_for_ack is None:
                        _advance_awaiting_ci_to_qualifying()
                except Exception as exc:  # noqa: BLE001
                    try:
                        log(
                            "warning",
                            "round-37 awaiting_ci advance failed",
                            error=str(exc),
                        )
                    except Exception:  # noqa: BLE001
                        pass

            if args.dry_sim:
                # --dry-sim: print the decision and skip every
                # state-mutating step (no worker launch, no event
                # mark, no review request). The flag is honoured
                # before any lease / cooldown / launch_worker
                # branch so a real worker is never produced.
                log(
                    "info",
                    "dry-sim: would launch worker",
                    events=[
                        e.get("kind") for e in new_events
                        if e.get("id") in (
                            e.get("id") for e in new_events
                            if e.get("id") and e.get("id")
                            not in launched_event_ids()
                        )
                    ] if False else [
                        e.get("kind") for e in new_events
                    ],
                    head=iteration.get("head_sha"),
                    state=cur_state,
                )
                if args.once:
                    return 0
                time.sleep(heartbeat_seconds)
                continue
            # Sync the supervisor's readiness state with the
            # orchestration controller's state. When the
            # relay escalates to human, the controller is in
            # BLOCKED. The supervisor's readiness state must
            # reflect this so the quiet-window logic does not
            # promote readiness in subsequent heartbeats.
            _sync_readiness_state_with_controller()
            # Round-32: provider cooldown detection. The
            # supervisor MUST process provider quota /
            # pause / cooldown messages from the live
            # snapshot on every iteration. The previous
            # design defined ``process_provider_quotas`` +
            # ``handle_paused_providers`` but never called
            # them in the main loop, so CodeRabbit's
            # "review limit reached" comment did not
            # persist a cooldown ledger or schedule a
            # recovery request. ``handle_paused_providers``
            # itself issues the canonical provider-request
            # via ``post_review_request`` (a real ``gh pr
            # comment``) once the cooldown window elapses.
            try:
                _live_for_quota = capture_live_snapshot(
                    rs, token or "",
                )
                quota_statuses = process_provider_quotas(
                    _live_for_quota,
                )
                _any_paused, _paused = handle_paused_providers(
                    _live_for_quota, quota_statuses,
                )
            except Exception as exc:
                log(
                    "warning",
                    "process_provider_quotas/handle_paused_providers failed; "
                    "supervisor continues",
                    error=str(exc),
                )
            if new_events and not cooldown_active():
                handle_new_events(rs, new_events, token, iteration)
            elif new_events and cooldown_active():
                # Round-33 P1#1 (cooldown-skipped event loss):
                # events that arrive during cooldown are
                # persisted (write_unconsumed_event in
                # run_iteration_v5) but the dispatch is
                # skipped. They MUST NOT be cleared by the
                # quiet-window post-loop clear. Track them
                # as cooldown-deferred so the next
                # post-loop clear preserves them, and the
                # SAME event dispatches automatically the
                # moment cooldown expires (exactly one
                # ownership transition: PENDING ->
                # dispatched when cooldown lifts).
                _mark_cooldown_deferred(new_events)

            # Round-39 P1#2: replay cooldown-deferred events
            # when cooldown expires. The deferred ledger
            # records events that were skipped during
            # cooldown; without this branch, the next
            # iteration's snapshot A absorbs the new event
            # so ``detect_new_actionable_events`` returns
            # no event (the snapshot deltas already include
            # it), and the deferred ledger becomes a
            # permanent tombstone. When cooldown has
            # expired, merge the deferred events into the
            # unconsumed-events ledger with full payload
            # so handle_new_events can dispatch them on
            # the SAME heartbeat. Exactly-one ownership:
            # PENDING -> dispatched, never twice.
            if not cooldown_active():
                _replay_cooldown_deferred_if_any()

            # Round-51/C19 Objective 2: REPAIR-BEFORE-QUALIFICATION.
            # If runnable repair work exists, dispatch it
            # immediately and SKIP the quiet window for this
            # iteration. The 180s qualification quiet window
            # applies ONLY AFTER runnable repair work has
            # reached zero. CI/check changes must not
            # indefinitely prevent known repair work from
            # launching. The previous design gated all
            # dispatch on the qualifying interval, so a
            # ``check_conclusion_change`` arriving every
            # ~13s from CI checks would reset the window
            # forever and strand ``unresolved_thread_drain``
            # events in the unconsumed ledger.
            if cur_state == STATE_ACTIVE_REPAIR:
                _runnable_repair = (
                    _has_runnable_repair_generation()
                    if not cooldown_active()
                    else False
                )
                if _runnable_repair and new_events:
                    # Runnable repair exists AND we have
                    # fresh events. Dispatch now (the
                    # ``handle_new_events`` branch above
                    # already did this on the same iteration
                    # when new_events was non-empty; this
                    # is a defensive second-chance). Then
                    # SKIP the quiet window entirely for
                    # this iteration. The window only
                    # applies when there is NO runnable
                    # repair work pending.
                    log(
                        "info",
                        "round-51: skipping quiet window; "
                        "runnable repair work exists",
                        event_count=len(new_events),
                    )
                    pre_unconsumed_ids = {
                        e.get("id") for e in list_unconsumed_events()
                    }
                    quiet_window_outcome = None
                else:
                    pre_unconsumed_ids = {
                        e.get("id") for e in list_unconsumed_events()
                    }
                    quiet_window_outcome = active_repair_quiet_window(
                        rs, token or "", quiet_window, pre_unconsumed_ids,
                )
                # If a new event arrived during the window,
                # the relay preserved it. The next iteration
                # sees it in new_events and routes to
                # handle_new_events. The escalation / None
                # outcomes are handled inline.
                if quiet_window_outcome == "escalation":
                    log(
                        "warning",
                        "main loop paused: controller in BLOCKED",
                    )

            elif cur_state in READINESS_STATES:
                snap_now = capture_live_snapshot(rs, token or "")
                reasons = snapshot_differs(
                    read_snapshot("A"), snap_now,
                    AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
                )
                if reasons:
                    revoke_readiness(
                        reason="snapshot_drift",
                        head_sha=snap_now.get("head_sha"),
                    )
                    log(
                        "info",
                        "revoking readiness (snapshot drift in "
                        "heartbeat)",
                        reasons=reasons,
                    )
                else:
                    result = evaluate_readiness(
                        snap_now, AUTHORITATIVE_HEAD  # type: ignore[name-defined]
                    )
                    if not result.get("ready"):
                        revoke_readiness(
                            reason=result.get("reason"),
                            head_sha=snap_now.get("head_sha"),
                        )
                        log(
                            "info",
                            "revoking readiness "
                            "(evaluate_readiness failed)",
                            reason=result.get("reason"),
                        )
                    elif cur_state == STATE_PROVISIONAL_READY:
                        enter_readiness(
                            STATE_AWAITING_MERGE_AUTHORIZATION,
                            head_sha=snap_now.get("head_sha"),
                        )
                        log(
                            "info",
                            "PROVISIONAL_READY remained stable; "
                            "promoting to AWAITING_MERGE_AUTHORIZATION",
                        )
                    else:
                        write_snapshot("A", snap_now)

            if args.once:
                return 0
            time.sleep(heartbeat_seconds)
    finally:
        log("info", "supervisor exiting")


if __name__ == "__main__":
    sys.exit(main())
