"""Generic autonomous-development lifecycle primitives.

This package provides pure, autonomous-development lifecycle primitives with
no project-, provider-, or repository-specific coupling. It is a leaf
package: it does not import supervisor machinery, GitHub clients, or any
external schema. The lifecycle states it knows are platform-generic
terminal and hold categories only — provider- and project-specific
state names belong in adapters that consume this package.

Public API
----------

- :class:`LifecycleStateRegistry` — protocol for pluggable state registries
- :class:`ImmutableLifecycleRegistry` — default immutable registry
- :class:`RegistryBuilder` — builder for derived registries
- :class:`CheckpointState` — serializable checkpoint dataclass
- :func:`validate_checkpoint` — structural checkpoint validation
- :func:`validate_resume_observations` — observed-head drift detection
- :func:`next_action_from_checkpoint` — next runner action selection
- :func:`checkpoint_requires_operator` — operator-intervention decision
- :func:`classify_terminal_message` — terminal-output classifier
- :func:`is_terminal_lifecycle_state` — terminal-state predicate
- :class:`WatchdogState` — watchdog dataclass
- :func:`evaluate_watchdog` — deterministic watchdog evaluation
- :func:`should_continue_polling` — bounded polling helper
- :data:`OK_TERMINAL`, :data:`OK_PROGRESS_WITH_NEXT_ACTION`,
  :data:`STALL_PHASE_HEADER_ONLY`, :data:`STALL_WAITING_FOR_CONTINUE`,
  :data:`STALL_NO_TERMINAL_STATE`, :data:`STALL_NO_CHECKPOINT`
- :data:`STALL_RISK`, :data:`WATCHDOG_PROGRESS_REQUIRED`

Invariants
----------

- No filesystem reads.
- No network calls (no GitHub, no Git, no shelled subprocess).
- No writes outside the caller's inputs.
- No project-specific identifiers in the runtime contracts.
"""
from __future__ import annotations

from .checkpoint import (
    CheckpointState,
    checkpoint_requires_operator,
    next_action_from_checkpoint,
    validate_checkpoint,
    validate_resume_observations,
)
from .no_stall import (
    OK_PROGRESS_WITH_NEXT_ACTION,
    OK_TERMINAL,
    STALL_NO_CHECKPOINT,
    STALL_NO_TERMINAL_STATE,
    STALL_PHASE_HEADER_ONLY,
    STALL_WAITING_FOR_CONTINUE,
    classify_terminal_message,
    is_terminal_lifecycle_state,
)
from .registry import (
    ImmutableLifecycleRegistry,
    LifecycleCategory,
    LifecycleStateRegistry,
    RegistryBuilder,
)
from .watchdog import (
    STALL_RISK,
    WATCHDOG_OK,
    WATCHDOG_PROGRESS_REQUIRED,
    WatchdogState,
    evaluate_watchdog,
    should_continue_polling,
)

__all__ = [
    # registry
    "LifecycleCategory",
    "LifecycleStateRegistry",
    "ImmutableLifecycleRegistry",
    "RegistryBuilder",
    # checkpoint
    "CheckpointState",
    "checkpoint_requires_operator",
    "next_action_from_checkpoint",
    "validate_checkpoint",
    "validate_resume_observations",
    # no_stall
    "OK_PROGRESS_WITH_NEXT_ACTION",
    "OK_TERMINAL",
    "STALL_NO_CHECKPOINT",
    "STALL_NO_TERMINAL_STATE",
    "STALL_PHASE_HEADER_ONLY",
    "STALL_WAITING_FOR_CONTINUE",
    "classify_terminal_message",
    "is_terminal_lifecycle_state",
    # watchdog
    "STALL_RISK",
    "WATCHDOG_OK",
    "WATCHDOG_PROGRESS_REQUIRED",
    "WatchdogState",
    "evaluate_watchdog",
    "should_continue_polling",
]
