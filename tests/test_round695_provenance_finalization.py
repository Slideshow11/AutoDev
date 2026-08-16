"""Round-695: deterministic worker provenance finalization.

The pre-canary proof that a source-changing worker can
deterministically produce a push-ready commit whose
manifest hash matches the on-disk bytes when the worker
runs ``provenance_finalize_if_needed`` before commit, AND
that the supervisor's
``_validate_provenance_consistency`` gate rejects a
commit that did not run the finalizer.

Directive §7 required test cases:

A. CONTROLLED SOURCE CHANGE - worker repair modifies a
   manifest-controlled destination; running the helper
   produces a push-ready commit satisfying size+sha
   invariants.

B. UNCONTROLLED SOURCE CHANGE - worker changes a file
   outside the controlled set; the helper is a no-op.

C. NO-OP WORKER - all findings terminal no-change; no
   source edit; the helper is a no-op.

D. FINALIZER FAILURE - controlled source changed but
   ``provenance_finalize`` fails; the worker must not
   claim successful REPAIR_PUSHED.

E. LATE CONTROLLED EDIT - controlled file changed after
   an earlier finalization; the stale finalization
   cannot satisfy the push-ready condition.

F. REAL CURRENT FAILURE - the exact failure class
   currently visible on PR #5 (autocoder_orchestration/cli.py
   manifest drift) is repaired by the helper.

This module tests BOTH the worker-side helper and the
supervisor-side validator by importing the production
modules and exercising them against a real on-disk git
repository (a tmp_path) with a real manifest fixture.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _make_repo(tmp_path: Path) -> Path:
    """Create a tmp repo with a controlled-file fixture."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # init git
    subprocess.run(
        ["git", "-C", str(repo), "init", "--quiet", "-b", "main"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@x"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "test"],
        check=True,
        capture_output=True,
    )
    # Create a controlled file (mimics supervisor.py)
    (repo / "aed_supervisor").mkdir()
    (repo / "aed_supervisor" / "supervisor.py").write_text(
        "ORIGINAL_CONTENT\n"
    )
    # Create an uncontrolled file (mimics a test file)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_extras.py").write_text("# test\n")
    # Copy the production scripts directory to the test repo
    # so the helper's invocation of scripts/provenance_audit.py
    # finds a real script.
    scripts_src = Path(__file__).resolve().parent.parent / "scripts"
    if scripts_src.is_dir():
        import shutil
        shutil.copytree(str(scripts_src), str(repo / "scripts"))
    # Create the manifest directory and a properly-shaped initial audit
    (repo / "provenance").mkdir()
    initial_audit = {
        "schema_version": "autocoder.source_completeness.v1",
        "extracted_manifest_match": {
            "manifest_files_count": 0,
            "manifest_source_files_count": 0,
            "manifest_standalone_additions_count": 0,
            "manifest_records": [],
        },
        "metrics": {
            "extracted_manifest_match": {
                "manifest_files_count": 0,
                "manifest_source_files_count": 0,
                "manifest_standalone_additions_count": 0,
            },
            "total_source_paths_in_manifest": 0,
            "manifest_standalone_additions": 0,
        },
    }
    with open(repo / "provenance" / "AUTOCODER_SOURCE_COMPLETENESS.json", "w") as f:
        json.dump(initial_audit, f)
    # Create a manifest that records ONLY the controlled file.
    # Note: tests/test_extras.py is intentionally NOT in the manifest
    # because it is the unmodified uncontrolled file used by test B.
    manifest_data = {
        "schema_version": "autocoder.extraction_manifest.v1",
        "generated_at": "2026-08-15T00:00:00Z",
        "files": [
            {
                "destination_path": "aed_supervisor/supervisor.py",
                "source_path": "scripts/local/aed_supervisor/supervisor.py",
                "destination_sha256": subprocess.run(
                    ["git", "-C", str(repo), "hash-object",
                     "aed_supervisor/supervisor.py"],
                    capture_output=True, text=True,
                ).stdout.strip(),
                "destination_size_bytes": os.path.getsize(
                    repo / "aed_supervisor/supervisor.py"
                ),
                "transformation_classification": "byte_identical",
            },
        ],
    }
    with open(repo / "provenance" / "aed-pr417-source-manifest.json", "w") as f:
        json.dump(manifest_data, f)
    # Stage everything
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--quiet", "-m", "init"],
        check=True,
        capture_output=True,
    )
    prelaunch = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return repo


def _current_manifest_index(repo: Path) -> dict:
    """Return the destination -> (sha256, size_bytes) manifest index."""
    manifest_path = repo / "provenance" / "aed-pr417-source-manifest.json"
    data = json.loads(manifest_path.read_text())
    out = {}
    for entry in data.get("files", []):
        dp = entry.get("destination_path")
        sha = entry.get("destination_sha256")
        sz = entry.get("destination_size_bytes")
        if isinstance(dp, str) and isinstance(sha, str):
            out[dp] = (sha, sz)
    return out


@pytest.fixture
def repo(tmp_path):
    return _make_repo(tmp_path)


