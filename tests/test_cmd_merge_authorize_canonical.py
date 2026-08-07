"""Regression tests for defect 3.3: cmd_merge_authorize must read
the canonical evidence-root artifacts, not the state-root copies.

The merge authorization binds exact-file digests into
``MergeAuthorization``. Those digests are the values the merge
transaction later re-verifies. If the authorization reads the
state-root ``candidate.json`` / ``verifier-record.json`` copies
while the merge transaction reads the canonical evidence-root
``candidate.json`` / ``verifier.json``, the two paths can disagree
and an authorization can be bound to artifacts the merge
transaction will refuse to consume.

These tests prove:

* Test A — canonical artifacts present and valid; state-root
  copies contain different data; cmd_merge_authorize MUST bind
  the canonical evidence-root digests. This is the bug-detector
  property: with the broken implementation the digests would
  derive from the state-root copies and diverge from the canonical
  evidence-root digests the merge transaction will re-read.
* Test B — state-root copies exist; canonical candidate is
  missing; authorization MUST fail.
* Test C — state-root copies exist; canonical verifier is missing;
  authorization MUST fail.
* Test D — canonical candidate or verifier is altered after
  sidecar generation; authorization MUST fail.

Each test exercises the real ``cmd_merge_authorize`` function
with a minimal seeded run context and the canonical
``read_artifact`` / ``write_artifact`` writers.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autocoder_orchestration import cli as cli_module
from autocoder_orchestration.artifacts import read_artifact, write_artifact
from autocoder_orchestration.canonical_paths import canonical_paths
from autocoder_orchestration.candidate import SCHEMA_VERSION as CAND_SCHEMA
from autocoder_orchestration.candidate import Candidate
from autocoder_orchestration.context import SCHEMA_VERSION as RC_SCHEMA
from autocoder_orchestration.context import RunContext
from autocoder_orchestration.store import StateStore


def _make_run_context(evidence_root: Path, run_state_root: Path) -> RunContext:
    return RunContext(
        schema_version=RC_SCHEMA,
        run_id="test-cmd-merge-authorize-canonical",
        created_at="2026-08-07T10:00:00Z",
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
                          "package-smoke", "provenance",
                          "committed-state-scan"),
        reviewer_policy="approve-only",
        quiet_window_seconds=180,
        implementation_worker_command=("echo", "worker"),
        verifier_command=None,
        verifier_handoff_policy="strict",
        permitted_mutations=("candidate.json", "verifier.json",
                             "merge-record.json"),
        human_only_actions=("merge",),
        evidence_root=str(evidence_root),
        state_root=str(run_state_root),
        next_wave_policy="none",
    )


def _seed_state(store: StateStore, target_state: str) -> None:
    from autocoder_orchestration.state_machine import StateMachine
    sm = StateMachine(current_state=target_state)
    store.write_atomic("state.json", sm.to_dict())


def _make_candidate_payload(ctx: RunContext) -> dict:
    return {
        "schema_version": CAND_SCHEMA,
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
        "thread_inventory": {"unresolved_current": 0,
                             "unresolved_outdated": 0},
        "strict_observation_log_hash": "",
        "process_identity": {},
        "lock_release_evidence": {},
        "controller_state_revision": 1,
        "controller_state_path": "state.json",
        "input_hashes": {},
        "source_files": {},
        "aed_source_files": {},
        "created_at": "2026-08-07T10:00:00Z",
    }


def _make_verifier_payload(ctx: RunContext) -> dict:
    return {
        "schema_version": "autocoder.verifier_record.v1",
        "candidate_sha256": "c" * 64,
        "verdict": "VERIFIED",
        "defects": [],
        "aed_checked": True,
        "aed_clean": True,
    }


def _make_args(ctx: RunContext, run_state_root: Path,
               evidence_root: Path, **overrides) -> object:
    """Build an args Namespace-like object compatible with
    ``cmd_merge_authorize``. argparse.Namespace requires attribute
    access for every kwarg; missing keys raise AttributeError.
    """
    base = {
        "run_id": ctx.run_id,
        "state_root": str(run_state_root),
        "evidence_root": str(evidence_root),
        "json": True,
        "pr_number": ctx.pr_number,
        "authorized_head": ctx.current_authorized_head,
        "method": "squash",
        "keep_branch": False,
        "author": "Slideshow11",
        "notes": "",
    }
    base.update(overrides)
    return type("Args", (), base)()


class CmdMergeAuthorizeCanonicalEvidenceTests(unittest.TestCase):
    """Section 3: cmd_merge_authorize binds canonical evidence-root
    digests. State-root copies are non-authoritative audit
    observables and MUST NOT influence the authorization."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="aed-auth-can-"))
        self.run_state_root = self.tmpdir / "state"
        self.evidence_root = self.tmpdir / "evidence"
        self.run_state_root.mkdir(parents=True)
        self.evidence_root.mkdir(parents=True)
        self.ctx = _make_run_context(self.evidence_root, self.run_state_root)
        self.store = StateStore(str(self.run_state_root))
        self.store.write_atomic("run_context.json", self.ctx.to_dict())
        # Seed state at AWAITING_MERGE_AUTHORIZATION so
        # cmd_merge_authorize's downstream controller call succeeds
        # if digests bind correctly.
        from autocoder_orchestration.state_machine import (
            STATE_AWAITING_MERGE_AUTHORIZATION,
        )
        _seed_state(self.store, STATE_AWAITING_MERGE_AUTHORIZATION)
        self.paths = canonical_paths(self.evidence_root)
        for p in self.paths.values():
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- Test A: state-root copies MUST NOT influence authorization

    def test_a_state_root_copies_do_not_override_canonical_authorization(
        self,
    ) -> None:
        """Canonical candidate + verifier exist with sidecars.
        State-root copies contain *different* data (different
        ``exact_head``, different verdict). cmd_merge_authorize
        MUST bind the canonical evidence-root digests, NEVER the
        state-root digests.

        Bug-detector property: with the broken implementation the
        authorization would bind the state-root candidate and
        verifier digests. Those digests would not match the
        canonical evidence-root digests; the merge transaction
        would later refuse to consume the authorization.
        """
        # Canonical evidence-root artifacts.
        cand_obj = Candidate.from_dict(_make_candidate_payload(self.ctx))
        write_artifact(self.paths["candidate"], cand_obj.to_dict())
        verifier_canonical = _make_verifier_payload(self.ctx)
        write_artifact(self.paths["verifier"], verifier_canonical)
        canonical_cand_digest = read_artifact(self.paths["candidate"]).digest
        canonical_verifier_digest = read_artifact(self.paths["verifier"]).digest

        # State-root copies: deliberately different content. The
        # authorization MUST NOT read these.
        bogus_state_root_candidate = dict(_make_candidate_payload(self.ctx))
        bogus_state_root_candidate["exact_head"] = (
            "ffffffffffffffffffffffffffffffffffffffff"  # different SHA
        )
        self.store.write_atomic(
            "candidate.json", bogus_state_root_candidate,
        )
        bogus_state_root_verifier = {
            "schema_version": "autocoder.verifier_record.v1",
            "candidate_sha256": "9" * 64,  # different digest
            "verdict": "FAILED",  # different verdict
            "defects": ["synthetic"],
        }
        self.store.write_atomic(
            "verifier-record.json", bogus_state_root_verifier,
        )

        args = _make_args(self.ctx, self.run_state_root, self.evidence_root)
        exit_code = cli_module.cmd_merge_authorize(args)
        self.assertEqual(
            exit_code, 0,
            f"cmd_merge_authorize failed with exit {exit_code}; "
            "canonical artifacts and state are valid",
        )

        # Authorization MUST bind canonical digests.
        auth_result = read_artifact(self.paths["authorization"])
        auth_payload = auth_result.payload
        self.assertEqual(
            auth_payload["candidate_sha256"], canonical_cand_digest,
            "cmd_merge_authorize must bind the canonical evidence-root "
            "candidate digest; got a different value, which means the "
            "state-root copy influenced the authorization",
        )
        self.assertEqual(
            auth_payload["verifier_record_sha256"], canonical_verifier_digest,
            "cmd_merge_authorize must bind the canonical evidence-root "
            "verifier digest; got a different value, which means the "
            "state-root copy influenced the authorization",
        )
        # And the digests MUST NOT equal the state-root digests.
        import hashlib
        state_root_cand_digest = hashlib.sha256(
            json.dumps(
                bogus_state_root_candidate, sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        state_root_verifier_digest = hashlib.sha256(
            json.dumps(
                bogus_state_root_verifier, sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        self.assertNotEqual(
            auth_payload["candidate_sha256"], state_root_cand_digest,
            "authorization candidate digest equals the state-root "
            "copy digest; the canonical evidence-root digest must "
            "have been used instead",
        )
        self.assertNotEqual(
            auth_payload["verifier_record_sha256"], state_root_verifier_digest,
            "authorization verifier digest equals the state-root "
            "copy digest; the canonical evidence-root digest must "
            "have been used instead",
        )

    # --- Test B: state-root copies exist; canonical candidate missing

    def test_b_missing_canonical_candidate_blocks_authorization(self) -> None:
        """State-root candidate.json exists; canonical
        evidence-root candidate.json is missing. Authorization
        MUST fail.

        Bug-detector property: the broken implementation reads
        ``store.read_optional("candidate.json")`` and would happily
        produce an authorization bound to a state-root-only artifact
        that the merge transaction cannot re-read.
        """
        # State-root copy: pretend everything is fine.
        self.store.write_atomic(
            "candidate.json", _make_candidate_payload(self.ctx),
        )
        self.store.write_atomic(
            "verifier-record.json", _make_verifier_payload(self.ctx),
        )
        # Canonical verifier exists; canonical candidate is missing.
        write_artifact(
            self.paths["verifier"], _make_verifier_payload(self.ctx),
        )

        args = _make_args(self.ctx, self.run_state_root, self.evidence_root)
        exit_code = cli_module.cmd_merge_authorize(args)
        self.assertNotEqual(
            exit_code, 0,
            "cmd_merge_authorize succeeded despite the canonical "
            "evidence-root candidate.json missing; the authorization "
            "would bind a state-root-only artifact the merge "
            "transaction cannot consume",
        )
        # Authorization must not have been written.
        self.assertFalse(
            self.paths["authorization"].exists(),
            "authorization.json must not be written when canonical "
            "candidate.json is missing",
        )

    # --- Test C: state-root copies exist; canonical verifier missing

    def test_c_missing_canonical_verifier_blocks_authorization(self) -> None:
        """State-root verifier-record.json exists; canonical
        evidence-root verifier.json is missing. Authorization
        MUST fail."""
        # State-root copies present.
        self.store.write_atomic(
            "candidate.json", _make_candidate_payload(self.ctx),
        )
        self.store.write_atomic(
            "verifier-record.json", _make_verifier_payload(self.ctx),
        )
        # Canonical candidate exists; canonical verifier is missing.
        cand_obj = Candidate.from_dict(_make_candidate_payload(self.ctx))
        write_artifact(self.paths["candidate"], cand_obj.to_dict())

        args = _make_args(self.ctx, self.run_state_root, self.evidence_root)
        exit_code = cli_module.cmd_merge_authorize(args)
        self.assertNotEqual(
            exit_code, 0,
            "cmd_merge_authorize succeeded despite the canonical "
            "evidence-root verifier.json missing; the authorization "
            "would bind a state-root-only artifact the merge "
            "transaction cannot consume",
        )
        self.assertFalse(
            self.paths["authorization"].exists(),
            "authorization.json must not be written when canonical "
            "verifier.json is missing",
        )

    # --- Test D: artifact altered after sidecar generation

    def test_d_altered_canonical_artifact_blocks_authorization(self) -> None:
        """Canonical candidate exists with a sidecar, but the
        artifact body has been altered after sidecar generation.
        ``read_artifact`` MUST detect the digest mismatch via the
        sidecar and the authorization MUST fail. This proves the
        authorization uses the strict reader, not a permissive
        JSON-only loader."""
        cand_obj = Candidate.from_dict(_make_candidate_payload(self.ctx))
        write_artifact(self.paths["candidate"], cand_obj.to_dict())
        write_artifact(self.paths["verifier"], _make_verifier_payload(self.ctx))

        # Tamper with the candidate body after sidecar generation.
        # The sidecar still records the original digest; the
        # reader must fail-closed.
        tampered = dict(cand_obj.to_dict())
        tampered["exact_head"] = (
            "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"  # mutated
        )
        # Write tampered JSON without updating the sidecar.
        # Bypass write_artifact so the sidecar remains stale.
        self.paths["candidate"].write_text(json.dumps(tampered, indent=2))

        args = _make_args(self.ctx, self.run_state_root, self.evidence_root)
        exit_code = cli_module.cmd_merge_authorize(args)
        self.assertNotEqual(
            exit_code, 0,
            "cmd_merge_authorize accepted a tampered canonical "
            "candidate; the sidecar digest must have been re-checked",
        )
        self.assertFalse(
            self.paths["authorization"].exists(),
            "authorization.json must not be written when the "
            "canonical candidate's sidecar digest does not match "
            "the artifact body",
        )


if __name__ == "__main__":
    unittest.main()