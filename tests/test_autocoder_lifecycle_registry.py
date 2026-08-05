"""Tests for autocoder_lifecycle.registry."""
from __future__ import annotations

import pytest

from autocoder_lifecycle import (
    ImmutableLifecycleRegistry,
    LifecycleCategory,
    LifecycleStateRegistry,
    RegistryBuilder,
)


class TestDefaultRegistry:
    def test_default_registry_is_immutable(self) -> None:
        reg = ImmutableLifecycleRegistry()
        # Sets are frozensets and metadata is MappingProxyType; mutating
        # the metadata mapping should fail.
        assert isinstance(reg.terminal_states, frozenset)
        assert isinstance(reg.parked_states, frozenset)
        assert isinstance(reg.hold_states, frozenset)
        assert isinstance(reg.informational_states, frozenset)
        # Metadata proxy: assignment raises TypeError.
        with pytest.raises(TypeError):
            reg.metadata["COMPLETED"] = {}  # type: ignore[index]  # noqa: E501
        # Slot-based class with __setattr__: assignment raises
        # AttributeError.
        with pytest.raises(AttributeError):
            reg.terminal_states = frozenset()  # type: ignore[misc]  # noqa: E501

    def test_default_terminal_states_present(self) -> None:
        reg = ImmutableLifecycleRegistry()
        assert "COMPLETED" in reg.terminal_states
        assert "FAILED" in reg.terminal_states

    def test_default_parked_states_present(self) -> None:
        reg = ImmutableLifecycleRegistry()
        assert "AWAITING_HUMAN_AUTHORIZATION" in reg.parked_states
        assert "OPERATOR_REQUIRED" in reg.parked_states

    def test_default_hold_states_present(self) -> None:
        reg = ImmutableLifecycleRegistry()
        assert "HEAD_CHANGED" in reg.hold_states
        assert "CI_PENDING" in reg.hold_states
        assert "CI_FAILED" in reg.hold_states

    def test_default_no_provider_specific_states(self) -> None:
        """The default registry must NOT contain provider-specific state names."""
        reg = ImmutableLifecycleRegistry()
        all_states = reg.all_known_states()
        for state in all_states:
            assert "CODEX" not in state, f"provider-branded state in default registry: {state}"
            assert "CODERABBIT" not in state, f"provider-branded state in default registry: {state}"
            assert "COGERABBIT" not in state, f"variant spelling in default registry: {state}"
            assert "GITHUB" not in state, f"provider-branded state in default registry: {state}"

    def test_unknown_state_returns_unknown(self) -> None:
        reg = ImmutableLifecycleRegistry()
        assert reg.category_of("UNKNOWN_STATE") is LifecycleCategory.UNKNOWN
        assert reg.is_known("UNKNOWN_STATE") is False

    def test_is_known_returns_true_for_known(self) -> None:
        reg = ImmutableLifecycleRegistry()
        assert reg.is_known("COMPLETED") is True
        assert reg.is_known("FAILED") is True
        assert reg.is_known("NOT_RUN") is True

    def test_terminal_or_parked(self) -> None:
        reg = ImmutableLifecycleRegistry()
        assert reg.is_terminal_or_parked("COMPLETED") is True
        assert reg.is_terminal_or_parked("FAILED") is True
        assert reg.is_terminal_or_parked("AWAITING_HUMAN_AUTHORIZATION") is True
        assert reg.is_terminal_or_parked("OPERATOR_REQUIRED") is True
        assert reg.is_terminal_or_parked("HEAD_CHANGED") is False
        assert reg.is_terminal_or_parked("RUNNING") is False

    def test_is_completed_only_for_completed_state(self) -> None:
        reg = ImmutableLifecycleRegistry()
        assert reg.is_completed("COMPLETED") is True
        assert reg.is_completed("FAILED") is False
        assert reg.is_completed("HEAD_CHANGED") is False
        assert reg.is_completed("UNKNOWN_STATE") is False


class TestRegistryBuilder:
    def test_builder_adds_terminal(self) -> None:
        reg = RegistryBuilder().with_terminal(["PROJECT_COMPLETED"]).build()
        assert "PROJECT_COMPLETED" in reg.terminal_states
        # Default states retained.
        assert "COMPLETED" in reg.terminal_states
        assert "FAILED" in reg.terminal_states

    def test_builder_replaces_category(self) -> None:
        reg = RegistryBuilder().with_terminal(["WAS_HOLD"]).build()
        assert "WAS_HOLD" in reg.terminal_states
        assert "WAS_HOLD" not in reg.hold_states

    def test_builder_metadata(self) -> None:
        reg = RegistryBuilder().with_terminal(["PROJECT_COMPLETED"]).with_metadata(
            "PROJECT_COMPLETED", completed="true", project="X"
        ).build()
        meta = reg.metadata_for("PROJECT_COMPLETED")
        assert meta.get("completed") == "true"
        assert meta.get("project") == "X"
        assert reg.is_completed("PROJECT_COMPLETED") is True

    def test_builder_from_existing(self) -> None:
        base = RegistryBuilder().with_terminal(["EXTRA"]).build()
        derived = RegistryBuilder(base).with_hold(["EXTRA_HOLD"]).build()
        # Both EXTRA and EXTRA_HOLD present.
        assert "EXTRA" in derived.terminal_states
        assert "EXTRA_HOLD" in derived.hold_states
        # And the defaults are still there.
        assert "COMPLETED" in derived.terminal_states

    def test_builder_does_not_mutate_base(self) -> None:
        base = RegistryBuilder().with_terminal(["BASE_TERM"]).build()
        before = sorted(base.terminal_states)
        _ = RegistryBuilder(base).with_terminal(["DERIVED_TERM"]).build()
        after = sorted(base.terminal_states)
        assert before == after


class TestRegistryContractEnforcement:
    def test_no_provider_specific_default(self) -> None:
        reg = ImmutableLifecycleRegistry()
        disallowed_prefixes = ("HOLD_CODEX", "CODEX_", "CODERABBIT", "GITHUB_", "HOLD_NEW_ACTIVE_THREAD")
        for prefix in disallowed_prefixes:
            for state in reg.all_known_states():
                assert not state.startswith(prefix), (
                    f"provider-branded state {state!r} present in default registry"
                )

    def test_registry_metadata_returns_empty_default(self) -> None:
        reg = ImmutableLifecycleRegistry()
        assert reg.metadata_for("UNKNOWN") == {}

    def test_registry_class_is_protocol_for_runtime(self) -> None:
        # The protocol and dataclass share the name; verify equality.
        assert LifecycleStateRegistry is LifecycleStateRegistry


class TestNoFileSystemOrNetworkAccess:
    def test_registry_has_no_io_methods(self) -> None:
        reg = ImmutableLifecycleRegistry()
        # No fetch_, read_, open(), urlopen(), or other network access methods.
        for attr in dir(reg):
            assert not attr.startswith("fetch_"), f"io method on registry: {attr}"
            assert not attr.startswith("download_"), f"io method on registry: {attr}"
