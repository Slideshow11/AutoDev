"""Extraction-specific regression tests for AutoDev v1.

Verifies the extraction provenance and the evidence-
semantics correction. These tests must run on the standalone
repository without depending on AED.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import codecs
import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PROVENANCE_PATH = REPO_ROOT / "provenance" / "aed-pr417-source-manifest.json"
EXTRACTION_NARRATIVE = REPO_ROOT / "provenance" / "EXTRACTION.md"

# Import SCANNER_INPUT_REL at module scope so the helper
# ``_build_minimal_manifest`` can resolve the constant without a
# per-caller import. Tests that exercise the scanner still
# import their own copy from canonical_scanner for convenience.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from canonical_scanner import SCANNER_INPUT_REL  # noqa: E402


FORBIDDEN_TOKENS_FULL = (
    '/home/', '/root/', '/Users/', '/~/.hermes/', 'gho_',
    'ghp_', 'ghs_', 'sk-', '-----BEGIN', 'aws_access_key_id',
    'aws_secret_access_key', 'oauth_token:', 'xoxa-', 'xoxb-', 'xoxp-',
    'xoxs-', 'password=', 'secret=', 'Authorization: Bearer '
)


@pytest.fixture
def scanner_fixture(tmp_path):
    """Fixture tree with SCANNER_INPUT_REL parent. The
    fixture does NOT pre-populate the occurrence allowlist
    so tests can either (a) call copy_real_allowlist() to
    use the real manifest or (b) build their own minimal
    manifest that excludes the file under test.
    """
    scanner_input_parent = tmp_path / ".github" / "workflows"
    scanner_input_parent.mkdir(parents=True, exist_ok=True)
    return tmp_path


def copy_real_allowlist(tmp_path):
    """Copy the real scanner-occurrence-allowlist.json into
    a fixture tree.
    """
    import shutil as _sh
    real = REPO_ROOT / "scripts" / "scanner-occurrence-allowlist.json"
    if real.exists():
        allowlist = tmp_path / "scripts"
        allowlist.mkdir(parents=True, exist_ok=True)
        _sh.copy(real, allowlist / "scanner-occurrence-allowlist.json")
    return tmp_path


def _copy_real_source_files(scanner_fixture):
    """Copy every source file referenced by the real
    occurrence allowlist into the fixture tree. Without
    this, manifest entries for files outside the fixture
    are reported as stale.
    """
    import shutil as _sh
    import json as _json
    real_manifest = REPO_ROOT / "scripts" / "scanner-occurrence-allowlist.json"
    if not real_manifest.exists():
        return
    data = _json.loads(real_manifest.read_text())
    for occ in data["occurrences"]:
        src = REPO_ROOT / occ["path"]
        dst = scanner_fixture / occ["path"]
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            _sh.copy(src, dst)





def _build_minimal_manifest(tmp_path, scanner_input):
    """Build a minimal occurrence allowlist by walking
    the fixture tree and recording every occurrence of
    every forbidden token in the scanner_input file. This
    makes each test self-contained: only the test's
    fixture contents are documented.
    """
    import hashlib as _hl
    import json as _json
    forbidden_text = scanner_input.read_text()
    forbidden = [
        ln.strip() for ln in forbidden_text.splitlines() if ln.strip()
    ]
    token_id_to_token = {}
    token_to_token_id = {}
    for tok in forbidden:
        tid = "tok_" + _hl.sha256(tok.encode("utf-8")).hexdigest()[:12]
        token_id_to_token[tid] = tok
        token_to_token_id[tok] = tid
    occurrences = []
    excluded = (
        "/.git", "/__pycache__", "/dist", "/build",
        "/venv", "/.venv", "/.pytest_cache", "/node_modules",
    )
    for root, dirs, files in os.walk(tmp_path):
        if any(ex in root for ex in excluded):
            continue
        for f in files:
            p = Path(root) / f
            try:
                rel = p.relative_to(tmp_path)
            except ValueError:
                continue
            if rel == Path(SCANNER_INPUT_REL):
                continue
            try:
                content = p.read_bytes()
            except OSError:
                continue
            # Detect encoding.
            if content.startswith(b"\xff\xfe\x00\x00"):
                enc = "utf-32-le"
            elif content.startswith(b"\x00\x00\xfe\xff"):
                enc = "utf-32-be"
            elif content.startswith(b"\xff\xfe"):
                enc = "utf-16-le"
            elif content.startswith(b"\xfe\xff"):
                enc = "utf-16-be"
            else:
                enc = "utf-8"
            try:
                text = content.decode(enc, errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = content.decode("utf-8", errors="replace")
            normalized = ""
            if enc in ("utf-32-le", "utf-32-be"):
                try:
                    out = b""
                    for i in range(0, len(content) - 3, 4):
                        ch = content[i:i + 4]
                        if len(ch) == 4 and ch != b"\x00\x00\x00":
                            if enc == "utf-32-le":
                                out += bytes([ch[0]])
                            else:
                                out += bytes([ch[3]])
                    normalized = out.decode("ascii", errors="ignore")
                except Exception:
                    normalized = ""
            for tok in forbidden:
                ordinal = 0
                pos = 0
                while True:
                    j = text.find(tok, pos)
                    if j < 0:
                        break
                    ordinal += 1
                    line_no = text.count("\n", 0, j) + 1
                    line_text = (
                        text.splitlines()[line_no - 1]
                        if line_no - 1 < len(text.splitlines()) else ""
                    )
                    line_sha = _hl.sha256(line_text.encode("utf-8")).hexdigest()
                    occurrences.append({
                        "schema_version":
                            "autocoder.scanner_occurrence_allowlist.v1",
                        "path": str(rel),
                        "token_id": token_to_token_id[tok],
                        "ordinal": ordinal,
                        "line": line_no,
                        "line_sha256": line_sha,
                        "purpose": (
                            f"Documented occurrence of token_id "
                            f"{token_to_token_id[tok]} in {rel} at "
                            f"line {line_no}; encoding={enc}; approved "
                            f"by review."
                        ),
                    })
                    pos = j + 1
                if ordinal == 0:
                    if tok.encode("ascii") in content:
                        occurrences.append({
                            "schema_version":
                                "autocoder.scanner_occurrence_allowlist.v1",
                            "path": str(rel),
                            "token_id": token_to_token_id[tok],
                            "ordinal": 1,
                            "line": 1,
                            "line_sha256": _hl.sha256(tok.encode("utf-8")).hexdigest(),
                            "purpose": (
                                f"Raw-byte fallback occurrence of "
                                f"token_id {token_to_token_id[tok]} in "
                                f"{rel}; approved by review."
                            ),
                        })
                    elif normalized and tok in normalized:
                        occurrences.append({
                            "schema_version":
                                "autocoder.scanner_occurrence_allowlist.v1",
                            "path": str(rel),
                            "token_id": token_to_token_id[tok],
                            "ordinal": 1,
                            "line": 1,
                            "line_sha256": _hl.sha256(tok.encode("utf-8")).hexdigest(),
                            "purpose": (
                                f"UTF-32 normalized fallback occurrence "
                                f"of token_id {token_to_token_id[tok]} in "
                                f"{rel}; approved by review."
                            ),
                        })
    payload = {
        "schema_version":
            "autocoder.scanner_occurrence_allowlist.v1",
        "token_id_registry": [
            {"token_id": tid, "token_sha256_prefix":
                _hl.sha256(t.encode("utf-8")).hexdigest()[:16],
             "note": "Token ID is the SHA-256 of the forbidden token."}
            for tid, t in token_id_to_token.items()
        ],
        "occurrences": occurrences,
    }
    allowlist_dir = tmp_path / "scripts"
    allowlist_dir.mkdir(parents=True, exist_ok=True)
    allowlist_path = allowlist_dir / "scanner-occurrence-allowlist.json"
    allowlist_path.write_text(_json.dumps(payload, indent=2) + "\n")
    return allowlist_path







SOURCE_REPO = "Slideshow11/Automated-Edge-Discovery"
SOURCE_COMMIT = "b57fcaad806c68b93668bcd318fa26ab15a8ab40"
SOURCE_PR = 417
SOURCE_REVIEWED_HEAD = "18ba0df49d2a19779e350d6df5a102b254cbeed7"


def _read_provenance() -> dict:
    assert PROVENANCE_PATH.exists(), (
        f"provenance manifest missing: {PROVENANCE_PATH}"
    )
    return json.loads(PROVENANCE_PATH.read_text())


def test_provenance_manifest_parses_and_contains_source_commit():
    """The provenance manifest is valid JSON and contains
    the exact AED source commit and reviewed head.
    """
    data = _read_provenance()
    assert data["source_repository"] == SOURCE_REPO
    assert data["source_commit"] == SOURCE_COMMIT
    assert data["source_pr"] == SOURCE_PR
    assert data["source_reviewed_head"] == SOURCE_REVIEWED_HEAD
    assert "files" in data
    assert isinstance(data["files"], list)
    assert len(data["files"]) >= 17


def test_provenance_every_listed_destination_file_exists():
    """Every file listed in the provenance manifest exists
    on disk in the destination repository (relative to the
    repo root).
    """
    data = _read_provenance()
    for entry in data["files"]:
        dst = entry["destination_path"]
        p = REPO_ROOT / dst
        assert p.exists(), f"missing destination file: {dst}"
        assert p.is_file(), f"destination is not a file: {dst}"


def test_provenance_destination_hashes_match_actual_files():
    """Every file's recorded destination SHA-256 matches the
    actual file on disk.
    """
    data = _read_provenance()
    for entry in data["files"]:
        dst = entry["destination_path"]
        p = REPO_ROOT / dst
        if not p.is_file():
            continue  # the manifest itself is excluded
        actual = hashlib.sha256(p.read_bytes()).hexdigest()
        recorded = entry["destination_sha256"]
        assert recorded == actual, (
            f"hash mismatch for {dst}: recorded {recorded[:12]}..., "
            f"actual {actual[:12]}..."
        )


def test_byte_identical_classifications_have_matching_hashes():
    """Files classified as ``byte_identical`` must have
    destination SHA-256 equal to source SHA-256.
    """
    data = _read_provenance()
    byte_identical = [
        f for f in data["files"]
        if f.get("transformation_classification") == "byte_identical"
    ]
    # The exact count varies by repair round. The manifests
    # we have produced historically include between 8 and 9
    # byte-identical files; subsequent repairs that change
    # supervisor.py / config.py / installation docs reduce
    # this number. We require at least one byte-identical
    # file to prove the byte_identical classification still
    # works end-to-end, and we assert that every byte-identical
    # entry's hashes actually match.
    assert len(byte_identical) >= 1, (
        f"expected at least 1 byte-identical file (the manifest's "
        f"byte-identical classification must still apply to "
        f"something); found {len(byte_identical)}"
    )
    for entry in byte_identical:
        assert entry.get("source_sha256") is not None
        assert entry["source_sha256"] == entry["destination_sha256"], (
            f"{entry['source_path']}: source and destination "
            f"hashes must match for byte_identical classification"
        )


def test_provenance_does_not_contain_tokens_or_user_paths():
    """The provenance manifest itself does not contain
    tokens, credentials, or user-specific paths.
    """
    data = _read_provenance()
    forbidden = [
        "/home/max/", "/root/", "/Users/",
        "gho_", "ghp_", "ghs_",
        "sk-", "-----BEGIN",
        "aws_access_key_id", "aws_secret_access_key",
        "oauth_token:",
    ]
    text = json.dumps(data)
    for token in forbidden:
        assert token not in text, (
            f"provenance manifest contains forbidden token: {token!r}"
        )


def test_canonical_scanner_returns_clean_on_current_tree():
    """The canonical committed-state scanner returns 0 on
    the current committed tree (no real credentials, no
    user paths, no runtime evidence).
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run as scanner_run, SCANNER_INPUT_REL
    rc = scanner_run(REPO_ROOT, REPO_ROOT / SCANNER_INPUT_REL)
    assert rc == 0


