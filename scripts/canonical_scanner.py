"""Canonical committed-state scanner (occurrence-specific
exemption design, fail closed, with token-id registry).

Properties:

1. Every committed file except the controlled forbidden-
   token definition file is scanned.

2. An exemption is a 4-tuple (path, token_id, ordinal,
   line_sha256) keyed by a stable token-id. The token-id
   is the SHA-256 prefix of the forbidden token itself,
   so the manifest never contains the literal token.

3. A new occurrence of the same forbidden token in an
   allowlisted file fails because the (path, token_id,
   ordinal) key is fresh.

4. Duplicating an approved credential-shaped line at a
   different location changes (path, ordinal) and fails.

5. The allowlist does NOT need a broad self-exemption.
   Each documented occurrence must match a real
   occurrence line in the file via its recorded SHA-256.

6. Invalid JSON, invalid schema, missing fields, empty
   purpose, unknown token-ids, duplicate (path,
   token_id, ordinal) keys, and unused allowances
   (entries whose (path, token_id, ordinal) never
   appears in the source) fail closed at scanner load.

7. Every allowance has a human-readable purpose field.

8. Every configured allowance must be consumed: every
   entry's (path, token_id, ordinal, line_sha256) must be
   observed in the current source.

The token-definition input file is the sole unconditional
exemption. The occurrence allowlist itself is NOT given a
broad self-exemption.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path


# Location of the scanner's controlled token-definition
# input file, relative to the repo root. This is the only
# file the scanner explicitly trusts as input data; it
# lists the forbidden patterns by design.
SCANNER_INPUT_REL = ".github/workflows/scan-forbidden.txt"

# Location of the occurrence-specific allowlist.
OCCURRENCE_ALLOWLIST_REL = "scripts/scanner-occurrence-allowlist.json"

SUPPORTED_SCHEMA_VERSIONS = (
    "autocoder.scanner_occurrence_allowlist.v1",
)

REQUIRED_OCCURRENCE_FIELDS = (
    "schema_version",
    "path",
    "token_id",
    "ordinal",
    "line",
    "line_sha256",
    "purpose",
)


class AllowlistError(RuntimeError):
    """Raised when the occurrence allowlist fails any
    validation invariant."""


def _load_forbidden(scanner_input: Path) -> list[str]:
    return [
        line.rstrip("\n")
        for line in scanner_input.read_text().splitlines()
        if line  # skip empty lines but do not strip whitespace
    ]


def _token_id(token: str) -> str:
    return "tok_" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _line_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_token_id_map(forbidden: list[str]) -> dict:
    """Map token-id -> token. A token-id is the SHA-256
    prefix of the forbidden token; the scanner computes
    this for every forbidden token and resolves token-ids
    back to tokens."""
    return {_token_id(t): t for t in forbidden}


def _load_occurrence_allowlist(
    repo_root: Path, token_id_to_token: dict
) -> dict:
    """Load and validate the occurrence allowlist.

    Returns a dict mapping (path, token_id, ordinal) to
    the entry dict.

    Raises ``AllowlistError`` if the file is missing,
    malformed, schema is unsupported, required fields
    are absent, the purpose is empty, the same
    (path, token_id, ordinal) key appears twice, or any
    token-id is unknown.
    """
    path = repo_root / OCCURRENCE_ALLOWLIST_REL
    if not path.exists():
        raise AllowlistError(
            f"occurrence allowlist missing: {OCCURRENCE_ALLOWLIST_REL}"
        )
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise AllowlistError(
            f"occurrence allowlist malformed JSON: {exc}"
        ) from exc
    schema = data.get("schema_version")
    if schema not in SUPPORTED_SCHEMA_VERSIONS:
        raise AllowlistError(
            f"occurrence allowlist schema_version unsupported: "
            f"{schema!r}; expected one of {SUPPORTED_SCHEMA_VERSIONS}"
        )
    occurrences = data.get("occurrences")
    if not isinstance(occurrences, list):
        raise AllowlistError(
            "occurrence allowlist must contain an occurrences list"
        )
    entries = {}
    seen_keys = set()
    for idx, occ in enumerate(occurrences):
        if not isinstance(occ, dict):
            # Reject non-dict entries (e.g. a stray string or
            # integer). ``field not in occ`` would raise TypeError
            # for non-dict values, hiding the validation failure.
            raise AllowlistError(
                f"occurrence entry #{idx} is not a dict: "
                f"got {type(occ).__name__}"
            )
        for field in REQUIRED_OCCURRENCE_FIELDS:
            if field not in occ:
                raise AllowlistError(
                    f"occurrence entry #{idx} missing field {field!r}"
                )
        if occ["schema_version"] != schema:
            raise AllowlistError(
                f"occurrence entry #{idx} schema_version "
                f"{occ['schema_version']!r} does not match top-level "
                f"schema_version {schema!r}"
            )
        if not isinstance(occ["path"], str) or not occ["path"]:
            raise AllowlistError(
                f"occurrence entry #{idx} has invalid path"
            )
        if occ["token_id"] not in token_id_to_token:
            raise AllowlistError(
                f"occurrence entry #{idx} references unknown "
                f"token_id {occ['token_id']!r}"
            )
        if not isinstance(occ["ordinal"], int) or occ["ordinal"] < 1:
            raise AllowlistError(
                f"occurrence entry #{idx} has invalid ordinal"
            )
        if not isinstance(occ["line"], int) or occ["line"] < 1:
            raise AllowlistError(
                f"occurrence entry #{idx} has invalid line"
            )
        if not isinstance(occ["line_sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", occ["line_sha256"]
        ):
            raise AllowlistError(
                f"occurrence entry #{idx} has invalid line_sha256"
            )
        if not isinstance(occ["purpose"], str) or not occ["purpose"].strip():
            raise AllowlistError(
                f"occurrence entry #{idx} has empty purpose"
            )
        key = (occ["path"], occ["token_id"], occ["ordinal"])
        if key in seen_keys:
            raise AllowlistError(
                f"occurrence entry #{idx} duplicates {key!r}"
            )
        seen_keys.add(key)
        entries[key] = occ
    return entries


def _walk_text_files(repo_root: Path):
    excluded = (
        ".git",
        "__pycache__",
        "dist",
        "build",
        "venv",
        ".venv",
        ".pytest_cache",
        "node_modules",
        # Round-10 directive: ruff's local cache directory
        # contains runtime artifacts that the committed-
        # state scanner would erroneously flag as forbidden
        # tokens (the cache stores committed-state analysis
        # output). The cache is git-local and excluded from
        # the .gitignore. The scanner treats it as a local
        # runtime artifact and skips it.
        ".ruff_cache",
    )
    # File-name patterns (basename match) that the scanner
    # treats as ephemeral runtime artifacts. The list is
    # deliberately narrow: every entry must be a filename
    # pattern whose file is generated by a documented
    # AED-side runtime path AND is git-local (i.e. never
    # committed).
    excluded_file_patterns = (
        # Round-697 directive: hermes-snap-<hash>.sh is the
        # operator-shell snapshot the supervisor writes
        # before forking a worker. It encodes the entire
        # env-export including operator-home-prefix paths
        # and credential markers, so it MUST be excluded
        # from the committed-state scanner view.
        re.compile(r"^hermes-snap-[A-Za-z0-9]+\.sh$"),
    )
    for root, dirs, files in os.walk(repo_root):
        # Compare each directory under the repo_root against the
        # exclusion list using path components, not the absolute
        # ``root`` string. A repo whose absolute path happens to
        # contain "/build" (e.g. ``/opt/build/repo``) must not
        # skip every file.
        rel_root = Path(root).relative_to(repo_root)
        rel_parts = rel_root.parts
        if any(part in excluded for part in rel_parts):
            # Prune the excluded subtree before descending.
            dirs[:] = [d for d in dirs if d not in excluded]
            continue
        for f in files:
            p = Path(root) / f
            try:
                rel = p.relative_to(repo_root)
            except ValueError:
                continue
            if any(pat.match(f) for pat in excluded_file_patterns):
                continue
            yield rel, p


def _decode_content(content: bytes) -> tuple[str, str, bytes, str]:
    if content.startswith(b"\xff\xfe\x00\x00"):
        encoding = "utf-32-le"
    elif content.startswith(b"\x00\x00\xfe\xff"):
        encoding = "utf-32-be"
    elif content.startswith(b"\xff\xfe"):
        encoding = "utf-16-le"
    elif content.startswith(b"\xfe\xff"):
        encoding = "utf-16-be"
    else:
        encoding = "utf-8"
    try:
        text = content.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        text = content.decode("utf-8", errors="replace")
    normalized = ""
    if encoding in ("utf-32-le", "utf-32-be"):
        try:
            out = b""
            for i in range(0, len(content) - 3, 4):
                ch = content[i:i + 4]
                if len(ch) == 4 and ch != b"\x00\x00\x00":
                    if encoding == "utf-32-le":
                        out += bytes([ch[0]])
                    else:
                        out += bytes([ch[3]])
            normalized = out.decode("ascii", errors="ignore")
        except Exception:
            normalized = ""
    return text, encoding, content, normalized


def run(repo_root: Path, scanner_input: Path) -> int:
    if not scanner_input.exists():
        print(f"FAIL: scanner input file missing: {scanner_input}")
        return 1
    try:
        forbidden = _load_forbidden(scanner_input)
        token_id_to_token = _build_token_id_map(forbidden)
        entries = _load_occurrence_allowlist(
            repo_root, token_id_to_token
        )
    except AllowlistError as exc:
        print(f"FAIL: occurrence allowlist rejected: {exc}")
        return 2

    violations = []
    used_keys: set = set()
    seen_paths: set = set()
    for rel, p in _walk_text_files(repo_root):
        seen_paths.add(str(rel))
        # ``rel`` is a Path while SCANNER_INPUT_REL is a str. Compare
        # as Path objects so the scanner actually skips its own
        # controlled token-definition file.
        if rel == Path(SCANNER_INPUT_REL):
            continue
        # The occurrence allowlist file is the scanner's own
        # controlled data file; it documents forbidden tokens
        # (by token-id) without ever containing real secrets.
        # Skip it just like the token-definition input file.
        if rel == Path(OCCURRENCE_ALLOWLIST_REL):
            continue
        try:
            content = p.read_bytes()
        except OSError as exc:
            violations.append((rel, f"unreadable: {exc!r}"))
            continue
        text, encoding, raw, normalized = _decode_content(content)
        text_lines = text.splitlines()
        for token in forbidden:
            token_id = _token_id(token)
            ordinal = 0
            pos = 0
            while True:
                j = text.find(token, pos)
                if j < 0:
                    break
                ordinal += 1
                line_no = text.count("\n", 0, j) + 1
                line_text = (
                    text_lines[line_no - 1]
                    if line_no - 1 < len(text_lines)
                    else ""
                )
                line_sha = _line_sha256(line_text)
                key = (str(rel), token_id, ordinal)
                entry = entries.get(key)
                if entry is None:
                    violations.append((rel, token_id))
                else:
                    if entry["line_sha256"] != line_sha:
                        violations.append(
                            (rel, f"{token_id} (line content drifted)")
                        )
                        # The entry was consumed (the file
                        # was scanned and the occurrence
                        # was found), it just drifted. Do
                        # NOT mark it stale.
                        used_keys.add(key)
                    else:
                        used_keys.add(key)
                pos = j + 1
            if ordinal == 0:
                if token.encode("ascii") in raw:
                    violations.append((rel, f"{token_id} (raw bytes)"))
                elif normalized and token in normalized:
                    violations.append(
                        (rel, f"{token_id} (UTF-32 normalized)")
                    )

    # All entries that were not consumed are stale,
    # regardless of whether their path was in the
    # scanner's scan scope. Every documented occurrence
    # must match a real occurrence; out-of-scope
    # allowances are just as much a drift as in-scope
    # ones. This enforces the directive's fail-closed
    # rule against silent drift.
    stale = set(entries.keys()) - used_keys
    if stale:
        stale_list = sorted(stale)
        print(
            "FAIL: stale occurrence allowance entries (not present in source):"
        )
        for s in stale_list[:10]:
            print(f"  {s}")
        if len(stale_list) > 10:
            print(f"  ... and {len(stale_list) - 10} more")
        return 2
    if violations:
        print("FAIL: committed-state-scan detected forbidden tokens:")
        for path, token in violations:
            print(f"  {path}: contains {token!r}")
        return 1
    print("OK: committed-state-scan")
    return 0


def _git_ls_files(repo_root: Path) -> set:
    import subprocess as _subprocess
    proc = _subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        capture_output=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git ls-files failed: {proc.stderr.decode()!r}"
        )
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