"""Lifecycle-state registry contract.

A :class:`LifecycleStateRegistry` is a pluggable, in-memory catalog of
lifecycle states and their categories. This module is intentionally pure:
no filesystem reads, no GitHub calls, no project-specific identifiers.

Default AutoDev vocabulary
--------------------------

The default :class:`ImmutableLifecycleRegistry` provides only platform-
generic terminal, hold, and informational states:

- ``COMPLETED``         - generic successful close (terminal)
- ``FAILED``            - generic hard failure (terminal)
- ``AWAITING_HUMAN_AUTHORIZATION`` - generic human-authorization request (terminal / parked)
- ``OPERATOR_REQUIRED`` - generic operator-intervention request (terminal / parked)
- ``HEAD_CHANGED``      - generic head drift (terminal / parked)
- ``CI_PENDING``        - generic external dependency pending (terminal / parked)
- ``CI_FAILED``         - generic external dependency failure (terminal / parked)
- ``RUNNING``           - generic in-progress (informational)
- ``NOT_RUN``           - generic initial state (informational)

Provider- and project-specific states (e.g. ``HOLD_CODEX_RESPONSE_PENDING``
or ``MERGE_READY_AWAITING_HUMAN_AUTHORIZATION``) must live in adapters that
consume the registry interface.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import FrozenSet, Mapping


class LifecycleCategory(str, __import__("enum").Enum):
    """Stable identifiers for lifecycle-state categories.

    A registry implementation MAY use any subset of these categories.
    Default AutoDev vocabulary uses all of them.
    """

    TERMINAL = "terminal"
    PARKED = "parked"
    HOLD = "hold"
    INFORMATIONAL = "informational"
    UNKNOWN = "unknown"


def _frozen_metadata():
    """Build an immutable metadata mapping with normalized per-state entries."""
    raw = {
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
    return MappingProxyType(
        {state: MappingProxyType(meta) for state, meta in raw.items()}
    )


class LifecycleStateRegistry:
    """Immutable, pluggable lifecycle-state registry.

    All set-typed fields are ``frozenset`` and the metadata mapping is
    wrapped in ``MappingProxyType`` so callers cannot mutate the registry
    after construction. Providers / projects construct derived registries
    via :class:`RegistryBuilder`, which produces a similarly immutable
    instance.
    """

    __slots__ = (
        "terminal_states",
        "parked_states",
        "hold_states",
        "informational_states",
        "metadata",
    )

    def __init__(
        self,
        terminal_states,
        parked_states,
        hold_states,
        informational_states,
        metadata=None,
    ) -> None:
        object.__setattr__(self, "terminal_states", frozenset(terminal_states))
        object.__setattr__(self, "parked_states", frozenset(parked_states))
        object.__setattr__(self, "hold_states", frozenset(hold_states))
        object.__setattr__(self, "informational_states", frozenset(informational_states))
        object.__setattr__(self, "metadata", _frozen_metadata() if metadata is None else _build_metadata(metadata))

    def __setattr__(self, name, value) -> None:
        raise AttributeError(
            "LifecycleStateRegistry is immutable; build a derived registry via RegistryBuilder"
        )

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
        m = self.metadata.get(state, {})
        return m if isinstance(m, MappingProxyType) else _EMPTY_METADATA


_EMPTY_METADATA = MappingProxyType({})


def _build_metadata(raw: Mapping[str, Mapping[str, str]]) -> Mapping[str, Mapping[str, str]]:
    """Wrap every metadata level in ``MappingProxyType`` so the result is fully immutable."""
    return MappingProxyType(
        {state: MappingProxyType(dict(meta)) for state, meta in raw.items()}
    )


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
        # Start from the base's existing metadata entries.
        self._metadata: dict[str, dict[str, str]] = {
            state: dict(meta)
            for state, meta in base.metadata.items()
        }

    def with_terminal(self, states) -> "RegistryBuilder":
        for s in states:
            self._terminal.add(s)
            self._parked.discard(s)
            self._hold.discard(s)
            self._informational.discard(s)
        return self

    def with_parked(self, states) -> "RegistryBuilder":
        for s in states:
            self._parked.add(s)
            self._terminal.discard(s)
            self._hold.discard(s)
            self._informational.discard(s)
        return self

    def with_hold(self, states) -> "RegistryBuilder":
        for s in states:
            self._hold.add(s)
            self._terminal.discard(s)
            self._parked.discard(s)
            self._informational.discard(s)
        return self

    def with_informational(self, states) -> "RegistryBuilder":
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
        # Construct an immutable registry directly.
        return LifecycleStateRegistry(
            terminal_states=frozenset(self._terminal),
            parked_states=frozenset(self._parked),
            hold_states=frozenset(self._hold),
            informational_states=frozenset(self._informational),
            metadata=MappingProxyType(
                {state: dict(meta) for state, meta in self._metadata.items()}
            ),
        )


_DEFAULT_REGISTRY = LifecycleStateRegistry(
    terminal_states=frozenset({"COMPLETED", "FAILED"}),
    parked_states=frozenset({"AWAITING_HUMAN_AUTHORIZATION", "OPERATOR_REQUIRED"}),
    hold_states=frozenset({"HEAD_CHANGED", "CI_PENDING", "CI_FAILED"}),
    informational_states=frozenset({"RUNNING", "NOT_RUN"}),
)


def ImmutableLifecycleRegistry() -> LifecycleStateRegistry:
    """Return the default AutoDev generic lifecycle registry.

    The returned instance is module-level and immutable; callers MUST
    NOT mutate it. The registry is exposed for tests and default
    vocabulary use; project-specific states are registered via the
    AED-side adapter (PR 1b in the validated roadmap).
    """
    return _DEFAULT_REGISTRY
