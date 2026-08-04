"""Packaging regression test for the autocoder_supervisor package.

This test reproduces the exact documented installation
procedure in an isolated temp directory, then proves that
the installed package:

1. is registered with the right top-level name
2. contains source files (not an empty wheel)
3. imports cleanly from outside the repository checkout
4. supports ``python -m autocoder_supervisor.supervisor`` and
   ``python -m autocoder_supervisor.validate`` from outside the
   repository checkout

If any of these checks fail, the install procedure is
broken and the test fails with a clear diagnostic.

The test does NOT mutate any production state, does NOT
require network access, and does NOT require the repository
to be on PYTHONPATH after the install completes (we run the
installed Python with ``PYTHONPATH=`` cleared).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_PKG = REPO_ROOT / "autocoder_supervisor"
SOURCE_MANIFEST = REPO_ROOT / "pyproject.toml"


def _python_executable() -> str:
    """The Python executable to use for the test venv.

    Honours ``sys.executable`` so the test runs against the
    same interpreter as pytest itself.
    """
    return sys.executable


def _stage_install_root(staging: Path) -> Path:
    """Reproduce the documented install procedure verbatim.

    Returns the path to the staged install root.
    """
    install_root = staging / "install-root"
    # Step 1: copy the package directory.
    shutil.copytree(SOURCE_PKG, install_root / "autocoder_supervisor")
    # Step 2: copy the packaging manifest.
    shutil.copy(SOURCE_MANIFEST, install_root / "pyproject.toml")
    return install_root


def _create_venv(staging: Path) -> Path:
    venv_dir = staging / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    return venv_dir


def _pip_install(venv_dir: Path, install_root: Path) -> None:
    """Install the staged package into the venv via ``pip
    install`` **without** ``--no-deps``.

    The staged install is the real production install. The
    conditional ``tomli`` dependency is declared in
    ``pyproject.toml`` for Python <3.11 and is installed by
    ``pip`` on those versions. Suppressing dependencies
    here would mask the Python 3.10 failure mode the
    package-install test is designed to catch.
    """
    pip = venv_dir / "bin" / "pip"
    proc = subprocess.run(
        [str(pip), "install", str(install_root)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"pip install failed (exit {proc.returncode}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def _pip_build_wheel(venv_dir: Path, install_root: Path,
                      wheel_dir: Path) -> Path:
    """Build a wheel into ``wheel_dir`` using ``pip wheel``.

    Returns the path to the built wheel. The test uses a
    test-owned wheel directory (not ``~/.cache/pip/wheels``)
    so the test never depends on shared state and the
    wheel path is deterministic.
    """
    pip = venv_dir / "bin" / "pip"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [str(pip), "wheel", "--no-deps", "--wheel-dir",
         str(wheel_dir), str(install_root)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"pip wheel failed (exit {proc.returncode}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    wheels = sorted(wheel_dir.glob("autocoder_supervisor-*.whl"))
    assert len(wheels) == 1, (
        f"expected exactly one wheel in {wheel_dir}, "
        f"found {len(wheels)}: {[w.name for w in wheels]}"
    )
    return wheels[0]


@pytest.fixture
def installed_venv(tmp_path: Path) -> Path:
    """Stage + install the package into an isolated venv.

    Yields the venv directory path. The test must use the
    venv's Python for import checks (so that the package is
    picked up via site-packages rather than via the test
    process's own import path).
    """
    staging = tmp_path / "staging"
    staging.mkdir()
    install_root = _stage_install_root(staging)
    venv_dir = _create_venv(staging)
    _pip_install(venv_dir, install_root)
    return venv_dir


@pytest.fixture
def wheel_path(tmp_path: Path) -> Path:
    """Build the wheel into a test-owned wheel directory.

    Returns the path to the built wheel. This fixture is
    independent of the ``installed_venv`` fixture so tests
    that only want to inspect the wheel don't need to
    install the package.
    """
    staging = tmp_path / "staging"
    staging.mkdir()
    install_root = _stage_install_root(staging)
    venv_dir = _create_venv(staging)
    wheel_dir = tmp_path / "wheels"
    return _pip_build_wheel(venv_dir, install_root, wheel_dir)


def test_install_produces_non_empty_wheel(wheel_path: Path):
    """The wheel produced by ``pip wheel`` must contain the
    package source files. An empty wheel (the previous
    failure mode) means the package was not discoverable by
    setuptools at install time.
    """
    with zipfile.ZipFile(wheel_path) as zf:
        names = zf.namelist()
    # Top-level package directory must contain the package
    # files. We assert on the existence of the canonical
    # modules rather than on the whole package layout.
    package_files = [
        n for n in names if n.startswith("autocoder_supervisor/")
    ]
    assert len(package_files) >= 5, (
        f"wheel {wheel_path} contains {len(package_files)} package "
        f"files; expected at least 5 (the canonical modules). "
        f"Package files: {package_files}"
    )
    assert "autocoder_supervisor/__init__.py" in names
    assert "autocoder_supervisor/supervisor.py" in names
    assert "autocoder_supervisor/validate.py" in names


def test_installed_package_top_level_name(wheel_path: Path):
    """The wheel's ``top_level.txt`` must list ``autocoder_supervisor``
    (and only that top-level package).
    """
    with zipfile.ZipFile(wheel_path) as zf:
        # Locate the .dist-info directory by scanning the
        # archive; do not hard-code a version number.
        top_level_paths = [
            name for name in zf.namelist()
            if name.endswith(".dist-info/top_level.txt")
        ]
        assert len(top_level_paths) == 1, (
            f"expected exactly one top_level.txt, "
            f"found {top_level_paths!r}"
        )
        with zf.open(top_level_paths[0]) as f:
            top_level = f.read().decode("utf-8").strip().splitlines()
    assert "autocoder_supervisor" in top_level, (
        f"top_level.txt does not list autocoder_supervisor: "
        f"{top_level!r}"
    )
    assert len(top_level) == 1, (
        f"top_level.txt lists multiple top-level packages: "
        f"{top_level!r}; the install layout has multiple package roots"
    )


def test_installed_package_imports_cleanly(installed_venv: Path, tmp_path):
    """Import the package from outside the repo with the
    repository's source tree NOT on PYTHONPATH.
    """
    python = str(installed_venv / "bin" / "python")
    # Run from a temp cwd that is NOT the AED repo so we
    # cannot accidentally import from the source tree.
    cwd = tmp_path / "import-cwd"
    cwd.mkdir()
    # PYTHONPATH must be cleared: any inherited value could
    # leak the AED repo into the test.
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONPATH"] = ""
    proc = subprocess.run(
        [
            python,
            "-c",
            "import autocoder_supervisor; "
            "from autocoder_supervisor import supervisor, validate, "
            "config, contracts; "
            "print(autocoder_supervisor.__file__); "
            "print(supervisor.__file__); "
            "print(validate.__file__); "
            "print(config.__file__); "
            "print(contracts.__file__)",
        ],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"installed package is not importable from outside the repo "
        f"(exit {proc.returncode}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    # The module paths must point at the venv's site-packages,
    # not at the AED repo's source tree.
    for line in proc.stdout.strip().splitlines():
        assert "site-packages" in line, (
            f"imported module path {line!r} is not in site-packages; "
            "the installed package is shadowed by something else"
        )
        assert str(REPO_ROOT) not in line, (
            f"imported module path {line!r} still points at the AED "
            "repo; the installed package is not actually being used"
        )


def test_installed_package_supervisor_help(installed_venv: Path, tmp_path):
    """``python -m autocoder_supervisor.supervisor --help`` works
    from outside the repo with PYTHONPATH cleared.
    """
    python = str(installed_venv / "bin" / "python")
    cwd = tmp_path / "help-cwd"
    cwd.mkdir()
    env = os.environ.copy()
    env["PYTHONPATH"] = ""
    proc = subprocess.run(
        [python, "-m", "autocoder_supervisor.supervisor", "--help"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"-m autocoder_supervisor.supervisor failed "
        f"(exit {proc.returncode}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "supervisor.py" in proc.stdout
    assert "--once" in proc.stdout


def test_installed_package_validate_help(installed_venv: Path, tmp_path):
    """``python -m autocoder_supervisor.validate --help`` works
    from outside the repo with PYTHONPATH cleared.
    """
    python = str(installed_venv / "bin" / "python")
    cwd = tmp_path / "help-cwd"
    cwd.mkdir()
    env = os.environ.copy()
    env["PYTHONPATH"] = ""
    proc = subprocess.run(
        [python, "-m", "autocoder_supervisor.validate", "--help"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"-m autocoder_supervisor.validate failed "
        f"(exit {proc.returncode}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "validate.py" in proc.stdout
    assert "--config" in proc.stdout


def test_staging_layout_matches_documented_install(
    installed_venv: Path, tmp_path
):
    """The staged install root has the exact documented layout:
    ``pyproject.toml`` and ``autocoder_supervisor/`` side-by-side
    at the install root.
    """
    # The install_root is the parent of the venv (both
    # are siblings under tmp_path/staging). Locate it by
    # scanning for the marker.
    install_roots = list(tmp_path.rglob("pyproject.toml"))
    assert install_roots, "no pyproject.toml found under tmp_path"
    for ir in install_roots:
        if ir.parent.name == "install-root":
            assert ir.is_file(), f"{ir} not a regular file"
            assert (ir.parent / "autocoder_supervisor").is_dir(), (
                f"{ir.parent}/autocoder_supervisor/ missing"
            )
            assert (ir.parent / "autocoder_supervisor" / "__init__.py").is_file()
            assert (ir.parent / "autocoder_supervisor" / "supervisor.py").is_file()
            assert (ir.parent / "autocoder_supervisor" / "validate.py").is_file()
            break
    else:
        pytest.fail(
            "no install-root/pyproject.toml found under tmp_path; "
            "the staging procedure did not produce the documented layout"
        )


def test_python_3_10_install_includes_tomli_dependency(
    installed_venv: Path,
):
    """On Python <3.11 the staged install (with declared
    runtime dependencies) pulls in the conditional
    ``tomli`` runtime dependency via the venv pip.

    On Python 3.11+ the dependency is skipped because the
    stdlib ``tomllib`` is used.
    """
    pip = installed_venv / "bin" / "pip"
    proc = subprocess.run(
        [str(pip), "show", "tomli"],
        capture_output=True, text=True,
    )
    py = subprocess.run(
        [str(installed_venv / "bin" / "python"), "-c",
         "import sys; print(sys.version_info.major, sys.version_info.minor)"],
        capture_output=True, text=True,
    )
    py_version = tuple(int(x) for x in py.stdout.strip().split())
    if py_version < (3, 11):
        assert proc.returncode == 0, (
            f"tomli is required on Python <3.11 (this venv is Python "
            f"{py_version}) but was not installed by pip:\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
        assert "tomli" in proc.stdout
    else:
        # On 3.11+, tomli is intentionally not installed.
        assert proc.returncode != 0, (
            f"tomli should not be installed on Python "
            f"{py_version} (tomllib is in stdlib)"
        )