def test_canonical_scanner_skips_its_own_data_input(scanner_fixture):
    """The scanner's controlled token-definition file
    is unconditionally exempt. Adding a forbidden token
    in that file MUST NOT be a violation.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text("ghp_REAL_TOKEN\n", encoding="utf-8")
    _build_minimal_manifest(scanner_fixture, scanner_input)
    rc = run(scanner_fixture, scanner_input)
    assert rc == 0, (
        "scanner's own data input is unconditionally exempt"
    )


def test_canonical_scanner_rejects_real_credential_in_source_file(scanner_fixture):
    """A real credential-shaped token added to a file
    that is NOT exempted fails. This proves the
    occurrence allowlist is enforced.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    copy_real_allowlist(scanner_fixture)
    _copy_real_source_files(scanner_fixture)
    src = scanner_fixture / "src"
    src.mkdir()
    leak = src / "leak.txt"
    leak.write_text("my real credential: ghp_ABCDEF123\n")
    rc = run(scanner_fixture, scanner_input)
    assert rc == 1, (
        f"a real credential must fail scanning; rc={rc}"
    )


def test_canonical_scanner_documented_occurrence_passes(scanner_fixture):
    """An approved documented occurrence in the
    occurrence allowlist passes when the actual source
    line matches.

    The fixture file's content matches the documented
    allowlist entry line-for-line (same path, token_id,
    ordinal). The scanner should accept it as a documented
    occurrence and exit 0.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import (
        run, SCANNER_INPUT_REL, OCCURRENCE_ALLOWLIST_REL,
    )
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    # Build a fixture-specific allowlist containing ONLY the
    # one occurrence we are about to reproduce in the fixture.
    # Copying the entire real allowlist would fail the scanner
    # with rc=2 because the fixture tree does not contain the
    # real source files referenced by the other allowances.
    real = REPO_ROOT / OCCURRENCE_ALLOWLIST_REL
    real_manifest = json.loads(real.read_text())
    real_repo = REPO_ROOT
    # Pick the first occurrence that maps to a real repo file
    # AND whose recorded line contains exactly one forbidden
    # token (so the fixture reproduces a single clean entry
    # without dragging in collateral tokens from neighbouring
    # occurrences on the same line).
    forbidden = [
        ln.strip() for ln in
        scanner_input.read_text().splitlines() if ln.strip()
    ]
    src_occ = None
    for occ in real_manifest["occurrences"]:
        rel = occ["path"]
        if not (real_repo / rel).exists():
            continue
        text = (real_repo / rel).read_text(encoding="utf-8")
        lines = text.splitlines()
        if occ["line"] - 1 >= len(lines):
            continue
        line = lines[occ["line"] - 1]
        token_hits = [t for t in forbidden if t in line]
        if len(token_hits) != 1:
            continue
        # Resolve the recorded token_id back to its literal token
        # to confirm the single hit matches the allowance.
        recorded_token = None
        for tid_entry in real_manifest["token_id_registry"]:
            if tid_entry["token_id"] == occ["token_id"]:
                # The token-id registry stores SHA-256 prefixes
                # only; resolve via the scanner's runtime map.
                pass
        # Build the runtime token_id_to_token map (the
        # scanner does this internally from the forbidden
        # tokens; replicate for the assertion below).
        token_id_to_token = {
            "tok_" + hashlib.sha256(t.encode("utf-8")).hexdigest()[:12]: t
            for t in forbidden
        }
        recorded_token = token_id_to_token.get(occ["token_id"])
        if recorded_token is None or recorded_token not in token_hits:
            continue
        src_occ = occ
        break
    assert src_occ is not None, (
        "no single-token occurrence in the real manifest maps to a "
        "file in the repo and reproduces cleanly"
    )
    fixture_allowlist = {
        "schema_version": real_manifest["schema_version"],
        "token_id_registry": real_manifest["token_id_registry"],
        "occurrences": [src_occ],
    }
    allowlist = scanner_fixture / OCCURRENCE_ALLOWLIST_REL
    allowlist.parent.mkdir(parents=True, exist_ok=True)
    allowlist.write_text(
        json.dumps(fixture_allowlist), encoding="utf-8"
    )
    # Reproduce the documented line in the fixture.
    src = scanner_fixture / "src"
    src.mkdir()
    rel = src_occ["path"]
    fixture_file = scanner_fixture / rel
    fixture_file.parent.mkdir(parents=True, exist_ok=True)
    real_file = real_repo / rel
    line_no = src_occ["line"]
    line_text = real_file.read_text(encoding="utf-8").splitlines()[line_no - 1] + "\n"
    fixture_file.write_text(line_text, encoding="utf-8")
    rc = run(scanner_fixture, scanner_input)
    # The scanner must accept the documented occurrence
    # (rc == 0) since the fixture file reproduces the recorded
    # line exactly and the fixture allowlist documents only
    # this single (path, token_id, ordinal).
    assert rc == 0, (
        f"documented occurrence must pass; rc={rc}"
    )


def test_canonical_scanner_rejects_unknown_token_id(scanner_fixture):
    """The scanner rejects an occurrence allowlist that
    references an unknown token_id.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    copy_real_allowlist(scanner_fixture)
    _copy_real_source_files(scanner_fixture)
    src = scanner_fixture / "src"
    src.mkdir()
    leak = src / "leak.txt"
    leak.write_text("my credential: ghp_ABC\n")
    bad_allowlist = {
        "schema_version": "autocoder.scanner_occurrence_allowlist.v1",
        "token_id_registry": [
            {"token_id": "tok_unknown", "token_sha256_prefix": "dead",
             "note": "Unknown token-id"},
        ],
        "occurrences": [
            {
                "schema_version": "autocoder.scanner_occurrence_allowlist.v1",
                "path": "src/leak.txt",
                "token_id": "tok_unknown",
                "ordinal": 1,
                "line": 1,
                "line_sha256": "abc",
                "purpose": "test",
            },
        ],
    }
    allowlist_path = (
        scanner_fixture / "scripts" / "scanner-occurrence-allowlist.json"
    )
    allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    allowlist_path.write_text(json.dumps(bad_allowlist), encoding="utf-8")
    rc = run(scanner_fixture, scanner_input)
    assert rc == 2, (
        f"unknown token_id must fail-closed; rc={rc}"
    )


