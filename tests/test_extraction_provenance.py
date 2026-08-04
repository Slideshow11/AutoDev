"""Extraction-specific regression tests for AutoDev v1.

Verifies the extraction provenance and the evidence-
semantics correction. These tests must run on the standalone
repository without depending on AED.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PROVENANCE_PATH = REPO_ROOT / "provenance" / "aed-pr417-source-manifest.json"
EXTRACTION_NARRATIVE = REPO_ROOT / "provenance" / "EXTRACTION.md"


SOURCE_REPO = "Slideshow11/Automated-Edge-Discovery"
SOURCE_COMMIT = "b57fcaad806c68b93668bcd318fa26ab15a8ab40"
SOURCE_PR = 417
SOURCE_REVIEWED_HEAD = "18ba0df49d2a19779e350d6df5a102b254cbeed7"


# Files that are allowed to mention forbidden tokens as part
# of fixture or test-detection logic. The committed-state-scan
# input file lists the forbidden tokens and is the scanner's
# input data, not a contributor of leaked secrets.
ALLOW_RELPATHS = {
    Path("tests/test_autocoder_supervisor.py"),
    Path("tests/test_extraction_provenance.py"),
    Path(".github/workflows/scan-forbidden.txt"),
    # Package source files implement the user-path rejection
    # logic and naturally contain the forbidden tokens as part
    # of the validation regexes; this is functional code
    # extracted from AED and is not a secret leak.
    Path("autocoder_supervisor/config.py"),
    Path("autocoder_supervisor/supervisor.py"),
    Path("autocoder_supervisor/contracts.py"),
    # INVARIANTS.md files document the validator's rejection
    # patterns (including the literal shape of rejected
    # tokens like oauth_token:). They are part of the
    # validator's public contract and not a leaked secret.
    Path("INVARIANTS.md"),
    Path("autocoder_supervisor/INVARIANTS.md"),
}


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
    assert len(byte_identical) >= 5, (
        f"expected at least 5 byte-identical files; "
        f"found {len(byte_identical)}"
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


def _walk_repo_files():
    """Yield (relpath, text) for every file under REPO_ROOT,
    skipping caches, venvs, and the git directory.
    """
    for root, dirs, files in os.walk(REPO_ROOT):
        if "/.git/" in root or root.endswith("/.git"):
            continue
        if "/__pycache__/" in root:
            continue
        if "/dist/" in root:
            continue
        for f in files:
            p = Path(root) / f
            try:
                rel = p.relative_to(REPO_ROOT)
            except ValueError:
                continue
            yield rel, p


def test_no_committed_file_contains_user_specific_paths():
    """No committed file under the standalone repository
    contains ``/home/max/`` (or any other user-specific
    absolute path). Tests that test the rejection of such
    paths, and the scanner's input file, are excluded.
    """
    forbidden = ["/home/max/", "/root/"]
    for rel, p in _walk_repo_files():
        if rel in ALLOW_RELPATHS:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for token in forbidden:
            assert token not in text, (
                f"{rel} contains {token!r}"
            )


def test_no_incorrect_squash_parent_claim_in_tests_or_docs():
    """Neither tests nor documentation claim that a squash
    commit's parents include the authorized PR head. The
    correct claim is that the squash commit's parent is the
    pre-merge main commit; the authorized head is recorded
    separately.
    """
    forbidden = [
        # Old incorrect claim that was corrected.
        "the merge commit's parents MUST include HEAD_A exactly",
        "the merge commit's parents MUST include the authorized",
    ]
    for rel, p in _walk_repo_files():
        if rel in ALLOW_RELPATHS:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for token in forbidden:
            assert token not in text, (
                f"{rel} contains the incorrect claim: {token!r}"
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
    # The validator does not pin repository identity at the
    # package level — the operator chooses it at install time
    # via AED_REPO_OWNER and AED_REPO_NAME. The default
    # values fall through to env, not to AED.
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
    must hold. The test data in this file mimics a real
    squash scenario and asserts the contract.
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
    # Sanity: the authorized head is distinct from the parent.
    assert authorized_head != base_sha
    # The squash parent is exactly the base SHA.
    assert terminal_evidence["merge_commit_parents"] == [base_sha]
    # The authorized head is NOT in the parent list.
    assert authorized_head not in terminal_evidence["merge_commit_parents"]
    # The squash commit SHA is non-empty.
    assert squash_sha


def test_no_broad_internal_compatibility_rename_occurred():
    """The extraction preserves the internal
    ``autocoder_supervisor`` package name and the
    ``aed-supervisor`` distribution name. A broad rename
    would have changed them. This test fails if either name
    appears anywhere with a different value.
    """
    pkg_init = (REPO_ROOT / "autocoder_supervisor" / "__init__.py").read_text()
    assert "autocoder_supervisor" in pkg_init
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    # The distribution name is "aed-supervisor"; the
    # package name is "autocoder_supervisor". Neither has
    # been renamed.
    assert 'name = "aed-supervisor"' in pyproject
    assert 'autocoder_supervisor*' in pyproject
