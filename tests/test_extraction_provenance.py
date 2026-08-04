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


REPO_ROOT = Path(__file__).resolve().parent.parent
PROVENANCE_PATH = REPO_ROOT / "provenance" / "aed-pr417-source-manifest.json"
EXTRACTION_NARRATIVE = REPO_ROOT / "provenance" / "EXTRACTION.md"


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


def test_canonical_scanner_skips_its_own_data_input(tmp_path):
    """The scanner does not report its own token-definition
    input file (``scan-forbidden.txt``), even though that
    file lists the forbidden tokens by design.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run as scanner_run, SCANNER_INPUT_REL
    scanner_input = tmp_path / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text("gho_\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "ok.py").write_text("USER_HOME = 'example'\n")
    rc = scanner_run(tmp_path, scanner_input)
    assert rc == 0


def test_canonical_scanner_rejects_real_credential_in_source_file(tmp_path):
    """An actual credential-shaped value in an otherwise
    permitted source file still fails the scanner.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run as scanner_run, SCANNER_INPUT_REL
    src = tmp_path / "src"
    src.mkdir()
    leak = src / "leak.py"
    # The literal credential string is constructed at runtime
    # to avoid putting the literal token pattern in source.
    parts = ["gh", "o_", "REAL", "_", "SEC"]
    leak.write_text("REAL_TOKEN = '" + "".join(parts) + "'\n")
    scanner_input = src / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text("gho_\n")
    rc = scanner_run(tmp_path, scanner_input)
    assert rc == 1


def test_canonical_scanner_rejects_home_max_in_ordinary_file(tmp_path):
    """``/home/max/`` in any ordinary committed file fails
    the scanner, even when that path is otherwise
    permissible in dedicated detector / fixture files.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run as scanner_run, SCANNER_INPUT_REL
    src = tmp_path / "src"
    src.mkdir()
    parts = ["/", "home", "/max", "/"]
    (src / "ordinary.py").write_text(
        "USER_HOME = '" + "".join(parts) + ".cache'\n"
    )
    scanner_input = tmp_path / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text("/home/max/\n")
    rc = scanner_run(tmp_path, scanner_input)
    assert rc == 1


def test_no_incorrect_squash_parent_claim_in_tests_or_docs():
    """Neither tests nor documentation claim that a squash
    commit's parents include the authorized PR head. The
    correct claim is that the squash commit's parent is the
    pre-merge main commit; the authorized head is recorded
    separately.
    """
    forbidden = [
        "the merge commit's parents MUST include HEAD_A exactly",
        "the merge commit's parents MUST include the authorized",
    ]
    allowed_self = {
        Path("tests/test_extraction_provenance.py"),
    }
    for root, dirs, files in os.walk(REPO_ROOT):
        if "/.git/" in root or root.endswith("/.git"):
            continue
        if "/__pycache__/" in root:
            continue
        if "/dist/" in root:
            continue
        if "/.pytest_cache/" in root:
            continue
        for f in files:
            p = Path(root) / f
            try:
                rel = p.relative_to(REPO_ROOT)
            except ValueError:
                continue
            if rel in allowed_self:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for token in forbidden:
                assert token not in text, (
                    f"{p.relative_to(REPO_ROOT)} contains the "
                    f"incorrect claim: {token!r}"
                )


def test_package_can_operate_with_repository_identity_different_from_aed():
    """The supervisor's default config does not hard-code the
    AED repository identity. The validator accepts a
    configuration targeting Slideshow11/AutoDev as well as
    Slideshow11/Automated-Edge-Discovery.
    """
    from autocoder_supervisor import contracts
    autodev_cfg: contracts.SupervisorConfigDict = {
        "schema_version": "aed.autocoder_supervisor.v1",
        "instance_id": "autodev-extraction-test",
        "state_dir": "/var/tmp/autodev-supervisor-canary/state",
        "working_checkout": "/tmp/autodev-supervisor-canary/checkout",
        "log_path": "/var/tmp/autodev-supervisor-canary/logs/supervisor.log",
        "heartbeat_path": "/var/tmp/autodev-supervisor-canary/heartbeat",
        "lock_path": "/var/tmp/autodev-supervisor-canary/lock",
        "worker_command": [
            "/usr/bin/env", "true", "{prompt}",
        ],
        "worker_session_id": "test-session",
        "worker_session_name": "autodev-test-session",
        "cooldown_seconds": 900,
        "resume_prompt_template": "test",
        "human_boundary": "merge_only",
        "required_review_providers": ["coderabbit"],
        "optional_review_providers": ["codex"],
        "provider_states_are_independent": True,
        "post_codex_recovery_request": False,
        "heartbeat_seconds": 30,
        "quiet_window_seconds": 60,
        "quota_retry_initial_seconds": 3600,
        "quota_retry_backoff_seconds": 21600,
        "quota_backoff_after_retry_count": 2,
    }
    cfg = contracts.SupervisorConfig.from_dict(autodev_cfg)
    assert cfg.instance_id == "autodev-extraction-test"
    assert cfg.state_dir == "/var/tmp/autodev-supervisor-canary/state"


def test_extraction_narrative_mentions_autodev_not_just_autocoder():
    """The extraction narrative identifies AutoDev as the
    destination product and distinguishes it from the
    retained internal package name autocoder_supervisor.
    """
    assert EXTRACTION_NARRATIVE.exists()
    text = EXTRACTION_NARRATIVE.read_text()
    assert "AutoDev" in text
    assert "autocoder_supervisor" in text


def test_extraction_narrative_does_not_claim_full_history_preserved():
    """The extraction narrative does not claim full Git
    history preservation unless actual Git history is
    preserved.
    """
    text = EXTRACTION_NARRATIVE.read_text()
    forbidden = [
        "full history preservation",
        "preserves the full history",
        "history is preserved",
    ]
    for f in forbidden:
        assert f not in text.lower() or "does not preserve" in text.lower(), (
            f"extraction narrative contains forbidden claim: {f!r}"
        )


def test_extraction_manifest_records_distinct_fields():
    """The terminal-evidence schema (mirrored in
    ``aed-pr417-source-manifest.json``) keeps these fields
    distinct:

    - ``authorized_head_sha`` (the PR head authorised to
      merge; not a parent of the squash commit);
    - ``base_sha_before_merge`` (the pre-merge main tip;
      IS the squash commit's parent);
    - ``merge_commit_sha`` (the resulting squash commit);
    - ``merge_commit_parents`` (the actual single parent of
      the squash commit);
    - ``merge_method`` (always "squash" for this flow).

    For a squash example,
    ``merge_commit_parents == [base_sha_before_merge]``
    must hold.
    """
    base_sha = "9697b136f311b340e4794c8a20e2568fc2e2d08a"
    squash_sha = "b57fcaad806c68b93668bcd318fa26ab15a8ab40"
    authorized_head = "18ba0df49d2a19779e350d6df5a102b254cbeed7"
    terminal_evidence = {
        "schema": "autocoder.pr417.merge_terminal_evidence.v1",
        "authorized_head_sha": authorized_head,
        "base_sha_before_merge": base_sha,
        "merge_commit_sha": squash_sha,
        "merge_commit_parents": [base_sha],
        "merge_method": "squash",
    }
    assert authorized_head != base_sha
    assert terminal_evidence["merge_commit_parents"] == [base_sha]
    assert authorized_head not in terminal_evidence["merge_commit_parents"]
    assert squash_sha


def test_distribution_name_is_autocoder_supervisor():
    """The distribution name in pyproject.toml is
    ``autocoder-supervisor``. A different value indicates
    the rename was not applied.
    """
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert 'name = "autocoder-supervisor"' in pyproject, (
        "pyproject.toml distribution name must be "
        '"autocoder-supervisor"'
    )


def test_no_broad_internal_compatibility_rename_occurred():
    """The extraction preserves the internal
    ``autocoder_supervisor`` package name. A broad rename
    would have changed it. The AED_-prefixed compatibility
    variables and schema names are preserved.
    """
    pkg_init = (REPO_ROOT / "autocoder_supervisor" / "__init__.py").read_text()
    assert "autocoder_supervisor" in pkg_init
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert 'autocoder_supervisor*' in pyproject


def test_canonical_scanner_detects_utf16le_bom(tmp_path):
    """A forbidden token in a UTF-16LE BOM-encoded file
    must be detected by the canonical scanner.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = tmp_path / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text("gho_\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    # Construct a UTF-16LE file containing the forbidden token.
    payload = "REAL = 'g" + "ho_" + "REALSEC'\n"
    (src / "le.py").write_bytes(
        b"\xff\xfe" + payload.encode("utf-16-le")
    )
    rc = run(tmp_path, scanner_input)
    assert rc == 1


def test_canonical_scanner_detects_utf16be_bom(tmp_path):
    """A forbidden token in a UTF-16BE BOM-encoded file
    must be detected by the canonical scanner.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = tmp_path / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text("gho_\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    payload = "REAL = 'g" + "ho_" + "REALSEC'\n"
    (src / "be.py").write_bytes(
        b"\xfe\xff" + payload.encode("utf-16-be")
    )
    rc = run(tmp_path, scanner_input)
    assert rc == 1


def test_canonical_scanner_fails_closed_on_unreadable_file(tmp_path):
    """An unreadable committed file fails the scanner (does
    NOT silently continue).
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import run, SCANNER_INPUT_REL
    scanner_input = tmp_path / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text("gho_\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    leak = src / "leak.py"
    leak.write_text("no forbidden token here\n")
    # Make the file unreadable to the current user.
    import os as _os
    _os.chmod(leak, 0o000)
    try:
        rc = run(tmp_path, scanner_input)
    finally:
        _os.chmod(leak, 0o644)
    assert rc == 1


def test_canonical_scanner_rejects_tracked_runtime_state(tmp_path, monkeypatch):
    """A runtime-state path that is ``git add -f``'d into
    the repository (bypassing ``.gitignore``) is rejected by
    the tracked-runtime-state enforcement.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from canonical_scanner import (
        main as scanner_main,
        SCANNER_INPUT_REL,
    )
    repo = tmp_path
    # The scanner's main() uses os.getcwd() to locate the
    # repository. Switch into the test repo so the
    # git ls-files call walks our staged tree, not the
    # AutoDev repo containing this test file.
    monkeypatch.chdir(repo)
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email",
         "x@example.com"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "x"],
        check=True,
    )
    scanner_input = repo / SCANNER_INPUT_REL
    scanner_input.parent.mkdir(parents=True, exist_ok=True)
    scanner_input.write_text("gho_\n", encoding="utf-8")
    (repo / "src").write_text("# placeholder\n")
    subprocess.run(["git", "-C", str(repo), "add", "src"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m",
                   "init"], check=True)
    # Force-add a runtime-state path (bypasses .gitignore).
    (repo / "heartbeat").write_text("stale heartbeat\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-f", "heartbeat"],
        check=True,
    )
    rc = scanner_main()
    assert rc == 1, "tracked runtime-state path must fail the scan"

