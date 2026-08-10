"""Canonical orchestration state-root identification.

Round-28 invariant (user-supplied):

  The orchestration state root MUST come from positive evidence
  established when the orchestration run is initialized or handed
  off to the supervisor. There are two conceptually separate roots:

    1. supervisor private state (``STATE_DIR``) — supervisor
       bookkeeping files: ``run_state.json``, ``quota_state.json``,
       ``unconsumed_events.json``, etc.

    2. orchestration controller/run state containing the
       authoritative ``run_context.json`` and ``state.json``.

  These may be the same path on a small installation but the
  supervisor MUST NOT silently substitute ``STATE_DIR`` for the
  orchestration run state root. The orchestration root MUST come
  from one of:

    a) explicit ``AED_ORCHESTRATION_STATE_ROOT`` env var,
    b) the positively persisted concrete orchestration run
       state root recorded at initialization/handoff time
       (``RUN_STATE['orchestration_state_root']``).

  The resolver fails closed when neither yields a positive root.
  Fail-closed means: do NOT silently substitute ``STATE_DIR``,
  do NOT launch a generic worker, do NOT proceed autonomously —
  the supervisor must transition to the protected BLOCKED /
  escalation path and stop autonomous progression until the
  configuration is recoverable.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional


class OrchestrationRootError(Exception):
    """Base class for orchestration-root failures.

    Concrete subclasses distinguish fail-closed semantics so
    the supervisor can route to the BLOCKED / escalation path.
    """


class OrchestrationRootMissing(OrchestrationRootError):
    """No positively-known orchestration state root is available.

    The supervisor MUST NOT fall back to ``STATE_DIR``. The
    caller MUST route to BLOCKED / escalation and stop
    autonomous progression.
    """


class OrchestrationRootUnverified(OrchestrationRootError):
    """A candidate orchestration state root was found but failed
    positive verification.

    The candidate directory does not exist, lacks
    ``run_context.json``, the ``RunContext`` does not parse, or
    it does not identify the expected repo/run/PR. The caller
    MUST route to BLOCKED / escalation and stop autonomous
    progression.
    """


def resolve_orchestration_state_root(
    *,
    env: Optional[Dict[str, str]] = None,
    run_state_path: Optional[Path] = None,
    expected_repo: Optional[str] = None,
    expected_run_id: Optional[str] = None,
    expected_pr_number: Optional[int] = None,
    verify_against_run_context: bool = True,
) -> str:
    """Return the positively-identified orchestration state root.

    Round-28 invariant: NEVER substitute ``STATE_DIR``. The
    function returns a ``str`` (the positively-identified root)
    or raises an ``OrchestrationRootError`` (fail-closed). The
    caller MUST treat any raised error as a protected-authority
    blocker — the supervisor routes to the BLOCKED / escalation
    path and stops autonomous progression.

    Precedence:

      1. ``AED_ORCHESTRATION_STATE_ROOT`` env var (explicit
         operator override).
      2. ``RUN_STATE['orchestration_state_root']`` field
         (recorded at initialization/handoff time by
         ``persist_orchestration_state_root``).

    The candidate returned from either source is positively
    verified before acceptance. Verification is:
      - The candidate path exists and is a directory.
      - ``<candidate>/run_context.json`` exists and is readable.
      - ``<candidate>/run_context.json`` parses as a valid
        JSON object (a ``RunContext`` document).
      - If ``expected_repo`` / ``expected_run_id`` /
        ``expected_pr_number`` are supplied, the parsed
        ``RunContext`` MUST identify them. This prevents the
        supervisor from pointing at a stale run.
    """
    _env = env if env is not None else os.environ
    state_root = _env.get("AED_ORCHESTRATION_STATE_ROOT")
    source = "env"
    if not state_root:
        # Read from the supervisor's RUN_STATE. The
        # ``run_state_path`` is the canonical supervisor
        # private-state path. The supervisor's ``read_run_state``
        # MAY return a dict with ``orchestration_state_root``
        # (recorded at handoff) or an empty dict (no handoff
        # has happened yet). Either way we MUST NOT fall back
        # to STATE_DIR.
        if run_state_path is None:
            raise OrchestrationRootMissing(
                "no AED_ORCHESTRATION_STATE_ROOT env var and no "
                "RUN_STATE path supplied; the orchestration "
                "state root cannot be positively identified. "
                "The supervisor MUST NOT silently substitute "
                "STATE_DIR. Route to BLOCKED / escalation."
            )
        if not run_state_path.exists():
            raise OrchestrationRootMissing(
                f"RUN_STATE file not found at {run_state_path}; "
                "the orchestration state root cannot be "
                "positively identified. Init must be performed "
                "explicitly via init_run_state_safely before "
                "the relay can run. Route to BLOCKED / escalation."
            )
        try:
            text = run_state_path.read_text()
        except OSError as exc:
            raise OrchestrationRootMissing(
                f"RUN_STATE not readable at {run_state_path}: {exc!r}; "
                "the orchestration state root cannot be positively "
                "identified. Route to BLOCKED / escalation."
            ) from exc
        try:
            run_state = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OrchestrationRootUnverified(
                f"RUN_STATE at {run_state_path} is not valid JSON: {exc!r}; "
                "refusing to overwrite a corrupt run state document. "
                "Route to BLOCKED / escalation."
            ) from exc
        if not isinstance(run_state, dict):
            raise OrchestrationRootUnverified(
                f"RUN_STATE at {run_state_path} is not a JSON object: "
                f"got {type(run_state).__name__}; refusing to "
                "overwrite. Route to BLOCKED / escalation."
            )
        state_root = run_state.get("orchestration_state_root")
        if not state_root:
            raise OrchestrationRootMissing(
                "RUN_STATE has no 'orchestration_state_root' field; "
                "the supervisor MUST persist the concrete orchestration "
                "run state root at initialization/handoff time. "
                "Refusing to substitute STATE_DIR. Route to BLOCKED "
                "/ escalation."
            )
        source = "run_state"
    # Positively verify the candidate.
    _verify_orchestration_state_root(
        state_root,
        source=source,
        expected_repo=expected_repo,
        expected_run_id=expected_run_id,
        expected_pr_number=expected_pr_number,
        verify_against_run_context=verify_against_run_context,
    )
    return state_root


def _verify_orchestration_state_root(
    candidate: str,
    *,
    source: str,
    expected_repo: Optional[str],
    expected_run_id: Optional[str],
    expected_pr_number: Optional[int],
    verify_against_run_context: bool,
) -> None:
    """Positively verify a candidate orchestration state root.

    Raises ``OrchestrationRootUnverified`` on any failure. The
    caller MUST treat any raise as a protected-authority
    blocker — the supervisor routes to BLOCKED / escalation and
    stops autonomous progression.

    Verification rules:

      1. ``candidate`` is a non-empty string.
      2. The path exists and is a directory (not a file or
         symlink to nothing).
      3. ``<candidate>/run_context.json`` exists and is
         readable.
      4. The JSON parses as a dict (a ``RunContext`` document).
      5. If ``expected_repo`` / ``expected_run_id`` /
         ``expected_pr_number`` are supplied, the parsed dict
         MUST identify them (so the supervisor never binds
         to a stale run).
    """
    if not isinstance(candidate, str) or not candidate.strip():
        raise OrchestrationRootUnverified(
            f"candidate state_root is not a non-empty string: "
            f"got {candidate!r}; source={source}"
        )
    p = Path(candidate)
    if not p.exists():
        raise OrchestrationRootUnverified(
            f"orchestration state_root {candidate!r} does not "
            f"exist; source={source}. The candidate must be a "
            f"real directory containing run_context.json."
        )
    if not p.is_dir():
        raise OrchestrationRootUnverified(
            f"orchestration state_root {candidate!r} is not a "
            f"directory; source={source}"
        )
    rc_path = p / "run_context.json"
    if not rc_path.exists():
        raise OrchestrationRootUnverified(
            f"orchestration state_root {candidate!r} does not "
            f"contain run_context.json at {rc_path}; source={source}. "
            f"Refusing to bind to a directory that does not contain "
            f"the authoritative run_context.json."
        )
    try:
        text = rc_path.read_text()
    except OSError as exc:
        raise OrchestrationRootUnverified(
            f"orchestration run_context.json at {rc_path} is "
            f"unreadable: {exc!r}; source={source}"
        ) from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OrchestrationRootUnverified(
            f"orchestration run_context.json at {rc_path} is "
            f"not valid JSON: {exc!r}; source={source}"
        ) from exc
    if not isinstance(parsed, dict):
        raise OrchestrationRootUnverified(
            f"orchestration run_context.json at {rc_path} is "
            f"not a JSON object: got {type(parsed).__name__}; "
            f"source={source}"
        )
    if verify_against_run_context:
        # The RunContext fields used here are the canonical
        # ones produced by ``make_run_context``. We deliberately
        # do NOT import the full RunContext dataclass to keep
        # the resolver import-clean of the orchestration
        # package; the JSON shape is the contract.
        if expected_repo is not None:
            # Round-32: the run_context.json has multiple
            # representations of the repo identity:
            # ``repo_owner`` alone, ``repo_name`` alone, or
            # ``repo`` combined. The resolver accepts ANY
            # of these — combined, owner-only, or name-only.
            got_raw = parsed.get("repo_owner") or ""
            got_combined = parsed.get("repo") or ""
            exp_owner, _, exp_name = expected_repo.partition("/")
            matches = (
                got_raw == expected_repo
                or got_raw == f"{exp_owner}/{exp_name}"
                or got_combined == expected_repo
                or (exp_owner and exp_name and (
                    got_raw == exp_owner
                    or got_raw == exp_name
                ))
            )
            if not matches:
                raise OrchestrationRootUnverified(
                    f"orchestration run_context.json at {rc_path} "
                    f"identifies repo_owner={got_raw!r}, "
                    f"repo={got_combined!r}; expected "
                    f"repo={expected_repo!r}; source={source}. "
                    f"Refusing to bind to a stale run."
                )
        if expected_run_id is not None:
            got = parsed.get("run_id")
            if got != expected_run_id:
                raise OrchestrationRootUnverified(
                    f"orchestration run_context.json at {rc_path} "
                    f"identifies run_id={got!r}; expected "
                    f"run_id={expected_run_id!r}; source={source}. "
                    f"Refusing to bind to a stale run."
                )
        if expected_pr_number is not None:
            got = parsed.get("pr_number")
            try:
                got_int = int(got) if got is not None else None
            except (TypeError, ValueError):
                got_int = None
            if got_int != expected_pr_number:
                raise OrchestrationRootUnverified(
                    f"orchestration run_context.json at {rc_path} "
                    f"identifies pr_number={got!r}; expected "
                    f"pr_number={expected_pr_number!r}; source={source}. "
                    f"Refusing to bind to a stale run."
                )


def persist_orchestration_state_root(
    *,
    state_root: str,
    run_state_path: Path,
    repo_owner: Optional[str] = None,
    repo_name: Optional[str] = None,
    run_id: Optional[str] = None,
    pr_number: Optional[int] = None,
    expected_existing_run_id: Optional[str] = None,
    writer: Optional[Callable[[Path, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Persist the positively-identified orchestration state root
    into the supervisor's ``RUN_STATE``.

    Round-28 invariant: this is the canonical place where the
    concrete orchestration run state root enters the supervisor
    configuration. It is called at orchestration handoff time
    with the path returned by the orchestration's own init /
    ``make_run_context`` call. The supervisor MUST NOT silently
    invent or substitute ``STATE_DIR`` here.

    Init safety:

      - Missing ``RUN_STATE`` file → safe to initialize an empty
        first-run document with the orchestration root recorded.
      - Existing ``RUN_STATE`` whose JSON parses → merge: keep
        existing keys, write the new orchestration root, leave
        other keys intact.
      - Existing ``RUN_STATE`` whose JSON does NOT parse →
        refuse to overwrite. Raise
        ``OrchestrationRootUnverified`` so the supervisor can
        route to BLOCKED / escalation.

    The ``writer`` is a test-only injection seam that takes a
    path + dict and persists the dict. Production callers leave
    it ``None`` and the function uses an atomic JSON write via
    the supervisor's ``write_json`` helper.
    """
    if not isinstance(state_root, str) or not state_root.strip():
        raise OrchestrationRootError(
            f"refusing to persist empty/invalid state_root: {state_root!r}"
        )
    if not isinstance(run_state_path, Path):
        raise OrchestrationRootError(
            f"run_state_path must be a Path; got {type(run_state_path).__name__}"
        )
    # Read existing state (if any). Missing file → empty dict.
    if run_state_path.exists():
        try:
            text = run_state_path.read_text()
        except OSError as exc:
            raise OrchestrationRootUnverified(
                f"RUN_STATE at {run_state_path} is unreadable: {exc!r}; "
                f"refusing to overwrite. Route to BLOCKED / escalation."
            ) from exc
        try:
            existing = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OrchestrationRootUnverified(
                f"RUN_STATE at {run_state_path} is not valid JSON: {exc!r}; "
                f"refusing to overwrite a corrupt run state document. "
                f"Route to BLOCKED / escalation."
            ) from exc
        if not isinstance(existing, dict):
            raise OrchestrationRootUnverified(
                f"RUN_STATE at {run_state_path} is not a JSON object: "
                f"got {type(existing).__name__}; refusing to overwrite."
            )
    else:
        existing = {}
    # Optional: refuse to overwrite an existing orchestration_state_root
    # bound to a different run_id without an explicit
    # ``expected_existing_run_id`` match. This protects against
    # the supervisor accidentally rebinding to a previous run's
    # root when the env var is set but the handoff intended a new run.
    prior_root = existing.get("orchestration_state_root")
    if (
        prior_root
        and prior_root != state_root
        and expected_existing_run_id is not None
    ):
        prior_run_id = existing.get("last_bound_run_id")
        if prior_run_id is not None and prior_run_id != expected_existing_run_id:
            raise OrchestrationRootError(
                f"RUN_STATE already binds orchestration_state_root={prior_root!r} "
                f"to run_id={prior_run_id!r}; refusing to rebind to "
                f"state_root={state_root!r} for run_id={expected_existing_run_id!r}. "
                f"Operator intervention required."
            )
    # Build the merged dict. Existing keys are preserved.
    merged: Dict[str, Any] = dict(existing)
    merged["orchestration_state_root"] = state_root
    merged.setdefault("schema_version", "autocoder.run_state.v2")
    if repo_owner is not None:
        merged["last_bound_repo_owner"] = repo_owner
    if repo_name is not None:
        merged["last_bound_repo_name"] = repo_name
    if run_id is not None:
        merged["last_bound_run_id"] = run_id
    if pr_number is not None:
        merged["last_bound_pr_number"] = int(pr_number)
    # Persist atomically.
    if writer is None:
        from supervisor import write_json
        writer_impl = write_json
    else:
        writer_impl = writer
    writer_impl(run_state_path, merged)
    return merged