# ---------------------------------------------------------------------------
# WORKER-SIDE HELPER TESTS
# ---------------------------------------------------------------------------


def test_a_controlled_source_change_helper_runs_finalizer(repo):
    """Case A: worker modifies a manifest-controlled destination.
    The helper detects the controlled-file change and runs
    the canonical finalization."""
    from autocoder_supervisor.provenance_maintenance import (
        run_provenance_finalize_if_needed,
    )
    # Modify the controlled file
    (repo / "aed_supervisor" / "supervisor.py").write_text(
        "MODIFIED_CONTENT\n"
    )
    prelaunch = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    out = run_provenance_finalize_if_needed(
        repo_root=repo,
        prelaunch_head=prelaunch,
    )
    assert out["ran"] is True
    assert "aed_supervisor/supervisor.py" in out["controlled_files"]
    # The manifest hash must now match the on-disk bytes
    manifest_index = _current_manifest_index(repo)
    sha, size = manifest_index["aed_supervisor/supervisor.py"]
    actual = (
        hashlib.sha256(
            (repo / "aed_supervisor/supervisor.py").read_bytes()
        ).hexdigest()
        if False
        else None
    )
    import hashlib
    actual = hashlib.sha256(
        (repo / "aed_supervisor/supervisor.py").read_bytes()
    ).hexdigest()
    assert sha == actual
    actual_size = os.path.getsize(repo / "aed_supervisor" / "supervisor.py")
    assert size == actual_size


def test_b_uncontrolled_source_change_helper_is_noop(repo):
    """Case B: worker changes a file outside the controlled set.
    The helper returns ran=False without invoking finalization."""
    from autocoder_supervisor.provenance_maintenance import (
        run_provenance_finalize_if_needed,
    )
    (repo / "tests" / "test_extras.py").write_text("# MODIFIED\n")
    prelaunch = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    out = run_provenance_finalize_if_needed(
        repo_root=repo,
        prelaunch_head=prelaunch,
    )
    assert out["ran"] is False
    assert out["reason"] == "no controlled-file changes"
    assert "tests/test_extras.py" in out["changed_files"]
    assert out["controlled_files"] == []


def test_c_no_change_helper_is_noop(repo):
    """Case C: no source change. The helper is a no-op."""
    from autocoder_supervisor.provenance_maintenance import (
        run_provenance_finalize_if_needed,
    )
    prelaunch = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    out = run_provenance_finalize_if_needed(
        repo_root=repo,
        prelaunch_head=prelaunch,
    )
    assert out["ran"] is False
    assert out["changed_files"] == []


def test_e_late_controlled_edit_requires_another_run(repo):
    """Case E: a controlled file changed AFTER the helper ran.
    A subsequent helper call must regenerate the manifest."""
    from autocoder_supervisor.provenance_maintenance import (
        run_provenance_finalize_if_needed,
    )
    (repo / "aed_supervisor" / "supervisor.py").write_text(
        "FIRST_MODIFY\n"
    )
    prelaunch = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    out1 = run_provenance_finalize_if_needed(
        repo_root=repo,
        prelaunch_head=prelaunch,
    )
    assert out1["ran"] is True
    # Now edit the controlled file AGAIN
    (repo / "aed_supervisor" / "supervisor.py").write_text(
        "SECOND_MODIFY\n"
    )
    # After the second edit, the manifest is stale. Running
    # the helper again must regenerate the manifest for the
    # new bytes.
    import hashlib
    actual = hashlib.sha256(
        (repo / "aed_supervisor/supervisor.py").read_bytes()
    ).hexdigest()
    manifest_index = _current_manifest_index(repo)
    # Internally the helper checks WORKING-TREE bytes, so the
    # second call must regenerate.
    out2 = run_provenance_finalize_if_needed(
        repo_root=repo,
        prelaunch_head=prelaunch,
    )
    assert out2["ran"] is True
    manifest_index_2 = _current_manifest_index(repo)
    assert manifest_index_2["aed_supervisor/supervisor.py"][0] == actual


def test_d_finalizer_failure_raises(repo):
    """Case D: provenance_finalize fails when the manifest is
    missing. The helper must raise ProvenanceFinalizeError
    so the worker can emit WORKER_RESULT_INVALID."""
    from autocoder_supervisor.provenance_maintenance import (
        run_provenance_finalize_if_needed,
        ProvenanceFinalizeError,
    )
    # Delete the manifest to make finalize fail
    (repo / "provenance" / "aed-pr417-source-manifest.json").unlink()
    (repo / "aed_supervisor" / "supervisor.py").write_text("MODIFIED\n")
    prelaunch = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    with pytest.raises(ProvenanceFinalizeError):
        run_provenance_finalize_if_needed(
            repo_root=repo,
            prelaunch_head=prelaunch,
        )


