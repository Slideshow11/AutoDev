"""Smoke-test script for the wheel install path.

Builds the autocoder-supervisor wheel into a fresh temporary directory,
installs the produced wheel into a fresh isolated venv (without the
repository source tree on PYTHONPATH), and verifies that both
autocoder_supervisor and autocoder_lifecycle are importable from the
installed distribution.

Strict isolation invariants:

- The wheel is built into a fresh ``TemporaryDirectory`` that has no
  pre-existing artifacts. The repository ``build/`` directory is NOT
  used.
- The build must produce EXACTLY ONE matching wheel. The script fails
  closed on zero or more than one matching wheel.
- ``PYTHONPATH`` is cleared before the install/import tests so that
  the repository source tree is not on the search path.
- All temporary directories are automatically cleaned at exit.
- Imports are exercised outside the repository checkout (in the
  TemporaryDirectory) so that the test cannot pass via repo source.

Run by:

    python3 scripts/build_wheel_and_install_smoke.py

Exits 0 on success; non-zero on any failure. Prints PKG_OK on success.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(args, **kwargs) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if proc.returncode != 0:
        print(f"FAIL: {' '.join(map(str, args))}")
        print(f"STDOUT: {proc.stdout}")
        print(f"STDERR: {proc.stderr}")
        sys.exit(proc.returncode)
    return proc


def main() -> None:
    # Fresh, isolated scratch space: do NOT use REPO_ROOT/build/ because
    # stale artifacts from previous runs can leak in. A TemporaryDirectory
    # under /tmp is automatically cleaned at exit.
    scratch = tempfile.TemporaryDirectory(prefix="autocoder_smoke_")
    try:
        scratch_root = Path(scratch.name)
        out_dir = scratch_root / "wheel"
        out_dir.mkdir(parents=True, exist_ok=True)
        _run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)],
            cwd=str(REPO_ROOT),
        )
        wheels = sorted(out_dir.glob("autocoder_supervisor-*.whl"))
        # Strict: require exactly one matching wheel.
        if len(wheels) == 0:
            print(f"FAIL: no wheel produced in {out_dir}")
            sys.exit(1)
        if len(wheels) > 1:
            print(
                f"FAIL: expected exactly one matching wheel, found {len(wheels)}: {wheels}"
            )
            sys.exit(1)
        wheel_path = wheels[0]
        print(f"Built: {wheel_path}")

        venv_dir = scratch_root / "venv"
        _run([sys.executable, "-m", "venv", str(venv_dir)])
        pip = venv_dir / "bin" / "pip"

        # Strict: clear PYTHONPATH so the repo source tree is invisible.
        env = {**os.environ, "PYTHONPATH": ""}
        _run([str(pip), "install", str(wheel_path)], env=env, cwd=str(scratch_root))

        # Import test with PYTHONPATH explicitly cleared.
        py = str(venv_dir / "bin" / "python3")
        cmd = (
            "import autocoder_lifecycle as l, autocoder_supervisor as s; "
            "assert hasattr(l, 'CheckpointState'); "
            "assert hasattr(l, 'RegistryBuilder'); "
            "assert hasattr(l, 'WatchdogState'); "
            "assert hasattr(s, '__name__'); "
            "print('PKG_OK')"
        )
        proc = subprocess.run(
            [py, "-c", cmd],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": ""},
            cwd=str(scratch_root),
        )
        if proc.returncode != 0:
            print("FAIL: import smoke")
            print(f"STDOUT: {proc.stdout}")
            print(f"STDERR: {proc.stderr}")
            sys.exit(proc.returncode)
        if "PKG_OK" not in proc.stdout:
            print(f"FAIL: import smoke did not print PKG_OK (got {proc.stdout!r})")
            sys.exit(1)

        # CLI help test for autocoder_supervisor.supervisor.
        proc = subprocess.run(
            [py, "-m", "autocoder_supervisor.supervisor", "--help"],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": ""},
            cwd=str(scratch_root),
        )
        if proc.returncode != 0:
            print("FAIL: supervisor CLI help smoke")
            print(f"STDOUT: {proc.stdout}")
            print(f"STDERR: {proc.stderr}")
            sys.exit(proc.returncode)

        # Lifecycle import sanity test.
        proc = subprocess.run(
            [py, "-c",
             "import autocoder_lifecycle as m; "
             "assert m.ImmutableLifecycleRegistry() is not None; "
             "print('LIFECYCLE_OK')"],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": ""},
            cwd=str(scratch_root),
        )
        if proc.returncode != 0 or "LIFECYCLE_OK" not in proc.stdout:
            print("FAIL: lifecycle import smoke")
            print(f"STDOUT: {proc.stdout}")
            print(f"STDERR: {proc.stderr}")
            sys.exit(proc.returncode)

        print("PKG_OK")
    finally:
        # TemporaryDirectory cleanup. scratch is always defined even if
        # the body failed before reaching scratch_root.
        scratch.cleanup()


if __name__ == "__main__":
    main()
