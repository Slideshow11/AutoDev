"""Immutable run context for an AutoDev run.

The :class:`RunContext` is the typed, immutable manifest that binds
every other component of the control plane to a single run. It is
the trust root of the orchestration layer.

A run context is created once per PR (or per pending run before a PR
exists) and is never mutated. Downstream code reads the context, never
writes it.

Roles are explicit string identifiers so that no caller can confuse
worker output with controller output, or verifier output with
implementation-worker output.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


#: Role identifiers used by the controller for action authorization.
ACTOR_CONTROLLER = "controller"
ACTOR_IMPL_WORKER = "implementation_worker"
ACTOR_VERIFIER = "independent_verifier"
ACTOR_HUMAN = "human_operator"
ACTOR_OBSERVER = "strict_observer"
ACTOR_CANDIDATE_BUILDER = "candidate_builder"

ALL_ACTORS = frozenset(
    {
        ACTOR_CONTROLLER,
        ACTOR_IMPL_WORKER,
        ACTOR_VERIFIER,
        ACTOR_HUMAN,
        ACTOR_OBSERVER,
        ACTOR_CANDIDATE_BUILDER,
    }
)


SCHEMA_VERSION = "autocoder.run_context.v1"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_path(path: Optional[Path], label: str) -> Optional[str]:
    """Validate that a path is absolute and contains no symlinks.

    The orchestration layer does not allow symlink paths inside the
    durable state tree because a symlink could be redirected between
    the time the controller validates it and the time it writes
    state.
    """
    if path is None:
        return None
    text = str(path)
    if not os.path.isabs(text):
        raise ValueError(f"{label} must be absolute: {text!r}")
    p = Path(text)
    if p.exists() and p.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {text!r}")
    return text


@dataclasses.dataclass(frozen=True)
class RunContext:
    """Immutable run context for a single AutoDev run.

    The context is bound to either a pending path (no PR yet) or a
    PR-scoped path. The path is hierarchical so that one auto_dev
    evidence root can hold multiple repositories and PRs without
    collisions.
    """

    # === Identification ===
    schema_version: str
    run_id: str
    created_at: str
    # === Repository binding ===
    repo_owner: str
    repo_name: str
    local_checkout: str
    # === GitHub binding ===
    base_branch: str
    authorized_base_sha: str
    feature_branch: str
    pr_number: Optional[int]
    current_authorized_head: Optional[str]
    # === Task specification ===
    task_specification_path: str
    task_specification_sha256: str
    # === Policies ===
    required_ci_jobs: tuple
    reviewer_policy: str
    quiet_window_seconds: int
    # === Commands (NOT shell-evaluated) ===
    implementation_worker_command: tuple
    verifier_command: Optional[tuple]
    verifier_handoff_policy: str
    # === Authorization boundaries ===
    permitted_mutations: tuple
    human_only_actions: tuple
    # === Locations ===
    evidence_root: str
    state_root: str
    # === Next-wave policy ===
    next_wave_policy: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported run context schema: {self.schema_version!r}"
            )
        if not self.repo_owner or "/" in self.repo_owner:
            raise ValueError(f"invalid repo_owner: {self.repo_owner!r}")
        if not self.repo_name or "/" in self.repo_name:
            raise ValueError(f"invalid repo_name: {self.repo_name!r}")
        if not self.run_id:
            raise ValueError("run_id must be non-empty")
        if not self.feature_branch:
            raise ValueError("feature_branch must be non-empty")
        if not self.base_branch:
            raise ValueError("base_branch must be non-empty")
        if not self.authorized_base_sha or (len(self.authorized_base_sha) != 40 and len(self.authorized_base_sha) != 64):
            raise ValueError(
                f"authorized_base_sha must be 64 lowercase hex chars: {self.authorized_base_sha!r}"
            )
        if self.current_authorized_head is not None and (len(self.current_authorized_head) != 40 and len(self.current_authorized_head) != 64):
            raise ValueError(
                "current_authorized_head must be 64 lowercase hex chars when set"
            )
        if not self.task_specification_path:
            raise ValueError("task_specification_path must be non-empty")
        if not self.task_specification_sha256 or len(self.task_specification_sha256) != 64:
            raise ValueError("task_specification_sha256 must be 64 lowercase hex chars")
        if self.quiet_window_seconds < 0:
            raise ValueError("quiet_window_seconds must be non-negative")
        for label, path in [
            ("local_checkout", self.local_checkout),
            ("task_specification_path", self.task_specification_path),
            ("evidence_root", self.evidence_root),
            ("state_root", self.state_root),
        ]:
            _check_path(Path(path), label)
        # Commands must be tuples of strings (sanitised; no shell evaluation)
        for label, cmd in [
            ("implementation_worker_command", self.implementation_worker_command),
            ("verifier_command", self.verifier_command),
        ]:
            if cmd is not None:
                if not isinstance(cmd, tuple):
                    raise ValueError(f"{label} must be a tuple of strings")
                for arg in cmd:
                    if not isinstance(arg, str):
                        raise ValueError(f"{label} entries must be strings")

    @property
    def state_path(self) -> str:
        """Hierarchical, PR-scoped state directory path."""
        if self.pr_number is None:
            return os.path.join(
                self.state_root,
                self.repo_owner,
                self.repo_name,
                "pending",
                self.run_id,
            )
        return os.path.join(
            self.state_root,
            self.repo_owner,
            self.repo_name,
            f"pr-{self.pr_number}",
            self.run_id,
        )

    @property
    def evidence_path(self) -> str:
        if self.pr_number is None:
            return os.path.join(
                self.evidence_root,
                self.repo_owner,
                self.repo_name,
                "pending",
                self.run_id,
            )
        return os.path.join(
            self.evidence_root,
            self.repo_owner,
            self.repo_name,
            f"pr-{self.pr_number}",
            self.run_id,
        )

    def _bound_pr_paths(self) -> bool:
        """True iff the run is bound to a PR-scoped path."""
        return self.pr_number is not None

    def with_promoted_pr(self, pr_number: int, pr_head: str) -> "RunContext":
        """Return a new context with the PR number and head bound.

        Pending runs are promoted to PR-scoped runs atomically. The
        run-id is preserved; state tree moves to the PR-scoped path.
        """
        # bool is a subclass of int; reject it explicitly.
        if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
            raise ValueError("pr_number must be a positive integer (not a bool)")
        # PR head sha accepts 40- or 64-character lowercase hex.
        if (
            not isinstance(pr_head, str)
            or (len(pr_head) != 40 and len(pr_head) != 64)
            or not all(c in "0123456789abcdef" for c in pr_head)
        ):
            raise ValueError("pr_head must be 40 or 64 lowercase hex chars")
        return dataclasses.replace(self, pr_number=pr_number, current_authorized_head=pr_head)

    def with_new_head(self, new_head: str) -> "RunContext":
        """Return a new context with current_authorized_head updated.

        Used only when the controller explicitly observes a new head
        (e.g. after a repair push). The candidate, verifier record,
        and merge authorization must be invalidated separately.
        """
        if not isinstance(new_head, str) or len(new_head) != 64:
            raise ValueError("new_head must be 64 lowercase hex chars")
        return dataclasses.replace(self, current_authorized_head=new_head)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "repo_owner": self.repo_owner,
            "repo_name": self.repo_name,
            "local_checkout": self.local_checkout,
            "base_branch": self.base_branch,
            "authorized_base_sha": self.authorized_base_sha,
            "feature_branch": self.feature_branch,
            "pr_number": self.pr_number,
            "current_authorized_head": self.current_authorized_head,
            "task_specification_path": self.task_specification_path,
            "task_specification_sha256": self.task_specification_sha256,
            "required_ci_jobs": list(self.required_ci_jobs),
            "reviewer_policy": self.reviewer_policy,
            "quiet_window_seconds": self.quiet_window_seconds,
            "implementation_worker_command": list(self.implementation_worker_command),
            "verifier_command": list(self.verifier_command) if self.verifier_command is not None else None,
            "verifier_handoff_policy": self.verifier_handoff_policy,
            "permitted_mutations": list(self.permitted_mutations),
            "human_only_actions": list(self.human_only_actions),
            "evidence_root": self.evidence_root,
            "state_root": self.state_root,
            "next_wave_policy": self.next_wave_policy,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "RunContext":
        if not isinstance(payload, dict):
            raise ValueError("run context must be a dict")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported run context schema: {payload.get('schema_version')!r}"
            )
        required = {
            "run_id": str,
            "repo_owner": str,
            "repo_name": str,
            "local_checkout": str,
            "base_branch": str,
            "authorized_base_sha": str,
            "feature_branch": str,
            "task_specification_path": str,
            "task_specification_sha256": str,
            "required_ci_jobs": list,
            "evidence_root": str,
            "state_root": str,
        }
        for key, kind in required.items():
            if key not in payload:
                raise ValueError(f"run context missing required field: {key!r}")
            if not isinstance(payload[key], kind):
                raise ValueError(f"run context field {key!r} has wrong type")
        for label in ("implementation_worker_command",):
            v = payload.get(label)
            if v is not None and not isinstance(v, list):
                raise ValueError(f"run context field {label!r} must be a list of strings")
            if v is not None:
                for entry in v:
                    if not isinstance(entry, str):
                        raise ValueError(f"run context field {label!r} entries must be strings")
        return cls(
            schema_version=SCHEMA_VERSION,
            run_id=payload["run_id"],
            created_at=payload.get("created_at") or _now_iso(),
            repo_owner=payload["repo_owner"],
            repo_name=payload["repo_name"],
            local_checkout=payload["local_checkout"],
            base_branch=payload["base_branch"],
            authorized_base_sha=payload["authorized_base_sha"],
            feature_branch=payload["feature_branch"],
            pr_number=payload.get("pr_number"),
            current_authorized_head=payload.get("current_authorized_head"),
            task_specification_path=payload["task_specification_path"],
            task_specification_sha256=payload["task_specification_sha256"],
            required_ci_jobs=tuple(payload["required_ci_jobs"]),
            reviewer_policy=payload.get("reviewer_policy", "exact_head_approval"),
            quiet_window_seconds=int(payload.get("quiet_window_seconds", 180)),
            implementation_worker_command=tuple(
                payload.get("implementation_worker_command") or []
            ),
            verifier_command=(
                tuple(payload["verifier_command"])
                if payload.get("verifier_command") is not None
                else None
            ),
            verifier_handoff_policy=payload.get(
                "verifier_handoff_policy", "fresh_session_required"
            ),
            permitted_mutations=tuple(payload.get("permitted_mutations") or ()),
            human_only_actions=tuple(payload.get("human_only_actions") or ()),
            evidence_root=payload["evidence_root"],
            state_root=payload["state_root"],
            next_wave_policy=payload.get("next_wave_policy", "explicit_only"),
        )

    def sha256(self) -> str:
        """Stable hash of the context payload (used to invalidate
        candidates and readiness certificates).
        """
        import json
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def generate_run_id() -> str:
    """Generate a fresh run id."""
    return uuid.uuid4().hex


def make_run_context(
    *,
    repo_owner: str,
    repo_name: str,
    local_checkout: str,
    base_branch: str,
    authorized_base_sha: str,
    feature_branch: str,
    task_specification_path: str,
    task_specification_sha256: str,
    required_ci_jobs: list,
    implementation_worker_command: list,
    evidence_root: str,
    state_root: str,
    pr_number: Optional[int] = None,
    current_authorized_head: Optional[str] = None,
    reviewer_policy: str = "exact_head_approval",
    quiet_window_seconds: int = 180,
    verifier_command: Optional[list] = None,
    verifier_handoff_policy: str = "fresh_session_required",
    permitted_mutations: Optional[list] = None,
    human_only_actions: Optional[list] = None,
    next_wave_policy: str = "explicit_only",
    run_id: Optional[str] = None,
) -> RunContext:
    """Construct a :class:`RunContext` with sensible defaults."""
    if permitted_mutations is None:
        permitted_mutations = [
            "create_branch",
            "commit",
            "push",
            "open_pr",
            "push_to_pr",
            "resolve_review_thread",
        ]
    if human_only_actions is None:
        human_only_actions = [
            "merge_pr",
            "enable_auto_merge",
            "dismiss_review",
            "authorize_next_wave",
        ]
    return RunContext(
        schema_version=SCHEMA_VERSION,
        run_id=run_id or generate_run_id(),
        created_at=_now_iso(),
        repo_owner=repo_owner,
        repo_name=repo_name,
        local_checkout=local_checkout,
        base_branch=base_branch,
        authorized_base_sha=authorized_base_sha,
        feature_branch=feature_branch,
        pr_number=pr_number,
        current_authorized_head=current_authorized_head,
        task_specification_path=task_specification_path,
        task_specification_sha256=task_specification_sha256,
        required_ci_jobs=tuple(required_ci_jobs),
        reviewer_policy=reviewer_policy,
        quiet_window_seconds=quiet_window_seconds,
        implementation_worker_command=tuple(implementation_worker_command),
        verifier_command=tuple(verifier_command) if verifier_command is not None else None,
        verifier_handoff_policy=verifier_handoff_policy,
        permitted_mutations=tuple(permitted_mutations),
        human_only_actions=tuple(human_only_actions),
        evidence_root=evidence_root,
        state_root=state_root,
        next_wave_policy=next_wave_policy,
    )
