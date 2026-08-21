"""Round-C24-R2 / Temporary independent review (CodeRabbit) regression tests.

Covers the six validated findings from the CodeRabbit independent
review of PR #9 at exact head 25c4f02fa35fbe58f1870a6b2f6498ec3d6990f1:

  CR-001 (Critical): ``mark_head_advanced_public`` referenced
        ``REPO_OWNER`` / ``REPO_NAME`` / ``PR_NUMBER`` without
        importing them; the resulting NameError escaped the narrow
        exception handler so every verified repair push failed to
        record ``report_repair_pushed`` and the controller never
        advanced REPAIRING_REVIEW_FINDINGS -> AWAITING_CI.
  CR-002 (Major): the exact-head commit_id binding returned True
        BEFORE the R3 actionable-body check, letting a status
        marker bound to the new head resurrect an outdated thread.
  CR-003 (Major): ``check_provider_freshness`` read the legacy
        scalar ``policy.required`` instead of the phase-resolved
        value, producing a spurious NOT_NEEDED (fail-open) for
        phase-required deployments.
  CR-004 (Major): the superseded-record accounting helper read
        ``superseded_head``/``head_sha`` while the supervisor
        persists ``stale_head``, so the per-head subtraction never
        ran in production.
  CR-005 (Major): the request-cooldown branch required an explicit
        ``now``; production calls omit it, so the cooldown never
        applied and a failed remote mutation could re-issue
        REQUEST every heartbeat slice.
  CR-006 (Major): ``_evaluate_c23_required_blockers`` blocked
        readiness on ANY non-NOT_NEEDED plan entry, including
        optional providers whose outage must be informational.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_REPO_ROOT = Path(os.environ.get("REPO_ROOT", str(Path(__file__).resolve().parent.parent)))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ===========================================================================
# CR-002: R3 (actionable body) must gate the exact-head binding branch
# ===========================================================================

from autocoder_orchestration.review_repair_relay import (  # noqa: E402
    _maybe_resurrect_outdated_thread,
)

HEAD_OLD = "a" * 40
HEAD_NEW = "b" * 40
SUPERSEDED_AT = "2026-08-20T12:00:00Z"
FOLLOWUP_TS = "2026-08-20T13:00:00Z"


def _cr2_thread(*, body: str, commit_id: str | None) -> dict:
    return {
        "id": "PRRT_CR2",
        "thread_id": "PRRT_CR2",
        "outdated": True,
        "resolved": False,
        "superseded_by_head": HEAD_NEW,
        "superseded_at": SUPERSEDED_AT,
        "replies": [
            {
                "id": 1,
                "createdAt": FOLLOWUP_TS,
                "author": "chatgpt-codex-connector[bot]",
                "body": body,
                "commit_id": commit_id,
            }
        ],
    }


class TestCR2R3BeforeExactHeadBinding:
    def test_status_marker_bound_to_new_head_does_not_resurrect(self) -> None:
        """A 'Walkthrough' status marker whose commit_id equals the
        new head MUST NOT resurrect the outdated thread."""
        thread = _cr2_thread(body="Walkthrough", commit_id=HEAD_NEW)
        result = _maybe_resurrect_outdated_thread(
            thread,
            current_head=HEAD_NEW,
            operator_logins=(),
        )
        assert result is None, (
            "status marker bound to the new head must not resurrect"
        )

    def test_actionable_followup_bound_to_new_head_still_resurrects(
        self,
    ) -> None:
        """The exact-head binding still qualifies ACTIONABLE
        follow-ups (no regression to the C24-R2 contract)."""
        thread = _cr2_thread(
            body="<sub><sub>![P1 Badge](...)</sub></sub> P1: foo.py:1 broken",
            commit_id=HEAD_NEW,
        )
        result = _maybe_resurrect_outdated_thread(
            thread,
            current_head=HEAD_NEW,
            operator_logins=(),
        )
        assert result is not None

    def test_status_marker_on_legacy_timestamp_path_still_rejected(
        self,
    ) -> None:
        """Without a commit binding, a status marker remains
        rejected through the legacy timestamp path too."""
        thread = _cr2_thread(body="Walkthrough", commit_id=None)
        result = _maybe_resurrect_outdated_thread(
            thread,
            current_head=HEAD_NEW,
            operator_logins=(),
        )
        assert result is None


# ===========================================================================
# CR-003 / CR-004 / CR-005 / CR-006: reviewer-policy phase awareness,
# stale_head accounting, production cooldown, required-only blockers
# ===========================================================================

from autocoder_supervisor.reviewer_policy import (  # noqa: E402
    FRESHNESS_OPTIONAL_STALE,
    FRESHNESS_PENDING,
    PHASE_INITIAL_HEAD,
    ReviewerPolicy,
    _count_active_request_records,
    check_provider_freshness,
    plan_reviewer_actions,
)


def _cr3_policy(
    *,
    required: bool = False,
    max_requests_per_head: int = 1,
    request_cooldown_seconds: int = 600,
    phase_required: dict | None = None,
) -> ReviewerPolicy:
    return ReviewerPolicy(
        name="codex",
        required=required,
        auto_runs_on_pr_creation=True,
        auto_runs_on_push=False,
        auto_trigger=True,
        trigger_handle="@codex review",
        budget_per_pr=None,
        max_requests_per_head=max_requests_per_head,
        freshness_grace_seconds=180,
        unavailable_behavior="BLOCK",
        request_cooldown_seconds=request_cooldown_seconds,
        phase_required=(
            phase_required
            if phase_required is not None
            else {PHASE_INITIAL_HEAD: True}
        ),
    )


class TestCR3PhaseResolvedRequiredInFreshness:
    def test_phase_required_provider_with_no_evidence_requests(self) -> None:
        """required=False scalar + phase_required[INITIAL]=True must
        produce REQUEST (not the fail-open NOT_NEEDED)."""
        snap = {
            "formal_reviews": [],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
            "issue_comments": [],
        }
        plans = plan_reviewer_actions(
            head_sha="a" * 40,
            snap=snap,
            policies={"codex": _cr3_policy()},
            phase=PHASE_INITIAL_HEAD,
        )
        assert plans["codex"].action == "REQUEST"

    def test_freshness_override_direct(self) -> None:
        snap = {
            "formal_reviews": [],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
            "issue_comments": [],
        }
        # Override False beats scalar True.
        res = check_provider_freshness(
            provider="codex",
            policy=_cr3_policy(required=True),
            snap=snap,
            head_sha="a" * 40,
            required_override=False,
        )
        assert res.state == FRESHNESS_OPTIONAL_STALE
        # Override True beats scalar False.
        res = check_provider_freshness(
            provider="codex",
            policy=_cr3_policy(),
            snap=snap,
            head_sha="a" * 40,
            required_override=True,
        )
        assert res.state == FRESHNESS_PENDING


class TestCR4StaleHeadAccounting:
    def test_stale_head_record_subtracts_from_per_head_cap(
        self, tmp_path: Path,
    ) -> None:
        """A SUPERSEDED record persisted by the supervisor carries
        the head under ``stale_head``; the accounting helper MUST
        honour that key so the per-head subtraction runs."""
        ledger = tmp_path / "review_requests"
        ledger.mkdir(parents=True)
        head = "a" * 40
        (ledger / f"codex__{head}.json").write_text(json.dumps({
            "lifecycle": "REQUEST_SENT",
            "requested_at": "2026-08-19T14:00:00Z",
            "request_id": "req-1",
        }))
        on_current, in_pr = _count_active_request_records(
            ledger_path=ledger,
            provider="codex",
            head_sha=head,
            superseded_records=[
                {"provider": "codex", "stale_head": head},
            ],
        )
        assert on_current == 0
        assert in_pr == 1

    def test_legacy_alias_keys_still_subtract(self, tmp_path: Path) -> None:
        ledger = tmp_path / "review_requests"
        ledger.mkdir(parents=True)
        head = "a" * 40
        (ledger / f"codex__{head}.json").write_text(json.dumps({
            "lifecycle": "REQUEST_SENT",
            "requested_at": "2026-08-19T14:00:00Z",
            "request_id": "req-1",
        }))
        for key in ("superseded_head", "head_sha"):
            on_current, _ = _count_active_request_records(
                ledger_path=ledger,
                provider="codex",
                head_sha=head,
                superseded_records=[{"provider": "codex", key: head}],
            )
            assert on_current == 0, f"alias {key} must subtract"


class TestCR5CooldownAppliesWithoutExplicitNow:
    def test_recent_intent_without_now_waits_for_auto(
        self, tmp_path: Path,
    ) -> None:
        """Production calls plan_reviewer_actions WITHOUT ``now``;
        a REQUEST_INTENT posted within the cooldown window must
        yield WAITING_FOR_AUTO, not a duplicate REQUEST."""
        ledger = tmp_path / "review_requests"
        ledger.mkdir(parents=True)
        head = "a" * 40
        recent = (
            datetime.now(timezone.utc) - timedelta(seconds=60)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        (ledger / f"codex__{head}.json").write_text(json.dumps({
            "lifecycle": "REQUEST_INTENT",
            "requested_at": recent,
            "request_id": "req-intent-1",
        }))
        snap = {
            "formal_reviews": [],
            "provider_surfaces": {},
            "_provider_issue_comments": {},
            "providers": {},
            "issue_comments": [],
        }
        plans = plan_reviewer_actions(
            head_sha=head,
            snap=snap,
            policies={"codex": _cr3_policy(
                required=True,
                phase_required={},
                max_requests_per_head=2,
            )},
            ledger_path=ledger,
            # NOTE: no ``now`` — mirrors apply_reviewer_plan.
        )
        assert plans["codex"].action == "WAITING_FOR_AUTO"


# ===========================================================================
# CR-006: readiness blockers filter on the required flag
# ===========================================================================

from autocoder_supervisor.supervisor import (  # noqa: E402
    _evaluate_c23_required_blockers,
)


class TestCR6OnlyRequiredProvidersBlock:
    def test_optional_provider_pending_does_not_block(self) -> None:
        snap = {
            "reviewer_plan": {
                "codex": {
                    "action": "WAITING_FOR_AUTO",
                    "reason": "grace_window",
                    "required": True,
                },
                "sourcery": {
                    "action": "BLOCK",
                    "reason": "provider_reported_paused",
                    "required": False,
                },
            },
        }
        blockers = _evaluate_c23_required_blockers(snap, "a" * 40)
        providers = [b["provider"] for b in blockers]
        assert providers == ["codex"], (
            "optional provider outage must not block qualification"
        )

    def test_missing_required_flag_defaults_to_blocking(self) -> None:
        """Legacy stamped plans without the flag keep the previous
        conservative behaviour (fail-closed)."""
        snap = {
            "reviewer_plan": {
                "codex": {"action": "REQUEST", "reason": "stale"},
            },
        }
        blockers = _evaluate_c23_required_blockers(snap, "a" * 40)
        assert [b["provider"] for b in blockers] == ["codex"]


# ===========================================================================
# CR-001: mark_head_advanced_public end-to-end (NameError regression)
# ===========================================================================

from autocoder_orchestration.state_machine import (  # noqa: E402
    STATE_AWAITING_CI,
    STATE_REPAIRING_REVIEW_FINDINGS,
    StateMachine,
)
from autocoder_orchestration.store import StateStore  # noqa: E402
from autocoder_orchestration.worker_attempt import (  # noqa: E402
    LIFECYCLE_PUSH_VERIFIED,
    SCHEMA_VERSION,
    WorkerAttemptRecord,
    WorkerResultArtifact,
)


class TestCR1MarkHeadAdvancedPublicEndToEnd:
    """A fully-provenanced verified repair push MUST advance the
    controller to AWAITING_CI. Before the CR-001 fix this raised an
    uncaught NameError (REPO_OWNER/REPO_NAME/PR_NUMBER undefined in
    relay_wiring), so report_repair_pushed never fired."""

    HEAD_OLD = "c" * 40
    HEAD_NEW = "d" * 40

    def _seed_attempt(self, store_dir: Path) -> WorkerAttemptRecord:
        rec = WorkerAttemptRecord(
            schema_version=SCHEMA_VERSION,
            attempt_id="att-cr1",
            claim_id="claim-cr1",
            repo_owner="owner",
            repo_name="repo",
            pr_number=4,
            event_ids=(),
            finding_ids=(),
            directive_digest="",
            directive_path="",
            prelaunch_head=self.HEAD_OLD,
            expected_branch="feat/test",
            pid=999_999,
            lease_id="att-cr1",
            started_at="2026-08-20T14:00:00Z",
            last_progress_at="2026-08-20T14:00:00Z",
            finished_at=None,
            lifecycle=LIFECYCLE_PUSH_VERIFIED,
            attempt_count=1,
            stdout_path=None,
            stderr_path=None,
            exit_code=None,
            signal=None,
            result_artifact_path=None,
            produced_commit_sha=self.HEAD_NEW,
            pushed_commit_sha=self.HEAD_NEW,
            origin_head_verified=True,
            github_head_verified=True,
            terminal_reason=None,
        )
        rec.extra["result_contract_id"] = "rc-cr1"
        from autocoder_orchestration.worker_attempt import (
            WorkerAttemptStore,
        )
        WorkerAttemptStore(store_dir).write(rec)
        return rec

    def _write_artifact(self, path: Path, rec: WorkerAttemptRecord) -> None:
        from autocoder_orchestration.worker_attempt import (
            WORKER_RESULT_SCHEMA_VERSION,
        )
        artifact = WorkerResultArtifact(
            schema_version=WORKER_RESULT_SCHEMA_VERSION,
            attempt_id=rec.attempt_id,
            claim_id=rec.claim_id,
            directive_digest=rec.directive_digest,
            result_type="REPAIRS_PUSHED",
            produced_commit_shas=(self.HEAD_NEW,),
            pushed_commit_shas=(self.HEAD_NEW,),
            completed_at="2026-08-20T14:05:00Z",
            repo="owner/repo",
            pr_number=rec.pr_number,
            expected_branch=rec.expected_branch,
            prelaunch_head=rec.prelaunch_head,
            extra={
                "expected_result_contract_id": "rc-cr1",
                "observed_result_contract_id": "rc-cr1",
                "result_contract_match": True,
                "worker_result_envelope_seen": True,
                "envelope_status": "ok",
            },
        )
        artifact.write(path)

    def _setup_orch_state(self, tmp_path: Path) -> Path:
        orch = tmp_path / "orch_run"
        orch.mkdir()
        task_spec = tmp_path / "workitem.md"
        task_spec.write_text("# Workitem (test fixture)\n")
        (orch / "run_context.json").write_text(json.dumps({
            "schema_version": "autocoder.run_context.v1",
            "run_id": "r-cr1",
            "repo_owner": "owner",
            "repo_name": "repo",
            "local_checkout": str(tmp_path),
            "base_branch": "main",
            "authorized_base_sha": "e" * 64,
            "feature_branch": "feat/test",
            "task_specification_path": str(task_spec),
            "task_specification_sha256": "f" * 64,
            "required_ci_jobs": [],
            "implementation_worker_command": [],
            "evidence_root": str(tmp_path / "evidence"),
            "state_root": str(orch),
            "pr_number": 4,
            "current_authorized_head": self.HEAD_OLD,
        }))
        sm = StateMachine(current_state=STATE_REPAIRING_REVIEW_FINDINGS)
        # StateStore enforces owner-only permissions (0600) on every
        # file it reads; write both canonical files through the store
        # so the modes match its invariant.
        store = StateStore(str(orch))
        store.write_atomic("state.json", sm.to_dict())
        return orch

    def test_verified_push_advances_controller_to_awaiting_ci(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from autocoder_supervisor import relay_wiring
        from autocoder_supervisor import supervisor as sup_mod

        store_dir = tmp_path / "wa"
        store_dir.mkdir()
        rec = self._seed_attempt(store_dir)
        artifact_path = tmp_path / "result.json"
        self._write_artifact(artifact_path, rec)
        rec.result_artifact_path = str(artifact_path)
        from autocoder_orchestration.worker_attempt import (
            WorkerAttemptStore,
        )
        WorkerAttemptStore(store_dir).write(rec)

        orch = self._setup_orch_state(tmp_path)
        # The StateStore read path enforces owner-only (0600)
        # permissions on every canonical file; align the fixture.
        os.chmod(orch / "run_context.json", 0o600)
        run_state_path = tmp_path / "sup" / "run_state.json"
        run_state_path.parent.mkdir(parents=True)
        # 0600: the resolver's StateStore read enforces owner-only
        # permissions on RUN_STATE as well.
        run_state_path.write_text(json.dumps({
            "orchestration_state_root": str(orch),
        }))
        os.chmod(run_state_path, 0o600)

        monkeypatch.setattr(sup_mod, "RUN_STATE", run_state_path)
        monkeypatch.setattr(
            sup_mod, "_worker_attempt_store",
            lambda: __import__(
                "autocoder_orchestration.worker_attempt",
                fromlist=["WorkerAttemptStore"],
            ).WorkerAttemptStore(store_dir),
        )
        monkeypatch.setattr(
            relay_wiring, "_fetch_pr_head_pushed_at",
            lambda **kwargs: "",
        )

        result = relay_wiring.mark_head_advanced_public(
            old_head_sha=self.HEAD_OLD,
            new_head_sha=self.HEAD_NEW,
            attempt_id="att-cr1",
        )
        assert result is True, (
            "verified repair push must acknowledge the head advance"
        )
        sm_after = json.loads((orch / "state.json").read_text())
        assert sm_after.get("current_state") == STATE_AWAITING_CI, (
            "controller must advance REPAIRING -> AWAITING_CI "
            f"; got {sm_after.get('current_state')!r}"
        )