def test_f_real_current_failure_class(repo):
    """Case F: the exact failure class currently visible on PR #5
    (autocoder_orchestration/cli.py manifest drift) is
    repaired by the helper."""
    from autocoder_supervisor.provenance_maintenance import (
        run_provenance_finalize_if_needed,
    )
    # Replace the manifest with one that records ONLY
    # aed_supervisor/supervisor.py, then edit that file.
    manifest_path = repo / "provenance" / "aed-pr417-source-manifest.json"
    # Initial recorded hash
    import hashlib
    original_sha = hashlib.sha256(
        (repo / "aed_supervisor/supervisor.py").read_bytes()
    ).hexdigest()
    original_size = os.path.getsize(
        repo / "aed_supervisor/supervisor.py"
    )
    # Pre-modify the manifest to a STALE hash (simulate the
    # exact production failure: another worker edited the
    # controlled file but never regenerated the manifest)
    manifest_data = json.loads(manifest_path.read_text())
    for entry in manifest_data.get("files", []):
        if entry["destination_path"] == "aed_supervisor/supervisor.py":
            entry["destination_sha256"] = "0" * 64
            entry["destination_size_bytes"] = 0
    with open(manifest_path, "w") as f:
        json.dump(manifest_data, f)
    # Now edit the controlled file
    (repo / "aed_supervisor" / "supervisor.py").write_text(
        "REPAIRED_CONTENT\n"
    )
    prelaunch = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    out = run_provenance_finalize_if_needed(
        repo_root=repo,
        prelaunch_head=prelaunch,
    )
    assert out["ran"] is True
    # After the helper, the manifest MUST match the on-disk bytes
    actual_sha = hashlib.sha256(
        (repo / "aed_supervisor/supervisor.py").read_bytes()
    ).hexdigest()
    actual_size = os.path.getsize(
        repo / "aed_supervisor/supervisor.py"
    )
    manifest_index = _current_manifest_index(repo)
    sha, size = manifest_index["aed_supervisor/supervisor.py"]
    assert sha == actual_sha
    assert size == actual_size
    # The orphan
    assert sha != "0" * 64
    assert size != 0


# ---------------------------------------------------------------------------
# SUPERVISOR-SIDE VALIDATOR TESTS
# ---------------------------------------------------------------------------


def test_supervisor_validator_passes_when_consistent(repo):
    """The validator returns (True, []) when the manifest
    matches the on-disk bytes at pushed_head."""
    from autocoder_supervisor.supervisor import (
        _validate_provenance_consistency,
    )
    # Modify the controlled file AND regenerate the manifest
    # (mimicking what the worker-side helper does pre-commit).
    (repo / "aed_supervisor" / "supervisor.py").write_text(
        "MODIFIED\n"
    )
    import hashlib
    new_sha = hashlib.sha256(
        (repo / "aed_supervisor/supervisor.py").read_bytes()
    ).hexdigest()
    new_size = os.path.getsize(repo / "aed_supervisor/supervisor.py")
    manifest_path = repo / "provenance" / "aed-pr417-source-manifest.json"
    manifest_data = json.loads(manifest_path.read_text())
    for entry in manifest_data.get("files", []):
        if entry["destination_path"] == "aed_supervisor/supervisor.py":
            entry["destination_sha256"] = new_sha
            entry["destination_size_bytes"] = new_size
    with open(manifest_path, "w") as f:
        json.dump(manifest_data, f)
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--quiet", "-m", "test"],
        check=True,
        capture_output=True,
    )
    pushed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    prelaunch = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD~1"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    ok, errors = _validate_provenance_consistency(
        prelaunch_head=prelaunch,
        pushed_head=pushed,
        repo_root=repo,
    )
    assert ok is True, errors
    assert errors == []


def test_supervisor_validator_fails_when_manifest_stale(repo):
    """The validator returns (False, [...]) when the controlled
    file changed but the manifest was not updated in the same
    commit (the exact production failure)."""
    from autocoder_supervisor.supervisor import (
        _validate_provenance_consistency,
    )
    # Edit the controlled file but DO NOT update the manifest
    (repo / "aed_supervisor" / "supervisor.py").write_text(
        "MODIFIED\n"
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "aed_supervisor/supervisor.py"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--quiet", "-m", "test"],
        check=True,
        capture_output=True,
    )
    pushed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    prelaunch = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD~1"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    ok, errors = _validate_provenance_consistency(
        prelaunch_head=prelaunch,
        pushed_head=pushed,
        repo_root=repo,
    )
    assert ok is False
    assert len(errors) > 0
    # The error must mention the manifest wasn't updated
    assert any("manifest" in e or "controlled" in e for e in errors)


def test_supervisor_validator_passes_when_no_controlled_change(repo):
    """The validator returns (True, []) when no controlled
    destination changed (only uncontrolled files)."""
    from autocoder_supervisor.supervisor import (
        _validate_provenance_consistency,
    )
    # Edit only the uncontrolled file
    (repo / "tests" / "test_extras.py").write_text(
        "MODIFIED\n"
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "tests/test_extras.py"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--quiet", "-m", "test"],
        check=True,
        capture_output=True,
    )
    pushed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    prelaunch = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD~1"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    ok, errors = _validate_provenance_consistency(
        prelaunch_head=prelaunch,
        pushed_head=pushed,
        repo_root=repo,
    )
    assert ok is True
    assert errors == []