def test_canonical_scanner_rejects_malformed_json(scanner_fixture):
    """A malformed JSON allowlist fails closed.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    copy_real_allowlist(scanner_fixture)
    _copy_real_source_files(scanner_fixture)
    allowlist_path = (
        scanner_fixture / "scripts" / "scanner-occurrence-allowlist.json"
    )
    allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    allowlist_path.write_text("not json at all {{")
    rc = run(scanner_fixture, scanner_input)
    assert rc == 2, f"malformed JSON must fail closed; rc={rc}"


def test_canonical_scanner_rejects_malformed_occurrence_schema(scanner_fixture):
    """An occurrence entry missing required fields
    fails closed.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    copy_real_allowlist(scanner_fixture)
    _copy_real_source_files(scanner_fixture)
    bad = {
        "schema_version": "autocoder.scanner_occurrence_allowlist.v1",
        "token_id_registry": [
            {"token_id": "tok_x", "token_sha256_prefix": "abc",
             "note": "x"},
        ],
        "occurrences": [
            {
                "schema_version": "autocoder.scanner_occurrence_allowlist.v1",
                "path": "src/leak.txt",
                "token_id": "tok_x",
                "ordinal": 1,
                # missing line, line_sha256, purpose
            },
        ],
    }
    allowlist_path = (
        scanner_fixture / "scripts" / "scanner-occurrence-allowlist.json"
    )
    allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    allowlist_path.write_text(json.dumps(bad), encoding="utf-8")
    rc = run(scanner_fixture, scanner_input)
    assert rc == 2, (
        f"malformed occurrence schema must fail closed; rc={rc}"
    )


