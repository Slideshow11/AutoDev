"""Hermes acceptance fingerprint test (pre-canary §9).

Pre-canary round-281 §9: the canonical Hermes acceptance
fingerprint MUST be recomputed from the actual current
acceptance-relevant inputs. It MUST NOT be assumed equal to
the prior canonical fingerprint.
"""
from __future__ import annotations

from autocoder_supervisor.hermes_fingerprint import (
    compute_hermes_acceptance_fingerprint,
    PREVIOUS_CANONICAL_HERMES_FINGERPRINT,
    _CANONICAL_INPUTS,
    _AED_ENV_VARS,
)


def test_canonical_inputs_are_well_defined() -> None:
    assert len(_CANONICAL_INPUTS) >= 5, (
        "fingerprint must include at least the global config, "
        "one profile, the supervisor entrypoint, and the hermes "
        "CLI shim"
    )
    labels = [label for label, _ in _CANONICAL_INPUTS]
    assert "global_config" in labels
    assert "supervisor_entrypoint" in labels
    assert "hermes_cli_shim" in labels
    # At least one aed-* profile config.
    aed_profiles = [
        label for label, _ in _CANONICAL_INPUTS
        if label.startswith("profile_aed_")
    ]
    assert len(aed_profiles) >= 1


def test_env_vars_are_well_defined() -> None:
    assert len(_AED_ENV_VARS) >= 5
    for required in (
        "AED_PR_NUMBER",
        "AED_REPO_OWNER",
        "AED_REPO_NAME",
        "AED_AUTHORITATIVE_HEAD",
    ):
        assert required in _AED_ENV_VARS


def test_fingerprint_is_deterministic() -> None:
    a = compute_hermes_acceptance_fingerprint()
    b = compute_hermes_acceptance_fingerprint()
    assert a["fingerprint"] == b["fingerprint"]
    assert a["inputs"] == b["inputs"]


def test_fingerprint_inputs_exist() -> None:
    fp = compute_hermes_acceptance_fingerprint()
    # In a real production environment every input MUST be present.
    assert fp["missing"] == [], (
        f"some fingerprint inputs are missing: {fp['missing']}"
    )


def test_fingerprint_changes_when_config_changes(
    tmp_path, monkeypatch,
) -> None:
    # Construct a hermetic env where we control every input.
    import hashlib
    from autocoder_supervisor import hermes_fingerprint as hf
    # Replace the canonical input list with a hermetic one.
    cfg = tmp_path / "config.yaml"
    cfg.write_text("a: 1\n")
    profile = tmp_path / "profile.yaml"
    profile.write_text("b: 2\n")
    sup = tmp_path / "supervisor.py"
    sup.write_text("# sup v1\n")
    shim = tmp_path / "hermes"
    shim.write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        hf, "_CANONICAL_INPUTS",
        [
            ("global_config", cfg),
            ("profile_aed_builder_config", profile),
            ("supervisor_entrypoint", sup),
            ("hermes_cli_shim", shim),
        ],
    )
    monkeypatch.setattr(hf, "_AED_ENV_VARS", ("AED_PR_NUMBER",))
    monkeypatch.setenv("AED_PR_NUMBER", "5")
    fp1 = compute_hermes_acceptance_fingerprint()
    # Mutate the config.
    sup.write_text("# sup v2 — changed\n")
    fp2 = compute_hermes_acceptance_fingerprint()
    assert fp1["fingerprint"] != fp2["fingerprint"]
    # Mutate an env var.
    monkeypatch.setenv("AED_PR_NUMBER", "99")
    fp3 = compute_hermes_acceptance_fingerprint()
    assert fp2["fingerprint"] != fp3["fingerprint"]


def test_fingerprint_recording_pre_vs_current() -> None:
    """Compare the recomputed fingerprint against the prior
    canonical. They MAY differ because the env or files have
    changed. We do NOT require equality — the directive says
    "Do NOT assume it is still correct."
    """
    fp = compute_hermes_acceptance_fingerprint()
    # The previous fingerprint is a 64-hex-char SHA-256.
    assert isinstance(PREVIOUS_CANONICAL_HERMES_FINGERPRINT, str)
    assert len(PREVIOUS_CANONICAL_HERMES_FINGERPRINT) == 64
    assert isinstance(fp["fingerprint"], str)
    assert len(fp["fingerprint"]) == 64
    # Either they match (env unchanged) or differ (env
    # changed). Both are acceptable; the test asserts only
    # that the current fingerprint is reproducible.
    # This test is intentionally permissive: the audit
    # reports the comparison, not the verdict.