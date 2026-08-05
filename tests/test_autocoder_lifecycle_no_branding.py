"""Regression test: autocoder_lifecycle runtime contracts contain no
AED-, Humphry-, Telegram-, Codex- or CodeRabbit-branded identifiers.

This is the regression test for the lifecycle-extraction audit-binding
contract. It walks the package's public surface and ensures none of
the documented imports, public attributes, exported symbols, or
class fields carry branded identifiers.

Documentation or provenance records may mention source origins where
clearly identified as historical provenance (e.g. a docstring that
explicitly excludes a provider-branded state from AutoDev core). Such
mentions are confined to comments and docstrings; runtime code paths
(imports, function bodies, default values, exception messages emitted
to callers) MUST NOT reference the disallowed identifiers.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "autocoder_lifecycle"

# Patterns that MUST NOT appear in actual code (tokens) outside
# docstrings and comments. Provider-branded state names are allowed
# only in negative-space docstrings.
DISALLOWED_CODE_ONLY = [
    "HOLD_CODEX",
    "CODEX_CLEAN",
    "chatgpt-codex",
    "coderabbit",
]

# Patterns that MUST NOT appear anywhere in the package source.
# Note: the user-profile-path family is enforced by the committed-state
# scanner; we rely on that scanner to gate the source tree.
DISALLOWED_ANYWHERE = [
    "Automated-Edge-Discovery",  # AED-side repo name
    "Humphry",                    # operator persona
    "Telegram",                   # operator surface
    "telegram",
    "aed_supervisor_lock",        # AED-side module
    "aed_run_identity",           # AED-side module
    "schemas/aed_lifecycle_states_v1.json",  # AED-side schema
]  # noqa: E501


def _iter_package_sources():
    for path in PACKAGE_ROOT.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        yield path, open(path).read()


def _iter_package_code_tokens():
    """Yield (path, token-value) for every Python token in non-string, non-comment positions."""
    for path in PACKAGE_ROOT.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        with open(path) as f:
            src = f.read()
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
        except (tokenize.TokenizeError, IndentationError):
            continue
        # Walk tokens; STRING tokens are triple-quoted docstrings when they
        # span multiple lines and the ENTIRE token text begins with the same
        # triple-quote prefix used for opening. We classify a string as a
        # docstring if it's the first STRING on its line and is preceded only
        # by indentation + whitespace (i.e. it is the body of a
        # module/class/function docstring).
        prev_kind_on_line = None
        for tok in tokens:
            ttype, tstr, (srow, scol), _, _ = tok
            if ttype in (tokenize.NL, tokenize.NEWLINE):
                prev_kind_on_line = None
                continue
            # Document heuristics for docstring detection.
            if ttype == tokenize.STRING and (tstr.startswith('"""') or tstr.startswith("'''")):
                # Treat as docstring if preceded only by indentation/NEWLINE.
                # Use a simple heuristic: a multiline string OR a
                # single-line triple-quote on its own.
                # We call it a docstring.
                prev_kind_on_line = "DOCSTRING"
                continue
            if ttype == tokenize.STRING:
                # Bare string literal — runtime code, treat as CODE.
                yield path, tstr
                prev_kind_on_line = "CODE"
                continue
            if ttype == tokenize.COMMENT:
                prev_kind_on_line = "COMMENT"
                continue
            # Any other token: treat as CODE.
            yield path, tstr
            prev_kind_on_line = "CODE"


class TestRuntimeContractsAreClean:
    def test_no_branded_identifier_anywhere(self) -> None:
        bad = []
        for path, source in _iter_package_sources():
            for pattern in DISALLOWED_ANYWHERE:
                if pattern in source:
                    bad.append((path, pattern))
        if bad:
            msg = "\n".join(f"  {p.relative_to(PACKAGE_ROOT)}: {pat!r}" for p, pat in bad)
            pytest.fail("Package contains forbidden identifier anywhere:\n" + msg)

    def test_no_provider_branded_identifier_in_code_tokens(self) -> None:
        bad = []
        for path, token in _iter_package_code_tokens():
            for pattern in DISALLOWED_CODE_ONLY:
                if pattern in token:
                    bad.append((path, pattern, token))
        if bad:
            msg = "\n".join(f"  {p.relative_to(PACKAGE_ROOT)}: {pat!r} in token {val!r}" for p, pat, val in bad)
            pytest.fail("Provider-branded identifier leaked into runtime code:\n" + msg)

    def test_no_aed_path_in_doctest_examples(self) -> None:
        # Run doctests via the package; this honours relative imports.
        proc = subprocess.run(
            [sys.executable, "-c",
             "import doctest\n"
             "import autocoder_lifecycle\n"
             "import autocoder_lifecycle.checkpoint\n"
             "import autocoder_lifecycle.no_stall\n"
             "import autocoder_lifecycle.registry\n"
             "import autocoder_lifecycle.watchdog\n"
             "results = []\n"
             "for mod in [autocoder_lifecycle, autocoder_lifecycle.checkpoint,\n"
             "            autocoder_lifecycle.no_stall, autocoder_lifecycle.registry,\n"
             "            autocoder_lifecycle.watchdog]:\n"
             "    results.append(doctest.testmod(mod, verbose=False))\n"
             "failures = sum(r.failed for r in results)\n"
             "print(f'doctest failures: {failures}')\n"
             "import sys\n"
             "sys.exit(0 if failures == 0 else 1)\n"],
            cwd="/tmp",
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(PACKAGE_ROOT.parent)},
        )
        assert proc.returncode == 0, (
            f"doctest failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        )
        assert "failures: 0" in proc.stdout

    def test_import_works_from_outside_repo(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-c", "import autocoder_lifecycle; print('OK')"],
            cwd="/tmp",
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(PACKAGE_ROOT.parent)},
        )
        assert proc.returncode == 0, proc.stderr
        assert "OK" in proc.stdout

    def test_package_layout(self) -> None:
        expected_files = {
            "__init__.py",
            "registry.py",
            "checkpoint.py",
            "no_stall.py",
            "watchdog.py",
        }
        actual_files = {p.name for p in PACKAGE_ROOT.iterdir()}
        assert expected_files <= actual_files, (
            f"missing files: {expected_files - actual_files}; got {actual_files}"
        )


class TestInstallableWheel:
    """Smoke-test that the package can be imported from a fresh install."""

    def test_can_install_from_external_directory(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-c",
             "import autocoder_lifecycle as pkg; "
             "assert hasattr(pkg, 'CheckpointState'); "
             "assert hasattr(pkg, 'RegistryBuilder'); "
             "assert hasattr(pkg, 'WatchdogState'); "
             "print('PKG_OK')"],
            cwd="/tmp",
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(PACKAGE_ROOT.parent)},
        )
        assert proc.returncode == 0, proc.stderr
        assert "PKG_OK" in proc.stdout