def test_canonical_scanner_rejects_duplicate_allowance(scanner_fixture):
    """Two entries with the same (path, token_id, ordinal)
    key fail closed.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    copy_real_allowlist(scanner_fixture)
    real = REPO_ROOT / "scripts" / "scanner-occurrence-allowlist.json"
    manifest = json.loads(real.read_text())
    tid = next(
        occ["token_id"] for occ in manifest["occurrences"]
        if occ["path"] != "scripts/scanner-occurrence-allowlist.json"
    )
    # Take ONE occurrence and forge an exact duplicate
    # (same path, same token_id, same ordinal, same
    # line_sha256).
    base = next(
        occ for occ in manifest["occurrences"]
        if occ["token_id"] == tid
        and occ["path"] != "scripts/scanner-occurrence-allowlist.json"
    )
    duplicate = dict(base)
    entries = [base, duplicate]
    bad = {
        "schema_version": "autocoder.scanner_occurrence_allowlist.v1",
        "token_id_registry": manifest["token_id_registry"],
        "occurrences": entries,
    }
    allowlist_path = (
        scanner_fixture / "scripts" / "scanner-occurrence-allowlist.json"
    )
    allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    allowlist_path.write_text(json.dumps(bad), encoding="utf-8")
    rc = run(scanner_fixture, scanner_input)
    assert rc == 2, (
        f"duplicate allowance entries must fail closed; rc={rc}"
    )


def test_canonical_scanner_rejects_unused_allowance(scanner_fixture):
    """An occurrence allowance that is not present in
    the current source fails closed.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    real = REPO_ROOT / "scripts" / "scanner-occurrence-allowlist.json"
    manifest = json.loads(real.read_text())
    # Take an occurrence from the real manifest and put
    # it into a fixture manifest. The fixture file the
    # occurrence references will NOT be created, so the
    # occurrence allowance becomes unused and stale.
    occ = next(
        occ for occ in manifest["occurrences"]
        if occ["path"] != "scripts/scanner-occurrence-allowlist.json"
        and occ["line_sha256"] is not None
    )
    # Build a fixture manifest with this single entry, but
    # don't create the corresponding source file.
    bad = {
        "schema_version": "autocoder.scanner_occurrence_allowlist.v1",
        "token_id_registry": manifest["token_id_registry"],
        "occurrences": [occ],
    }
    allowlist_path = (
        scanner_fixture / "scripts" / "scanner-occurrence-allowlist.json"
    )
    allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    allowlist_path.write_text(json.dumps(bad), encoding="utf-8")
    rc = run(scanner_fixture, scanner_input)
    assert rc == 2, (
        f"unused allowance entries must fail closed; rc={rc}"
    )



