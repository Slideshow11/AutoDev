"""Round-41 regression tests — terminal worker outcomes
(no-op vs startup failure vs execution failure) and the
controller handoff.

Covers the production paths introduced by the round-41
infrastructure repair:

  Section 5  — Worker startup handshake semantics
  Section 6  — NO_CHANGES_REQUIRED lifecycle routing
  Section 8  — Worker startup failure distinct from
               generic WORKER_EXITED_NO_PUSH
  Section 9  — Thread inventory reconciliation
  Section 10 — Thread resolution with proof
  Section 13 — Test coverage

The tests exercise the production modules directly:
    autocoder_supervisor.supervisor
    autocoder_orchestration.worker_attempt
    autocoder_orchestration.controller
    autocoder_orchestration.state_machine
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


# Resolve the package imports once at module load.
from autocoder_supervisor import supervisor
from autocoder_orchestration.worker_attempt import (
    LIFECYCLE_NO_CHANGES_REQUIRED,
    LIFECYCLE_TERMINAL_REPAIRED,
    LIFECYCLE_WORKER_EXITED_NO_PUSH,
    LIFECYCLE_WORKER_RUNNING,
    LIFECYCLE_PUSH_VERIFIED,
    TERMINAL_LIFECYCLES,
    WorkerAttemptRecord,
    WorkerAttemptStore,
)
from autocoder_orchestration.controller import Controller
from autocoder_orchestration.context import make_run_context
from autocoder_orchestration.state_machine import (
    STATE_QUALIFYING_READINESS,
    STATE_REPAIRING_REVIEW_FINDINGS,
)
from autocoder_orchestration.store import StateStore


# ---------------------------------------------------------------------------
# TEST 1 — new lifecycle constants exist
# ---------------------------------------------------------------------------

def test_no_changes_required_lifecycle_constant() -> None:
    """Round-41: a structured worker no-op outcome MUST be
    a distinct terminal lifecycle so the controller can
    advance toward qualifying readiness.
    """
    assert LIFECYCLE_NO_CHANGES_REQUIRED == "NO_CHANGES_REQUIRED"


def test_terminal_lifecycles_include_no_changes_required() -> None:
    """Round-41: a no-op worker is a SUCCESS terminal
    lifecycle; the controller can advance the state
    machine from REPAIRING_REVIEW_FINDINGS to
    QUALIFYING_READINESS without an intermediate
    AWAITING_CI (no commit was produced, so no CI gate is
    required for THIS round's commit).
    """
    assert LIFECYCLE_NO_CHANGES_REQUIRED in TERMINAL_LIFECYCLES


# ---------------------------------------------------------------------------
# TEST 2 — supervisor routes no-op attempt to NO_CHANGES_REQUIRED
# ---------------------------------------------------------------------------

def _make_record(attempt_id: str, prelaunch_head: str, extra: dict) -> WorkerAttemptRecord:
    return WorkerAttemptRecord(
        schema_version="autocoder.worker_attempt.v1",
        attempt_id=attempt_id,
        claim_id=f"claim-{attempt_id}",
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        pr_number=5,
        event_ids=("ev-1",),
        finding_ids=(),
        directive_digest="d" * 64,
        directive_path="/tmp/directive.json",
        prelaunch_head=prelaunch_head,
        expected_branch="feat/review-repair-relay-v1",
        pid=99999,  # nonexistent -> pid_alive() returns False
        lease_id=attempt_id,
        started_at="2026-08-10T00:00:00Z",
        last_progress_at="2026-08-10T00:00:00Z",
        finished_at=None,
        lifecycle=LIFECYCLE_WORKER_RUNNING,
        attempt_count=1,
        stdout_path=None,
        stderr_path=None,
        exit_code=None,
        signal=None,
        result_artifact_path=None,
        produced_commit_sha=None,
        pushed_commit_sha=None,
        origin_head_verified=False,
        github_head_verified=False,
        terminal_reason=None,
        extra=extra,
    )


def test_poll_worker_attempt_routes_no_op_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-41: when ``poll_worker_attempt`` observes a
    worker attempt whose ``extra.no_changes_required_proof``
    carries structured evidence, the supervisor MUST
    transition the attempt to ``LIFECYCLE_NO_CHANGES_REQUIRED``
    (NOT ``WORKER_EXITED_NO_PUSH``) and release the lease.
    """
    store_dir = tmp_path / "worker_attempts"
    store_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(supervisor, "WORKER_ATTEMPTS_DIR", store_dir)

    attempt_id = "att-round41-noop-1"
    record = _make_record(
        attempt_id=attempt_id,
        prelaunch_head="a" * 40,
        extra={
            "no_changes_required_proof": {
                "findings": [
                    {
                        "category": "ALREADY_SATISFIED",
                        "finding_id": "thread:PRRT_TEST",
                    }
                ],
                "verification": {"verifier_tests_passed": 260},
            }
        },
    )
    WorkerAttemptStore(store_dir).write(record)

    monkeypatch.setattr(supervisor, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(supervisor, "_reap_worker", lambda _pid: (0, None))
    monkeypatch.setattr(supervisor, "remove_lease", lambda: None)

    lease = {
        "attempt_id": attempt_id,
        "pid": 99999,
        "session_id": "ses_FAKE",
    }
    result = supervisor.poll_worker_attempt(
        attempt_id=attempt_id, lease=lease,
    )
    assert result == "DIED"
    rec = WorkerAttemptStore(store_dir).read(attempt_id)
    assert rec is not None
    assert rec.lifecycle == LIFECYCLE_NO_CHANGES_REQUIRED


def test_poll_worker_attempt_no_op_uses_round_dispatch_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-41: the structured worker outcome may also
    live in the canonical round dispatch JSON. The
    supervisor's ``poll_worker_attempt`` MUST detect this
    and route to ``LIFECYCLE_NO_CHANGES_REQUIRED``.

    Round-45: also patch out the GitHub push-recovery probe
    so the test exercises ONLY the dispatch-ledger fallback.
    Without this patch the round-37 attribution would
    legitimately promote a generic dead worker to
    PUSH_VERIFIED when the real ``feat/review-repair-relay-v1``
    head has advanced past the fixture's ``"a"*40`` prelaunch
    head — that promotion is the correct behaviour under the
    round-45 precedence fix, not a test failure.
    """
    store_dir = tmp_path / "worker_attempts"
    store_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(supervisor, "WORKER_ATTEMPTS_DIR", store_dir)

    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    # Set HOME so the supervisor's ``os.environ.get("HOME")``
    # resolves to ``fake_home`` regardless of the C module
    # ``os.path.expanduser`` binding.
    monkeypatch.setenv("HOME", str(fake_home))
    # Set the supervisor's repo globals so the AED runs
    # directory lookup targets our fake home.
    monkeypatch.setattr(supervisor, "REPO_OWNER", "Slideshow11")
    monkeypatch.setattr(supervisor, "REPO_NAME", "AutoDev")
    monkeypatch.setattr(supervisor, "PR_NUMBER", 5)
    # Round-45: stub out the GitHub push-recovery probe so the
    # test exercises ONLY the dispatch-ledger fallback path.
    # Returning ``None`` makes ``_live_head`` empty, the
    # ``_live_head and _live_head != rec.prelaunch_head`` guard
    # fails, and ``push_attributable`` stays False — leaving the
    # dispatch-ledger NO_OP routing as the only classifier.
    monkeypatch.setattr(supervisor, "get_github_token", lambda: "")
    monkeypatch.setattr(supervisor, "github_get", lambda *_a, **_kw: None)
    runs_dir = (
        fake_home
        / ".hermes" / "aed" / "runs" / "Slideshow11" / "AutoDev" / "5"
    )
    runs_dir.mkdir(parents=True, exist_ok=True)
    head_sha = "a" * 40
    round_payload = {
        "directive_id": "test-directive-1",
        "head_sha_at_dispatch": head_sha,
        "round_index": 99,
        "outcome": "NO_OP",
        "verdict": "no_source_edit_required",
    }
    (runs_dir / "round_99_dispatch.json").write_text(
        json.dumps(round_payload), encoding="utf-8",
    )

    attempt_id = "att-round41-noop-json"
    record = _make_record(
        attempt_id=attempt_id,
        prelaunch_head=head_sha,
        extra={},
    )
    WorkerAttemptStore(store_dir).write(record)

    monkeypatch.setattr(supervisor, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(supervisor, "_reap_worker", lambda _pid: (0, None))
    monkeypatch.setattr(supervisor, "remove_lease", lambda: None)

    result = supervisor.poll_worker_attempt(
        attempt_id=attempt_id,
        lease={
            "attempt_id": attempt_id,
            "pid": 99999,
            "session_id": "ses_FAKE",
        },
    )
    assert result == "DIED"
    rec = WorkerAttemptStore(store_dir).read(attempt_id)
    assert rec is not None
    assert rec.lifecycle == LIFECYCLE_NO_CHANGES_REQUIRED


# ---------------------------------------------------------------------------
# TEST 3 — generic dead worker stays NO_PUSH
# ---------------------------------------------------------------------------

def test_poll_worker_attempt_generic_dead_still_no_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that died WITHOUT structured no-op proof
    AND without a real push MUST still be classified
    ``WORKER_EXITED_NO_PUSH`` so the controller routes to
    retry/escalation.

    Round-45: also patch out the GitHub push-recovery probe
    AND the supervisor's REPO_OWNER/REPO_NAME/PR_NUMBER
    globals so the test exercises the NO_PUSH transition
    without being mis-attributed to the real
    ``feat/review-repair-relay-v1`` head or picking up a
    stale ``round_<N>_dispatch.json`` from the AED runs
    directory. Without these patches the round-37
    attribution or the dispatch-ledger NO_OP routing
    would shadow the genuine WORKER_EXITED_NO_PUSH
    outcome — both behaviours are correct under the
    round-45 precedence fix, not test failures.
    """
    store_dir = tmp_path / "worker_attempts"
    store_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(supervisor, "WORKER_ATTEMPTS_DIR", store_dir)
    monkeypatch.setattr(supervisor, "REPO_OWNER", "fake-owner")
    monkeypatch.setattr(supervisor, "REPO_NAME", "fake-repo")
    monkeypatch.setattr(supervisor, "PR_NUMBER", 0)

    attempt_id = "att-round41-generic-1"
    record = _make_record(
        attempt_id=attempt_id,
        prelaunch_head="a" * 40,
        extra={},
    )
    WorkerAttemptStore(store_dir).write(record)
    monkeypatch.setattr(supervisor, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(supervisor, "_reap_worker", lambda _pid: (1, None))
    # Round-45: stub out the GitHub push-recovery probe so the
    # generic-dead-worker classification is the actual test path
    # under examination, not the round-37 PUSH_VERIFIED promotion.
    monkeypatch.setattr(supervisor, "get_github_token", lambda: "")
    monkeypatch.setattr(supervisor, "github_get", lambda *_a, **_kw: None)

    result = supervisor.poll_worker_attempt(
        attempt_id=attempt_id,
        lease={
            "attempt_id": attempt_id,
            "pid": 99999,
            "session_id": "ses_FAKE",
        },
    )
    rec = WorkerAttemptStore(store_dir).read(attempt_id)
    assert rec is not None
    assert rec.lifecycle == LIFECYCLE_WORKER_EXITED_NO_PUSH


# ---------------------------------------------------------------------------
# TEST 3b — round-45 precedence: stale dispatch-ledger MUST NOT shadow a
# real worker push (finding 4 regression guard).
# ---------------------------------------------------------------------------

def test_poll_worker_attempt_push_verified_beats_stale_noop_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-45 finding 4 regression guard: a stale
    ``round_<N>_dispatch.json`` at the current head MUST NOT
    shadow a real worker push that the round-37 attribution
    can verify. Without this fix, the dead worker is wrongly
    terminalized as ``LIFECYCLE_NO_CHANGES_REQUIRED``,
    orphaning the legitimate repair.

    The fixture simulates: a worker attempted at prelaunch
    head ``a*40``, the live GitHub head advanced to ``b*40``
    on ``feat/review-repair-relay-v1``, ``origin/<branch>``
    matches, and the committer-date is one minute AFTER the
    worker's ``started_at``. A stale ``round_99_dispatch.json``
    carrying ``outcome=NO_OP`` and ``head_sha_at_dispatch``
    matching the AUTHORITATIVE_HEAD is also present — under
    the previous ordering the worker would be misclassified
    as ``NO_CHANGES_REQUIRED``. Under the round-45
    precedence, push-recovery runs first and the worker is
    correctly promoted to ``PUSH_VERIFIED``.
    """
    store_dir = tmp_path / "worker_attempts"
    store_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(supervisor, "WORKER_ATTEMPTS_DIR", store_dir)
    monkeypatch.setattr(supervisor, "REPO_OWNER", "Slideshow11")
    monkeypatch.setattr(supervisor, "REPO_NAME", "AutoDev")
    monkeypatch.setattr(supervisor, "PR_NUMBER", 5)
    monkeypatch.setattr(supervisor, "AUTHORITATIVE_HEAD", "b" * 40)

    # Set up the AED runs dir with a stale no-op dispatch at
    # AUTHORITATIVE_HEAD so the dispatch-ledger fallback WOULD
    # match under the previous (buggy) ordering.
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    runs_dir = (
        fake_home
        / ".hermes" / "aed" / "runs" / "Slideshow11" / "AutoDev" / "5"
    )
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "round_99_dispatch.json").write_text(
        json.dumps({
            "directive_id": "test-directive-stale",
            "head_sha_at_dispatch": "b" * 40,
            "round_index": 99,
            "outcome": "NO_OP",
            "verdict": "no_source_edit_required",
        }),
        encoding="utf-8",
    )

    prelaunch_head = "a" * 40
    new_head = "b" * 40
    attempt_id = "att-round45-p1-precedence"
    record = _make_record(
        attempt_id=attempt_id,
        prelaunch_head=prelaunch_head,
        extra={},
    )
    WorkerAttemptStore(store_dir).write(record)
    monkeypatch.setattr(supervisor, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(supervisor, "_reap_worker", lambda _pid: (0, None))
    monkeypatch.setattr(supervisor, "remove_lease", lambda: None)

    # Simulate a genuine worker push: the live PR head
    # advanced, origin/<branch> matches, committer date is
    # AFTER started_at.
    def _fake_gh(path: str, token: str = "") -> dict | None:
        if "/pulls/" in path and "/reviews" not in path:
            return {"head": {"sha": new_head}}
        return None

    monkeypatch.setattr(supervisor, "github_get", _fake_gh)
    monkeypatch.setattr(supervisor, "get_github_token", lambda: "fake-token")

    class _SR:
        def __init__(self, out: str = "") -> None:
            self.stdout = out
            self.returncode = 0

    def _fake_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "rev-parse" in cmd:
            return _SR(new_head)
        return _SR("")

    class _CO:
        def __init__(self, out: str) -> None:
            self._out = out
        def strip(self) -> str:
            return self._out

    def _fake_check_output(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "log" in cmd:
            return _CO("2026-08-10T00:01:00+00:00")
        return _CO("")

    monkeypatch.setattr(supervisor.subprocess, "run", _fake_run)
    monkeypatch.setattr(supervisor.subprocess, "check_output", _fake_check_output)

    result = supervisor.poll_worker_attempt(
        attempt_id=attempt_id,
        lease={
            "attempt_id": attempt_id,
            "pid": 99999,
            "session_id": "ses_FAKE",
        },
    )
    rec = WorkerAttemptStore(store_dir).read(attempt_id)
    assert rec is not None
    # Round-45 finding 4: push-recovery attribution must win
    # over a stale dispatch-ledger NO_OP. The worker is
    # promoted to PUSH_VERIFIED and the head-rebind path
    # can route the advance through mark_head_advanced_public.
    assert rec.lifecycle == LIFECYCLE_PUSH_VERIFIED, (
        "round-45 P1 precedence regression: a stale "
        "round_<N>_dispatch.json at AUTHORITATIVE_HEAD "
        "MUST NOT shadow a real worker push with "
        "origin/<branch>=live_head AND committer_date > "
        "rec.started_at — push-recovery attribution runs "
        "FIRST and the worker is promoted to PUSH_VERIFIED."
    )
    assert rec.pushed_commit_sha == new_head


# ---------------------------------------------------------------------------
# TEST 4 — controller report_no_changes_required advances state
# ---------------------------------------------------------------------------

def _build_controller(
    state_root: str,
    *,
    current_state: str = STATE_REPAIRING_REVIEW_FINDINGS,
    head: str = "a" * 40,
) -> Controller:
    ctx = make_run_context(
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        local_checkout="/tmp/checkout",
        base_branch="main",
        authorized_base_sha="a" * 40,
        feature_branch="feat/review-repair-relay-v1",
        task_specification_path="/tmp/task",
        task_specification_sha256="b" * 64,
        required_ci_jobs=[],
        implementation_worker_command=[],
        evidence_root="/tmp/evidence",
        state_root=state_root,
        pr_number=5,
        current_authorized_head=head,
    )
    store = StateStore(state_root)
    store.write_atomic("run_context.json", ctx.to_dict())
    # Initialize the state machine to the requested state.
    from autocoder_orchestration import StateMachine
    sm = StateMachine()
    sm_dict = sm.to_dict()
    sm_dict["current_state"] = current_state
    store.write_atomic("state.json", sm_dict)
    return Controller(ctx, store)


def test_controller_report_no_changes_required_advances_state(
    tmp_path: Path,
) -> None:
    """Round-41: a structured ``NO_CHANGES_REQUIRED``
    outcome advances ``REPAIRING_REVIEW_FINDINGS`` directly
    to ``QUALIFYING_READINESS``.
    """
    state_root = str(tmp_path / "orch")
    Path(state_root).mkdir(parents=True, exist_ok=True)
    ctrl = _build_controller(
        state_root, current_state=STATE_REPAIRING_REVIEW_FINDINGS,
    )

    new_sm = ctrl.report_no_changes_required(
        head_observed="b" * 40,
        proof={
            "findings": [
                {"category": "ALREADY_SATISFIED", "finding_id": "thread:X"}
            ],
        },
    )
    assert new_sm.current_state == STATE_QUALIFYING_READINESS


# ---------------------------------------------------------------------------
# TEST 5 — state machine transition is encoded
# ---------------------------------------------------------------------------

def test_state_machine_no_changes_required_transition_encoded() -> None:
    """Round-41: the no-op transition is encoded
    explicitly.
    """
    from autocoder_orchestration.state_machine import _FORWARD_TRANSITIONS

    found = False
    for t in _FORWARD_TRANSITIONS:
        if (
            t.source == STATE_REPAIRING_REVIEW_FINDINGS
            and t.target == STATE_QUALIFYING_READINESS
            and "no_changes_required_proof" in t.required_evidence
        ):
            found = True
            break
    assert found, (
        "Round-41: REPAIRING_REVIEW_FINDINGS -> "
        "QUALIFYING_READINESS transition with "
        "no_changes_required_proof evidence is required."
    )


# ---------------------------------------------------------------------------
# TEST 6 — terminal lifecycles are well-defined
# ---------------------------------------------------------------------------

def test_terminal_lifecycles_set_is_well_defined() -> None:
    """Round-41: ``TERMINAL_LIFECYCLES`` includes both
    success and failure terminal states.
    """
    assert isinstance(TERMINAL_LIFECYCLES, frozenset)
    assert LIFECYCLE_TERMINAL_REPAIRED in TERMINAL_LIFECYCLES
    assert LIFECYCLE_WORKER_EXITED_NO_PUSH in TERMINAL_LIFECYCLES
    assert LIFECYCLE_NO_CHANGES_REQUIRED in TERMINAL_LIFECYCLES


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
