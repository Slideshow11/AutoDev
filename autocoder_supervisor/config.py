"""Supervisor configuration loader.

This module is the single source of truth for parsing,
validating, and constructing ``SupervisorConfig`` objects. The
supervisor module reads its configuration from here so that
the package can be installed under any prefix and used by any
operator.

The validator explicitly rejects:

* tokens, cookies, oauth secrets, and credentials
* absolute user-specific paths under ``/home/<user>/...``
* absolute user-specific paths under ``~/<user>/...``
* absolute paths whose first component looks like a home
  directory (e.g. ``/Users/max/...`` on macOS)

These rejections make it impossible to commit a working
supervisor configuration that leaks the operator's home
directory into the source tree.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any, Mapping

try:  # Python 3.11+
    import tomllib  # type: ignore[import]
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[import,no-redef]

from .contracts import SupervisorConfig, SupervisorConfigDict


# Required keys.
_REQUIRED_KEYS = (
    "schema_version",
    "instance_id",
    "state_dir",
    "working_checkout",
    "log_path",
    "heartbeat_path",
    "lock_path",
    "worker_command",
    "worker_session_id",
    "worker_session_name",
    "cooldown_seconds",
    "resume_prompt_template",
    "human_boundary",
    "required_review_providers",
    "optional_review_providers",
    "provider_states_are_independent",
    "post_codex_recovery_request",
    "heartbeat_seconds",
    "quiet_window_seconds",
    "quota_retry_initial_seconds",
    "quota_retry_backoff_seconds",
    "quota_backoff_after_retry_count",
)

# Patterns that look like credentials. These are deliberately
# broad so a false-positive is preferable to a leaked secret.
_CREDENTIAL_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),  # GitHub PATs
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),  # Slack tokens
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access keys
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),  # PEM private keys
    re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._-]{8,}"),
    re.compile(r"(?i)\boauth_token[:=]\s*[A-Za-z0-9._-]{8,}"),
    re.compile(r"(?i)cookie:\s*[A-Za-z0-9._-]{8,}="),
    re.compile(r"(?i)password\s*[:=]\s*[^\s]{6,}"),
    re.compile(r"(?i)secret\s*[:=]\s*[^\s]{6,}"),
)


def _looks_like_absolute_user_path(value: str) -> bool:
    """Return True if ``value`` is an absolute path that lives
    under a user's home directory.
    """
    if not value:
        return False
    p = Path(os.path.expandvars(os.path.expanduser(value)))
    if not p.is_absolute():
        return False
    parts = p.parts
    if not parts:
        return False
    # Linux / WSL: ``/home/<user>/...`` or ``/root/...``
    if parts[0] == "/" and len(parts) >= 3 and parts[1] == "home":
        return True
    if parts[0] == "/" and len(parts) >= 2 and parts[1] == "root":
        return True
    # macOS: ``/Users/<user>/...``
    if parts[0] == "/" and len(parts) >= 3 and parts[1] == "Users":
        return True
    return False


def _string_contains_credential(value: str) -> bool:
    return any(p.search(value) for p in _CREDENTIAL_PATTERNS)


def _walk_for_credentials(node: Any, prefix: str = "") -> list[str]:
    """Walk a config object and collect credential-shaped values.

    The walker raises the moment it finds a credential-shaped
    string so the validator can produce a precise error
    message that points at the offending key.
    """
    findings: list[str] = []
    if isinstance(node, str):
        if _string_contains_credential(node):
            findings.append(prefix or "<root>")
        return findings
    if isinstance(node, list):
        for i, item in enumerate(node):
            findings.extend(_walk_for_credentials(item, f"{prefix}[{i}]"))
        return findings
    if isinstance(node, Mapping):
        for k, v in node.items():
            child_prefix = f"{prefix}.{k}" if prefix else str(k)
            findings.extend(_walk_for_credentials(v, child_prefix))
        return findings
    return findings


def validate_config_dict(
    data: Mapping[str, Any],
    *,
    reject_user_paths: bool = True,
) -> None:
    """Validate the configuration dict.

    ``reject_user_paths`` defaults to True so that any
    *persisted* configuration file is forced to use
    non-user-specific absolute paths. ``default_config_from_env``
    and tests may pass ``False`` so that an in-process
    default — which is never persisted — can point at the
    current working checkout.

    Raises ``ValueError`` on the first invalid input. The
    validator enforces:

    * every required key is present
    * every path is absolute OR a safe relative path (the
      package refuses to operate against absolute user-home
      paths, but ``$VAR`` expansion is supported so an
      installer can pin a non-user-specific prefix)
    * no value matches a credential pattern
    * ``human_boundary`` is exactly ``"merge_only"``
    * ``required_review_providers`` and
      ``optional_review_providers`` are disjoint
    """
    missing = [k for k in _REQUIRED_KEYS if k not in data]
    if missing:
        raise ValueError(
            f"supervisor config is missing required keys: {missing}"
        )

    # Credential scan across every leaf string.
    bad = _walk_for_credentials(dict(data))
    if bad:
        raise ValueError(
            "supervisor config contains credential-shaped values at: "
            f"{bad}; tokens, cookies, oauth secrets and similar must "
            "NOT be committed to the source tree"
        )

    # Path safety scan.
    path_keys = (
        "state_dir",
        "working_checkout",
        "log_path",
        "heartbeat_path",
        "lock_path",
    )
    for k in path_keys:
        v = data.get(k, "")
        if not isinstance(v, str):
            raise ValueError(
                f"supervisor config key '{k}' must be a string path"
            )
        if reject_user_paths and _looks_like_absolute_user_path(v):
            raise ValueError(
                f"supervisor config key '{k}' is an absolute "
                f"user-specific path ({v!r}); use a non-user path "
                "(e.g. /opt/aed-supervisor/state, $AED_STATE_DIR) "
                "so the configuration is portable"
            )

    # human_boundary is locked to merge_only by the
    # stabilisation-phase contract.
    if data.get("human_boundary") != "merge_only":
        raise ValueError(
            "supervisor config: human_boundary must be 'merge_only'"
        )

    required = list(data.get("required_review_providers", []))
    optional = list(data.get("optional_review_providers", []))
    if not required:
        raise ValueError(
            "supervisor config: required_review_providers must "
            "contain at least one provider"
        )
    overlap = set(required) & set(optional)
    if overlap:
        raise ValueError(
            f"supervisor config: providers listed in both required "
            f"and optional: {sorted(overlap)}"
        )
    if not isinstance(
        data.get("provider_states_are_independent"), bool
    ):
        raise ValueError(
            "supervisor config: provider_states_are_independent "
            "must be a bool"
        )
    if not isinstance(data.get("post_codex_recovery_request"), bool):
        raise ValueError(
            "supervisor config: post_codex_recovery_request "
            "must be a bool"
        )

    # Numeric ranges.
    int_keys = (
        "cooldown_seconds",
        "heartbeat_seconds",
        "quiet_window_seconds",
        "quota_retry_initial_seconds",
        "quota_retry_backoff_seconds",
        "quota_backoff_after_retry_count",
    )
    for k in int_keys:
        v = data.get(k)
        if not isinstance(v, int) or v < 0:
            raise ValueError(
                f"supervisor config: {k} must be a non-negative integer"
            )
    # Cadence keys must be strictly positive; ``0`` would turn
    # the supervisor loop into an unthrottled GitHub API
    # poller and trigger rate limiting.
    for k in ("heartbeat_seconds", "quiet_window_seconds"):
        if int(data[k]) < 1:
            raise ValueError(
                f"supervisor config: {k} must be at least 1 second"
            )

    # Worker command must be a non-empty list of strings.
    wc = data.get("worker_command")
    if (
        not isinstance(wc, list)
        or not wc
        or not all(isinstance(x, str) for x in wc)
    ):
        raise ValueError(
            "supervisor config: worker_command must be a non-empty "
            "list of strings"
        )


def load_config(path: str | os.PathLike[str]) -> SupervisorConfig:
    """Load and validate a supervisor configuration from TOML.

    Persisted TOML files are validated against the strict
    user-path guard because they are exactly the artifact that
    could leak the operator's home directory into the source
    tree if the guard were disabled.
    """
    p = Path(path)
    text = p.read_text()
    data = tomllib.loads(text)
    return SupervisorConfig.from_dict(data)  # type: ignore[arg-type]


def default_config_from_env() -> SupervisorConfig:
    """Build a config from environment variables.

    This is the fallback used when the package is imported
    without an explicit configuration. The paths default to
    a per-process private runtime directory so that no local
    user can pre-create or race the supervisor's state.

    The fallback is intentionally an *in-process* default —
    it is never persisted. The ``reject_user_paths`` guard is
    therefore disabled (``from_dict(reject_user_paths=False)``)
    so the operator's local ``$PWD``-derived
    ``working_checkout`` is accepted. The strict guard is
    reserved for ``load_config``, which reads a *persisted*
    TOML file.
    """
    # Build a per-process private runtime directory.
    # Prefer $XDG_RUNTIME_DIR (mode 0700 by convention) and
    # fall back to a tempfile.mkdtemp() if it is not set.
    # mkdtemp creates the directory with mode 0700.
    import tempfile

    runtime_root = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_root:
        runtime_root = os.path.join(
            runtime_root, f"aed-supervisor-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        os.makedirs(runtime_root, mode=0o700, exist_ok=True)
    else:
        runtime_root = tempfile.mkdtemp(
            prefix=f"aed-supervisor-{os.getpid()}-",
        )
        # tempfile.mkdtemp creates with mode 0700 already;
        # chmod is defensive in case the umask interfered.
        try:
            os.chmod(runtime_root, 0o700)
        except OSError:
            pass

    state_dir = os.environ.get(
        "AED_SUPERVISOR_STATE_DIR", os.path.join(runtime_root, "state")
    )
    log_path = os.environ.get(
        "AED_SUPERVISOR_LOG_PATH", os.path.join(runtime_root, "supervisor.log")
    )
    heartbeat_path = os.environ.get(
        "AED_SUPERVISOR_HEARTBEAT_PATH",
        os.path.join(runtime_root, "heartbeat"),
    )
    lock_path = os.environ.get(
        "AED_SUPERVISOR_LOCK_PATH", os.path.join(runtime_root, "lock")
    )
    # The working_checkout falls back to $PWD for the
    # in-process default. This is acceptable because the
    # result is never persisted; load_config (which reads
    # TOML files) is what guards against committed user
    # paths.
    working_checkout = os.environ.get(
        "AED_SUPERVISOR_WORKING_CHECKOUT",
        os.environ.get("PWD", "/tmp/aed-supervisor-default/working_checkout"),
    )

    data: SupervisorConfigDict = {
        "schema_version": "aed.autocoder_supervisor.v1",
        "instance_id": os.environ.get(
            "AED_SUPERVISOR_INSTANCE_ID", "aed-supervisor-default"
        ),
        "state_dir": state_dir,
        "working_checkout": working_checkout,
        "log_path": log_path,
        "heartbeat_path": heartbeat_path,
        "lock_path": lock_path,
        "worker_command": ["hermes", "chat", "-q", "{prompt}", "--resume", "{session_id}"],
        "worker_session_id": os.environ.get(
            "AED_SESSION_ID", "aed-supervisor-default"
        ),
        "worker_session_name": os.environ.get(
            "AED_SESSION_NAME", "AED-Autocoder-Supervisor"
        ),
        "cooldown_seconds": int(
            os.environ.get("AED_RESUME_COOLDOWN_SECS", "900")
        ),
        "resume_prompt_template": (
            "[AED-AUTOCODER RESUME — standing authorization] "
            "Continue the repair cycle for PR {pr_number} "
            "({repo_owner}/{repo_name}, branch {branch}). "
            "Authoritative head: {head}. Inspect every applicable "
            "review surface; apply every valid-current repair "
            "autonomously; verify by re-running the focused suite "
            "and the gate; never post a duplicate review request "
            "for an unchanged head; stop only when the run reaches "
            "a terminal classification. Standing authorization is "
            "already recorded in run_state.json."
        ),
        "human_boundary": "merge_only",
        "required_review_providers": ["coderabbit"],
        "optional_review_providers": ["codex"],
        "provider_states_are_independent": True,
        "post_codex_recovery_request": False,
        "heartbeat_seconds": int(
            os.environ.get("AED_HEARTBEAT_SECS", "120")
        ),
        "quiet_window_seconds": int(
            os.environ.get("AED_QUIET_WINDOW_SECONDS", "180")
        ),
        "quota_retry_initial_seconds": int(
            os.environ.get("AED_QUOTA_RETRY_INITIAL_SECS", "3600")
        ),
        "quota_retry_backoff_seconds": int(
            os.environ.get("AED_QUOTA_RETRY_BACKOFF_SECS", "21600")
        ),
        "quota_backoff_after_retry_count": int(
            os.environ.get(
                "AED_QUOTA_BACKOFF_AFTER_RETRY_COUNT", "2"
            )
        ),
        "provider_quota_reset": {},
    }
    return SupervisorConfig.from_dict(data, reject_user_paths=False)  # type: ignore[arg-type]
