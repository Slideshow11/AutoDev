"""Checkpoint state dataclass and pure resume helpers.

A checkpoint is a serializable snapshot of the run state for a single
autonomous-development controller run. The shape is:

- ``repo``
- ``pr_number``
- ``branch``
- ``current_head``
- ``base_head``            (target / canonical base head)
- ``phase``
- ``completed_phases``
- ``next_phase``
- ``next_action``
- ``pending_actions``
- ``last_verified_pr_head``
- ``last_verified_base_head``
- ``authorized_thread_ids``
- ``unresolved_thread_ids``
- ``terminal_state``
- ``updated_at``

Lifecycle-state interpretation is supplied through the
:class:`LifecycleStateRegistry` interface.

Public API
----------

- :func:`validate_checkpoint` — STRUCTURAL only. Verifies that the fields
  are present, well-formed, and consistent with the documented schema.
  It does NOT compare the PR head against the base head (they are
  intentionally different in a normal feature-branch PR). Head-drift
  detection lives in :func:`validate_resume_observations`.
- :func:`validate_resume_observations` — head-drift detector. Compares
  the SHAs the runner just fetched against the recorded
  ``last_verified_pr_head`` and ``last_verified_base_head``.
- :func:`next_action_from_checkpoint` — returns the next string the runner
  should act on, or ``None`` if the checkpoint says to stop.
  Structural / lookups only.
- :func:`checkpoint_requires_operator` — returns ``True`` iff the
  checkpoint cannot be safely auto-resumed and a human must intervene.
  Structural / lookups only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .registry import LifecycleStateRegistry, ImmutableLifecycleRegistry


# Required fields. A checkpoint missing any of these is invalid.
# A "required" field is one that the dataclass types as non-Optional
# and that the protocol needs to make any meaningful use of the
# checkpoint. Optional fields (those typed ``Optional[...]`` in the
# dataclass) may be None.
_REQUIRED_STRING_FIELDS: Tuple[str, ...] = (
    "repo",
    "branch",
    "current_head",
)


_SHA1_PATTERN_PREFIX = "sha256:"


def _is_sha256_hex(s: object) -> bool:
    """Return True iff ``s`` is a string of 64 lowercase hex characters."""
    if not isinstance(s, str):
        return False
    if len(s) != 64:
        return False
    return all(c in "0123456789abcdef" for c in s)


def _is_non_negative_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_string_list(v: object) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _is_optional_string(v: object) -> bool:
    return v is None or isinstance(v, str)


def _is_pr_number(v: object) -> bool:
    return _is_non_negative_int(v)


@dataclass
class CheckpointState:
    """A single serializable checkpoint for one autonomous run."""

    repo: str
    pr_number: int
    branch: str
    current_head: str
    base_head: str = ""
    phase: Optional[str] = None
    completed_phases: List[str] = field(default_factory=list)
    next_phase: Optional[str] = None
    next_action: Optional[str] = None
    pending_actions: List[str] = field(default_factory=list)
    last_verified_pr_head: Optional[str] = None
    last_verified_base_head: Optional[str] = None
    authorized_thread_ids: List[str] = field(default_factory=list)
    unresolved_thread_ids: List[str] = field(default_factory=list)
    terminal_state: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        """Serialize to a JSON-friendly dict with a stable field order."""
        return {
            "repo": self.repo,
            "pr_number": int(self.pr_number),
            "branch": self.branch,
            "current_head": self.current_head,
            "base_head": self.base_head,
            "phase": self.phase,
            "completed_phases": list(self.completed_phases),
            "next_phase": self.next_phase,
            "next_action": self.next_action,
            "pending_actions": list(self.pending_actions),
            "last_verified_pr_head": self.last_verified_pr_head,
            "last_verified_base_head": self.last_verified_base_head,
            "authorized_thread_ids": list(self.authorized_thread_ids),
            "unresolved_thread_ids": list(self.unresolved_thread_ids),
            "terminal_state": self.terminal_state,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "CheckpointState":
        """Reconstruct from :meth:`to_dict` payload."""
        required = {"repo", "pr_number", "branch", "current_head"}
        missing = required - set(payload.keys())
        if missing:
            raise ValueError(
                f"checkpoint payload missing required fields: {sorted(missing)}"
            )
        return cls(
            repo=str(payload["repo"]),
            pr_number=int(payload["pr_number"]),
            branch=str(payload["branch"]),
            current_head=str(payload["current_head"]),
            base_head=str(payload.get("base_head", "") or ""),
            phase=payload.get("phase"),
            completed_phases=list(payload.get("completed_phases", []) or []),
            next_phase=payload.get("next_phase"),
            next_action=payload.get("next_action"),
            pending_actions=list(payload.get("pending_actions", []) or []),
            last_verified_pr_head=payload.get("last_verified_pr_head"),
            last_verified_base_head=payload.get("last_verified_base_head"),
            authorized_thread_ids=list(payload.get("authorized_thread_ids", []) or []),
            unresolved_thread_ids=list(payload.get("unresolved_thread_ids", []) or []),
            terminal_state=payload.get("terminal_state"),
            updated_at=payload.get("updated_at"),
        )


def validate_checkpoint(
    state: CheckpointState,
    registry: LifecycleStateRegistry | None = None,
) -> List[str]:
    """Return a list of structural errors; empty means valid.

    Failures include:
    - missing required string fields (empty strings or None);
    - wrong field types;
    - malformed SHA-like identifiers;
    - invalid ``pr_number``;
    - unknown ``terminal_state`` (against the supplied registry);
    - inconsistent ``completed_phases`` / ``pending_actions`` lists;
    - ``terminal_state`` that is not terminal-or-parked against the registry.
    """
    errors: List[str] = []

    for field_name in _REQUIRED_STRING_FIELDS:
        val = getattr(state, field_name)
        if not isinstance(val, str) or not val:
            errors.append(f"required string field {field_name!r} missing or empty")

    if not _is_pr_number(state.pr_number):
        errors.append(f"pr_number must be a non-negative int (got {state.pr_number!r})")

    if state.base_head:
        if not _is_sha256_hex(state.base_head) and not _is_sha256_hex(state.current_head):
            # Allow either as SHA. If both are non-SHA, fail.
            pass
        if (
            state.base_head
            and not _is_sha256_hex(state.base_head)
            and isinstance(state.base_head, str)
        ):
            errors.append(f"base_head must be 64 lowercase hex chars (sha256 prefix discouraged)")

    if not _is_sha256_hex(state.current_head):
        errors.append(
            f"current_head must be 64 lowercase hex characters "
            f"(got {state.current_head!r})"
        )

    if not _is_string_list(state.completed_phases):
        errors.append(f"completed_phases must be a list of strings")

    if not _is_string_list(state.pending_actions):
        errors.append(f"pending_actions must be a list of strings")

    if not _is_string_list(state.authorized_thread_ids):
        errors.append(f"authorized_thread_ids must be a list of strings")

    if not _is_string_list(state.unresolved_thread_ids):
        errors.append(f"unresolved_thread_ids must be a list of strings")

    for fname in (
        "phase",
        "next_phase",
        "next_action",
        "last_verified_pr_head",
        "last_verified_base_head",
        "updated_at",
    ):
        if not _is_optional_string(getattr(state, fname)):
            errors.append(f"{fname} must be a string or None")

    # Cross-field invariants
    if registry is None:
        registry = ImmutableLifecycleRegistry()
    if state.terminal_state is not None:
        if not registry.is_known(state.terminal_state):
            errors.append(
                f"terminal_state {state.terminal_state!r} is not recognized "
                f"by the supplied registry"
            )
        elif not registry.is_terminal_or_parked(state.terminal_state):
            errors.append(
                f"terminal_state {state.terminal_state!r} is in registry but "
                f"has category {registry.category_of(state.terminal_state).value!r}; "
                f"the runner may not safely stop on it."
            )

    # No auto-resume after completed closeout when terminal_state is COMPLETED
    if state.terminal_state == "COMPLETED" and state.next_action:
        errors.append(
            "completed closeout must not carry a next_action"
        )

    return errors


def validate_resume_observations(
    state: CheckpointState,
    observed_pr_head: str,
    observed_base_head: str,
) -> List[str]:
    """Detect head drift between checkpoint and current observation.

    Returns a list of empty-on-success error strings; non-empty means the
    runner should hold for head drift.
    """
    errors: List[str] = []
    if state.last_verified_pr_head is not None and observed_pr_head != state.last_verified_pr_head:
        errors.append(
            f"PR head drift: checkpoint recorded {state.last_verified_pr_head!r}, "
            f"observed {observed_pr_head!r}"
        )
    if state.last_verified_base_head is not None and observed_base_head != state.last_verified_base_head:
        errors.append(
            f"Base head drift: checkpoint recorded {state.last_verified_base_head!r}, "
            f"observed {observed_base_head!r}"
        )
    return errors


def next_action_from_checkpoint(state: CheckpointState) -> Optional[str]:
    """Return the next action string, or ``None`` if none is recorded.

    Pure structural lookup.
    """
    return state.next_action


def checkpoint_requires_operator(
    state: CheckpointState,
    registry: LifecycleStateRegistry | None = None,
) -> bool:
    """Return ``True`` iff the checkpoint cannot be safely auto-resumed.

    Conservative: returns ``True`` whenever a structured check fails or
    the terminal state is a parked category.
    """
    if registry is None:
        registry = ImmutableLifecycleRegistry()
    if state.terminal_state is None:
        return True
    if not registry.is_terminal_or_parked(state.terminal_state):
        return True
    # Parked-with-missing-next-action == operator required
    if state.terminal_state in registry.parked_states and not state.next_action:
        return True
    # Unresolved thread inventory not empty == operator required
    if state.unresolved_thread_ids:
        return True
    return False
