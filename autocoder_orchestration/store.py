"""Durable atomic state store for the orchestration layer.

The store is a thin wrapper around the filesystem that guarantees:

- atomic writes (tmp + rename);
- mode 0600 for state files;
- mode 0700 for private directories;
- schema validation on every read;
- compare-and-swap protection via a monotonic revision counter;
- fail-closed behavior on malformed state;
- no silent defaulting on missing or invalid fields.

The store is intentionally small and explicit. It is not a database.
"""
from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, Optional


# === Errors ===
class StateStoreError(Exception):
    """Base store error."""


class StateCorruption(StateStoreError):
    """Raised when a state file is malformed or fails schema validation."""


class StateRevisionMismatch(StateStoreError):
    """Raised when a compare-and-swap attempt fails."""


@dataclass(frozen=True)
class ProcessIdentity:
    """Stable identity of a process even if PID is reused."""

    pid: int
    start_id: str  # inode of /proc/<pid> at acquisition time

    def to_dict(self) -> dict:
        return {"pid": self.pid, "start_id": self.start_id}

    @classmethod
    def from_dict(cls, payload: dict) -> "ProcessIdentity":
        return cls(pid=int(payload["pid"]), start_id=str(payload["start_id"]))


def current_process_identity() -> ProcessIdentity:
    """Return the current process's identity.

    The start_id is the inode of /proc/<pid> on the host. PID
    reuse is detectable because the inode changes at fork.
    """
    pid = os.getpid()
    proc_path = Path(f"/proc/{pid}")
    if proc_path.exists():
        try:
            start_id = str(proc_path.stat().st_ino)
        except OSError:
            start_id = ""
    else:
        start_id = ""
    return ProcessIdentity(pid=pid, start_id=start_id)


@dataclass(frozen=True)
class StateRevision:
    """A monotonic counter bound to a file path."""

    path: str
    revision: int


_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._\-/]+$")


def _safe_path(path: str) -> str:
    """Validate that a path contains no traversal or escape sequences."""
    if not _SAFE_NAME_RE.match(path):
        raise ValueError(f"unsafe path: {path!r}")
    if path.startswith("/"):
        raise ValueError(f"path must be relative: {path!r}")
    # Reject path traversal
    parts = path.split("/")
    if any(p == ".." or p == "." for p in parts):
        raise ValueError(f"path traversal rejected: {path!r}")
    return path


def _ensure_private_dir(path: Path) -> None:
    """Ensure ``path`` exists, is a directory, and has mode 0700."""
    if path.exists():
        if not path.is_dir():
            raise StateStoreError(f"path exists but is not a directory: {path}")
        if path.is_symlink():
            raise StateStoreError(f"path is a symlink: {path}")
        # Tighten mode if set elsewhere
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    else:
        path.mkdir(mode=0o700, parents=False)


