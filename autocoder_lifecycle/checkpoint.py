"""Checkpoint state dataclass and pure resume helpers.

A checkpoint is a serializable snapshot of the run state for a single
autonomous-development controller run. The shape is:

- ``repo``
- ``pr_number``
- ``branch``
- ``current_head``
- ``base_head``            (target / canonical base head; "" when absent)
- ``phase``
- ``completed_phases``
- ``next_phase``
- ``next_action``
- ``pending_actions``
- ``last_verified_pr_head``   (None or sha256-hex)
- ``last_verified_base_head`` (None or sha256-hex)
- ``authorized_thread_ids``
- ``unresolved_thread_ids``
- ``terminal_state``          (None or string)
- ``updated_at``

Type contract:

- All string fields must be string-typed. The only absent sentinel
  for head-like fields is ``""`` (empty string) for ``base_head``;
  ``None`` is rejected for head-like fields. ``last_verified_*_head``
  and ``terminal_state`` use ``None`` as the absent sentinel.
- Numeric fields must be non-negative ints.
- Lists must be lists of strings.

Lifecycle-state interpretation is supplied through the
:class:`LifecycleStateRegistry` interface.

Public API
----------

- :func:`validate_checkpoint` - STRUCTURAL only. Verifies that the fields
  are present, well-formed, and consistent with the documented schema.
- :func:`validate_resume_observations` - head-drift detector.
- :func:`next_action_from_checkpoint` - returns the next string the runner
  should act on, or ``None`` if the checkpoint says to stop.
- :func:`checkpoint_requires_operator` - returns ``True`` iff the
  checkpoint cannot be safely auto-resumed and a human must intervene.
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
    """True iff v is None or a string. Used for phase/next_phase/next_action/updated_at."""
    return v is None or isinstance(v, str)


def _is_optional_sha_hex(v: object) -> bool:
    """True iff v is None or a valid 64-char lowercase-hex sha256 string."""
    return v is None or (isinstance(v, str) and _is_sha256_hex(v))


def _validate_base_head(v: object) -> Optional[str]:
    """Validate base_head field.

    Returns the normalised value (the empty-string sentinel for absent)
    or raises ValueError on invalid input.

    Contract:
    - base_head must be a string;
    - "" is the sole absent-value sentinel;
    - non-empty values must be 64-char lowercase-hex sha256.
    """
    if not isinstance(v, str):
        raise ValueError(
            f"base_head must be a string (got {type(v).__name__})"
        )
    if v == "":
        return ""
    if not _is_sha256_hex(v):
        raise ValueError(
            f"base_head must be a 64-char lowercase hex sha256 (got {v!r})"
        )
    return v


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

    def __post_init__(self) -> None:
        # Apply uniform type validation in BOTH the direct constructor
        # and from_dict paths.
        for fname in ("repo", "branch", "current_head"):
            v = getattr(self, fname)
            if not isinstance(v, str) or not v:
                raise ValueError(
                    f"{fname!r} must be a non-empty string (got {v!r})"
                )
        if not _is_sha256_hex(self.current_head):
            raise ValueError(
                f"current_head must be 64 lowercase hex characters (got {self.current_head!r})"
            )
        # base_head: strict string-or-empty; non-empty must be valid sha
        if not isinstance(self.base_head, str):
            raise ValueError(
                f"base_head must be a string (got {type(self.base_head).__name__})"
            )
        if self.base_head != "" and not _is_sha256_hex(self.base_head):
            raise ValueError(
                f"base_head must be a 64-char lowercase hex sha256 (got {self.base_head!r})"
            )
        # last_verified_*_head: None or valid sha
        for fname in ("last_verified_pr_head", "last_verified_base_head"):
            v = getattr(self, fname)
            if not _is_optional_sha_hex(v):
                raise ValueError(
                    f"{fname!r} must be None or 64-char lowercase hex sha256 "
                    f"(got {v!r})"
                )
        # terminal_state: None or string
        if not _is_optional_string(self.terminal_state):
            raise ValueError(
                f"terminal_state must be None or a string "
                f"(got {type(self.terminal_state).__name__})"
            )
        # phase/next_phase/next_action/updated_at: None or string
        for fname in ("phase", "next_phase", "next_action", "updated_at"):
            if not _is_optional_string(getattr(self, fname)):
                raise ValueError(
                    f"{fname!r} must be None or a string"
                )
        # pr_number: non-negative int
        if not _is_non_negative_int(self.pr_number):
            raise ValueError(
                f"pr_number must be a non-negative int (got {self.pr_number!r})"
            )
        # List-of-strings
        for fname in (
            "completed_phases",
            "pending_actions",
            "authorized_thread_ids",
            "unresolved_thread_ids",
        ):
            v = getattr(self, fname)
            if not _is_string_list(v):
                raise ValueError(
                    f"{fname!r} must be a list of strings"
                )

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
            if value is None:
                return None
            if value == "":
                errors.append(
                    "%r must be None or a valid sha256 hex "
                    "(got empty string)" % name
                )
                return None
            v = optional_str(name, value)
            if v is None:
                return None
            if not _is_sha256_hex(v):
                errors.append(
                    "%r must be 64-char lowercase hex sha256 (got %r)"
                    % (name, v)
                )
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

        # base_head: strict string; empty allowed; non-empty must be valid sha
        raw_base_head = payload.get("base_head", "")
        if raw_base_head is None:
            errors.append("base_head must be a string (got None)")
            base_head = ""
        elif not isinstance(raw_base_head, str):
            errors.append(
                "base_head must be a string (got %s)"
                % type(raw_base_head).__name__
            )
            base_head = ""
        elif raw_base_head == "":
            base_head = ""
        elif not _is_sha256_hex(raw_base_head):
            errors.append(
                "base_head must be 64-char lowercase hex sha256 (got %r)"
                % (raw_base_head,)
            )
            base_head = ""
        else:
            base_head = raw_base_head

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
            base_head=base_head,
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

    The function operates on a CheckpointState instance whose fields
    have already passed type validation at construction time
    (CheckpointState.__post_init__). The checks here cover:

    - registry-based terminal_state recognition;
    - terminal_state terminal-or-parked classification;
    - completed closeout consistency (no next_action or pending
      next_phase for registry-marked completed states).
    """
    if registry is None:
        registry = ImmutableLifecycleRegistry()

    # If the constructor's __post_init__ was bypassed (e.g. by mutating
    # an already-validated instance), re-check type invariants here so
    # we never invoke registry methods with non-string values.
    errors: List[str] = []
    if not isinstance(state.current_head, str) or not _is_sha256_hex(state.current_head):
        errors.append("current_head must be 64 lowercase hex characters")
    if not isinstance(state.base_head, str):
        errors.append("base_head must be a string")
    elif state.base_head != "" and not _is_sha256_hex(state.base_head):
        errors.append("base_head must be 64 lowercase hex chars")
    for fname in ("last_verified_pr_head", "last_verified_base_head"):
        v = getattr(state, fname)
        if v is not None and (not isinstance(v, str) or not _is_sha256_hex(v)):
            errors.append("%s must be None or 64-char lowercase hex sha256" % fname)
    if state.terminal_state is not None and not isinstance(state.terminal_state, str):
        errors.append("terminal_state must be None or a string")

    if state.terminal_state is not None and isinstance(state.terminal_state, str):
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
    """Detect head drift between checkpoint and current observation."""
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
    """Return the next action string, or None if none is recorded."""
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