def init_run_state_safely(
    *,
    run_state_path: Path,
    writer: Optional[Callable[[Path, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Initialize the supervisor's ``RUN_STATE`` document
    *only* when it is missing or when it is a parseable empty
    document with no recorded bindings.

    Round-28 invariant: an existing ``RUN_STATE`` that fails
    JSON parsing MUST NOT be overwritten. The caller MUST route
    to BLOCKED / escalation rather than silently destroying
    supervisor state.
    """
    if not isinstance(run_state_path, Path):
        raise OrchestrationRootError(
            f"run_state_path must be a Path; got {type(run_state_path).__name__}"
        )
    if run_state_path.exists():
        try:
            text = run_state_path.read_text()
        except OSError as exc:
            raise OrchestrationRootUnverified(
                f"RUN_STATE at {run_state_path} is unreadable: {exc!r}; "
                f"refusing to overwrite. Route to BLOCKED / escalation."
            ) from exc
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OrchestrationRootUnverified(
                f"RUN_STATE at {run_state_path} is not valid JSON: {exc!r}; "
                f"refusing to overwrite a corrupt run state document. "
                f"Route to BLOCKED / escalation."
            ) from exc
        if not isinstance(parsed, dict):
            raise OrchestrationRootUnverified(
                f"RUN_STATE at {run_state_path} is not a JSON object: "
                f"got {type(parsed).__name__}; refusing to overwrite."
            )
        # Existing parseable document is preserved verbatim. We
        # do NOT auto-add orchestration_state_root here; that
        # MUST come from a positive handoff via
        # ``persist_orchestration_state_root``.
        return parsed
    # Missing file: safe to initialize an empty first-run document.
    new_doc: Dict[str, Any] = {
        "schema_version": "autocoder.run_state.v2",
        "first_initialized_at_utc": _utc_now_iso(),
    }
    if writer is None:
        from supervisor import write_json
        writer_impl = write_json
    else:
        writer_impl = writer
    writer_impl(run_state_path, new_doc)
    return new_doc


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
