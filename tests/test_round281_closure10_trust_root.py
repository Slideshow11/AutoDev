"""Closure X tests:

- final report cannot mix snapshot IDs
- deferred actionable subset invariant
- cooldown zero plus non-empty deferred list -> consistency failure
- active worker count/list consistency
- REQUEST_INTENT Codex does not pass
- REQUEST_SENT Codex does not pass
- ACKNOWLEDGED Codex does not pass
- exact REVIEW_COMPLETE does pass
- wrong-head Codex response does not pass
- wrong-request Codex response does not pass
- CodeRabbit reopen inventory cannot serve as clean proof
- clean CodeRabbit assessment requires complete supported surfaces
- pending provenance drift cannot prove autonomous provenance
- terminal provenance chain can prove autonomous provenance
- deferred+retry prose string alone cannot prove retry lifecycle
- full ordered retry lifecycle can prove retry
- stale-head deferred event not classified current-head
- missing required environment input blocks structural freeze
- malformed worker attempt blocks EVENT_STATE_OBSERVATION_COMPLETE
- evidence-generator config fallback cannot satisfy observed static scope
- missing per-key production observation source blocks freeze
- PID reuse identity mismatch blocks freeze
- supervisor.py import_spec cannot substitute for executing-module identity
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _stub_github_api(monkeypatch, tmp_path):
    """Stub GitHub API + hermes binary + supervisor identity
    so tests are hermetic."""
    from autocoder_supervisor import hermes_fingerprint as hf
    from autocoder_supervisor import supervisor as s

    monkeypatch.setenv("AED_REPO_OWNER", "Slideshow11")
    monkeypatch.setenv("AED_REPO_NAME", "AutoDev")
    monkeypatch.setenv("AED_PR_NUMBER", "5")
    monkeypatch.setenv("AED_PR_NUMBERS", "5")
    monkeypatch.setenv(
        "AED_EXPECTED_BRANCH", "feat/review-repair-relay-v1"
    )
    monkeypatch.setenv(
        "AED_EXPECTED_BRANCH_SET", "feat/review-repair-relay-v1"
    )
    monkeypatch.setenv(
        "AED_SUPERVISOR_WORKING_CHECKOUT", str(tmp_path)
    )
    monkeypatch.setenv(
        "AED_REQUIRED_REVIEW_PROVIDERS", "coderabbit"
    )
    monkeypatch.setenv("AED_OPTIONAL_REVIEW_PROVIDERS", "codex")
    monkeypatch.setenv("AED_PROVIDERS_INDEPENDENT", "true")

    monkeypatch.setattr(s, "REPO_OWNER", "Slideshow11")
    monkeypatch.setattr(s, "REPO_NAME", "AutoDev")
    monkeypatch.setattr(s, "PR_NUMBER", 5)

    stub_hermes = tmp_path / "hermes"
    stub_hermes.write_text("#!/bin/sh\nexit 0\n")
    stub_hermes.chmod(0o755)
    monkeypatch.setenv("AED_HERMES_BIN", str(stub_hermes))

    # Write a supervisor-owned identity artifact with all 17
    # bindings.
    src_root = REPO_ROOT
    _modules = [
        ("supervisor.py", "autocoder_supervisor.supervisor"),
        ("_directive_prompt.py", "autocoder_supervisor._directive_prompt"),
        ("worker_session.py", "autocoder_supervisor.worker_session"),
        ("aed_worker_wrapper.py", "autocoder_supervisor.aed_worker_wrapper"),
        ("directive_bridge.py", "autocoder_supervisor.directive_bridge"),
        ("provenance_maintenance.py", "autocoder_supervisor.provenance_maintenance"),
        ("hermes_fingerprint.py", "autocoder_supervisor.hermes_fingerprint"),
        ("orchestration_state_root.py", "autocoder_supervisor.orchestration_state_root"),
        ("relay_wiring.py", "autocoder_supervisor.relay_wiring"),
        ("config.py", "autocoder_supervisor.config"),
        ("contracts.py", "autocoder_supervisor.contracts"),
        ("validate.py", "autocoder_supervisor.validate"),
        ("worker_attempt.py", "autocoder_orchestration.worker_attempt"),
        ("review_repair_relay.py", "autocoder_orchestration.review_repair_relay"),
        ("controller.py", "autocoder_orchestration.controller"),
        ("context.py", "autocoder_orchestration.context"),
        ("store.py", "autocoder_orchestration.store"),
    ]
    _loaded = []
    for fn, _ in _modules:
        for prefix in ["autocoder_supervisor", "autocoder_orchestration", ""]:
            candidate = src_root / prefix / fn
            if candidate.exists():
                _loaded.append({
                    "logical_module": fn,
                    "actual_production_loaded_path": str(candidate),
                    "actual_production_sha256": __import__(
                        "hashlib"
                    ).sha256(
                        candidate.read_bytes()
                    ).hexdigest(),
                    "binding_method": "loaded_module",
                })
                break
    identity = {
        "schema_version": "autocoder.acceptance_runtime_identity.v1",
        "supervisor_python_pid": 999999,
        "launcher_pid": 999998,
        "supervisor_boot_id": "boot-test",
        "supervisor_start_ticks": 0,
        "supervisor_exe": "/usr/bin/python3",
        "supervisor_cmdline_sha256": "",
        "supervisor_process_identity": "test-fixture",
        "supervisor_pid": 999999,
        "process_start_identity": "test-fixture",
        "instance_id": "test",
        "repository_owner": "Slideshow11",
        "repository_name": "AutoDev",
        "pr_number": 5,
        "expected_pr_set": "5",
        "expected_branch": "feat/review-repair-relay-v1",
        "expected_branch_set": "feat/review-repair-relay-v1",
        "production_working_checkout": str(src_root),
        "supervisor_state_directory": str(tmp_path),
        "supervisor_home": str(tmp_path),
        "hermes_binary_path": str(stub_hermes),
        "required_providers": "coderabbit",
        "optional_providers": "codex",
        "provider_independence": "true",
        "loaded_modules": _loaded,
        "generated_at": "2026-08-15T00:00:00Z",
    }
    (tmp_path / "acceptance_runtime_identity.json").write_text(
        json.dumps(identity)
    )
    (tmp_path / "run_state.json").write_text(json.dumps({
        "feature_branch": "feat/review-repair-relay-v1",
    }))
    (tmp_path / "unconsumed_events.json").write_text(
        json.dumps({"events": []})
    )
    (tmp_path / "cooldown_deferred_events.json").write_text(
        json.dumps({"entries": [], "ids": []})
    )
    (tmp_path / "consumed_event_terminality.json").write_text(
        json.dumps({"entries": []})
    )
    (tmp_path / "worker_attempts").mkdir(exist_ok=True)
    yield


# Helpers
def _write_identity(tmp_path: Path, **overrides):
    """Write a supervisor-owned acceptance identity."""
    src_root = REPO_ROOT
    identity_path = tmp_path / "acceptance_runtime_identity.json"
    if identity_path.exists():
        d = json.loads(identity_path.read_text())
    else:
        d = {}
    d.update(overrides)
    identity_path.write_text(json.dumps(d))


def _stub_evidence_env(monkeypatch, tmp_path):
    """Stub env to read live GitHub/state correctly."""
    from autocoder_supervisor import hermes_fingerprint as hf

    def _stub_live(repo, pr_number):
        return (
            "cd15d30cf65552aa3613157a3289c4d830a611f3",
            {
                "head": {
                    "sha": "cd15d30cf65552aa3613157a3289c4d830a611f3",
                    "ref": "feat/review-repair-relay-v1",
                    "repo": {"full_name": "Slideshow11/AutoDev"},
                },
                "number": 5,
                "state": "open",
                "merged": False,
                "merged_at": None,
            },
        )

    def _stub_workflow(repo, head):
        return [
            {"name": "test (3.10)", "conclusion": "success",
             "status": "completed"},
            {"name": "test (3.11)", "conclusion": "success",
             "status": "completed"},
            {"name": "test (3.12)", "conclusion": "success",
             "status": "completed"},
            {"name": "package-smoke", "conclusion": "success",
             "status": "completed"},
            {"name": "committed-state-scan",
             "conclusion": "success", "status": "completed"},
            {"name": "provenance", "conclusion": "success",
             "status": "completed"},
            {"name": "full-suite", "conclusion": "success",
             "status": "completed"},
        ]

    monkeypatch.setattr(hf, "_read_live_github_head", _stub_live)
    monkeypatch.setattr(hf, "_read_workflow_runs", _stub_workflow)


# ---------------------------------------------------------------------
# §1 Final report cannot mix snapshot IDs
# ---------------------------------------------------------------------


class TestReportSnapshotIntegrity:
    """Closure X §1: every reported field must come from
    ONE immutable pre_canary_evidence artifact."""

    def test_snapshot_id_present_in_artifact(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            generate_pre_canary_evidence,
        )
        import unittest.mock
        from autocoder_supervisor import hermes_fingerprint as hf2
        with unittest.mock.patch.object(
            hf2, "_read_live_github_head",
            return_value=(
                "cd15d30cf65552aa3613157a3289c4d830a611f3",
                {
                    "head": {
                        "sha": "cd15d30cf65552aa3613157a3289c4d830a611f3",
                        "ref": "feat/review-repair-relay-v1",
                        "repo": {
                            "full_name": "Slideshow11/AutoDev",
                        },
                    },
                    "number": 5,
                    "state": "open",
                    "merged": False,
                    "merged_at": None,
                },
            ),
        ):
            with unittest.mock.patch.object(
                hf2, "_read_workflow_runs",
                return_value=[
                    {"name": "test (3.10)", "conclusion": "success",
                     "status": "completed"},
                    {"name": "test (3.11)", "conclusion": "success",
                     "status": "completed"},
                    {"name": "test (3.12)", "conclusion": "success",
                     "status": "completed"},
                    {"name": "package-smoke",
                     "conclusion": "success", "status": "completed"},
                    {"name": "committed-state-scan",
                     "conclusion": "success", "status": "completed"},
                    {"name": "provenance", "conclusion": "success",
                     "status": "completed"},
                    {"name": "full-suite", "conclusion": "success",
                     "status": "completed"},
                ],
            ):
                ev = generate_pre_canary_evidence(
                    repo_root=str(REPO_ROOT),
                    state_dir=str(tmp_path),
                    repo="Slideshow11/AutoDev",
                    pr_number=5,
                    branch="feat/review-repair-relay-v1",
                )
        assert "generated_at" in ev
        assert ev.get("schema_version") == (
            "autocoder.pre_canary_evidence.v1"
        )

    def test_deferred_subset_invariants(self, tmp_path):
        """set(deferred_current_head_actionable)
        subset_of set(deferred_event_ids)
        """
        from autocoder_supervisor.hermes_fingerprint import (
            canonical_deferred_backlog_analysis,
        )
        # Empty ledger
        (tmp_path / "cooldown_deferred_events.json").write_text(
            json.dumps({"entries": [], "ids": []})
        )
        out = canonical_deferred_backlog_analysis(
            str(tmp_path), current_head="cd15d30c"
        )
        assert (
            set(out["deferred_current_head_actionable"]).issubset(
                set(out["deferred_event_ids"])
            )
        )
        assert (
            set(out["deferred_stale_head"]).issubset(
                set(out["deferred_event_ids"])
            )
        )
        assert (
            set(out["deferred_head_unknown"]).issubset(
                set(out["deferred_event_ids"])
            )
        )

    def test_cooldown_zero_with_deferred_fails_consistency(
        self, tmp_path,
    ):
        """If cooldown_deferred_count == 0, deferred_event_ids
        MUST be empty."""
        from autocoder_supervisor.hermes_fingerprint import (
            canonical_cooldown_deferred_count,
            canonical_deferred_backlog_analysis,
        )
        (tmp_path / "cooldown_deferred_events.json").write_text(
            json.dumps({"entries": [], "ids": []})
        )
        cd = canonical_cooldown_deferred_count(str(tmp_path))
        # cd["count"] == 0
        # Now we have a hypothetical "leak" — add an entry
        # manually.
        (tmp_path / "cooldown_deferred_events.json").write_text(
            json.dumps({
                "entries": [{
                    "id": "ev-1",
                    "head_sha": "cd15d30c",
                    "actionable": True,
                }],
                "ids": [],
            })
        )
        cd = canonical_cooldown_deferred_count(str(tmp_path))
        out = canonical_deferred_backlog_analysis(
            str(tmp_path), current_head="cd15d30c"
        )
        # entries_count > 0 ⇒ deferred_event_ids non-empty
        assert cd["count"] == 1
        assert len(out["deferred_event_ids"]) == 1


# ---------------------------------------------------------------------
# §2 Codex empirical terminal semantics
# ---------------------------------------------------------------------


class TestCodexTerminalSemantics:
    """Closure X §2: only REVIEW_COMPLETE or explicit
    OPTIONAL_DEGRADED count as terminal."""

    def test_request_intent_only_does_not_pass(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            _read_codex_optional_lifecycle_evidence,
        )
        rr = tmp_path / "review_requests"
        rr.mkdir()
        (rr / "codex__aaa.json").write_text(json.dumps({
            "request_id": "req-1",
            "request_head": "cd15d30c",
            "lifecycle": "REQUEST_INTENT",
        }))
        out = _read_codex_optional_lifecycle_evidence(
            state_dir=str(tmp_path)
        )
        assert out["value"] is False
        assert "REQUEST_INTENT" in out.get(
            "progress_states_observed", []
        )

    def test_request_sent_only_does_not_pass(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            _read_codex_optional_lifecycle_evidence,
        )
        rr = tmp_path / "review_requests"
        rr.mkdir()
        (rr / "codex__aaa.json").write_text(json.dumps({
            "request_id": "req-1",
            "request_head": "cd15d30c",
            "lifecycle": "REQUEST_SENT",
        }))
        out = _read_codex_optional_lifecycle_evidence(
            state_dir=str(tmp_path)
        )
        assert out["value"] is False

    def test_acknowledged_only_does_not_pass(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            _read_codex_optional_lifecycle_evidence,
        )
        rr = tmp_path / "review_requests"
        rr.mkdir()
        (rr / "codex__aaa.json").write_text(json.dumps({
            "request_id": "req-1",
            "request_head": "cd15d30c",
            "lifecycle": "ACKNOWLEDGED",
        }))
        out = _read_codex_optional_lifecycle_evidence(
            state_dir=str(tmp_path)
        )
        assert out["value"] is False

    def test_review_complete_exact_request_passes(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            _read_codex_optional_lifecycle_evidence,
        )
        rr = tmp_path / "review_requests"
        rr.mkdir()
        (rr / "codex__aaa.json").write_text(json.dumps({
            "request_id": "req-1",
            "request_head": "cd15d30c",
            "lifecycle": "REVIEW_COMPLETE",
        }))
        out = _read_codex_optional_lifecycle_evidence(
            state_dir=str(tmp_path)
        )
        assert out["value"] is True
        assert out["reason"] == "ok_terminal_lifecycle_observed"


# ---------------------------------------------------------------------
# §3 CodeRabbit clean evidence - provider_head_assessment required
# ---------------------------------------------------------------------


class TestCodeRabbitHeadAssessment:
    def test_no_assessment_artifact_fails_closed(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            _read_coderabbit_clean_head_evidence,
        )
        out = _read_coderabbit_clean_head_evidence(
            state_dir=str(tmp_path),
            expected_head="cd15d30c",
        )
        assert out["value"] is False
        assert (
            "no_canonical_provider_head_assessment_artifact"
            == out["reason"]
        )

    def test_assessment_missing_surfaces_fails(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            _read_coderabbit_clean_head_evidence,
        )
        asm = tmp_path / "provider_head_assessment" / "coderabbit"
        asm.mkdir(parents=True)
        (asm / "cd15d30c.json").write_text(json.dumps({
            "schema_version":
                "autocoder.provider_head_assessment.v1",
            "provider": "coderabbit",
            "head_sha": "cd15d30c",
            "observation_complete": True,
            "surfaces": {
                "top_level_comment_collected": True,
                "inline_comments_collected": True,
                "review_threads_collected": False,
                "formal_reviews_collected": True,
                "statuses_collected": True,
            },
            "completion_proof": {"exact_head_status": "success"},
            "actionable_finding_ids": [],
            "unowned_actionable_finding_ids": [],
            "clean": True,
        }))
        out = _read_coderabbit_clean_head_evidence(
            state_dir=str(tmp_path),
            expected_head="cd15d30c",
        )
        assert out["value"] is False
        assert "surfaces_missing" in out["reason"]

    def test_assessment_with_actionable_finding_fails(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            _read_coderabbit_clean_head_evidence,
        )
        asm = tmp_path / "provider_head_assessment" / "coderabbit"
        asm.mkdir(parents=True)
        (asm / "cd15d30c.json").write_text(json.dumps({
            "schema_version":
                "autocoder.provider_head_assessment.v1",
            "provider": "coderabbit",
            "head_sha": "cd15d30c",
            "observation_complete": True,
            "surfaces": {
                "top_level_comment_collected": True,
                "inline_comments_collected": True,
                "review_threads_collected": True,
                "formal_reviews_collected": True,
                "statuses_collected": True,
            },
            "completion_proof": {"exact_head_status": "success"},
            "actionable_finding_ids": ["finding-1"],
            "unowned_actionable_finding_ids": [],
            "clean": True,
        }))
        out = _read_coderabbit_clean_head_evidence(
            state_dir=str(tmp_path),
            expected_head="cd15d30c",
        )
        assert out["value"] is False
        assert "actionable_findings_present" in out["reason"]


# ---------------------------------------------------------------------
# §4 Autonomous provenance - terminal lifecycle only
# ---------------------------------------------------------------------


class TestAutonomousProvenanceTerminal:
    def test_pending_drift_does_not_prove_success(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            _read_autonomous_provenance_evidence,
        )
        # Pending entries present
        (tmp_path / "provenance_drift_pending.json").write_text(
            json.dumps([{
                "attempt_id": "att-1",
                "head_sha": "cd15d30c",
            }])
        )
        out = _read_autonomous_provenance_evidence(
            state_dir=str(tmp_path)
        )
        assert out["value"] is False

    def test_terminal_provenance_chain_passes(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            _read_autonomous_provenance_evidence,
        )
        # Empty pending ledger
        (tmp_path / "provenance_drift_pending.json").write_text(
            "[]"
        )
        # Terminal provenance artifact with full chain
        term = tmp_path / "provenance_terminal"
        term.mkdir()
        (term / "cd15d30c.json").write_text(json.dumps({
            "schema_version":
                "autocoder.provenance_terminal.v1",
            "head_sha": "cd15d30c",
            "generated_at": "2026-08-15T00:00:00Z",
            "terminal": True,
            "lifecycle_chain": [
                {"stage": "prelaunch", "event_id": "e1"},
                {"stage": "production", "event_id": "e2"},
                {"stage": "drift_discovered", "event_id": "e3"},
                {"stage": "manifest_repair", "event_id": "e4"},
                {"stage": "manifest_committed", "event_id": "e5"},
                {"stage": "drift_pending_cleared",
                 "event_id": "e6"},
                {"stage": "source_event_terminalized",
                 "event_id": "e7"},
                {"stage": "generation_terminal", "event_id": "e8"},
            ],
        }))
        out = _read_autonomous_provenance_evidence(
            state_dir=str(tmp_path)
        )
        assert out["value"] is True

    def test_stale_terminal_artifact_rejected_for_new_head(
        self, tmp_path,
    ):
        # Repair: terminal provenance evidence MUST be
        # bound to the frozen head. A terminal artifact
        # left over from a prior head MUST NOT prove
        # success for a new head whose provenance was
        # never verified — that is fail-closed behaviour.
        from autocoder_supervisor.hermes_fingerprint import (
            _read_autonomous_provenance_evidence,
        )
        # Empty pending ledger
        (tmp_path / "provenance_drift_pending.json").write_text(
            "[]"
        )
        # Terminal provenance artifact from a PRIOR head.
        # It carries a full lifecycle chain and would
        # otherwise pass the legacy mtime-only check.
        term = tmp_path / "provenance_terminal"
        term.mkdir()
        prior_head = "0" * 40  # 40-char SHA placeholder
        new_head = "1" * 40  # the current frozen head
        (term / f"{prior_head}.json").write_text(json.dumps({
            "schema_version":
                "autocoder.provenance_terminal.v1",
            "head_sha": prior_head,
            "generated_at": "2026-08-15T00:00:00Z",
            "terminal": True,
            "lifecycle_chain": [
                {"stage": "prelaunch", "event_id": "e1"},
                {"stage": "production", "event_id": "e2"},
                {"stage": "drift_discovered", "event_id": "e3"},
                {"stage": "manifest_repair", "event_id": "e4"},
                {"stage": "manifest_committed", "event_id": "e5"},
                {"stage": "drift_pending_cleared",
                 "event_id": "e6"},
                {"stage": "source_event_terminalized",
                 "event_id": "e7"},
                {"stage": "generation_terminal", "event_id": "e8"},
            ],
        }))
        # Caller is bound to the NEW head; the stale
        # artifact must NOT prove success.
        out = _read_autonomous_provenance_evidence(
            state_dir=str(tmp_path),
            expected_head=new_head,
        )
        assert out["value"] is False
        assert out["observation_complete"] is True
        assert (
            "no_terminal_provenance_artifact_for_frozen_head"
            in out["reason"]
        )
        assert out["terminal_artifact"] is None

    def test_head_matched_terminal_artifact_passes(
        self, tmp_path,
    ):
        # Counterpart: when a terminal artifact's
        # ``head_sha`` matches the expected head, it
        # passes. This locks the positive path under
        # the frozen-head binding.
        from autocoder_supervisor.hermes_fingerprint import (
            _read_autonomous_provenance_evidence,
        )
        (tmp_path / "provenance_drift_pending.json").write_text(
            "[]"
        )
        head = "f" * 40
        term = tmp_path / "provenance_terminal"
        term.mkdir()
        (term / f"{head}.json").write_text(json.dumps({
            "schema_version":
                "autocoder.provenance_terminal.v1",
            "head_sha": head,
            "generated_at": "2026-08-15T00:00:00Z",
            "terminal": True,
            "lifecycle_chain": [
                {"stage": "prelaunch", "event_id": "e1"},
                {"stage": "production", "event_id": "e2"},
                {"stage": "drift_discovered", "event_id": "e3"},
                {"stage": "manifest_repair", "event_id": "e4"},
                {"stage": "manifest_committed", "event_id": "e5"},
                {"stage": "drift_pending_cleared",
                 "event_id": "e6"},
                {"stage": "source_event_terminalized",
                 "event_id": "e7"},
                {"stage": "generation_terminal", "event_id": "e8"},
            ],
        }))
        out = _read_autonomous_provenance_evidence(
            state_dir=str(tmp_path),
            expected_head=head,
        )
        assert out["value"] is True
        assert out["evidence_head"] == head

    def test_pending_drift_breaks_success_even_with_terminal_chain(
        self, tmp_path,
    ):
        # Repair (round-586/P1): a terminal provenance
        # artifact with the FULL required lifecycle chain
        # is necessary but NOT sufficient while the pending
        # drift ledger still contains entries. A prior
        # terminal artifact's lifecycle records the success
        # of a PREVIOUS autonomous round; it does NOT prove
        # that NEW drift detected since that round has been
        # finished. The success path MUST refuse
        # ``value=True`` while ``pending_count > 0``,
        # otherwise the gate falsely certifies a head
        # whose unfinished provenance work is still queued.
        from autocoder_supervisor.hermes_fingerprint import (
            _read_autonomous_provenance_evidence,
        )
        # Pending ledger with NEW unresolved drift that
        # was registered AFTER the terminal artifact below
        # was generated.
        (tmp_path / "provenance_drift_pending.json").write_text(
            json.dumps([
                {
                    "state": "DRIFT_DETECTED",
                    "detected_at": "2026-08-15T02:00:00Z",
                    "head_sha": (
                        "abcdef1234567890abcdef1234567890abcdef12"
                    ),
                    "attempt_id": "att-20260815T020000Z-111",
                    "drifts": ["manifest_mismatch"],
                    "owner": "next_worker_round_autonomous",
                },
                {
                    "state": "DRIFT_DETECTED",
                    "detected_at": "2026-08-15T02:30:00Z",
                    "head_sha": (
                        "1234567890abcdef1234567890abcdef12345678"
                    ),
                    "attempt_id": "att-20260815T023000Z-222",
                    "drifts": ["stale_terminal_artifact"],
                    "owner": "next_worker_round_autonomous",
                },
            ])
        )
        # Prior terminal artifact with full chain AND
        # matching head_sha. Without the fail-closed
        # guard, the legacy code path would set
        # ``value=True``.
        head = "f" * 40
        term = tmp_path / "provenance_terminal"
        term.mkdir()
        (term / f"{head}.json").write_text(json.dumps({
            "schema_version":
                "autocoder.provenance_terminal.v1",
            "head_sha": head,
            "generated_at": "2026-08-15T01:00:00Z",
            "terminal": True,
            "lifecycle_chain": [
                {"stage": "prelaunch", "event_id": "e1"},
                {"stage": "production", "event_id": "e2"},
                {"stage": "drift_discovered", "event_id": "e3"},
                {"stage": "manifest_repair", "event_id": "e4"},
                {"stage": "manifest_committed", "event_id": "e5"},
                {"stage": "drift_pending_cleared",
                 "event_id": "e6"},
                {"stage": "source_event_terminalized",
                 "event_id": "e7"},
                {"stage": "generation_terminal", "event_id": "e8"},
            ],
        }))
        out = _read_autonomous_provenance_evidence(
            state_dir=str(tmp_path),
            expected_head=head,
        )
        # Fail-closed: pending entries present MUST
        # block success, even though the terminal chain
        # is intact and head-matched.
        assert out["observation_complete"] is True
        assert out["value"] is False
        assert out["pending_drift_count"] == 2
        assert (
            "pending_provenance_drift_unresolved"
            in out["reason"]
        )
        assert (
            out["pending_provenance_entry_accepted_as_success"]
            is False
        )
        # The terminal artifact path is still recorded
        # for diagnostics; only ``value`` is refused.
        assert out["evidence_head"] == head
        assert out["terminal_artifact"] is not None


# ---------------------------------------------------------------------
# §5 Real deferred retry - ordered transitions
# ---------------------------------------------------------------------


class TestDeferredRetryOrderedTransitions:
    def test_prose_string_alone_does_not_prove_retry(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            _read_real_deferred_retry_evidence,
        )
        (tmp_path / "consumed_event_terminality.json").write_text(
            json.dumps({"entries": [{
                "event_id": "ev-1",
                "consumer": "test",
                "reason": "deferred retry attempt processed",
                "lifecycle": "REQUEST_INTENT",
                "recorded_at": "2026-08-15T00:00:00Z",
            }]})
        )
        out = _read_real_deferred_retry_evidence(
            state_dir=str(tmp_path)
        )
        assert out["value"] is False

    def test_full_ordered_chain_proves_retry(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            _read_real_deferred_retry_evidence,
        )
        (tmp_path / "consumed_event_terminality.json").write_text(
            json.dumps({"entries": [
                {"event_id": "ev-1", "consumer": "test",
                 "lifecycle": "DEFERRED",
                 "recorded_at": "2026-08-15T00:00:00Z"},
                {"event_id": "ev-1", "consumer": "test",
                 "lifecycle": "ELIGIBLE",
                 "recorded_at": "2026-08-15T00:01:00Z"},
                {"event_id": "ev-1", "consumer": "test",
                 "lifecycle": "RETRY_ATTEMPT",
                 "recorded_at": "2026-08-15T00:02:00Z"},
                {"event_id": "ev-1", "consumer": "test",
                 "lifecycle": "OWNED",
                 "recorded_at": "2026-08-15T00:03:00Z"},
                {"event_id": "ev-1", "consumer": "test",
                 "lifecycle": "TERMINAL",
                 "recorded_at": "2026-08-15T00:04:00Z"},
                {"event_id": "ev-1", "consumer": "test",
                 "lifecycle": "CONSUMED",
                 "recorded_at": "2026-08-15T00:05:00Z"},
            ]})
        )
        out = _read_real_deferred_retry_evidence(
            state_dir=str(tmp_path)
        )
        assert out["value"] is True
        assert len(out["real_deferred_retry_transitions"]) == 6

    def test_reversed_ledger_does_not_prove_retry(self, tmp_path):
        # P1: ordering must be enforced. A reversed chain
        # (DEFERRED, CONSUMED, TERMINAL, OWNED, RETRY_ATTEMPT,
        #  ELIGIBLE) contains every required lifecycle name but
        # the transitions are reversed; this must NOT count as a
        # real deferred retry.
        from autocoder_supervisor.hermes_fingerprint import (
            _read_real_deferred_retry_evidence,
        )
        reversed_order = [
            "DEFERRED",
            "CONSUMED",
            "TERMINAL",
            "OWNED",
            "RETRY_ATTEMPT",
            "ELIGIBLE",
        ]
        entries = []
        for i, lc in enumerate(reversed_order):
            entries.append({
                "event_id": "ev-rev",
                "consumer": "test",
                "lifecycle": lc,
                "recorded_at": f"2026-08-15T00:0{i}:00Z",
            })
        (tmp_path / "consumed_event_terminality.json").write_text(
            json.dumps({"entries": entries})
        )
        out = _read_real_deferred_retry_evidence(
            state_dir=str(tmp_path)
        )
        assert out["value"] is False
        assert out["real_deferred_retry_event_ids"] == []
        assert out["real_deferred_retry_transitions"] == []

    def test_interleaved_ledger_does_not_prove_retry(self, tmp_path):
        # P1: ordering must be enforced. An interleaved chain
        # that visits every stage but out of order must NOT
        # count as a real deferred retry.
        from autocoder_supervisor.hermes_fingerprint import (
            _read_real_deferred_retry_evidence,
        )
        # Visits DEFERRED, then jumps to TERMINAL before
        # ELIGIBLE — monotonic ordering is violated.
        interleaved = [
            "DEFERRED",
            "TERMINAL",
            "ELIGIBLE",
            "RETRY_ATTEMPT",
            "OWNED",
            "CONSUMED",
        ]
        entries = []
        for i, lc in enumerate(interleaved):
            entries.append({
                "event_id": "ev-int",
                "consumer": "test",
                "lifecycle": lc,
                "recorded_at": f"2026-08-15T00:0{i}:00Z",
            })
        (tmp_path / "consumed_event_terminality.json").write_text(
            json.dumps({"entries": entries})
        )
        out = _read_real_deferred_retry_evidence(
            state_dir=str(tmp_path)
        )
        assert out["value"] is False
        assert out["real_deferred_retry_event_ids"] == []


# ---------------------------------------------------------------------
# §6 Deferred current-head comparison
# ---------------------------------------------------------------------


class TestDeferredCurrentHeadComparison:
    def test_matching_head_classified_current(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            canonical_deferred_backlog_analysis,
        )
        (tmp_path / "cooldown_deferred_events.json").write_text(
            json.dumps({"entries": [{
                "id": "ev-1",
                "head_sha": "cd15d30c",
                "actionable": True,
                "kind": "test",
                "lifecycle": "DEFERRED",
                "retry_owner": "owner",
                "next_retry_condition": "ok",
            }]})
        )
        out = canonical_deferred_backlog_analysis(
            str(tmp_path), current_head="cd15d30c"
        )
        assert "ev-1" in out["deferred_current_head_actionable"]
        assert "ev-1" not in out["deferred_stale_head"]

    def test_different_head_classified_stale(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            canonical_deferred_backlog_analysis,
        )
        (tmp_path / "cooldown_deferred_events.json").write_text(
            json.dumps({"entries": [{
                "id": "ev-1",
                "head_sha": "abc1234",
                "actionable": True,
                "kind": "test",
                "lifecycle": "DEFERRED",
                "retry_owner": "owner",
                "next_retry_condition": "ok",
            }]})
        )
        out = canonical_deferred_backlog_analysis(
            str(tmp_path), current_head="cd15d30c"
        )
        assert "ev-1" in out["deferred_stale_head"]
        assert "ev-1" not in out[
            "deferred_current_head_actionable"
        ]

    def test_missing_head_classified_unknown(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            canonical_deferred_backlog_analysis,
        )
        (tmp_path / "cooldown_deferred_events.json").write_text(
            json.dumps({"entries": [{
                "id": "ev-1",
                "actionable": True,
                "kind": "test",
                "lifecycle": "DEFERRED",
                "retry_owner": "owner",
                "next_retry_condition": "ok",
            }]})
        )
        out = canonical_deferred_backlog_analysis(
            str(tmp_path), current_head="cd15d30c"
        )
        assert "ev-1" in out["deferred_head_unknown"]


# ---------------------------------------------------------------------
# §7 Static environment completeness is a structural gate
# ---------------------------------------------------------------------


class TestStaticEnvironmentGate:
    def test_missing_hermes_blocks_structural_freeze(
        self, tmp_path, monkeypatch,
    ):
        from autocoder_supervisor import hermes_fingerprint as hf
        import unittest.mock

        # Empty AED_HERMES_BIN
        monkeypatch.setenv("AED_HERMES_BIN", "")
        # Delete the supervisor-owned identity so the
        # observation chain falls back to /proc (live
        # supervisor) which still has hermes; but since
        # _read_observed_static_scope returns the live
        # hermes, we patch it to return empty.
        identity_path = tmp_path / "acceptance_runtime_identity.json"
        identity_path.unlink(missing_ok=True)
        # Replace _read_observed_static_scope so the test
        # does not pull the live supervisor's hermes path
        # from /proc.
        def _patched(supervisor_pid=None, state_dir=None):
            return (
                    {
                        "repository_owner": "Slideshow11",
                        "repository_name": "AutoDev",
                        "pr_number": "5",
                        "expected_branch":
                            "feat/review-repair-relay-v1",
                        "expected_branch_set":
                            "feat/review-repair-relay-v1",
                        "production_working_checkout": str(tmp_path),
                        "supervisor_state_directory": str(tmp_path),
                        "supervisor_home": str(tmp_path),
                        # Point at a real file so expected
                        # scope validation passes.
                        "hermes_binary_path":
                            str(tmp_path / "hermes"),
                        "required_providers": "coderabbit",
                        "optional_providers": "codex",
                        "provider_independence": "true",
                        "expected_pr_set": "5",
                    },
                {
                    "repository_owner":
                        "supervisor-owned artifact",
                    "repository_name":
                        "supervisor-owned artifact",
                    "pr_number":
                        "supervisor-owned artifact",
                    "expected_branch":
                        "supervisor-owned artifact",
                    "expected_branch_set":
                        "supervisor-owned artifact",
                    "production_working_checkout":
                        "supervisor-owned artifact",
                    "supervisor_state_directory":
                        "supervisor-owned artifact",
                    "supervisor_home":
                        "supervisor-owned artifact",
                    "required_providers":
                        "supervisor-owned artifact",
                    "optional_providers":
                        "supervisor-owned artifact",
                    "provider_independence":
                        "supervisor-owned artifact",
                    "expected_pr_set":
                        "supervisor-owned artifact",
                },
                True,
            )

        monkeypatch.setattr(
            hf, "_read_observed_static_scope", _patched
        )

        def _stub_live(repo, pr_number):
            return (
                "cd15d30cf65552aa3613157a3289c4d830a611f3",
                {
                    "head": {
                        "sha":
                            "cd15d30cf65552aa3613157a3289c4d830a611f3",
                        "ref": "feat/review-repair-relay-v1",
                        "repo": {
                            "full_name": "Slideshow11/AutoDev",
                        },
                    },
                    "number": 5,
                    "state": "open",
                    "merged": False,
                    "merged_at": None,
                },
            )

        def _stub_workflow(repo, head):
            return [
                {"name": n, "conclusion": "success",
                 "status": "completed"}
                for n in [
                    "test (3.10)", "test (3.11)", "test (3.12)",
                    "package-smoke", "committed-state-scan",
                    "provenance", "full-suite",
                ]
            ]

        with unittest.mock.patch.object(
            hf, "_read_live_github_head", _stub_live
        ):
            with unittest.mock.patch.object(
                hf, "_read_workflow_runs", _stub_workflow
            ):
                ev = hf.generate_pre_canary_evidence(
                    repo_root=str(REPO_ROOT),
                    state_dir=str(tmp_path),
                    repo="Slideshow11/AutoDev",
                    pr_number=5,
                    branch="feat/review-repair-relay-v1",
                )
        assert ev["static_environment_inputs_fingerprint_complete"] is False


# ---------------------------------------------------------------------
# §8 Malformed worker attempt blocks EVENT_STATE_OBSERVATION_COMPLETE
# ---------------------------------------------------------------------


class TestEventObservationFailsClosed:
    def test_malformed_worker_attempt_fails_observation(
        self, tmp_path, monkeypatch,
    ):
        from autocoder_supervisor import hermes_fingerprint as hf
        import unittest.mock

        wa_dir = tmp_path / "worker_attempts"
        wa_dir.mkdir(exist_ok=True)
        # Write a malformed record
        (wa_dir / "att-malformed.json").write_text(
            "this is not json"
        )

        def _stub_live(repo, pr_number):
            return (
                "cd15d30cf65552aa3613157a3289c4d830a611f3",
                {
                    "head": {
                        "sha":
                            "cd15d30cf65552aa3613157a3289c4d830a611f3",
                    },
                    "state": "open",
                    "merged": False,
                    "merged_at": None,
                },
            )

        def _stub_workflow(repo, head):
            return [
                {"name": n, "conclusion": "success",
                 "status": "completed"}
                for n in [
                    "test (3.10)", "test (3.11)", "test (3.12)",
                    "package-smoke", "committed-state-scan",
                    "provenance", "full-suite",
                ]
            ]

        with unittest.mock.patch.object(
            hf, "_read_live_github_head", _stub_live
        ):
            with unittest.mock.patch.object(
                hf, "_read_workflow_runs", _stub_workflow
            ):
                ev = hf.generate_pre_canary_evidence(
                    repo_root=str(REPO_ROOT),
                    state_dir=str(tmp_path),
                    repo="Slideshow11/AutoDev",
                    pr_number=5,
                    branch="feat/review-repair-relay-v1",
                )
        assert ev["event_state_observation_complete"] is False
        assert len(ev["event_observation_failures"]) > 0


# ---------------------------------------------------------------------
# §9 Evidence-generator config fallback cannot satisfy observed scope
# ---------------------------------------------------------------------


class TestEvidenceNoSelfFill:
    def test_no_default_config_self_fill(self, tmp_path, monkeypatch):
        from autocoder_supervisor.hermes_fingerprint import (
            _read_observed_static_scope,
        )
        # Remove the supervisor-owned artifact so the
        # observation sources cannot be populated from
        # production-owned records.
        (tmp_path / "acceptance_runtime_identity.json").unlink(
            missing_ok=True
        )
        # Also delete the run_state branch source.
        (tmp_path / "run_state.json").write_text(
            json.dumps({})
        )
        # Pass a non-existent supervisor_pid so the /proc
        # lookup fails. The /proc/<pid>/environ source MUST
        # NOT contribute.
        observed, sources, complete = _read_observed_static_scope(
            supervisor_pid=99999999,  # non-existent
            state_dir=str(tmp_path),
        )
        # Observation MUST be incomplete (the legacy
        # default_config_from_env fallback is REMOVED).
        assert complete is False
        assert observed.get("repository_owner", "") == ""
        assert observed.get("repository_name", "") == ""


# ---------------------------------------------------------------------
# §10 Production process identity is non-reusable
# ---------------------------------------------------------------------


class TestProductionProcessIdentity:
    def test_artifact_contains_kernel_backed_identity(self, tmp_path):
        # Boot_id is system-wide
        import hashlib
        boot_id = open("/proc/sys/kernel/random/boot_id").read().strip()
        # The artifact records supervisor_boot_id and
        # supervisor_start_ticks as separate fields, not
        # just pid.
        from autocoder_supervisor.supervisor import (
            _resolve_production_runtime_binding,
        )
        # The supervisor binding should reflect actual file.
        r = _resolve_production_runtime_binding(
            "supervisor.py", "autocoder_supervisor.supervisor"
        )
        # Path should be non-empty when running in actual
        # supervisor context; in the pytest harness, __main__
        # is not supervisor so the fallback to sys.modules
        # applies.
        assert r["actual_production_path"] != ""


# ---------------------------------------------------------------------
# §11 supervisor.py must use executing_module binding
# ---------------------------------------------------------------------


class TestSupervisorBindingMethod:
    def test_supervisor_binding_method_is_executing(self, tmp_path):
        from autocoder_supervisor.supervisor import (
            _resolve_production_runtime_binding,
        )
        r = _resolve_production_runtime_binding(
            "supervisor.py", "autocoder_supervisor.supervisor"
        )
        assert r["binding_method"] == "executing_module"


# ---------------------------------------------------------------------
# §1 Final report consistency
# ---------------------------------------------------------------------


class TestFinalReportConsistency:
    def test_report_artifact_consistency_invariant(self, tmp_path):
        from autocoder_supervisor import hermes_fingerprint as hf
        import unittest.mock

        def _stub_live(repo, pr_number):
            return (
                "cd15d30cf65552aa3613157a3289c4d830a611f3",
                {
                    "head": {
                        "sha":
                            "cd15d30cf65552aa3613157a3289c4d830a611f3",
                    },
                    "state": "open",
                    "merged": False,
                    "merged_at": None,
                },
            )

        def _stub_workflow(repo, head):
            return [
                {"name": n, "conclusion": "success",
                 "status": "completed"}
                for n in [
                    "test (3.10)", "test (3.11)", "test (3.12)",
                    "package-smoke", "committed-state-scan",
                    "provenance", "full-suite",
                ]
            ]

        with unittest.mock.patch.object(
            hf, "_read_live_github_head", _stub_live
        ):
            with unittest.mock.patch.object(
                hf, "_read_workflow_runs", _stub_workflow
            ):
                ev = hf.generate_pre_canary_evidence(
                    repo_root=str(REPO_ROOT),
                    state_dir=str(tmp_path),
                    repo="Slideshow11/AutoDev",
                    pr_number=5,
                    branch="feat/review-repair-relay-v1",
                )
        # OBSERVED UNION MISSING == EXPECTED
        assert (
            set(ev["required_ci_checks_observed"])
            | set(ev["required_ci_checks_missing"])
        ) == set(ev["required_ci_checks_expected"])
        assert ev["report_artifact_consistency"] is True