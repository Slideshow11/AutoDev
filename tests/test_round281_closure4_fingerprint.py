"""Closure IV tests for hermes_fingerprint.py:

  §6 — Static fingerprint must change when any acceptance
        runtime module changes.
  §7 — Static fingerprint covers the full acceptance runtime
        file set.
  §8 — Static scope (repo/PR/branch/state-dir) is frozen
        separately from dynamic run binding.
  §9 — Run binding has a strict required schema (no empty,
        no missing, no malformed).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# §6 — Static fingerprint changes for runtime module mutations
# ---------------------------------------------------------------------------


def _write_run(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class TestStaticFingerprintRuntimeCoverage:
    """The static fingerprint MUST change when ANY acceptance
    runtime module changes."""

    def test_static_fingerprint_changes_when_supervisor_changes(
        self, tmp_path, monkeypatch,
    ):
        from autocoder_supervisor import hermes_fingerprint as hf
        cfg = tmp_path / "config.yaml"
        cfg.write_text("a: 1\n")
        profile = tmp_path / "profile.yaml"
        profile.write_text("b: 2\n")
        sup = tmp_path / "supervisor.py"
        sup.write_text("# sup v1\n")
        shim = tmp_path / "hermes"
        shim.write_text("#!/bin/sh\n")
        worker_session = tmp_path / "worker_session.py"
        worker_session.write_text("# ws v1\n")
        aed_worker_wrapper = tmp_path / "aed_worker_wrapper.py"
        aed_worker_wrapper.write_text("# aw v1\n")
        directive_bridge = tmp_path / "directive_bridge.py"
        directive_bridge.write_text("# db v1\n")
        _directive_prompt = tmp_path / "_directive_prompt.py"
        _directive_prompt.write_text("# dp v1\n")
        provenance_maintenance = tmp_path / "provenance_maintenance.py"
        provenance_maintenance.write_text("# pm v1\n")
        hermes_fingerprint_file = tmp_path / "hermes_fingerprint.py"
        hermes_fingerprint_file.write_text("# hf v1\n")
        worker_attempt = tmp_path / "worker_attempt.py"
        worker_attempt.write_text("# wa v1\n")
        review_repair_relay = tmp_path / "review_repair_relay.py"
        review_repair_relay.write_text("# rr v1\n")
        merge_authorize = tmp_path / "merge_authorize.py"
        merge_authorize.write_text("# ma v1\n")
        provider_lifecycle = tmp_path / "provider_lifecycle.py"
        provider_lifecycle.write_text("# pl v1\n")
        canonical_state = tmp_path / "canonical_state.py"
        canonical_state.write_text("# cs v1\n")
        inputs = [
            ("global_config", cfg),
            ("profile_aed_builder_config", profile),
            ("supervisor_entrypoint", sup),
            ("hermes_cli_shim", shim),
            ("worker_session", worker_session),
            ("aed_worker_wrapper", aed_worker_wrapper),
            ("directive_bridge", directive_bridge),
            ("_directive_prompt", _directive_prompt),
            ("provenance_maintenance", provenance_maintenance),
            ("hermes_fingerprint_file", hermes_fingerprint_file),
            ("worker_attempt", worker_attempt),
            ("review_repair_relay", review_repair_relay),
            ("merge_authorize", merge_authorize),
            ("provider_lifecycle", provider_lifecycle),
            ("canonical_state", canonical_state),
        ]
        from autocoder_supervisor.hermes_fingerprint import (
            compute_static_hermes_environment_fingerprint,
        )
        fp1 = compute_static_hermes_environment_fingerprint(
            inputs=list(inputs)
        )
        # Mutate supervisor only.
        sup.write_text("# sup v2 — changed\n")
        fp2 = compute_static_hermes_environment_fingerprint(
            inputs=list(inputs)
        )
        assert fp1["fingerprint"] != fp2["fingerprint"], (
            "supervisor.py change MUST change static fingerprint"
        )

    def test_static_fingerprint_changes_when_worker_session_changes(
        self, tmp_path,
    ):
        from autocoder_supervisor.hermes_fingerprint import (
            compute_static_hermes_environment_fingerprint,
        )
        inputs = []
        for label in (
            "global_config", "profile_aed_builder_config",
            "supervisor_entrypoint", "hermes_cli_shim",
            "worker_session", "aed_worker_wrapper",
            "directive_bridge", "_directive_prompt",
            "provenance_maintenance", "hermes_fingerprint_file",
            "worker_attempt", "review_repair_relay",
            "merge_authorize", "provider_lifecycle",
            "canonical_state",
        ):
            p = tmp_path / f"{label}.x"
            p.write_text("x")
            inputs.append((label, p))
        fp1 = compute_static_hermes_environment_fingerprint(
            inputs=list(inputs)
        )
        for label, p in inputs:
            if label == "worker_session":
                p.write_text("x changed")
                break
        fp2 = compute_static_hermes_environment_fingerprint(
            inputs=list(inputs)
        )
        assert fp1["fingerprint"] != fp2["fingerprint"]

    def test_static_fingerprint_changes_when_aed_worker_wrapper_changes(
        self, tmp_path,
    ):
        from autocoder_supervisor.hermes_fingerprint import (
            compute_static_hermes_environment_fingerprint,
        )
        inputs = []
        for label in (
            "global_config", "profile_aed_builder_config",
            "supervisor_entrypoint", "hermes_cli_shim",
            "worker_session", "aed_worker_wrapper",
            "directive_bridge", "_directive_prompt",
            "provenance_maintenance", "hermes_fingerprint_file",
            "worker_attempt", "review_repair_relay",
            "merge_authorize", "provider_lifecycle",
            "canonical_state",
        ):
            p = tmp_path / f"{label}.x"
            p.write_text("x")
            inputs.append((label, p))
        fp1 = compute_static_hermes_environment_fingerprint(
            inputs=list(inputs)
        )
        for label, p in inputs:
            if label == "aed_worker_wrapper":
                p.write_text("x changed")
                break
        fp2 = compute_static_hermes_environment_fingerprint(
            inputs=list(inputs)
        )
        assert fp1["fingerprint"] != fp2["fingerprint"]

    def test_static_fingerprint_changes_when_provenance_maintenance_changes(
        self, tmp_path,
    ):
        from autocoder_supervisor.hermes_fingerprint import (
            compute_static_hermes_environment_fingerprint,
        )
        inputs = []
        for label in (
            "global_config", "profile_aed_builder_config",
            "supervisor_entrypoint", "hermes_cli_shim",
            "worker_session", "aed_worker_wrapper",
            "directive_bridge", "_directive_prompt",
            "provenance_maintenance", "hermes_fingerprint_file",
            "worker_attempt", "review_repair_relay",
            "merge_authorize", "provider_lifecycle",
            "canonical_state",
        ):
            p = tmp_path / f"{label}.x"
            p.write_text("x")
            inputs.append((label, p))
        fp1 = compute_static_hermes_environment_fingerprint(
            inputs=list(inputs)
        )
        for label, p in inputs:
            if label == "provenance_maintenance":
                p.write_text("x changed")
                break
        fp2 = compute_static_hermes_environment_fingerprint(
            inputs=list(inputs)
        )
        assert fp1["fingerprint"] != fp2["fingerprint"]


# ---------------------------------------------------------------------------
# §8 — Static scope vs dynamic run binding
# ---------------------------------------------------------------------------


class TestStaticScopeSeparateFromRunBinding:
    def test_static_scope_includes_repo_identity(self):
        from autocoder_supervisor.hermes_fingerprint import (
            compute_static_acceptance_scope_fingerprint,
            STATIC_SCOPE_KEYS,
        )
        # Required static-scope keys.
        required = {
            "repository_owner",
            "repository_name",
            "pr_number",
            "expected_branch",
            "production_working_checkout",
            "supervisor_state_directory",
            "supervisor_home",
            "hermes_binary_path",
            "required_providers",
            "optional_providers",
            "provider_independence",
        }
        assert required.issubset(set(STATIC_SCOPE_KEYS)), (
            f"STATIC_SCOPE_KEYS missing required: "
            f"{required - set(STATIC_SCOPE_KEYS)}"
        )

    def test_aed_pr_number_is_static_scope_not_dynamic(self):
        """Closure IV §8: AED_PR_NUMBER is NOT a normal
        per-generation dynamic identity. PR #5 must remain
        PR #5."""
        from autocoder_supervisor.hermes_fingerprint import (
            STATIC_SCOPE_KEYS,
            _RUN_BINDING_KEYS,
        )
        assert "pr_number" in STATIC_SCOPE_KEYS
        assert "pr_number" not in _RUN_BINDING_KEYS

    def test_repository_identity_in_static_scope(self):
        from autocoder_supervisor.hermes_fingerprint import (
            STATIC_SCOPE_KEYS,
            _RUN_BINDING_KEYS,
        )
        assert "repository_owner" in STATIC_SCOPE_KEYS
        assert "repository_name" in STATIC_SCOPE_KEYS
        assert "repository_owner" not in _RUN_BINDING_KEYS
        assert "repository_name" not in _RUN_BINDING_KEYS