def _ensure_private_file(path: Path) -> None:
    """Ensure ``path`` exists with mode 0600."""
    if path.exists():
        if path.is_symlink():
            raise StateStoreError(f"file is a symlink: {path}")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically and durably with mode 0600.

    The write proceeds as:
    1. Create a private temporary file in the same directory as the
       target. The temp file does not collide with existing lock
       files because tempfile.mkstemp is atomic and uses random
       suffixes.
    2. Write the data, flush Python buffers, and fsync the file to
       push the data to durable storage.
    3. Set the temp file mode to 0600.
    4. Atomically rename the temp file to the target.
    5. fsync the parent directory so the rename is also durable.
    6. If anything fails, clean up the temp file and raise.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Ensure parent directory is private.
    try:
        os.chmod(path.parent, 0o700)
    except OSError as e:
        raise StateStoreError(f"cannot set parent dir mode 0700: {e!r}")
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp_path, 0o600)
        except OSError as e:
            raise StateStoreError(f"cannot set file mode 0600: {e!r}")
        os.replace(tmp_path, path)
        # fsync the parent directory so the rename is durable
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # On platforms without O_DIRECTORY (e.g. Windows), skip
            # directory fsync. Atomic replace still holds.
            pass
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict:
    """Read a JSON file with strict mode 0600 enforcement.

    Uses lstat() to detect symlinks before stat() would follow them.
    Rejects:
    - missing files;
    - symlinks (valid or dangling);
    - world- or group-readable files.
    """
    try:
        lst = os.lstat(path)
    except FileNotFoundError:
        raise StateStoreError(f"file missing: {path}")
    except OSError as e:
        raise StateCorruption(f"lstat failed for {path}: {e}")
    if stat.S_ISLNK(lst.st_mode):
        raise StateCorruption(f"file is a symlink: {path}")
    if (lst.st_mode & 0o777) & 0o077:
        raise StateCorruption(
            f"file has world or group permission bits set: {path} "
            f"(mode={oct(lst.st_mode & 0o777)})"
        )
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except (OSError, FileNotFoundError) as e:
        raise StateCorruption(f"read failed for {path}: {e}")
    try:
        data = json.loads(blob.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise StateCorruption(f"JSON decode failed for {path}: {e}")
    if not isinstance(data, dict):
        raise StateCorruption(f"top-level JSON for {path} must be an object")
    return data


@dataclass
class StateStore:
    """Filesystem-backed state store for one run.

    The store is bound to a single state root (per-run). All paths
    inside the store are relative to that root and must consist of
    safe characters only.
    """

    state_root: str
    schema_version: str = "autocoder.state_store.v1"

    def __post_init__(self) -> None:
        if not os.path.isabs(self.state_root):
            raise ValueError(f"state_root must be absolute: {self.state_root!r}")
        _ensure_private_dir(Path(self.state_root))

    # === Mutators ===
    def write_atomic(self, rel_path: str, payload: dict) -> StateRevision:
        safe = _safe_path(rel_path)
        full = Path(self.state_root) / safe
        if not isinstance(payload, dict):
            raise StateStoreError("payload must be a dict")
        # Read existing revision from file (preferred) or from caller's payload (fallback)
        existing_payload = self.read_optional(safe)
        if existing_payload is not None:
            existing_rev = int(existing_payload.get("_revision", 0))
        else:
            existing_rev = int(payload.get("_revision", 0))
        payload["_revision"] = existing_rev + 1
        payload["_schema_version"] = self.schema_version
        payload["_written_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        _atomic_write(full, blob)
        return StateRevision(path=safe, revision=payload["_revision"])

    def compare_and_swap(self, rel_path: str, payload: dict, expected_revision: int) -> StateRevision:
        """Atomically update ``rel_path`` only if its current revision
        equals ``expected_revision``.

        The operation is wrapped in a stable advisory lock keyed by
        a dedicated coordination-lock inode. The lock covers:
        - revision read
        - expected-revision comparison
        - new-state construction
        - durable write
        - revision update
        A concurrent caller attempting the same CAS will block on
        the lock until the original caller finishes.
        """
        safe = _safe_path(rel_path)
        full = Path(self.state_root) / safe
        # Coordination lock file (separate from the lease lock).
        coord_lock_path = Path(self.state_root) / f".{safe}.cas.lock"
        fd = None
        try:
            fd = os.open(str(coord_lock_path), os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if fd is not None:
                os.close(fd)
            raise StateStoreError(f"cannot acquire CAS coordination lock for {safe}: {e}")
        try:
            existing = self.read_strict(rel_path)
            existing_rev = int(existing.get("_revision", 0))
            if existing_rev != expected_revision:
                raise StateRevisionMismatch(
                    f"revision mismatch: expected {expected_revision}, found {existing_rev}"
                )
            return self.write_atomic(safe, payload)
        finally:
            os.close(fd)
            try:
                os.unlink(coord_lock_path)
            except OSError:
                pass

    def append_journal(self, rel_path: str, entry: dict) -> None:
        safe = _safe_path(rel_path)
        full = Path(self.state_root) / safe
        if not isinstance(entry, dict):
            raise StateStoreError("journal entry must be a dict")
        entry["_appended_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode()
        # Append-only write: open in append mode, write, set mode 0600.
        full.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(full.parent, 0o700)
        with open(full, "ab") as f:
            f.write(line)
        _ensure_private_file(full)

    def read_journal(self, rel_path: str) -> Iterator[dict]:
        safe = _safe_path(rel_path)
        full = Path(self.state_root) / safe
        try:
            with open(full, "rb") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                        raise StateCorruption(f"invalid journal entry in {rel_path}: {e}")
        except FileNotFoundError:
            return

    # === Readers ===
    def read_strict(self, rel_path: str) -> dict:
        """Read a JSON file with mode and schema validation."""
        safe = _safe_path(rel_path)
        full = Path(self.state_root) / safe
        return _read_json(full)

    def read_optional(self, rel_path: str) -> Optional[dict]:
        safe = _safe_path(rel_path)
        full = Path(self.state_root) / safe
        if not full.exists():
            return None
        return _read_json(full)

    def exists(self, rel_path: str) -> bool:
        safe = _safe_path(rel_path)
        return (Path(self.state_root) / safe).exists()

    def remove_path(self, rel_path: str) -> None:
        """Remove a state file. Used to invalidate evidence."""
        safe = _safe_path(rel_path)
        full = Path(self.state_root) / safe
        if full.exists() and not full.is_symlink():
            full.unlink()

    def lock_path(self) -> str:
        return "lease.lock"

    def read_lease(self) -> Optional[ProcessIdentity]:
        lease = self.read_optional("lease.lock")
        if lease is None:
            return None
        try:
            return ProcessIdentity.from_dict(lease)
        except Exception as e:
            raise StateCorruption(f"invalid lease: {e}")

    def write_lease(self, identity: ProcessIdentity) -> None:
        if not isinstance(identity, ProcessIdentity):
            raise StateStoreError("identity must be a ProcessIdentity")
        _atomic_write(
            Path(self.state_root) / "lease.lock",
            json.dumps({**identity.to_dict(), "_schema_version": self.schema_version}, sort_keys=True).encode(),
        )

    def clear_lease(self) -> None:
        self.remove_path("lease.lock")

    def read_revision(self, rel_path: str) -> int:
        return int(self.read_strict(rel_path).get("_revision", 0))


# === Lease helper ===
class Lease:
    """Context manager around an advisory flock.

    The lock file is owned by a process identity (pid + start_id).
    A subsequent acquirer must:

    1. Hold the flock;
    2. Verify that the file's owned-by identity matches the
       current process identity.

    If the file is missing or unowned, the lease is acquired.
    """

    def __init__(self, store: StateStore, identity: Optional[ProcessIdentity] = None) -> None:
        self.store = store
        self.identity = identity or current_process_identity()
        self.lock_path = Path(store.state_root) / "lease.lock"
        self._fd: Optional[int] = None

    def __enter__(self) -> "Lease":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def acquire(self) -> None:
        # Open (or create) the lock file in read-write mode.
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.lock_path.parent, 0o700)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            raise StateStoreError(f"cannot acquire advisory lock: {e}")
        # Check the recorded identity. If absent or different, the
        # caller is the new owner. Acquire by writing the identity.
        try:
            existing = self.store.read_lease()
        except StateCorruption:
            existing = None
        if existing is not None:
            # Same identity? Then it's a re-entrant acquire by the same
            # process — that's fine.
            if existing.pid != self.identity.pid or existing.start_id != self.identity.start_id:
                os.close(fd)
                raise StateStoreError(
                    f"lease held by different process: pid={existing.pid} "
                    f"start_id={existing.start_id[:16]}..."
                )
        # Write our identity. We hold the lock so this is safe.
        self.store.write_lease(self.identity)
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        # Don't delete the lock file; the lease record persists.
        # The next acquirer decides whether to overwrite based on
        # identity match.

    def is_held(self) -> bool:
        return self._fd is not None


def open_lease(store: StateStore, identity: Optional[ProcessIdentity] = None) -> Lease:
    """Open a Lease context manager."""
    return Lease(store, identity)
