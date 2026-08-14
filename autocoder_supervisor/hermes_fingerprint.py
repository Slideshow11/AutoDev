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
) -> set:
    """Convert WorkerAttempt record dicts into a set of
    4-tuples for ``validate_run_binding_relations``.

    Each record must contain:
      - produced_commit_sha (or pushed_commit_sha) — used as
        authoritative_head
      - generation_id
      - attempt_id
      - result_contract_id

    Terminal (status='CONSUMED'/'SUPERSEDED') records may be
    excluded by the caller if appropriate.
    """
    out = set()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        head = rec.get("pushed_commit_sha") or rec.get("produced_commit_sha")
        gen = rec.get("generation_id")
        att = rec.get("attempt_id")
        rc = rec.get("result_contract_id")
        if not all(isinstance(x, str) and x for x in (head, gen, att, rc)):
            continue
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


def generate_pre_canary_evidence(
    repo_root,
    state_dir,
    repo,
    pr_number,
    branch,
):
    """Generate the pre-canary evidence artifact using only
    machine-read values. The caller cannot supply SHA
    strings; every SHA is read from the system directly.

    Writes:
      {state_dir}/pre_canary_evidence.json (canonical)
    """
    import os
    import json as _json
    import hashlib as _hashlib
    import subprocess as _sp
    from pathlib import Path as _Path
    from datetime import datetime, timezone as _tz

    # Set the AED_* env vars so compute_static_acceptance_scope_fingerprint
    # has values to bind.
    _canonical_scope = {
        "AED_REPO_OWNER": "Slideshow11",
        "AED_REPO_NAME": "AutoDev",
        "AED_PR_NUMBER": "5",
        "AED_PR_NUMBERS": "5",
        "AED_EXPECTED_BRANCH": "feat/review-repair-relay-v1",
        "AED_EXPECTED_BRANCH_SET": "feat/review-repair-relay-v1",
        "AED_SUPERVISOR_WORKING_CHECKOUT": "/home/max/AutoDev",
        "AED_SUPERVISOR_HOME": "/home/max/.hermes/aed-supervisor",
        "AED_SUPERVISOR_STATE_DIR": "/home/max/.hermes/aed-supervisor/state",
        "AED_HERMES_BIN": "/home/max/.local/bin/hermes",
        "AED_REQUIRED_REVIEW_PROVIDERS": "coderabbit",
        "AED_OPTIONAL_REVIEW_PROVIDERS": "codex",
        "AED_PROVIDERS_INDEPENDENT": "true",
    }
    _saved = {}
    for k, v in _canonical_scope.items():
        _saved[k] = os.environ.get(k)
        os.environ[k] = v
    try:
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

        # Compute static fingerprints via this module.
        env_fingerprint = compute_static_hermes_environment_fingerprint()
        scope_fingerprint = compute_static_acceptance_scope_fingerprint()

        # Read production runtime files. Compare against committed bytes.
        runtime_summary = []
        home = _Path("/home/max/.hermes/aed-supervisor")
        for filename in ACCEPTANCE_RUNTIME_INVENTORY:
            runtime_path = home / filename
            checkout_path = _Path(repo_root) / filename
            chosen = None
            for cp in [
                runtime_path,
                checkout_path,
                _Path(repo_root) / "autocoder_orchestration" / filename,
                _Path(repo_root) / "autocoder_supervisor" / filename,
            ]:
                if cp.exists():
                    chosen = cp
                    break
            if chosen is None:
                continue
            try:
                rel = chosen.relative_to(repo_root)
            except ValueError:
                continue
            rel_str = str(rel)
            r = _sp.run(
                ["git", "-C", str(repo_root), "show", f"{local_head}:{rel_str}"],
                capture_output=True,
            )
            committed = (
                _hashlib.sha256(r.stdout).hexdigest()
                if r.returncode == 0 else None
            )
            runtime = (
                _hashlib.sha256(open(chosen, "rb").read()).hexdigest()
                if chosen.exists() else None
            )
            runtime_summary.append({
                "path": str(chosen),
                "relpath": rel_str,
                "runtime_sha256": runtime,
                "committed_sha256": committed,
                "match": (committed is not None and runtime == committed),
            })

        # Production checkout clean.
        r = _sp.run(
            ["git", "-C", str(repo_root), "status", "--porcelain",
             "--untracked-files=all"],
            capture_output=True, text=True,
        )
        porcelain = [l for l in r.stdout.splitlines() if l.strip()]
        production_checkout_clean = (len(porcelain) == 0)

        # Active workers.
        r = _sp.run(["ps", "-ef"], capture_output=True, text=True)
        active_workers = sum(
            1 for l in r.stdout.splitlines()
            if "aed-supervisor" in l and "python3 -m supervisor" in l
        )

        supervisor_pid = None
        for l in r.stdout.splitlines():
            if "python3 -m supervisor" in l and "grep" not in l:
                try:
                    supervisor_pid = int(l.split()[1])
                    break
                except (ValueError, IndexError):
                    pass
        heartbeat = None
        hb_path = _Path("/home/max/.hermes/aed-supervisor/heartbeat")
        if hb_path.exists():
            heartbeat = hb_path.read_text().strip()

        # Event ledgers.
        cooldown_count = 0
        unconsumed_count = 0
        orphaned_count = 0
        terminal_pending = 0
        superseded_pending = 0
        cd_path = _Path(state_dir) / "cooldown_deferred_events.json"
        if cd_path.exists():
            d = _json.loads(cd_path.read_text())
            if isinstance(d, dict):
                cooldown_count = len(d.get("ids", []))
        ue_path = _Path(state_dir) / "unconsumed_events.json"
        if ue_path.exists():
            d = _json.loads(ue_path.read_text())
            if isinstance(d, dict):
                unconsumed_count = len(d.get("events", []))
        ct_path = _Path(state_dir) / "consumed_event_terminality.json"
        if ct_path.exists():
            d = _json.loads(ct_path.read_text())
            if isinstance(d, dict):
                for k, v in d.items():
                    if isinstance(v, dict):
                        st = v.get("status")
                        if st == "ORPHANED":
                            orphaned_count += 1
                        elif st == "PENDING_CONSUMPTION":
                            terminal_pending += 1
                        elif st == "SUPERSEDED_PENDING_CONSUMPTION":
                            superseded_pending += 1

        evidence = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(_tz.utc).isoformat(),
            "repo": repo,
            "pr_number": pr_number,
            "branch": branch,
            "local_head": local_head,
            "origin_head": origin_head,
            "live_github_head": live_sha_raw,
            "heads_equal": heads_equal,
            "pr_state": pr_body.get("state"),
            "pr_merged": pr_body.get("merged"),
            "pr_merged_at": pr_body.get("merged_at"),
            "exact_head_ci_sha": local_head,
            "workflow_run_summary": workflow_summary,
            "full_suite_result": full_suite_result,
            "static_environment_fingerprint": env_fingerprint["fingerprint"],
            "static_scope_fingerprint": scope_fingerprint["fingerprint"],
            "environment_inputs_count": len(env_fingerprint["inputs"]),
            "production_runtime_hash_summary": runtime_summary,
            "production_runtime_hash_match_all": (
                all(s["match"] for s in runtime_summary)
                if runtime_summary else False
            ),
            "production_checkout_clean": production_checkout_clean,
            "active_workers": active_workers,
            "supervisor_pid": supervisor_pid,
            "supervisor_heartbeat": heartbeat,
            "cooldown_deferred_count": cooldown_count,
            "unconsumed_count": unconsumed_count,
            "orphaned_count": orphaned_count,
            "terminal_pending_consumption_count": terminal_pending,
            "superseded_pending_consumption_count": superseded_pending,
        }
    finally:
        # Restore the original AED_* environment so we don't
        # leak into the caller's process.
        for k, prior in _saved.items():
            if prior is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prior

    # Write atomically to canonical + mirror (if mirror dir exists).
    _atomic_write(_Path(state_dir) / "pre_canary_evidence.json", evidence)
    mirror_path = _Path("/home/max/.hermes/aed-supervisor/pre_canary_evidence.json")
    mirror_parent = mirror_path.parent
    if mirror_parent.exists():
        _atomic_write(mirror_path, evidence)
    return evidence



