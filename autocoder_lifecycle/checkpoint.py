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

- :func:`validate_checkpoint` - STRUCTURAL only. Verifies that the fields
  are present, well-formed, and consistent with the documented schema.
  It does NOT compare the PR head against the base head (they are
  intentionally different in a normal feature-branch PR). Head-drift
  detection lives in :func:`validate_resume_observations`.
- :func:`validate_resume_observations` - head-drift detector. Compares
  the SHAs the runner just fetched against the recorded
  ``last_verified_pr_head`` and ``last_verified_base_head``.
- :func:`next_action_from_checkpoint` - returns the next string the runner
  should act on, or ``None`` if the checkpoint says to stop.
  Structural / lookups only.
- :func:`checkpoint_requires_operator` - returns ``True`` iff the
  checkpoint cannot be safely auto-resumed and a human must intervene.
  Structural / lookups only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .registry import LifecycleStateRegistry, ImmutableLifecycleRegistry


_REQUIRED_STRING_FIELDS: Tuple[str, ...] = (
    "repo",
    "branch",
    "current_head",
)


def _is_sha256_hex(s: object) -> bool:
    """Return True iff s is a string of 64 lowercase hex characters."""
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
    def from_dict(cls, payload) -> "CheckpointState":
        """Reconstruct from a JSON-decoded dict and STRUCTURALLY validate it.

        Field coercion is intentionally strict: anything other than the
        documented type is rejected. This protects downstream validators
        from acting on values that survived an unsafe conversion.
        """
        if not isinstance(payload, dict):
            raise ValueError(
                "checkpoint payload must be a dict (got %s)"
                % type(payload).__name__
            )

        errors: List[str] = []

        def require_str(name: str, value):
            if not isinstance(value, str):
                errors.append(
                    "%r must be a string (got %s)"
                    % (name, type(value).__name__)
                )
                return ""
            return value

        def optional_str(name: str, value):
            if value is None:
                return None
            if not isinstance(value, str):
                errors.append(
                    "%r must be a string or None (got %s)"
                    % (name, type(value).__name__)
                )
                return None
            return value

        def require_sha_hex(name: str, value):
            v = optional_str(name, value)
            if v is None:
                errors.append("%r must be a 64-char lowercase hex sha256" % name)
                return ""
            if not _is_sha256_hex(v):
                errors.append(
                    "%r must be a 64-char lowercase hex sha256 (got %r)"
                    % (name, v)
                )
            return v

        def optional_sha_hex(name: str, value):
            if value is None or value == "":
                # Empty string is treated as absent (consistent with
                # ``base_head=""`` default sentinel in CheckpointState).
                return None
            v = optional_str(name, value)
            if v is None:
                return None
            if not _is_sha256_hex(v):
                errors.append("%r must be a 64-char lowercase hex sha256" % name)
                return None
            return v

        def require_int(name: str, value):
            if not _is_non_negative_int(value):
                errors.append("%r must be a non-negative int" % name)
                return 0
            return int(value)

        def require_str_list(name: str, value):
            if not _is_string_list(value):
                errors.append("%r must be a list of strings" % name)
                return []
            return list(value)

        repo = require_str("repo", payload.get("repo"))
        branch = require_str("branch", payload.get("branch"))
        current_head = require_sha_hex("current_head", payload.get("current_head"))
        base_head = optional_sha_hex("base_head", payload.get("base_head", ""))
        pr_number = require_int("pr_number", payload.get("pr_number"))
        phase = optional_str("phase", payload.get("phase"))
        completed_phases = require_str_list(
            "completed_phases", payload.get("completed_phases", [])
        )
        next_phase = optional_str("next_phase", payload.get("next_phase"))
        next_action = optional_str("next_action", payload.get("next_action"))
        pending_actions = require_str_list(
            "pending_actions", payload.get("pending_actions", [])
        )
        last_verified_pr_head = optional_sha_hex(
            "last_verified_pr_head", payload.get("last_verified_pr_head")
        )
        last_verified_base_head = optional_sha_hex(
            "last_verified_base_head", payload.get("last_verified_base_head")
        )
        authorized_thread_ids = require_str_list(
            "authorized_thread_ids", payload.get("authorized_thread_ids", [])
        )
        unresolved_thread_ids = require_str_list(
            "unresolved_thread_ids", payload.get("unresolved_thread_ids", [])
        )
        terminal_state = optional_str(
            "terminal_state", payload.get("terminal_state")
        )
        updated_at = optional_str("updated_at", payload.get("updated_at"))

        if errors:
            raise ValueError(
                "checkpoint payload invalid (%d error(s)): %s"
                % (len(errors), "; ".join(errors))
            )

        return cls(
            repo=repo,
            pr_number=pr_number,
            branch=branch,
            current_head=current_head,
            base_head=base_head or "",
            phase=phase,
            completed_phases=completed_phases,
            next_phase=next_phase,
            next_action=next_action,
            pending_actions=pending_actions,
            last_verified_pr_head=last_verified_pr_head,
            last_verified_base_head=last_verified_base_head,
            authorized_thread_ids=authorized_thread_ids,
            unresolved_thread_ids=unresolved_thread_ids,
            terminal_state=terminal_state,
            updated_at=updated_at,
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
    - invalid pr_number;
    - unknown terminal_state (against the supplied registry);
    - inconsistent completed_phases / pending_actions lists;
    - a recorded terminal_state that is not terminal-or-parked against the
      registry;
    - a recorded completed closeout mismatch (terminal_state registered
      as completed but next_action or next_phase still pending).
    """
    errors: List[str] = []

    for field_name in _REQUIRED_STRING_FIELDS:
        val = getattr(state, field_name)
        if not isinstance(val, str) or not val:
            errors.append(
                "required string field %r missing or empty" % field_name
            )

    if not _is_non_negative_int(state.pr_number):
        errors.append(
            "pr_number must be a non-negative int (got %r)" % (state.pr_number,)
        )

    if state.base_head:
        if not _is_sha256_hex(state.base_head):
            errors.append("base_head must be 64 lowercase hex chars")

    if not _is_sha256_hex(state.current_head):
        errors.append(
            "current_head must be 64 lowercase hex characters (got %r)"
            % (state.current_head,)
        )

    if not _is_string_list(state.completed_phases):
        errors.append("completed_phases must be a list of strings")

    if not _is_string_list(state.pending_actions):
        errors.append("pending_actions must be a list of strings")

    if not _is_string_list(state.authorized_thread_ids):
        errors.append("authorized_thread_ids must be a list of strings")

    if not _is_string_list(state.unresolved_thread_ids):
        errors.append("unresolved_thread_ids must be a list of strings")

    for fname in (
        "phase",
        "next_phase",
        "next_action",
        "last_verified_pr_head",
        "last_verified_base_head",
        "updated_at",
    ):
        if not _is_optional_string(getattr(state, fname)):
            errors.append("%s must be a string or None" % fname)

    if registry is None:
        registry = ImmutableLifecycleRegistry()

    if state.terminal_state is not None:
        if not registry.is_known(state.terminal_state):
            errors.append(
                "terminal_state %r is not recognized by the supplied registry"
                % (state.terminal_state,)
            )
        elif not registry.is_terminal_or_parked(state.terminal_state):
            errors.append(
                "terminal_state %r is in registry but has category %r; "
                "the runner may not safely stop on it."
                % (
                    state.terminal_state,
                    registry.category_of(state.terminal_state).value,
                )
            )
        else:
            # Use the registry's completed-state semantics, not a literal
            # literal string check. Derived registries can mark other
            # terminals as completed states.
            if registry.is_completed(state.terminal_state):
                if state.next_action:
                    errors.append(
                        "completed closeout must not carry a next_action"
                    )
                if state.next_phase and state.next_phase not in state.completed_phases:
                    errors.append(
                        "completed closeout must not carry a pending next_phase %r"
                        % (state.next_phase,)
                    )

    return errors


def validate_resume_observations(
    state: CheckpointState,
    observed_pr_head: str,
    observed_base_head: str,
) -> List[str]:
    """Detect head drift between checkpoint and current observation.

    Returns a list of empty-on-success error strings; non-empty means
    the runner should hold for head drift.
    """
    errors: List[str] = []
    if state.last_verified_pr_head is not None:
        if not _is_sha256_hex(observed_pr_head):
            errors.append(
                "observed PR head must be 64-char lowercase hex sha256 (got %r)"
                % (observed_pr_head,)
            )
        elif observed_pr_head != state.last_verified_pr_head:
            errors.append(
                "PR head drift: checkpoint recorded %r, observed %r"
                % (state.last_verified_pr_head, observed_pr_head)
            )
    if state.last_verified_base_head is not None:
        if not _is_sha256_hex(observed_base_head):
            errors.append(
                "observed base head must be 64-char lowercase hex sha256 (got %r)"
                % (observed_base_head,)
            )
        elif observed_base_head != state.last_verified_base_head:
            errors.append(
                "Base head drift: checkpoint recorded %r, observed %r"
                % (state.last_verified_base_head, observed_base_head)
            )
    return errors


def next_action_from_checkpoint(state: CheckpointState) -> Optional[str]:
    """Return the next action string, or None if none is recorded.

    Pure structural lookup.
    """
    return state.next_action


def checkpoint_requires_operator(
    state: CheckpointState,
    registry: LifecycleStateRegistry | None = None,
) -> bool:
    """Return True iff the checkpoint cannot be safely auto-resumed.

    Conservative: returns True whenever the structural validation
    fails OR the terminal state is a parked category OR unresolved
    threads exist. A checkpoint that cannot even pass structural
    validation must NOT be auto-resumed under any policy because the
    runner cannot reason about its lifecycle identity.
    """
    if registry is None:
        registry = ImmutableLifecycleRegistry()
    structural = validate_checkpoint(state, registry)
    if structural:
        return True
    if state.terminal_state is None:
        return True
    if not registry.is_terminal_or_parked(state.terminal_state):
        return True
    if state.terminal_state in registry.parked_states and not state.next_action:
        return True
    if state.unresolved_thread_ids:
        return True
    return False
