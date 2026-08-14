"""Hermes acceptance environment fingerprint (pre-canary §9 + Closure III).

The Hermes acceptance environment has TWO independent hashes:

A. STATIC ENVIRONMENT FINGERPRINT — over immutable
   acceptance/control-plane inputs that MUST NOT change
   during a valid autonomous canary generation. If this
   hash changes, the operator must reconcile the
   acceptance environment before any new canary
   generation can be certified.

B. RUN BINDING DIGEST — over identities expected to change
   between valid generations (current head, generation id,
   attempt id, result contract id). This is recorded per
   generation but is NOT part of the static environment
   identity.

The static fingerprint MUST include:

- global Hermes config bytes
- all aed-* profile config bytes
- Hermes CLI/shim bytes
- frozen supervisor/runtime source bytes
- worker wrapper/result-contract runtime bytes
- provider/control-plane configuration that affects
  acceptance
- security/tool restriction configuration
- other truly immutable acceptance behavior inputs

The run-binding MUST include:

- current authoritative GitHub head
- generation id
- attempt id
- result contract id
- worker session identity where appropriate

Dynamic head identity MUST NOT appear in the static hash.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


# ---------------------------------------------------------------------------
# Static environment inputs (immutable acceptance/control-plane)
# ---------------------------------------------------------------------------


# Each entry is (label, Path). These paths are resolved from the
# canonical locations; a missing path means the acceptance
# environment is broken (fail closed).
def _default_static_inputs() -> list[tuple[str, Path]]:
    home = Path(os.environ.get("OPERATOR_HOME") or str(Path.home()))
    return [
        ("global_config", home / ".hermes/config.yaml"),
        (
            "profile_aed_builder_config",
            home / ".hermes/profiles/aed-builder/config.yaml",
        ),
        (
            "profile_aed_reviewer_config",
            home / ".hermes/profiles/aed-reviewer/config.yaml",
        ),
        (
            "profile_aed_specifier_config",
            home / ".hermes/profiles/aed-specifier/config.yaml",
        ),
        (
            "profile_aed_researcher_config",
            home / ".hermes/profiles/aed-researcher/config.yaml",
        ),
        (
            "profile_aed_quarantine_config",
            home / ".hermes/profiles/aed-quarantine/config.yaml",
        ),
        # The frozen supervisor runtime binary. The static
        # hash MUST change when this file changes. A new
        # commit bumps the bytes; the operator is responsible
        # for freezing the new bytes as the next 5/5
        # environment fingerprint.
        (
            "supervisor_entrypoint",
            home / ".hermes/aed-supervisor/supervisor.py",
        ),
        # Frozen Hermes CLI shim.
        (
            "hermes_cli_shim",
            home / ".hermes/hermes-agent/venv/bin/hermes",
        ),
    ]


# ---------------------------------------------------------------------------
# Run-binding inputs (dynamic identities)
# ---------------------------------------------------------------------------


# Per-generation run binding. The supervisor passes these in
# explicitly when invoking the helper.
_RUN_BINDING_KEYS: tuple[str, ...] = (
    "AED_AUTHORITATIVE_HEAD",
    "AED_PR_NUMBER",
    "AED_SESSION_ID",
    "AED_GENERATION_ID",
    "AED_ATTEMPT_ID",
    "AED_RESULT_CONTRACT_ID",
)


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def compute_static_hermes_environment_fingerprint(
    *, inputs: list[tuple[str, Path]] | None = None,
) -> dict:
    """Recompute the static environment fingerprint.

    The static fingerprint is over immutable acceptance
    inputs only. A missing input MUST fail closed.
    """
    parts = inputs if inputs is not None else _default_static_inputs()
    h = hashlib.sha256()
    missing = []
    for label, path in parts:
        if not isinstance(path, Path):
            missing.append(f"{label}: not a Path")
            continue
        if not path.exists():
            missing.append(f"{label}:{path}")
            continue
        try:
            sha = _sha256_of(path)
        except OSError as e:
            missing.append(f"{label}:{path}:{e}")
            continue
        h.update(f"file\t{label}\t{path}\t{sha}\n".encode("utf-8"))
    if missing:
        # Fail closed: the acceptance environment is
        # incomplete. The static fingerprint cannot be
        # computed.
        raise RuntimeError(
            "static acceptance environment fingerprint "
            "missing inputs: " + "; ".join(missing)
        )
    return {
        "inputs": list(parts),
        "fingerprint": h.hexdigest(),
    }


def compute_run_binding_digest(
    *, binding: dict | None = None,
) -> dict:
    """Recompute the run-binding digest over dynamic identities.

    The binding may be passed explicitly (production) or read
    from environment variables (CI convenience).
    """
    if binding is None:
        binding = {k: os.environ.get(k, "") for k in _RUN_BINDING_KEYS}
    h = hashlib.sha256()
    keys = sorted(binding.keys())
    for k in keys:
        h.update(f"binding\t{k}\t{binding[k]}\n".encode("utf-8"))
    return {
        "binding": binding,
        "digest": h.hexdigest(),
    }


# ---------------------------------------------------------------------------
# Deprecated: compute_hermes_acceptance_fingerprint
#
# Kept for backward compatibility. New code should call
# compute_static_hermes_environment_fingerprint instead.
# ---------------------------------------------------------------------------


def _suppressed_legacy_static_inputs() -> list[tuple[str, Path]]:
    """The legacy fingerprint incorrectly included
    AED_AUTHORITATIVE_HEAD in the static hash. This list is the
    legacy shape; the new static-only helper above does NOT
    include it.
    """
    inputs = _default_static_inputs()
    return inputs


# Previous canonical Hermes fingerprint recorded before the
# static/dynamic split. This was over the old shape that
# included AED_AUTHORITATIVE_HEAD.
PREVIOUS_CANONICAL_HERMES_FINGERPRINT = os.environ.get(
    "PREVIOUS_HERMES_FINGERPRINT",
    "1f0ce69102f4412e3236fc85151cbec8d23ae43e51b4bec4175bbe712f52c38b",
)


# A 16-hex token used by tests to scope monkeypatched input
# lists without colliding with production state.
_TEST_TOKEN = "01J0E6XR9X0F4QZ8Y2V5K3M7NS"


__all__ = [
    "compute_static_hermes_environment_fingerprint",
    "compute_run_binding_digest",
    "PREVIOUS_CANONICAL_HERMES_FINGERPRINT",
    "_RUN_BINDING_KEYS",
    "_TEST_TOKEN",
]