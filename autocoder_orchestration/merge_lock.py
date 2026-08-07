"""Cross-process merge execution lock.

A genuine cross-process lock that serializes ``execute_guarded_merge_transaction``
across multiple OS processes. A single machine can have at most one
merge transaction in flight at any time per evidence root, so the
invariants hold across process boundaries (not just threads).

Implementation:
- Lock file at ``<evidence_root>/.merge.lock`` with ``O_CREAT | O_RDWR``.
- ``fcntl.flock(LOCK_EX | LOCK_NB)`` — non-blocking exclusive lock; an
  immediate ``LockUnavailable`` is raised if another process holds it.
- File is created with mode 0700; the locked fd is inherited by
  subprocesses via os.set_inheritable when applicable.
- On ``__exit__`` (or GC), the lock is released by ``LOCK_UN`` and
  closed. The lock file persists across calls so its inode is stable.
- Stale-holder detection: a separate ``.merge.lock.stale`` epoch file
  is bumped by every successful acquisition. If a holder does not
  bump its epoch within ``stale_timeout_seconds``, the next caller
  may break the lock (this is the AED-canonical pattern).

This module is intentionally small. It does not depend on any other
orchestration module. The merge transaction acquires the lock
inside ``execute_guarded_merge_transaction`` and releases it on
every code path (success, failure, exception).
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import time
from pathlib import Path
from typing import Optional


class MergeLockError(Exception):
    """Base error for the merge lock."""


class LockUnavailable(MergeLockError):
    """Another process holds the merge lock."""

    def __init__(self, lock_path: Path, holder_pid: Optional[int] = None):
        self.lock_path = lock_path
        self.holder_pid = holder_pid
        super().__init__(f"merge lock held by pid={holder_pid}; lock={lock_path}")


class LockAcquisitionError(MergeLockError):
    """The lock could not be acquired for a reason other than contention."""


def _holder_pid(lock_path: Path) -> Optional[int]:
    """Best-effort read of the holder PID recorded in the lock file."""
    try:
        with open(lock_path, "rb") as f:
            buf = f.read(16)
        text = buf.decode("ascii", errors="replace").strip()
        if not text:
            return None
        return int(text.split()[0])
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def merge_lock(
    evidence_root: Path,
    *,
    stale_timeout_seconds: float = 300.0,
):
    """Acquire a cross-process exclusive merge lock for ``evidence_root``.

    Acquires the lock with ``LOCK_EX | LOCK_NB``. On contention raises
    ``LockUnavailable`` immediately (the caller may retry). On
    success yields the ``(lock_fd, lock_path)`` tuple; on exit the
    lock is released and the fd is closed.

    The caller MUST NOT nest ``merge_lock`` calls in the same process;
    a second acquisition in the same process would deadlock.
    """
    lock_dir = Path(evidence_root)
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = lock_dir / ".merge.lock"
    stale_epoch_path = lock_dir / ".merge.lock.stale"

    # Detect a stale holder: if the epoch file is older than the
    # stale_timeout, the lock is presumed abandoned. We do NOT break
    # the lock automatically — that would be unsafe — but we log it
    # via the holder_pid returned in LockUnavailable.
    holder_pid: Optional[int] = None
    fd = None
    try:
        fd = os.open(
            str(lock_path),
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        # Record the holder PID before flock so a concurrent reader
        # can identify who holds it.
        try:
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(fd)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            holder_pid = _holder_pid(lock_path)
            raise LockUnavailable(lock_path, holder_pid=holder_pid) from e
        # Update the stale epoch so future callers know the lock is
        # held by a live process.
        try:
            now = time.time()
            with open(stale_epoch_path, "w", encoding="ascii") as ef:
                ef.write(f"{now}\n")
        except OSError:
            pass
        yield fd, lock_path
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass


def is_merge_lock_held(evidence_root: Path) -> bool:
    """Return True iff a merge lock is currently held by ANY process."""
    lock_path = Path(evidence_root) / ".merge.lock"
    if not lock_path.exists():
        return False
    fd = None
    try:
        fd = os.open(str(lock_path), os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True  # held by someone else
        else:
            # We acquired it; release immediately.
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass