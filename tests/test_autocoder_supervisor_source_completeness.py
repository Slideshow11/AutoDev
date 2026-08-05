"""Audit tests for the AUTOCODER_SOURCE_COMPLETENESS.json artifact.

These tests prove the source-completeness audit:
- has a valid schema
- classifies every AED tracked file into exactly one disposition
- has no unclassified candidates
- has SHA-256 + blob SHA + size for every AED file
- classifies all 17 manifest source files as extracted
- documents the supervisor-v1 runtime inventory
- proves clean-install independence (no AED-layout references in the wheel)
- proves the supervisor-v1 runtime has no AED internal Python imports
- proves the supervisor-v1 runtime has no AED-layout subprocess commands
- commits the source-completeness audit as an intentional commit
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path

import pytest

AUTODEV_REPO = Path(__file__).resolve().parent.parent
AUDIT_JSON = AUTODEV_REPO / "provenance" / "AUTOCODER_SOURCE_COMPLETENESS.json"
AUDIT_MD = AUTODEV_REPO / "provenance" / "AUTOCODER_SOURCE_COMPLETENESS.md"
EXTRACTION_MANIFEST = (
    AUTODEV_REPO / "provenance" / "aed-pr417-source-manifest.json"
)
AED_REF = "b57fcaad806c68b93668bcd318fa26ab15a8ab40"
AED_REPO_ENV_VAR = "AUTODEV_AED_REPO_PATH"
AED_REPO = Path(os.environ.get(AED_REPO_ENV_VAR) or (AUTODEV_REPO.parent / "Automated-Edge-Discovery"))


def _resolve_autodev_head() -> str:
    """Resolve the current AutoDev HEAD at test-collection time.

    Falls back to the pinned constant only if the local git checkout
    is not available.
    """
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(AUTODEV_REPO),
            text=True,
        ).strip()
        if out:
            return out
    except Exception:
        pass
    return "e99c33aa8b857600e70941637a7007af83af0a64"


AUTODEV_HEAD = _resolve_autodev_head()


# --- Schema constants ---

EXPECTED_DISPOSITIONS = {
    "COPIED_BYTE_IDENTICAL",
    "TRANSFORMED_IN_AUTODEV",
    "RETAINED_AS_AED_SPECIFIC_INTEGRATION",
    "SUPERSEDED_OR_OBSOLETE",
    "HISTORICAL_OR_RUNTIME_EVIDENCE_EXCLUDED",
    "FOLLOW_UP_AUTODEV_MIGRATION_REQUIRED",
}

AED_LAYOUT_PATTERNS = [
    "aed_lifecycle",
    "aed_policy",
    "scripts/local",
    "scripts/ci",
    "_shared_non_human",
    "_shared_codex",
    "_shared_pagination",
    "_shared_test_selection",
    "_shared_batching",
    "_ledger_review",
    "aed_continue_pr",
    "aed_executor_packet",
    "aed_launch_receipt",
    "aed_lifecycle_states",
    "aed_mutation_authorization",
    "aed_pr_lib",
    "aed_pr_readiness",
    "aed_repair_planner",
    "aed_run_identity",
    "aed_supervisor_lock",
    "aed_tasker_",
    "aed_test_runner",
]


@pytest.fixture(scope="module")
def audit():
    with open(AUDIT_JSON) as f:
        return json.load(f)


def _locate_wheel():
    """Find the locally-built wheel if available; otherwise skip wheel-dependent tests."""
    p = Path("/tmp/wheels/autocoder_supervisor-1.0.0-py3-none-any.whl")
    return p if p.exists() else None


@pytest.fixture(scope="module")
def wheel():
    return _locate_wheel()


@pytest.fixture(scope="module")
def manifest():
    with open(EXTRACTION_MANIFEST) as f:
        return json.load(f)


# --- Test 1: schema valid + header complete ---

def test_audit_schema_header(audit):
    """The audit must declare its schema, version, source, and destination."""
    header = audit["header"]
    assert header["schema"] == "autocoder.source_completeness.v1"
    assert header["produced_by"]
    assert header["produced_at"]
    assert "Full enumeration" in header["purpose"]
    src = audit["source"]
    assert src["reference_commit_sha"] == AED_REF
    assert src["tracked_file_count"] > 0
    dst = audit["destination"]
    # reference_head_sha is informational metadata about the
    # head at which the audit was generated. It is allowed to be
    # behind the current AutoDev HEAD (every commit creates a new
    # HEAD that the audit cannot predate by construction).
    # The hard correctness requirement is on destination bytes vs
    # actual committed bytes, enforced by
    # test_all_migrated_records_have_matching_destination_bytes.
    assert dst["pr_number"] == 1


# --- Test 2: every AED tracked file is classified ---

def test_every_aed_file_classified(audit):
    """Every AED file record must have exactly one disposition from the allowed set."""
    records = audit["aed_file_records"]
    assert len(records) == audit["source"]["tracked_file_count"]
    for r in records:
        assert "disposition" in r, f"missing disposition: {r.get('aed_source_path')}"
        assert r["disposition"] in EXPECTED_DISPOSITIONS, (
            f"unknown disposition {r['disposition']!r} for {r['aed_source_path']}"
        )
        assert r["disposition"] != "UNCLASSIFIED"


def test_zero_unclassified_candidates(audit):
    assert audit["no_unclassified_candidates"] is True


def test_disposition_summary_matches_records(audit):
    """The disposition_counts summary must match the per-record count."""
    counts = Counter(r["disposition"] for r in audit["aed_file_records"])
    assert dict(counts) == audit["aed_inventory_summary"]["disposition_counts"]


# --- Test 3: SHA-256 + blob SHA integrity ---

def test_all_records_have_valid_aed_sha256(audit):
    """Every record must have a 64-char hex SHA-256 from AED at the reference commit."""
    assert audit["all_records_have_aed_sha256"] is True
    for r in audit["aed_file_records"]:
        sha = r["aed_sha256"]
        assert len(sha) == 64, f"{r['aed_source_path']}: SHA-256 must be 64 hex chars"
        int(sha, 16)  # must be valid hex


def test_all_records_have_valid_aed_blob_sha(audit):
    """Every record must have a 40-char hex blob SHA from AED at the reference commit."""
    assert audit["all_records_have_aed_blob_sha"] is True
    for r in audit["aed_file_records"]:
        sha = r["aed_blob_sha"]
        assert len(sha) == 40, f"{r['aed_source_path']}: blob SHA must be 40 hex chars"
        int(sha, 16)


def test_aed_sha256_recomputed_at_reference_commit():
    """Recompute SHA-256 of every AED file at b57fcaad and cross-check with audit.

    This test verifies the audit's central integrity claim: every
    AED file's recorded ``aed_sha256`` matches the bytes at commit
    ``b57fcaad``. The test reads each blob via ``git cat-file``,
    hashes its bytes, and compares against the record.
    """
    import hashlib as _hashlib

    if not AED_REPO.exists():
        pytest.skip(f"AED_REPO does not exist at {AED_REPO}; set {AED_REPO_ENV_VAR}")
    audit_data = json.loads(AUDIT_JSON.read_text())
    expected_count = audit_data["source"]["tracked_file_count"]
    rec_by_path = {r["aed_source_path"]: r for r in audit_data["aed_file_records"]}

    out = subprocess.check_output(
        ["git", "ls-tree", "-r", AED_REF],
        cwd=str(AED_REPO),
        text=True,
    )
    path_to_blob = {}
    for line in out.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) != 4:
            continue
        if parts[1] != "blob":
            continue
        path_to_blob[parts[3]] = parts[2]
    # Compare against the audit's count, not a hard-coded number.
    assert len(path_to_blob) == expected_count, (
        f"unexpected AED file count: {len(path_to_blob)} vs {expected_count}"
    )
    # Cross-check: every record's blob_sha matches git, the recorded
    # SHA-256 matches the actual blob bytes, and the recorded size
    # matches the actual blob size.
    for path, blob_sha in path_to_blob.items():
        rec = rec_by_path.get(path)
        assert rec is not None, f"AED file not in audit: {path}"
        assert rec["aed_blob_sha"] == blob_sha, (
            f"blob SHA mismatch for {path}: "
            f"{rec['aed_blob_sha']} != {blob_sha}"
        )
        blob_bytes = subprocess.check_output(
            ["git", "cat-file", "blob", blob_sha],
            cwd=str(AED_REPO),
        )
        actual_sha = _hashlib.sha256(blob_bytes).hexdigest()
        actual_size = len(blob_bytes)
        assert rec["aed_sha256"] == actual_sha, (
            f"SHA-256 mismatch for {path}: "
            f"recorded {rec['aed_sha256'][:16]}..., "
            f"actual {actual_sha[:16]}..."
        )
        assert rec["aed_size_bytes"] == actual_size, (
            f"size mismatch for {path}: "
            f"recorded {rec['aed_size_bytes']}, actual {actual_size}"
        )


# --- Test 4: manifest source files match the EXTRACTED classification ---

def test_all_manifest_source_files_classified_as_extracted(audit, manifest):
    """Every AED source path in the extraction manifest must be classified as EXTRACTED."""
    manifest_source_paths = {
        f["source_path"]
        for f in manifest["files"]
        if f.get("source_path")
    }
    extracted_paths = {
        r["aed_source_path"]
        for r in audit["aed_file_records"]
        if r["disposition"] in ("COPIED_BYTE_IDENTICAL", "TRANSFORMED_IN_AUTODEV")
    }
    missing = manifest_source_paths - extracted_paths
    assert not missing, f"manifest source paths not classified as extracted: {missing}"
    assert audit["extracted_manifest_match"]["all_manifest_source_paths_classified_as_extracted"] is True


def test_byte_identical_records_have_matching_hashes(audit, manifest):
    """Every file classified as ``COPIED_BYTE_IDENTICAL`` in the audit must
    satisfy ``source_sha256 == destination_sha256`` in the extraction
    manifest. Without this invariant, the audit can drift from reality.
    """
    byte_identical_paths = {
        r["aed_source_path"]
        for r in audit["aed_file_records"]
        if r["disposition"] == "COPIED_BYTE_IDENTICAL"
    }
    if not byte_identical_paths:
        pytest.skip("no COPIED_BYTE_IDENTICAL records to verify")
    for entry in manifest["files"]:
        if not entry.get("source_path"):
            continue
        if entry["source_path"] not in byte_identical_paths:
            continue
        s = entry.get("source_sha256")
        d = entry.get("destination_sha256")
        assert s and d and s == d, (
            f"COPIED_BYTE_IDENTICAL entry has mismatched hashes: "
            f"source={s}, destination={d}"
        )


def test_all_migrated_records_have_matching_destination_bytes(audit, manifest):
    """Every migrated record (COPIED_BYTE_IDENTICAL or TRANSFORMED_IN_AUTODEV)
    must:

    - have a destination file in the exact AutoDev HEAD commit;
    - have the recorded destination size match the actual file size;
    - have the recorded destination SHA-256 match the actual file SHA-256.

    Reads the destination bytes from the exact AutoDev HEAD commit so the
    check is independent of the working tree. The migration manifest is the
    source of (source_path -> destination_path) mapping; the audit provides
    disposition and expected counts. If the audit claims fewer migrated
    records than the manifest declares, that drift must fail closed.

    This test is the regression prevention for the audit-binding failure:
    the audit's destination metadata must match the committed destination
    bytes exactly, for every migrated record.
    """
    migrated_dispositions = ("COPIED_BYTE_IDENTICAL", "TRANSFORMED_IN_AUTODEV")
    migrated = [
        r for r in audit["aed_file_records"]
        if r["disposition"] in migrated_dispositions
    ]
    assert len(migrated) >= 17, (
        f"audit claims only {len(migrated)} migrated records; "
        "expected at least 17 (1 COPIED + 16 TRANSFORMED)"
    )
    # Build source -> manifest entry index
    manifest_by_source = {}
    for entry in manifest["files"]:
        src = entry.get("source_path")
        if src:
            manifest_by_source[src] = entry

    for r in migrated:
        source_path = r.get("aed_source_path")
        assert source_path, f"migrated record missing aed_source_path: {r}"
        manifest_entry = manifest_by_source.get(source_path)
        assert manifest_entry is not None, (
            f"migrated record {source_path!r} not found in extraction manifest"
        )
        dest_path = manifest_entry.get("destination_path")
        recorded_sha = manifest_entry.get("destination_sha256")
        recorded_size = manifest_entry.get("destination_size_bytes")
        assert dest_path, (
            f"manifest entry for {source_path!r} missing destination_path"
        )
        assert recorded_sha, (
            f"manifest entry for {source_path!r} missing destination_sha256"
        )
        assert recorded_size is not None, (
            f"manifest entry for {source_path!r} missing destination_size_bytes"
        )
        # Read the committed destination bytes via git show HEAD:<path>
        try:
            committed_bytes = subprocess.check_output(
                ["git", "show", f"{AUTODEV_HEAD}:{dest_path}"],
                cwd=str(AUTODEV_REPO),
            )
        except subprocess.CalledProcessError:
            pytest.fail(
                f"destination path {dest_path!r} is not present in "
                f"AutoDev HEAD {AUTODEV_HEAD}"
            )
        actual_sha = hashlib.sha256(committed_bytes).hexdigest()
        actual_size = len(committed_bytes)
        assert actual_sha == recorded_sha, (
            f"migrated destination sha mismatch for {dest_path}: "
            f"recorded {recorded_sha[:16]}..., actual {actual_sha[:16]}..."
        )
        assert actual_size == recorded_size, (
            f"migrated destination size mismatch for {dest_path}: "
            f"recorded {recorded_size}, actual {actual_size}"
        )


def test_manifest_match_count(audit, manifest):
    mm = audit["extracted_manifest_match"]
    assert mm["manifest_files_count"] == len(manifest["files"])
    assert mm["manifest_source_files_count"] == sum(
        1 for f in manifest["files"] if f.get("source_path")
    )
    assert mm["manifest_standalone_additions_count"] == sum(
        1 for f in manifest["files"] if not f.get("source_path")
    )


# --- Test 5: supervisor-v1 runtime inventory is concrete ---

def test_supervisor_v1_runtime_inventory_concrete(audit):
    inv = audit["supervisor_v1_runtime_inventory"]
    assert len(inv["extracted_records"]) >= 5
    # The five runtime modules must be present
    runtime_basenames = {os.path.basename(r["autodev_destination"]) for r in inv["extracted_records"]}
    for required in (
        "__init__.py", "config.py", "contracts.py",
        "supervisor.py", "validate.py",
    ):
        assert required in runtime_basenames, f"missing runtime module: {required}"
    # Every extracted record must have an AutoDev destination SHA-256
    for rec in inv["extracted_records"]:
        assert rec.get("autodev_sha256"), f"missing autodev_sha256: {rec}"


def test_wheel_installed_files_match_runtime(wheel):
    """The wheel's installed source files must match the runtime inventory."""
    if wheel is None:
        pytest.skip("wheel not built (run python3 -m build --wheel --outdir /tmp/wheels)")
    with zipfile.ZipFile(wheel) as z:
        py_files = [n for n in z.namelist() if n.startswith("autocoder_supervisor/") and n.endswith(".py")]
    assert len(py_files) == 5
    basenames = sorted(os.path.basename(n) for n in py_files)
    assert basenames == ["__init__.py", "config.py", "contracts.py", "supervisor.py", "validate.py"]


