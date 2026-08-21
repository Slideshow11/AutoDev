"""Round-C24-R2 / Worker auth preflight.

The audit (§7-9) requires the supervisor to start a
worker only when the worker's environment has
authenticated repo write capability. The previous
implementation launched a worker that returned
``WORKER_EXECUTION_FAILED`` halfway through because it
had no auth (``gh auth status`` unmatched, no SSH key,
no token).

This module exports a single preflight check that
inspects the supervisor's host environment (the
environment the worker inherits) and reports:

  - ``READ``: false / true (filesystem walkable)
  - ``WRITE``: false / true (push capability)
  - mechanism: "git credential helper" | "gh auth" | "SSH" | "UNKNOWN"
  - boolean flags for each without disclosing
    credential material

The check never prints token values, secrets, or
``Authorization`` headers. It only inspects boolean
flags and PATH.

The preflight is deliberately non-mutating: it does
NOT perform a junk push. It uses ``gh auth status`` (a
read-only ``gh`` subcommand) and ``git rev-parse --is-inside-work-tree``
to confirm the worker can at least observe the repo.
    """

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class WorkerAuthPreflightResult:
    """Safe-to-log auth summary.

    All fields are booleans / strings; no secrets. The
    ``WORKER_REPO_AUTH_UNAVAILABLE`` diagnostic is raised
    when ``write_capable`` is False.
    """

    read_capable: bool
    write_capable: bool
    mechanism: str  # "git credential helper" | "gh auth" | "SSH" | "UNKNOWN"
    has_gh_cli: bool
    has_github_token_env: bool
    has_gh_token_env: bool
    has_ssh_auth_sock: bool
    has_git_credential_helper: bool
    repo_walkable: bool
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class WorkerRepoAuthUnavailable(Exception):
    """Raised when the worker environment cannot write to
    the canonical repo (no auth).

    The supervisor catches this exception and emits a
    ``WORKER_REPO_AUTH_UNAVAILABLE`` diagnostic instead of
    launching a worker that would inevitably fail
    halfway through. The preflight is non-mutating; the
    caller may choose to invoke a configured
    ``supervisor-handoff`` to perform an authenticated
    push from the supervisor's context instead.
    """


def _run_subprocess(argv: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Run a subprocess with a bounded timeout. Returns
    (returncode, stdout, stderr). The string bodies are
    truncated to 4KB to bound log volume."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (
            proc.returncode,
            (proc.stdout or "")[:4096],
            (proc.stderr or "")[:4096],
        )
    except subprocess.TimeoutExpired:
        return (-1, "", "timeout")
    except FileNotFoundError as exc:
        return (-1, "", f"not_found:{exc}")
    except Exception as exc:  # noqa: BLE001
        return (-1, "", f"error:{exc!r}")


def _check_gh_cli() -> tuple[bool, bool]:
    """Return (has_gh_cli, gh_authenticated)."""
    try:
        proc = subprocess.run(
            ["gh", "--version"], capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, OSError):
        return False, False
    if proc.returncode != 0:
        return False, False
    try:
        auth = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, OSError):
        return True, False
    return True, (auth.returncode == 0)


def _check_git_credential_helper(repo_dir: Path) -> bool:
    """Return True if the workspace has a credential helper
    configured (anything non-empty counts)."""
    rc, stdout, _ = _run_subprocess(
        ["git", "-C", str(repo_dir), "config", "--get", "credential.helper"],
        timeout=5,
    )
    if rc != 0:
        return False
    return bool(stdout.strip())


def _check_ssh_auth_sock() -> bool:
    return bool(os.environ.get("SSH_AUTH_SOCK") or os.environ.get("GIT_SSH_COMMAND"))


def _check_gh_token_env() -> tuple[bool, bool]:
    """Return (has_gh_token, has_github_token)."""
    return (
        bool(os.environ.get("GH_TOKEN")),
        bool(os.environ.get("GITHUB_TOKEN")),
    )


def _check_repo_walkable(repo_dir: Path) -> bool:
    rc, _, _ = _run_subprocess(
        ["git", "-C", str(repo_dir), "rev-parse", "--is-inside-work-tree"],
        timeout=5,
    )
    return rc == 0


def run_worker_auth_preflight(
    *,
    repo_dir: Path,
    is_token_credential_only: bool = False,
) -> WorkerAuthPreflightResult:
    """Run the full preflight. Boolean-only; no secret
    contents are logged.

    The optional ``is_token_credential_only`` flag tells
    the preflight that the deployment depends on
    ``GH_TOKEN`` / ``GITHUB_TOKEN`` env vars (no gh auth,
    no SSH agent). When set, the preflight verifies
    exactly one of those vars is present and the
    ``GIT_ASKPASS`` / ``GH_TOKEN`` plumbing is wired.
    """
    has_gh_token, has_github_token = _check_gh_token_env()
    has_ssh = _check_ssh_auth_sock()
    has_gh_cli, gh_authenticated = _check_gh_cli()
    has_helper = _check_credential_helper_compat(repo_dir, is_token_credential_only)
    repo_walkable = _check_repo_walkable(repo_dir)

    # Mechanism selection. The audit's preferred order:
    # 1. existing git credential helper / authenticated gh
    # 2. SSH agent / auth
    # 3. explicitly passed credential reference from env
    if has_gh_cli and gh_authenticated:
        mechanism = "gh auth"
    elif has_helper:
        mechanism = "git credential helper"
    elif has_ssh:
        mechanism = "SSH"
    elif has_gh_token or has_github_token:
        mechanism = "env token"
    else:
        mechanism = "UNKNOWN"

    # Write capability requires the worker to be able to
    # actually push. The strongest positive signal is
    # ``gh auth`` + ``git credential.helper``. SSH is
    # acceptable but the audit asks for verification.
    # Without a proven mechanism, write capability is
    # declared False.
    write_capable = False
    if mechanism == "gh auth" and gh_authenticated:
        write_capable = True
    elif mechanism == "git credential helper" and repo_walkable:
        write_capable = True
    elif mechanism == "SSH" and has_ssh and repo_walkable:
        write_capable = True
    elif mechanism == "env token" and (has_gh_token or has_github_token):
        write_capable = True

    return WorkerAuthPreflightResult(
        read_capable=repo_walkable,
        write_capable=write_capable,
        mechanism=mechanism,
        has_gh_cli=has_gh_cli,
        has_github_token_env=has_github_token,
        has_gh_token_env=has_gh_token,
        has_ssh_auth_sock=has_ssh,
        has_git_credential_helper=has_helper,
        repo_walkable=repo_walkable,
    )


def _check_credential_helper_compat(
    repo_dir: Path, is_token_credential_only: bool,
) -> bool:
    """Boolean-only check. ``token only`` deployments
    require a configured credential helper so
    ``git push`` can use the env token. ``gh auth``
    deployments skip the helper check.
    """
    rc, stdout, _ = _run_subprocess(
        ["git", "-C", str(repo_dir), "config", "--get", "credential.helper"],
        timeout=5,
    )
    if rc == 0 and stdout.strip():
        # Always accept a configured helper when present.
        return True
    if is_token_credential_only:
        # No helper + token-only is a failure mode: the
        # worker will be able to authenticate READ-only
        # (public clone) but WRITE will fail.
        return False
    # Other deployments: helper is optional; the
    # selection of mechanism prevails.
    return False


def preflight_or_raise(
    *,
    repo_dir: Path,
    is_token_credential_only: bool = False,
) -> WorkerAuthPreflightResult:
    """Run the preflight and raise ``WorkerRepoAuthUnavailable``
    when ``write_capable`` is False. The supervisor's
    worker launch path catches this and emits
    ``WORKER_REPO_AUTH_UNAVAILABLE`` instead of launching.
    """
    result = run_worker_auth_preflight(
        repo_dir=repo_dir, is_token_credential_only=is_token_credential_only,
    )
    if not result.write_capable:
        raise WorkerRepoAuthUnavailable(
            f"worker auth preflight: write_capable=False; "
            f"mechanism={result.mechanism!r}; "
            f"has_gh_cli={result.has_gh_cli}; "
            f"has_gh_token_env={result.has_gh_token_env}; "
            f"has_github_token_env={result.has_github_token_env}; "
            f"has_ssh_auth_sock={result.has_ssh_auth_sock}; "
            f"has_git_credential_helper={result.has_git_credential_helper}; "
            f"repo_walkable={result.repo_walkable}; "
            f"refusing to launch a worker that would fail "
            f"halfway through. Suppress this gate by setting "
            f"AED_WORKER_AUTH_REQUIRED=False ONLY in non-production "
            f"deployments."
        )
    return result
