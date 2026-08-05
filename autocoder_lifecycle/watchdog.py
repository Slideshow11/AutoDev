"""Deterministic watchdog evaluator.

Pure dataclass watchdog with injected timestamps and a generic
pending-action classifier. No clock reads, no GitHub calls, no filesystem
I/O.

Public API
----------

- :class:`WatchdogState` - pure dataclass state
- :func:`evaluate_watchdog` - deterministic verdict
- :func:`should_continue_polling` - bounded polling helper
- :data:`STALL_RISK` - verdict returned when the watchdog detects a stall
  risk
- :data:`WATCHDOG_PROGRESS_REQUIRED` - verdict returned when the
  watchdog is idle beyond the budget and last_progress_at is not recent
  enough
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .registry import LifecycleStateRegistry, ImmutableLifecycleRegistry


STALL_RISK = "STALL_RISK"
"""Returned when the watchdog detects a stall risk."""

WATCHDOG_PROGRESS_REQUIRED = "WATCHDOG_PROGRESS_REQUIRED"
"""Returned when the watchdog has flagged that progress is required."""

WATCHDOG_OK = "WATCHDOG_OK"
"""Returned when the watchdog considers the state healthy."""


# A caller-supplied exhaustion classifier takes a (state, now) pair and
# returns a verdict string or None. The default implementation returns
# None and lets the watchdog compute from budgets only.
ExhaustionClassifier = Callable[["WatchdogState", float], Optional[str]]


def _default_exhaustion_classifier(state: "WatchdogState", now: float) -> Optional[str]:
    return None


def _has_pending_action(state: "WatchdogState") -> bool:
    """True iff state carries both a next_action and a checkpoint_path.

    A pending-action stall condition is the first-priority watchdog trap:
    the runner has everything it needs to advance but has not done so.
    The watchdog returns STALL_RISK regardless of how generous the time
    budgets are.
    """
    return bool(state.next_action) and bool(state.checkpoint_path)


@dataclass(frozen=True)
class WatchdogState:
    """Pure watchdog state. All time values are floats (epoch seconds).

    next_action and checkpoint_path are optional. terminal_state is
    supplied as a string; the watchdog consults the supplied
    :class:`LifecycleStateRegistry` for terminal/parked-state semantics
    so it is portable across registries.
    """

    phase_name: str
    started_at: float
    last_progress_at: float
    max_idle_seconds: float
    max_phase_seconds: float
    next_action: Optional[str] = None
    checkpoint_path: Optional[str] = None
    terminal_state: Optional[str] = None

    def elapsed(self, now: float) -> float:
        return max(0.0, now - self.started_at)

    def idle(self, now: float) -> float:
        return max(0.0, now - self.last_progress_at)


def evaluate_watchdog(
    state: WatchdogState,
    *,
    now: float,
    registry: LifecycleStateRegistry | None = None,
    exhaustion_classifier: ExhaustionClassifier | None = None,
) -> str:
    """Return the deterministic watchdog verdict.

    Verdict (priority order):
    - STALL_RISK if the runner has both a next_action AND a
      checkpoint_path: caller failed to act on already-known next
      action.
    - STALL_RISK if the runner has a recognized terminal/parked state
      but is still attempting to work; documented as a hard stop.
    - STALL_RISK if the phase timer exceeded max_phase_seconds without
      terminal state.
    - STALL_RISK if caller_exhaustion_classifier returns a stall verdict.
    - WATCHDOG_PROGRESS_REQUIRED if idle > max_idle_seconds and no
      terminal state.
    - WATCHDOG_OK otherwise.

    No clock reads: the caller supplies now. No filesystem or network
    I/O.
    """
    if registry is None:
        registry = ImmutableLifecycleRegistry()

    has_terminal = (
        state.terminal_state is not None
        and registry.is_terminal_or_parked(state.terminal_state)
    )

    # First-priority pending-action stall: the runner already has both
    # a next_action and a checkpoint_path and has not advanced. Stop.
    if has_terminal:
        # Even when terminal, we surface a terminal-state verdict
        # separately so callers can know to stop, and we do NOT consult
        # the caller-supplied exhaustion classifier (which would let
        # custom verdicts like "RETRY" keep polling).
        return STALL_RISK

    # Pending-action stall: next_action + checkpoint_path set, no
    # terminal state, no action yet.
    if _has_pending_action(state):
        return STALL_RISK

    # Phase timeout.
    if state.elapsed(now) > state.max_phase_seconds:
        return STALL_RISK

    # Idle timeout without terminal state.
    if state.idle(now) > state.max_idle_seconds:
        return WATCHDOG_PROGRESS_REQUIRED

    # Caller-supplied exhaustion classifier (only consulted for
    # non-terminal, non-pending-action cases).
    verdict = (exhaustion_classifier or _default_exhaustion_classifier)(state, now)
    if verdict is not None:
        return verdict

    return WATCHDOG_OK


def should_continue_polling(
    state: WatchdogState,
    *,
    now: float,
    deadline: float,
    registry: LifecycleStateRegistry | None = None,
    exhaustion_classifier: ExhaustionClassifier | None = None,
) -> bool:
    """Return True if the runner should keep polling.

    Returns False if:
    - the deadline has passed;
    - the watchdog returns STALL_RISK;
    - the watchdog returns WATCHDOG_OK AND a terminal-or-parked
      state has been reached.
    """
    if now >= deadline:
        return False
    verdict = evaluate_watchdog(
        state,
        now=now,
        registry=registry,
        exhaustion_classifier=exhaustion_classifier,
    )
    if verdict == STALL_RISK:
        return False
    if registry is None:
        registry = ImmutableLifecycleRegistry()
    if (
        verdict == WATCHDOG_OK
        and state.terminal_state is not None
        and registry.is_terminal_or_parked(state.terminal_state)
    ):
        return False
    return True
