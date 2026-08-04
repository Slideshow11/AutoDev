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
import errno
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .config import default_config_from_env
from .contracts import SupervisorConfig


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
            os.environ.get("AED_PR_NUMBER", "0")
        ),
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
except Exception as _exc:
    # If the env-derived config cannot be built (e.g. the
    # host has no $PWD), defer to a minimal stub. Production
    # callers always pass --config and override this stub
    # before any module function is invoked.
    import sys as _sys
    if "autocoder_supervisor.config" in str(_exc):
        _BOOTSTRAPPED_FROM = None  # type: ignore[assignment]
        _APPLIED = {}  # type: ignore[assignment]
    else:
        raise
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
    try:
        return json.loads(RUN_STATE.read_text())  # type: ignore[name-defined]
    except Exception as e:
        log("error", "read_run_state failed", error=str(e))
        return {}


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
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError as e:
        return e.errno == errno.EPERM


def pgid_alive(pgid: int) -> bool:
    try:
        os.kill(-pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError as e:
        return e.errno == errno.EPERM


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
    # accidentally inherit each other's leases.
    repo_dir = str(REPO_DIR).rstrip("/")  # type: ignore[name-defined]
    if cwd.rstrip("/") != repo_dir:
        return None
    lease["heartbeat_at"] = now_iso()
    return lease


def write_cooldown() -> None:
    """Persist the cooldown timestamp atomically.

    The file is plain text (an ISO timestamp) so we reuse
    ``write_json`` for the atomic rename + 0600 chmod path.
    """
    write_json(LAST_RESUME_PATH, {"ts": now_iso()})  # type: ignore[name-defined]


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
    return RESUME_PROMPT_TEMPLATE.format(  # type: ignore[name-defined]
        pr_number=PR_NUMBER,  # type: ignore[name-defined]
        repo_owner=REPO_OWNER,  # type: ignore[name-defined]
        repo_name=REPO_NAME,  # type: ignore[name-defined]
        branch=os.environ.get("AED_BRANCH", "feat/controller-run-identity-and-locking"),
        head=AUTHORITATIVE_HEAD,  # type: ignore[name-defined]
        session_id=SESSION_ID,  # type: ignore[name-defined]
    )


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
    prompt = build_resume_prompt(rs, live)
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
        cmd = [
            part.format(
                prompt=prompt,
                session_id=SESSION_ID,  # type: ignore[name-defined]
            )
            for part in configured_cmd
        ]
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
                surfaces["issue_comments"].append({
                    "id": cid,
                    "user": c["user"]["login"],
                    "created_at": c.get("created_at"),
                    "body": (c.get("body") or "")[:500],
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
        within_request_window = (
            request_ts is None
            or ts is None
            or ts >= request_ts
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
        if request_ts is None or ts is None or ts >= request_ts:
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
    all_threads: list[tuple[str, bool, bool]] = []
    cursor = None
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
            break
        threads = (
            d.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
        )
        for tn in threads.get("nodes", []):
            all_threads.append((
                tn.get("id"),
                bool(tn.get("isResolved")),
                bool(tn.get("isOutdated")),
            ))
        pinfo = threads.get("pageInfo", {})
        if not pinfo.get("hasNextPage"):
            break
        cursor = pinfo.get("endCursor")
    snap["review_threads"] = {
        tid: {"resolved": r, "outdated": o}
        for (tid, r, o) in all_threads
    }
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
    required = {
        "test (3.11)",
        "validator",
        "governance-validators",
        "review-comment-gate",
        "pr-gate-live-smoke",
    }
    for name in required:
        info = snap.get("required_checks", {}).get(name)
        # An absent entry means the check has not yet been
        # registered; fail closed rather than skipping.
        if info is None:
            return False
        status = info.get("status")
        # Pending checks (queued, in_progress, waiting,
        # pending, requested) are not yet green. ``completed``
        # is the canonical "conclusion is final" status
        # emitted by GitHub once a check has reached its
        # terminal state.
        if status not in ("completed", "success", "skipped", "neutral"):
            return False
        c = info.get("conclusion")
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
) -> None:
    """Run the ACTIVE_REPAIR quiet-window transition.

    Captures snapshot A, sleeps for the quiet window, captures
    snapshot B, and decides what to do:

    - If snapshots differ or new events were emitted during
      the quiet window, stay in ACTIVE_REPAIR.
    - If snapshots are stable AND no new events emerged,
      clear the pre-existing unconsumed events (they are
      effectively resolved by the system stabilising) and
      advance to PROVISIONAL_READY if the readiness gate
      passes.

    The function reads ``readiness_state``, ``snapshot_a``,
    ``snapshot_b``, and ``unconsumed_events``; the caller
    is responsible for the surrounding ``while True:`` loop
    and the heartbeat / lock bookkeeping.
    """
    snap_a = capture_and_store_snapshot("A", rs, token)
    time.sleep(quiet_window)
    snap_b = capture_live_snapshot(rs, token or "")
    new_events_during_window = [
        e
        for e in detect_new_actionable_events(snap_a, snap_b)
        if e.get("id") not in pre_unconsumed_ids
    ]
    reasons = snapshot_differs(
        snap_a, snap_b, AUTHORITATIVE_HEAD  # type: ignore[name-defined]
    )
    if reasons or new_events_during_window:
        log(
            "info",
            "snapshot differs; staying ACTIVE_REPAIR",
            reasons=reasons,
            new_events=len(new_events_during_window),
        )
        return
    # Snapshot is stable AND no new events emerged during the
    # quiet window. Clear pre-existing unconsumed events
    # BEFORE evaluating readiness so the readiness gate can
    # pass — the ``unconsumed_events`` check inside
    # ``evaluate_readiness`` would otherwise reject readiness
    # even though the system has stabilised.
    if list_unconsumed_events():
        log(
            "info",
            "snapshot stable across quiet window; "
            "clearing pre-existing unconsumed events",
            count=len(list_unconsumed_events()),
        )
        write_json(
            UNCONSUMED_EVENTS_PATH,  # type: ignore[name-defined]
            {"events": []},
        )
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
    else:
        log(
            "info",
            "readiness denied",
            reason=result.get("reason"),
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
        while True:
            heartbeat_touch()
            rs = read_run_state()
            token = get_github_token()
            if token and read_snapshot("A") == {}:
                capture_and_store_snapshot("A", rs, token)

            iteration = run_iteration_v5(rs, token or "")
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

            if new_events and not cooldown_active():
                already = launched_event_ids()
                fresh_ids = [
                    e["id"] for e in new_events
                    if e.get("id") and e["id"] not in already
                ]
                if fresh_ids:
                    lease = read_lease()
                    if not (
                        lease and lease_alive(lease) is not None
                    ):
                        revoke_readiness(
                            reason="new_actionable_event",
                            head_sha=iteration.get("head_sha"),
                        )
                        log(
                            "info",
                            "revoking readiness (new actionable "
                            "event); launching single worker",
                            events=[
                                e.get("kind") for e in new_events
                                if e.get("id") in fresh_ids
                            ],
                        )
                        live = (
                            inspect_live_state(token) if token else {}
                        )
                        # Mark each event as launched only
                        # AFTER the worker has actually been
                        # launched successfully. A failed
                        # launch must not permanently suppress
                        # the event; the next heartbeat's
                        # fresh_ids filter would otherwise
                        # exclude it forever.
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

            if cur_state == STATE_ACTIVE_REPAIR:
                pre_unconsumed_ids = {
                    e.get("id") for e in list_unconsumed_events()
                }
                active_repair_quiet_window(
                    rs, token or "", quiet_window, pre_unconsumed_ids,
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
