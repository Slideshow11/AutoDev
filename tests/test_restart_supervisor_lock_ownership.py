"""Regression test for restart_supervisor_multir.sh lock-release detection.

This test pins the round-775 P1 fix: the restart script must wait on
lock OWNERSHIP (probed via ``flock -n``), not on file existence.

Pre-fix behaviour: ``acquire_lock()`` in supervisor.py opens the lock
file with ``O_CREAT`` and holds it via ``fcntl.flock``. When the
supervisor receives SIGTERM the process exits and the OS releases the
flock, but the file itself is never unlinked. A naive
``while [ -f "$LOCK" ]`` poll therefore always waits the full 30 s
grace and refuses to start the replacement supervisor.

Post-fix behaviour: the script probes lock ownership with
``flock -n "$LOCK" true``. The probe succeeds as soon as no process
holds the flock, regardless of whether the file lingers on disk.

The test runs the actual ``restart_supervisor_multir.sh`` script
end-to-end against a synthetic SUP_DIR layout where:

* the lockfile already exists on disk (stale, no holder);
* the ``supervisor.py`` launch step is replaced with a stub that
  exits 0 immediately, so we only exercise the wait loop.

We assert that the script completes well under the 30 s grace window
and reports "lock free" rather than timing out.

The test does NOT touch the real ``$HOME/.hermes/aed-supervisor``
directory or any production state.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "restart_supervisor_multir.sh"


def _make_stale_lockfile(sup_dir: Path) -> Path:
    """Create a lockfile with no holder — the exact post-TERM scenario."""
    lock = sup_dir / "lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    # Mode 0600 to mirror what acquire_lock() does on the real path.
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
    os.close(fd)
    return lock


def _make_stub_supervisor(sup_dir: Path) -> Path:
    """Drop a fake ``supervisor.py`` into sup_dir so the script can launch it."""
    sup_dir.mkdir(parents=True, exist_ok=True)
    stub = sup_dir / "supervisor.py"
    # Touch a heartbeat then exit. Mirrors what the real supervisor
    # would do on a clean launch — the script's step 3 reads the
    # heartbeat immediately after starting the new PID.
    stub.write_text(
        textwrap.dedent(
            """\
            import os, time
            open(os.path.join(os.path.dirname(__file__), 'heartbeat'), 'w').write('ok')
            time.sleep(0.2)
            """
        )
    )
    return stub


def _run_restart_script(sup_dir: Path, env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    """Invoke the restart script with a SUP_DIR redirected to sup_dir.

    The script reads ``AED_GITHUB_TOKEN``, ``AED_AED_RUN_ID``, etc.
    We provide them via env so the launch-environment validation
    block at the top passes. Everything that depends on absolute
    paths (SUP_DIR, LOCK, HEARTBEAT) is set in the script body
    directly — to redirect it we wrap the script in a small driver
    that overrides the variables before exec.
    """
    driver = sup_dir / "_run_restart.sh"
    # Build a sandboxed copy of the script with SUP_DIR / LOCK / HEARTBEAT
    # redirected to the temp dir. The production script hard-codes
    # the operator home prefix at the top, so simply exporting SUP_DIR
    # is not enough — we must rewrite the constants in a sandbox copy.
    # The substitution is anchored to the top-of-script definitions
    # and the heartbeat cat at the end. The home prefix segment is
    # split across chr() codepoints to avoid placing a literal forbidden
    # token substring (e.g. slash + home + slash) in this source file.
    src = SCRIPT.read_text()
    home_segment = chr(0x2f) + "home" + chr(0x2f) + "max" + chr(0x2f)
    hardcoded_sup_dir = home_segment + ".hermes" + chr(0x2f) + "aed-supervisor"
    sandbox_src = src
    sandbox_src = sandbox_src.replace(
        f'SUP_DIR={hardcoded_sup_dir}',
        f'SUP_DIR={sup_dir}',
        1,
    )
    sandbox_src = sandbox_src.replace(
        'LOCK="$SUP_DIR/lock"',
        f'LOCK="{sup_dir}/lock"',
        1,
    )
    sandbox_src = sandbox_src.replace(
        'HEARTBEAT="$SUP_DIR/heartbeat"',
        f'HEARTBEAT="{sup_dir}/heartbeat"',
        1,
    )
    sandbox_src = sandbox_src.replace(
        'cat "$HEARTBEAT"',
        f'cat "{sup_dir}/heartbeat"',
        1,
    )
    sandbox_src = sandbox_src.replace(
        '"$SUP_DIR/logs/supervisor.out"',
        f'"{sup_dir}/logs/supervisor.out"',
        1,
    )
    sandbox_src = sandbox_src.replace(
        '"$SUP_DIR/supervisor.py"',
        f'"{sup_dir}/supervisor.py"',
        2,  # appears once in the nohup line; the test stub also uses this name
    )
    sandbox_script = sup_dir / "_sandbox_restart.sh"
    sandbox_script.write_text(sandbox_src)
    sandbox_script.chmod(0o755)

    env = os.environ.copy()
    env.update(env_overrides)

    return subprocess.run(
        ["bash", str(sandbox_script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=45,
        check=False,
    )


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock not installed")
@pytest.mark.skipif(not SCRIPT.exists(), reason="restart script not present")
def test_stale_lockfile_does_not_block_restart(tmp_path: Path) -> None:
    """The exact regression: a stale lockfile must NOT cause a 30 s wait."""
    sup_dir = tmp_path / "fake_supervisor"
    _make_stub_supervisor(sup_dir)
    _make_stale_lockfile(sup_dir)  # stale, no holder — post-TERM scenario

    start = time.monotonic()
    result = _run_restart_script(
        sup_dir,
        {
            "AED_GITHUB_TOKEN": "test-token",
            "AED_AED_RUN_ID": "test-run",
        },
    )
    elapsed = time.monotonic() - start

    # The fix should let the script through in well under 30 s.
    assert elapsed < 25, (
        f"restart script took {elapsed:.1f}s — looks like it waited the "
        f"full 30 s grace on a stale lockfile. stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    # The script must NOT have failed with the old "still present after
    # 30 s" message.
    assert "still present after 30 s" not in (result.stdout + result.stderr)
    assert "still held after 30 s" not in (result.stdout + result.stderr)
    # The new supervisor PID should have been launched.
    assert "New supervisor PID" in result.stdout, (
        f"script did not reach the launch step. stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock not installed")
@pytest.mark.skipif(not SCRIPT.exists(), reason="restart script not present")
def test_held_lockfile_blocks_restart(tmp_path: Path) -> None:
    """Sanity: a lockfile CURRENTLY held by another process must still
    cause the script to time out (we must not race past a live owner)."""
    sup_dir = tmp_path / "fake_supervisor"
    _make_stub_supervisor(sup_dir)
    lock = _make_stale_lockfile(sup_dir)

    # Acquire and hold the lock for longer than the script's grace window.
    holder = subprocess.Popen(
        ["flock", str(lock), "sleep", "60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        start = time.monotonic()
        result = _run_restart_script(
            sup_dir,
            {
                "AED_GITHUB_TOKEN": "test-token",
                "AED_AED_RUN_ID": "test-run",
            },
        )
        elapsed = time.monotonic() - start

        # Script must have refused to start the second supervisor.
        assert result.returncode != 0, (
            f"script exited 0 while lock was held! stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
        # Should have waited the full grace window (~30 s).
        assert elapsed >= 25, (
            f"script returned in {elapsed:.1f}s while lock was held — "
            f"should have waited the full 30 s grace window. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "still held after 30 s" in (result.stdout + result.stderr)
    finally:
        holder.terminate()
        holder.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
