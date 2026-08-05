"""Generic autonomous-run output classifier.

Pure-function classifier for the terminal output of an autonomous
run. Lifecycle-state interpretation is supplied through the
:class:`LifecycleStateRegistry` interface so the same classifier
handles default generic vocabulary and any custom registry.

Public API
----------

- :data:`OK_TERMINAL` - recognized terminal state detected
- :data:`OK_PROGRESS_WITH_NEXT_ACTION` - both next_action and
  value-bearing checkpoint detected
- :data:`STALL_PHASE_HEADER_ONLY` - output is just a phase header
- :data:`STALL_WAITING_FOR_CONTINUE` - output is a prompt-style
  question asking for a human reply
- :data:`STALL_NO_TERMINAL_STATE` - generic progress with no terminal
  state and no next_action
- :data:`STALL_NO_CHECKPOINT` - references a checkpoint but does not
  name one explicitly, has no next_action, no terminal state
- :func:`is_terminal_lifecycle_state` - strict predicate
- :func:`classify_terminal_message` - classify an output string
"""
from __future__ import annotations

import re
from typing import Optional

from .registry import LifecycleStateRegistry, ImmutableLifecycleRegistry


OK_TERMINAL = "OK_TERMINAL"
"""The final output carries a recognized terminal lifecycle state."""

OK_PROGRESS_WITH_NEXT_ACTION = "OK_PROGRESS_WITH_NEXT_ACTION"
"""The final output is mid-phase progress and explicitly carries BOTH
a next_action AND a value-bearing checkpoint path so the runner can
resume from where it left off.

The classifier requires both pieces of evidence. A final output that
has a valid next_action but no value-bearing checkpoint falls
through to STALL_NO_CHECKPOINT; a final output that has a
value-bearing checkpoint but no next_action falls through to
STALL_NO_TERMINAL_STATE.
"""

STALL_PHASE_HEADER_ONLY = "STALL_PHASE_HEADER_ONLY"
"""The final output is just a phase header (e.g. Starting PHASE 1
or Now PHASE 8) with no checkpoint, no terminal state, and no
next_action.
"""

STALL_WAITING_FOR_CONTINUE = "STALL_WAITING_FOR_CONTINUE"
"""The final output is a prompt-style question asking the operator
to type Continue (or yes) before proceeding.
"""

STALL_NO_TERMINAL_STATE = "STALL_NO_TERMINAL_STATE"
"""The final output is a generic progress note with no terminal state
and no next_action. The runner cannot tell whether the work is
done, paused, or stuck.
"""

STALL_NO_CHECKPOINT = "STALL_NO_CHECKPOINT"
"""The final output references a checkpoint but does not name one
explicitly, has no next_action, and no terminal state."""


_PHASE_HEADER_PATTERN = re.compile(
    r"(?im)^\s*(?:starting|now|begin|continuing)\s+phase\b"
)
_WAITING_FOR_CONTINUE_PATTERN = re.compile(
    r"(?im)\b(?:continue|y(?:es)?|y\b|press\s*enter)\b\s*[?.!\s]*$"
)
_TERMINAL_PREFIX_PATTERN = re.compile(r"(?i)^\s*terminal[-\s_]state\s*:")


def is_terminal_lifecycle_state(
    state,
    registry: LifecycleStateRegistry | None = None,
) -> bool:
    """Return True iff state is recognized as terminal-or-parked.

    The function is strict: anything that is not a non-empty string
    recognized by the supplied registry returns False. Abbreviated or
    project-branded strings are rejected; callers must use the
    canonical identifier from their registry.
    """
    if not isinstance(state, str) or not state:
        return False
    if registry is None:
        registry = ImmutableLifecycleRegistry()
    return registry.is_terminal_or_parked(state)


def classify_terminal_message(
    message: str,
    registry: LifecycleStateRegistry | None = None,
) -> str:
    """Classify the terminal output of an autonomous run.

    Returns one of the OK_* / STALL_* constants. Pure function; no
    filesystem or network I/O. Caller supplies a registry if the
    vocabulary is project-specific. If registry is None, the default
    generic registry is used.
    """
    if not isinstance(message, str) or not message.strip():
        return STALL_NO_TERMINAL_STATE

    text = message.strip()

    # Extract optional structured fields.
    next_action = _extract_value(text, "next_action")
    checkpoint_path = _extract_value(text, "checkpoint") or _extract_value(
        text, "ckpt"
    )
    terminal_state = _extract_value(text, "terminal_state")

    if terminal_state is None:
        terminal_state = _extract_field_label(text)

    recognised_terminal: Optional[str] = None
    if terminal_state:
        if registry is None:
            registry = ImmutableLifecycleRegistry()
        if registry.is_terminal_or_parked(terminal_state):
            recognised_terminal = terminal_state

    # Phase-header-only: short message, no actionable content.
    if (
        recognised_terminal is None
        and next_action is None
        and checkpoint_path is None
        and _PHASE_HEADER_PATTERN.search(text)
    ):
        return STALL_PHASE_HEADER_ONLY

    # Waiting-for-continue: prompt-style reply expected.
    if (
        recognised_terminal is None
        and next_action is None
        and _WAITING_FOR_CONTINUE_PATTERN.search(text)
    ):
        return STALL_WAITING_FOR_CONTINUE

    # Terminal (or parked) state present: respect registry completion
    # semantics so a derived registry with completed=False still
    # classifies as STALL_NO_TERMINAL_STATE.
    if recognised_terminal is not None:
        if not registry.is_completed(recognised_terminal):
            return OK_TERMINAL
        # Registry mark this terminal as "completed" -> OK only if both
        # action+checkpoint present.
        if next_action is not None and checkpoint_path:
            return OK_PROGRESS_WITH_NEXT_ACTION
        return OK_TERMINAL

    # No terminal state but action+checkpoint present.
    if next_action is not None and checkpoint_path:
        return OK_PROGRESS_WITH_NEXT_ACTION

    # Has next_action but no checkpoint.
    if next_action is not None and not checkpoint_path:
        return STALL_NO_CHECKPOINT

    # References checkpoint but no next_action or terminal state.
    if checkpoint_path is not None:
        return STALL_NO_TERMINAL_STATE

    # Generic progress with no actionable signal.
    return STALL_NO_TERMINAL_STATE


def _extract_value(text: str, key: str) -> Optional[str]:
    """Return the value of a key: value or key=value field, if any."""
    m = re.search(
        r"(?im)^\s*" + re.escape(key) + r"\s*[:=]\s*(.+?)\s*$", text
    )
    if not m:
        return None
    val = m.group(1).strip()
    if not val or val == "-":
        return None
    return val


def _extract_field_label(text: str) -> Optional[str]:
    """Extract a state identifier from the message header line."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _TERMINAL_PREFIX_PATTERN.match(stripped):
            parts = stripped.split(":", 1)
            if len(parts) == 2 and parts[1].strip():
                return parts[1].strip()
        return stripped
    return None
