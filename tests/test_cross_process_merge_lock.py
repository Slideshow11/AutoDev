"""Cross-process merge lock tests.

These tests use real subprocesses (not threads) to prove the merge
lock serializes concurrent transactions across OS process boundaries.

A skipped thread-based test is NOT evidence of cross-process
serialization. The lock must be a real OS-level resource (fcntl.flock)
held by a separate process, with the holder's PID recorded in the
lock file for stale-holder detection.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autocoder_orchestration.merge_lock import (
    LockUnavailable,
    is_merge_lock_held,
    merge_lock,
)


class CrossProcessMergeLockTests(unittest.TestCase):
    """The merge lock serializes transactions across OS processes."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="aed-xproc-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_lock_can_be_acquired_when_free(self) -> None:
        """An unheld lock can be acquired; ``is_merge_lock_held``
        reflects the held state while the context manager is active."""
        # Before acquisition: not held.
        self.assertFalse(is_merge_lock_held(self.tmpdir))
        with merge_lock(self.tmpdir):
            self.assertTrue(is_merge_lock_held(self.tmpdir))
        # After release: not held.
        self.assertFalse(is_merge_lock_held(self.tmpdir))

    def test_lock_blocks_second_acquisition(self) -> None:
        """While one context holds the lock, a second context raises
        ``LockUnavailable`` immediately."""
        with merge_lock(self.tmpdir):
            with self.assertRaises(LockUnavailable):
                with merge_lock(self.tmpdir):
                    pass  # should never run

    def test_lock_releases_on_exception(self) -> None:
        """If the holder raises inside the context, the lock is
        released and another process can acquire it."""
        with self.assertRaises(RuntimeError):
            with merge_lock(self.tmpdir):
                raise RuntimeError("simulated crash inside the lock")
        # Lock must be free again.
        self.assertFalse(is_merge_lock_held(self.tmpdir))
        with merge_lock(self.tmpdir):
            pass

    def test_lock_file_persists_across_calls(self) -> None:
        """The lock file inode is stable so the OS flock tracks the
        same lock across acquire/release cycles."""
        lock_path = self.tmpdir / ".merge.lock"
        inodes: list[int] = []
        for _ in range(3):
            with merge_lock(self.tmpdir):
                inodes.append(lock_path.stat().st_ino)
        # All acquisitions saw the same lock file inode.
        self.assertEqual(len(set(inodes)), 1,
            f"lock file inode changed: {inodes}")

    def test_cross_process_serialization(self) -> None:
        """Two real subprocesses cannot both acquire the merge lock.
        The second subprocess must observe a held lock and exit
        cleanly with a controlled error. This is the production
        scenario: the supervisor launches a merge worker; a
        concurrent worker must back off."""
        # Resolve the repository root ONCE. The subprocess
        # scripts MUST be able to import autocoder_orchestration,
        # so sys.path has to point at the actual repo root --
        # not at self.tmpdir.parent (which is the system temp
        # directory and would fail the imports).
        repo_root = str(Path(__file__).resolve().parent.parent)
        # The first subprocess holds the lock for a measurable interval.
        holder_script = (
            "import sys, time\n"
            f"sys.path.insert(0, {repo_root!r})\n"
            "from autocoder_orchestration.merge_lock import merge_lock\n"
            f"with merge_lock({str(self.tmpdir)!r}):\n"
            "    time.sleep(2.0)\n"
            "    print('holder released')\n"
        )
        # The second subprocess attempts to acquire the lock immediately.
        contender_script = (
            "import sys\n"
            f"sys.path.insert(0, {repo_root!r})\n"
            "from autocoder_orchestration.merge_lock import (\n"
            "    LockUnavailable, merge_lock,\n"
            ")\n"
            "try:\n"
            f"    with merge_lock({str(self.tmpdir)!r}):\n"
            "        print('CONTENDER_ACQUIRED')\n"
            "except LockUnavailable as e:\n"
            "    print('CONTENDER_BLOCKED', str(e))\n"
            "    sys.exit(0)\n"
        )
        # Run the holder in background.
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        # Give the holder ~0.3s to acquire the lock.
        import time
        time.sleep(0.3)
        # Run the contender. It must be blocked.
        contender = subprocess.run(
            [sys.executable, "-c", contender_script],
            capture_output=True, text=True, timeout=10,
        )
        # Wait for the holder to release.
        holder_out, holder_err = holder.communicate(timeout=10)

        # Holder subprocess MUST exit successfully. A holder
        # failure would be reported as a misleading
        # "contender acquired" downstream. We assert the exit
        # status explicitly here.
        self.assertEqual(
            holder.returncode, 0,
            f"holder subprocess failed (rc={holder.returncode}); "
            f"stdout={holder_out!r} stderr={holder_err!r}",
        )

        self.assertIn("CONTENDER_BLOCKED", contender.stdout,
            f"contender stdout: {contender.stdout!r}\nstderr: {contender.stderr!r}")
        self.assertIn("holder released", holder_out,
            f"holder stdout: {holder_out!r}\nstderr: {holder_err!r}")
        self.assertEqual(contender.returncode, 0)

    def test_stale_holder_detection_records_pid(self) -> None:
        """When the lock is held, the holder's PID is recorded in the
        lock file so a subsequent acquirer can identify the stale
        holder for stale-holder detection."""
        lock_path = self.tmpdir / ".merge.lock"
        with merge_lock(self.tmpdir):
            # Read the lock file contents to find the holder PID.
            data = lock_path.read_bytes().decode("ascii", errors="replace")
            self.assertIn(str(os.getpid()), data,
                f"holder PID {os.getpid()} not recorded in {data!r}")
            # A second acquirer must raise LockUnavailable with the
            # holder's PID.
            try:
                with merge_lock(self.tmpdir):
                    self.fail("second acquisition should not have succeeded")
            except LockUnavailable as e:
                self.assertEqual(e.holder_pid, os.getpid())


if __name__ == "__main__":
    unittest.main()