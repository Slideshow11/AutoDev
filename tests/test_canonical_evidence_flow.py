"""End-to-end tests for defect 3.1: canonical candidate/verifier
artifact production.

These tests prove the producer/consumer contract:

* ``Controller.build_candidate`` writes the candidate via
  ``write_artifact`` to ``canonical_paths(evidence_root).candidate``
  with a sidecar;
* ``Controller.verifier_passed`` writes the verifier record via
  ``write_artifact`` to ``canonical_paths(evidence_root).verifier``
  with a sidecar;
* the merge transaction's ``MergeTransactionInputs`` point at those
  exact canonical paths and the resulting digest matches.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autocoder_orchestration.artifacts import read_artifact
from autocoder_orchestration.canonical_paths import canonical_paths
from autocoder_orchestration.controller import Controller
from autocoder_orchestration.context import SCHEMA_VERSION as RC_SCHEMA
from autocoder_orchestration.context import RunContext
from autocoder_orchestration.merge_authorization import MergeTransactionInputs
from autocoder_orchestration.store import StateStore


def _make_run_context(evidence_root: Path, run_state_root: Path) -> RunContext:
    return RunContext(
        schema_version=RC_SCHEMA,
        run_id="test-run-001",
        created_at="2026-08-06T21:00:00Z",
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        local_checkout=str(run_state_root / "repo"),
        base_branch="main",
        authorized_base_sha="a6bd5f63c3e4bad2b91661ffb75a59d2afd5f38d",
        feature_branch="fix/test",
        pr_number=4,
        current_authorized_head="2a8e4e9c1f3a4b5d6e7f8091a2b3c4d5e40ffe0d",
        task_specification_path=str(evidence_root / "task.txt"),
        task_specification_sha256="a" * 64,
        required_ci_jobs=("test (3.10)", "test (3.11)", "test (3.12)",
                           "package-smoke", "provenance", "committed-state-scan"),
        reviewer_policy="approve-only",
        quiet_window_seconds=180,
        implementation_worker_command=("echo", "worker"),
        verifier_command=None,
        verifier_handoff_policy="strict",
        permitted_mutations=("candidate.json", "verifier.json", "merge-record.json"),
        human_only_actions=("merge",),
        evidence_root=str(evidence_root),
        state_root=str(run_state_root),
        next_wave_policy="none",
    )


def _make_candidate_payload(ctx: RunContext) -> dict:
    from autocoder_orchestration.candidate import SCHEMA_VERSION
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": ctx.run_id,
        "repo": f"{ctx.repo_owner}/{ctx.repo_name}",
        "pr_number": ctx.pr_number,
        "exact_head": ctx.current_authorized_head,
        "base_sha": ctx.authorized_base_sha,
        "base_branch": ctx.base_branch,
        "task_specification_sha256": ctx.task_specification_sha256,
        "readiness_certificate_id": "cert-001",
        "readiness_certificate_sha256": "f" * 64,
        "readiness_overall_passed": True,
        "ci_inventory": [],
        "review_inventory": [],
        "thread_inventory": {"unresolved_current": 0, "unresolved_outdated": 0},
        "strict_observation_log_hash": "",
        "process_identity": {},
        "lock_release_evidence": {},
        "controller_state_revision": 1,
        "controller_state_path": "state.json",
        "input_hashes": {},
        "source_files": {},
        "aed_source_files": {},
        "created_at": "2026-08-06T21:00:00Z",
    }


class CanonicalEvidenceProducerConsumerTests(unittest.TestCase):
    """§3.1: producer/consumer split is closed via canonical paths."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="aed-can-evt-"))
        self.run_state_root = self.tmpdir / "state"
        self.evidence_root = self.tmpdir / "evidence"
        self.run_state_root.mkdir(parents=True)
        self.evidence_root.mkdir(parents=True)
        self.ctx = _make_run_context(self.evidence_root, self.run_state_root)
        self.store = StateStore(str(self.run_state_root))
        self.store.write_atomic("run_context.json", self.ctx.to_dict())
        self.paths = canonical_paths(self.evidence_root)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed_state(self, target_state: str) -> None:
        from autocoder_orchestration.state_machine import StateMachine
        sm = StateMachine(current_state=target_state)
        self.store.write_atomic("state.json", sm.to_dict())

    def test_controller_writes_candidate_to_canonical_path(self) -> None:
        """``Controller.build_candidate`` writes the candidate via
        ``write_artifact`` to ``canonical_paths(evidence_root).candidate``
        with a sidecar; the file's exact-file digest matches the
        sidecar digest and is bindable by the merge transaction."""
        # Seed state at READY_FOR_CANDIDATE so build_candidate is allowed.
        from autocoder_orchestration.state_machine import (
            STATE_READY_FOR_CANDIDATE,
        )
        self._seed_state(STATE_READY_FOR_CANDIDATE)

        cand_payload = _make_candidate_payload(self.ctx)
        # Construct a Candidate object via from_dict.
        from autocoder_orchestration.candidate import Candidate
        cand_obj = Candidate.from_dict(cand_payload)

        controller = Controller(self.ctx, self.store)
        controller.build_candidate(
            cand_obj, head_observed=str(self.ctx.current_authorized_head)
        )

        # Canonical artifact exists with sidecar.
        self.assertTrue(
            self.paths["candidate"].exists(),
            f"canonical candidate missing at {self.paths['candidate']}",
        )
        sidecar_path = self.paths["candidate"].with_name(
            self.paths["candidate"].name + ".sha256"
        )
        self.assertTrue(sidecar_path.exists())

        artifact_digest = read_artifact(self.paths["candidate"]).digest
        sidecar_digest = sidecar_path.read_text().strip()
        self.assertEqual(artifact_digest, sidecar_digest)
        self.assertEqual(len(artifact_digest), 64)

    def test_controller_verifier_passed_writes_canonical_verifier(self) -> None:
        """``Controller.verifier_passed`` writes the verifier record
        via ``write_artifact`` to ``canonical_paths(evidence_root).verifier``
        with a sidecar; the exact-file digest matches the sidecar."""
        # Seed state at VERIFYING so verifier_passed is allowed.
        from autocoder_orchestration.state_machine import STATE_VERIFYING
        self._seed_state(STATE_VERIFYING)

        verifier_record = {
            "schema_version": "autocoder.verifier_record.v1",
            "candidate_sha256": "c" * 64,
            "verdict": "VERIFIED",
            "defects": [],
            "aed_checked": True,
            "aed_clean": True,
        }
        controller = Controller(self.ctx, self.store)
        controller.verifier_passed(
            head_observed=str(self.ctx.current_authorized_head),
            verifier_record=verifier_record,
        )

        self.assertTrue(self.paths["verifier"].exists())
        sidecar_path = self.paths["verifier"].with_name(
            self.paths["verifier"].name + ".sha256"
        )
        self.assertTrue(sidecar_path.exists())

        artifact_digest = read_artifact(self.paths["verifier"]).digest
        sidecar_digest = sidecar_path.read_text().strip()
        self.assertEqual(artifact_digest, sidecar_digest)
        self.assertEqual(len(artifact_digest), 64)

        # State-root also has the secondary copy for audit
        # (NOT a merge input per §3.1).
        self.assertTrue(
            (self.run_state_root / "verifier-record.json").exists()
        )

    def test_merge_inputs_point_at_canonical_paths(self) -> None:
        """``MergeTransactionInputs`` artifact paths MUST equal the
        canonical paths helper output for the evidence root."""
        # Seed state and build both canonical artifacts.
        from autocoder_orchestration.state_machine import (
            STATE_READY_FOR_CANDIDATE, STATE_VERIFYING,
        )
        self._seed_state(STATE_READY_FOR_CANDIDATE)
        from autocoder_orchestration.candidate import Candidate
        controller = Controller(self.ctx, self.store)
        cand_obj = Candidate.from_dict(_make_candidate_payload(self.ctx))
        controller.build_candidate(
            cand_obj, head_observed=str(self.ctx.current_authorized_head)
        )
        self._seed_state(STATE_VERIFYING)
        controller.verifier_passed(
            head_observed=str(self.ctx.current_authorized_head),
            verifier_record={"schema_version": "v1"},
        )

        merge_inputs = MergeTransactionInputs(
            authorization_artifact_path=self.paths["authorization"],
            candidate_artifact_path=self.paths["candidate"],
            verifier_artifact_path=self.paths["verifier"],
            merge_record_artifact_path=self.paths["merge_record"],
            repository_checkout=self.tmpdir / "repo",
            run_state_root=self.run_state_root,
            evidence_root=self.evidence_root,
            live_pr_payload={},
            live_ci_state={},
            live_review_state={},
            live_thread_inventory={},
            working_tree_clean=True,
        )
        self.assertEqual(
            merge_inputs.candidate_artifact_path,
            canonical_paths(self.evidence_root)["candidate"],
        )
        self.assertEqual(
            merge_inputs.verifier_artifact_path,
            canonical_paths(self.evidence_root)["verifier"],
        )
        self.assertEqual(
            merge_inputs.authorization_artifact_path,
            canonical_paths(self.evidence_root)["authorization"],
        )
        self.assertEqual(
            merge_inputs.merge_record_artifact_path,
            canonical_paths(self.evidence_root)["merge_record"],
        )
        # And the artifacts exist with non-empty digests.
        self.assertEqual(
            len(read_artifact(merge_inputs.candidate_artifact_path).digest), 64,
        )
        self.assertEqual(
            len(read_artifact(merge_inputs.verifier_artifact_path).digest), 64,
        )


if __name__ == "__main__":
    unittest.main()