def test_canonical_scanner_rejects_line_drift(scanner_fixture):
    """Changing an approved line without updating its
    occurrence record fails (line_sha256 drift detected
    as a violation).
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    # Build a minimal manifest that documents exactly
    # one occurrence in our leak file. We do NOT use
    # copy_real_allowlist() because that brings in entries
    # for files outside the fixture and they would be
    # reported as stale.
    real = REPO_ROOT / "scripts" / "scanner-occurrence-allowlist.json"
    manifest = json.loads(real.read_text())
    # Take an occurrence from the real manifest whose
    # path is one we can create in the fixture and whose
    # token we can put on a single line.
    occ = next(
        occ for occ in manifest["occurrences"]
        if occ["path"] != "scripts/scanner-occurrence-allowlist.json"
        and occ["line_sha256"] is not None
    )
    src_path = scanner_fixture / occ["path"]
    src_path.parent.mkdir(parents=True, exist_ok=True)
    # Resolve the token from the token-id.
    token_id_to_token = {
        r["token_id"]: r.get("token")
        for r in manifest["token_id_registry"]
    }
    # The token-id maps to the token via SHA-256; recompute.
    import hashlib as _hl
    token = None
    for t in [ln.strip() for ln in scanner_input.read_text().splitlines() if ln.strip()]:
        if "tok_" + _hl.sha256(t.encode()).hexdigest()[:12] == occ["token_id"]:
            token = t
            break
    assert token is not None, (
        f"could not resolve token for token_id={occ['token_id']}"
    )
    # Write a file containing the SAME token on a line that
    # has DIFFERENT surrounding text so the line_sha256
    # drifts.
    src_path.write_text(
        "some intro\n"
        "# this line has " + token + " embedded in different text\n"
        "some outro\n"
    )
    # Build the minimal manifest and write it.
    bad = {
        "schema_version": "autocoder.scanner_occurrence_allowlist.v1",
        "token_id_registry": manifest["token_id_registry"],
        "occurrences": [{
            "schema_version": "autocoder.scanner_occurrence_allowlist.v1",
            "path": occ["path"],
            "token_id": occ["token_id"],
            "ordinal": 1,
            "line": 2,
            "line_sha256": "0" * 64,
            "purpose": "test fixture: line_sha256 mismatch forces drift",
        }],
    }
    allowlist_path = (
        scanner_fixture / "scripts" / "scanner-occurrence-allowlist.json"
    )
    allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    allowlist_path.write_text(json.dumps(bad), encoding="utf-8")
    rc = run(scanner_fixture, scanner_input)
    assert rc == 1, (
        f"line_sha256 drift must be flagged as a violation; rc={rc}"
    )



def test_canonical_scanner_utf8_encoding(scanner_fixture):
    """UTF-8 encoded files scan correctly.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    copy_real_allowlist(scanner_fixture)
    _copy_real_source_files(scanner_fixture)
    src = scanner_fixture / "src"
    src.mkdir()
    leak = src / "leak.txt"
    leak.write_text("my credential: ghp_TEST\n", encoding="utf-8")
    rc = run(scanner_fixture, scanner_input)
    assert rc == 1, f"UTF-8 credential must fail; rc={rc}"