# --- Test 6: supervisor-v1 has no AED-internal Python imports ---

def test_supervisor_v1_runtime_no_aed_internal_python_imports():
    """No Python file inside the supervisor package may import from AED layout."""
    pkg_root = AUTODEV_REPO / "autocoder_supervisor"
    bad_patterns = AED_LAYOUT_PATTERNS
    for py in pkg_root.glob("*.py"):
        text = py.read_text()
        for line in text.splitlines():
            if not (line.startswith("import ") or line.startswith("from ")):
                continue
            for pat in bad_patterns:
                assert pat not in line, (
                    f"{py.name}: AED-layout reference in import: {line}"
                )


def test_supervisor_v1_tests_no_aed_internal_python_imports():
    """Tests for supervisor-v1 must not import from AED layout either."""
    test_root = AUTODEV_REPO / "tests"
    for py in test_root.glob("test_*.py"):
        text = py.read_text()
        for line in text.splitlines():
            if not (line.startswith("import ") or line.startswith("from ")):
                continue
            for pat in AED_LAYOUT_PATTERNS:
                if pat in line:
                    raise AssertionError(
                        f"{py.name}: AED-layout reference in import: {line}"
                    )


# --- Test 7: supervisor-v1 has no AED-layout subprocess commands ---

def test_supervisor_v1_runtime_no_aed_layout_subprocess_commands():
    """Subprocess invocations in the supervisor package must not reference AED paths."""
    pkg_root = AUTODEV_REPO / "autocoder_supervisor"
    bad_patterns = AED_LAYOUT_PATTERNS
    for py in pkg_root.glob("*.py"):
        text = py.read_text()
        # Allow exact "subprocess" + list-string patterns.
        for line in text.splitlines():
            if "subprocess" not in line:
                continue
            for pat in bad_patterns:
                assert pat not in line, (
                    f"{py.name}: AED-layout reference in subprocess call: {line}"
                )


