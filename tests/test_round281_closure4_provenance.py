"""Closure IV tests:

  §2 — Provenance failure preserves attempt identity and never
        collapses into UNRELATED or QUALIFYING.
  §3 — Deleted controlled source file is detected as drift.
  §4 — Manifest enumeration fails closed on missing/malformed.
  §6 — Static fingerprint changes when acceptance runtime
        modules change.
  §7 — Static fingerprint covers the full acceptance runtime set.
  §8 — Static scope (repo/PR/branch) is frozen separately from
        dynamic run binding.
  §9 — Run binding has a strict required schema.
  §10 — Test sentinel reconciliation via migration logic.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# §2 — HeadAdvanceResult preserves attempt identity
# ---------------------------------------------------------------------------


class TestHeadAdvanceStateModel:
    def test_unknown_state_rejected(self):
        from autocoder_supervisor.provenance_maintenance import (
            HeadAdvanceResult,
        )
        with pytest.raises(ValueError):
            HeadAdvanceResult(state="NOT_A_REAL_STATE")

    def test_unrelated_has_no_attempt_identity(self):
        from autocoder_supervisor.provenance_maintenance import (
            HeadAdvanceResult,
            HEAD_ADVANCE_UNRELATED,
        )
        h = HeadAdvanceResult(state=HEAD_ADVANCE_UNRELATED)
        assert h.state == HEAD_ADVANCE_UNRELATED
        assert h.attempt_id is None
        assert h.claim_id is None
        assert h.result_contract_id is None
        assert h.blocks_qualifying is True
        assert h.is_worker_push is False
        assert h.is_provenance_verified is False

    def test_provenance_verified_preserves_all_identity(self):
        from autocoder_supervisor.provenance_maintenance import (
            HeadAdvanceResult,
            HEAD_ADVANCE_WORKER_PROVENANCE_VERIFIED,
        )
        h = HeadAdvanceResult(
            state=HEAD_ADVANCE_WORKER_PROVENANCE_VERIFIED,
            attempt_id="att-1",
            claim_id="claim-1",
            result_contract_id="rc-1",
            produced_sha="a" * 40,
            pushed_sha="b" * 40,
            origin_head_verified=True,
            github_head_verified=True,
        )
        assert h.attempt_id == "att-1"
        assert h.claim_id == "claim-1"
        assert h.result_contract_id == "rc-1"
        assert h.produced_sha == "a" * 40
        assert h.pushed_sha == "b" * 40
        assert h.origin_head_verified is True
        assert h.github_head_verified is True
        assert h.blocks_qualifying is False
        assert h.is_worker_push is True
        assert h.is_provenance_verified is True

    def test_provenance_blocked_preserves_attempt_identity(self):
        """Closure IV §2 critical invariant: a verified worker
        push whose provenance check FAILED must preserve
        attempt_id, claim_id, result_contract_id, produced_sha,
        pushed_sha, origin/github verification. The state
        encodes BLOCKED; the identity is NOT erased.
        """
        from autocoder_supervisor.provenance_maintenance import (
            HeadAdvanceResult,
            HEAD_ADVANCE_WORKER_PROVENANCE_BLOCKED,
        )
        h = HeadAdvanceResult(
            state=HEAD_ADVANCE_WORKER_PROVENANCE_BLOCKED,
            attempt_id="att-1",
            claim_id="claim-1",
            result_contract_id="rc-1",
            produced_sha="a" * 40,
            pushed_sha="b" * 40,
            origin_head_verified=True,
            github_head_verified=True,
            provenance_status="ERROR",
            provenance_error="drift detection failed",
        )
        assert h.attempt_id == "att-1"
        assert h.claim_id == "claim-1"
        assert h.result_contract_id == "rc-1"
        assert h.produced_sha == "a" * 40
        assert h.pushed_sha == "b" * 40
        assert h.blocks_qualifying is True
        assert h.is_worker_push is True
        assert h.is_provenance_verified is False

    def test_push_invalid_preserves_attempt_identity(self):
        from autocoder_supervisor.provenance_maintenance import (
            HeadAdvanceResult,
            HEAD_ADVANCE_WORKER_PUSH_INVALID,
        )
        h = HeadAdvanceResult(
            state=HEAD_ADVANCE_WORKER_PUSH_INVALID,
            attempt_id="att-2",
            claim_id="claim-2",
            produced_sha="c" * 40,
            pushed_sha="d" * 40,
        )
        assert h.attempt_id == "att-2"
        assert h.blocks_qualifying is True
        assert h.is_provenance_verified is False

    def test_blocks_qualifying_states(self):
        from autocoder_supervisor.provenance_maintenance import (
            HEAD_ADVANCE_STATES_THAT_BLOCK_QUALIFYING,
            HEAD_ADVANCE_UNRELATED,
            HEAD_ADVANCE_WORKER_PROVENANCE_BLOCKED,
            HEAD_ADVANCE_WORKER_PUSH_INVALID,
            HEAD_ADVANCE_WORKER_PROVENANCE_VERIFIED,
        )
        # Three states block qualifying.
        assert (
            HEAD_ADVANCE_UNRELATED in HEAD_ADVANCE_STATES_THAT_BLOCK_QUALIFYING
        )
        assert (
            HEAD_ADVANCE_WORKER_PROVENANCE_BLOCKED
            in HEAD_ADVANCE_STATES_THAT_BLOCK_QUALIFYING
        )
        assert (
            HEAD_ADVANCE_WORKER_PUSH_INVALID
            in HEAD_ADVANCE_STATES_THAT_BLOCK_QUALIFYING
        )
        # One state does NOT block.
        assert (
            HEAD_ADVANCE_WORKER_PROVENANCE_VERIFIED
            not in HEAD_ADVANCE_STATES_THAT_BLOCK_QUALIFYING
        )


# ---------------------------------------------------------------------------
# §3 — Deleted controlled source file is drift
# ---------------------------------------------------------------------------


def _git_init(tmp_path: Path) -> Path:
    repo = tmp_path / "src_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True,
    )
    return repo


def _commit(repo: Path, files: dict) -> str:
    for rel, content in files.items():
        full = repo / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "fixture"], cwd=repo, check=True,
    )
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()


def _write_manifest(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": "autocoder.provenance.v1",
        "files": [
            {
                "destination_path": r["destination_path"],
                "destination_sha256": r["destination_sha256"],
                "destination_size_bytes": r.get("destination_size_bytes", 0),
                "source_path": r.get("source_path", ""),
                "source_sha256": r.get("source_sha256", ""),
                "transformation_classification": "path_only",
            }
            for r in records
        ],
    }
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


class TestControlledFileDeletion:
    def test_delete_supervisor_py_records_drift(self, tmp_path):
        from autocoder_supervisor.provenance_maintenance import (
            find_drift_at_head,
        )
        repo = _git_init(tmp_path)
        head = _commit(repo, {
            "autocoder_supervisor/supervisor.py": "# v1\n",
            "provenance/AUTOCODER_SOURCE_COMPLETENESS.json": '{"files": []}\n',
        })
        import hashlib
        sha = hashlib.sha256(
            subprocess.check_output(
                ["git", "show", f"{head}:autocoder_supervisor/supervisor.py"],
                cwd=repo,
            )
        ).hexdigest()
        manifest = tmp_path / "m.json"
        _write_manifest(
            manifest,
            [{
                "destination_path": "autocoder_supervisor/supervisor.py",
                "destination_sha256": sha,
            }],
        )
        # At initial head, no drift.
        drifts = find_drift_at_head(
            repo_root=repo, head_sha=head, manifest_paths=[manifest]
        )
        assert drifts == []
        # Now commit a deletion of the controlled file.
        head2 = _commit(repo, {
            "autocoder_supervisor/supervisor.py": None,  # None means: do NOT create
            "provenance/AUTOCODER_SOURCE_COMPLETENESS.json": '{"files": []}\n',
        }) if False else None
        # Actually do deletion:
        (repo / "autocoder_supervisor").mkdir(exist_ok=True)
        # Remove the file by writing an empty manifest commit only:
        subprocess.run(["git", "rm", "autocoder_supervisor/supervisor.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "delete"], cwd=repo, check=True)
        head2 = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip()
        # Find drift: deletion IS drift.
        drifts = find_drift_at_head(
            repo_root=repo, head_sha=head2, manifest_paths=[manifest]
        )
        assert len(drifts) == 1
        assert drifts[0]["drift_kind"] == "DELETION"
        assert drifts[0]["destination"] == "autocoder_supervisor/supervisor.py"
        assert drifts[0]["expected_sha256"] == sha
        assert drifts[0]["actual_sha256"] == ""

    def test_delete_worker_attempt_py_records_drift(self, tmp_path):
        from autocoder_supervisor.provenance_maintenance import (
            find_drift_at_head,
        )
        repo = _git_init(tmp_path)
        head = _commit(repo, {
            "autocoder_orchestration/worker_attempt.py": "# v1\n",
        })
        import hashlib
        sha = hashlib.sha256(
            subprocess.check_output(
                ["git", "show", f"{head}:autocoder_orchestration/worker_attempt.py"],
                cwd=repo,
            )
        ).hexdigest()
        manifest = tmp_path / "m.json"
        _write_manifest(
            manifest,
            [{
                "destination_path": "autocoder_orchestration/worker_attempt.py",
                "destination_sha256": sha,
            }],
        )
        # Delete the file.
        subprocess.run(["git", "rm", "autocoder_orchestration/worker_attempt.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "delete"], cwd=repo, check=True)
        head2 = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip()
        drifts = find_drift_at_head(
            repo_root=repo, head_sha=head2, manifest_paths=[manifest]
        )
        assert len(drifts) == 1
        assert drifts[0]["drift_kind"] == "DELETION"

    def test_delete_directive_bridge_py_records_drift(self, tmp_path):
        from autocoder_supervisor.provenance_maintenance import (
            find_drift_at_head,
        )
        repo = _git_init(tmp_path)
        head = _commit(repo, {
            "autocoder_supervisor/directive_bridge.py": "# v1\n",
        })
        import hashlib
        sha = hashlib.sha256(
            subprocess.check_output(
                ["git", "show", f"{head}:autocoder_supervisor/directive_bridge.py"],
                cwd=repo,
            )
        ).hexdigest()
        manifest = tmp_path / "m.json"
        _write_manifest(
            manifest,
            [{
                "destination_path": "autocoder_supervisor/directive_bridge.py",
                "destination_sha256": sha,
            }],
        )
        subprocess.run(["git", "rm", "autocoder_supervisor/directive_bridge.py"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "delete"], cwd=repo, check=True)
        head2 = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip()
        drifts = find_drift_at_head(
            repo_root=repo, head_sha=head2, manifest_paths=[manifest]
        )
        assert len(drifts) == 1
        assert drifts[0]["drift_kind"] == "DELETION"

    def test_invalid_head_sha_raises(self, tmp_path):
        from autocoder_supervisor.provenance_maintenance import (
            find_drift_at_head,
            ProvenanceCheckError,
        )
        repo = _git_init(tmp_path)
        head = _commit(repo, {"x.py": "x\n"})
        manifest = tmp_path / "m.json"
        _write_manifest(manifest, [
            {"destination_path": "x.py", "destination_sha256": "a" * 64}
        ])
        with pytest.raises(ProvenanceCheckError):
            find_drift_at_head(
                repo_root=repo,
                head_sha="not-a-sha",
                manifest_paths=[manifest],
            )

    def test_missing_manifest_raises(self, tmp_path):
        from autocoder_supervisor.provenance_maintenance import (
            find_drift_at_head,
            ProvenanceCheckError,
        )
        repo = _git_init(tmp_path)
        _commit(repo, {"x.py": "x\n"})
        missing = tmp_path / "no_such_manifest.json"
        with pytest.raises(ProvenanceCheckError):
            find_drift_at_head(
                repo_root=repo,
                head_sha=subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=repo, text=True
                ).strip(),
                manifest_paths=[missing],
            )

    def test_malformed_manifest_raises(self, tmp_path):
        from autocoder_supervisor.provenance_maintenance import (
            find_drift_at_head,
            ProvenanceCheckError,
        )
        repo = _git_init(tmp_path)
        _commit(repo, {"x.py": "x\n"})
        bad = tmp_path / "bad.json"
        bad.write_text("{ this is not valid json")
        with pytest.raises(ProvenanceCheckError):
            find_drift_at_head(
                repo_root=repo,
                head_sha=subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=repo, text=True
                ).strip(),
                manifest_paths=[bad],
            )


# ---------------------------------------------------------------------------
# §4 — Manifest enumeration fails closed
# ---------------------------------------------------------------------------


class TestEnumerationFailClosed:
    def test_missing_manifest_raises(self, tmp_path):
        from autocoder_supervisor.provenance_maintenance import (
            enumerate_controlled_destinations_strict,
            ManifestEnumerationError,
        )
        mp = tmp_path / "m.json"
        _write_manifest(mp, [
            {"destination_path": "x.py", "destination_sha256": "a" * 64}
        ])
        with pytest.raises(ManifestEnumerationError):
            enumerate_controlled_destinations_strict([mp, tmp_path / "no.json"])

    def test_malformed_manifest_raises(self, tmp_path):
        from autocoder_supervisor.provenance_maintenance import (
            enumerate_controlled_destinations_strict,
            ManifestEnumerationError,
        )
        mp = tmp_path / "m.json"
        _write_manifest(mp, [
            {"destination_path": "x.py", "destination_sha256": "a" * 64}
        ])
        bad = tmp_path / "bad.json"
        bad.write_text("{ malformed")
        with pytest.raises(ManifestEnumerationError):
            enumerate_controlled_destinations_strict([mp, bad])

    def test_empty_manifests_raise(self, tmp_path):
        from autocoder_supervisor.provenance_maintenance import (
            enumerate_controlled_destinations_strict,
            ManifestEnumerationError,
        )
        mp = tmp_path / "m.json"
        _write_manifest(mp, [])
        with pytest.raises(ManifestEnumerationError):
            enumerate_controlled_destinations_strict([mp])

    def test_diagnostic_helper_silently_skips(self, tmp_path):
        """The diagnostic helper is for inspection only and
        silently skips unreadable manifests. The production
        acceptance path must NOT use this helper."""
        from autocoder_supervisor.provenance_maintenance import (
            enumerate_controlled_destinations,
        )
        mp = tmp_path / "m.json"
        _write_manifest(mp, [
            {"destination_path": "x.py", "destination_sha256": "a" * 64}
        ])
        # Missing manifest is silently skipped.
        result = enumerate_controlled_destinations(
            [mp, tmp_path / "no.json"]
        )
        assert "x.py" in result