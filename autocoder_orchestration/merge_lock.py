"""Cross-process merge execution lock.

A genuine cross-process lock that serializes ``execute_guarded_merge_transaction``
across multiple OS processes. A single machine can have at most one
merge transaction in flight at any time per evidence root, so the
invariants hold across process boundaries (not just threads).

Implementation:
- Lock file at ``<evidence_root>/.merge.lock`` with ``O_CREAT | O_RDWR``
  and mode 0600.
- ``fcntl.flock(LOCK_EX | LOCK_NB)`` -- non-blocking exclusive lock;
  an immediate ``LockUnavailable`` is raised if another process
  holds it.
- The file is created with mode 0600 (NOT inherited via
  ``os.set_inheritable``; that call is intentionally omitted
  because subprocesses must not inherit the holder's fd).
- The holder PID is written into the lock file ONLY after
  ``flock`` succeeds, so a contender can never overwrite the
  real holder's metadata. A contender that opens the file before
  acquiring ``flock`` will fail to acquire ``flock`` and will
  observe the actual holder's PID, not its own.
- The holder PID is truncated to length 0 and ``ftruncate``d /
  ``write``n / ``fsync``d after ``flock`` succeeds, so a short
  PID cannot leave trailing bytes from a prior holder.
- On ``__exit__`` (or GC), the lock is released by ``LOCK_UN``
  and closed. The lock file persists across calls so its inode
  is stable.
- Held locks are NOT broken by the next caller. A stale lock is
  detected only by the holder's ability to acquire it; the
  design assumes processes holding the lock are short-lived.

This module is intentionally small. It does not depend on any
other orchestration module. The merge transaction acquires the
lock inside ``execute_guarded_merge_transaction`` and releases
it on every code path (success, failure, exception).
"""
from __future__ import annotations

import contextlib
import fcntl
import os
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
    """Best-effort read of the holder PID recorded in the lock file.

    Reads up to 32 bytes and parses the leading decimal integer.
    Returns None if the file is empty or unparseable.
    """
    try:
        with open(lock_path, "rb") as f:
            buf = f.read(32)
        text = buf.decode("ascii", errors="replace").strip()
        if not text:
            return None
        return int(text.split()[0])
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def merge_lock(evidence_root: Path):
    """Acquire a cross-process exclusive merge lock for ``evidence_root``.

    Acquires the lock with ``LOCK_EX | LOCK_NB``. On contention
    raises ``LockUnavailable`` immediately; the holder PID read
    at that moment is the actual holder, not the contender.

    The caller MUST NOT nest ``merge_lock`` calls in the same
    process; a second acquisition in the same process returns
    a ``LockUnavailable`` immediately because the flock is
    already held by the same process and ``LOCK_NB`` rejects
    the second acquisition. This is consistent with the
    ``LOCK_NB`` call and with the
    ``test_lock_blocks_second_acquisition`` test.
    """
    lock_dir = Path(evidence_root)
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = lock_dir / ".merge.lock"

    holder_pid: Optional[int] = None
    fd = None
    try:
        fd = os.open(
            str(lock_path),
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        # Attempt to acquire the exclusive lock FIRST. Only the
        # actual holder writes the PID. A contender that opens
        # the file before flock will fail to acquire flock and
        # will read the holder PID from the file -- never its own.
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            # Lock not acquired. Read the actual holder's PID.
            # The contender MUST NOT modify the file: the holder
            # may be in the middle of writing its own PID.
            holder_pid = _holder_pid(lock_path)
            raise LockUnavailable(lock_path, holder_pid=holder_pid) from e
        # Lock acquired. NOW we can record the holder PID. Truncate
        # to length 0 first so a shorter PID cannot leave trailing
        # bytes from a prior holder, then write, then fsync.
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        try:
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError:
            pass
        try:
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(fd)
        except OSError as e:
            raise LockAcquisitionError(
                f"failed to record holder PID: {e!r}"
            ) from e
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