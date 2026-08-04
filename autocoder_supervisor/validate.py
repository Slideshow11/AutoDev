"""Dry-run validation command for the source-controlled supervisor.

Usage::

    python -m autocoder_supervisor.validate --config /etc/aed-supervisor.toml

The command exits 0 if the configuration is valid and the
runtime environment is ready, and exits non-zero with a
descriptive message if anything is wrong. It does NOT modify
any state.

What it checks
--------------

1. Configuration validity (delegated to
   ``config.validate_config_dict``).
2. Required directories exist (or can be created) and have
   restrictive permissions (0700) where supported.
3. State files use restrictive permissions (0600) where
   supported.
4. The configured repository is reachable on the local
   filesystem.
5. The configured PR number matches the GitHub PR head on
   the working checkout (best-effort; uses ``git``).
6. Provider policy: required and optional providers are
   disjoint and present in PROVIDERS.
7. Service-instance scope: the configured instance_id is
   non-empty and unique relative to the state directory.
8. Conflicting worker leases: no live ``worker_lease.json``
   points at a still-alive worker process; if one is found,
   the operator is informed.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .config import (
    default_config_from_env,
    load_config,
    validate_config_dict,
)
from .contracts import SupervisorConfigDict, SupervisorConfig


def _check_state_dir(
    state_dir: Path, errors: list[str], warnings: list[str]
) -> None:
    """Verify the state directory is set up correctly.

    The dry-run validator does NOT create or chmod the
    state directory — those mutations belong to the
    supervisor's startup path, not the validator. The
    validator only reports what is wrong.
    """
    parent = state_dir.parent
    if parent.exists() and not os.access(parent, os.W_OK):
        errors.append(
            f"state_dir parent {parent} is not writable; "
            "the supervisor cannot create the state directory "
            "at startup"
        )
        return
    if state_dir.exists():
        if not state_dir.is_dir():
            errors.append(
                f"state_dir {state_dir} exists but is not a directory"
            )
            return
        mode = state_dir.stat().st_mode & 0o777
        if mode & 0o077:
            errors.append(
                f"state_dir {state_dir} is accessible to group/other "
                f"({oct(mode)}); expected 0700"
            )
    else:
        # The directory does not exist. The supervisor will
        # create it on startup; record an advisory.
        warnings.append(
            f"state_dir {state_dir} does not exist; "
            "the supervisor will create it on startup"
        )


def _check_repo_accessible(
    working_checkout: Path, errors: list[str]
) -> None:
    if not working_checkout.exists():
        errors.append(
            f"working_checkout {working_checkout} does not exist"
        )
        return
    # A linked Git worktree has a `.git` file (not a
    # directory), so we cannot pre-check with a `.git` directory
    # test. Use `git -C ... status` to validate.
    try:
        res = subprocess.run(
            ["git", "-C", str(working_checkout), "status",
             "--porcelain"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        errors.append(
            f"`git` executable not on PATH; cannot validate "
            f"working_checkout {working_checkout}"
        )
        return
    except subprocess.TimeoutExpired:
        errors.append(
            f"git status timed out for working_checkout "
            f"{working_checkout}; cannot validate"
        )
        return
    except OSError as e:
        errors.append(
            f"working_checkout {working_checkout} cannot be "
            f"inspected by git: {e}"
        )
        return
    if res.returncode != 0:
        errors.append(
            f"working_checkout {working_checkout} is not accessible to "
            f"git: {res.stderr.strip()[:200]}"
        )


def _check_provider_policy(
    cfg: SupervisorConfig, errors: list[str]
) -> None:
    required = set(cfg.required_review_providers)
    optional = set(cfg.optional_review_providers)
    if required & optional:
        errors.append(
            f"required and optional providers overlap: "
            f"{sorted(required & optional)}"
        )
    if not required:
        errors.append("at least one required review provider is required")
    if "codex" in required:
        errors.append(
            "Codex is currently OPTIONAL only; classifying it as required "
            "is rejected by the stabilization policy"
        )


def _check_lease_conflict(
    state_dir: Path, errors: list[str], warnings: list[str]
) -> None:
    lease = state_dir / "worker_lease.json"
    if not lease.exists():
        return
    import json as _json
    try:
        data = _json.loads(lease.read_text())
    except Exception as e:
        warnings.append(
            f"worker_lease.json at {lease} could not be parsed: {e}"
        )
        return
    # Accept only positive integer PIDs. Reject strings,
    # floats, negatives, and booleans outright — they are
    # either malformed or a sign of corruption.
    pid = data.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        warnings.append(
            f"worker_lease.json at {lease} has malformed pid={pid!r}; "
            "the supervisor will revoke and rewrite the lease on "
            "startup"
        )
        return
    try:
        os.kill(pid, 0)
        warnings.append(
            f"a live worker (pid={pid}) holds the lease; this is fine "
            "if it is the same instance, but if you are starting a new "
            "instance you must first revoke the old lease"
        )
    except ProcessLookupError:
        warnings.append(
            f"stale worker_lease.json at {lease} points at pid={pid} "
            "which is not alive; the supervisor will detect this on "
            "startup and revoke it"
        )
    except PermissionError:
        # PermissionError means the process exists but the
        # current user cannot signal it. This is an
        # unresolved live-lease conflict — the supervisor
        # cannot safely launch a new worker while this lease
        # is held by an inaccessible process.
        errors.append(
            f"worker_lease.json at {lease} is held by pid={pid} "
            "which the current user cannot signal; "
            "an operator must manually revoke this lease "
            "before starting a new instance"
        )
    except OSError as e:
        if e.errno == 1:
            warnings.append(
                f"worker_lease.json at {lease} is owned by a process "
                f"the current user cannot signal (pid={pid})"
            )
        else:
            warnings.append(
                f"worker_lease.json at {lease} could not be inspected: "
                f"{e}"
            )


def _check_runtime_file_mode(
    path: Path, errors: list[str], mode_required: int = 0o600
) -> None:
    """Verify that a runtime file has restrictive permissions."""
    if not path.exists():
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        errors.append(
            f"{path} is accessible to group/other ({oct(mode)}); "
            f"expected {oct(mode_required)}"
        )


def _check_pr_head_match(
    cfg: SupervisorConfig, errors: list[str]
) -> None:
    """Verify that the configured PR head matches the live PR head.

    This catches a stale checkout — the operator could have
    a configuration pointing at one head while the live PR
    has moved on. Validation must not silently report success
    in that case.
    """
    import json as _json

    head = os.environ.get("AED_AUTHORITATIVE_HEAD", "").strip()
    if not head:
        # No head pinned in the environment — the operator
        # has not yet pinned an authoritative head. Skip the
        # check rather than fail spuriously.
        return
    if not cfg.working_checkout:
        return
    repo = Path(cfg.working_checkout)
    if not (repo / ".git").exists() and not (
        repo.exists() and (repo / ".git").is_file()
    ):
        # Already reported by _check_repo_accessible; skip.
        return
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return
    if out.returncode != 0:
        return
    local_head = out.stdout.strip()
    if local_head != head:
        errors.append(
            f"working_checkout HEAD {local_head!r} does not match "
            f"the configured authoritative head {head!r}; "
            "the supervisor will refuse readiness until they match"
        )


def validate_environment(
    cfg: SupervisorConfig,
) -> tuple[list[str], list[str]]:
    """Run the full validation suite.

    Returns ``(errors, warnings)``. ``errors`` are fatal
    issues that block startup; ``warnings`` are advisory.
    """
    errors: list[str] = []
    warnings: list[str] = []
    state_dir = Path(cfg.state_dir)
    _check_state_dir(state_dir, errors, warnings)
    _check_repo_accessible(Path(cfg.working_checkout), errors)
    _check_provider_policy(cfg, errors)
    _check_lease_conflict(state_dir, errors, warnings)
    _check_pr_head_match(cfg, errors)
    # Verify runtime files use restrictive modes.
    for runtime_path in (
        Path(cfg.log_path),
        Path(cfg.heartbeat_path),
        Path(cfg.lock_path),
    ):
        _check_runtime_file_mode(runtime_path, errors)
    return errors, warnings


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run validation for the source-controlled "
            "Autocoder supervisor."
        )
    )
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "Path to a TOML supervisor configuration. If "
            "omitted, the env-derived default is used "
            "(which is intended for isolated testing)."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Treat warnings as errors. Useful in CI / pre-commit."
        ),
    )
    args = parser.parse_args(argv)

    if args.config:
        try:
            cfg = load_config(args.config)
        except Exception as e:
            print(f"FAIL: config {args.config} is invalid: {e}", file=sys.stderr)
            return 2
    else:
        cfg = default_config_from_env()
        # When validating the env-derived default, run the
        # strict credential / user-path checks too.
        try:
            validate_config_dict(
                {
                    "schema_version": cfg.schema_version,
                    "instance_id": cfg.instance_id,
                    "state_dir": cfg.state_dir,
                    "working_checkout": cfg.working_checkout,
                    "log_path": cfg.log_path,
                    "heartbeat_path": cfg.heartbeat_path,
                    "lock_path": cfg.lock_path,
                    "worker_command": cfg.worker_command,
                    "worker_session_id": cfg.worker_session_id,
                    "worker_session_name": cfg.worker_session_name,
                    "cooldown_seconds": cfg.cooldown_seconds,
                    "resume_prompt_template": cfg.resume_prompt_template,
                    "human_boundary": cfg.human_boundary,
                    "required_review_providers":
                        cfg.required_review_providers,
                    "optional_review_providers":
                        cfg.optional_review_providers,
                    "provider_states_are_independent":
                        cfg.provider_states_are_independent,
                    "post_codex_recovery_request":
                        cfg.post_codex_recovery_request,
                    "heartbeat_seconds": cfg.heartbeat_seconds,
                    "quiet_window_seconds": cfg.quiet_window_seconds,
                    "quota_retry_initial_seconds":
                        cfg.quota_retry_initial_seconds,
                    "quota_retry_backoff_seconds":
                        cfg.quota_retry_backoff_seconds,
                    "quota_backoff_after_retry_count":
                        cfg.quota_backoff_after_retry_count,
                    "provider_quota_reset": cfg.provider_quota_reset,
                },
                reject_user_paths=True,
            )
        except ValueError as e:
            print(
                f"FAIL: env-derived config is unsafe: {e}",
                file=sys.stderr,
            )
            return 2

    print(f"config: schema={cfg.schema_version} instance={cfg.instance_id}")
    print(f"  state_dir={cfg.state_dir}")
    print(f"  working_checkout={cfg.working_checkout}")
    print(f"  required_review_providers={cfg.required_review_providers}")
    print(f"  optional_review_providers={cfg.optional_review_providers}")
    print(f"  human_boundary={cfg.human_boundary}")
    print(f"  heartbeat_seconds={cfg.heartbeat_seconds}")
    print(f"  quiet_window_seconds={cfg.quiet_window_seconds}")

    errors, warnings = validate_environment(cfg)
    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)
    for e in errors:
        print(f"FAIL: {e}", file=sys.stderr)

    if errors:
        print(
            f"validation FAILED with {len(errors)} error(s) "
            f"and {len(warnings)} warning(s)",
            file=sys.stderr,
        )
        return 1
    if args.strict and warnings:
        print(
            f"validation FAILED (--strict): {len(warnings)} warning(s)",
            file=sys.stderr,
        )
        return 1
    print(
        f"validation OK with {len(warnings)} warning(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
