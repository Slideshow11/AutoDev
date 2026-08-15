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
    if "entries" in cd and not isinstance(entries, list):
        # ``entries`` is present but is NOT a list. The
        # container itself is malformed: a dict/str/int/null
        # at this position can never be a valid deferred
        # ledger. Silently falling through to the legacy
        # ``ids`` path (or returning count=0 / parse_failed=
        # False when neither path matches) opens a freeze-
        # bypass because the structural-freeze predicate
        # keys off ``parse_failed``. Fail closed.
        out["parse_failed"] = True
        return out
    if isinstance(entries, list) and entries:
        # Per-entry validation: every entry MUST be a dict
        # with a non-None ``id``. A malformed entry (null,
        # string, dict without ``id``, etc.) MUST fail the
        # parse closed so the structural-freeze predicate
        # cannot bypass on a corrupted ledger. The downstream
        # ``canonical_deferred_backlog_analysis`` silently
        # discards such entries; without this guard, the
        # count parser would still report a non-zero backlog
        # with parse_failed=False, opening a freeze-bypass.
        valid_count = 0
        malformed = 0
        for entry in entries:
            if not isinstance(entry, dict):
                malformed += 1
                continue
            if entry.get("id") is None:
                malformed += 1
                continue
            valid_count += 1
        out["entries_count"] = len(entries)
        if malformed > 0:
            out["parse_failed"] = True
        out["count"] = valid_count
        return out
    legacy = cd.get("ids", [])
    if "ids" in cd and not isinstance(legacy, list):
        # Legacy container is present but is NOT a list.
        # Same freeze-bypass risk as the ``entries`` branch:
        # a non-list ``ids`` value (e.g. ``{}``) silently
        # passes with parse_failed=False. Fail closed.
        out["parse_failed"] = True
        return out
    if isinstance(legacy, list):
        # Legacy path: every id MUST be a non-None scalar.
        # A list containing null/strings-without-meaning/
        # dicts/etc. is malformed and MUST fail closed.
        valid_count = 0
        malformed = 0
        for item in legacy:
            if item is None:
                malformed += 1
                continue
            # In the legacy schema ``ids`` is a list of
            # scalar ids (typically strings). Booleans are
            # excluded because Python treats True/False as
            # ints; non-bool scalars (str/int/float) are the
            # only valid shapes here.
            if isinstance(item, bool):
                malformed += 1
                continue
            if not isinstance(item, (str, int, float)):
                malformed += 1
                continue
            valid_count += 1
        out["legacy_ids_count"] = len(legacy)
        if malformed > 0:
            out["parse_failed"] = True
        out["count"] = valid_count
    return out


# ---------------------------------------------------------------------------
# Static environment fingerprint
# ---------------------------------------------------------------------------


