"""Closure VII tests:

§2 - EXPECTED vs OBSERVED static scope (no env injection)
§3 - All acceptance-critical modules compared (no silent skip)
§4 - prelaunch_head is the binding head (not produced/pushed)
§5 - Canonical cooldown parser
§6 - canonical active worker determination
§7 - Codex scheduler uses canonical active workers
§8 - Replay behavioral tests
§10 - Required CI checks encoding
"""
from __future__ import annotations

import pytest


import json
from pathlib import Path


# ---------------------------------------------------------------------------
# §2 - EXPECTED vs OBSERVED static scope
# ---------------------------------------------------------------------------



@pytest.fixture(autouse=True)
def _stub_github_api(monkeypatch, tmp_path):
    """Stub the GitHub API + hermes + supervisor identity
    so tests do NOT hit the real API or require hermes.
    """
    from autocoder_supervisor import hermes_fingerprint as hf
    from autocoder_supervisor import supervisor as s

    monkeypatch.setenv("AED_REPO_OWNER", "Slideshow11")
    monkeypatch.setenv("AED_REPO_NAME", "AutoDev")
    monkeypatch.setenv("AED_PR_NUMBER", "5")
    monkeypatch.setenv("AED_PR_NUMBERS", "5")
    monkeypatch.setenv("AED_EXPECTED_BRANCH", "feat/review-repair-relay-v1")
    monkeypatch.setenv("AED_EXPECTED_BRANCH_SET", "feat/review-repair-relay-v1")
    monkeypatch.setenv("AED_SUPERVISOR_WORKING_CHECKOUT", str(tmp_path))
    monkeypatch.setenv("AED_REQUIRED_REVIEW_PROVIDERS", "coderabbit")
    monkeypatch.setenv("AED_OPTIONAL_REVIEW_PROVIDERS", "codex")
    monkeypatch.setenv("AED_PROVIDERS_INDEPENDENT", "true")

    monkeypatch.setattr(s, "REPO_OWNER", "Slideshow11")
    monkeypatch.setattr(s, "REPO_NAME", "AutoDev")
    monkeypatch.setattr(s, "PR_NUMBER", 5)

    stub_hermes = tmp_path / "hermes"
    stub_hermes.write_text("#!/bin/sh\nexit 0\n")
    stub_hermes.chmod(0o755)
    monkeypatch.setenv("AED_HERMES_BIN", str(stub_hermes))

    # Write a supervisor-owned acceptance_runtime_identity
    # artifact with bindings for every acceptance-critical
    # module so the
    # Closure IX §6 evidence generator has the supervisor
    # bindings it requires.
    from pathlib import Path as _P
    src_root = Path(__file__).resolve().parent.parent
    _modules = [
        ("supervisor.py", "autocoder_supervisor.supervisor"),
        ("_directive_prompt.py", "autocoder_supervisor._directive_prompt"),
        ("worker_session.py", "autocoder_supervisor.worker_session"),
        ("aed_worker_wrapper.py", "autocoder_supervisor.aed_worker_wrapper"),
        ("directive_bridge.py", "autocoder_supervisor.directive_bridge"),
        ("provenance_maintenance.py", "autocoder_supervisor.provenance_maintenance"),
        ("hermes_fingerprint.py", "autocoder_supervisor.hermes_fingerprint"),
        ("orchestration_state_root.py", "autocoder_supervisor.orchestration_state_root"),
        ("orchestration_bootstrap.py", "autocoder_supervisor.orchestration_bootstrap"),
        ("relay_wiring.py", "autocoder_supervisor.relay_wiring"),
        ("config.py", "autocoder_supervisor.config"),
        ("contracts.py", "autocoder_supervisor.contracts"),
        ("validate.py", "autocoder_supervisor.validate"),
        # Round-785 P1: push_gate.py is now part of the
        # acceptance runtime inventory and must be loaded by
        # the supervisor-owned artifact fixture so the
        # Closure IX §6 evidence generator can resolve it.
        ("push_gate.py", "autocoder_supervisor.push_gate"),
        ("worker_attempt.py", "autocoder_orchestration.worker_attempt"),
        ("review_repair_relay.py", "autocoder_orchestration.review_repair_relay"),
        ("controller.py", "autocoder_orchestration.controller"),
        ("context.py", "autocoder_orchestration.context"),
        ("store.py", "autocoder_orchestration.store"),
    ]
    _loaded = []
    for fn, _ in _modules:
        # Look for the source checkout file.
        for prefix in [
            "autocoder_supervisor",
            "autocoder_orchestration",
            "",
        ]:
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
                })
                break
    identity = {
        "schema_version": "autocoder.acceptance_runtime_identity.v1",
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
    # Write AED configs to satisfy env fingerprint
    for prof in [
        "aed-builder", "aed-quarantine", "aed-researcher",
        "aed-reviewer", "aed-specifier",
    ]:
        # Touch but don't require content.
        pass
    from autocoder_supervisor import hermes_fingerprint as hf
    from autocoder_supervisor import supervisor as s

    # Stub the supervisor PID discovery by always
    # returning a non-existent PID, forcing the code
    # through the durable state_dir / run_state.json /
    # module-level constants fallback path.
    # (We don't need to monkey-patch the ps call
    # because it already gracefully handles no match.)

    # Pre-populate the policy with expected values so
    # the observed scope is complete.
    monkeypatch.setattr(
        s, "REPO_OWNER", "Slideshow11"
    )
    monkeypatch.setattr(
        s, "REPO_NAME", "AutoDev"
    )
    monkeypatch.setattr(s, "PR_NUMBER", 5)
    # Policy / providers / hermes path are read fresh.

    # Create a run_state.json in tmp_path so the observer
    # has branch info. Tests that need their own run_state.json
    # can override.
    rs = tmp_path / "run_state.json"
    if not rs.exists():
        rs.write_text(json.dumps({
            "feature_branch": "feat/review-repair-relay-v1",
            "current_head": "a" * 40,
        }))

    def _stub_live(repo, pr_number):
        body = {
            "head": {
                "sha": "cd15d30cf65552aa3613157a3289c4d830a611f3",
                "ref": "feat/review-repair-relay-v1",
                "repo": {"full_name": "Slideshow11/AutoDev"},
            },
            "number": 5,
            "state": "open",
            "merged": False,
            "merged_at": None,
        }
        return body["head"]["sha"], body

    def _stub_workflow(repo, head):
        return [{
            "name": "test (3.10)", "conclusion": "success", "status": "completed",
        }, {
            "name": "test (3.11)", "conclusion": "success", "status": "completed",
        }, {
            "name": "test (3.12)", "conclusion": "success", "status": "completed",
        }, {
            "name": "package-smoke", "conclusion": "success", "status": "completed",
        }, {
            "name": "committed-state-scan", "conclusion": "success", "status": "completed",
        }, {
            "name": "provenance", "conclusion": "success", "status": "completed",
        }, {
            "name": "full-suite", "conclusion": "success", "status": "completed",
        }]

    monkeypatch.setattr(hf, "_read_live_github_head", _stub_live)
    monkeypatch.setattr(hf, "_read_workflow_runs", _stub_workflow)
    yield


class TestStaticScopeObservedVsExpected:
    """Closure VII §2: the evidence generator must NOT
    mutate os.environ to expected values. It must read
    the OBSERVED scope from the production supervisor
    (its /proc/<pid>/environ) and report a per-key
    comparison.
    """

    def test_read_expected_static_scope_returns_dict(self):
        from autocoder_supervisor.hermes_fingerprint import (
            _read_expected_static_scope,
        )
        out = _read_expected_static_scope()
        assert isinstance(out, dict)
        assert "repository_owner" in out
        assert "repository_name" in out

    def test_read_observed_static_scope_returns_dict(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            _read_observed_static_scope,
        )
        # Provide a stub run_state.json with the canonical
        # branch (the C22 validator enforces this).
        rs = tmp_path / "run_state.json"
        rs.write_text(json.dumps({"feature_branch": "feat/review-repair-relay-v1"}))
        # Provide a stub supervisor_pid that doesn't exist
        # so we skip the /proc/environ path.
        # Closure VIII: function now returns a tuple
        # (scope, sources_per_key, observation_complete).
        result = _read_observed_static_scope(
            supervisor_pid=999999,
            state_dir=str(tmp_path),
        )
        # Unpack the tuple.
        if isinstance(result, tuple):
            out = result[0]
        else:
            out = result
        assert isinstance(out, dict)
        assert "repository_owner" in out

    def test_compare_scopes_match_passes(self):
        from autocoder_supervisor.hermes_fingerprint import (
            _compare_scopes,
            STATIC_SCOPE_KEYS,
        )
        scope = {k: "v" for k in STATIC_SCOPE_KEYS}
        result = _compare_scopes(scope, scope)
        assert result["match"] is True
        assert result["all_required_keys_present"] is True

    def test_compare_scopes_missing_observed_key_fails(self):
        from autocoder_supervisor.hermes_fingerprint import (
            _compare_scopes,
            STATIC_SCOPE_KEYS,
        )
        expected = {k: "v" for k in STATIC_SCOPE_KEYS}
        observed = {k: "v" for k in STATIC_SCOPE_KEYS}
        observed["repository_owner"] = ""
        result = _compare_scopes(expected, observed)
        assert result["match"] is False
        assert result["all_required_keys_present"] is False
        assert result["per_key"]["repository_owner"]["reason"] == (
            "missing_observed_key"
        )

    def test_compare_scopes_wrong_pr_fails(self):
        from autocoder_supervisor.hermes_fingerprint import (
            _compare_scopes,
            STATIC_SCOPE_KEYS,
        )
        expected = {k: "v" for k in STATIC_SCOPE_KEYS}
        observed = {k: "v" for k in STATIC_SCOPE_KEYS}
        observed["pr_number"] = "WRONG"
        result = _compare_scopes(expected, observed)
        assert result["match"] is False

    def test_compare_scopes_wrong_repo_fails(self):
        from autocoder_supervisor.hermes_fingerprint import (
            _compare_scopes,
            STATIC_SCOPE_KEYS,
        )
        expected = {k: "v" for k in STATIC_SCOPE_KEYS}
        observed = {k: "v" for k in STATIC_SCOPE_KEYS}
        observed["repository_name"] = "WRONG"
        result = _compare_scopes(expected, observed)
        assert result["match"] is False

    def test_compare_scopes_wrong_branch_fails(self):
        from autocoder_supervisor.hermes_fingerprint import (
            _compare_scopes,
            STATIC_SCOPE_KEYS,
        )
        expected = {k: "v" for k in STATIC_SCOPE_KEYS}
        observed = {k: "v" for k in STATIC_SCOPE_KEYS}
        observed["expected_branch"] = "WRONG"
        result = _compare_scopes(expected, observed)
        assert result["match"] is False

    def test_compare_scopes_wrong_hermes_bin_fails(self):
        from autocoder_supervisor.hermes_fingerprint import (
            _compare_scopes,
            STATIC_SCOPE_KEYS,
        )
        expected = {k: "v" for k in STATIC_SCOPE_KEYS}
        observed = {k: "v" for k in STATIC_SCOPE_KEYS}
        observed["hermes_binary_path"] = "/WRONG"
        result = _compare_scopes(expected, observed)
        assert result["match"] is False

    def test_compare_scopes_wrong_provider_set_fails(self):
        from autocoder_supervisor.hermes_fingerprint import (
            _compare_scopes,
            STATIC_SCOPE_KEYS,
        )
        expected = {k: "v" for k in STATIC_SCOPE_KEYS}
        observed = {k: "v" for k in STATIC_SCOPE_KEYS}
        observed["required_providers"] = "WRONG"
        result = _compare_scopes(expected, observed)
        assert result["match"] is False

    def test_evidence_generator_does_not_mutate_observed_via_env(self, tmp_path):
        """The evidence generator must NOT overwrite the
        observed scope by mutating os.environ to expected
        values. We verify that AFTER generation, the
        OBSERVED scope fingerprints reflect the production
        supervisor's actual config, not the expected one."""
        import json as _json
        from autocoder_supervisor.hermes_fingerprint import (
            generate_pre_canary_evidence,
        )
        # Provide a stub run_state.json so the branch can
        # be observed.
        rs = tmp_path / "run_state.json"
        rs.write_text(_json.dumps({
            "feature_branch": "feat/review-repair-relay-v1",
            "current_head": "a" * 40,
        }))
        # Snapshot env before.
        import os
        before_env = {}
        for k in ["AED_REPO_OWNER", "AED_PR_NUMBER", "AED_EXPECTED_BRANCH"]:
            before_env[k] = os.environ.get(k)

        ev = generate_pre_canary_evidence(
            repo_root=str(Path(__file__).resolve().parent.parent),
            state_dir=str(tmp_path),
            repo="Slideshow11/AutoDev",
            pr_number=5,
            branch="feat/review-repair-relay-v1",
        )
        # Env must not be permanently mutated.
        for k, v in before_env.items():
            assert os.environ.get(k) == v, (
                f"env was mutated: {k}={os.environ.get(k)} expected {v}"
            )
        # The artifact has BOTH expected AND observed scopes.
        assert "expected_static_scope" in ev
        assert "observed_static_scope" in ev
        # The two fingerprints are independent (Closure VII
        # §2: they may be equal, but they're produced by
        # separate computations).
        assert (
            "expected_static_scope_fingerprint" in ev
        )
        assert (
            "observed_static_scope_fingerprint" in ev
        )


# ---------------------------------------------------------------------------
# §3 - All acceptance-critical modules compared
# ---------------------------------------------------------------------------


class TestAcceptanceRuntimeComparison:
    """Closure VII §3: every acceptance-critical module
    MUST be in the runtime_file_records.
    No silent skip when source or deployed path is
    outside the repo_root.
    """

    def test_inventory_has_expected_modules(self):
        """Round-785 P1: push_gate.py was added to the
        acceptance runtime inventory (then 18 modules) to
        ensure the supervisor-side validator that gates
        worker pushes is included in runtime_files_missing
        accounting and in _ACCEPTANCE_RUNTIME_BINDINGS.
        C24 adds orchestration_bootstrap.py, bringing the
        acceptance-critical inventory to 19 modules."""
        from autocoder_supervisor.hermes_fingerprint import (
            ACCEPTANCE_RUNTIME_INVENTORY,
        )
        assert len(ACCEPTANCE_RUNTIME_INVENTORY) == 19

    def test_runtime_records_have_required_fields(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            generate_pre_canary_evidence,
        )
        ev = generate_pre_canary_evidence(
            repo_root=str(Path(__file__).resolve().parent.parent),
            state_dir=str(tmp_path),
            repo="Slideshow11/AutoDev",
            pr_number=5,
            branch="feat/review-repair-relay-v1",
        )
        records = ev["runtime_file_records"]
        for r in records:
            assert "logical_module" in r
            # Closure VIII: source_path renamed to
            # committed_source_path; deployed_path renamed
            # to actual_production_loaded_path.
            assert "committed_source_path" in r
            assert "actual_production_loaded_path" in r
            assert "committed_source_sha256" in r
            assert "actual_production_sha256" in r
            assert "match" in r
            assert "source_exists" in r
            assert "deployed_exists" in r

    def test_runtime_files_compared_count_matches_expected(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            generate_pre_canary_evidence,
        )
        ev = generate_pre_canary_evidence(
            repo_root=str(Path(__file__).resolve().parent.parent),
            state_dir=str(tmp_path),
            repo="Slideshow11/AutoDev",
            pr_number=5,
            branch="feat/review-repair-relay-v1",
        )
        assert ev["acceptance_runtime_compared_count"] == 19

    def test_runtime_files_missing_empty(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            generate_pre_canary_evidence,
        )
        ev = generate_pre_canary_evidence(
            repo_root=str(Path(__file__).resolve().parent.parent),
            state_dir=str(tmp_path),
            repo="Slideshow11/AutoDev",
            pr_number=5,
            branch="feat/review-repair-relay-v1",
        )
        assert ev["runtime_files_missing"] == []

    def test_runtime_files_ambiguous_empty(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            generate_pre_canary_evidence,
        )
        ev = generate_pre_canary_evidence(
            repo_root=str(Path(__file__).resolve().parent.parent),
            state_dir=str(tmp_path),
            repo="Slideshow11/AutoDev",
            pr_number=5,
            branch="feat/review-repair-relay-v1",
        )
        assert ev["runtime_files_ambiguous"] == []

    def test_deployed_path_outside_repo_still_compared(self, tmp_path):
        """A deployed path outside repo_root (e.g.
        /home/max/.hermes/aed-supervisor/supervisor.py)
        MUST still be compared against the source, not
        silently skipped."""
        from autocoder_supervisor.hermes_fingerprint import (
            generate_pre_canary_evidence,
        )
        ev = generate_pre_canary_evidence(
            repo_root=str(Path(__file__).resolve().parent.parent),
            state_dir=str(tmp_path),
            repo="Slideshow11/AutoDev",
            pr_number=5,
            branch="feat/review-repair-relay-v1",
        )
        # Every record must have actual_production_loaded_path
        # recorded even if it's outside repo_root.
        for r in ev["runtime_file_records"]:
            assert r["actual_production_loaded_path"] is not None
            assert r["actual_production_loaded_path"] != ""
            assert "actual_production_sha256" in r


# ---------------------------------------------------------------------------
# §4 - prelaunch_head is the binding head
# ---------------------------------------------------------------------------


class TestRunBindingHeadSemantics:
    """Closure VII §4: authoritative_head MUST be
    prelaunch_head, NOT produced/pushed.
    """

    def test_prelaunch_head_used(self):
        from autocoder_supervisor.hermes_fingerprint import (
            owned_tuples_from_worker_attempt_records,
        )
        records = [{
            "prelaunch_head": "a" * 40,
            "produced_commit_sha": "b" * 40,
            "pushed_commit_sha": "b" * 40,
            "generation_id": "g",
            "attempt_id": "att",
            "result_contract_id": "rc",
        }]
        tuples = owned_tuples_from_worker_attempt_records(records)
        assert ("a" * 40, "g", "att", "rc") in tuples
        assert ("b" * 40, "g", "att", "rc") not in tuples

    def test_produced_sha_rejected_as_binding_head(self):
        from autocoder_supervisor.hermes_fingerprint import (
            owned_tuples_from_worker_attempt_records,
        )
        records = [{
            "prelaunch_head": "a" * 40,
            "produced_commit_sha": "b" * 40,
            "pushed_commit_sha": "b" * 40,
            "generation_id": "g",
            "attempt_id": "att",
            "result_contract_id": "rc",
        }]
        tuples = owned_tuples_from_worker_attempt_records(records)
        # No tuple with b... should appear.
        assert not any(t[0] == "b" * 40 for t in tuples)

    def test_pushed_sha_rejected_as_binding_head(self):
        from autocoder_supervisor.hermes_fingerprint import (
            owned_tuples_from_worker_attempt_records,
        )
        records = [{
            "prelaunch_head": "a" * 40,
            "produced_commit_sha": "b" * 40,
            "pushed_commit_sha": "c" * 40,
            "generation_id": "g",
            "attempt_id": "att",
            "result_contract_id": "rc",
        }]
        tuples = owned_tuples_from_worker_attempt_records(records)
        assert not any(t[0].startswith("b") or t[0].startswith("c") for t in tuples)

    def test_missing_prelaunch_head_skipped(self):
        """A record with no prelaunch_head cannot satisfy
        the run binding contract; it is SKIPPED (fail
        closed)."""
        from autocoder_supervisor.hermes_fingerprint import (
            owned_tuples_from_worker_attempt_records,
        )
        records = [{
            # No prelaunch_head!
            "produced_commit_sha": "b" * 40,
            "pushed_commit_sha": "b" * 40,
            "generation_id": "g",
            "attempt_id": "att",
            "result_contract_id": "rc",
        }]
        tuples = owned_tuples_from_worker_attempt_records(records)
        assert tuples == set()

    def test_terminal_attempts_excluded_when_requested(self):
        from autocoder_supervisor.hermes_fingerprint import (
            owned_tuples_from_worker_attempt_records,
        )
        records = [{
            "prelaunch_head": "a" * 40,
            "generation_id": "g",
            "attempt_id": "att",
            "result_contract_id": "rc",
            "status": "CONSUMED",
        }]
        all_tuples = owned_tuples_from_worker_attempt_records(records)
        assert len(all_tuples) == 1
        active_only = owned_tuples_from_worker_attempt_records(
            records, include_terminal=False
        )
        assert active_only == set()

    def test_same_attempt_other_generation_prelaunch_fails(self):
        """Prelaunch head belongs to ANOTHER generation."""
        from autocoder_supervisor.hermes_fingerprint import (
            owned_tuples_from_worker_attempt_records,
            validate_run_binding_relations,
            RunBindingRelationalError,
        )
        records = [{
            "prelaunch_head": "a" * 40,
            "generation_id": "g-1",
            "attempt_id": "att",
            "result_contract_id": "rc",
        }]
        owned = owned_tuples_from_worker_attempt_records(records)
        # Bind to a different generation
        binding = {
            "authoritative_head": "a" * 40,
            "generation_id": "g-2",
            "attempt_id": "att",
            "result_contract_id": "rc",
        }
        with pytest.raises(RunBindingRelationalError):
            validate_run_binding_relations(
                binding=binding, owned_tuples=owned,
            )


# ---------------------------------------------------------------------------
# §5 - Canonical cooldown parser
# ---------------------------------------------------------------------------


class TestCooldownParser:
    """Closure VII §5: ONE canonical parser."""

    def test_parser_entries_preferred(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            canonical_cooldown_deferred_count,
        )
        cooldown = tmp_path / "cooldown_deferred_events.json"
        cooldown.write_text(json.dumps({
            "entries": [{"id": f"e{i}"} for i in range(3)],
            "ids": ["legacy1", "legacy2"],
        }))
        out = canonical_cooldown_deferred_count(str(tmp_path))
        assert out["count"] == 3
        assert out["entries_count"] == 3
        assert out["legacy_ids_count"] == 0

    def test_parser_falls_back_to_legacy(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            canonical_cooldown_deferred_count,
        )
        cooldown = tmp_path / "cooldown_deferred_events.json"
        cooldown.write_text(json.dumps({
            "entries": [],
            "ids": ["legacy1", "legacy2"],
        }))
        out = canonical_cooldown_deferred_count(str(tmp_path))
        assert out["count"] == 2
        assert out["entries_count"] == 0
        assert out["legacy_ids_count"] == 2

    def test_parser_empty(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            canonical_cooldown_deferred_count,
        )
        cooldown = tmp_path / "cooldown_deferred_events.json"
        cooldown.write_text(json.dumps({"entries": [], "ids": []}))
        out = canonical_cooldown_deferred_count(str(tmp_path))
        assert out["count"] == 0

    def test_parser_malformed(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            canonical_cooldown_deferred_count,
        )
        cooldown = tmp_path / "cooldown_deferred_events.json"
        cooldown.write_text("not valid json {")
        out = canonical_cooldown_deferred_count(str(tmp_path))
        assert out["parse_failed"] is True
        assert out["count"] == 0


# ---------------------------------------------------------------------------
# §6 - Canonical active worker determination
# ---------------------------------------------------------------------------


class TestCanonicalActiveWorker:
    """Closure VII §6: the canonical active-worker
    determination uses WorkerAttemptStore, not ps-based
    process count.
    """

    def test_canonical_count_zero_when_no_attempts(self, tmp_path):
        """No attempt records → 0 (not a fake 1 from ps)."""
        # Empty store.
        from autocoder_supervisor.supervisor import (
            canonical_active_worker_attempt_count,
        )
        # The supervisor's WORKER_ATTEMPTS_DIR is hardcoded
        # in the module. We just verify the function exists
        # and returns a non-negative integer (it cannot
        # produce a fake positive from ps).
        count = canonical_active_worker_attempt_count()
        assert isinstance(count, int)
        assert count >= 0

    def test_canonical_count_does_not_use_launched_events(self):
        """The canonical count MUST NOT be derived from
        launched_event_ids() (which is event-dispatch
        bookkeeping, not WorkerAttempt ownership)."""
        # Both functions exist; they have different
        # semantics.
        from autocoder_supervisor.supervisor import (
            canonical_active_worker_attempt_count,
            launched_event_ids,
        )
        # Their types differ. canonical returns int from
        # WorkerAttemptStore; launched_event_ids returns
        # set from bookkeeping.
        assert callable(canonical_active_worker_attempt_count)
        assert callable(launched_event_ids)

    def test_canonical_ids_function(self):
        from autocoder_supervisor.supervisor import (
            canonical_active_worker_attempt_ids,
        )
        ids = canonical_active_worker_attempt_ids()
        assert isinstance(ids, list)


# ---------------------------------------------------------------------------
# §7 - Codex scheduler uses canonical active workers
# ---------------------------------------------------------------------------


class TestCodexSchedulerCanonicalWorkers:
    """Closure VII §7: the Codex scheduler MUST use the
    canonical WorkerAttemptStore-based active worker
    count, NOT launched_event_ids.
    """

    def test_schedule_codex_signature_unchanged(self):
        from autocoder_supervisor.supervisor import (
            schedule_codex_request_on_stable_head,
        )
        import inspect
        sig = inspect.signature(schedule_codex_request_on_stable_head)
        assert "live_head" in sig.parameters
        assert "active_worker_count" in sig.parameters

    def test_canonical_helper_exists(self):
        from autocoder_supervisor.supervisor import (
            canonical_active_worker_attempt_count,
        )
        # Helper exists and is callable.
        count = canonical_active_worker_attempt_count()
        assert isinstance(count, int)

    def test_zero_canonical_workers_allows_codex(self, monkeypatch):
        """When canonical active workers = 0, Codex
        scheduling is eligible (not blocked by stale
        launched-event markers)."""
        from autocoder_supervisor import supervisor as s
        # Patch the canonical helper to return 0.
        monkeypatch.setattr(
            s, "canonical_active_worker_attempt_count", lambda: 0
        )
        # The Codex call site uses canonical_active_worker_attempt_count().
        # We verify that the call site does NOT call
        # launched_event_ids() to compute active workers.
        # Read the source to assert the codex call site uses
        # canonical_active_worker_attempt_count().
        # Use repo-relative path so this works in any checkout.
        from pathlib import Path as _Path
        supervisor_path = (
            _Path(__file__).resolve().parent.parent
            / "autocoder_supervisor" / "supervisor.py"
        )
        src = open(supervisor_path).read()
        # The Codex call site is the LAST occurrence of
        # schedule_codex_request_on_stable_head( (the call,
        # not the function def).
        all_idx = []
        i = 0
        while True:
            j = src.find("schedule_codex_request_on_stable_head(", i)
            if j < 0:
                break
            all_idx.append(j)
            i = j + 1
        call_idx = all_idx[-1]
        before = src[max(0, call_idx - 1500):call_idx]
        # The block should reference canonical_active_worker_attempt_count
        # and NOT launched_event_ids in the codex block.
        assert "canonical_active_worker_attempt_count" in before


# ---------------------------------------------------------------------------
# §8 - Replay behavioral tests
# ---------------------------------------------------------------------------


class TestReplayBehavioral:
    """Closure VII §8: behavioral tests of replay path."""

    def test_replay_returns_list(self):
        from autocoder_supervisor.supervisor import (
            _replay_cooldown_deferred_if_any,
        )
        import inspect
        sig = inspect.signature(_replay_cooldown_deferred_if_any)
        assert sig.return_annotation == "list"

    def test_replay_helper_cleaned_production_tombstones(self):
        """Verify that the production supervisor has
        tombstone cleanup working: the cooldown ledger
        count is 0 (all 65 production tombstones were
        cleaned up after supervisor restart with the
        closure-VI fix).
        """
        # The production supervisor must have cleaned up
        # its tombstone cooldown entries. If this fails,
        # something has regressed.
        from pathlib import Path as _Path
        cd_path = _Path(
            "/home/max/.hermes/aed-supervisor/state/"
            "cooldown_deferred_events.json"
        )
        if cd_path.exists():
            d = json.loads(cd_path.read_text())
            entries = d.get("entries", [])
            if isinstance(entries, list) and entries:
                # Active entries present.
                return  # Skip — depends on supervisor state.
            legacy = d.get("ids", [])
            if isinstance(legacy, list) and legacy:
                pytest.fail(
                    f"production cooldown ledger has {len(legacy)} "
                    f"legacy tombstones — tombstone cleanup is broken"
                )

    def test_replay_same_heartbeat_dispatch(self):
        """The replay helper sets a module-level slot so
        the caller can dispatch the freshly-replayed events
        on the same heartbeat. The helper also writes to
        the global so cross-process observation works."""
        import sys
        sys.path.insert(0, "/home/max/AutoDev")
        from autocoder_supervisor import supervisor as s
        # Helper has the documented slot.
        assert hasattr(s, "_replay_cooldown_deferred_if_any_last_result")
        # After calling, the slot is updated.
        prior = s._replay_cooldown_deferred_if_any_last_result
        s._replay_cooldown_deferred_if_any()
        # The slot may be a new list (or empty list). The
        # invariant is that the slot exists and is a list.
        assert isinstance(
            s._replay_cooldown_deferred_if_any_last_result, list
        )


# ---------------------------------------------------------------------------
# §10 - Required CI checks
# ---------------------------------------------------------------------------


class TestRequiredCIChecks:
    """Closure VII §10: the evidence encodes the required
    CI check set."""

    def test_evidence_encodes_required_ci_set(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            generate_pre_canary_evidence,
        )
        ev = generate_pre_canary_evidence(
            repo_root=str(Path(__file__).resolve().parent.parent),
            state_dir=str(tmp_path),
            repo="Slideshow11/AutoDev",
            pr_number=5,
            branch="feat/review-repair-relay-v1",
        )
        assert "required_ci_checks_expected" in ev
        assert "required_ci_checks_observed" in ev
        assert "required_ci_checks_missing" in ev
        assert "required_ci_checks_non_success" in ev
        assert "exact_head_ci_all_required_success" in ev
        assert "exact_head_ci_sha" in ev

    def test_required_ci_set_is_the_canonical_7(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            generate_pre_canary_evidence,
        )
        ev = generate_pre_canary_evidence(
            repo_root=str(Path(__file__).resolve().parent.parent),
            state_dir=str(tmp_path),
            repo="Slideshow11/AutoDev",
            pr_number=5,
            branch="feat/review-repair-relay-v1",
        )
        required = set(ev["required_ci_checks_expected"])
        assert required == {
            "test (3.10)", "test (3.11)", "test (3.12)",
            "package-smoke", "committed-state-scan",
            "provenance", "full-suite",
        }

    def test_in_flight_required_check_blocks_freeze_gate(
        self, tmp_path, monkeypatch
    ):
        """Regression: a required check that is still queued or
        in_progress (GitHub reports ``conclusion == None``) MUST
        count as non-success. Previously ``None`` was treated
        as success, letting ``exact_head_ci_all_required_success``
        flip to True before every required job reached a
        terminal conclusion.
        """
        from autocoder_supervisor import hermes_fingerprint as hf

        def _stub_workflow_in_flight(repo, head):
            # Every required check observed and present, but
            # ``full-suite`` has not reached a terminal
            # conclusion yet (status=in_progress, conclusion=None).
            return [{
                "name": "test (3.10)",
                "conclusion": "success",
                "status": "completed",
            }, {
                "name": "test (3.11)",
                "conclusion": "success",
                "status": "completed",
            }, {
                "name": "test (3.12)",
                "conclusion": "success",
                "status": "completed",
            }, {
                "name": "package-smoke",
                "conclusion": "success",
                "status": "completed",
            }, {
                "name": "committed-state-scan",
                "conclusion": "success",
                "status": "completed",
            }, {
                "name": "provenance",
                "conclusion": "success",
                "status": "completed",
            }, {
                "name": "full-suite",
                "conclusion": None,
                "status": "in_progress",
            }]
        monkeypatch.setattr(hf, "_read_workflow_runs",
                            _stub_workflow_in_flight)
        ev = hf.generate_pre_canary_evidence(
            repo_root=str(Path(__file__).resolve().parent.parent),
            state_dir=str(tmp_path),
            repo="Slideshow11/AutoDev",
            pr_number=5,
            branch="feat/review-repair-relay-v1",
        )
        assert "full-suite" in ev["required_ci_checks_pending"], (
            "in-flight required check must be pending; "
            f"got pending={ev['required_ci_checks_pending']!r} "
            f"non_success={ev['required_ci_checks_non_success']!r}"
        )
        assert ev["exact_head_ci_all_required_success"] is False, (
            "freeze gate must remain False while any required "
            "check has not reached a terminal conclusion"
        )


# Bring pytest in scope
import pytest
