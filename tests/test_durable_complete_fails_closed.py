"""Regression tests for defect 3.2: durable COMPLETE must fail closed.

The merge record is written through the canonical artifact writer
before ``Controller.report_complete()`` is called. The CLI must not
report success unless the COMPLETE state transition is durable.

If ``report_complete()`` raises (ControllerError, StateStoreError,
OSError), the CLI returns a nonzero exit code, leaves the merge
record durable for retry, and emits a recovery message. The CLI
does NOT claim COMPLETE in this case.
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
from autocoder_orchestration.controller import Controller, ControllerError
from autocoder_orchestration.context import SCHEMA_VERSION as RC_SCHEMA
from autocoder_orchestration.context import RunContext
from autocoder_orchestration.store import StateStore, StateStoreError


def _make_run_context(evidence_root: Path, run_state_root: Path) -> RunContext:
    return RunContext(
        schema_version=RC_SCHEMA,
        run_id="test-complete-001",
        created_at="2026-08-06T22:00:00Z",
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


class DurableCompleteFailsClosedTests(unittest.TestCase):
    """§3.2: report_complete() failure must produce a controlled
    nonzero CLI result; the merge record remains durable for retry."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="aed-complete-"))
        self.run_state_root = self.tmpdir / "state"
        self.evidence_root = self.tmpdir / "evidence"
        self.run_state_root.mkdir(parents=True)
        self.evidence_root.mkdir(parents=True)
        self.ctx = _make_run_context(self.evidence_root, self.run_state_root)
        self.store = StateStore(str(self.run_state_root))
        self.store.write_atomic("run_context.json", self.ctx.to_dict())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_controller_report_complete_state_store_error_propagates(self) -> None:
        """When ``StateStore.write_atomic`` raises during the COMPLETE
        transition, ``Controller.report_complete`` propagates the
        ``StateStoreError`` unchanged (does NOT swallow it)."""
        from autocoder_orchestration.state_machine import (
            StateMachine, STATE_POST_MERGE_VERIFYING,
        )
        sm = StateMachine(current_state=STATE_POST_MERGE_VERIFYING)
        self.store.write_atomic("state.json", sm.to_dict())

        controller = Controller(self.ctx, self.store)
        # Force write_atomic to raise on the COMPLETE state write.
        # save_state_machine calls write_atomic("state.json", ...).
        original_write = self.store.write_atomic
        def fail_on_state(rel, payload):
            if rel == "state.json":
                raise StateStoreError("simulated disk full")
            return original_write(rel, payload)
        self.store.write_atomic = fail_on_state
        try:
            with self.assertRaises(StateStoreError):
                controller.report_complete()
        finally:
            self.store.write_atomic = original_write

    def test_cli_report_complete_failure_returns_nonzero(self) -> None:
        """When the CLI runs ``cmd_post_merge_verify`` and
        ``report_complete()`` raises ``StateStoreError``, the CLI
        returns a nonzero exit code and emits a controlled error
        message. The merge record remains durable."""
        from autocoder_orchestration.state_machine import (
            StateMachine, STATE_MERGE_AUTHORIZED,
        )
        sm = StateMachine(current_state=STATE_MERGE_AUTHORIZED)
        self.store.write_atomic("state.json", sm.to_dict())

        # Write a minimal MergeRecord so cmd_post_merge_verify can read it.
        from autocoder_orchestration.merge_authorization import MergeRecord
        from autocoder_orchestration.canonical_paths import canonical_paths
        paths = canonical_paths(self.evidence_root)
        paths["merge_record"].parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        rec = MergeRecord(
            schema_version="autocoder.merge_record.v2",
            run_id=self.ctx.run_id,
            repo=f"{self.ctx.repo_owner}/{self.ctx.repo_name}",
            pr_number=int(self.ctx.pr_number),
            authorized_head=str(self.ctx.current_authorized_head),
            squash_merge_commit="a" * 40,
            merge_commit_parent="b" * 40,
            final_local_main_sha="a" * 40,
            final_origin_main_sha="a" * 40,
            local_main_equals_origin_main=True,
            working_tree_clean=True,
            aed_clean_post_merge=True,
            final_state="COMPLETE",
        )
        from autocoder_orchestration.artifacts import write_artifact
        write_artifact(paths["merge_record"], rec.to_dict())

        # Now invoke cmd_post_merge_verify with report_complete mocked
        # to raise StateStoreError. The CLI must return EXIT_STATE (4),
        # not EXIT_OK (0).
        args = type("Args", (), {
            "run_id": self.ctx.run_id,
            "state_root": str(self.run_state_root),
            "evidence_root": str(self.evidence_root),
            "json": True,
        })()

        # Patch Controller.report_complete to simulate the failure.
        original_report_complete = Controller.report_complete
        def fail_report_complete(self_):
            raise StateStoreError("simulated post-merge disk full")
        with mock.patch.object(
            Controller, "report_complete", fail_report_complete
        ):
            exit_code = cli_module.cmd_post_merge_verify(args)
        self.assertNotEqual(exit_code, 0,
            "report_complete() failure must not produce exit code 0")
        self.assertEqual(exit_code, 4,  # EXIT_STATE
            f"expected EXIT_STATE=4; got {exit_code}")

        # Restore for cleanup
        Controller.report_complete = original_report_complete

    def test_merge_record_remains_durable_when_complete_fails(self) -> None:
        """When ``report_complete()`` raises, the merge record MUST
        remain durable so a subsequent retry can persist COMPLETE."""
        from autocoder_orchestration.state_machine import (
            StateMachine, STATE_MERGE_AUTHORIZED,
        )
        sm = StateMachine(current_state=STATE_MERGE_AUTHORIZED)
        self.store.write_atomic("state.json", sm.to_dict())

        from autocoder_orchestration.merge_authorization import MergeRecord
        from autocoder_orchestration.canonical_paths import canonical_paths
        paths = canonical_paths(self.evidence_root)
        paths["merge_record"].parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        rec = MergeRecord(
            schema_version="autocoder.merge_record.v2",
            run_id=self.ctx.run_id,
            repo=f"{self.ctx.repo_owner}/{self.ctx.repo_name}",
            pr_number=int(self.ctx.pr_number),
            authorized_head=str(self.ctx.current_authorized_head),
            squash_merge_commit="a" * 40,
            merge_commit_parent="b" * 40,
            final_local_main_sha="a" * 40,
            final_origin_main_sha="a" * 40,
            local_main_equals_origin_main=True,
            working_tree_clean=True,
            final_state="COMPLETE",
        )
        from autocoder_orchestration.artifacts import write_artifact
        write_artifact(paths["merge_record"], rec.to_dict())

        # Pre-condition: merge record exists.
        self.assertTrue(paths["merge_record"].exists())

        # Simulate report_complete failure.
        args = type("Args", (), {
            "run_id": self.ctx.run_id,
            "state_root": str(self.run_state_root),
            "evidence_root": str(self.evidence_root),
            "json": True,
        })()

        original_report_complete = Controller.report_complete
        def fail_report_complete(self_):
            raise StateStoreError("simulated")
        with mock.patch.object(
            Controller, "report_complete", fail_report_complete
        ):
            exit_code = cli_module.cmd_post_merge_verify(args)
        Controller.report_complete = original_report_complete

        # Merge record MUST still exist for retry.
        self.assertTrue(
            paths["merge_record"].exists(),
            "merge record was deleted; retry recovery would fail",
        )


if __name__ == "__main__":
    unittest.main()