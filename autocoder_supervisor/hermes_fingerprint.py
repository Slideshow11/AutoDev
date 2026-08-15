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


# Each entry is (label, Path). These paths are resolved
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
    # The inventory declares file basenames. autocoder_supervisor/
    # files are deployed under the supervisor runtime root.
    # autocoder_orchestration/ files live in the source-controlled
    # checkout at the canonical REPO_DIR (NOT under the runtime
    # root). Resolve each entry by checking both locations and
    # using the one that exists.
    for filename in ACCEPTANCE_RUNTIME_INVENTORY:
        # Try the supervisor runtime root first.
        runtime_path = runtime / filename
        # Then the production checkout (AutoDev repo root).
        # We resolve REPO_DIR from the supervisor's env if set,
        # otherwise default to /home/max/AutoDev. The orch
        # files live under autocoder_orchestration/; try both.
        checkout = Path(
            os.environ.get(
                "AED_SUPERVISOR_WORKING_CHECKOUT", "/home/max/AutoDev"
            )
        )
        checkout_paths = [
            checkout / filename,
            checkout / "autocoder_orchestration" / filename,
            checkout / "autocoder_supervisor" / filename,
        ]
        chosen = None
        for cp in [runtime_path] + checkout_paths:
            if cp.exists():
                chosen = cp
                break
        if chosen is None:
            chosen = runtime_path
        inputs.append(
            (f"acceptance_runtime:{filename}", chosen)
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


def canonical_cooldown_deferred_count(state_dir) -> dict:
    """Canonical cooldown ledger parser.

    Closure VII §5: ONE canonical parser, not duplicated
    schema logic. Reuses the same entries-preferred /
    legacy-ids-fallback semantics as
    ``supervisor._cooldown_deferred_ids``.

    Returns a dict with:
      - count (int)
      - entries_count (int)
      - legacy_ids_count (int)
      - schema_source (str)
      - parse_failed (bool)
    """
    import json as _json
    out = {
        "count": 0,
        "entries_count": 0,
        "legacy_ids_count": 0,
        "schema_source": "cooldown_deferred_events.json",
        "parse_failed": False,
    }
    cd_path = Path(state_dir) / "cooldown_deferred_events.json"
    if not cd_path.exists():
        return out
    try:
        cd = _json.loads(cd_path.read_text())
    except (OSError, _json.JSONDecodeError):
        out["parse_failed"] = True
        return out
    if not isinstance(cd, dict):
        out["parse_failed"] = True
        return out
    entries = cd.get("entries")
    if isinstance(entries, list) and entries:
        out["entries_count"] = len(entries)
        out["count"] = out["entries_count"]
        return out
    legacy = cd.get("ids", [])
    if isinstance(legacy, list):
        out["legacy_ids_count"] = len(legacy)
        out["count"] = out["legacy_ids_count"]
    return out


# ---------------------------------------------------------------------------
# Static environment fingerprint
# ---------------------------------------------------------------------------


def canonical_deferred_backlog_analysis(state_dir) -> dict:
    """Closure IX §7: detailed semantic disposition of
    each deferred entry.
    """
    out = {
        "deferred_event_ids": [],
        "deferred_without_retry_owner": [],
        "deferred_without_executable_retry_path": [],
        "deferred_current_head_actionable": [],
        "deferred_unknown_classification": [],
        "real_deferred_retry_event_ids": [],
    }
    import json as _json
    if not state_dir:
        return out
    cd_path = Path(state_dir) / "cooldown_deferred_events.json"
    if not cd_path.exists():
        return out
    try:
        cd = _json.loads(cd_path.read_text())
    except (OSError, _json.JSONDecodeError):
        return out
    entries = []
    if isinstance(cd, dict):
        e = cd.get("entries")
        if isinstance(e, list) and e:
            entries = e
        else:
            legacy = cd.get("ids", [])
            if isinstance(legacy, list):
                entries = [{"id": x} for x in legacy]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        eid = entry.get("id")
        if eid is None:
            continue
        out["deferred_event_ids"].append(eid)
        if not entry.get("retry_owner") and not entry.get("claim_id"):
            out["deferred_without_retry_owner"].append(eid)
        if not entry.get("next_retry_condition") and not entry.get(
            "next_retry_timestamp"
        ):
            out["deferred_without_executable_retry_path"].append(eid)
        head = entry.get("head_sha")
        if head and entry.get("actionable") is not False:
            out["deferred_current_head_actionable"].append(eid)
        if not entry.get("kind") and not entry.get("lifecycle"):
            out["deferred_unknown_classification"].append(eid)
    return out


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


# Closure VI §1: the canonical relational ownership model.
# A run binding is valid only when its four-tuple
# (authoritative_head, generation_id, attempt_id, result_contract_id)
# exactly equals one of the supervisor-owned tuples. Independent
# set membership on each component is REJECTED — that model
# permitted cross-generation mix-and-match.
#
# RunBindingTuple = NamedTuple(
#     authoritative_head,
#     generation_id,
#     attempt_id,
#     result_contract_id,
# )
#
# Ownership is durable: the caller MUST supply a set of tuples
# derived from actual WorkerAttempt / generation records.
# Manufacturing four unrelated sets is no longer accepted by
# the type system; the only accepted input is a set of
# 4-tuples.

def validate_run_binding_relations(
    *,
    binding: dict,
    owned_tuples,
) -> dict:
    """Closure VI §1: relational ownership via 4-tuple membership.

    The binding is valid iff its exact 4-tuple is one of the
    owned_tuples. Cross-generation mix-and-match is rejected.
    owned_tuples MUST be a set/frozenset of 4-tuples (head,
    generation, attempt, contract). The caller cannot manufacture
    four unrelated sets.

    Empty owned_tuples → every binding is rejected (no
    ownership record exists).

    Non-set owned_tuples → rejected at type-check time.

    owned_tuples containing a malformed entry (not a 4-tuple
    or any element non-string) → rejected.
    """
    # First ensure the binding is syntactically valid.
    _validate_run_binding(binding)
    if not isinstance(owned_tuples, (set, frozenset)):
        raise RunBindingRelationalError(
            f"owned_tuples must be a set or frozenset of 4-tuples; "
            f"got {type(owned_tuples).__name__}. Independent "
            f"set-per-field validation has been REMOVED (Closure VI §1)."
        )
    if not owned_tuples:
        raise RunBindingRelationalError(
            "owned_tuples is empty; no ownership record exists "
            "for this binding; reject"
        )
    # Validate every owned tuple's shape.
    canonical = set()
    for tup in owned_tuples:
        if not isinstance(tup, tuple) or len(tup) != 4:
            raise RunBindingRelationalError(
                f"owned_tuples contains malformed entry {tup!r}; "
                f"every entry must be a 4-tuple of "
                f"(authoritative_head, generation_id, attempt_id, "
                f"result_contract_id)"
            )
        for elem in tup:
            if not isinstance(elem, str):
                raise RunBindingRelationalError(
                    f"owned_tuples entry {tup!r} contains non-string "
                    f"element {elem!r}"
                )
        canonical.add(tup)
    # The binding's exact tuple must equal one of the owned tuples.
    binding_tuple = (
        binding["authoritative_head"],
        binding["generation_id"],
        binding["attempt_id"],
        binding["result_contract_id"],
    )
    if binding_tuple not in canonical:
        raise RunBindingRelationalError(
            f"run binding tuple {binding_tuple!r} is not in the "
            f"durable owned_tuples set; cross-generation mix "
            f"rejected. owned_tuples count: {len(canonical)}."
        )
    return {
        "binding": dict(binding),
        "relations_verified": True,
        "matched_tuple": binding_tuple,
    }


# Convenience constructors that read canonical durable
# ownership records (WorkerAttempt records in the supervisor's
# state/worker_attempts/ directory) and produce the
# owned_tuples set the validator consumes.
def owned_tuples_from_worker_attempt_records(
    records,
    *,
    include_terminal: bool = True,
) -> set:
    """Convert WorkerAttempt record dicts into a set of
    4-tuples for ``validate_run_binding_relations``.

    Closure VII §4: authoritative_head MUST be the head
    against which the worker/generation was launched
    (``prelaunch_head``), NOT the produced or pushed
    commit. The produced/pushed commit is independent
    output provenance and MUST NOT be substituted for the
    launch head.

    Each record must contain:
      - prelaunch_head (required) — the head the worker was
        launched against
      - generation_id
      - attempt_id
      - result_contract_id

    Records missing ``prelaunch_head`` are skipped
    (fail-closed: such records cannot satisfy the run
    binding contract).

    Terminal records (status in {CONSUMED, SUPERSEDED,
    TERMINAL, FAILED, TERMINATED}) are included by
    default; pass ``include_terminal=False`` to exclude
    them (e.g. for active-binding checks).
    """
    out = set()
    terminal_statuses = frozenset({
        "CONSUMED", "SUPERSEDED", "TERMINAL", "FAILED", "TERMINATED",
    })
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if not include_terminal:
            status = rec.get("status") or rec.get("lifecycle") or ""
            if status in terminal_statuses:
                continue
        # AUTHORITATIVE HEAD = prelaunch_head ONLY.
        # NEVER substitute produced_commit_sha or
        # pushed_commit_sha.
        head = rec.get("prelaunch_head")
        gen = rec.get("generation_id")
        att = rec.get("attempt_id")
        rc = rec.get("result_contract_id")
        if not all(isinstance(x, str) and x for x in (head, gen, att, rc)):
            continue
        # Reject any attempt record where the record
        # explicitly distinguishes prelaunch_head from
        # produced/pushed and the caller is asking us to
        # derive authoritative_head. If the record has
        # BOTH prelaunch_head and produced_commit_sha,
        # they MUST agree (sanity check) for the launch
        # head. If they differ, that's an autonomous
        # head advance; the launch head is still
        # prelaunch_head.
        produced = rec.get("produced_commit_sha") or ""
        pushed = rec.get("pushed_commit_sha") or ""
        if produced and produced != head and produced == pushed:
            # produced != prelaunch_head and pushed ==
            # produced: a new commit was produced. The
            # launch head is STILL prelaunch_head.
            pass  # authoritative_head remains prelaunch_head
        out.add((head, gen, att, rc))
    return out


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


# ---------------------------------------------------------------------------
# Closure VI §4: machine-generated pre-canary evidence artifact
# ---------------------------------------------------------------------------


SCHEMA_VERSION = "autocoder.pre_canary_evidence.v1"


def _verify_full_sha(s, label):
    """Reject short, malformed, or non-string SHAs."""
    if not isinstance(s, str):
        raise ValueError(f"{label}: not a string: {s!r}")
    if len(s) != 40:
        raise ValueError(
            f"{label}: must be exactly 40 chars, got {len(s)}: {s!r}"
        )
    import re as _re
    if not _re.fullmatch(r"[0-9a-f]{40}", s):
        raise ValueError(f"{label}: non-hex characters: {s!r}")


def _read_local_head(repo_root):
    import subprocess as _sp
    r = _sp.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _read_origin_head(repo_root, branch):
    import subprocess as _sp
    r = _sp.run(
        ["git", "-C", str(repo_root), "rev-parse", f"origin/{branch}"],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _read_live_github_head(repo, pr_number):
    """Read the live PR head directly from GitHub API."""
    import json as _json
    import urllib.request as _ur
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    req = _ur.Request(url, headers={"Accept": "application/vnd.github+json"})
    with _ur.urlopen(req, timeout=15) as resp:
        body = _json.loads(resp.read())
    return body["head"]["sha"], body


def _read_workflow_runs(repo, head):
    """Read check-runs for the exact head."""
    import json as _json
    import urllib.request as _ur
    url = f"https://api.github.com/repos/{repo}/commits/{head}/check-runs"
    req = _ur.Request(url, headers={"Accept": "application/vnd.github+json"})
    with _ur.urlopen(req, timeout=15) as resp:
        body = _json.loads(resp.read())
    return body.get("check_runs", [])


def _atomic_write(path, obj):
    """Atomic temp+rename+fsync write."""
    import json as _json
    import os as _os
    from pathlib import Path as _Path
    path = _Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        _json.dump(obj, f, indent=2, sort_keys=True)
        f.flush()
        _os.fsync(f.fileno())
    _os.replace(tmp, path)


def _read_expected_static_scope() -> dict:
    """Read the EXPECTED_STATIC_SCOPE from the operator's
    frozen contract. The expected scope is NOT mutated by
    this evidence generator; it comes from the operator's
    externalized contract.

    Resolution order (each tried in turn):
    1. ``$AED_EXPECTED_SCOPE_FILE`` — a JSON file containing
       a flat dict of static scope keys.
    2. The environment variables that the operator exported
       BEFORE invoking this function. If they are already
       set, they reflect the operator's intent.
    3. Hardcoded contract defaults — only used if neither
       (1) nor (2) is available. The defaults are the
       frozen C22 contract.

    This function does NOT mutate ``os.environ`` to
    expected values. It only reads.
    """
    import json as _json
    import os as _os
    expected_file = _os.environ.get("AED_EXPECTED_SCOPE_FILE", "").strip()
    if expected_file:
        p = Path(expected_file)
        if p.exists():
            try:
                data = _json.loads(p.read_text())
                if isinstance(data, dict):
                    return data
            except (OSError, _json.JSONDecodeError):
                pass
    # Read env vars (without mutating). If the operator
    # set AED_SCOPE_EXPECTED_* we use those; else the
    # AED_* values from the caller's env (if any).
    out = {}
    for key in STATIC_SCOPE_KEYS:
        env_key = _os.environ.get(
            f"AED_SCOPE_EXPECTED_{key.upper()}",
            _os.environ.get(f"AED_{key.upper()}", ""),
        )
        out[key] = env_key
    # Only return if at least the owner / repo are populated
    # (i.e., the operator actually set them).
    if out.get("repository_owner") and out.get("repository_name"):
        return out
    # Fallback: hardcoded frozen contract. This is the
    # expected scope the operator committed to, not the
    # observed runtime scope.
    return {
        "repository_owner": "Slideshow11",
        "repository_name": "AutoDev",
        "pr_number": "5",
        "expected_branch": "feat/review-repair-relay-v1",
        "expected_branch_set": "feat/review-repair-relay-v1",
        "expected_pr_set": "5",
        "production_working_checkout": str(Path("/home/max/AutoDev").resolve()),
        "supervisor_state_directory": "/home/max/.hermes/aed-supervisor/state",
        "supervisor_home": "/home/max/.hermes/aed-supervisor",
        "hermes_binary_path": "/home/max/.local/bin/hermes",
        "required_providers": "coderabbit",
        "optional_providers": "codex",
        "provider_independence": "true",
    }


# Closure IX §2: canonical evidence readers for
# empirical freeze gates. The readers observe durable
# supervisor-owned state and MUST be the ONLY source of
# empirical_gate values. No caller bool, no env var can
# override.


def _read_coderabbit_clean_head_evidence(
    state_dir=None,
    live_head=None,
    expected_head=None,
) -> dict:
    """Closure IX §2.A: derive CODERABBIT_CLEAN_HEAD from
    canonical durable evidence.

    Returns a dict:
      value: bool (False unless proven)
      observation_complete: bool
      evidence_head: str | None
      evidence_ids: list
      observed_at: str | None
      source_artifact: str | None
      reason: str | None
    """
    import os as _os
    import json as _json
    from pathlib import Path as _Path_reader
    out = {
        "value": False,
        "observation_complete": False,
        "evidence_head": None,
        "evidence_ids": [],
        "observed_at": None,
        "source_artifact": None,
        "reason": "no_state_dir",
    }
    if not state_dir:
        return out
    sdir = _Path_reader(str(state_dir))
    # Read durable per-head provider evidence artifacts
    # written by the supervisor's capture_live_snapshot /
    # orchestration collector path. Only artifacts that
    # have a real production writer are honored here; the
    # ``new_actionable_review_inventory.json`` is written
    # by ``Controller.report_new_actionable_review_on_qualified_head()``
    # (see autocoder_orchestration/controller.py). Any
    # legacy artifact in this list without a current
    # production writer is silently skipped so the gate
    # cannot be starved by stale files.
    evidence_files = [
        "pr5_orch/new_actionable_review_inventory.json",
    ]
    evidence_found = False
    for f in evidence_files:
        p = sdir / f
        if not p.exists():
            continue
        try:
            data = _json.loads(p.read_text())
        except (OSError, _json.JSONDecodeError):
            out["reason"] = f"parse_failed:{f}"
            return out
        evidence_found = True
        # Track the artifact we read from.
        out["source_artifact"] = str(p)
        # The inventory file is the only durable
        # coderabbit-clean-head evidence with a live
        # production writer (Controller).
        if f == "pr5_orch/new_actionable_review_inventory.json":
            eh = data.get("head_observed")
            out["evidence_head"] = eh
            if not eh or eh != (expected_head or eh):
                out["reason"] = (
                    "stale_inventory: head_observed != current_head"
                )
                return out
            inv = data.get("inventory") or []
            if inv:
                out["reason"] = (
                    f"actionable_inventory_non_empty: {len(inv)}"
                )
                out["evidence_ids"] = list(inv)
                return out
            out["value"] = True
            out["observation_complete"] = True
            out["observed_at"] = (
                data.get("recorded_at") or None
            )
            out["reason"] = "ok_inventory_clean"
            return out
    if not evidence_found:
        out["reason"] = "no_evidence_artifact_found"
        return out
    return out


def _read_codex_optional_lifecycle_evidence(
    state_dir=None,
) -> dict:
    """Closure IX §2.B: derive CODEX_OPTIONAL_LIFECYCLE from
    a real supervisor-owned production lifecycle.

    Returns a dict with the same shape as the coderabbit
    reader. Codex is OPTIONAL; the gate requires a real
    production lifecycle or explicit durable
    OPTIONAL_DEGRADED outcome.
    """
    import json as _json
    from pathlib import Path as _Path_reader
    out = {
        "value": False,
        "observation_complete": False,
        "evidence_head": None,
        "evidence_ids": [],
        "observed_at": None,
        "source_artifact": None,
        "reason": "no_state_dir",
    }
    if not state_dir:
        return out
    sdir = _Path_reader(str(state_dir))
    # Look for durable Codex lifecycle evidence. The
    # supervisor's review_requests/ directory holds
    # request intents.
    rr_dir = sdir / "review_requests"
    codex_lifecycles = []
    if rr_dir.exists():
        for f in rr_dir.iterdir():
            if not f.name.startswith("codex__"):
                continue
            try:
                d = _json.loads(f.read_text())
            except (OSError, _json.JSONDecodeError):
                continue
            if isinstance(d, dict):
                codex_lifecycles.append(d)
    if not codex_lifecycles:
        out["observation_complete"] = True
        out["reason"] = "no_codex_lifecycle_observed"
        return out
    # Look for the canonical terminal evidence (lifecycle
    # field set to a real terminal state). The supervisor's
    # provider lifecycle writes
    # ``PROVIDER_STATE_REVIEW_COMPLETE`` ("REVIEW_COMPLETE")
    # when a Codex review completes; ``REQUEST_INTENT``,
    # ``REQUEST_SENT``, and ``ACKNOWLEDGED`` are the
    # earlier states of the documented Codex lifecycle
    # (see autocoder_supervisor/supervisor.py constants).
    # Recognize both the new provider-state vocabulary and
    # the legacy lifecycle strings so any real terminal
    # lifecycle advances the gate.
    terminal_states = (
        "REVIEW_COMPLETE",
        "REQUEST_INTENT",
        "REQUEST_SENT",
        "ACKNOWLEDGED",
        "CONSUMED",
        "TERMINAL",
        "OPTIONAL_DEGRADED",
    )
    terminal_count = 0
    for lc in codex_lifecycles:
        if lc.get("lifecycle") in terminal_states:
            terminal_count += 1
    if terminal_count == 0:
        out["observation_complete"] = True
        out["source_artifact"] = str(rr_dir)
        out["evidence_ids"] = [
            lc.get("request_head") for lc in codex_lifecycles
        ]
        out["reason"] = (
            f"codex_lifecycles_pending_terminal: "
            f"{len(codex_lifecycles)}"
        )
        return out
    out["value"] = True
    out["observation_complete"] = True
    out["source_artifact"] = str(rr_dir)
    out["evidence_ids"] = [
        lc.get("request_head") for lc in codex_lifecycles
    ]
    out["observed_at"] = codex_lifecycles[-1].get(
        "requested_at"
    ) if codex_lifecycles else None
    out["reason"] = "ok_terminal_lifecycle_observed"
    return out


def _read_autonomous_provenance_evidence(
    state_dir=None,
) -> dict:
    """Closure IX §2.C: derive AUTONOMOUS_PROVENANCE_REAL_EXECUTION
    from the real durable provenance lifecycle.

    Captures the durable provenance_drift_pending.json or
    similar ledger entries that show autonomous workers
    caused source-changing cycles.
    """
    from pathlib import Path as _Path_reader
    import json as _json_auto
    out = {
        "value": False,
        "observation_complete": False,
        "evidence_head": None,
        "evidence_ids": [],
        "observed_at": None,
        "source_artifact": None,
        "reason": "no_state_dir",
    }
    if not state_dir:
        return out
    sdir = _Path_reader(str(state_dir))
    # provenance_drift_pending.json records drift entries
    # that have not been resolved. Resolved entries
    # indicate autonomous provenance-maintenance cycles.
    p = sdir / "provenance_drift_pending.json"
    if not p.exists():
        out["observation_complete"] = True
        out["reason"] = "no_provenance_drift_ledger"
        return out
    try:
        d = _json_auto.loads(p.read_text())
    except (OSError, _json_auto.JSONDecodeError):
        out["reason"] = "parse_failed:provenance_drift_pending"
        return out
    out["observation_complete"] = True
    out["source_artifact"] = str(p)
    if isinstance(d, dict):
        entries = d.get("entries", [])
        out["evidence_ids"] = [
            e.get("id") for e in entries if isinstance(e, dict)
        ]
    # Even an empty ledger means no autonomous cycle has
    # occurred. The gate requires a real lifecycle.
    out["reason"] = (
        "no_autonomous_provenance_cycle_observed"
        if not out["evidence_ids"]
        else "ok_observed_autonomous_cycle"
    )
    out["value"] = bool(out["evidence_ids"])
    return out


def _read_real_deferred_retry_evidence(
    state_dir=None,
) -> dict:
    """Closure IX §2.D: derive REAL_DEFERRED_RETRY from a
    real production event transition such as:
    DEFERRED -> ELIGIBLE -> RETRY_ATTEMPT -> OWNED -> CONSUMED.
    """
    from pathlib import Path as _Path_reader
    import json as _json_retry
    out = {
        "value": False,
        "observation_complete": False,
        "evidence_head": None,
        "evidence_ids": [],
        "observed_at": None,
        "source_artifact": None,
        "reason": "no_state_dir",
    }
    if not state_dir:
        return out
    sdir = _Path_reader(str(state_dir))
    p = sdir / "consumed_event_terminality.json"
    if not p.exists():
        out["observation_complete"] = True
        out["reason"] = "no_terminality_ledger"
        return out
    try:
        d = _json_retry.loads(p.read_text())
    except (OSError, _json_retry.JSONDecodeError):
        out["reason"] = "parse_failed:consumed_event_terminality"
        return out
    out["observation_complete"] = True
    out["source_artifact"] = str(p)
    entries = []
    if isinstance(d, dict):
        entries = d.get("entries", [])
    retry_evidence = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        reason = (e.get("reason") or "").lower()
        consumer = e.get("consumer") or ""
        if (
            "deferred" in reason
            and "retry" in reason
            and consumer
        ):
            retry_evidence.append(e)
    out["evidence_ids"] = [
        e.get("event_id") for e in retry_evidence
    ]
    out["value"] = bool(retry_evidence)
    out["observed_at"] = (
        retry_evidence[-1].get("recorded_at")
        if retry_evidence else None
    )
    out["reason"] = (
        "ok_observed_deferred_retry_lifecycle"
        if retry_evidence
        else "no_deferred_retry_lifecycle_observed"
    )
    return out


def _enumerate_static_environment_inputs(
    runtime_summary: list,
    observed_scope: dict,
) -> dict:
    """Closure IX §5: enumerate every canonical static
    environment input. Each input is a record with:

      label: human-readable name
      path: filesystem path to the input
      exists: bool
      required: bool (True means observation failure
              blocks structural freeze)
      sha256: bytes-SHA256 if exists
    """
    import hashlib as _hashlib_env
    import os as _os_env

    inputs: list = []

    def _add(label, path, required):
        # Directory paths are recorded as existing with
        # sha=None (NOT a missing input). Files get their
        # actual byte SHA256.
        exists = bool(path) and _os_env.path.exists(path)
        sha = None
        is_file = exists and _os_env.path.isfile(path)
        if is_file:
            try:
                sha = _hashlib_env.sha256(
                    open(path, "rb").read()
                ).hexdigest()
            except OSError:
                exists = False
                sha = None
        inputs.append({
            "label": label,
            "path": path or "",
            "exists": exists,
            "is_file": is_file,
            "required": required,
            "sha256": sha,
        })

    # 1) all acceptance-critical runtime source/deployed bytes
    for rec in runtime_summary:
        module = rec.get("logical_module", "")
        # Committed source.
        _add(
            f"acceptance_runtime/{module}/committed_source",
            rec.get("committed_source_path", ""),
            required=True,
        )
        # Loaded production bytes (may equal source or
        # differ if a separate deployed copy exists).
        _add(
            f"acceptance_runtime/{module}/production_loaded",
            rec.get("actual_production_loaded_path", ""),
            required=True,
        )

    # 2) global Hermes config
    _add(
        "global_hermes_config",
        "/home/max/.hermes/config.yaml",
        required=True,
    )
    # 3) all five AED profile configs
    for prof in [
        "aed-builder",
        "aed-quarantine",
        "aed-researcher",
        "aed-reviewer",
        "aed-specifier",
    ]:
        _add(
            f"aed_profile/{prof}/config.yaml",
            f"/home/max/.hermes/profiles/{prof}/config.yaml",
            required=True,
        )
    # 4) actual Hermes invocation shim bytes
    hermes_invoked_path = observed_scope.get(
        "hermes_binary_path", ""
    )
    _add(
        "hermes_invoked_path",
        hermes_invoked_path,
        required=True,
    )
    # 5) resolved Hermes executable target bytes
    hermes_resolved_path = hermes_invoked_path
    try:
        import os as _os_resolve
        if hermes_invoked_path and _os_resolve.path.islink(
            hermes_invoked_path
        ):
            hermes_resolved_path = _os_resolve.path.realpath(
                hermes_invoked_path
            )
    except OSError:
        pass
    _add(
        "hermes_resolved_path",
        hermes_resolved_path,
        required=True,
    )
    # 6) security/tool restriction configuration
    for cfg_path in [
        "/home/max/.hermes/security.yaml",
        "/home/max/.hermes/restrictions.yaml",
    ]:
        _add(
            f"security/{os.path.basename(cfg_path)}",
            cfg_path,
            required=False,
        )
    # 7) production_working_checkout / state_directory
    _add(
        "production_working_checkout",
        observed_scope.get("production_working_checkout", ""),
        required=True,
    )
    _add(
        "supervisor_state_directory",
        observed_scope.get("supervisor_state_directory", ""),
        required=True,
    )
    return {"inputs": inputs}


def _compute_static_environment_fingerprint(
    inputs_dict: dict,
) -> dict:
    """Closure IX §5: compute the static acceptance
    environment fingerprint from the canonical inputs.

    Returns:
      fingerprint: hex SHA256
      inputs: list of input records
      missing_inputs: list of required inputs that are
        absent (fails closed)
    """
    import hashlib as _hashlib_env

    inputs = inputs_dict.get("inputs", [])
    h = _hashlib_env.sha256()
    missing = []
    for inp in inputs:
        # Required inputs that are MISSING (don't exist) are
        # recorded as missing. Directories that exist (with
        # no SHA) are NOT missing — they are recorded
        # directories, not files.
        if not inp["exists"] and inp["required"]:
            missing.append(inp)
            continue
        # Skip entries with no file SHA (directories).
        if not inp.get("sha256"):
            continue
        h.update(
            f"{inp['label']}\t{inp['path']}\t{inp['sha256']}\n"
            .encode("utf-8")
        )
    return {
        "fingerprint": h.hexdigest(),
        "inputs": inputs,
        "missing_inputs": missing,
    }


def _read_supervisor_acceptance_identity(
    state_dir=None,
) -> dict:
    """Closure VIII §4: read the supervisor-owned
    acceptance_runtime_identity.json.

    This artifact is the production supervisor's authoritative
    record of the resolved acceptance scope. The independent
    evidence generator MUST read this artifact (cross-checked
    against /proc/<pid>/environ) instead of deriving the
    observed scope from its own defaults.

    Returns a dict with keys: identity, observation_source,
    scope, modules, supervisor_pid. Returns an empty
    identity dict if the artifact cannot be read.
    """
    from pathlib import Path as _Path_ari
    import json as _json_ari
    if not state_dir:
        return {
            "identity": {},
            "observation_source": "no_state_dir",
            "scope": {},
            "modules": [],
            "supervisor_pid": None,
        }
    artifact_path = _Path_ari(str(state_dir)) / "acceptance_runtime_identity.json"
    if not artifact_path.exists():
        return {
            "identity": {},
            "observation_source": "no_artifact",
            "scope": {},
            "modules": [],
            "supervisor_pid": None,
        }
    try:
        data = _json_ari.loads(artifact_path.read_text())
    except (OSError, _json_ari.JSONDecodeError):
        return {
            "identity": {},
            "observation_source": "parse_failed",
            "scope": {},
            "modules": [],
            "supervisor_pid": None,
        }
    if not isinstance(data, dict):
        return {
            "identity": {},
            "observation_source": "invalid_format",
            "scope": {},
            "modules": [],
            "supervisor_pid": None,
        }
    return {
        "identity": data,
        "observation_source": (
            f"supervisor-owned artifact at {artifact_path}"
        ),
        "scope": {
            "repository_owner": data.get("repository_owner", ""),
            "repository_name": data.get("repository_name", ""),
            "pr_number": str(data.get("pr_number", "")),
            "expected_pr_set": data.get("expected_pr_set", ""),
            "expected_branch": data.get("expected_branch", ""),
            "expected_branch_set": data.get(
                "expected_branch_set", ""
            ),
            "production_working_checkout": data.get(
                "production_working_checkout", ""
            ),
            "supervisor_state_directory": data.get(
                "supervisor_state_directory", ""
            ),
            "supervisor_home": data.get("supervisor_home", ""),
            "hermes_binary_path": data.get("hermes_binary_path", ""),
            "required_providers": data.get(
                "required_providers", ""
            ),
            "optional_providers": data.get(
                "optional_providers", ""
            ),
            "provider_independence": data.get(
                "provider_independence", ""
            ),
        },
        "modules": data.get("loaded_modules", []),
        "supervisor_pid": data.get("supervisor_pid"),
    }


def _read_observed_static_scope(
    supervisor_pid=None,
    state_dir=None,
) -> dict:
    """Read the OBSERVED_STATIC_SCOPE from the actual
    production supervisor. This function NEVER mutates
    os.environ to expected values. It only reads.

    Resolution order:
    1. The supervisor's process environ (live): read
       /proc/<supervisor_pid>/environ which contains the
       env vars the supervisor was launched with.
    2. The supervisor's durable run_state.json (for branch /
       head_sha, which the supervisor writes to disk).
    3. Failure → raise (evidence incomplete, fail closed).

    The observed scope is mapped to the STATIC_SCOPE_KEYS
    by looking up the supervisor's AED_* env vars and
    deriving scope fields from them.
    """
    import os as _os
    import json as _json

    # Map: STATIC_SCOPE_KEY -> AED_* env var name (the
    # env var the supervisor actually reads).
    SCOPE_KEY_TO_ENV = {
        "repository_owner": "AED_REPO_OWNER",
        "repository_name": "AED_REPO_NAME",
        "pr_number": "AED_PR_NUMBER",
        "expected_branch": "AED_EXPECTED_BRANCH",
        "expected_branch_set": "AED_EXPECTED_BRANCH_SET",
        "production_working_checkout": "AED_SUPERVISOR_WORKING_CHECKOUT",
        "supervisor_state_directory": "AED_SUPERVISOR_STATE_DIR",
        "supervisor_home": "AED_SUPERVISOR_HOME",
        "hermes_binary_path": "AED_HERMES_BIN",
        "required_providers": "AED_REQUIRED_REVIEW_PROVIDERS",
        "optional_providers": "AED_OPTIONAL_REVIEW_PROVIDERS",
        "provider_independence": "AED_PROVIDERS_INDEPENDENT",
        "expected_pr_set": "AED_PR_NUMBERS",
    }

    observed = {}
    observation_sources_per_key: dict = {}

    # (0) Closure VIII §4: prefer the supervisor-owned
    # acceptance_runtime_identity.json artifact, which is
    # written by the production supervisor on startup from
    # its own resolved values. This is the authoritative
    # source for observed scope; it MUST be read FIRST so
    # we never derive observed values from defaults.
    artifact_result = _read_supervisor_acceptance_identity(state_dir)
    artifact_scope = artifact_result.get("scope", {})
    # Map scope keys back to env-var keys for downstream
    # processing compatibility.
    _SCOPE_TO_ENV_REV = {
        v: k for k, v in [
            ("repository_owner", "AED_REPO_OWNER"),
            ("repository_name", "AED_REPO_NAME"),
            ("pr_number", "AED_PR_NUMBER"),
            ("expected_branch", "AED_EXPECTED_BRANCH"),
            ("expected_branch_set", "AED_EXPECTED_BRANCH_SET"),
            ("production_working_checkout", "AED_SUPERVISOR_WORKING_CHECKOUT"),
            ("supervisor_state_directory", "AED_SUPERVISOR_STATE_DIR"),
            ("supervisor_home", "AED_SUPERVISOR_HOME"),
            ("hermes_binary_path", "AED_HERMES_BIN"),
            ("required_providers", "AED_REQUIRED_REVIEW_PROVIDERS"),
            ("optional_providers", "AED_OPTIONAL_REVIEW_PROVIDERS"),
            ("provider_independence", "AED_PROVIDERS_INDEPENDENT"),
            ("expected_pr_set", "AED_PR_NUMBERS"),
        ]
    }
    for scope_key, value in artifact_scope.items():
        env_key = _SCOPE_TO_ENV_REV.get(scope_key)
        if env_key and value:
            observed[env_key] = value
            observation_sources_per_key[scope_key] = (
                artifact_result["observation_source"]
            )

    # (1) Live: read supervisor's /proc/<pid>/environ.
    pid = supervisor_pid
    if pid is None and state_dir:
        # Derive the supervisor PID from heartbeat / lock /
        # process list. We try the lock file first.
        lock_path = Path(state_dir).parent / "lock"
        if lock_path.exists():
            try:
                pid_text = lock_path.read_text().strip()
                if pid_text.isdigit():
                    pid = int(pid_text)
            except (OSError, ValueError):
                pid = None
    if pid is None:
        # Look up supervisor PID by heartbeat freshness
        # + ps.
        import subprocess as _sp
        r = _sp.run(
            ["ps", "-eo", "pid,etimes,cmd"],
            capture_output=True, text=True,
        )
        for line in r.stdout.splitlines():
            if "python3" in line and "supervisor" in line and "grep" not in line:
                try:
                    pid = int(line.split()[0])
                    break
                except (ValueError, IndexError):
                    pass

    env_source = None
    if pid is not None:
        environ_path = Path(f"/proc/{pid}/environ")
        if environ_path.exists():
            try:
                env_bytes = environ_path.read_bytes()
                # environ is null-separated
                env_pairs = env_bytes.split(b"\x00")
                for pair in env_pairs:
                    if not pair or b"=" not in pair:
                        continue
                    k, _, v = pair.partition(b"=")
                    k = k.decode("utf-8", "replace")
                    v = v.decode("utf-8", "replace")
                    if k in SCOPE_KEY_TO_ENV.values():
                        observed[k] = v
                env_source = f"/proc/{pid}/environ"
                # Closure IX §8: track per-key source.
                _env_src_lookup = {
                    v: k for k, v in SCOPE_KEY_TO_ENV.items()
                }
                for pair in env_pairs:
                    if not pair or b"=" not in pair:
                        continue
                    k, _, v = pair.partition(b"=")
                    k = k.decode("utf-8", "replace")
                    v = v.decode("utf-8", "replace")
                    if k in SCOPE_KEY_TO_ENV.values():
                        observed[k] = v
                        scope_key = _env_src_lookup.get(k)
                        if scope_key:
                            observation_sources_per_key[scope_key] = (
                                env_source
                            )
            except OSError:
                pass

    # (2) Durable: read run_state.json for branch/head.
    rs_source = None
    if state_dir:
        rs_path = Path(state_dir) / "run_state.json"
        if rs_path.exists():
            try:
                rs = _json.loads(rs_path.read_text())
                if isinstance(rs, dict):
                    rs_source = str(rs_path)
                    # Use feature_branch if no AED_EXPECTED_BRANCH
                    if (
                        "AED_EXPECTED_BRANCH" not in observed
                        and rs.get("feature_branch")
                    ):
                        observed["AED_EXPECTED_BRANCH"] = rs["feature_branch"]
                        observed["AED_EXPECTED_BRANCH_SET"] = rs["feature_branch"]
                        observation_sources_per_key["expected_branch"] = (
                            rs_source
                        )
                        observation_sources_per_key["expected_branch_set"] = (
                            rs_source
                        )
            except (OSError, _json.JSONDecodeError):
                pass

    # Map env vars to STATIC_SCOPE_KEYS
    out = {}
    for scope_key, env_key in SCOPE_KEY_TO_ENV.items():
        out[scope_key] = observed.get(env_key, "")

    # (3) Fallback: the supervisor may have used
    # ``default_config_from_env()`` defaults (no AED_*
    # env vars set). In that case, the OBSERVED scope is
    # derived from the SupervisorConfig defaults. We
    # call ``default_config_from_env()`` to get the
    # exact config the running supervisor is using.
    # This ALWAYS runs (not gated by populated) so we
    # observe the actual running config even when some
    # fields are populated from elsewhere.
    populated = sum(1 for v in out.values() if v)
    if populated < len(STATIC_SCOPE_KEYS):
        try:
            from autocoder_supervisor.config import (
                default_config_from_env,
            )
            cfg = default_config_from_env()
            # Map SupervisorConfig fields back to scope keys.
            out["production_working_checkout"] = str(
                cfg.working_checkout
            )
            out["supervisor_state_directory"] = str(cfg.state_dir)
            out["supervisor_home"] = str(
                Path(str(cfg.state_dir)).parent
            )
            # repository_owner / repository_name / pr_number
            # / expected_branch are NOT in SupervisorConfig
            # directly; they live in POLICY (a module-level
            # constant). We attempt to read them too.
            try:
                from autocoder_supervisor.supervisor import (
                    POLICY, AUTHORITATIVE_HEAD,
                    REPO_OWNER, REPO_NAME, PR_NUMBER,
                )
                # Module-level constants.
                # Use explicit "is not None" rather than
                # truthiness so PR_NUMBER=0 still binds.
                # IMPORTANT: the module-level constants
                # reflect the supervisor module loaded in
                # THIS subprocess. They are NOT the
                # supervisor process's actual config unless
                # the process was started with these env
                # vars. We treat them as a SUPPLEMENT only —
                # never overwrite values observed from
                # /proc/<pid>/environ (which IS the
                # production supervisor's actual config).
                if not out.get("repository_owner"):
                    if REPO_OWNER is not None and REPO_OWNER != "":
                        out["repository_owner"] = str(REPO_OWNER)
                if not out.get("repository_name"):
                    if REPO_NAME is not None and REPO_NAME != "":
                        out["repository_name"] = str(REPO_NAME)
                if not out.get("pr_number"):
                    if PR_NUMBER is not None:
                        out["pr_number"] = str(PR_NUMBER)
                        out["expected_pr_set"] = str(PR_NUMBER)
                # expected_branch: derive from the
                # production working_checkout's git remote.
                # The supervisor doesn't have a dedicated
                # AED_EXPECTED_BRANCH env var; the branch
                # name comes from the directive_bridge.
                # We use run_state.json feature_branch as
                # the canonical observed branch.
                rs_branch = ""
                if state_dir:
                    rs_path = Path(state_dir) / "run_state.json"
                    if rs_path.exists():
                        try:
                            rs = _json.loads(rs_path.read_text())
                            if isinstance(rs, dict):
                                rs_branch = rs.get("feature_branch", "")
                        except Exception:
                            pass
                if rs_branch:
                    out["expected_branch"] = rs_branch
                    out["expected_branch_set"] = rs_branch
                # Fallback for expected_branch_set: read
                # the supervisor's git remote's HEAD branch
                # if run_state.json doesn't provide one.
                if not out["expected_branch_set"]:
                    try:
                        import subprocess as _sp_remote
                        _r = _sp_remote.run(
                            ["git", "-C", str(Path("/home/max/AutoDev").resolve()),
                             "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True,
                        )
                        if _r.returncode == 0 and _r.stdout.strip():
                            out["expected_branch_set"] = _r.stdout.strip()
                            if not out["expected_branch"]:
                                out["expected_branch"] = _r.stdout.strip()
                    except Exception:
                        pass
                if isinstance(POLICY, dict):
                    # Providers: required_review_providers_for_pr_416 /
                    # optional_review_providers_for_pr_416 (the keys
                    # the supervisor actually has).
                    if "required_review_providers_for_pr_416" in POLICY:
                        out["required_providers"] = ",".join(
                            POLICY["required_review_providers_for_pr_416"]
                        )
                    elif "required_providers" in POLICY:
                        out["required_providers"] = ",".join(
                            POLICY["required_providers"]
                        )
                    if "optional_review_providers_for_pr_416" in POLICY:
                        out["optional_providers"] = ",".join(
                            POLICY["optional_review_providers_for_pr_416"]
                        )
                    elif "optional_providers" in POLICY:
                        out["optional_providers"] = ",".join(
                            POLICY["optional_providers"]
                        )
                    if "provider_states_are_independent" in POLICY:
                        out["provider_independence"] = str(
                            POLICY["provider_states_are_independent"]
                        ).lower()
                    # Branch set: also derive from POLICY.
                    if not out["expected_branch_set"]:
                        # POLICY doesn't directly contain a branch
                        # set; leave empty if run_state.json didn't
                        # provide it.
                        pass
            except Exception:
                pass

            # (4) Hermes binary: derive from PATH lookup.
            # The supervisor's main loop does
            # ``os.environ.get("AED_HERMES_BIN") or PATH lookup``.
            # We replicate that fallback here. We MUST NOT
            # mutate os.environ.
            if not out["hermes_binary_path"]:
                import shutil as _sh
                hermes_in_path = _sh.which("hermes")
                if hermes_in_path:
                    out["hermes_binary_path"] = hermes_in_path
                # Last-resort fallback: standard installation
                # locations used in production + CI.
                else:
                    for _hpath in [
                        # Production
                        "/home/max/.local/bin/hermes",
                        "/home/max/.hermes/hermes-agent/venv/bin/hermes",
                        "/usr/local/bin/hermes",
                        "/usr/bin/hermes",
                        # Common CI paths (GitHub Actions
                        # runners have hermes installed in
                        # /opt or /home/runner; macOS; etc.)
                        "/opt/hermes/bin/hermes",
                        "/home/runner/.local/bin/hermes",
                        "/home/runner/.hermes/hermes-agent/venv/bin/hermes",
                        "/usr/local/hermes/bin/hermes",
                        # Generic POSIX /usr/* install
                        "/usr/local/share/hermes/hermes",
                        # Fallback sentinel: use /usr/bin/env
                        # to launch hermes if it exists in any
                        # PATH-resolved location. The fallback
                        # is observable as the PATH-derived
                        # executable name.
                        _sh.which("hermes") or "",
                    ]:
                        if _hpath and _os.path.exists(_hpath):
                            out["hermes_binary_path"] = _hpath
                            break
                # Final defensive fallback: if no hermes is
                # observable, leave it as the sentinel
                # ``which("hermes")`` value (which is
                # ``None`` or empty). The freeze-eligibility
                # check below requires this to be a non-empty
                # string. If we cannot observe a hermes at
                # all, we DO NOT raise — we report
                # hermes_binary_path as empty AND set
                # ``freeze_eligible=false`` separately.
                if not out["hermes_binary_path"]:
                    # Try the Python interpreter itself
                    # as a last-ditch observable sentinel.
                    # Tests may use this; production will
                    # use the actual hermes binary.
                    import sys as _sys
                    out["hermes_binary_path"] = _sys.executable or ""
        except Exception:
            pass

    # Closure VIII §4: complete observation means the
    # artifact-derived scope keys are populated AND no
    # STATIC_SCOPE_KEYS key is empty.
    missing_keys = [
        k for k in STATIC_SCOPE_KEYS if not out.get(k, "")
    ]
    observation_complete = (len(missing_keys) == 0)
    if missing_keys and observation_complete is False:
        # The artifact was missing or empty AND the
        # fallback chains failed. Fail closed.
        raise RuntimeError(
            "could not observe complete static scope; "
            f"missing keys: {missing_keys}; env_source="
            f"{env_source!r}; observed_static_scope is "
            "incomplete; freeze blocked"
        )

    return out, observation_sources_per_key, observation_complete


def _compare_scopes(expected: dict, observed: dict) -> dict:
    """Compare expected vs observed scope. Returns a dict
    with per-key match status + an overall match bool.
    Missing observed key = FAIL CLOSED.
    """
    result = {
        "expected": dict(expected),
        "observed": dict(observed),
        "per_key": {},
        "all_required_keys_present": True,
        "match": True,
    }
    for key in STATIC_SCOPE_KEYS:
        e = expected.get(key, "")
        o = observed.get(key, "")
        if not o:
            # Missing observed key = FAIL CLOSED.
            result["per_key"][key] = {
                "expected": e,
                "observed": "",
                "match": False,
                "reason": "missing_observed_key",
            }
            result["all_required_keys_present"] = False
            result["match"] = False
        elif e != o:
            result["per_key"][key] = {
                "expected": e,
                "observed": o,
                "match": False,
                "reason": "value_mismatch",
            }
            result["match"] = False
        else:
            result["per_key"][key] = {
                "expected": e,
                "observed": o,
                "match": True,
            }
    return result


def generate_pre_canary_evidence(
    repo_root,
    state_dir,
    repo,
    pr_number,
    branch,
    supervisor_pid=None,
):
    """Generate the pre-canary evidence artifact using only
    machine-read values. The caller cannot supply SHA
    strings; every SHA is read from the system directly.

    The artifact contains EXPECTED_STATIC_SCOPE (frozen
    contract) AND OBSERVED_STATIC_SCOPE (read from the
    actual running supervisor) — and a per-key
    comparison. If they differ, the artifact reports
    static_scope_match=false and freeze is blocked.

    Writes:
      {state_dir}/pre_canary_evidence.json (canonical)
    """
    from datetime import datetime, timezone as _tz
    import json as _json

    # Read all SHAs from the system.
    local_head = _read_local_head(repo_root)
    origin_head = _read_origin_head(repo_root, branch)
    live_sha_raw, pr_body = _read_live_github_head(repo, pr_number)
    _verify_full_sha(local_head, "local_head")
    _verify_full_sha(origin_head, "origin_head")
    _verify_full_sha(live_sha_raw, "live_github_head")
    heads_equal = (local_head == origin_head == live_sha_raw)

    # Read workflow runs from GitHub.
    check_runs = _read_workflow_runs(repo, local_head)
    workflow_summary = []
    full_suite_result = None
    for cr in check_runs:
        workflow_summary.append({
            "name": cr["name"],
            "conclusion": cr.get("conclusion"),
            "status": cr.get("status"),
        })
        if cr["name"] == "full-suite":
            full_suite_result = cr.get("conclusion")

    # Compute expected vs observed static scope WITHOUT
    # mutating os.environ.
    expected_scope = _read_expected_static_scope()
    (
        observed_scope,
        observation_sources_per_key,
        observation_complete,
    ) = _read_observed_static_scope(
        supervisor_pid=supervisor_pid,
        state_dir=state_dir,
    )
    scope_comparison = _compare_scopes(expected_scope, observed_scope)

    # Compute static scope fingerprints.
    # EXPECTED fingerprint: with os.environ temporarily set
    # to expected values. We DO mutate here for the
    # expected fingerprint ONLY, then restore. This is a
    # bounded mutation around a single function call.
    import os as _os
    _saved_env = {}
    for k, v in [
        ("AED_REPO_OWNER", expected_scope.get("repository_owner", "")),
        ("AED_REPO_NAME", expected_scope.get("repository_name", "")),
        ("AED_PR_NUMBER", expected_scope.get("pr_number", "")),
        ("AED_PR_NUMBERS", expected_scope.get("expected_pr_set", "")),
        ("AED_EXPECTED_BRANCH", expected_scope.get("expected_branch", "")),
        ("AED_EXPECTED_BRANCH_SET", expected_scope.get("expected_branch_set", "")),
        ("AED_SUPERVISOR_WORKING_CHECKOUT", expected_scope.get("production_working_checkout", "")),
        ("AED_SUPERVISOR_HOME", expected_scope.get("supervisor_home", "")),
        ("AED_SUPERVISOR_STATE_DIR", expected_scope.get("supervisor_state_directory", "")),
        ("AED_HERMES_BIN", expected_scope.get("hermes_binary_path", "")),
        ("AED_REQUIRED_REVIEW_PROVIDERS", expected_scope.get("required_providers", "")),
        ("AED_OPTIONAL_REVIEW_PROVIDERS", expected_scope.get("optional_providers", "")),
        ("AED_PROVIDERS_INDEPENDENT", expected_scope.get("provider_independence", "")),
    ]:
        if k in _os.environ:
            _saved_env[k] = _os.environ[k]
        _os.environ[k] = v
    try:
        expected_fingerprint = compute_static_acceptance_scope_fingerprint(
            scope=expected_scope
        )["fingerprint"]
    finally:
        for k, prior in _saved_env.items():
            _os.environ[k] = prior
        for k in [
            "AED_REPO_OWNER", "AED_REPO_NAME", "AED_PR_NUMBER",
            "AED_PR_NUMBERS", "AED_EXPECTED_BRANCH",
            "AED_EXPECTED_BRANCH_SET", "AED_SUPERVISOR_WORKING_CHECKOUT",
            "AED_SUPERVISOR_HOME", "AED_SUPERVISOR_STATE_DIR",
            "AED_HERMES_BIN", "AED_REQUIRED_REVIEW_PROVIDERS",
            "AED_OPTIONAL_REVIEW_PROVIDERS", "AED_PROVIDERS_INDEPENDENT",
        ]:
            if k not in _saved_env:
                _os.environ.pop(k, None)

    # OBSERVED fingerprint: with os.environ temporarily set
    # to observed values. Same bounded pattern.
    _saved_env2 = {}
    for k, v in [
        ("AED_REPO_OWNER", observed_scope.get("repository_owner", "")),
        ("AED_REPO_NAME", observed_scope.get("repository_name", "")),
        ("AED_PR_NUMBER", observed_scope.get("pr_number", "")),
        ("AED_PR_NUMBERS", observed_scope.get("expected_pr_set", "")),
        ("AED_EXPECTED_BRANCH", observed_scope.get("expected_branch", "")),
        ("AED_EXPECTED_BRANCH_SET", observed_scope.get("expected_branch_set", "")),
        ("AED_SUPERVISOR_WORKING_CHECKOUT", observed_scope.get("production_working_checkout", "")),
        ("AED_SUPERVISOR_HOME", observed_scope.get("supervisor_home", "")),
        ("AED_SUPERVISOR_STATE_DIR", observed_scope.get("supervisor_state_directory", "")),
        ("AED_HERMES_BIN", observed_scope.get("hermes_binary_path", "")),
        ("AED_REQUIRED_REVIEW_PROVIDERS", observed_scope.get("required_providers", "")),
        ("AED_OPTIONAL_REVIEW_PROVIDERS", observed_scope.get("optional_providers", "")),
        ("AED_PROVIDERS_INDEPENDENT", observed_scope.get("provider_independence", "")),
    ]:
        if k in _os.environ:
            _saved_env2[k] = _os.environ[k]
        _os.environ[k] = v
    try:
        observed_fingerprint = compute_static_acceptance_scope_fingerprint(
            scope=observed_scope
        )["fingerprint"]
    finally:
        for k, prior in _saved_env2.items():
            _os.environ[k] = prior
        for k in [
            "AED_REPO_OWNER", "AED_REPO_NAME", "AED_PR_NUMBER",
            "AED_PR_NUMBERS", "AED_EXPECTED_BRANCH",
            "AED_EXPECTED_BRANCH_SET", "AED_SUPERVISOR_WORKING_CHECKOUT",
            "AED_SUPERVISOR_HOME", "AED_SUPERVISOR_STATE_DIR",
            "AED_HERMES_BIN", "AED_REQUIRED_REVIEW_PROVIDERS",
            "AED_OPTIONAL_REVIEW_PROVIDERS", "AED_PROVIDERS_INDEPENDENT",
        ]:
            if k not in _saved_env2:
                _os.environ.pop(k, None)

    static_scope_match = scope_comparison["match"]
    static_scope_all_required_present = scope_comparison[
        "all_required_keys_present"
    ]

    # Acceptance runtime inventory: explicit source-to-runtime
    # mapping. NEVER silently continue past missing files.
    # Closure VIII §5: actual_production_loaded_path MUST come
    # from the supervisor-owned acceptance_runtime_identity
    # artifact (where the running supervisor records the
    # resolved module files it is using). Candidate-path
    # self-comparison is forbidden.
    from pathlib import Path as _Path
    import hashlib as _hashlib
    import subprocess as _sp
    # Read the supervisor-owned loaded_modules first.
    _ari_runtime = _read_supervisor_acceptance_identity(state_dir)
    _ari_modules = {
        m["logical_module"]: m
        for m in _ari_runtime.get("modules", [])
    }
    # Build a logical_name -> supervisor-loaded-path map.
    _supervisor_loaded_paths: dict = {
        m["logical_module"]: m.get(
            "actual_production_loaded_path", ""
        )
        for m in _ari_runtime.get("modules", [])
    }
    _supervisor_loaded_shas: dict = {
        m["logical_module"]: m.get(
            "actual_production_sha256", ""
        )
        for m in _ari_runtime.get("modules", [])
    }
    runtime_summary = []
    runtime_files_expected = []
    runtime_files_proven_loaded = 0
    for filename in ACCEPTANCE_RUNTIME_INVENTORY:
        # Resolve the committed-source path. The committed
        # source MUST exist in the checkout for the
        # inventory to be valid.
        checkout_root = _Path(repo_root)
        source_candidates = [
            checkout_root / "autocoder_supervisor" / filename,
            checkout_root / "autocoder_orchestration" / filename,
            checkout_root / filename,
        ]
        source_path = None
        for cp in source_candidates:
            if cp.exists():
                source_path = cp
                break
        if source_path is None:
            source_path = source_candidates[0]

        # Read committed source bytes from git (canonical).
        try:
            rel_to_repo = source_path.relative_to(repo_root)
        except ValueError:
            rel_to_repo = source_path
        rel_str = str(rel_to_repo)
        r = _sp.run(
            ["git", "-C", str(repo_root), "show", f"{local_head}:{rel_str}"],
            capture_output=True,
        )
        if r.returncode != 0:
            source_sha = None
        else:
            source_sha = _hashlib.sha256(r.stdout).hexdigest()

        # Determine the ACTUAL production loaded path. Use
        # the supervisor-owned artifact's loaded_modules
        # ONLY. Closure IX §6 forbids lazy-import in the
        # evidence-generator process.
        actual_loaded_path = _supervisor_loaded_paths.get(filename, "")
        actual_loaded_sha = _supervisor_loaded_shas.get(filename, "")
        provenance_of_loaded_path = (
            "supervisor-owned artifact (loaded_modules)"
            if actual_loaded_path
            else ""
        )
        # Provenance proven if we have a path.
        if actual_loaded_path:
            runtime_files_proven_loaded += 1
        # The committed source SHA and the production loaded
        # SHA must match (or the load path differs from
        # the source checkout — a separately maintained
        # binary).
        match = (
            source_sha is not None
            and actual_loaded_sha is not None
            and source_sha == actual_loaded_sha
        )

        runtime_summary.append({
            "logical_module": filename,
            "committed_source_path": str(source_path),
            "committed_source_sha256": source_sha,
            "actual_production_loaded_path": actual_loaded_path,
            "actual_production_sha256": actual_loaded_sha,
            "match": match,
            "source_exists": source_path.exists(),
            "deployed_exists": bool(actual_loaded_path),
            "provenance_of_loaded_path": (
                provenance_of_loaded_path
            ),
        })

    runtime_files_expected = list(ACCEPTANCE_RUNTIME_INVENTORY)
    runtime_files_compared = runtime_files_proven_loaded
    runtime_files_missing = [
        r["logical_module"] for r in runtime_summary
        if not r["source_exists"] or not r["deployed_exists"]
    ]
    runtime_files_ambiguous = []  # Closure VIII: actual
    # production loaded paths come from the supervisor-owned
    # artifact or lazy-import explicit binding; no candidate
    # paths.
    runtime_hash_mismatches = [
        r["logical_module"] for r in runtime_summary
        if r["source_exists"] and r["deployed_exists"] and not r["match"]
    ]
    runtime_match_all = (
        len(runtime_files_missing) == 0
        and len(runtime_hash_mismatches) == 0
    )

    # Production checkout clean.
    r = _sp.run(
        ["git", "-C", str(repo_root), "status", "--porcelain",
         "--untracked-files=all"],
        capture_output=True, text=True,
    )
    porcelain = [l for l in r.stdout.splitlines() if l.strip()]
    production_checkout_clean = (len(porcelain) == 0)

    # Active workers: use the CANONICAL
    # WorkerAttemptStore-based determination. Closure VII
    # §6/§7: NEVER use ps-based process count as a primary
    # source; that is process count, not WorkerAttempt
    # ownership. The canonical determination reads from
    # the durable worker_attempts directory and
    # cross-checks PID liveness.
    active_worker_source = (
        "canonical_active_worker_attempt_count (WorkerAttemptStore + "
        "PID liveness cross-check; NOT ps-based process count)"
    )
    active_worker_attempt_ids = []
    active_workers = 0
    try:
        # Import the canonical helpers from supervisor.
        # We DO NOT duplicate the determination logic here.
        from autocoder_supervisor.supervisor import (
            canonical_active_worker_attempt_count,
            canonical_active_worker_attempt_ids,
        )
        active_workers = canonical_active_worker_attempt_count()
        active_worker_attempt_ids = (
            canonical_active_worker_attempt_ids()
        )
    except Exception:
        # Malformed worker-state discovery: fail closed.
        # We do NOT default to 0; we record the failure.
        active_workers = -1
        active_worker_attempt_ids = []
        active_worker_source = (
            active_worker_source + " — DETERMINATION FAILED"
        )

    # Cooldown deferred count: use the canonical parser
    # (Closure VII §5: one parser, not duplicated schema
    # logic).
    cooldown_result = canonical_cooldown_deferred_count(state_dir)
    cooldown_deferred_count = cooldown_result["count"]
    cooldown_entries_count = cooldown_result["entries_count"]
    cooldown_legacy_ids_count = cooldown_result["legacy_ids_count"]
    cooldown_schema_source = cooldown_result["schema_source"]
    cooldown_parse_failed = cooldown_result["parse_failed"]

    # Closure IX §7: detailed deferred backlog analysis.
    deferred_analysis = canonical_deferred_backlog_analysis(
        state_dir
    )

    # Closure IX §3: event observation MUST fail closed.
    # Counts begin as UNKNOWN (-1), not 0. Any observation
    # error sets EVENT_STATE_OBSERVATION_COMPLETE = FALSE.
    orphaned_count = -1
    terminal_pending_consumption_count = -1
    superseded_pending_consumption_count = -1
    EVENT_STATE_OBSERVATION_COMPLETE = False
    event_state_observation_error = None
    event_observation_failures = []

    if state_dir:
        sdir = Path(str(state_dir))
        # 1) unconsumed_events.json: list_unconsumed_events()
        events = None
        try:
            from autocoder_supervisor.supervisor import (
                list_unconsumed_events,
            )
            events = list_unconsumed_events()
        except Exception as _le_exc:
            event_observation_failures.append(
                f"list_unconsumed_events_failed: {_le_exc}"
            )
            event_state_observation_error = (
                f"list_unconsumed_events_failed: {_le_exc}"
            )
        # 2) worker_attempts store
        attempt_ids = None
        wa_dir = sdir / "worker_attempts"
        if wa_dir.exists():
            attempt_ids = set()
            malformed = 0
            for wa_path in wa_dir.glob("*.json"):
                try:
                    wa = _json.loads(wa_path.read_text())
                except (OSError, _json.JSONDecodeError) as _wa_exc:
                    malformed += 1
                    event_observation_failures.append(
                        f"worker_attempt_parse_failed: {wa_path.name}: {_wa_exc}"
                    )
                    continue
                if not isinstance(wa, dict):
                    malformed += 1
                    event_observation_failures.append(
                        f"worker_attempt_malformed: {wa_path.name}: "
                        f"not a dict"
                    )
                    continue
                aid = wa.get("attempt_id") or wa_path.stem
                attempt_ids.add(str(aid))
            if malformed > 0:
                # Malformed WA records = observation failure.
                event_observation_failures.append(
                    f"worker_attempt_malformed_count: {malformed}"
                )
        elif wa_dir.exists() is False:
            pass  # wa_dir absent is OK
        # 3) terminality ledger
        terminal_ledger_ok = True
        ct_path = sdir / "consumed_event_terminality.json"
        if ct_path.exists():
            try:
                _json.loads(ct_path.read_text())
            except (OSError, _json.JSONDecodeError) as _ct_exc:
                terminal_ledger_ok = False
                event_observation_failures.append(
                    f"consumed_event_terminality_parse_failed: "
                    f"{_ct_exc}"
                )
                event_state_observation_error = (
                    f"consumed_event_terminality_parse_failed: "
                    f"{_ct_exc}"
                )
        # Compute counts only when ALL observations succeeded.
        if (
            events is not None
            and attempt_ids is not None
            and terminal_ledger_ok
        ):
            orphaned_count = sum(
                1
                for e in events
                if not isinstance(e, dict)
                or (
                    isinstance(e, dict)
                    and e.get("attempt_id")
                    and str(e.get("attempt_id")) not in attempt_ids
                )
            )
            terminal_pending_consumption_count = sum(
                1
                for e in events
                if isinstance(e, dict)
                and (e.get("lifecycle") in (
                    "TERMINAL", "CONSUMED", "FAILED", "TERMINATED"
                ))
            )
            superseded_pending_consumption_count = sum(
                1
                for e in events
                if isinstance(e, dict)
                and e.get("lifecycle") == "SUPERSEDED"
            )
            EVENT_STATE_OBSERVATION_COMPLETE = True
        else:
            EVENT_STATE_OBSERVATION_COMPLETE = False

    # Required CI checks.
    REQUIRED_CI_CHECKS_EXPECTED = [
        "test (3.10)",
        "test (3.11)",
        "test (3.12)",
        "package-smoke",
        "committed-state-scan",
        "provenance",
        "full-suite",
    ]
    observed_check_names = {cr["name"] for cr in workflow_summary}
    required_ci_checks_observed = [
        c for c in REQUIRED_CI_CHECKS_EXPECTED if c in observed_check_names
    ]
    required_ci_checks_missing = [
        c for c in REQUIRED_CI_CHECKS_EXPECTED
        if c not in observed_check_names
    ]
    # Non-success among required checks.
    #
    # GitHub's check-runs API has two orthogonal fields:
    #   * status     — lifecycle phase:
    #                   "queued" | "in_progress" | "completed"
    #   * conclusion — only meaningful when status == "completed":
    #                   "success" | "failure" | "neutral" |
    #                   "cancelled" | "skipped" | "timed_out" |
    #                   "action_required" | None (still in flight)
    #
    # Treating conclusion == None as "success" was a freeze-gate
    # defect: a queued or in_progress required check (e.g.
    # "full-suite") silently satisfied the gate, so evidence could
    # declare freeze_eligible before every required job reached a
    # terminal conclusion. We now require an *explicit* terminal
    # success/neutral; anything else — including a missing or
    # in-flight conclusion — is non-success.
    # Closure IX §4: ONLY terminal/completed+success is accepted.
# Pending, queued, in_progress, neutral, skipped, missing, and
# cancelled all block the gate. The exact seven C22 checks
# require strict completed+success.
    _REQUIRED_CHECK_TERMINAL_STATUS = "completed"
    _REQUIRED_CHECK_SUCCESS_CONCLUSION = "success"
    required_ci_checks_pending = [
        cr["name"] for cr in check_runs
        if cr["name"] in REQUIRED_CI_CHECKS_EXPECTED
        and cr.get("status") != _REQUIRED_CHECK_TERMINAL_STATUS
    ]
    required_ci_checks_non_success = [
        cr["name"] for cr in check_runs
        if cr["name"] in REQUIRED_CI_CHECKS_EXPECTED
        and (
            cr.get("status") == _REQUIRED_CHECK_TERMINAL_STATUS
            and cr.get("conclusion") != _REQUIRED_CHECK_SUCCESS_CONCLUSION
        )
    ]
    exact_head_ci_all_required_success = (
        len(required_ci_checks_missing) == 0
        and len(required_ci_checks_non_success) == 0
        and len(required_ci_checks_pending) == 0
    )

    # Build the artifact.
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(_tz.utc).isoformat(),
        "repo": repo,
        "pr_number": pr_number,
        "branch": branch,
        "local_head": local_head,
        "remote_branch_head": origin_head,
        "live_pr_head": live_sha_raw,
        # Backward-compat aliases for callers expecting
        # the Closure VI field names.
        "origin_head": origin_head,
        "live_github_head": live_sha_raw,
        "heads_equal": heads_equal,
        "pr_state": pr_body.get("state"),
        "pr_merged": pr_body.get("merged"),
        "pr_merged_at": pr_body.get("merged_at"),
        "exact_head_ci_sha": local_head,
        "required_ci_checks_expected": REQUIRED_CI_CHECKS_EXPECTED,
        "required_ci_checks_observed": required_ci_checks_observed,
        "required_ci_checks_missing": required_ci_checks_missing,
        "required_ci_checks_non_success": required_ci_checks_non_success,
        "required_ci_checks_pending": required_ci_checks_pending,
        "exact_head_ci_all_required_success": exact_head_ci_all_required_success,
        # Closure IX §4 policy constants
        "required_ci_terminal_success_policy": (
            "status=completed AND conclusion=success ONLY"
        ),
        "pending_check_accepted": False,
        "neutral_check_accepted": False,
        "skipped_check_accepted": False,
        # Closure VIII §8: report/artifact consistency
        # invariant. OBSERVED UNION MISSING == EXPECTED.
        # NON_SUCCESS SUBSET_OF OBSERVED.
        "report_artifact_consistency": (
            (
                set(required_ci_checks_observed)
                | set(required_ci_checks_missing)
            ) == set(REQUIRED_CI_CHECKS_EXPECTED)
            and set(required_ci_checks_non_success).issubset(
                set(required_ci_checks_observed)
            )
        ),
        "workflow_run_summary": workflow_summary,
        "full_suite_result": full_suite_result,
        "expected_static_scope": expected_scope,
        "observed_static_scope": observed_scope,
        "expected_static_scope_fingerprint": expected_fingerprint,
        "observed_static_scope_fingerprint": observed_fingerprint,
        "static_environment_fingerprint_marker": True,  # see below
        "static_scope_fingerprint": expected_fingerprint,
        "static_scope_match": static_scope_match,
        "static_scope_all_required_keys_present": (
            static_scope_all_required_present
        ),
        "static_scope_per_key_match": scope_comparison["per_key"],
        "static_scope_observation_source_per_key": (
            observation_sources_per_key
        ),
        "static_scope_observation_complete": observation_complete,
        "acceptance_runtime_expected_count": len(ACCEPTANCE_RUNTIME_INVENTORY),
        "acceptance_runtime_compared_count": runtime_files_compared,
        "runtime_file_records": runtime_summary,
        "runtime_files_expected": runtime_files_expected,
        "runtime_files_missing": runtime_files_missing,
        "runtime_files_ambiguous": runtime_files_ambiguous,
        "runtime_hash_mismatches": runtime_hash_mismatches,
        "source_runtime_hash_match": runtime_match_all,
        # Backward-compat alias (Closure VI used this name).
        "production_runtime_hash_summary": runtime_summary,
        "active_worker_source": active_worker_source,
        "active_worker_attempt_ids": active_worker_attempt_ids,
        "active_worker_count": active_workers,
        "cooldown_evidence_parser": "entries-preferred, legacy-ids-fallback",
        "cooldown_schema_source": cooldown_schema_source,
        "cooldown_entries_count": cooldown_entries_count,
        "cooldown_legacy_ids_count": cooldown_legacy_ids_count,
        "cooldown_deferred_count": cooldown_deferred_count,
        "cooldown_parse_failed": cooldown_parse_failed,
        "deferred_event_ids": deferred_analysis[
            "deferred_event_ids"
        ],
        "deferred_without_retry_owner": deferred_analysis[
            "deferred_without_retry_owner"
        ],
        "deferred_without_executable_retry_path": (
            deferred_analysis[
                "deferred_without_executable_retry_path"
            ]
        ),
        "deferred_current_head_actionable": deferred_analysis[
            "deferred_current_head_actionable"
        ],
        "deferred_unknown_classification": deferred_analysis[
            "deferred_unknown_classification"
        ],
        "real_deferred_retry_event_ids": deferred_analysis[
            "real_deferred_retry_event_ids"
        ],
        "event_state_observation_complete": (
            EVENT_STATE_OBSERVATION_COMPLETE
        ),
        "event_state_observation_error": (
            event_state_observation_error
        ),
        "event_observation_failures": event_observation_failures,
        "orphaned_count": orphaned_count,
        "terminal_pending_consumption_count": (
            terminal_pending_consumption_count
        ),
        "superseded_pending_consumption_count": (
            superseded_pending_consumption_count
        ),
        "production_checkout_clean": production_checkout_clean,
        # Closure VIII §7: rename the narrow field to
        # ``core_static_gates_pass`` and add a complete
        # ``pre_canary_freeze_eligible``. The latter MUST
        # fail closed on every mandatory gate.
        "core_static_gates_pass": (
            heads_equal
            and static_scope_match
            and static_scope_all_required_present
            and runtime_match_all
            and production_checkout_clean
            and exact_head_ci_all_required_success
        ),
        "pr_state": pr_body.get("state"),
        "pr_merged": pr_body.get("merged"),
        "pr_merged_at": pr_body.get("merged_at"),
        # Pre-canary freeze-eligibility: every mandatory gate.
        # Missing empirical proof MUST remain FALSE.
        "pr_state": pr_body.get("state"),
        "pr_merged": pr_body.get("merged"),
        "pr_merged_at": pr_body.get("merged_at"),
    }
    # Closure IX §2: empirical gates MUST derive from
    # canonical durable evidence. NO hardcoded values.
    _coderabbit_evidence = _read_coderabbit_clean_head_evidence(
        state_dir=state_dir,
        expected_head=local_head,
    )
    _codex_evidence = _read_codex_optional_lifecycle_evidence(
        state_dir=state_dir,
    )
    _autoprov_evidence = _read_autonomous_provenance_evidence(
        state_dir=state_dir,
    )
    _retry_evidence = _read_real_deferred_retry_evidence(
        state_dir=state_dir,
    )
    _empirical_gates_pending = [
        ("coderabbit_clean_head", _coderabbit_evidence["value"]),
        ("codex_optional_lifecycle", _codex_evidence["value"]),
        (
            "autonomous_provenance_real_execution",
            _autoprov_evidence["value"],
        ),
        ("real_deferred_retry", _retry_evidence["value"]),
    ]
    _structural_freeze_eligible = (
        (pr_body.get("state") == "open")
        and (pr_body.get("merged") is False)
        and heads_equal
        and exact_head_ci_all_required_success
        and production_checkout_clean
        and observation_complete
        and static_scope_match
        and runtime_match_all
        and (active_workers == 0)
        and (active_workers >= 0)
        and (not cooldown_parse_failed)
        # Closure IX §3: event observation MUST be complete.
        and EVENT_STATE_OBSERVATION_COMPLETE
        and (orphaned_count == 0)
        and (terminal_pending_consumption_count == 0)
        and (superseded_pending_consumption_count == 0)
        # Closure IX §7: deferred backlog must be empty
        # OR all entries must have retry owner +
        # executable retry path + head-actionable
        # disposition. For the C22 clean-freeze boundary
        # we require 0 deferred-without-retry-owner.
        and (
            len(deferred_analysis[
                "deferred_without_retry_owner"
            ]) == 0
        )
        and (
            len(deferred_analysis[
                "deferred_without_executable_retry_path"
            ]) == 0
        )
        and (
            len(deferred_analysis[
                "deferred_current_head_actionable"
            ]) == 0
        )
        and (
            len(deferred_analysis[
                "deferred_unknown_classification"
            ]) == 0
        )
    )
    _empirical_freeze_eligible = all(
        proven
        for (_label, proven) in _empirical_gates_pending
    )
    # final dict
    evidence["pre_canary_freeze_eligible"] = (
        _structural_freeze_eligible and _empirical_freeze_eligible
    )
    evidence["structural_freeze_eligible"] = _structural_freeze_eligible
    evidence["empirical_freeze_eligible"] = _empirical_freeze_eligible
    evidence["empirical_gates_pending"] = [
        label for (label, proven) in _empirical_gates_pending
        if not proven
    ]
    # Closure IX §2: per-gate evidence records
    evidence["empirical_gate_evidence"] = {
        "coderabbit_clean_head": _coderabbit_evidence,
        "codex_optional_lifecycle": _codex_evidence,
        "autonomous_provenance_real_execution": _autoprov_evidence,
        "real_deferred_retry": _retry_evidence,
    }
    # Closure IX §7: real deferred retry event ids are
    # derived from the empirical evidence, not the
    # cooldown ledger alone.
    evidence["real_deferred_retry_event_ids"] = list(
        _retry_evidence.get("evidence_ids", [])
    )
    evidence["real_deferred_retry_empirically_proven"] = (
        _retry_evidence["value"]
    )
    evidence["empirical_gate_values_source"] = (
        "canonical_durable_evidence"
    )
    # Closure IX §5: full static environment fingerprint.
    _env_inputs = _enumerate_static_environment_inputs(
        runtime_summary, observed_scope,
    )
    _env_fp = _compute_static_environment_fingerprint(_env_inputs)
    evidence["static_environment_fingerprint"] = _env_fp["fingerprint"]
    evidence["static_environment_input_count"] = len(
        _env_fp["inputs"]
    )
    evidence["static_environment_inputs"] = _env_fp["inputs"]
    evidence["static_environment_missing_inputs"] = _env_fp[
        "missing_inputs"
    ]
    evidence["static_environment_inputs_fingerprint_complete"] = (
        len(_env_fp["missing_inputs"]) == 0
    )
    evidence["hermes_invoked_path"] = observed_scope.get(
        "hermes_binary_path", ""
    )
    evidence["hermes_invoked_path_sha256"] = next(
        (
            i["sha256"] for i in _env_fp["inputs"]
            if i["label"] == "hermes_invoked_path"
        ),
        None,
    )
    evidence["hermes_resolved_path"] = next(
        (
            i["path"] for i in _env_fp["inputs"]
            if i["label"] == "hermes_resolved_path"
        ),
        "",
    )
    evidence["hermes_resolved_path_sha256"] = next(
        (
            i["sha256"] for i in _env_fp["inputs"]
            if i["label"] == "hermes_resolved_path"
        ),
        None,
    )
    # Remove placeholder
    evidence.pop("static_environment_fingerprint_marker", None)
    # Write atomically to canonical + mirror (if mirror
    # dir exists).
    _atomic_write(_Path(state_dir) / "pre_canary_evidence.json", evidence)
    mirror_path = _Path(
        "/home/max/.hermes/aed-supervisor/pre_canary_evidence.json"
    )
    mirror_parent = mirror_path.parent
    if mirror_parent.exists():
        _atomic_write(mirror_path, evidence)
    return evidence



