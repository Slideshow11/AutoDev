"""Canonical committed-state scanner (entry point module).

The set of forbidden tokens is loaded at runtime from a
controlled token-definition file. The scanner does not
have the literal forbidden tokens in its own source code;
they are loaded from the token-definition file at scan
time. This means the scanner's own source file contains
none of the forbidden strings, so it can be scanned
alongside every other committed file without special
exemption.

A small set of files contain forbidden tokens as part of
their documented purpose (validator source, invariants
ledger, rejection-test fixtures). For each such file we
accept an explicit allow-list of tokens; any other match
is a violation.

The token-definition input file (.github/workflows/scan-forbidden.txt)
is unconditionally exempt — it lists the forbidden
patterns by design.

Public entry points:

- run(repo_root, scanner_input) returns the exit code
  (0 if clean, 1 on violation).
- main() reads the repo root from the working directory
  and the forbidden-token list from the token-definition
  input file.

The scanner fails closed: any unexpected token match causes
the program to print the offending file + token and exit 1.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


# Location of the scanner's controlled token-definition
# input file, relative to the repo root. This is the only
# file the scanner explicitly trusts as input data; it
# lists the forbidden patterns by design.
SCANNER_INPUT_REL = ".github/workflows/scan-forbidden.txt"


def _load_forbidden(scanner_input: Path) -> list[str]:
    return [
        line.rstrip("\n")
        for line in scanner_input.read_text().splitlines()
        if line.strip()
    ]


SCANNER_ALLOWLIST_REL = "scripts/scanner-allowlist.json"


def _load_allowlist(repo_root: Path) -> dict:
    """Load the scanner's per-file allow-list mapping.

    The allow-list is loaded from a checked-in JSON file so
    that the scanner's own source does not contain any of
    the forbidden tokens. Any change to the allow-list must
    be reviewed.
    """
    allowlist_path = repo_root / SCANNER_ALLOWLIST_REL
    if not allowlist_path.exists():
        return {}
    import json as _json
    data = _json.loads(allowlist_path.read_text())
    return {
        Path(k): set(v)
        for k, v in data.get("files", {}).items()
    }


def run(repo_root: Path, scanner_input: Path) -> int:
    """Run the canonical committed-state scan.

    Returns 0 on success, 1 on violation.
    """
    if not scanner_input.exists():
        print(f"FAIL: scanner input file missing: {scanner_input}")
        return 1
    forbidden = _load_forbidden(scanner_input)
    allowlist = _load_allowlist(repo_root)

    # The set of documented detector patterns, by file path,
    # is provided by the tests/ fixtures (see
    # tests/test_extraction_provenance.py). The scanner does
    # not carry those mappings as literals — that would
    # defeat the purpose of the scanner by hard-coding the
    # forbidden tokens in its own source. Instead the
    # The per-file allow-list is loaded from
    # scripts/scanner-allowlist.json so the scanner's own
    # source does not have to carry the literal forbidden
    # tokens. The default behaviour is strict: any forbidden
    # token found in a committed file is a violation UNLESS
    # the allow-list explicitly matches it for that file.
    # Allowing a file requires a reviewed commit. Adding a
    # file to the allow-list without a clear documentary
    # purpose is a publication-safety violation.

    violations = []
    for root, dirs, files in os.walk(repo_root):
        if (
            "/.git" in root
            or root.endswith("/.git")
            or "/__pycache__" in root
            or "/dist" in root or root.endswith("/dist")
            or "/build" in root or root.endswith("/build")
            or "/venv" in root or root.endswith("/venv")
            or "/.venv" in root or root.endswith("/.venv")
            or "/.pytest_cache" in root or root.endswith("/.pytest_cache")
            or "/node_modules" in root or root.endswith("/node_modules")
        ):
            continue
        for f in files:
            p = Path(root) / f
            try:
                rel = p.relative_to(repo_root)
            except ValueError:
                continue
            # The scanner's own data input is the only
            # unconditionally-exempt file (it lists the
            # forbidden patterns by design).
            if rel == Path(SCANNER_INPUT_REL):
                continue
            try:
                content = p.read_bytes()
                text = content.decode("utf-8", errors="replace")
            except Exception:
                continue
            for token in forbidden:
                if token not in text:
                    continue
                allowed = allowlist.get(rel)
                if allowed is not None and token in allowed:
                    continue
                violations.append((rel, token))
    if violations:
        print("FAIL: committed-state-scan detected forbidden tokens:")
        for path, token in violations:
            print(f"  {path}: contains {token!r}")
        return 1
    print("OK: committed-state-scan")
    return 0


def main() -> int:
    repo_root = Path(os.getcwd()).resolve()
    scanner_input = repo_root / SCANNER_INPUT_REL
    return run(repo_root, scanner_input)


if __name__ == "__main__":
    sys.exit(main())