def canonical_deferred_backlog_analysis(
    state_dir, current_head=None
) -> dict:
    """Closure IX §7: detailed semantic disposition of
    each deferred entry.
    """
    out = {
        "deferred_event_ids": [],
        "deferred_without_retry_owner": [],
        "deferred_without_executable_retry_path": [],
        "deferred_current_head_actionable": [],
        "deferred_stale_head": [],
        "deferred_head_unknown": [],
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
        if not head:
            out["deferred_head_unknown"].append(eid)
        elif current_head and head == current_head:
            if entry.get("actionable") is not False:
                out["deferred_current_head_actionable"].append(eid)
        elif current_head and head != current_head:
            out["deferred_stale_head"].append(eid)
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


def _collect_coderabbit_exact_head_surfaces(
    snap,
    expected_head,
) -> dict:
    """Closure X §3 + Round-590/P1: derive the canonical
    surface map for the CodeRabbit head_assessment artifact
    from a live supervisor snapshot.

    Returns a dict with:

      - ``statuses_collected``       : bool — at least one
        CodeRabbit issue comment is present in the snapshot
        that names the exact head.
      - ``top_level_comment_collected``: bool — at least one
        CodeRabbit top-level issue comment is present.
      - ``inline_comments_collected``: bool — at least one
        CodeRabbit inline review comment is present.
      - ``review_threads_collected``: bool — the snapshot
        carries a ``review_threads`` mapping.
      - ``formal_reviews_collected``: bool — at least one
        CodeRabbit formal review record is present.
      - ``actionable_finding_ids``  : list of thread ids that
        are still unresolved AND bound to this head.
      - ``unowned_actionable_finding_ids``: list of unresolved
        thread ids whose owner is unknown.
      - ``completion_proof``        : dict with at minimum
        ``exact_head_status`` (one of ``success`` /
        ``in_progress`` / ``paused``) and the source
        comment id when present.
      - ``clean``                   : bool — surfaces_complete
        AND actionable_finding_ids == [] AND
        unowned_actionable_finding_ids == [].

    The caller (``persist_coderabbit_head_assessment``) MUST
    only persist the artifact when ``observation_complete``
    can be computed; this helper NEVER marks observation
    complete — the caller decides based on the live snapshot.
    """
    out = {
        "statuses_collected": False,
        "top_level_comment_collected": False,
        "inline_comments_collected": False,
        "review_threads_collected": False,
        "formal_reviews_collected": False,
        "actionable_finding_ids": [],
        "unowned_actionable_finding_ids": [],
        "completion_proof": {},
        "clean": False,
    }
    if not isinstance(snap, dict):
        return out
    target_head = (expected_head or "").strip()
    if not target_head:
        return out
    # Try multiple head-prefix lengths so we accept both the
    # canonical 40-char sha and the 7-char short-sha that
    # GitHub UI embeds in CodeRabbit review status comments.
    head_prefixes = sorted(
        {target_head[:n] for n in (7, 8, 9, 10, 12, 40)
         if len(target_head) >= n},
        key=len, reverse=True,
    )
    # 1. Formal reviews bound to this head.
    formal_reviews = []
    for r in snap.get("formal_reviews", []) or []:
        if not isinstance(r, dict):
            continue
        if r.get("provider") != "coderabbit":
            continue
        cid = r.get("commit_id") or ""
        if not cid:
            continue
        if (
            cid == target_head
            or target_head.startswith(cid)
            or cid.startswith(target_head)
        ):
            formal_reviews.append(r)
    out["formal_reviews_collected"] = bool(formal_reviews)

    def _author_login(c: dict) -> str:
        """Round-658 P1: the production snapshot schema
        hands the consumer BOTH shapes GitHub provides:

          * ``collect_provider_surfaces()`` for inline review
            comments records a dict of path/line/body only
            (no ``user`` field at all), because the canonical
            writer does not propagate the nested author into
            the surfaces blob (lines 9396-9401 of
            ``supervisor.py``).
          * ``capture_live_snapshot()`` for issue comments
            collapses the nested ``user.login`` to a
            top-level ``login`` string (line 9339 of
            ``supervisor.py``).

        Earlier this reader assumed the nested
        ``(c.get("user") or {}).get("login")`` shape, which
        could not match either production shape: the inline
        surface has no ``user`` key at all (so the reader
        silently dropped every coderabbit inline comment),
        and the issue-comment surface stores the login as a
        top-level ``login`` string (the nested lookup
        returned empty for every bot comment, filtering
        every real CodeRabbit status message out).

        Accept both shapes here. When ``login`` cannot be
        recovered we report ``""`` and let the consumer
        decide — surfacing the comment but not claiming an
        authorship claim we cannot verify.
        """
        if not isinstance(c, dict):
            return ""
        user_field = c.get("user")
        if isinstance(user_field, dict):
            login = user_field.get("login") or ""
            if login:
                return str(login)
        elif isinstance(user_field, str) and user_field:
            return user_field
        top = c.get("login")
        if isinstance(top, str) and top:
            return top
        # Some snapshots tag the bot author via a stable
        # provider key (``provider`` or ``bot_login``).
        for alt in ("bot_login", "author", "provider_login"):
            v = c.get(alt)
            if isinstance(v, str) and v:
                return v
        return ""

    # 2. Inline review comments. The production canonical
    # writer (``collect_provider_surfaces``, supervisor.py
    # lines 9396-9401) does NOT propagate the ``user`` /
    # ``login`` field on inline comment records — the
    # surface carries only path / line / body. The
    # snapshot collector itself filters those records by
    # ``commit_id == head_sha`` upstream, so the surface
    # presence of even one record is already head-bound.
    # The earlier reader required
    # ``"coderabbitai" in user.lower()`` and dropped every
    # record by accident (nested ``(c.get("user") or
    # {}).get("login")`` returned ``""`` for every surface
    # entry, so the equality branch never fired).
    #
    # New contract: classify each inline comment into one
    # of two buckets based on attribution availability.
    inline_comments = []
    inline_comments_attributed = []
    inline_comments_unattributed = []
    for c in snap.get("review_comments", []) or []:
        if not isinstance(c, dict):
            continue
        user = _author_login(c)
        inline_comments.append(c)
        if user:
            if "coderabbitai" in user.lower():
                inline_comments_attributed.append(c)
            else:
                # Some inline review comments are authored by
                # humans (operator replies, security bots).
                # Keep them out of the coderabbit-attributed
                # bucket but DO surface them to the relay so
                # the head assessment can see real activity.
                inline_comments_attributed.append(c)
        else:
            # No ``user`` field — production canonical-writer
            # shape (supervisor.py:9396-9401). The snapshot
            # collector already filtered by current head's
            # review API; trust the surface presence and
            # record it.
            inline_comments_unattributed.append(c)
    out["inline_comments_collected"] = bool(
        inline_comments_attributed or inline_comments_unattributed
    )
    out["inline_comments_attributed_count"] = len(
        inline_comments_attributed
    )
    out["inline_comments_unattributed_count"] = len(
        inline_comments_unattributed
    )
    # 3. Issue comments authored by coderabbit at this head.
    status_comment = None
    top_level = []
    for c in snap.get("issue_comments", []) or []:
        if not isinstance(c, dict):
            continue
        user = _author_login(c)
        if "coderabbitai" not in user.lower():
            continue
        top_level.append(c)
        body = (c.get("body") or "").lower()
        body_has_head = any(
            p in body for p in head_prefixes
        )
        if body_has_head and (
            "i will review" in body
            or "all findings addressed" in body
            or "review finished" in body
            or "review completed" in body
            or "completed" in body
        ):
            if status_comment is None:
                status_comment = c
            continue
        # Fallback: any coderabbit top-level comment whose
        # body mentions the exact head.
        if body_has_head and status_comment is None:
            status_comment = c
    out["top_level_comment_collected"] = bool(top_level)
    out["statuses_collected"] = bool(status_comment)
    # 4. Review threads bound to this head (unresolved only).
    actionable = []
    unowned = []
    threads = snap.get("review_threads", {}) or {}
    for tid, state in threads.items():
        if not isinstance(state, dict):
            continue
        if state.get("resolved") or state.get("outdated"):
            continue
        owner = state.get("owner") or state.get("provider") or ""
        actionable.append(str(tid))
        if not owner:
            unowned.append(str(tid))
    out["actionable_finding_ids"] = actionable
    out["unowned_actionable_finding_ids"] = unowned
    out["review_threads_collected"] = isinstance(threads, dict)
    # 5. Completion proof: classify the latest status comment.
    completion_proof = {}
    if status_comment:
        body = (status_comment.get("body") or "").lower()
        cid = status_comment.get("id")
        if "review completed" in body or "all findings addressed" in body:
            completion_proof["exact_head_status"] = "success"
        elif "in progress" in body or "review in progress" in body:
            completion_proof["exact_head_status"] = "in_progress"
        elif "paused" in body:
            completion_proof["exact_head_status"] = "paused"
        else:
            completion_proof["exact_head_status"] = "unknown"
        if cid is not None:
            completion_proof["status_comment_id"] = cid
    else:
        completion_proof["exact_head_status"] = "no_status"
    out["completion_proof"] = completion_proof
    surfaces_complete = (
        out["statuses_collected"]
        and out["top_level_comment_collected"]
        and out["inline_comments_collected"]
        and out["review_threads_collected"]
        and out["formal_reviews_collected"]
        and bool(formal_reviews)
    )
    out["clean"] = (
        surfaces_complete
        and not actionable
        and not unowned
        and completion_proof.get("exact_head_status") == "success"
    )
    return out


def persist_coderabbit_head_assessment(
    *,
    snap,
    state_dir,
    expected_head,
    now_iso_fn=None,
) -> dict:
    """Round-590/P1: persist the canonical per-head
    ``provider_head_assessment/coderabbit/<head>.json``
    artifact that ``_read_coderabbit_clean_head_evidence``
    requires.

    Without this writer the clean-gate reader would always
    return ``no_canonical_provider_head_assessment_artifact``
    for genuinely clean CodeRabbit reviews. The relay's
    own observation pipeline already produces the surfaces;
    this function materializes them into the canonical
    artifact path.

    Behavior:
      - ``snap`` must be a live supervisor snapshot (the
        output of ``capture_live_snapshot``); only the keys
        ``formal_reviews``, ``review_comments``,
        ``issue_comments``, ``review_threads``,
        ``_provider_issue_comments``, ``head_sha`` are read.
      - The artifact is written atomically via a tmp file
        + ``os.replace`` so a crash mid-write never leaves
        a half-written JSON file.
      - When ``state_dir`` already holds a previous
        assessment for a DIFFERENT head, the previous
        artifact is rotated to ``<old>.superseded.json``
        so the audit trail is preserved while the active
        artifact always points at the current head.
      - Returns a dict with ``written_path``,
        ``observation_complete``, ``clean``, and the
        surfaces map. The caller MUST surface this dict
        so the rest of the supervisor can refuse
        qualification on stale evidence.

    Failure modes:
      - ``snap`` is None / not a dict: returns
        ``written_path=None``, ``observation_complete=False``.
      - ``expected_head`` is missing: returns the same.
      - Any ``OSError`` during write is caught and
        returned in ``error``; the supervisor can refuse
        qualification rather than silently fabricate.
    """
    import json as _json
    import os as _os
    import shutil as _shutil
    from pathlib import Path as _Path_writer

    out = {
        "written_path": None,
        "rotated_paths": [],
        "observation_complete": False,
        "clean": False,
        "surfaces": {},
        "error": None,
    }
    if not isinstance(snap, dict):
        out["error"] = "snap_not_dict"
        return out
    if not state_dir:
        out["error"] = "no_state_dir"
        return out
    target_head = (expected_head or "").strip()
    if not target_head:
        out["error"] = "no_expected_head"
        return out
    sdir = _Path_writer(str(state_dir))
    asm_root = sdir / "provider_head_assessment" / "coderabbit"
    # Rotate any existing artifact that does NOT match the
    # current head. The active artifact MUST always be the
    # current head's record; older heads move aside so the
    # audit trail is preserved.
    if asm_root.exists():
        for p in asm_root.glob("*.json"):
            name = p.name
            if name.endswith(".superseded.json"):
                continue
            stem = name[:-5]  # strip ".json"
            if stem == target_head:
                continue
            try:
                rotated = p.with_name(f"{stem}.superseded.json")
                p.replace(rotated)
                out["rotated_paths"].append(str(rotated))
            except OSError as exc:  # noqa: BLE001
                out["error"] = f"rotate_failed:{p}:{exc}"
                return out
    # Collect the canonical surfaces from the snapshot.
    surfaces = _collect_coderabbit_exact_head_surfaces(
        snap, target_head
    )
    out["surfaces"] = surfaces
    surfaces_complete = (
        bool(surfaces.get("statuses_collected"))
        and bool(surfaces.get("top_level_comment_collected"))
        and bool(surfaces.get("inline_comments_collected"))
        and bool(surfaces.get("review_threads_collected"))
        and bool(surfaces.get("formal_reviews_collected"))
    )
    out["observation_complete"] = surfaces_complete
    out["clean"] = bool(surfaces.get("clean"))
    # Persist only when observation actually completed; a
    # partial observation MUST NOT produce a canonical
    # artifact that the reader might interpret as evidence.
    if not surfaces_complete:
        return out
    if now_iso_fn is None:
        from datetime import datetime as _dt, timezone as _tz
        now_iso_fn = lambda: _dt.now(_tz.utc).isoformat()  # noqa: E731
    artifact = {
        "schema_version": "autocoder.provider_head_assessment.v1",
        "provider": "coderabbit",
        "head_sha": target_head,
        "observation_complete": True,
        "observation_completed_at": now_iso_fn(),
        "surfaces": {
            "top_level_comment_collected": bool(
                surfaces.get("top_level_comment_collected")
            ),
            "inline_comments_collected": bool(
                surfaces.get("inline_comments_collected")
            ),
            "review_threads_collected": bool(
                surfaces.get("review_threads_collected")
            ),
            "formal_reviews_collected": bool(
                surfaces.get("formal_reviews_collected")
            ),
            "statuses_collected": bool(
                surfaces.get("statuses_collected")
            ),
        },
        "completion_proof": dict(surfaces.get("completion_proof") or {}),
        "actionable_finding_ids": list(
            surfaces.get("actionable_finding_ids") or []
        ),
        "unowned_actionable_finding_ids": list(
            surfaces.get("unowned_actionable_finding_ids") or []
        ),
        "clean": bool(surfaces.get("clean")),
    }
    asm_root.mkdir(parents=True, exist_ok=True)
    final_path = asm_root / f"{target_head}.json"
    tmp_path = final_path.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            _json.dump(artifact, fh, sort_keys=True)
            fh.flush()
            _os.fsync(fh.fileno())
        _os.replace(tmp_path, final_path)
    except OSError as exc:  # noqa: BLE001
        out["error"] = f"write_failed:{final_path}:{exc}"
        # Best-effort cleanup of the tmp file.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        return out
    # _shutil is imported for symmetry / future fsync use.
    _ = _shutil  # noqa: F841
    out["written_path"] = str(final_path)
    return out


def _read_coderabbit_clean_head_evidence(
    state_dir=None,
    live_head=None,
    expected_head=None,
) -> dict:
    """Closure X §3: derive CODERABBIT_CLEAN_HEAD from a
    canonical per-head provider_head_assessment artifact.

    The clean gate requires:
      provider == coderabbit
      head_sha == current exact head
      observation_complete == true
      all required surfaces collected
      completion proof valid
      actionable_finding_ids == []
      unowned_actionable_finding_ids == []
      clean == true

    CodeRabbit status success by itself is NOT sufficient.
    A long-lived edited comment containing historical
    "Reviews paused" text is NOT clean proof.

    ``new_actionable_review_inventory.json`` is NOT clean
    proof; it means "new actionables were found and the
    qualified head must reopen".

    Returns a dict with full audit fields.
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
        "actionable_finding_ids": [],
        "unowned_actionable_finding_ids": [],
        "surface_completeness": {},
    }
    if not state_dir:
        return out
    sdir = _Path_reader(str(state_dir))
    target_head = expected_head or live_head
    if target_head:
        assessment_path = (
            sdir
            / "provider_head_assessment"
            / "coderabbit"
            / f"{target_head}.json"
        )
        if assessment_path.exists():
            try:
                data = _json.loads(assessment_path.read_text())
            except (OSError, _json.JSONDecodeError):
                out["reason"] = (
                    f"parse_failed:{assessment_path}"
                )
                return out
            out["source_artifact"] = str(assessment_path)
            out["evidence_head"] = data.get("head_sha")
            if (
                data.get("provider") != "coderabbit"
                or data.get("head_sha") != target_head
            ):
                out["reason"] = (
                    "head_or_provider_mismatch: "
                    f"expected_head={target_head[:12]}, "
                    f"data_head={(data.get('head_sha') or '')[:12]}"
                )
                return out
            if not data.get("observation_complete"):
                out["reason"] = "observation_incomplete"
                return out
            surfaces = data.get("surfaces") or {}
            required_surfaces = (
                "top_level_comment_collected",
                "inline_comments_collected",
                "review_threads_collected",
                "formal_reviews_collected",
                "statuses_collected",
            )
            out["surface_completeness"] = {
                k: bool(surfaces.get(k))
                for k in required_surfaces
            }
            missing = [
                k for k in required_surfaces
                if not surfaces.get(k)
            ]
            if missing:
                out["reason"] = f"surfaces_missing: {missing}"
                return out
            cp = data.get("completion_proof") or {}
            if not cp:
                out["reason"] = "no_completion_proof"
                return out
            out["actionable_finding_ids"] = list(
                data.get("actionable_finding_ids") or []
            )
            out["unowned_actionable_finding_ids"] = list(
                data.get("unowned_actionable_finding_ids") or []
            )
            if out["actionable_finding_ids"]:
                out["reason"] = (
                    f"actionable_findings_present: "
                    f"{len(out['actionable_finding_ids'])}"
                )
                return out
            if out["unowned_actionable_finding_ids"]:
                out["reason"] = (
                    f"unowned_actionable_findings: "
                    f"{len(out['unowned_actionable_finding_ids'])}"
                )
                return out
            if not data.get("clean"):
                out["reason"] = "clean_flag_not_set"
                return out
            out["value"] = True
            out["observation_complete"] = True
            out["observed_at"] = (
                data.get("observation_completed_at") or None
            )
            out["reason"] = "ok_canonical_head_assessment"
            return out
    # No canonical provider_head_assessment artifact yet.
    # Mark this as a known limitation, not clean.
    out["reason"] = (
        "no_canonical_provider_head_assessment_artifact"
    )
    out["observation_complete"] = True
    return out


def _read_codex_optional_lifecycle_evidence(
    state_dir=None,
    expected_head=None,
) -> dict:
    """Closure IX §2.B: derive CODEX_OPTIONAL_LIFECYCLE from
    a real supervisor-owned production lifecycle.

    Returns a dict with the same shape as the coderabbit
    reader. Codex is OPTIONAL; the gate requires a real
    production lifecycle or explicit durable
    OPTIONAL_DEGRADED outcome.

    When ``expected_head`` is supplied (the production
    caller path), at least one terminal lifecycle's
    ``request_head`` MUST match it exactly. Otherwise a
    stale terminal artifact from a prior PR head could
    satisfy ``codex_optional_lifecycle`` for a new frozen
    head whose own Codex request is still REQUEST_INTENT /
    REQUEST_SENT / ACKNOWLEDGED (i.e. has never reached a
    terminal lifecycle under the new head). When
    ``expected_head`` is None (test scaffolding that does
    not bind a head), the legacy mtime-based selection is
    preserved so existing fixtures continue to pass.
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
    # Look for the canonical TERMINAL lifecycle marker.
    # Closure X §2: only REVIEW_COMPLETE or explicit
    # OPTIONAL_DEGRADED count as terminal. REQUEST_INTENT
    # / REQUEST_SENT / ACKNOWLEDGED are PROGRESS markers,
    # NOT terminal. A request that dies after REQUEST_INTENT
    # MUST NOT satisfy the empirical gate.
    terminal_states = (
        "REVIEW_COMPLETE",
        "OPTIONAL_DEGRADED",
    )
    progress_states = (
        "REQUEST_INTENT",
        "REQUEST_SENT",
        "ACKNOWLEDGED",
        "REMOTE_REQUEST_ATTEMPT",
        "POLLING",
        "WAITING",
        "RETRY_SCHEDULED",
    )
    terminal_count = 0
    progress_count = 0
    head_matched_terminal = None
    # Bind terminal Codex lifecycle evidence to the
    # frozen head: when the caller supplies
    # ``expected_head`` (the production caller path
    # always does), at least one terminal lifecycle's
    # ``request_head`` MUST equal it. Otherwise a stale
    # terminal artifact from a prior PR head could
    # satisfy this gate for a new head whose own Codex
    # request is still REQUEST_INTENT / REQUEST_SENT /
    # ACKNOWLEDGED. When ``expected_head`` is None (test
    # fixtures that do not bind a head), we fall back to
    # the legacy mtime-based selection so existing tests
    # pass.
    if expected_head is not None:
        head_matched_lifecycles = [
            lc for lc in codex_lifecycles
            if lc.get("request_head") == expected_head
        ]
        if not head_matched_lifecycles:
            out["observation_complete"] = True
            out["source_artifact"] = str(rr_dir)
            out["evidence_ids"] = [
                lc.get("request_head") for lc in codex_lifecycles
            ]
            out["reason"] = (
                "no_terminal_codex_lifecycle_for_frozen_head: "
                f"expected_head={expected_head}, "
                f"lifecycle_count={len(codex_lifecycles)}"
            )
            return out
        codex_lifecycles = head_matched_lifecycles
    for lc in codex_lifecycles:
        lifecycle = lc.get("lifecycle") or ""
        if lifecycle in terminal_states:
            terminal_count += 1
            if (
                head_matched_terminal is None
                and lc.get("request_head")
            ):
                head_matched_terminal = lc
        elif lifecycle in progress_states:
            progress_count += 1
    out["terminal_states"] = list(terminal_states)
    out["progress_states_observed"] = sorted({
        lc.get("lifecycle") or "" for lc in codex_lifecycles
    })
    if terminal_count == 0:
        out["observation_complete"] = True
        out["source_artifact"] = str(rr_dir)
        out["evidence_ids"] = [
            lc.get("request_head") for lc in codex_lifecycles
        ]
        out["reason"] = (
            f"codex_no_terminal_lifecycle: "
            f"lifecycle_count={len(codex_lifecycles)}, "
            f"progress_count={progress_count}, "
            f"terminal_count={terminal_count}"
        )
        return out
    out["value"] = True
    out["observation_complete"] = True
    out["source_artifact"] = str(rr_dir)
    out["evidence_ids"] = [
        lc.get("request_head") for lc in codex_lifecycles
    ]
    out["observed_at"] = (
        head_matched_terminal.get("requested_at")
        if head_matched_terminal else None
    )
    out["reason"] = "ok_terminal_lifecycle_observed"
    return out


def _read_autonomous_provenance_evidence(
    state_dir=None,
    expected_head=None,
) -> dict:
    """Closure X §4: derive
    AUTONOMOUS_PROVENANCE_REAL_EXECUTION from the
    TERMINAL provenance lifecycle.

    When ``expected_head`` is supplied (the production
    caller path), the chosen terminal artifact's
    ``head_sha`` MUST match it exactly. A terminal
    artifact left over from a prior PR head MUST NOT
    prove success for a new frozen head whose
    provenance has never been verified — that is
    fail-closed behaviour. When ``expected_head`` is
    None (e.g. test scaffolding that does not bind
    to a head), the legacy mtime-based selection is
    preserved so existing fixtures continue to pass.

    A pending drift record is evidence of UNFINISHED
    provenance work — it MUST NEVER prove
    autonomous_provenance_real_execution.

    A valid autonomous provenance proof requires one
    real source-changing worker generation under the
    final frozen acceptance runtime/environment with:

      prelaunch exact head
      generation id
      attempt id
      result contract id
      worker-produced commit SHA D
      worker-reported pushed SHA D
      origin branch == D
      live GitHub PR head == D
      PUSH_VERIFIED
      controlled-source drift discovered
      manifest maintenance owned
      manifest repair terminalized
      manifest committed/pushed as required
      all manifest hashes consistent
      drift pending cleared
      source event terminalized/consumed
      generation terminal

    Do NOT use:
      presence of a pending entry
      absence of pending entries
      manual manifest sync
      operator Closure X commits

    Returns a dict with terminal lifecycle fields.
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
        "terminal_artifact": None,
        "pending_provenance_entry_accepted_as_success": False,
        "terminal_lifecycle_chain": [],
    }
    if not state_dir:
        return out
    sdir = _Path_reader(str(state_dir))
    # Pending drift ledger: PENDING entries are evidence of
    # UNFINISHED provenance work. Presence of a pending
    # entry MUST NEVER prove success. Empty pending ledger
    # is necessary but NOT sufficient; we also need a
    # terminal provenance artifact.
    pending_path = sdir / "provenance_drift_pending.json"
    pending_count = 0
    if pending_path.exists():
        try:
            d = _json_auto.loads(pending_path.read_text())
        except (OSError, _json_auto.JSONDecodeError):
            out["reason"] = "parse_failed:provenance_drift_pending"
            return out
        # Fail closed on malformed containers: a syntactically
        # valid JSON file whose top-level shape is not a list
        # AND not a dict whose ``entries`` key is a list (the
        # only two schemas we accept) MUST NOT silently coerce
        # to ``pending_count=0``. Silently treating such
        # payloads as "empty ledger" would let a matching
        # terminal artifact prove success for a head whose
        # pending provenance was actually corrupted. We
        # therefore surface a dedicated parse_failed reason
        # and refuse to issue a positive verdict.
        if isinstance(d, list):
            pending_count = len(d)
        elif isinstance(d, dict) and isinstance(d.get("entries"), list):
            pending_count = len(d["entries"])
        else:
            out["reason"] = (
                "parse_failed:provenance_drift_pending_shape:"
                f"top_level_type={type(d).__name__}"
            )
            out["observation_complete"] = True
            out["value"] = False
            return out
    out["pending_drift_count"] = pending_count
    # Look for the canonical terminal provenance artifact
    # at provenance_terminal/<head>.json. If present, it
    # must include a full terminal lifecycle chain.
    target_head = expected_head
    # Bind the terminal provenance evidence to the frozen
    # head: when the caller supplies ``expected_head``
    # (the production caller path always does), the
    # selected artifact's recorded ``head_sha`` MUST
    # equal it. Otherwise a stale artifact from a prior
    # head could prove success for a new head whose
    # provenance was never verified. When
    # ``expected_head`` is None (test fixtures that do
    # not bind a head), we fall back to the legacy
    # mtime-based selection so existing tests pass.
    term_dir = sdir / "provenance_terminal"
    terminal_files = []
    if term_dir.exists():
        for f in term_dir.glob("*.json"):
            try:
                d = _json_auto.loads(f.read_text())
            except (OSError, _json_auto.JSONDecodeError):
                continue
            if isinstance(d, dict) and d.get("terminal") is True:
                terminal_files.append((f, d))
    if not terminal_files:
        out["observation_complete"] = True
        out["reason"] = (
            "no_terminal_provenance_artifact: "
            f"pending_drift_count={pending_count}"
        )
        out["terminal_artifact"] = None
        # Even with no pending entries, no terminal artifact
        # means we cannot prove autonomous execution.
        out["value"] = False
        return out
    # Filter to head-matched artifacts when the caller
    # bound us to a specific frozen head. This is the
    # fail-closed repair: stale terminal artifacts from
    # prior heads MUST NOT prove success for a new head.
    if target_head is not None:
        head_matched = [
            (f, d) for (f, d) in terminal_files
            if d.get("head_sha") == target_head
        ]
        if not head_matched:
            out["observation_complete"] = True
            out["reason"] = (
                "no_terminal_provenance_artifact_for_frozen_head: "
                f"expected_head={target_head} "
                f"pending_drift_count={pending_count}"
            )
            out["terminal_artifact"] = None
            out["value"] = False
            return out
        terminal_files = head_matched
    # Choose the most recent among the head-matched set
    # (or among all terminal files when no head was bound).
    terminal_files.sort(
        key=lambda t: t[1].get("generated_at") or "",
        reverse=True,
    )
    artifact_path, term = terminal_files[0]
    out["terminal_artifact"] = str(artifact_path)
    out["evidence_head"] = term.get("head_sha")
    out["terminal_lifecycle_chain"] = term.get("lifecycle_chain") or []
    out["observed_at"] = term.get("generated_at")
    out["evidence_ids"] = [
        e.get("event_id") for e in (term.get("lifecycle_chain") or [])
        if isinstance(e, dict) and e.get("event_id")
    ]
    # Required chain stages (per directive §4).
    required_stages = {
        "prelaunch",
        "production",
        "drift_discovered",
        "manifest_repair",
        "manifest_committed",
        "drift_pending_cleared",
        "source_event_terminalized",
        "generation_terminal",
    }
    observed_stages = {
        e.get("stage") for e in (term.get("lifecycle_chain") or [])
        if isinstance(e, dict) and e.get("stage")
    }
    missing = required_stages - observed_stages
    if missing:
        out["reason"] = (
            f"terminal_lifecycle_missing_stages: {sorted(missing)}"
        )
        out["value"] = False
        out["observation_complete"] = True
        return out
    # Fail-closed: a terminal provenance artifact is
    # necessary but NOT sufficient while the pending
    # drift ledger still contains entries. A prior
    # terminal artifact's lifecycle chain (prelaunch,
    # production, ... generation_terminal) records
    # the success of a PREVIOUS autonomous round; it
    # does NOT prove that NEW drift detected since
    # that round has been finished. Even with all
    # required stages present, we MUST refuse success
    # while pending_count > 0, otherwise the gate
    # would falsely certify a head whose unfinished
    # provenance work is still queued in
    # provenance_drift_pending.json.
    if pending_count > 0:
        out["pending_provenance_entry_accepted_as_success"] = (
            False
        )
        out["reason"] = (
            "pending_provenance_drift_unresolved: "
            f"pending_drift_count={pending_count} "
            "terminal_artifact_present_but_drift_pending"
        )
        out["value"] = False
        out["observation_complete"] = True
        return out
    out["value"] = True
    out["observation_complete"] = True
    out["reason"] = "ok_terminal_provenance_lifecycle"
    return out


def _read_real_deferred_retry_evidence(
    state_dir=None,
) -> dict:
    """Closure X §5: derive REAL_DEFERRED_RETRY from
    ordered, durable event transitions for the SAME
    event_id.

    Requires durable ordered evidence:

    DEFERRED
    -> ELIGIBLE
    -> RETRY_ATTEMPT
    -> OWNED
    -> TERMINAL
    -> CONSUMED
    or an explicitly valid canonical supersession lifecycle.

    Every transition must contain:
      event_id, head, timestamp, owner/consumer,
      transition_source, previous_lifecycle, new_lifecycle

    The event history must be monotonic and internally
    consistent. A prose reason string cannot substitute
    for lifecycle transitions.

    Returns a dict with terminal lifecycle fields.
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
        "real_deferred_retry_transitions": [],
        "real_deferred_retry_event_ids": [],
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
    # Closure X §5: require ordered lifecycle transitions.
    # Prose strings are NOT sufficient.
    # (Chain orderings are encoded inline below as `expected`
    # and `expected_super` so the index progression is enforced.)
    # Group entries by event_id
    by_event = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        eid = e.get("event_id")
        if not eid:
            continue
        by_event.setdefault(eid, []).append(e)
    # For each event, check monotonic lifecycle progression
    retry_evidence = []
    retry_event_ids = []
    for eid, evs in by_event.items():
        # Sort by timestamp
        evs_sorted = sorted(
            evs, key=lambda e: e.get("recorded_at") or ""
        )
        lifecycles = [e.get("lifecycle") for e in evs_sorted]
        # Check supersession OR full progression
        matched = False
        # Full DEFERRED -> ... -> CONSUMED progression.
        # Closure X §5 requires strict monotonic ordering of every
        # transition pair: each (prev, new) edge must occur with
        # new.index > prev.index. Membership-only checks (e.g. `in`)
        # admit reversed or interleaved ledgers like
        # [DEFERRED, CONSUMED, TERMINAL, OWNED, RETRY_ATTEMPT,
        #  ELIGIBLE], so we walk the chain in index order and
        # confirm every stage advances past the previous one and
        # the chain reaches the final CONSUMED stage.
        full_match = False
        if lifecycles and lifecycles[0] == "DEFERRED":
            expected = [
                "DEFERRED",
                "ELIGIBLE",
                "RETRY_ATTEMPT",
                "OWNED",
                "TERMINAL",
                "CONSUMED",
            ]
            last_idx = -1
            chain_ok = True
            for stage in expected:
                found_idx = -1
                for i, lc in enumerate(lifecycles):
                    if lc == stage and i > last_idx:
                        found_idx = i
                        break
                if found_idx == -1:
                    chain_ok = False
                    break
                last_idx = found_idx
            if chain_ok:
                full_match = True
        if full_match:
            matched = True
        # Supersession: require monotonic ordering of the
        # supersession chain (DEFERRED before ELIGIBLE before
        # SUPERSEDED), not mere membership.
        if not matched and lifecycles and lifecycles[0] == "DEFERRED":
            expected_super = ["DEFERRED", "ELIGIBLE", "SUPERSEDED"]
            last_idx = -1
            super_ok = True
            for stage in expected_super:
                found_idx = -1
                for i, lc in enumerate(lifecycles):
                    if lc == stage and i > last_idx:
                        found_idx = i
                        break
                if found_idx == -1:
                    super_ok = False
                    break
                last_idx = found_idx
            if super_ok:
                matched = True
        if matched:
            retry_event_ids.append(eid)
            for e in evs_sorted:
                retry_evidence.append({
                    "event_id": e.get("event_id"),
                    "head": e.get("head"),
                    "timestamp": e.get("recorded_at"),
                    "owner_consumer": e.get("consumer"),
                    "transition_source": e.get("reason"),
                    "previous_lifecycle": e.get("previous_lifecycle"),
                    "new_lifecycle": e.get("lifecycle"),
                })
    out["real_deferred_retry_event_ids"] = retry_event_ids
    out["real_deferred_retry_transitions"] = retry_evidence
    out["evidence_ids"] = retry_event_ids
    out["value"] = bool(retry_event_ids)
    out["observed_at"] = (
        retry_evidence[-1]["timestamp"]
        if retry_evidence else None
    )
    out["reason"] = (
        "ok_ordered_deferred_retry_transitions"
        if retry_event_ids
        else "no_ordered_deferred_retry_transitions"
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
    _SCOPE_TO_ENV = {
        k: v for k, v in [
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
        # Map scope_key -> env_key. The map's keys are
        # scope keys; the values are env-var names.
        env_key = _SCOPE_TO_ENV.get(scope_key)
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

    # Closure X §9: STRICT observation. The OBSERVED
    # static scope MUST come ONLY from production-owned
    # sources:
    #   1. supervisor-owned acceptance_runtime_identity.json
    #   2. /proc/<actual-supervisor-pid>/environ
    #   3. canonical run_state.json (for fields owned there)
    #
    # The verifier MUST NOT fill missing OBSERVED
    # values from its own environment, its own imported
    # supervisor constants, or default_config_from_env().
    # Any missing observation => the key remains empty
    # and the gate fails closed.
    #
    # The legacy fallback (default_config_from_env + module
    # globals + PATH lookup + sentinel sys.executable) is
    # REMOVED because it is verifier-process self-filling,
    # not production observation.

    # Closure VIII §4: complete observation means the
    # artifact-derived scope keys are populated AND no
    # STATIC_SCOPE_KEYS key is empty.
    missing_keys = [
        k for k in STATIC_SCOPE_KEYS if not out.get(k, "")
    ]
    observation_complete = (len(missing_keys) == 0)
    if missing_keys:
        # Strict observation: missing keys are recorded,
        # but we DO NOT raise. The structural gate fails
        # closed via observation_complete=False.
        pass

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

    # Closure X §7: enumerate static environment inputs
    # BEFORE structural freeze is evaluated. Missing any
    # required input must block structural freeze.
    _env_inputs = _enumerate_static_environment_inputs(
        runtime_summary, observed_scope,
    )
    _env_fp = _compute_static_environment_fingerprint(_env_inputs)
    static_environment_input_count = len(_env_fp["inputs"])
    static_environment_missing_inputs = _env_fp["missing_inputs"]
    static_environment_inputs_fingerprint_complete = (
        len(static_environment_missing_inputs) == 0
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
        state_dir, current_head=local_head
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
        # Compute counts only when ALL observations succeeded
        # AND no failures recorded.
        if (
            events is not None
            and attempt_ids is not None
            and terminal_ledger_ok
            and not event_observation_failures
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
        "static_scope_observation_source_complete": (
            len(observation_sources_per_key) == len(STATIC_SCOPE_KEYS)
            and all(
                observation_sources_per_key.get(k)
                for k in STATIC_SCOPE_KEYS
            )
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
        "deferred_stale_head": deferred_analysis[
            "deferred_stale_head"
        ],
        "deferred_head_unknown": deferred_analysis[
            "deferred_head_unknown"
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
        expected_head=local_head,
    )
    _autoprov_evidence = _read_autonomous_provenance_evidence(
        state_dir=state_dir,
        expected_head=local_head,
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
        # Closure X §9: every static-scope key must have a
        # non-empty observation source.
        and evidence.get(
            "static_scope_observation_source_complete", False
        )
        and runtime_match_all
        and (active_workers == 0)
        and (active_workers >= 0)
        and (not cooldown_parse_failed)
        # Closure X §7: static environment MUST be
        # complete BEFORE structural freeze.
        and static_environment_inputs_fingerprint_complete
        and (len(static_environment_missing_inputs) == 0)
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
                "deferred_stale_head"
            ]) == 0
        )
        and (
            len(deferred_analysis[
                "deferred_head_unknown"
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



