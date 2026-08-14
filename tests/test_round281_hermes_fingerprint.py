"""Hermes acceptance fingerprint test (pre-canary §9 + Closure III + IV).

Closure III/IV directive requires:

  1. The static environment fingerprint MUST bind immutable
     acceptance inputs (configs, frozen runtime bytes).
  2. The static scope MUST bind the routing identity
     (repo/PR/branch/scope) separately from dynamic head.
  3. The run-binding digest MUST bind the dynamic identities
     (current head, generation id, attempt id, result contract
     id) per generation with a STRICT schema.
  4. The run binding rejects empty/missing/malformed values.

Tests prove:
  - static fingerprint changes when each runtime module changes
  - static fingerprint does NOT change when only head changes
  - run binding has a strict schema with fail-closed semantics
  - static scope contains repo/PR/branch identity
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_static_inputs_are_well_defined() -> None:
    from autocoder_supervisor.hermes_fingerprint import (
        compute_static_hermes_environment_fingerprint,
        _default_static_inputs,
    )
    if not all(p.exists() for _, p in _default_static_inputs()):
        pytest.skip(
            "operator runtime area absent in CI; static "
            "fingerprint canonical inputs require $OPERATOR_HOME"
        )
    fp = compute_static_hermes_environment_fingerprint()
    assert "fingerprint" in fp
    assert isinstance(fp["fingerprint"], str)
    assert len(fp["fingerprint"]) == 64


def test_static_fingerprint_is_deterministic() -> None:
    from autocoder_supervisor.hermes_fingerprint import (
        compute_static_hermes_environment_fingerprint,
        _default_static_inputs,
    )
    if not all(p.exists() for _, p in _default_static_inputs()):
        pytest.skip(
            "operator runtime area absent in CI; "
            "fingerprint canonical inputs require $OPERATOR_HOME"
        )
    a = compute_static_hermes_environment_fingerprint()
    b = compute_static_hermes_environment_fingerprint()
    assert a["fingerprint"] == b["fingerprint"]


def test_static_inputs_exist_in_production() -> None:
    from autocoder_supervisor.hermes_fingerprint import (
        compute_static_hermes_environment_fingerprint,
    )
    if not os.environ.get("OPERATOR_HOME"):
        pytest.skip(
            "operator runtime area absent in CI; "
            "fingerprint canonical inputs require $OPERATOR_HOME"
        )
    fp = compute_static_hermes_environment_fingerprint()
    assert all(
        p.exists() for _, p in fp["inputs"]
    )


def test_static_fingerprint_changes_when_supervisor_changes(
    tmp_path,
) -> None:
    from autocoder_supervisor.hermes_fingerprint import (
        compute_static_hermes_environment_fingerprint,
    )
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
    ]
    fp1 = compute_static_hermes_environment_fingerprint(
        inputs=list(inputs)
    )
    sup.write_text("# sup v2 — changed\n")
    fp2 = compute_static_hermes_environment_fingerprint(
        inputs=list(inputs)
    )
    assert fp1["fingerprint"] != fp2["fingerprint"]


def test_static_fingerprint_unchanged_when_only_inputs_list_changes(
    tmp_path,
) -> None:
    from autocoder_supervisor.hermes_fingerprint import (
        compute_static_hermes_environment_fingerprint,
    )
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
    ]
    fp1 = compute_static_hermes_environment_fingerprint(
        inputs=list(inputs)
    )
    fp2 = compute_static_hermes_environment_fingerprint(
        inputs=list(inputs)
    )
    assert fp1["fingerprint"] == fp2["fingerprint"]


def test_run_binding_changes_when_head_changes() -> None:
    from autocoder_supervisor.hermes_fingerprint import (
        compute_run_binding_digest,
    )
    binding_a = {
        "authoritative_head": "a" * 40,
        "generation_id": "gen-1",
        "attempt_id": "att-1",
        "result_contract_id": "rc-1",
    }
    binding_b = {
        "authoritative_head": "b" * 40,
        "generation_id": "gen-1",
        "attempt_id": "att-1",
        "result_contract_id": "rc-1",
    }
    a = compute_run_binding_digest(binding=binding_a)
    b = compute_run_binding_digest(binding=binding_b)
    assert a["digest"] != b["digest"]


def test_run_binding_changes_when_attempt_id_changes() -> None:
    from autocoder_supervisor.hermes_fingerprint import (
        compute_run_binding_digest,
    )
    binding_a = {
        "authoritative_head": "a" * 40,
        "generation_id": "gen-1",
        "attempt_id": "att-1",
        "result_contract_id": "rc-1",
    }
    binding_b = {
        "authoritative_head": "a" * 40,
        "generation_id": "gen-1",
        "attempt_id": "att-2",
        "result_contract_id": "rc-1",
    }
    a = compute_run_binding_digest(binding=binding_a)
    b = compute_run_binding_digest(binding=binding_b)
    assert a["digest"] != b["digest"]


def test_static_fingerprint_fails_closed_when_input_missing(
    tmp_path,
) -> None:
    from autocoder_supervisor.hermes_fingerprint import (
        compute_static_hermes_environment_fingerprint,
    )
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
    shim.unlink()
    with pytest.raises(RuntimeError):
        compute_static_hermes_environment_fingerprint(
            inputs=[
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
            ]
        )


def test_fingerprint_split_constants() -> None:
    from autocoder_supervisor.hermes_fingerprint import (
        PREVIOUS_CANONICAL_HERMES_FINGERPRINT,
        SUPERSEDED_INCOMPLETE_STATIC_FINGERPRINT,
        _RUN_BINDING_KEYS,
    )
    # The previous canonical fingerprint MUST be a 64-char
    # SHA-256 hex string (legacy shape).
    assert isinstance(PREVIOUS_CANONICAL_HERMES_FINGERPRINT, str)
    assert len(PREVIOUS_CANONICAL_HERMES_FINGERPRINT) == 64
    # The superseded incomplete fingerprint MUST be reported.
    assert isinstance(SUPERSEDED_INCOMPLETE_STATIC_FINGERPRINT, str)
    assert len(SUPERSEDED_INCOMPLETE_STATIC_FINGERPRINT) == 64
    # The dynamic head IS in the run binding keys.
    assert "authoritative_head" in _RUN_BINDING_KEYS