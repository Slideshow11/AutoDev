"""Lifecycle-state registry contract.

A :class:`LifecycleStateRegistry` is a pluggable, in-memory catalog of
lifecycle states and their categories. This module is intentionally pure:
no filesystem reads, no GitHub calls, no project-specific identifiers.

Default AutoDev vocabulary
--------------------------

The default :class:`ImmutableLifecycleRegistry` provides only platform-
generic terminal, hold, and informational states:

- ``COMPLETED``         — generic successful close (terminal)
- ``FAILED``            — generic hard failure (terminal)
- ``AWAITING_HUMAN_AUTHORIZATION`` — generic human-authorization request (terminal / parked)
- ``OPERATOR_REQUIRED`` — generic operator-intervention request (terminal / parked)
- ``HEAD_CHANGED``      — generic head drift (terminal / parked)
- ``CI_PENDING``        — generic external dependency pending (terminal / parked)
- ``CI_FAILED``         — generic external dependency failure (terminal / parked)
- ``RUNNING``           — generic in-progress (informational)
- ``NOT_RUN``           — generic initial state (informational)

Provider- and project-specific states (e.g. ``HOLD_CODEX_RESPONSE_PENDING``
or ``MERGE_READY_AWAITING_HUMAN_AUTHORIZATION``) must live in adapters that
consume the registry interface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, Iterable, Mapping


class LifecycleCategory(str, Enum):
    """Stable identifiers for lifecycle-state categories.

    A registry implementation MAY use any subset of these categories.
    Default AutoDev vocabulary uses all of them.
    """

    TERMINAL = "terminal"
    PARKED = "parked"
    HOLD = "hold"
    INFORMATIONAL = "informational"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LifecycleStateRegistry:
    """Immutable, pluggable lifecycle-state registry.

    The protocol intentionally stores only state strings and category
    metadata. Provider- and project-specific vocabulary must be encoded in
    subclasses or derived registries, NOT in this core class.
    """

    terminal_states: FrozenSet[str]
    parked_states: FrozenSet[str]
    hold_states: FrozenSet[str]
    informational_states: FrozenSet[str]
    metadata: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def all_known_states(self) -> FrozenSet[str]:
        return (
            self.terminal_states
            | self.parked_states
            | self.hold_states
            | self.informational_states
        )

    def category_of(self, state: str) -> LifecycleCategory:
        if state in self.terminal_states:
            return LifecycleCategory.TERMINAL
        if state in self.parked_states:
            return LifecycleCategory.PARKED
        if state in self.hold_states:
            return LifecycleCategory.HOLD
        if state in self.informational_states:
            return LifecycleCategory.INFORMATIONAL
        return LifecycleCategory.UNKNOWN

    def is_known(self, state: str) -> bool:
        return self.category_of(state) != LifecycleCategory.UNKNOWN

    def is_terminal_or_parked(self, state: str) -> bool:
        cat = self.category_of(state)
        return cat in (LifecycleCategory.TERMINAL, LifecycleCategory.PARKED)

    def is_hold(self, state: str) -> bool:
        return self.category_of(state) == LifecycleCategory.HOLD

    def is_completed(self, state: str) -> bool:
        """Return True iff ``state`` represents a completed closeout.

        ``completed`` is mapped to ``category=terminal`` AND the metadata
        flag ``completed=true``. By default, ``COMPLETED`` and ``FAILED``
        are both terminals but only ``COMPLETED`` carries the
        ``completed`` flag. Adapters can override this by constructing a
        derived registry.
        """
        if not self.is_known(state):
            return False
        if self.category_of(state) != LifecycleCategory.TERMINAL:
            return False
        return self.metadata_for(state).get("completed", "").lower() == "true"

    def metadata_for(self, state: str) -> Mapping[str, str]:
        return self.metadata.get(state, {})


# Keep the protocol name as the concrete dataclass for simplicity.
# Adapters and callers program against the dataclass itself.
LifecycleStateRegistryProtocol = LifecycleStateRegistry


def _default_metadata() -> Dict[str, Dict[str, str]]:
    return {
        "COMPLETED": {"completed": "true"},
        "FAILED": {"completed": "false"},
        "AWAITING_HUMAN_AUTHORIZATION": {"completed": "false"},
        "OPERATOR_REQUIRED": {"completed": "false"},
        "HEAD_CHANGED": {"completed": "false"},
        "CI_PENDING": {"completed": "false"},
        "CI_FAILED": {"completed": "false"},
        "RUNNING": {"completed": "false"},
        "NOT_RUN": {"completed": "false"},
    }


_DEFAULT_REGISTRY = LifecycleStateRegistry(
    terminal_states=frozenset({"COMPLETED", "FAILED"}),
    parked_states=frozenset({"AWAITING_HUMAN_AUTHORIZATION", "OPERATOR_REQUIRED"}),
    hold_states=frozenset({"HEAD_CHANGED", "CI_PENDING", "CI_FAILED"}),
    informational_states=frozenset({"RUNNING", "NOT_RUN"}),
    metadata=_default_metadata(),
)


def ImmutableLifecycleRegistry() -> LifecycleStateRegistry:
    """Return the default AutoDev generic lifecycle registry.

    The returned instance is module-level and frozen; callers MUST NOT
    mutate it.
    """
    return _DEFAULT_REGISTRY


class RegistryBuilder:
    """Builder for a derived, project-specific registry.

    Builders are constructed from a base registry. Providers and projects
    extend the vocabulary by adding states to the desired categories.
    """

    def __init__(self, base: LifecycleStateRegistry | None = None) -> None:
        base = base if base is not None else _DEFAULT_REGISTRY
        self._terminal: set[str] = set(base.terminal_states)
        self._parked: set[str] = set(base.parked_states)
        self._hold: set[str] = set(base.hold_states)
        self._informational: set[str] = set(base.informational_states)
        self._metadata: dict[str, Dict[str, str]] = {
            state: dict(meta) for state, meta in base.metadata.items()
        }

    def with_terminal(self, states: Iterable[str]) -> "RegistryBuilder":
        for s in states:
            self._terminal.add(s)
            self._parked.discard(s)
            self._hold.discard(s)
            self._informational.discard(s)
        return self

    def with_parked(self, states: Iterable[str]) -> "RegistryBuilder":
        for s in states:
            self._parked.add(s)
            self._terminal.discard(s)
            self._hold.discard(s)
            self._informational.discard(s)
        return self

    def with_hold(self, states: Iterable[str]) -> "RegistryBuilder":
        for s in states:
            self._hold.add(s)
            self._terminal.discard(s)
            self._parked.discard(s)
            self._informational.discard(s)
        return self

    def with_informational(self, states: Iterable[str]) -> "RegistryBuilder":
        for s in states:
            self._informational.add(s)
            self._terminal.discard(s)
            self._parked.discard(s)
            self._hold.discard(s)
        return self

    def with_metadata(self, state: str, **kv: str) -> "RegistryBuilder":
        self._metadata.setdefault(state, {}).update(kv)
        return self

    def build(self) -> LifecycleStateRegistry:
        return LifecycleStateRegistry(
            terminal_states=frozenset(self._terminal),
            parked_states=frozenset(self._parked),
            hold_states=frozenset(self._hold),
            informational_states=frozenset(self._informational),
            metadata=dict(self._metadata),
        )
