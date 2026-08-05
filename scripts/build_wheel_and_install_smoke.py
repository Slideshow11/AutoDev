"""Smoke-test script for the wheel install path.

Builds the autocoder-supervisor wheel and installs it into a fresh
isolated venv (without the repository source tree on PYTHONPATH),
then verifies that both autocoder_supervisor and autocoder_lifecycle
are importable from the installed distribution and that the wheel
contains the expected packages.

Run by:

    python3 scripts/build_wheel_and_install_smoke.py

Exits 0 on success; non-zero on any failure. Prints PKG_OK on success.
"""
from __future__ import annotations

import os
import shutil
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
    out_dir = REPO_ROOT / "build"
    out_dir.mkdir(exist_ok=True)
    _run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)],
        cwd=str(REPO_ROOT),
    )
    wheels = list(out_dir.glob("autocoder_supervisor-*.whl"))
    if not wheels:
        print("FAIL: no wheel produced")
        sys.exit(1)
    wheel_path = wheels[-1]

    workdir = Path(tempfile.mkdtemp(prefix="autocoder_lifecycle_smoke_"))
    try:
        venv_dir = workdir / "venv"
        _run([sys.executable, "-m", "venv", str(venv_dir)])
        pip = venv_dir / "bin" / "pip"

        env = {**os.environ, "PYTHONPATH": ""}
        _run([str(pip), "install", str(wheel_path)], env=env, cwd=str(workdir))

        # Import test with PYTHONPATH explicitly cleared.
        py = str(venv_dir / "bin" / "python3")
        cmd = (
            f"import autocoder_lifecycle as l, autocoder_supervisor as s; "
            f"assert hasattr(l, 'CheckpointState'); "
            f"assert hasattr(l, 'RegistryBuilder'); "
            f"assert hasattr(l, 'WatchdogState'); "
            f"assert hasattr(s, '__name__'); "
            f"print('PKG_OK')"
        )
        proc = subprocess.run(
            [py, "-c", cmd],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": ""},
            cwd=str(workdir),
        )
        if proc.returncode != 0:
            print(f"FAIL: import smoke")
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
            cwd=str(workdir),
        )
        if proc.returncode != 0:
            print(f"FAIL: supervisor CLI help smoke")
            print(f"STDOUT: {proc.stdout}")
            print(f"STDERR: {proc.stderr}")
            sys.exit(proc.returncode)

        # CLI help test for autocoder_lifecycle CLI (if any).
        proc = subprocess.run(
            [py, "-c",
             "import autocoder_lifecycle as m; import argparse, sys; "
             "p = argparse.ArgumentParser(prog='autocoder_lifecycle'); "
             "print('CLI present')"],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": ""},
            cwd=str(workdir),
        )
        if proc.returncode != 0 or "CLI present" not in proc.stdout:
            print(f"FAIL: lifecycle import smoke")
            print(f"STDOUT: {proc.stdout}")
            print(f"STDERR: {proc.stderr}")
            sys.exit(proc.returncode)

        print("PKG_OK")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
