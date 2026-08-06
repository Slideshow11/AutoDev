"""Tests for autocoder_orchestration.store.StateStore."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from autocoder_orchestration.store import (
    StateStore,
    StateStoreError,
    StateCorruption,
    StateRevision,
    Lease,
    ProcessIdentity,
    current_process_identity,
    open_lease,
)


@pytest.fixture
def tmp_state_root(tmp_path):
    """Return a fresh state root directory."""
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    return str(root)


# === Construction ===
class TestStateStoreConstruction:
    def test_absolute_path_required(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            StateStore("relative/path")

    def test_creates_directory_with_0700(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        mode = Path(tmp_state_root).stat().st_mode & 0o777
        assert mode == 0o700


# === Atomic write ===
class TestAtomicWrite:
    def test_write_creates_file_with_0600(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        store.write_atomic("test.json", {"key": "value"})
        mode = Path(tmp_state_root, "test.json").stat().st_mode & 0o777
        assert mode == 0o600

    def test_write_revisions_increment(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        r1 = store.write_atomic("test.json", {"a": 1})
        r2 = store.write_atomic("test.json", {"a": 2})
        assert r1.revision == 1
        assert r2.revision == 2

    def test_write_rejects_dict_path(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        with pytest.raises(StateStoreError):
            store.write_atomic("test.json", "not a dict")

    def test_write_rejects_unsafe_path(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        with pytest.raises(ValueError):
            store.write_atomic("../escape.json", {})

    def test_write_rejects_absolute_path(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        with pytest.raises(ValueError):
            store.write_atomic("/abs/path.json", {})


# === Read with strict mode ===
class TestReadStrict:
    def test_read_returns_dict(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        store.write_atomic("test.json", {"key": "value"})
        data = store.read_strict("test.json")
        assert data["key"] == "value"

    def test_read_missing_file_raises(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        with pytest.raises(StateStoreError):
            store.read_strict("missing.json")

    def test_read_invalid_json_raises(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        # Write corrupt JSON
        path = Path(tmp_state_root) / "bad.json"
        path.write_bytes(b"not json {")
        with pytest.raises(StateCorruption):
            store.read_strict("bad.json")

    def test_read_non_dict_raises(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        path = Path(tmp_state_root) / "bad.json"
        path.write_bytes(b"[]")
        with pytest.raises(StateCorruption):
            store.read_strict("bad.json")

    def test_read_optional_returns_none(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        assert store.read_optional("missing.json") is None


# === Compare-and-swap ===
class TestCompareAndSwap:
    def test_cas_succeeds_on_match(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        store.write_atomic("test.json", {"k": 1})
        r = store.compare_and_swap("test.json", {"k": 2}, expected_revision=1)
        assert r.revision == 2

    def test_cas_fails_on_mismatch(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        store.write_atomic("test.json", {"k": 1})
        with pytest.raises(Exception):
            store.compare_and_swap("test.json", {"k": 2}, expected_revision=999)


# === Journal ===
class TestJournal:
    def test_append_and_read(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        store.append_journal("journal.jsonl", {"event": "a"})
        store.append_journal("journal.jsonl", {"event": "b"})
        entries = list(store.read_journal("journal.jsonl"))
        assert len(entries) == 2
        assert entries[0]["event"] == "a"
        assert entries[1]["event"] == "b"

    def test_journal_missing_returns_empty(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        assert list(store.read_journal("missing.jsonl")) == []


# === Process identity ===
class TestProcessIdentity:
    def test_current_identity_has_pid(self) -> None:
        ident = current_process_identity()
        assert ident.pid == os.getpid()
        assert isinstance(ident.start_id, str)

    def test_identity_serialization(self) -> None:
        ident = ProcessIdentity(pid=123, start_id="abc")
        d = ident.to_dict()
        assert d == {"pid": 123, "start_id": "abc"}
        restored = ProcessIdentity.from_dict(d)
        assert restored.pid == 123
        assert restored.start_id == "abc"


# === Lease ===
class TestLease:
    def test_acquire_release(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        with open_lease(store) as lease:
            assert lease.is_held()
        assert not lease.is_held()

    def test_different_process_cannot_acquire(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        # First acquire
        lease1 = Lease(store, ProcessIdentity(pid=111, start_id="foo"))
        lease1.acquire()
        try:
            # Different identity should fail
            lease2 = Lease(store, ProcessIdentity(pid=222, start_id="bar"))
            with pytest.raises(StateStoreError):
                lease2.acquire()
        finally:
            lease1.release()

    def test_same_process_re_acquire(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        ident = current_process_identity()
        lease1 = Lease(store, ident)
        lease1.acquire()
        try:
            # Same identity should succeed
            lease2 = Lease(store, ident)
            lease2.acquire()
            lease2.release()
        finally:
            lease1.release()


# === Schema validation ===
class TestSchemaVersion:
    def test_state_payload_includes_schema(self, tmp_state_root: str) -> None:
        store = StateStore(tmp_state_root)
        store.write_atomic("test.json", {"x": 1})
        data = store.read_strict("test.json")
        assert data["_schema_version"] == "autocoder.state_store.v1"
        assert data["_revision"] == 1
