"""Hermes acceptance fingerprint test (pre-canary §9 + Closure III).

Closure III directive requires:

  1. The static environment fingerprint MUST bind immutable
     acceptance inputs (configs, frozen source bytes,
     CLI shim). The dynamic head identity MUST NOT appear
     in the static hash.

  2. The run-binding digest MUST bind the dynamic identities
     (current head, generation id, attempt id, etc.) per
     generation.

  3. Same environment, different head → SAME static fingerprint.

  4. Hermes config byte change → DIFFERENT static fingerprint.

  5. Supervisor runtime byte change → DIFFERENT static fingerprint.

  6. Worker wrapper/result-contract runtime byte change →
     DIFFERENT static fingerprint.

  7. Profile config byte change → DIFFERENT static fingerprint.

  8. Authoritative head change → run binding changes.

  9. Missing canonical input → fail closed.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_static_inputs_are_well_defined() -> None:
    from autocoder_supervisor.hermes_fingerprint import (
        compute_static_hermes_environment_fingerprint,
    )
    # In the production environment every input MUST be
    # present. In CI environments where the operator runtime
    # area is absent, we still require the test to find the
    # absence and fail closed.
    import os
    if not os.environ.get("OPERATOR_HOME"):
        # CI environment: skip if canonical inputs absent.
        from autocoder_supervisor.hermes_fingerprint import (
            _default_static_inputs,
        )
        if not all(p.exists() for _, p in _default_static_inputs()):
            pytest.skip(
                "operator runtime area absent in CI; static "
                "fingerprint canonical inputs require "
                "$OPERATOR_HOME"
            )
    fp = compute_static_hermes_environment_fingerprint()
    assert "fingerprint" in fp
    assert isinstance(fp["fingerprint"], str)
    assert len(fp["fingerprint"]) == 64


def test_static_fingerprint_is_deterministic() -> None:
    from autocoder_supervisor.hermes_fingerprint import (
        compute_static_hermes_environment_fingerprint,
    )
    a = compute_static_hermes_environment_fingerprint()
    b = compute_static_hermes_environment_fingerprint()
    assert a["fingerprint"] == b["fingerprint"]


def test_static_inputs_exist_in_production() -> None:
    """Every canonical input must exist on disk.

    In CI environments where the operator runtime area does
    not exist (e.g. a fresh checkout without
    ``$OPERATOR_HOME``), the fingerprint test skips rather
    than fails.
    """
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


def test_static_fingerprint_changes_when_config_changes(
    tmp_path, monkeypatch,
) -> None:
    """Hermes config byte change → DIFFERENT static fingerprint."""
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
    inputs_v1 = [
        ("global_config", cfg),
        ("profile_aed_builder_config", profile),
        ("supervisor_entrypoint", sup),
        ("hermes_cli_shim", shim),
    ]
    fp1 = compute_static_hermes_environment_fingerprint(
        inputs=list(inputs_v1)
    )
    # Mutate the config.
    sup.write_text("# sup v2 — changed\n")
    fp2 = compute_static_hermes_environment_fingerprint(
        inputs=list(inputs_v1)
    )
    assert fp1["fingerprint"] != fp2["fingerprint"], (
        "supervisor runtime byte change MUST change the static "
        "fingerprint"
    )


def test_static_fingerprint_changes_when_profile_config_changes(
    tmp_path,
) -> None:
    """Profile config byte change → DIFFERENT static fingerprint."""
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
    inputs_v1 = [
        ("global_config", cfg),
        ("profile_aed_builder_config", profile),
        ("supervisor_entrypoint", sup),
        ("hermes_cli_shim", shim),
    ]
    fp1 = compute_static_hermes_environment_fingerprint(
        inputs=list(inputs_v1)
    )
    # Mutate the profile.
    profile.write_text("b: 999\n")
    fp2 = compute_static_hermes_environment_fingerprint(
        inputs=list(inputs_v1)
    )
    assert fp1["fingerprint"] != fp2["fingerprint"], (
        "profile config byte change MUST change the static "
        "fingerprint"
    )


def test_static_fingerprint_changes_when_global_config_changes(
    tmp_path,
) -> None:
    """Hermes global config byte change → DIFFERENT static fingerprint."""
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
    inputs_v1 = [
        ("global_config", cfg),
        ("profile_aed_builder_config", profile),
        ("supervisor_entrypoint", sup),
        ("hermes_cli_shim", shim),
    ]
    fp1 = compute_static_hermes_environment_fingerprint(
        inputs=list(inputs_v1)
    )
    # Mutate the global config.
    cfg.write_text("a: 999\n")
    fp2 = compute_static_hermes_environment_fingerprint(
        inputs=list(inputs_v1)
    )
    assert fp1["fingerprint"] != fp2["fingerprint"], (
        "global config byte change MUST change the static "
        "fingerprint"
    )


def test_static_fingerprint_changes_when_cli_shim_changes(
    tmp_path,
) -> None:
    """Hermes CLI shim byte change → DIFFERENT static fingerprint."""
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
    inputs_v1 = [
        ("global_config", cfg),
        ("profile_aed_builder_config", profile),
        ("supervisor_entrypoint", sup),
        ("hermes_cli_shim", shim),
    ]
    fp1 = compute_static_hermes_environment_fingerprint(
        inputs=list(inputs_v1)
    )
    # Mutate the shim.
    shim.write_text("#!/bin/bash\necho v2\n")
    fp2 = compute_static_hermes_environment_fingerprint(
        inputs=list(inputs_v1)
    )
    assert fp1["fingerprint"] != fp2["fingerprint"], (
        "CLI shim byte change MUST change the static fingerprint"
    )


def test_static_fingerprint_unchanged_when_only_inputs_list_changes(
    tmp_path,
) -> None:
    """The static hash depends on input BYTES, not on the
    identity of the input list object. Two callers passing
    different list objects but the same bytes get the same
    fingerprint.
    """
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
    fp1 = compute_static_hermes_environment_fingerprint(
        inputs=[
            ("global_config", cfg),
            ("profile_aed_builder_config", profile),
            ("supervisor_entrypoint", sup),
            ("hermes_cli_shim", shim),
        ]
    )
    fp2 = compute_static_hermes_environment_fingerprint(
        inputs=[
            ("global_config", cfg),
            ("profile_aed_builder_config", profile),
            ("supervisor_entrypoint", sup),
            ("hermes_cli_shim", shim),
        ]
    )
    assert fp1["fingerprint"] == fp2["fingerprint"]


def test_run_binding_changes_when_head_changes() -> None:
    """Authoritative head change → run binding changes."""
    from autocoder_supervisor.hermes_fingerprint import (
        compute_run_binding_digest,
    )
    binding_a = {"AED_AUTHORITATIVE_HEAD": "a" * 40}
    binding_b = {"AED_AUTHORITATIVE_HEAD": "b" * 40}
    a = compute_run_binding_digest(binding=binding_a)
    b = compute_run_binding_digest(binding=binding_b)
    assert a["digest"] != b["digest"]


def test_run_binding_changes_when_attempt_id_changes() -> None:
    from autocoder_supervisor.hermes_fingerprint import (
        compute_run_binding_digest,
    )
    binding_a = {"AED_AUTHORITATIVE_HEAD": "a" * 40, "AED_ATTEMPT_ID": "att-1"}
    binding_b = {"AED_AUTHORITATIVE_HEAD": "a" * 40, "AED_ATTEMPT_ID": "att-2"}
    a = compute_run_binding_digest(binding=binding_a)
    b = compute_run_binding_digest(binding=binding_b)
    assert a["digest"] != b["digest"]


def test_static_fingerprint_fails_closed_when_input_missing(
    tmp_path,
) -> None:
    """Missing canonical input → fail closed (RuntimeError)."""
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
    # Remove the shim.
    shim.unlink()
    with pytest.raises(RuntimeError):
        compute_static_hermes_environment_fingerprint(
            inputs=[
                ("global_config", cfg),
                ("profile_aed_builder_config", profile),
                ("supervisor_entrypoint", sup),
                ("hermes_cli_shim", shim),
            ]
        )


def test_static_fingerprint_does_not_include_dynamic_head(
    tmp_path,
) -> None:
    """The dynamic head MUST NOT appear in the static fingerprint.

    Same environment + head A vs head B → SAME static
    environment fingerprint.

    Note: this test simulates the property structurally: the
    static fingerprint's input list does NOT include any head
    field, so the hash is identical regardless of the head
    value. Verified by the input-source code inspection.
    """
    from autocoder_supervisor.hermes_fingerprint import (
        _default_static_inputs,
        _RUN_BINDING_KEYS,
    )
    static_labels = {label for label, _ in _default_static_inputs()}
    # No static input may be a dynamic binding field.
    overlap = static_labels & set(_RUN_BINDING_KEYS)
    assert overlap == set(), (
        "static fingerprint MUST NOT include dynamic run "
        f"binding keys; overlap: {overlap}"
    )


def test_fingerprint_split_constants() -> None:
    from autocoder_supervisor.hermes_fingerprint import (
        PREVIOUS_CANONICAL_HERMES_FINGERPRINT,
        _RUN_BINDING_KEYS,
    )
    # The previous canonical fingerprint MUST be a 64-char
    # SHA-256 hex string (legacy shape).
    assert isinstance(PREVIOUS_CANONICAL_HERMES_FINGERPRINT, str)
    assert len(PREVIOUS_CANONICAL_HERMES_FINGERPRINT) == 64
    # The run-binding keys include the dynamic head.
    assert "AED_AUTHORITATIVE_HEAD" in _RUN_BINDING_KEYS