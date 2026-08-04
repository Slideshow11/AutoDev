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
import re
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
            except OSError as exc:
                # Fail closed: an unreadable committed file
                # is recorded as a violation. The scanner
                # does not silently skip unreadable files
                # because a real secret could be hiding
                # behind a permission error.
                violations.append((rel, f"unreadable: {exc!r}"))
                continue
            # Decode: detect UTF-16LE / UTF-16BE BOMs before
            # falling back to UTF-8. UTF-8 decoding with
            # errors="replace" would otherwise insert NUL
            # bytes between every byte of a UTF-16-encoded
            # file and the scanner would miss the forbidden
            # tokens in such a file.
            if content.startswith(b"\xff\xfe"):
                encoding = "utf-16-le"
            elif content.startswith(b"\xfe\xff"):
                encoding = "utf-16-be"
            else:
                encoding = "utf-8"
            try:
                text = content.decode(encoding, errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = content.decode("utf-8", errors="replace")
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


def _git_ls_files(repo_root: Path) -> set:
    """Return the set of paths tracked by git, using
    ``git ls-files -z`` to handle any unusual filenames.

    Used by the tracked-runtime-state check: a runtime-state
    path is forbidden to be tracked even if ``.gitignore``
    is bypassed by ``git add -f``. The scanner reads the
    tracked path set and rejects any tracked runtime-state
    path.
    """
    import subprocess as _subprocess
    proc = _subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        capture_output=True, timeout=30,
    )
    if proc.returncode != 0:
        # If git is not available we cannot verify tracked
        # paths; surface that as a scan failure.
        raise RuntimeError(
            f"git ls-files failed: {proc.stderr.decode()!r}"
        )
    # NUL-separated paths.
    return {
        p.decode()
        for p in proc.stdout.split(b"\x00")
        if p
    }


RUNTIME_STATE_TRACKED_PATTERNS = (
    re.compile(r"(^|/)state(/|$)"),
    re.compile(r"(^|/)logs(/|$)"),
    re.compile(r"(^|/)(lock|heartbeat)$"),
    re.compile(r"(^|/)worker_lease\.json$"),
    re.compile(r"(^|/)quota_state\.json$"),
    re.compile(r"(^|/)unconsumed_events\.json$"),
    re.compile(r"(^|/)launched_events\.json$"),
    re.compile(r"(^|/)snapshot_[ab]\.json$"),
    re.compile(r"(^|/)readiness_state\.json$"),
    re.compile(r"(^|/)run_state\.json$"),
    re.compile(r"(^|/)review_requests(/|$)"),
    re.compile(r"MERGE_TERMINAL_EVIDENCE\.json$"),
    re.compile(r"PAUSED_CONTEXT_HANDOFF\.json$"),
    re.compile(r"supervisor\.log$"),
)


def _check_no_tracked_runtime_state(repo_root: Path) -> list:
    """Reject any tracked path that matches a runtime-state
    pattern. This catches ``git add -f`` bypassing the
    ``.gitignore``.
    """
    try:
        tracked = _git_ls_files(repo_root)
    except RuntimeError as exc:
        return [(Path("__git__"), str(exc))]
    bad = []
    for p in sorted(tracked):
        for pat in RUNTIME_STATE_TRACKED_PATTERNS:
            if pat.search(p):
                bad.append(Path(p))
                break
    return bad


def main() -> int:
    repo_root = Path(os.getcwd()).resolve()
    scanner_input = repo_root / SCANNER_INPUT_REL
    rc = run(repo_root, scanner_input)
    if rc != 0:
        return rc
    # Tracked-runtime-state enforcement.
    bad = _check_no_tracked_runtime_state(repo_root)
    if bad:
        print("FAIL: tracked-runtime-state paths in repository:")
        for p in bad:
            print(f"  {p}: matches runtime-state pattern")
        return 1
    print("OK: committed-state-scan")
    return 0


if __name__ == "__main__":
    sys.exit(main())
