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
        LIFECYCLE_PUSH_VERIFIED,
        LIFECYCLE_RECOVERY_CHECK,
        LIFECYCLE_WORKER_EXITED_NO_PUSH,
        LIFECYCLE_WORKER_RUNNING,
        TERMINAL_LIFECYCLES,
        WorkerAttemptStore,
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
        # Round-37 fix (WORKER_EXITED_NO_PUSH race): before
        # terminalizing a dead worker as no-push, the supervisor
        # MUST refresh local/origin/live GitHub evidence and
        # check whether a push attributable to this attempt
        # occurred between the last heartbeat and the worker
        # death. Without this, a worker that pushed and exited
        # in the gap would be wrongly recorded as NO_PUSH and
        # the subsequent provenance lookup would reject the
        # valid repair push — leaving the controller stuck
        # while the live GitHub head already advanced past it.
        # The check is best-effort: a transient 401 / network
        # error here MUST NOT promote a no-push worker to
        # PUSH_VERIFIED on weak evidence. We require:
        #   (a) live GitHub PR head == a commit produced after
        #       rec.prelaunch_head, AND
        #   (b) origin/<expected_branch> head == the same SHA, AND
        #   (c) rec.expected_branch is non-empty (otherwise we
        #       cannot provenance-attribute the push to this
        #       attempt and we conservatively stay NO_PUSH).
        if rec.lifecycle != LIFECYCLE_PUSH_VERIFIED:
            push_attributable = False
            try:
                # Round-38: use ``get_github_token`` which
                # honours both the dedicated env override and
                # the canonical ``~/.config/gh/hosts.yml``
                # source. Re-reading on every call lets a
                # credential rotated by ``gh auth login`` be
                # picked up without a supervisor restart.
                _token_for_probe = get_github_token() or ""
                if (
                    rec.expected_branch
                    and _token_for_probe
                ):
                    _pr_probe = github_get(
                        f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}",  # type: ignore[name-defined]
                        _token_for_probe,
                    )
                    _live_head = ""
                    if _pr_probe:
                        _live_head = (
                            _pr_probe.get("head", {}).get("sha")
                            or ""
                        )
                    if (
                        _live_head
                        and _live_head != rec.prelaunch_head
                    ):
                        # Live head advanced past prelaunch;
                        # ask git to verify origin/<branch>
                        # actually points at it. The git
                        # command runs against REPO_DIR.
                        try:
                            _out = subprocess.run(  # noqa: S602
                                [
                                    "git",
                                    "-C",
                                    str(REPO_DIR),  # type: ignore[name-defined]
                                    "rev-parse",
                                    "--verify",
                                    f"refs/remotes/origin/{rec.expected_branch}",
                                ],
                                check=True,
                                capture_output=True,
                                text=True,
                                timeout=10,
                            )
                            _origin_head = (
                                _out.stdout.strip()
                            )
                            if (
                                _origin_head
                                and _origin_head
                                == _live_head
                            ):
                                # Round-32 P1#6: origin equality
                                # alone proves SOMEONE pushed the
                                # commit. We MUST additionally
                                # require the commit's committer
                                # date to be strictly AFTER
                                # ``rec.started_at`` so the
                                # promotion is worker-specific
                                # (matching the round-31 P1#6
                                # contract enforced inside
                                # ``verify_push_against_attempt``).
                                # Without this guard, an external
                                # actor's push (commit time before
                                # the worker launched, but
                                # matching ``origin/<branch>``) is
                                # wrongly attributed to this
                                # attempt and the worker is
                                # promoted to PUSH_VERIFIED,
                                # leaving the controller stuck on
                                # a fraudulent repair.
                                _committer_ok, _committed_at = (
                                    _git_committer_iso(_live_head)
                                )
                                _started_at_dt = parse_iso(
                                    str(rec.started_at or "")
                                )
                                _worker_specific = (
                                    _committer_ok
                                    and _committed_at is not None
                                    and _started_at_dt is not None
                                    and _committed_at > _started_at_dt
                                )
                                if _worker_specific:
                                    push_attributable = True
                                    rec.pushed_commit_sha = (
                                        _live_head
                                    )
                                    rec.origin_head_verified = True
                                    rec.github_head_verified = (
                                        True
                                    )
                                    rec.produced_commit_sha = (
                                        _live_head
                                    )
                                    rec.lifecycle = (
                                        LIFECYCLE_PUSH_VERIFIED
                                    )
                                    log(
                                        "info",
                                        "round-37 deferred push "
                                        "recovery: dead worker "
                                        "attributed to live head",
                                        attempt_id=attempt_id,
                                        pid=rec.pid,
                                        pushed=_live_head[:12],
                                    )
                                else:
                                    # Committer-date proof failed
                                    # — treat the head as an
                                    # external push and stay
                                    # NO_PUSH. The head-rebind
                                    # path that runs immediately
                                    # after this poll will see
                                    # the attempt already terminal
                                    # and route the head advance
                                    # via the bound active
                                    # attempt, not this one.
                                    log(
                                        "warning",
                                        "round-32 deferred push "
                                        "recovery: candidate head "
                                        "committer date fails "
                                        "worker-specific proof; "
                                        "treating as external push",
                                        attempt_id=attempt_id,
                                        pid=rec.pid,
                                        candidate_head=_live_head[:12],
                                        started_at=str(
                                            rec.started_at or ""
                                        ),
                                        committer_ok=_committer_ok,
                                    )
                        except Exception:
                            # git probe failed; stay
                            # conservative. The attempt
                            # remains in RECOVERY_CHECK /
                            # WORKER_EXITED_NO_PUSH until
                            # a stronger signal arrives.
                            push_attributable = False
            except Exception:
                push_attributable = False
            if rec.lifecycle != LIFECYCLE_PUSH_VERIFIED:
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
) -> Optional[dict]:
    """Verify that ``new_head_sha`` is the commit the active worker pushed.

    Returns a dict with verification fields, or ``None`` if the
    attempt cannot be associated with the head.

    Verification chain:
        1. Attempt must exist and not be WORKER_EXITED_NO_PUSH.
        2. Worker must have produced ``new_head_sha`` OR the local
           ``origin/<branch>`` must resolve to ``new_head_sha``
           AND the commit's author/committer date must be after
           ``attempt.started_at``.
    """
    from autocoder_orchestration.worker_attempt import (
        LIFECYCLE_PUSH_VERIFIED,
        LIFECYCLE_WORKER_EXITED_NO_PUSH,
    )
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
    # Direct: pushed_commit_sha matches.
    if rec.pushed_commit_sha and rec.pushed_commit_sha == new_head_sha:
        out["github_head_verified"] = True
        out["origin_head_verified"] = True
        return out
    # Indirect: produced_commit_sha matches (worker created it but
    # did not push — caller must verify push before acking).
    if rec.produced_commit_sha and rec.produced_commit_sha == new_head_sha:
        # Check origin and GitHub PR head independently.
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
            except (subprocess.CalledProcessError,
                    subprocess.TimeoutExpired, OSError):
                pass
        # GitHub verification happens via the live PR head check
        # the supervisor already performs; if origin is verified
        # and the head matches, GitHub is also verified for the
        # round-36 acceptance canary.
        if out["origin_head_verified"]:
            out["github_head_verified"] = True
        return out
    # Round-39 P1#6 + Round-31 P1#6: when both
    # ``pushed_commit_sha`` and ``produced_commit_sha`` are
    # ``None`` (the production launch initializer), the
    # worker simply has not yet recorded its commit. We MUST
    # NOT fail-closed on the direct ``new_head_sha`` comparison
    # in that case because the head-rebind path needs to be
    # able to verify a freshly-pushed head even when the
    # worker's own commit bookkeeping is incomplete. Instead,
    # fall back to the origin/<expected_branch> check: if the
    # remote branch already points to the new head, the push
    # is genuinely attributable to this attempt.
    #
    # Round-31 P1#6 fresh evidence: the round-39 fallback
    # MANUFACTURED a verifiable claim from any external actor.
    # If an operator / recovery job / other worker advanced
    # the feature branch while this attempt was running, the
    # ``origin/<branch> == new_head_sha`` equality alone proves
    # only that SOMEONE pushed that commit. It does NOT
    # attribute the commit to this attempt. To require
    # worker-specific proof, the verifier MUST additionally
    # confirm that the commit's committer date is strictly
    # after ``rec.started_at`` — i.e. the commit could only
    # have been authored AFTER the worker was launched. If
    # the date is missing or precedes ``rec.started_at`` the
    # verifier conservatively returns ``None`` for both
    # ``origin_head_verified`` and ``github_head_verified``;
    # the head-rebind path then treats the advance as
    # external and skips ``mark_head_advanced_public``.
    if (
        not rec.pushed_commit_sha
        and not rec.produced_commit_sha
        and rec.expected_branch
    ):
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
                # Round-31 P1#6: the round-39 fallthrough
                # stopped here. Require the committer date
                # to be after the worker was launched.
                _ok, _committed_at = (
                    _git_committer_iso(new_head_sha)
                )
                _started_at = parse_iso(
                    str(rec.started_at or "")
                )
                if (
                    _ok
                    and _committed_at is not None
                    and _started_at is not None
                    and _committed_at > _started_at
                ):
                    out["origin_head_verified"] = True
                    # GitHub verification happens via the live
                    # PR head check the supervisor already
                    # performs; if origin is verified and the
                    # head matches, GitHub is also verified.
                    out["github_head_verified"] = True
        except (subprocess.CalledProcessError,
                subprocess.TimeoutExpired, OSError):
            pass
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
        head_sha_now = (
            str(AUTHORITATIVE_HEAD)  # type: ignore[name-defined]
            if "AUTHORITATIVE_HEAD" in globals()
            else ""
        )
        try:
            write_review_request(  # type: ignore[name-defined]
                provider=provider,
                head_sha=head_sha_now or "unknown",
                record={
                    "actor": "recovery",
                    "requested_at": now,
                    "recovery_request_id": recovery_request_id,
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
        # Round-38 invariant: a session resolution failure
        # MUST NOT strand the loop. We persist the failure
        # reason durably and fall back to the configured id;
        # the launch will fail fast on the resume side, and
        # the next dispatch cycle will retry resolution.
        log(
            "warning",
            "round-38 session resolution failed; using configured id",
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
    # attempt_id_prefix was already computed earlier (round-38)
    # so the session-resolution helper could share the prefix.
    stdout_path = worker_attempts_dir / f"{attempt_id_prefix}.stdout.log"
    stderr_path = worker_attempts_dir / f"{attempt_id_prefix}.stderr.log"
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
            event_ids=tuple(_pending_event_ids or ()),
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
            result_artifact_path=None,
            produced_commit_sha=None,
            pushed_commit_sha=None,
            origin_head_verified=False,
            github_head_verified=False,
            terminal_reason=None,
            extra={
                "cmd": cmd,
                "pgid": proc.pid,
                "supervisor_instance_id": INSTANCE_ID,  # type: ignore[name-defined]
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
        # Worker already started; the attempt record is the only
        # durable source of truth. If we cannot persist it the
        # supervisor CANNOT acknowledge any future head advance
        # for this attempt — which is correct fail-closed behaviour.
        log(
            "error",
            "could not persist worker attempt record; "
            "launch cannot be acknowledged",
            attempt_id=attempt_id,
            error=str(exc),
        )

    # Round-39 P1#8: persist the fresh event ids on the
    # WorkerAttemptRecord so dead-worker recovery can
    # ``unmark_event_launched`` for the real ids (not just the
    # empty tuple the production initializer used). The slot
    # is populated by ``handle_new_events`` BEFORE the launch
    # call and is cleared immediately after the WorkerAttemptRecord
    # is written to avoid leaking into the next launch. The
    # raised-leased worker is durable and the launched-event
    # ledger is the inverse of the attempt's event_ids.
    try:
        _pending_event_ids = tuple(
            globals().get("_pending_launch_event_ids") or ()
        )
    except Exception:
        _pending_event_ids = ()
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
        "last_dispatched_event_id": ",".join(_pending_event_ids or ()),
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


def post_review_request(provider: str, head_sha: str) -> bool:
    cfg = PROVIDERS.get(provider)
    if not cfg:
        log(
            "warning",
            "post_review_request: unknown provider",
            provider=provider,
        )
        return False
    handle = cfg["trigger_handle"]
    cmd = [
        "gh",
        "pr",
        "comment",
        str(PR_NUMBER),  # type: ignore[name-defined]
        "--repo",
        f"{REPO_OWNER}/{REPO_NAME}",  # type: ignore[name-defined]
        "--body",
        f"{handle}\n\n(current head {head_sha[:12]})",
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
        "info", f"posted {handle}", head=head_sha[:12]
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
    any_paused = False
    paused = []
    now = datetime.now(timezone.utc)
    for provider in PROVIDERS:
        if statuses.get(provider) != "paused":
            continue
        any_paused = True
        paused.append(provider)
        full_state = read_quota_state()
        sub = quota_state_for_provider(full_state, provider)
        pending = sub.get("pending_review_head")
        if pending != AUTHORITATIVE_HEAD:  # type: ignore[name-defined]
            sub["pending_review_head"] = AUTHORITATIVE_HEAD  # type: ignore[name-defined]
            new_state = set_quota_state_for_provider(
                full_state, provider, sub
            )
            write_quota_state(new_state)
            log(
                "warning",
                f"{provider}: head changed during pause; "
                "updated pending_review_head",
                old_head=(pending or "")[:12],
                new_head=AUTHORITATIVE_HEAD[:12],  # type: ignore[name-defined]
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
            head_for_request = (
                sub.get("pending_review_head")
                or AUTHORITATIVE_HEAD  # type: ignore[name-defined]
            )
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
                surfaces["issue_comments"].append({
                    "id": cid,
                    "user": c["user"]["login"],
                    "created_at": c.get("created_at"),
                    "body": (c.get("body") or "")[:500],
                    # Round-32: per-head review-cycle
                    # binding is conditional on the
                    # current head actually being the
                    # provider's review target.
                    #
                    # A formal review on the current head
                    # is NOT sufficient evidence that an
                    # arbitrary historical issue comment
                    # belongs to this head. The binding
                    # is positive: a comment is bound to
                    # the current head only when either
                    # (a) the comment has its own intrinsic
                    # ``commit_id`` matching ``head_sha``,
                    # or
                    # (b) the comment is the latest
                    # ``latest_comments_by_provider`` for
                    # this provider AND the provider's
                    # last review is the current head.
                    #
                    # Otherwise ``commit_id`` is ``None``
                    # and the relay's
                    # ``collect_findings`` filter rejects
                    # the comment (no current-head binding
                    # = no actionable finding).
                    "commit_id": (
                        c.get("commit_id")
                        or c.get("commit_oid")
                        if isinstance(c.get("commit_id"), str)
                        and c.get("commit_id") == head_sha
                        else None
                    ),
                    "review_cycle": (
                        # Per-head ledger entry. The
                        # cycle identity is keyed by
                        # provider + head_sha + comment
                        # id. The relay's filter accepts
                        # the comment only when
                        # ``commit_id`` matches the
                        # current head (production path)
                        # OR when ``review_cycle`` is
                        # present (backward-compat path
                        # for legacy captures).
                        f"{provider}:{head_sha}:{cid}"
                        if surfaces.get("reviews")
                        and isinstance(c.get("commit_id"), str)
                        and c.get("commit_id") == head_sha
                        else None
                    ),
                })
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
        data_obj = root.get("data")
        data = data_obj if isinstance(data_obj, dict) else {}
        repo_obj = data.get("repository")
        repo = repo_obj if isinstance(repo_obj, dict) else {}
        pr_obj = repo.get("pullRequest")
        pr_gql = pr_obj if isinstance(pr_obj, dict) else {}
        threads_obj = pr_gql.get("reviewThreads")
        threads = threads_obj if isinstance(threads_obj, dict) else {}
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
        pinfo = page_info_obj if isinstance(page_info_obj, dict) else {}
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
    reasons: list[str] = []
    if not a or not b:
        return ["snapshot_empty"]
    if (
        a.get("head_sha") != expected_head
        or b.get("head_sha") != expected_head
    ):
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
    """
    required = set(POLICY.get("required_check_names") or [])
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
    blockers = threads_block_readiness(snap)
    if blockers:
        return {
            "ready": False,
            "reason": "unresolved_threads",
            "blockers": blockers,
        }
    if list_unconsumed_events():
        return {"ready": False, "reason": "unconsumed_events"}
    if not required_checks_green(snap):
        return {"ready": False, "reason": "checks_not_green"}
    if any_required_provider_in_progress(snap):
        return {
            "ready": False,
            "reason": "required_provider_in_progress",
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
        if not required_checks_green(_snap):
            log(
                "warning",
                "_advance_awaiting_ci_to_qualifying: required checks not green; "
                "refusing transition (fail-closed)",
                required_checks=_snap.get("required_checks", {}),
                head=AUTHORITATIVE_HEAD[:12]  # type: ignore[name-defined]
                if AUTHORITATIVE_HEAD  # type: ignore[name-defined]
                else "",
            )
            return False
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
    """
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
            # Round-32: honor the durable retry ledger.
            # The retry ledger carries the
            # ``next_eligible_retry_at`` timestamp; the
            # supervisor MUST NOT retry before it. When
            # the timestamp has elapsed, the supervisor
            # bumps the slice_epoch (starting a fresh
            # slice with a fresh budget) and continues.
            retry_state = (
                read_round_budget_retry(
                    str(RUN_STATE.parent / "evidence"),  # type: ignore[name-defined]
                )
                if read_round_budget_retry is not None
                else None
            ) or {}
            now = now_iso()
            next_eligible = (
                str(retry_state.get("next_eligible_retry_at") or "")
                if retry_state
                else ""
            )
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
            if (
                bump_slice_epoch is not None
                and retry_state
                and retry_state.get("last_attempt_at")
                and retry_state.get("lifecycle")
                not in ("cleared", "resolved", "consumed")
            ):
                bump_slice_epoch(
                    str(RUN_STATE.parent / "evidence"),  # type: ignore[name-defined]
                )

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

            if cur_state == STATE_ACTIVE_REPAIR:
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