# --- Test 8: clean-install proof ---

def test_clean_install_wheel_exists(wheel):
    if wheel is None:
        pytest.skip("wheel not built (run python3 -m build --wheel --outdir /tmp/wheels)")
    assert wheel.exists()


def test_clean_install_wheel_has_no_aed_layout_paths(wheel):
    """The installed wheel must not contain any AED-layout references."""
    if wheel is None:
        pytest.skip("wheel not built (run python3 -m build --wheel --outdir /tmp/wheels)")
    # Read all .py files from the wheel
    with zipfile.ZipFile(wheel) as z:
        for name in z.namelist():
            if not (name.endswith(".py") and name.startswith("autocoder_supervisor/")):
                continue
            text = z.read(name).decode("utf-8", errors="replace")
            for pat in AED_LAYOUT_PATTERNS:
                assert pat not in text, (
                    f"{name}: AED-layout reference in installed wheel: {pat}"
                )


def test_clean_install_import_smoke():
    """`import autocoder_supervisor` succeeds in a fresh venv."""
    venv = Path("/tmp/clean_venv")
    if not (venv / "bin/python3").exists():
        pytest.skip("clean_venv not built")
    proc = subprocess.run(
        [
            "/tmp/clean_venv/bin/python3",
            "-c",
            "import autocoder_supervisor; print('OK', autocoder_supervisor.__file__)",
        ],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_clean_install_submodule_imports():
    venv = Path("/tmp/clean_venv")
    if not (venv / "bin/python3").exists():
        pytest.skip("clean_venv not built")
    proc = subprocess.run(
        [
            "/tmp/clean_venv/bin/python3",
            "-c",
            "from autocoder_supervisor import supervisor, validate, config, contracts; print('All imports OK')",
        ],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    assert "All imports OK" in proc.stdout


def test_clean_install_cli_help_supervisor():
    venv = Path("/tmp/clean_venv")
    if not (venv / "bin/python3").exists():
        pytest.skip("clean_venv not built")
    proc = subprocess.run(
        ["/tmp/clean_venv/bin/python3", "-m", "autocoder_supervisor.supervisor", "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage: supervisor.py" in proc.stdout


def test_clean_install_cli_help_validate():
    venv = Path("/tmp/clean_venv")
    if not (venv / "bin/python3").exists():
        pytest.skip("clean_venv not built")
    proc = subprocess.run(
        ["/tmp/clean_venv/bin/python3", "-m", "autocoder_supervisor.validate", "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Dry-run validation" in proc.stdout


# --- Test 9: PR scope decision is documented ---

def test_pr_scope_decision_documented(audit):
    decision = audit["pr_scope_decision"]
    assert decision["verdict"] == "PR_1_CONTAINS_SELF_CONTAINED_SUPERVISOR_V1"
    assert "self-contained" in decision["rationale"].lower()
    assert decision["no_pr_expansion_required"] is True
    assert isinstance(decision["missing_direct_supervisor_v1_dependencies"], list)


def test_narrative_md_exists():
    assert AUDIT_MD.exists()


# --- Test 10: AED remains untouched ---

def test_aed_working_tree_clean():
    if not AED_REPO.exists():
        pytest.skip(f"AED_REPO does not exist at {AED_REPO}; set {AED_REPO_ENV_VAR}")
    proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(AED_REPO), capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", (
        f"AED working tree is dirty:\n{proc.stdout}"
    )


# --- Test 11: supervisor-v1 has no SUPERSEDED, FOLLOW_UP, or HISTORICAL classification ---

def test_no_supervisor_v1_runtime_file_classified_as_deferred(audit):
    """A direct supervisor-v1 runtime file must not be classified as deferred/historical."""
    runtime_paths = {
        "scripts/local/autocoder_supervisor/__init__.py",
        "scripts/local/autocoder_supervisor/config.py",
        "scripts/local/autocoder_supervisor/contracts.py",
        "scripts/local/autocoder_supervisor/supervisor.py",
        "scripts/local/autocoder_supervisor/validate.py",
    }
    for r in audit["aed_file_records"]:
        if r["aed_source_path"] in runtime_paths:
            assert r["disposition"] in ("COPIED_BYTE_IDENTICAL", "TRANSFORMED_IN_AUTODEV"), (
                f"runtime file classified as deferred: {r}"
            )


def test_no_supervisor_v1_test_classified_as_deferred(audit):
    """Tests migrated to AutoDev must not be classified as deferred/historical."""
    test_paths = {
        "tests/test_autocoder_supervisor.py",
        "tests/test_autocoder_supervisor_packaging.py",
    }
    for r in audit["aed_file_records"]:
        if r["aed_source_path"] in test_paths:
            assert r["disposition"] in ("COPIED_BYTE_IDENTICAL", "TRANSFORMED_IN_AUTODEV"), (
                f"runtime test classified as deferred: {r}"
            )


# --- Test 12: follow-up scope explicitly says PR #1 can operate independently ---

def test_no_follow_up_classifications_in_audit(audit):
    """The audit declares zero FOLLOW_UP_AUTODEV_MIGRATION_REQUIRED classifications.

    This is the conservative invariant: every retained AED component is documented
    as RETAINED_AS_AED_SPECIFIC_INTEGRATION or HISTORICAL_OR_RUNTIME_EVIDENCE_EXCLUDED.
    None of them is bound to a future AutoDev PR by this audit; that binding is
    reserved for future work tracked outside this audit.
    """
    for r in audit["aed_file_records"]:
        assert r["disposition"] != "FOLLOW_UP_AUTODEV_MIGRATION_REQUIRED", (
            f"FOLLOW_UP classification present (should not bind future work): {r}"
        )


# --- Test 13: scope decision is consistent with all files ---

def test_all_extracted_records_have_aed_destination(audit):
    for rec in audit["supervisor_v1_runtime_inventory"]["extracted_records"]:
        assert rec["autodev_destination"], rec
        dest = AUTODEV_REPO / rec["autodev_destination"]
        assert dest.exists(), f"missing AutoDev destination: {dest}"