def test_canonical_scanner_utf16le_bom(scanner_fixture):
    """UTF-16LE encoded files with a BOM are scanned
    correctly and credential-shaped content fails.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    copy_real_allowlist(scanner_fixture)
    _copy_real_source_files(scanner_fixture)
    src = scanner_fixture / "src"
    src.mkdir()
    leak = src / "leak.txt"
    import codecs
    leak.write_bytes(codecs.BOM_UTF16_LE + "my credential: ghp_TEST\n"
                     .encode("utf-16-le"))
    rc = run(scanner_fixture, scanner_input)
    assert rc == 1, f"UTF-16LE credential must fail; rc={rc}"


def test_canonical_scanner_utf16be_bom(scanner_fixture):
    """UTF-16BE encoded files with a BOM are scanned
    correctly.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    copy_real_allowlist(scanner_fixture)
    _copy_real_source_files(scanner_fixture)
    src = scanner_fixture / "src"
    src.mkdir()
    leak = src / "leak.txt"
    import codecs
    leak.write_bytes(codecs.BOM_UTF16_BE + "my credential: ghp_TEST\n"
                     .encode("utf-16-be"))
    rc = run(scanner_fixture, scanner_input)
    assert rc == 1, f"UTF-16BE credential must fail; rc={rc}"


def test_canonical_scanner_utf32le_bom(scanner_fixture):
    """UTF-32LE encoded files with a BOM are scanned
    correctly.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    copy_real_allowlist(scanner_fixture)
    _copy_real_source_files(scanner_fixture)
    src = scanner_fixture / "src"
    src.mkdir()
    leak = src / "leak.txt"
    leak.write_bytes("my credential: ghp_TEST\n".encode("utf-32"))
    rc = run(scanner_fixture, scanner_input)
    assert rc == 1, f"UTF-32LE credential must fail; rc={rc}"


def test_canonical_scanner_utf32be_bom(scanner_fixture):
    """UTF-32BE encoded files with a BOM are scanned
    correctly.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    copy_real_allowlist(scanner_fixture)
    _copy_real_source_files(scanner_fixture)
    src = scanner_fixture / "src"
    src.mkdir()
    leak = src / "leak.txt"
    import codecs
    leak.write_bytes(
        codecs.BOM_UTF32_BE + "my credential: ghp_TEST\n"
        .encode("utf-32-be")
    )
    rc = run(scanner_fixture, scanner_input)
    assert rc == 1, f"UTF-32BE credential must fail; rc={rc}"


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="uid 0 bypasses file permission checks; chmod 0o000 cannot make a file unreadable to root",
)
def test_canonical_scanner_fails_closed_on_unreadable_file(scanner_fixture):
    """An unreadable committed file fails the scanner.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    copy_real_allowlist(scanner_fixture)
    _copy_real_source_files(scanner_fixture)
    src = scanner_fixture / "src"
    src.mkdir()
    leak = src / "leak.py"
    leak.write_text("no forbidden token here\n")
    import os as _os
    _os.chmod(leak, 0o000)
    try:
        rc = run(scanner_fixture, scanner_input)
    finally:
        _os.chmod(leak, 0o644)
    assert rc == 1, (
        f"unreadable committed file must fail scanning; "
        f"rc={rc}"
    )


def test_canonical_scanner_rejects_tracked_runtime_state(tmp_path, monkeypatch):
    """A runtime-state path that is ``git add -f``'d into
    the repository is rejected by the tracked-runtime-state
    enforcement.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import (
        run, SCANNER_INPUT_REL,
        _check_no_tracked_runtime_state,
    )
    scanner_input = tmp_path / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    # Init a fake git repo to provide ``git ls-files``.
    import subprocess as _sp
    _sp.run(["git", "init", "-q", str(tmp_path)], check=True)
    _sp.run(["git", "-C", str(tmp_path), "config",
             "user.email", "x@x"], check=True)
    _sp.run(["git", "-C", str(tmp_path), "config",
             "user.name", "x"], check=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    bad = state_dir / "readiness_state.json"
    bad.write_text("{}")
    _sp.run(["git", "-C", str(tmp_path), "add", "-f", "."], check=True)
    _sp.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "i"],
            check=True)
    bad_paths = _check_no_tracked_runtime_state(tmp_path)
    assert any(str(p).endswith("readiness_state.json") for p in bad_paths), (
        f"tracked readiness_state.json must be flagged; "
        f"got: {[str(p) for p in bad_paths]}"
    )


