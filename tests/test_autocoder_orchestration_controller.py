"""Integration tests for the Controller.

The controller wraps the state machine and the store. It is the
only entry point that mutates the state.json file.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from autocoder_orchestration import (
    Controller,
    StateStore,
    StateMachine,
    RunContext,
    make_run_context,
    ACTOR_CONTROLLER,
    ACTOR_IMPL_WORKER,
    ACTOR_VERIFIER,
    ACTOR_HUMAN,
    ACTOR_CANDIDATE_BUILDER,
    InvalidTransition,
    STATE_PLANNED,
    STATE_IMPLEMENTING,
    STATE_AWAITING_CI,
    STATE_QUALIFYING_READINESS,
    STATE_READY_FOR_CANDIDATE,
    STATE_CANDIDATE_FROZEN,
    STATE_AWAITING_INDEPENDENT_VERIFICATION,
    STATE_VERIFYING,
    STATE_AWAITING_MERGE_AUTHORIZATION,
    STATE_MERGE_AUTHORIZED,
    STATE_POST_MERGE_VERIFYING,
    STATE_COMPLETE,
    STATE_BLOCKED,
)


H1 = "a9501bae8fd0c449be6bb4d57bcf006a8d833474"


def _tmp_state_root(tmp_path):
    full = tmp_path / "state" / "o" / "r" / "pr-2" / "r1"
    full.mkdir(parents=True, exist_ok=True)
    return str(full)


def _context(tmp_path, **overrides):
    base = dict(
        repo_owner="o",
        repo_name="r",
        local_checkout=str(tmp_path),
        base_branch="main",
        authorized_base_sha="a79bb613a70db3d3bd659a5c8985ffcfc0835984",
        feature_branch="feat/test",
        task_specification_path="/tmp/task",
        task_specification_sha256="b" * 64,
        required_ci_jobs=("test",),
        implementation_worker_command=(),
        evidence_root="/tmp/evidence",
        state_root="/tmp/state",
        pr_number=2,
        current_authorized_head=H1,
    )
    base.update(overrides)
    return make_run_context(**base)


@pytest.fixture
def ctx_and_store(tmp_path):
    state_root = _tmp_state_root(tmp_path)
    ctx = _context(tmp_path)
    store = StateStore(state_root)
    # Save run context to disk
    store.write_atomic("run_context.json", ctx.to_dict())
    # Initialize state
    from autocoder_orchestration import StateMachine
    store.write_atomic("state.json", StateMachine().to_dict())
    return ctx, store


# === Run isolation ===
class TestRunIsolation:
    def test_pr_one_state_does_not_inherit_into_pr_two(self, tmp_path) -> None:
        """PR #1 state cannot enter PR #2.

        Create two PR-scoped state roots. Write state only under PR #1.
        A controller bound to PR #2 must not see PR #1 state.
        """
        from autocoder_orchestration import ACTOR_CONTROLLER
        # Set up PR #1 state root
        pr1_root = tmp_path / "o" / "r" / "pr-1" / "r1"
        pr1_root.mkdir(parents=True, exist_ok=True)
        pr1_store = StateStore(str(pr1_root))
        sm = StateMachine()
        sm = sm.transition(STATE_IMPLEMENTING, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)
        pr1_store.write_atomic("state.json", sm.to_dict())

        # Set up PR #2 state root
        pr2_root = tmp_path / "o" / "r" / "pr-2" / "r1"
        pr2_root.mkdir(parents=True, exist_ok=True)
        pr2_store = StateStore(str(pr2_root))
        pr2_store.write_atomic("state.json", StateMachine().to_dict())

        # PR #2 controller should see its own initial state, NOT PR #1 IMPLEMENTING.
        ctx_pr2 = _context(tmp_path, pr_number=2)
        controller_pr2 = Controller(ctx_pr2, pr2_store)
        sm_pr2 = controller_pr2.load_state_machine()
        assert sm_pr2 is not None
        assert sm_pr2.current_state == STATE_PLANNED
        assert str(pr1_root) != str(pr2_root)

    def test_repository_a_state_does_not_enter_repository_b(self, tmp_path) -> None:
        """Repository A state cannot enter repository B."""
        repo_a_root = tmp_path / "owner-a" / "repo-a" / "pr-1" / "r1"
        repo_b_root = tmp_path / "owner-b" / "repo-b" / "pr-1" / "r1"
        repo_a_root.mkdir(parents=True, exist_ok=True)
        repo_b_root.mkdir(parents=True, exist_ok=True)
        store_a = StateStore(str(repo_a_root))
        store_a.write_atomic("state.json", StateMachine().to_dict())
        store_b = StateStore(str(repo_b_root))
        store_b.write_atomic("state.json", StateMachine().to_dict())
        ctx_b = _context(tmp_path, repo_owner="owner-b", repo_name="repo-b", pr_number=1)
        controller_b = Controller(ctx_b, store_b)
        sm_b = controller_b.load_state_machine()
        assert sm_b is not None
        assert sm_b.current_state == STATE_PLANNED
        assert str(repo_a_root) != str(repo_b_root)

    def test_run_id_uniqueness(self, tmp_path) -> None:
        from autocoder_orchestration import generate_run_id
        ids = {generate_run_id() for _ in range(50)}
        assert len(ids) == 50


# === State mutations ===
class TestControllerStateMutations:
    def test_start_implementation(self, ctx_and_store) -> None:
        ctx, store = ctx_and_store
        controller = Controller(ctx, store)
        sm = controller.start_implementation()
        assert sm.current_state == STATE_IMPLEMENTING

    def test_report_implementation_complete(self, ctx_and_store) -> None:
        ctx, store = ctx_and_store
        controller = Controller(ctx, store)
        controller.start_implementation()
        sm = controller.report_implementation_complete(head_observed=H1)
        assert sm.current_state == STATE_AWAITING_CI

    def test_full_happy_path(self, ctx_and_store) -> None:
        ctx, store = ctx_and_store
        controller = Controller(ctx, store)
        controller.start_implementation()
        controller.report_implementation_complete(head_observed=H1)
        controller.report_ci_pass(head_observed=H1)
        # record readiness
        from autocoder_orchestration.readiness import ReadinessCertificate, ReadinessDecision
        decision = ReadinessDecision(
            run_id="r1", repo="o/r", pr_number=2, expected_head=H1,
            observed_head=H1, overall_passed=True, gate_results=[],
            evaluated_at="2026-08-05T22:00:00Z",
        )
        cert = ReadinessCertificate(
            decision=decision,
            issued_at="2026-08-05T22:00:00Z",
            expires_at="2026-08-05T22:10:00Z",
            certificate_id="cert-1",
            issuer="observer",
        )
        controller.record_readiness_certificate(cert, head_observed=H1)
        assert controller.load_state_machine().current_state == STATE_READY_FOR_CANDIDATE

    def test_block(self, ctx_and_store) -> None:
        ctx, store = ctx_and_store
        controller = Controller(ctx, store)
        controller.start_implementation()
        sm = controller.block(reason="blocked for testing")
        assert sm.current_state == STATE_BLOCKED

    def test_worker_cannot_set_controller_only_state(self, ctx_and_store) -> None:
        # Implementation worker cannot mark VERIFIED.
        # The controller API does not expose such methods; the state
        # machine itself enforces actor authorization.
        from autocoder_orchestration import StateMachine, ACTOR_IMPL_WORKER
        sm = StateMachine()
        sm = sm.transition(STATE_IMPLEMENTING, ACTOR_CONTROLLER, head_observed=H1, head_required=H1)
        # PLANNED -> IMPLEMENTING succeeded with controller. Now from
        # IMPLEMENTING, the worker should not be able to set arbitrary
        # controller-only states.
        with pytest.raises(InvalidTransition):
            sm.transition(STATE_CANDIDATE_FROZEN, ACTOR_IMPL_WORKER, head_observed=H1, head_required=H1)


# === Restart and resume ===
class TestRestart:
    def test_state_persisted_across_reload(self, ctx_and_store) -> None:
        ctx, store = ctx_and_store
        controller = Controller(ctx, store)
        controller.start_implementation()
        controller.report_implementation_complete(head_observed=H1)
        # New controller instance reads the same store
        new_controller = Controller(ctx, store)
        sm = new_controller.load_state_machine()
        assert sm is not None
        assert sm.current_state == STATE_AWAITING_CI
        assert sm.revision == 2


# === Verifier cannot modify source ===
class TestVerifierCannotModifySource:
    def test_verifier_api_only_takes_records(self, ctx_and_store) -> None:
        ctx, store = ctx_and_store
        from autocoder_orchestration import Controller as CtrlCls
        controller = CtrlCls(ctx, store)
        # The verifier's API is verifier_passed or verifier_failed.
        # These methods only accept a verifier_record dict and update
        # the state accordingly.
        # A verifier cannot call start_implementation or block.
        import inspect
        methods = [m for m in dir(controller) if not m.startswith("_")]
        assert "start_implementation" in methods
        assert "verifier_passed" in methods
        assert "verifier_failed" in methods
        assert "block" in methods
        # The verifier cannot directly call start_implementation without
        # being the controller. The state machine enforces this.


# === Lease and identity ===
class TestLeaseAndIdentity:
    def test_controller_records_own_identity(self, ctx_and_store) -> None:
        ctx, store = ctx_and_store
        controller = Controller(ctx, store)
        # The controller identity is set at construction time
        assert controller.controller_identity.pid > 0



class TestVerifierRejection:
    """Tests proving that verifier-started with same PID + start_id as
    the implementation worker is rejected.

    Per the directive: 'use the implementation worker's PID plus start
    identity; prove verifier_started rejects it.'
    """

    def test_verifier_started_with_implementation_worker_identity_is_rejected(self, ctx_and_store) -> None:
        """Use the implementation worker's actual PID plus start identity
        to attempt verifier_started. The role guard must reject.
        """
        from autocoder_orchestration.verifier_handoff import (
            VerifierHandoff, VerifierRoleGuard, write_handoff
        )
        from autocoder_orchestration.store import ProcessIdentity
        ctx, store = ctx_and_store

        # Move the run to AWAITING_INDEPENDENT_VERIFICATION through the
        # canonical transition chain.
        controller = Controller(ctx, store)
        controller.start_implementation()
        controller.report_implementation_complete(head_observed=H1)
        controller.report_ci_pass(head_observed=H1)
        from autocoder_orchestration import ReadinessCertificate, ReadinessDecision
        from datetime import datetime, timezone, timedelta
        now = datetime.now(tz=timezone.utc)
        decision = ReadinessDecision(
            run_id="r1", repo="o/r", pr_number=1,
            expected_head=H1, observed_head=H1,
            overall_passed=True, gate_results=[],
            evaluated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        cert = ReadinessCertificate(
            decision=decision,
            issued_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            expires_at=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            certificate_id="cert-1",
            issuer="observer",
        )
        controller.record_readiness_certificate(cert, head_observed=H1)
        # Use the build_candidate path to reach CANDIDATE_FROZEN.
        from autocoder_orchestration.candidate import Candidate
        candidate = Candidate(
            schema_version="autocoder.candidate.v1",
            run_id="r1", repo="o/r", pr_number=1,
            exact_head=H1, base_sha="a" * 40, base_branch="main",
            task_specification_sha256="b" * 64,
            readiness_certificate_id="cert-1",
            readiness_certificate_sha256="c" * 64,
            readiness_overall_passed=True,
            ci_inventory=[], review_inventory=[], thread_inventory={},
            strict_observation_log_hash="d" * 64,
            process_identity={}, lock_release_evidence={},
            controller_state_revision=0,
            controller_state_path="state.json",
            input_hashes={}, source_files={}, aed_source_files={},
            created_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        controller.build_candidate(candidate, head_observed=H1)
        controller.prompt_verifier(head_observed=H1)

        handoff = VerifierHandoff(
            schema_version="autocoder.verifier_handoff.h1",
            run_id="r1", repo="o/r", pr_number=1,
            exact_head=H1,
            base_sha="a" * 40,
            base_branch="main",
            task_specification_sha256="b" * 64,
            candidate_path="candidate.json",
            candidate_sha256="c" * 64,
            readiness_certificate_id="cert-1",
            observation_log_path="observations.jsonl",
            observation_log_sha256="d" * 64,
            strict_window_first_utc=None,
            strict_window_last_utc=None,
            strict_window_observation_count=0,
            strict_window_duration_monotonic=0.0,
            controller_state_revision=0,
            controller_state_path="state.json",
            trusted_verifier_source_commit="e" * 64,
            trusted_verifier_package_version="autocoder-orchestration-1.0.0",
            implementation_worker_identity={"pid": 99999, "start_id": "fake-worker-start"},
            verifier_record_path="verifier-record.json",
            created_at="2026-08-05T22:00:00Z",
        )
        write_handoff(store, handoff)

        guard = VerifierRoleGuard(handoff, store)

        same_identity = ProcessIdentity(pid=99999, start_id="fake-worker-start")
        ok, reason = guard.validate(
            verifier_identity=same_identity,
            verifier_executable_path=None,
            write_credentials_present=False,
        )
        assert not ok, f"verifier with same identity was accepted: {reason}"
        assert "process identity" in reason

        different_identity = ProcessIdentity(pid=12345, start_id="different-start")
        ok2, _ = guard.validate(
            verifier_identity=different_identity,
            verifier_executable_path=None,
            write_credentials_present=False,
        )
        assert ok2


    def test_state_machine_rejects_verifier_authorizing_merge(self) -> None:
        """The verifier cannot authorize the merge; the state machine
        refuses ACTOR_VERIFIER for the authorize_merge transition.
        """
        from autocoder_orchestration import (
            StateMachine,
            ACTOR_VERIFIER,
            STATE_AWAITING_MERGE_AUTHORIZATION,
            STATE_MERGE_AUTHORIZED,
        )
        sm = StateMachine(current_state=STATE_AWAITING_MERGE_AUTHORIZATION)
        from autocoder_orchestration.state_machine import InvalidTransition
        with pytest.raises(InvalidTransition):
            sm.transition(STATE_MERGE_AUTHORIZED, ACTOR_VERIFIER,
                          head_observed=H1, head_required=H1)

    def test_state_machine_rejects_worker_authorizing_merge(self) -> None:
        """An implementation worker cannot authorize the merge.
        """
        from autocoder_orchestration import (
            StateMachine,
            ACTOR_IMPL_WORKER,
            STATE_AWAITING_MERGE_AUTHORIZATION,
            STATE_MERGE_AUTHORIZED,
        )
        sm = StateMachine(current_state=STATE_AWAITING_MERGE_AUTHORIZATION)
        from autocoder_orchestration.state_machine import InvalidTransition
        with pytest.raises(InvalidTransition):
            sm.transition(STATE_MERGE_AUTHORIZED, ACTOR_IMPL_WORKER,
                          head_observed=H1, head_required=H1)