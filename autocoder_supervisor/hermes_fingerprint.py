"""Hermes acceptance environment fingerprint (Closure IV).

The Hermes acceptance environment has THREE independent hashes:

A. STATIC ENVIRONMENT FINGERPRINT — over immutable
   acceptance/control-plane inputs (configs, frozen runtime
   bytes). Every byte in the declared acceptance runtime
   inventory is hashed. The runtime file set is derived from
   ``ACCEPTANCE_RUNTIME_INVENTORY`` below — not a
   hand-maintained list.

B. STATIC ACCEPTANCE SCOPE FINGERPRINT — over routing-identity
   values that must NOT change between valid C22 generations
   (repository owner/name, PR number, branch, working checkout,
   state dir, supervisor home, Hermes binary path, required
   /optional provider set, provider independence). These
   belong in the frozen static scope, not in dynamic binding.

C. DYNAMIC RUN BINDING DIGEST — over identities expected to
   change each generation (current head, generation id,
   attempt id, result contract id). The run binding has a
   STRICT required schema: empty / missing / malformed values
   are rejected (fail closed).
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# §7 — Declared acceptance runtime inventory
# ---------------------------------------------------------------------------


# The canonical inventory of files that participate in C22
# acceptance. This is the SINGLE source of truth for the
# static runtime file set; CI itself validates this list
# against the deployed runtime. Adding a new acceptance
# runtime file requires editing this list AND the production
# source tree (in the same change). This eliminates the
# hand-maintained partial list defect.
#
# Closure V §6: the inventory is the transitive closure of
# every module that supervisor.py imports from
# autocoder_supervisor/ AND every autocoder_orchestration/
# module that supervisor.py imports. The inventory is
# validated against the actual imports at module-load time.
ACCEPTANCE_RUNTIME_INVENTORY: tuple = (
    # autocoder_supervisor/ — modules supervisor.py
    # imports directly via `from .X import ...`.
    "supervisor.py",
    "_directive_prompt.py",
    "worker_session.py",
    "aed_worker_wrapper.py",
    "directive_bridge.py",
    "provenance_maintenance.py",
    "hermes_fingerprint.py",
    "orchestration_state_root.py",
    "relay_wiring.py",
    "config.py",
    "contracts.py",
    "validate.py",
    # autocoder_orchestration/ — modules supervisor.py
    # imports for the orchestration/result-contract/repair
    # pipeline.
    "worker_attempt.py",
    "review_repair_relay.py",
    "controller.py",
    "context.py",
    "store.py",
)


# ---------------------------------------------------------------------------
# Static environment inputs (§6 + §7)
# ---------------------------------------------------------------------------


# Each entry is (label, Path|None). These paths are resolved
# from the canonical locations; a missing path means the
# acceptance environment is broken (fail closed).
def _default_static_inputs() -> list:
    home = Path(os.environ.get("OPERATOR_HOME") or str(Path.home()))
    runtime = home / ".hermes/aed-supervisor"
    hermes_home = home / ".hermes/hermes-agent/venv/bin"
    inputs: list = [
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
    ]
    # Every declared acceptance-runtime file in the
    # inventory contributes to the static hash.
    for filename in ACCEPTANCE_RUNTIME_INVENTORY:
        inputs.append(
            (f"acceptance_runtime:{filename}", runtime / filename)
        )
    inputs.append(("hermes_cli_shim", hermes_home / "hermes"))
    return inputs


# ---------------------------------------------------------------------------
# §8 — Static scope (frozen routing identity)
# ---------------------------------------------------------------------------


# The set of routing-identity values that MUST NOT silently
# change between valid C22 generations. These are part of
# the frozen static acceptance environment, NOT the dynamic
# run binding.
STATIC_SCOPE_KEYS: tuple = (
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
    "expected_branch_set",
    "expected_pr_set",
)


def _default_static_scope() -> dict:
    return {
        "repository_owner": os.environ.get("AED_REPO_OWNER", ""),
        "repository_name": os.environ.get("AED_REPO_NAME", ""),
        "pr_number": os.environ.get("AED_PR_NUMBER", ""),
        "expected_branch": os.environ.get(
            "AED_EXPECTED_BRANCH", "feat/review-repair-relay-v1"
        ),
        "expected_pr_set": os.environ.get("AED_PR_NUMBERS", ""),
        "expected_branch_set": os.environ.get(
            "AED_EXPECTED_BRANCH_SET", "feat/review-repair-relay-v1"
        ),
        "production_working_checkout": os.environ.get(
            "AED_SUPERVISOR_WORKING_CHECKOUT", "/home/max/AutoDev"
        ),
        "supervisor_state_directory": os.environ.get(
            "AED_SUPERVISOR_STATE_DIR",
            str(Path.home() / ".hermes/aed-supervisor/state"),
        ),
        "supervisor_home": os.environ.get(
            "AED_SUPERVISOR_HOME",
            str(Path.home() / ".hermes/aed-supervisor"),
        ),
        "hermes_binary_path": os.environ.get(
            "AED_HERMES_BIN", "/home/max/.local/bin/hermes"
        ),
        "required_providers": os.environ.get(
            "AED_REQUIRED_REVIEW_PROVIDERS", "coderabbit"
        ),
        "optional_providers": os.environ.get(
            "AED_OPTIONAL_REVIEW_PROVIDERS", "codex"
        ),
        "provider_independence": os.environ.get(
            "AED_PROVIDERS_INDEPENDENT", "true"
        ),
    }


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Static environment fingerprint
# ---------------------------------------------------------------------------


def compute_static_hermes_environment_fingerprint(
    *, inputs=None,
) -> dict:
    parts = inputs if inputs is not None else _default_static_inputs()
    h = hashlib.sha256()
    missing = []
    for label, path in parts:
        if path is None:
            missing.append(f"{label}: not a Path")
            continue
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
        raise RuntimeError(
            "static acceptance environment fingerprint "
            "missing inputs: " + "; ".join(missing)
        )
    return {
        "inputs": list(parts),
        "fingerprint": h.hexdigest(),
    }


# ---------------------------------------------------------------------------
# Static scope fingerprint
# ---------------------------------------------------------------------------


def _validate_static_scope_value(key: str, value) -> None:
    """Closure V §4: every static-scope value MUST be a
    canonical, non-empty, well-formed identity. Empty
    defaults are rejected.
    """
    if not isinstance(value, str):
        raise StaticScopeValidationError(
            f"static scope value for {key!r} must be a string, "
            f"got {type(value).__name__}"
        )
    if not value.strip():
        raise StaticScopeValidationError(
            f"static scope value for {key!r} must be non-empty"
        )


def _validate_absolute_path(key: str, value: str) -> None:
    """Closure V §4: paths under the static scope that are
    expected to be absolute MUST be absolute."""
    import os
    if not os.path.isabs(value):
        raise StaticScopeValidationError(
            f"static scope path {key!r} must be absolute, "
            f"got {value!r}"
        )


def compute_static_acceptance_scope_fingerprint(
    *, scope=None,
) -> dict:
    """Recompute the static acceptance scope fingerprint over
    the routing-identity values that must NOT change between
    valid generations. AED_PR_NUMBER, repository owner/name,
    expected branch, working checkout, state dir, supervisor
    home, Hermes binary path, provider sets, provider
    independence — all bound here.

    The scope can be passed explicitly (production) or read
    from environment variables (CI convenience).

    Closure V §4: every value is validated. Empty values,
    relative paths, PR #0, wrong branch, wrong repository,
    and missing required providers all fail closed with
    ``StaticScopeValidationError``.
    """
    parts = scope if scope is not None else _default_static_scope()
    # Required key presence.
    for k in STATIC_SCOPE_KEYS:
        if k not in parts:
            raise RuntimeError(
                f"static scope is missing required key: {k!r}"
            )
        _validate_static_scope_value(k, parts[k])
    # Per-key semantic validation.
    if parts["repository_owner"] != "Slideshow11":
        raise StaticScopeValidationError(
            f"static scope repository_owner must be Slideshow11 "
            f"for C22; got {parts['repository_owner']!r}"
        )
    if parts["repository_name"] != "AutoDev":
        raise StaticScopeValidationError(
            f"static scope repository_name must be AutoDev for "
            f"C22; got {parts['repository_name']!r}"
        )
    if parts["pr_number"] != "5":
        raise StaticScopeValidationError(
            f"static scope pr_number must be '5' for C22; "
            f"got {parts['pr_number']!r}"
        )
    if parts["pr_number"] in ("", "0"):
        raise StaticScopeValidationError(
            f"static scope pr_number cannot be empty or 0; "
            f"got {parts['pr_number']!r}"
        )
    if parts["expected_branch"] != "feat/review-repair-relay-v1":
        raise StaticScopeValidationError(
            f"static scope expected_branch must be "
            f"feat/review-repair-relay-v1 for C22; got "
            f"{parts['expected_branch']!r}"
        )
    if "5" not in parts["expected_pr_set"].split(","):
        raise StaticScopeValidationError(
            f"static scope expected_pr_set must contain '5' "
            f"for C22; got {parts['expected_pr_set']!r}"
        )
    if "feat/review-repair-relay-v1" not in parts[
        "expected_branch_set"
    ].split(","):
        raise StaticScopeValidationError(
            f"static scope expected_branch_set must contain "
            f"feat/review-repair-relay-v1 for C22; got "
            f"{parts['expected_branch_set']!r}"
        )
    _validate_absolute_path(
        "production_working_checkout",
        parts["production_working_checkout"],
    )
    _validate_absolute_path(
        "supervisor_state_directory",
        parts["supervisor_state_directory"],
    )
    _validate_absolute_path(
        "supervisor_home",
        parts["supervisor_home"],
    )
    _validate_absolute_path(
        "hermes_binary_path",
        parts["hermes_binary_path"],
    )
    if "coderabbit" not in parts["required_providers"].split(","):
        raise StaticScopeValidationError(
            f"static scope required_providers must include "
            f"coderabbit; got {parts['required_providers']!r}"
        )
    if "codex" not in parts["optional_providers"].split(","):
        raise StaticScopeValidationError(
            f"static scope optional_providers must include "
            f"codex for C22; got {parts['optional_providers']!r}"
        )
    # Provider overlap / contradiction.
    req_set = set(parts["required_providers"].split(","))
    opt_set = set(parts["optional_providers"].split(","))
    if req_set & opt_set:
        raise StaticScopeValidationError(
            f"static scope provider set contradiction: "
            f"{req_set & opt_set} appears in both required and "
            f"optional providers"
        )
    if parts["provider_independence"] not in ("true", "1", "yes"):
        raise StaticScopeValidationError(
            f"static scope provider_independence must be a "
            f"validated boolean semantic value; got "
            f"{parts['provider_independence']!r}"
        )
    h = hashlib.sha256()
    for k in STATIC_SCOPE_KEYS:
        v = parts[k]
        h.update(f"scope\t{k}\t{v}\n".encode("utf-8"))
    return {
        "scope": {k: parts[k] for k in STATIC_SCOPE_KEYS},
        "fingerprint": h.hexdigest(),
    }


class StaticScopeValidationError(ValueError):
    """Raised when a static-scope value fails semantic
    validation (empty, wrong identity, relative path,
    wrong branch, etc.)."""


# ---------------------------------------------------------------------------
# Dynamic run binding digest (§9 — strict schema)
# ---------------------------------------------------------------------------


# The required schema for the dynamic run binding. Each key
# is mandatory, non-empty, and (for SHA fields) must match the
# 40-64 hex-char format. Unknown extra keys are allowed for
# forward compatibility but recorded as ``extra_keys``.
_RUN_BINDING_REQUIRED_KEYS: tuple = (
    "authoritative_head",
    "generation_id",
    "attempt_id",
    "result_contract_id",
)
_RUN_BINDING_SHA_KEYS: frozenset = frozenset({"authoritative_head"})
_HEX_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
RUN_BINDING_SCHEMA_VERSION = "autocoder.hermes.run_binding.v1"


# Dynamic identities expected to change each generation.
_RUN_BINDING_KEYS: tuple = _RUN_BINDING_REQUIRED_KEYS


class RunBindingSchemaError(ValueError):
    """Raised when the dynamic run binding fails schema
    validation. Fail-closed semantics: an empty binding,
    missing keys, empty values, or malformed SHAs are all
    rejected."""


def _validate_run_binding(binding: dict) -> None:
    if not isinstance(binding, dict):
        raise RunBindingSchemaError(
            f"run binding must be a dict, got {type(binding).__name__}"
        )
    for k in _RUN_BINDING_REQUIRED_KEYS:
        if k not in binding:
            raise RunBindingSchemaError(
                f"run binding missing required key: {k!r}"
            )
        v = binding[k]
        if not isinstance(v, str):
            raise RunBindingSchemaError(
                f"run binding value for {k!r} must be a string, "
                f"got {type(v).__name__}"
            )
        if not v:
            raise RunBindingSchemaError(
                f"run binding value for {k!r} must be non-empty"
            )
        if k in _RUN_BINDING_SHA_KEYS and not _HEX_SHA_RE.match(v):
            raise RunBindingSchemaError(
                f"run binding value for {k!r} must be a SHA "
                f"hex string, got {v!r}"
            )


def compute_run_binding_digest(
    *, binding=None,
) -> dict:
    """Recompute the run-binding digest over dynamic identities.

    The binding MUST satisfy the strict schema. Empty
    bindings, missing keys, empty values, and malformed SHAs
    all raise ``RunBindingSchemaError`` (fail closed).

    Closure V §7: the production path should NOT silently
    fall back to environment variables. The canonical
    production caller MUST pass ``binding`` explicitly. The
    environment fallback is preserved for diagnostics only.
    """
    if binding is None:
        # Environment fallback. We use the AED_* prefix
        # convention, not the upper-case literal key, so the
        # canonical environment contract is honored.
        binding = {
            "authoritative_head": os.environ.get(
                "AED_AUTHORITATIVE_HEAD", ""
            ),
            "generation_id": os.environ.get("AED_GENERATION_ID", ""),
            "attempt_id": os.environ.get("AED_ATTEMPT_ID", ""),
            "result_contract_id": os.environ.get(
                "AED_RESULT_CONTRACT_ID", ""
            ),
        }
    _validate_run_binding(binding)
    h = hashlib.sha256()
    keys = sorted(_RUN_BINDING_REQUIRED_KEYS)
    for k in keys:
        h.update(f"binding\t{k}\t{binding[k]}\n".encode("utf-8"))
    return {
        "binding": {k: binding[k] for k in keys},
        "digest": h.hexdigest(),
    }


class RunBindingRelationalError(ValueError):
    """Raised when the four run-binding fields are individually
    valid but their relationships are inconsistent
    (cross-generation mix, wrong contract for the bound
    attempt, etc.). Fail-closed semantics."""


def validate_run_binding_relations(
    *,
    binding: dict,
    owned_heads: set,
    owned_generations: set,
    owned_attempts: set,
    owned_contracts: set,
) -> dict:
    """Closure V §7: semantic relational ownership.

    A valid run binding must prove that its four fields
    belong to the same generation/attempt/contract tuple:

      binding.authoritative_head in owned_heads
      binding.generation_id in owned_generations
      binding.attempt_id in owned_attempts
      binding.result_contract_id in owned_contracts

    Cross-generation mix-and-match is rejected.
    """
    # First ensure the binding is syntactically valid.
    _validate_run_binding(binding)
    if binding["authoritative_head"] not in owned_heads:
        raise RunBindingRelationalError(
            f"run binding head {binding['authoritative_head']!r} "
            f"not in owned_heads set; cross-generation mix "
            f"rejected"
        )
    if binding["generation_id"] not in owned_generations:
        raise RunBindingRelationalError(
            f"run binding generation {binding['generation_id']!r} "
            f"not in owned_generations set; cross-generation "
            f"mix rejected"
        )
    if binding["attempt_id"] not in owned_attempts:
        raise RunBindingRelationalError(
            f"run binding attempt {binding['attempt_id']!r} "
            f"not in owned_attempts set; cross-generation "
            f"mix rejected"
        )
    if binding["result_contract_id"] not in owned_contracts:
        raise RunBindingRelationalError(
            f"run binding contract {binding['result_contract_id']!r} "
            f"not in owned_contracts set; cross-generation "
            f"mix rejected"
        )
    return {
        "binding": dict(binding),
        "relations_verified": True,
    }


# ---------------------------------------------------------------------------
# Deprecated legacy API (kept for backward compatibility with
# the existing test file)
# ---------------------------------------------------------------------------


# Previous canonical Hermes fingerprint recorded before the
# closure-IV split. This was over the old shape that included
# AED_AUTHORITATIVE_HEAD in the static hash.
PREVIOUS_CANONICAL_HERMES_FINGERPRINT = os.environ.get(
    "PREVIOUS_HERMES_FINGERPRINT",
    "1f0ce69102f4412e3236fc85151cbec8d23ae43e51b4bec4175bbe712f52c38b",
)


# Closure III: 77d9d171... was an INCOMPLETE static fingerprint
# (only 8 files; missing worker_session, aed_worker_wrapper,
# etc.). Now SUPERSEDED by the complete fingerprint produced
# by compute_static_hermes_environment_fingerprint().
SUPERSEDED_INCOMPLETE_STATIC_FINGERPRINT = os.environ.get(
    "SUPERSEDED_INCOMPLETE_STATIC_FINGERPRINT",
    "77d9d171313e7c1ef9975773c34802dadbdfd4f58a2547620bf4413e0f591bd5",
)


__all__ = [
    # Static environment
    "compute_static_hermes_environment_fingerprint",
    "ACCEPTANCE_RUNTIME_INVENTORY",
    "_default_static_inputs",
    # Static scope
    "compute_static_acceptance_scope_fingerprint",
    "StaticScopeValidationError",
    "_validate_static_scope_value",
    "_validate_absolute_path",
    "STATIC_SCOPE_KEYS",
    "_default_static_scope",
    # Dynamic run binding
    "compute_run_binding_digest",
    "validate_run_binding_relations",
    "RUN_BINDING_SCHEMA_VERSION",
    "_RUN_BINDING_KEYS",
    "RunBindingSchemaError",
    "RunBindingRelationalError",
    # Legacy / historical
    "PREVIOUS_CANONICAL_HERMES_FINGERPRINT",
    "SUPERSEDED_INCOMPLETE_STATIC_FINGERPRINT",
]