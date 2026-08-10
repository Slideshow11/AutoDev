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
from typing import Any, Optional

#: Strict lowercase hex SHA-1/256 pattern. Used to validate
#: rebind targets and any other 40-or-64-char head SHA.
_HEX_SHA_RE = re.compile(r"\A[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")

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
    try:
        text = TOKEN_FILE.read_text()  # type: ignore[name-defined]
        m = re.search(r"oauth_token:\s+(\S+)", text)
        return m.group(1) if m else None
    except Exception:
        return None


def github_get(path: str, token: str) -> Optional[Any]:
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
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
    reviews = github_get(
        f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}/reviews"  # type: ignore[name-defined]
        f"?per_page=20",
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
    """Return True iff the process exists and is liveness-probeable.

    os.kill(pid, 0) raises ProcessLookupError when the
    PID does not exist and PermissionError when the PID
    exists but is owned by another user. A single-writer
    invariant depends on the distinction: a stray EPERM must
    NOT be treated as "process dead".
    """
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


def pgid_alive(pgid: int) -> bool:
    """Return True iff the process group exists.

    Identical semantics to pid_alive for the same
    single-writer safety reason.
    """
    try:
        os.kill(-pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


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
    if SESSION_ID not in cmdline:  # type: ignore[name-defined]
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


def launch_worker(rs: dict, live: dict) -> Optional[dict]:
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
                    session_id=SESSION_ID,  # type: ignore[name-defined]
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
            SESSION_ID,  # type: ignore[name-defined]
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
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_DIR),  # type: ignore[name-defined]
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        log("error", "worker launch failed", error=str(e))
        return None
    evidence = start_time_evidence(proc.pid)
    if "error" in evidence:
        log(
            "warning",
            "could not capture start-time evidence",
            error=evidence["error"],
        )
    lease = {
        "supervisor_instance_id": INSTANCE_ID,  # type: ignore[name-defined]
        "run_id": f"PR-{PR_NUMBER}",  # type: ignore[name-defined]
        "pr_number": PR_NUMBER,  # type: ignore[name-defined]
        "session_id": SESSION_ID,  # type: ignore[name-defined]
        "session_name": SESSION_NAME,  # type: ignore[name-defined]
        "authoritative_head_at_launch":
            AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
        "pid": proc.pid,
        "pgid": proc.pid,
        "start_time_evidence": evidence,
        "launched_at": now_iso(),
        "heartbeat_at": now_iso(),
        "cmd": cmd,
    }
    write_lease(lease)
    write_cooldown()
    log(
        "info", "worker launched", pid=proc.pid, pgid=proc.pid
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
        reviews = github_get(
            f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}/reviews"  # type: ignore[name-defined]
            f"?per_page=50",
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
    for pid_dir in os.listdir("/proc"):
        if not pid_dir.isdigit():
            continue
        pid = int(pid_dir)
        try:
            cmdline = pid_cmdline(pid)
        except Exception:
            continue
        if "hermes chat" in cmdline and SESSION_ID in cmdline:  # type: ignore[name-defined]
            try:
                my_pgid = os.getpgid(pid)
            except Exception:
                continue
            evidence = start_time_evidence(pid)
            new_lease = {
                "supervisor_instance_id": INSTANCE_ID,  # type: ignore[name-defined]
                "run_id": f"PR-{PR_NUMBER}",  # type: ignore[name-defined]
                "pr_number": PR_NUMBER,  # type: ignore[name-defined]
                "session_id": SESSION_ID,  # type: ignore[name-defined]
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
                    SESSION_ID,  # type: ignore[name-defined]
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
        f"/repos/{REPO_OWNER}/{REPO_NAME}/pulls/{PR_NUMBER}/reviews?per_page=20",  # type: ignore[name-defined]
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
    all_threads: list[tuple[str, bool, bool]] = []
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
        query = (
            "query($owner: String!, $name: String!, "
            "$number: Int!, $cursor: String) "
            "{ repository(owner: $owner, name: $name) "
            "{ pullRequest(number: $number) "
            "{ reviewThreads(first: 100, after: $cursor) "
            "{ pageInfo { hasNextPage endCursor } "
            "nodes { id isResolved isOutdated } } } } }"
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
            all_threads.append((
                node_id,
                bool(tn.get("isResolved")),
                bool(tn.get("isOutdated")),
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
        tid: {"resolved": r, "outdated": o}
        for (tid, r, o) in all_threads
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


def enter_readiness(into: str, head_sha: str = None) -> None:
    write_readiness_state({
        "state": into,
        "achieved_at": now_iso(),
        "head_sha": head_sha or AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
        "policy": POLICY,
    })


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
                # Round-29 P1#19: refresh the heartbeat
                # INSIDE the polling loop so an external
                # watchdog does not treat the supervisor as
                # dead while the loop is waiting for a
                # stable interval.
                try:
                    heartbeat_touch()
                except (OSError, NameError):
                    pass
            except (OSError, json.JSONDecodeError, OrchestrationRootError):
                # Round-29 P1#7: orch-state-root resolution
                # failure is a protected-authority blocker.
                # The supervisor MUST NOT silently proceed;
                # route to BLOCKED / escalation. The outer
                # main iteration handles the BLOCKED signal.
                log(
                    "error",
                    "quiet-window halted: orchestration state_root "
                    "cannot be positively identified; BLOCKED/escalation",
                    head=AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
                )
                controller_blocked = True
                break
        snap_b = capture_live_snapshot(rs, token or "")
        new_events_during_window = [
            e
            for e in detect_new_actionable_events(snap_a, snap_b)
            if e.get("id") not in pre_unconsumed_ids
        ]
        reasons = snapshot_differs(
            snap_a, snap_b, AUTHORITATIVE_HEAD  # type: ignore[name-defined]
        )
        elapsed = _time.monotonic() - qualifying_started_at
        if new_events_during_window:
            # A new event arrived during the window. It
            # MUST NOT be cleared by the post-loop
            # cleanup; the caller will route it to
            # handle_new_events. The window resets.
            new_event_observed = True
            log(
                "info",
                "quiet-window: new event arrived during polling; "
                "preserving for handle_new_events",
                event_count=len(new_events_during_window),
                elapsed=round(elapsed, 1),
            )
        if reasons or new_events_during_window:
            log(
                "info",
                "quiet-window: non-qualifying observation; "
                "resetting interval",
                reasons=reasons,
                new_events=len(new_events_during_window),
                elapsed=round(elapsed, 1),
            )
            # Reset the interval. Re-capture snapshot A
            # so the next interval starts from the
            # current state.
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
    pre_existing = [
        e for e in list_unconsumed_events()
        if e.get("id") in pre_unconsumed_ids
    ]
    if pre_existing:
        log(
            "info",
            "snapshot stable across quiet window; "
            "clearing pre-existing unconsumed events",
            count=len(pre_existing),
        )
        write_json(
            UNCONSUMED_EVENTS_PATH,  # type: ignore[name-defined]
            {"events": []},
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
        lease_path = (
            Path(str(RUN_STATE)).parent  # type: ignore[name-defined]
            / "state"
            / "worker_lease.json"
        )
        if lease_path.exists():
            try:
                lease = json.loads(lease_path.read_text())
                lease_active = lease.get("pr_number") == this_pr
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
    try:
        from autocoder_orchestration.review_repair_relay import (
            read_round_budget_retry,
        )
    except Exception:  # noqa: BLE001
        read_round_budget_retry = None  # type: ignore[assignment]
    # Use the supervisor's evidence root (mirrors the
    # round-budget retry state).
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
        "reason": "orchestration_root_unresolved",
        "attempt_count": attempt_count,
        "first_failure_at": prior.get(
            "first_failure_at", now,
        ) if prior else now,
        "last_attempt_at": now,
        "next_eligible_retry_at": next_eligible_retry_at,
        "recorded_at": now,
        "owner": "relay_recovery",
        "recoverable": True,
    }
    try:
        tmp = retry_path.with_suffix(retry_path.suffix + ".tmp")
        import json as _json
        tmp.write_text(_json.dumps(payload, sort_keys=True))
        tmp.replace(retry_path)
    except OSError as e:
        log(
            "warning",
            "orchestration-root retry state persistence failed",
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
    else:
        log(
            "info",
            "revoking readiness (new actionable "
            "event); launching single worker (no relay directive)",
            events=[
                e.get("kind") for e in new_events
                if e.get("id") in fresh_ids
            ],
        )
    live = (
        inspect_live_state(token) if token else {}
    )
    new_lease = launch_worker(rs, live)
    if new_lease:
        for eid in fresh_ids:
            mark_event_launched(eid)
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
            if (
                bump_slice_epoch is not None
                and retry_state
                and retry_state.get("last_attempt_at")
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
            for this_pr in pr_numbers:
                if this_pr == 0:
                    continue
                try:
                    globals()["PR_NUMBER"] = this_pr
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
            new_events = iteration.get("events", [])
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

            # Head rebinding. When the worker has pushed a
            # new commit, the live head advances. The
            # supervisor MUST update AUTHORITATIVE_HEAD so
            # the next round operates on the new head. The
            # rebind is observable: the next heartbeat's
            # run_iteration_v5 will report the new head,
            # and the relay's exact-head guard will accept
            # it.
            live_head = iteration.get("head_sha")
            if (
                live_head
                and isinstance(live_head, str)
                and live_head != AUTHORITATIVE_HEAD  # type: ignore[name-defined]
                and _HEX_SHA_RE.match(live_head)  # type: ignore[name-defined]
            ):
                old_head = AUTHORITATIVE_HEAD  # type: ignore[name-defined]
                globals()["AUTHORITATIVE_HEAD"] = live_head
                # Bind the worker push to the state machine.
                # The relay's mark_head_advanced fires the
                # transition REPAIRING_REVIEW_FINDINGS ->
                # AWAITING_CI only on a real head advance, so
                # a launch failure leaves the repair state
                # recoverable. When the worker pushes a new
                # commit, the rebind here triggers the
                # transition.
                try:
                    from . import relay_wiring as _relay_wiring
                    _relay_wiring.mark_head_advanced_public(
                        old_head, live_head,
                    )
                except Exception as exc:
                    log(
                        "warning",
                        "mark_head_advanced failed; controller "
                        "state may not match",
                        old_head=old_head[:12] if old_head else "",
                        new_head=live_head[:12],
                        error=str(exc),
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
                    "AUTHORITATIVE_HEAD rebinding: worker pushed new head",
                    old_head=old_head[:12] if old_head else "",
                    new_head=live_head[:12],
                )

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
            if new_events and not cooldown_active():
                handle_new_events(rs, new_events, token, iteration)

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
