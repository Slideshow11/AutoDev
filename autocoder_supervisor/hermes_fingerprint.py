"""Hermes acceptance environment fingerprint (pre-canary §9).

The canonical Hermes acceptance fingerprint is a SHA-256 over
the deterministic bytes of the acceptance-relevant inputs:

1. The global Hermes config that controls provider / toolset
   / memory governance for the acceptance environment.
2. Each aed-* profile config (builder, researcher, reviewer,
   specifier, quarantine) — these gate memory/profile writes
   and shadow-control-plane mutation.
3. The supervisor entrypoint binary
   (``/home/max/.hermes/aed-supervisor/supervisor.py``) which
   the production supervisor process actually loads.
4. The hermes CLI shim
   (``/home/max/.hermes/hermes-agent/venv/bin/hermes``).
5. The AED_* env vars (from the supervisor's process env).

The fingerprint is intentionally:

- Order-stable: the same inputs in the same order always
  produce the same digest.
- Source-only: it does NOT include transient runtime state
  (heartbeat, log files, lease records). Those change on
  every heartbeat and would prevent the fingerprint from
  being a meaningful invariant.
- Inclusion-only: every input that controls acceptance
  behavior MUST be included; adding new acceptance-relevant
  config without updating the fingerprint is a governance
  defect.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


# Canonical input list — order matters; this list defines the
# canonical fingerprint contract. Adding a new acceptance-relevant
# input requires updating this list AND regenerating the canonical
# fingerprint under operator authority.
_CANONICAL_INPUTS: list[tuple[str, Path | None]] = [
    ("global_config", Path("/home/max/.hermes/config.yaml")),
    (
        "profile_aed_builder_config",
        Path("/home/max/.hermes/profiles/aed-builder/config.yaml"),
    ),
    (
        "profile_aed_reviewer_config",
        Path("/home/max/.hermes/profiles/aed-reviewer/config.yaml"),
    ),
    (
        "profile_aed_specifier_config",
        Path("/home/max/.hermes/profiles/aed-specifier/config.yaml"),
    ),
    (
        "profile_aed_researcher_config",
        Path("/home/max/.hermes/profiles/aed-researcher/config.yaml"),
    ),
    (
        "profile_aed_quarantine_config",
        Path("/home/max/.hermes/profiles/aed-quarantine/config.yaml"),
    ),
    (
        "supervisor_entrypoint",
        Path("/home/max/.hermes/aed-supervisor/supervisor.py"),
    ),
    (
        "hermes_cli_shim",
        Path("/home/max/.hermes/hermes-agent/venv/bin/hermes"),
    ),
]

# AED_* env vars that control acceptance behavior.
_AED_ENV_VARS: tuple[str, ...] = (
    "AED_PR_NUMBER",
    "AED_PR_NUMBERS",
    "AED_REPO_OWNER",
    "AED_REPO_NAME",
    "AED_AUTHORITATIVE_HEAD",
    "AED_HEARTBEAT_SECONDS",
    "AED_SESSION_ID",
    "AED_SESSION_NAME",
    "AED_HERMES_BIN",
    "AED_SUPERVISOR_HOME",
    "AED_SUPERVISOR_STATE_DIR",
    "AED_SUPERVISOR_LOG_PATH",
    "AED_SUPERVISOR_HEARTBEAT_PATH",
    "AED_SUPERVISOR_LOCK_PATH",
    "AED_SUPERVISOR_WORKING_CHECKOUT",
)


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def compute_hermes_acceptance_fingerprint() -> dict:
    """Recompute the canonical Hermes acceptance fingerprint.

    Returns a dict with these keys:
      - ``inputs``: ordered list of (label, path, sha256_or_text)
      - ``fingerprint``: the SHA-256 of the canonical concatenation
      - ``missing``: list of inputs whose file is missing
    """
    parts: list[tuple[str, str, str]] = []
    missing: list[str] = []
    for label, path in _CANONICAL_INPUTS:
        if path is None:
            continue
        if not path.exists():
            missing.append(f"{label}:{path}")
            continue
        try:
            sha = _sha256_of(path)
        except OSError as e:
            missing.append(f"{label}:{path}:{e}")
            continue
        parts.append((label, str(path), sha))
    # Env vars (text, not bytes — they must be deterministic
    # for the fingerprint to be reproducible).
    env_parts: list[tuple[str, str, str]] = []
    for var in _AED_ENV_VARS:
        val = os.environ.get(var, "")
        env_parts.append((f"env:{var}", var, val))
    # Canonical concatenation: ordered, newline-separated.
    h = hashlib.sha256()
    for label, path, sha in parts:
        h.update(f"file\t{label}\t{path}\t{sha}\n".encode("utf-8"))
    for label, var, val in sorted(env_parts):
        h.update(f"env\t{label}\t{val}\n".encode("utf-8"))
    return {
        "inputs": parts + env_parts,
        "fingerprint": h.hexdigest(),
        "missing": missing,
    }


# The previous canonical fingerprint known to the operator.
# Operators may set this via env or rely on the literal.
PREVIOUS_CANONICAL_HERMES_FINGERPRINT = os.environ.get(
    "PREVIOUS_HERMES_FINGERPRINT",
    "1f0ce69102f4412e3236fc85151cbec8d23ae43e51b4bec4175bbe712f52c38b",
)


__all__ = [
    "compute_hermes_acceptance_fingerprint",
    "PREVIOUS_CANONICAL_HERMES_FINGERPRINT",
    "_CANONICAL_INPUTS",
    "_AED_ENV_VARS",
]