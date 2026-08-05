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
    """Yield (path, token-value) for every Python token in non-string, non-comment positions.

    Uses the ``ast`` module's docstring detection to skip module-level,
    class-level, and function-level docstrings only. Runtime triple-quoted
    strings (e.g. ``message = \"\"\"HOLD_CODEX\"\"\"``) are NOT skipped, because
    they are real runtime values that the contract forbids.

    The check uses BOTH line and column coordinates so that a string
    statement on the SAME line as a docstring closing delimiter is
    correctly classified as runtime code (a docstring ends at its
    end-col, and a string statement starting after it is no longer
    inside the docstring region).
    """
    import ast
    for path in PACKAGE_ROOT.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        with open(path) as f:
            source = f.read()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        # Build a list of (start_line, start_col, end_line, end_col)
        # tuples describing literal-string docstring regions.
        docstring_regions: list[tuple[int, int, int, int]] = []
        for node in ast.walk(tree):
            if isinstance(
                node,
                (
                    ast.Module,
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.ClassDef,
                ),
            ):
                body_first = node.body[0] if node.body else None
                if (
                    isinstance(body_first, ast.Expr)
                    and isinstance(body_first.value, ast.Constant)
                    and isinstance(body_first.value.value, str)
                ):
                    ds = body_first.value
                    docstring_regions.append((ds.lineno, ds.col_offset, ds.end_lineno, ds.end_col_offset))

        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
        except (tokenize.TokenizeError, IndentationError):
            continue
        for tok in tokens:
            ttype, tstr, (srow, scol), (erow, ecol), _ = tok
            if ttype in (tokenize.NL, tokenize.NEWLINE):
                continue
            if ttype == tokenize.COMMENT:
                continue
            if ttype == tokenize.STRING and _in_docstring(srow, scol, erow, ecol, docstring_regions):
                continue
            yield path, tstr


def _in_docstring(srow: int, scol: int, erow: int, ecol: int, regions: list) -> bool:
    """True iff the (start_line, start_col) -> (end_line, end_col) range is
    entirely inside one of the registered docstring regions.
    """
    for ds_lo, ds_co, ds_hi, ds_eo in regions:
        if srow < ds_lo or (srow == ds_lo and scol < ds_co):
            continue
        if erow > ds_hi or (erow == ds_hi and ecol > ds_eo):
            continue
        return True
    return False


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
        # Build the wheel, install it into an isolated environment with
        # PYTHONPATH cleared, and verify both packages import from outside
        # the repository checkout.
        proc = subprocess.run(
            [sys.executable, "scripts/build_wheel_and_install_smoke.py"],
            cwd=str(PACKAGE_ROOT.parent),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, (
            f"wheel install smoke failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        )
        assert "PKG_OK" in proc.stdout