def test_canonical_scanner_rejects_user_home_paths(scanner_fixture):
    """The configured forbidden-token rule includes the
    literal generic prefix ``/home/``. The scanner must
    reject actual Linux user-home paths.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text("/home/\n", encoding="utf-8")
    _build_minimal_manifest(scanner_fixture, scanner_input)
    src = scanner_fixture / "src"
    src.mkdir()
    leak = src / "leak.txt"
    leak.write_text(
        "see /home/alice/secret.txt\nalso /home/max/secret.txt\n"
    )
    rc = run(scanner_fixture, scanner_input)
    assert rc == 1, (
        f"/home/alice/ and /home/max/ must be rejected; rc={rc}"
    )


def test_canonical_scanner_does_not_require_self_exemption(scanner_fixture):
    """The scanner's own occurrence manifest is NOT given
    a broad self-exemption. Every documented occurrence
    in the manifest must be matched against an actual
    occurrence in the manifest file itself.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import (
        run, SCANNER_INPUT_REL, OCCURRENCE_ALLOWLIST_REL,
    )
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    _build_minimal_manifest(scanner_fixture, scanner_input)
    real = REPO_ROOT / OCCURRENCE_ALLOWLIST_REL
    allowlist = scanner_fixture / OCCURRENCE_ALLOWLIST_REL
    allowlist.parent.mkdir(parents=True, exist_ok=True)
    allowlist.write_text(real.read_text(), encoding="utf-8")
    _copy_real_source_files(scanner_fixture)
    rc = run(scanner_fixture, scanner_input)
    # Must succeed because the manifest documents its own
    # occurrences by token_id (no literal token in the
    # purpose strings).
    assert rc == 0, (
        f"manifest does not need broad self-exemption; "
        f"rc={rc}"
    )


def test_canonical_scanner_unchanged_text_accepted(scanner_fixture):
    """A file with no forbidden tokens remains clean.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    copy_real_allowlist(scanner_fixture)
    _copy_real_source_files(scanner_fixture)
    src = scanner_fixture / "src"
    src.mkdir()
    benign = src / "benign.txt"
    benign.write_text("plain ordinary prose with no user paths.\n")
    rc = run(scanner_fixture, scanner_input)
    assert rc == 0, f"benign text must scan clean; rc={rc}"


def test_canonical_scanner_rejects_duplicate_approved_line(scanner_fixture):
    """A duplicated approved credential-shaped line at a
    different location in the same file fails because
    (path, ordinal) is fresh.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    real = REPO_ROOT / "scripts" / "scanner-occurrence-allowlist.json"
    manifest = json.loads(real.read_text())
    occ = next(
        occ for occ in manifest["occurrences"]
        if occ["path"] != "scripts/scanner-occurrence-allowlist.json"
        and occ["line_sha256"] is not None
    )
    src_path = scanner_fixture / occ["path"]
    src_path.parent.mkdir(parents=True, exist_ok=True)
    # Resolve token.
    import hashlib as _hl
    token = None
    for t in [ln.strip() for ln in scanner_input.read_text().splitlines() if ln.strip()]:
        if "tok_" + _hl.sha256(t.encode()).hexdigest()[:12] == occ["token_id"]:
            token = t
            break
    assert token is not None, (
        f"could not resolve token for token_id={occ['token_id']}"
    )
    # Write a file with the SAME token TWICE on different lines.
    # The manifest documents ordinal=1 with line=2 and a specific
    # line_sha. The duplicate at line 4 has a different line_sha
    # because the surrounding text differs.
    src_path.write_text(
        "# intro\n"
        "# line 2 has " + token + " (the documented occurrence)\n"
        "# separator\n"
        "# line 4 has " + token + " (the duplicate, undriven SHA)\n"
    )
    # Manifest documents ordinal=1 at line 2 with the actual
    # line_sha of the documented line.
    documented_line_text = (
        f"# line 2 has {token} (the documented occurrence)"
    )
    documented_sha = _hl.sha256(
        documented_line_text.encode()
    ).hexdigest()
    bad = {
        "schema_version": "autocoder.scanner_occurrence_allowlist.v1",
        "token_id_registry": manifest["token_id_registry"],
        "occurrences": [{
            "schema_version": "autocoder.scanner_occurrence_allowlist.v1",
            "path": occ["path"],
            "token_id": occ["token_id"],
            "ordinal": 1,
            "line": 2,
            "line_sha256": documented_sha,
            "purpose": "test fixture: documented occurrence is "
                       "duplicated in the same file",
        }],
    }
    allowlist_path = (
        scanner_fixture / "scripts" / "scanner-occurrence-allowlist.json"
    )
    allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    allowlist_path.write_text(json.dumps(bad), encoding="utf-8")
    rc = run(scanner_fixture, scanner_input)
    assert rc == 1, (
        f"duplicate occurrence must fail scanning; rc={rc}"
    )