# ---------------------------------------------------------------------------
# §9 — Strict run-binding schema
# ---------------------------------------------------------------------------


class TestRunBindingSchema:
    def _binding(self, **overrides):
        b = {
            "authoritative_head": "a" * 40,
            "generation_id": "gen-1",
            "attempt_id": "att-1",
            "result_contract_id": "rc-1",
        }
        b.update(overrides)
        return b

    def test_empty_binding_rejected(self):
        from autocoder_supervisor.hermes_fingerprint import (
            compute_run_binding_digest,
            RunBindingSchemaError,
        )
        with pytest.raises(RunBindingSchemaError):
            compute_run_binding_digest(binding={})

    def test_missing_head_rejected(self):
        from autocoder_supervisor.hermes_fingerprint import (
            compute_run_binding_digest,
            RunBindingSchemaError,
        )
        b = self._binding()
        del b["authoritative_head"]
        with pytest.raises(RunBindingSchemaError):
            compute_run_binding_digest(binding=b)

    def test_missing_generation_rejected(self):
        from autocoder_supervisor.hermes_fingerprint import (
            compute_run_binding_digest,
            RunBindingSchemaError,
        )
        b = self._binding()
        del b["generation_id"]
        with pytest.raises(RunBindingSchemaError):
            compute_run_binding_digest(binding=b)

    def test_missing_attempt_rejected(self):
        from autocoder_supervisor.hermes_fingerprint import (
            compute_run_binding_digest,
            RunBindingSchemaError,
        )
        b = self._binding()
        del b["attempt_id"]
        with pytest.raises(RunBindingSchemaError):
            compute_run_binding_digest(binding=b)

    def test_missing_result_contract_rejected(self):
        from autocoder_supervisor.hermes_fingerprint import (
            compute_run_binding_digest,
            RunBindingSchemaError,
        )
        b = self._binding()
        del b["result_contract_id"]
        with pytest.raises(RunBindingSchemaError):
            compute_run_binding_digest(binding=b)

    def test_empty_head_rejected(self):
        from autocoder_supervisor.hermes_fingerprint import (
            compute_run_binding_digest,
            RunBindingSchemaError,
        )
        with pytest.raises(RunBindingSchemaError):
            compute_run_binding_digest(
                binding=self._binding(authoritative_head="")
            )

    def test_empty_result_contract_rejected(self):
        from autocoder_supervisor.hermes_fingerprint import (
            compute_run_binding_digest,
            RunBindingSchemaError,
        )
        with pytest.raises(RunBindingSchemaError):
            compute_run_binding_digest(
                binding=self._binding(result_contract_id="")
            )

    def test_malformed_head_rejected(self):
        from autocoder_supervisor.hermes_fingerprint import (
            compute_run_binding_digest,
            RunBindingSchemaError,
        )
        with pytest.raises(RunBindingSchemaError):
            compute_run_binding_digest(
                binding=self._binding(authoritative_head="not-a-sha")
            )

    def test_valid_binding_produces_deterministic_digest(self):
        from autocoder_supervisor.hermes_fingerprint import (
            compute_run_binding_digest,
        )
        b = self._binding()
        d1 = compute_run_binding_digest(binding=b)
        d2 = compute_run_binding_digest(binding=b)
        assert d1["digest"] == d2["digest"]
        assert isinstance(d1["digest"], str)
        assert len(d1["digest"]) == 64

    def test_head_change_changes_digest(self):
        from autocoder_supervisor.hermes_fingerprint import (
            compute_run_binding_digest,
        )
        d1 = compute_run_binding_digest(binding=self._binding())
        d2 = compute_run_binding_digest(
            binding=self._binding(authoritative_head="b" * 40)
        )
        assert d1["digest"] != d2["digest"]

    def test_attempt_change_changes_digest(self):
        from autocoder_supervisor.hermes_fingerprint import (
            compute_run_binding_digest,
        )
        d1 = compute_run_binding_digest(binding=self._binding())
        d2 = compute_run_binding_digest(
            binding=self._binding(attempt_id="att-2")
        )
        assert d1["digest"] != d2["digest"]

    def test_static_environment_unchanged(self):
        """Static environment hash unchanged when only the
        static-input byte identity is unchanged."""
        from autocoder_supervisor.hermes_fingerprint import (
            compute_static_hermes_environment_fingerprint,
            compute_run_binding_digest,
        )
        cfg = Path("/tmp/static_env_test.yaml")
        cfg.write_text("a: 1\n")
        inputs = [("global_config", cfg)]
        a = compute_static_hermes_environment_fingerprint(
            inputs=list(inputs)
        )
        b = compute_static_hermes_environment_fingerprint(
            inputs=list(inputs)
        )
        assert a["fingerprint"] == b["fingerprint"]


# ---------------------------------------------------------------------------
# §7 — Static fingerprint source-of-truth
# ---------------------------------------------------------------------------


class TestStaticRuntimeFileSourceOfTruth:
    """The static runtime file set is derived from a canonical
    declared acceptance-runtime inventory (not a hand-maintained
    list)."""

    def test_static_runtime_inventory_in_production_manifest(self):
        """The supervisor source declares the canonical
        acceptance-runtime inventory via the
        acceptance_runtime_files list. The static fingerprint
        helper iterates this declared set, not a hard-coded list.
        """
        from autocoder_supervisor.hermes_fingerprint import (
            ACCEPTANCE_RUNTIME_INVENTORY,
        )
        # The inventory MUST include the runtime modules
        # declared in the directive.
        required = [
            "supervisor.py",
            "worker_session.py",
            "aed_worker_wrapper.py",
            "directive_bridge.py",
            "_directive_prompt.py",
            "provenance_maintenance.py",
            "hermes_fingerprint.py",
            "worker_attempt.py",
            "review_repair_relay.py",
        ]
        for r in required:
            assert r in ACCEPTANCE_RUNTIME_INVENTORY, (
                f"acceptance runtime inventory missing {r}"
            )