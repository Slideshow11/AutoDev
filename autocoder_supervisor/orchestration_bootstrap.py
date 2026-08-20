"""Canonical bootstrap for a new supervisor-owned orchestration run.

The resolver remains fail-closed: it never substitutes ``STATE_DIR``.
This module provides the positive initialization step used only after the
supervisor singleton lock is held and no configured or persisted root exists.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Sequence

from .orchestration_state_root import (
    OrchestrationRootError,
    OrchestrationRootUnverified,
    _import_write_json,
    init_run_state_safely,
    persist_orchestration_state_root,
    resolve_orchestration_state_root,
)


_HEX_SHA_RE = re.compile(r"\A[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


def _write_json(
    path: Path,
    payload: Dict[str, Any],
    writer: Optional[Callable[[Path, Dict[str, Any]], None]],
) -> None:
    (writer or _import_write_json())(path, payload)


@contextmanager
def _bootstrap_lock(run_state_path: Path) -> Iterator[None]:
    """Serialize bootstrappers even when their private state dirs differ."""
    lock_path = run_state_path.with_name(run_state_path.name + ".bootstrap.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _validate_sha(value: str, label: str) -> str:
    if not isinstance(value, str) or not _HEX_SHA_RE.match(value):
        raise OrchestrationRootUnverified(
            f"{label} must be a full 40- or 64-character lowercase hex SHA; "
            f"got {value!r}"
        )
    return value


def _git_value(checkout: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(checkout), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def _slug(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise OrchestrationRootError(f"invalid {label}: {value!r}")
    return value


def _matching_existing_root(
    *,
    pr_root: Path,
    repo_owner: str,
    repo_name: str,
    pr_number: int,
    starting_head: str,
) -> Optional[Path]:
    """Adopt one valid interrupted bootstrap; reject every invalid candidate."""
    if not pr_root.exists():
        return None
    candidates = sorted(
        p for p in pr_root.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    if not candidates:
        return None
    valid: list[Path] = []
    from autocoder_orchestration.context import RunContext
    from autocoder_orchestration.state_machine import StateMachine
    for candidate in candidates:
        try:
            context_payload = json.loads(
                (candidate / "run_context.json").read_text(encoding="utf-8")
            )
            state_payload = json.loads(
                (candidate / "state.json").read_text(encoding="utf-8")
            )
            context = RunContext.from_dict(context_payload)
            StateMachine.from_dict(state_payload)
        except Exception as exc:
            raise OrchestrationRootUnverified(
                f"existing orchestration candidate {candidate} is invalid; "
                "refusing to allocate a replacement"
            ) from exc
        if (
            context.repo_owner != repo_owner
            or context.repo_name != repo_name
            or context.pr_number != pr_number
            or context.current_authorized_head != starting_head
            or Path(context.state_path).resolve() != candidate.resolve()
        ):
            raise OrchestrationRootUnverified(
                f"existing orchestration candidate {candidate} is bound to a "
                "different repo, PR, run path, or starting head"
            )
        valid.append(candidate)
    if len(valid) != 1:
        raise OrchestrationRootUnverified(
            f"found {len(valid)} authoritative orchestration candidates under "
            f"{pr_root}; refusing split-brain selection"
        )
    return valid[0]


def _persist_and_verify(
    *,
    root: Path,
    run_state_path: Path,
    repo_owner: str,
    repo_name: str,
    pr_number: int,
    run_id: str,
    writer: Optional[Callable[[Path, Dict[str, Any]], None]],
) -> str:
    persist_orchestration_state_root(
        state_root=str(root),
        run_state_path=run_state_path,
        repo_owner=repo_owner,
        repo_name=repo_name,
        run_id=run_id,
        pr_number=pr_number,
        expected_existing_run_id=run_id,
        writer=writer,
    )
    return resolve_orchestration_state_root(
        env={},
        run_state_path=run_state_path,
        expected_repo=f"{repo_owner}/{repo_name}",
        expected_run_id=run_id,
        expected_pr_number=pr_number,
    )


def bootstrap_orchestration_state_root(
    *,
    state_dir: Path,
    state_root_parent: Path,
    repo_owner: str,
    repo_name: str,
    pr_number: int,
    branch: Optional[str],
    run_state_path: Path,
    current_authorized_head: Optional[str] = None,
    authorized_base_sha: Optional[str] = None,
    expected_existing_run_id: Optional[str] = None,
    run_id: Optional[str] = None,
    writer: Optional[Callable[[Path, Dict[str, Any]], None]] = None,
    local_checkout: Optional[Path] = None,
    base_branch: str = "main",
    required_ci_jobs: Sequence[str] = (),
    implementation_worker_command: Sequence[str] = ("true",),
) -> str:
    """Create exactly one canonical run root, persist it, and verify it."""
    if not isinstance(state_dir, Path) or not isinstance(state_root_parent, Path):
        raise OrchestrationRootError("state_dir and state_root_parent must be Paths")
    if state_dir.resolve() == state_root_parent.resolve():
        raise OrchestrationRootError(
            "STATE_DIR itself must never be treated as the orchestration root"
        )
    owner = _slug(repo_owner, "repo_owner")
    name = _slug(repo_name, "repo_name")
    if isinstance(pr_number, bool) or int(pr_number) <= 0:
        raise OrchestrationRootError("pr_number must be a positive integer")
    checkout = Path(local_checkout or os.environ.get(
        "AED_SUPERVISOR_WORKING_CHECKOUT", os.getcwd()
    )).resolve()
    feature_branch = branch or _git_value(checkout, "branch", "--show-current")
    if not feature_branch:
        raise OrchestrationRootUnverified("feature branch could not be identified")
    starting_head = _validate_sha(
        current_authorized_head or _git_value(checkout, "rev-parse", "HEAD"),
        "current_authorized_head",
    )
    base_sha = authorized_base_sha or _git_value(
        checkout, "merge-base", "HEAD", f"origin/{base_branch}"
    )
    base_sha = _validate_sha(base_sha, "authorized_base_sha")
    parent = state_root_parent.resolve()
    pr_root = parent / owner / name / f"pr-{int(pr_number)}"

    with _bootstrap_lock(run_state_path):
        run_state = init_run_state_safely(
            run_state_path=run_state_path, writer=writer
        )
        prior_root = run_state.get("orchestration_state_root")
        if prior_root:
            expected_run = expected_existing_run_id or run_state.get(
                "last_bound_run_id"
            )
            return resolve_orchestration_state_root(
                env={},
                run_state_path=run_state_path,
                expected_repo=f"{owner}/{name}",
                expected_run_id=expected_run,
                expected_pr_number=int(pr_number),
            )
        for key, expected in (
            ("last_bound_repo_owner", owner),
            ("last_bound_repo_name", name),
            ("last_bound_pr_number", int(pr_number)),
        ):
            if key in run_state and run_state[key] != expected:
                raise OrchestrationRootUnverified(
                    f"RUN_STATE {key}={run_state[key]!r} does not match {expected!r}"
                )

        interrupted = _matching_existing_root(
            pr_root=pr_root,
            repo_owner=owner,
            repo_name=name,
            pr_number=int(pr_number),
            starting_head=starting_head,
        )
        if interrupted is not None:
            payload = json.loads(
                (interrupted / "run_context.json").read_text(encoding="utf-8")
            )
            interrupted_run_id = str(payload.get("run_id") or "")
            return _persist_and_verify(
                root=interrupted,
                run_state_path=run_state_path,
                repo_owner=owner,
                repo_name=name,
                pr_number=int(pr_number),
                run_id=interrupted_run_id,
                writer=writer,
            )

        final_run_id = run_id or (
            f"aed-{owner}-{name}-pr{int(pr_number)}-{uuid.uuid4().hex[:12]}"
        )
        if expected_existing_run_id and final_run_id != expected_existing_run_id:
            raise OrchestrationRootUnverified(
                "requested run_id does not match expected_existing_run_id"
            )
        final_root = pr_root / final_run_id
        pr_root.mkdir(parents=True, exist_ok=True)
        temp_root = pr_root / f".{final_run_id}.tmp-{uuid.uuid4().hex[:8]}"
        temp_root.mkdir(mode=0o700)

        from autocoder_orchestration.context import make_run_context
        from autocoder_orchestration.state_machine import StateMachine
        from autocoder_orchestration.store import StateStore

        task_spec_path = temp_root / "task_specification.json"
        task_spec_payload = {
            "schema_version": "autocoder.bootstrap_task.v1",
            "repo": f"{owner}/{name}",
            "pr_number": int(pr_number),
            "starting_head": starting_head,
            "feature_branch": feature_branch,
        }
        task_spec_blob = json.dumps(
            task_spec_payload, sort_keys=True, separators=(",", ":")
        )
        task_spec_path.write_text(task_spec_blob, encoding="utf-8")
        context = make_run_context(
            repo_owner=owner,
            repo_name=name,
            local_checkout=str(checkout),
            base_branch=base_branch,
            authorized_base_sha=base_sha,
            feature_branch=feature_branch,
            task_specification_path=str(final_root / task_spec_path.name),
            task_specification_sha256=hashlib.sha256(
                task_spec_blob.encode("utf-8")
            ).hexdigest(),
            required_ci_jobs=list(required_ci_jobs),
            implementation_worker_command=list(implementation_worker_command),
            evidence_root=str(parent / "evidence"),
            state_root=str(parent),
            pr_number=int(pr_number),
            current_authorized_head=starting_head,
            run_id=final_run_id,
        )
        if Path(context.state_path).resolve() != final_root.resolve():
            raise OrchestrationRootUnverified(
                "canonical RunContext state_path does not equal allocated run root"
            )
        store = StateStore(str(temp_root))
        store.write_atomic("run_context.json", context.to_dict())
        store.write_atomic("state.json", StateMachine().to_dict())
        os.replace(temp_root, final_root)
        return _persist_and_verify(
            root=final_root,
            run_state_path=run_state_path,
            repo_owner=owner,
            repo_name=name,
            pr_number=int(pr_number),
            run_id=final_run_id,
            writer=writer,
        )


def should_bootstrap_orchestration_state_root(
    *,
    env: Optional[Dict[str, str]] = None,
    run_state_path: Optional[Path] = None,
) -> bool:
    """Return true only for a missing, parseable, unbound RUN_STATE."""
    active_env = env if env is not None else os.environ
    if active_env.get("AED_ORCHESTRATION_STATE_ROOT"):
        return False
    if run_state_path is None or not run_state_path.exists():
        return True
    try:
        payload = json.loads(run_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and not payload.get(
        "orchestration_state_root"
    )