def test_canonical_scanner_rejects_second_occurrence_in_same_file(scanner_fixture):
    """A second occurrence of an approved token in the
    same file (new ordinal) fails.
    """
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = scanner_fixture / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text(
        "\n".join(FORBIDDEN_TOKENS_FULL) + "\n", encoding="utf-8"
    )
    real = REPO_ROOT / "scripts" / "scanner-occurrence-allowlist.json"
    manifest = json.loads(real.read_text())
    occ = next(
        occ for occ in manifest["occurrences"]
        if occ["path"] != "scripts/scanner-occurrence-allowlist.json"
        and occ["line_sha256"] is not None
    )
    src_path = scanner_fixture / occ["path"]
    src_path.parent.mkdir(parents=True, exist_ok=True)
    # Resolve token.
    import hashlib as _hl
    token = None
    for t in [ln.strip() for ln in scanner_input.read_text().splitlines() if ln.strip()]:
        if "tok_" + _hl.sha256(t.encode()).hexdigest()[:12] == occ["token_id"]:
            token = t
            break
    assert token is not None, (
        f"could not resolve token for token_id={occ['token_id']}"
    )
    # Write file with the token TWICE on different lines.
    src_path.write_text(
        "# intro\n"
        "# line 2 has " + token + " (the documented occurrence)\n"
        "# line 3 has " + token + " (the undocumented second occurrence)\n"
    )
    documented_line_text = (
        "# line 2 has " + token + " (the documented occurrence)"
    )
    documented_sha = _hl.sha256(
        documented_line_text.encode()
    ).hexdigest()
    bad = {
        "schema_version": "autocoder.scanner_occurrence_allowlist.v1",
        "token_id_registry": manifest["token_id_registry"],
        "occurrences": [{
            "schema_version": "autocoder.scanner_occurrence_allowlist.v1",
            "path": occ["path"],
            "token_id": occ["token_id"],
            "ordinal": 1,
            "line": 2,
            "line_sha256": documented_sha,
            "purpose": "test fixture: only the first occurrence is "
                       "documented",
        }],
    }
    allowlist_path = (
        scanner_fixture / "scripts" / "scanner-occurrence-allowlist.json"
    )
    allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    allowlist_path.write_text(json.dumps(bad), encoding="utf-8")
    rc = run(scanner_fixture, scanner_input)
    assert rc == 1, (
        f"second occurrence must fail scanning; rc={rc}"
    )



def test_install_md_creates_state_dir():
    """The example configuration in
    ``autocoder_supervisor/examples/aed-supervisor.example.toml``
    sets ``state_dir`` to ``/var/lib/aed-supervisor/<instance>/state``.
    INSTALL.md must create that nested ``state`` directory
    explicitly with owner ``aed-supervisor:aed-supervisor``
    and mode ``0700`` BEFORE service enablement, because
    the systemd ``StateDirectory=`` directive creates only
    the parent ``/var/lib/aed-supervisor/<instance>/``.
    The test verifies that all three required directives
    appear in the INSTALL.md installation procedure.
    """
    install_md = REPO_ROOT / "autocoder_supervisor" / "docs" / "INSTALL.md"
    example_toml = (
        REPO_ROOT / "autocoder_supervisor" / "examples"
        / "aed-supervisor.example.toml"
    )
    text = install_md.read_text()
    # Locate the example's state_dir.
    example_dir = None
    for line in example_toml.read_text().splitlines():
        s = line.strip()
        if s.startswith("state_dir"):
            example_dir = s.split("=", 1)[1].strip().strip('"').strip("'")
            break
    assert example_dir is not None, (
        "example configuration must declare a state_dir"
    )
    # Nested component is the trailing element past the
    # systemd StateDirectory parent. The example uses
    # /var/lib/aed-supervisor/<instance>/state — we
    # require the literal "state" segment inside the
    # nested install command.
    assert "/$INSTANCE/state" in text, (
        "INSTALL.md must mkdir the nested "
        "$INSTANCE/state directory before service "
        "enablement; the systemd StateDirectory= "
        "directive does not create the nested directory"
    )
    # The nested block must include owner (chown), mode
    # 0700, and a verify step.
    install_block_lower = text.lower()
    assert "chown aed-supervisor:aed-supervisor" in install_block_lower, (
        "INSTALL.md must chown the nested state_dir to "
        "aed-supervisor:aed-supervisor"
    )
    assert "chmod 0700" in install_block_lower, (
        "INSTALL.md must chmod the nested state_dir to "
        "0700"
    )
    assert "test -d /var/lib/aed-supervisor/$INSTANCE/state" in text, (
        "INSTALL.md must verify the nested state_dir "
        "exists with a test -d check"
    )

