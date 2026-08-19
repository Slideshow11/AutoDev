"""Top-level CLI for the orchestrator.

The CLI is a thin wrapper around :class:`Controller` and the
helper functions in the orchestration package. It is read-only
except for the ``run``, ``merge``, and ``apply-*`` commands.

All commands print JSON when ``--json`` is passed. Exit codes
follow the convention:

- 0 — success
- 2 — invalid arguments
- 3 — guard failure (e.g. readiness gate failed)
- 4 — state or input error
- 5 — internal error
"""
from __future__ import annotations

import argparse
from pathlib import Path
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, Optional, Sequence, Tuple

from .context import RunContext, make_run_context, generate_run_id
from .canonical_paths import canonical_paths as _canonical_artifact_paths
from .state_machine import (
    StateMachine,
    StateError,
    STATE_PLANNED,
    STATE_IMPLEMENTING,
    STATE_AWAITING_CI,
    STATE_REPAIRING_REVIEW_FINDINGS,
    STATE_QUALIFYING_READINESS,
    STATE_READY_FOR_CANDIDATE,
    STATE_CANDIDATE_FROZEN,
    STATE_AWAITING_INDEPENDENT_VERIFICATION,
    STATE_VERIFYING,
    STATE_VERIFICATION_FAILED,
    STATE_VERIFICATION_REPAIR,
    STATE_AWAITING_MERGE_AUTHORIZATION,
    STATE_MERGE_AUTHORIZED,
    STATE_POST_MERGE_VERIFYING,
    STATE_COMPLETE,
    STATE_BLOCKED,
)
from .store import StateStore, StateStoreError
from .controller import Controller, ControllerError
from .readiness import ReadinessCertificate
from .artifacts import ArtifactError, read_artifact, write_artifact  # noqa: F401
from .readiness import ReadinessEngine, ReadinessDecision, ReadinessCertificate
from .observer import ObservationLog, Observation
from .artifacts import ArtifactError, write_artifact, read_artifact
from .candidate import (
    Candidate,
    CandidateBuilder,
    CandidateError,
)
from .verifier_handoff import (
    VerifierHandoff,
    write_handoff,
)
from .merge_authorization import (
    MergeAuthorization,
    MergeRecord,
    MergeError,
    MergeTransactionInputs,
    MergeAuthorizationMissing,
    MergeAuthorizationMalformed,
    MergeInputsCollide,
    MergeSubprocessFailed,
    MergeAmbiguousOutcome,
    execute_guarded_merge_transaction,
    fetch_live_pr_payload,
)
from .review_repair_relay import (
    DEFAULT_MAX_ROUNDS,
    DirectiveStore,
    EscalateToHuman,
    RelayError,
    RelayLoop,
    build_worker_prompt,
)


EXIT_OK = 0
EXIT_INVARG = 2
EXIT_GUARD = 3
EXIT_STATE = 4
EXIT_INTERNAL = 5

# Round-591: the OPERATOR-POLICY set for PR #5. Migration
# operations MUST target exactly this set; narrowing is
# forbidden so the operator cannot silently shrink the
# required-check policy.
SEVEN_NAME_OPERATOR_POLICY: tuple[str, ...] = (
    "test (3.10)",
    "test (3.11)",
    "test (3.12)",
    "package-smoke",
    "provenance",
    "committed-state-scan",
    "full-suite",
)

# Author login used by CodeRabbit on this repository. The CLI filters
# latestReviews by this identity so a human review cannot satisfy the
# CodeRabbit guard. GitHub App bot logins are conventionally suffixed
# with "[bot]" in some APIs and not in others; the comparison
# normalizes by stripping one terminal "[bot]" suffix from both sides
# before equality, so the gate works for both forms.
CODERABBIT_AUTHOR_LOGIN = "coderabbitai"


def _normalize_coderabbit_login(login: object) -> str:
    """Strip one terminal ``[bot]`` suffix and lower-case the result.

    The GitHub GraphQL ``latestReviews.author.login`` field can return
    either ``coderabbitai`` (for bot accounts) or ``coderabbitai[bot]``
    (for GitHub Apps) depending on the installation. Stripping the
    suffix lets the same constant match both forms while a plain
    substring or ``startswith`` comparison would over-match
    similarly-named accounts.
    """
    if not isinstance(login, str):
        return ""
    s = login.lower().strip()
    if s.endswith("[bot]"):
        s = s[:-5].rstrip()
    return s


def _filter_coderabbit_review_state(reviews_data: dict) -> Optional[str]:
    """Extract the latest CodeRabbit review state from a GraphQL payload.

    Returns the latest review whose author matches
    :data:`CODERABBIT_AUTHOR_LOGIN` (with ``[bot]`` suffix tolerated),
    or ``None`` if no matching review exists. The state is left
    unavailable when the payload is empty or no CodeRabbit review is
    found, so the guarded transaction fails closed (C-25).

    GitHub returns ``"author": null`` for reviews whose reviewer
    account has been deleted. The filter MUST treat a null author
    as "no matching identity" without raising -- it returns
    ``None`` for that node, which propagates as "no matching
    CodeRabbit review" through the rest of the CLI.
    """
    target = _normalize_coderabbit_login(CODERABBIT_AUTHOR_LOGIN)
    nodes = (
        reviews_data.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("latestReviews", {})
        .get("nodes", [])
    )
    for node in nodes:
        # Normalize a null author to an empty mapping before reading
        # ``login``. GitHub returns ``"author": null`` for deleted
        # accounts; an ``AttributeError`` here would escape the
        # CLI as an uncaught traceback, defeating the fail-closed
        # contract.
        author_obj = node.get("author") or {}
        author_login = author_obj.get("login") or ""
        if _normalize_coderabbit_login(author_login) == target:
            return node.get("state")
    return None


# Maximum number of pages to walk through the CodeRabbit review
# inventory. 10 pages × 100 = 1000 reviews is well above any realistic
# PR's review count, and bounds total latency.
_CODERABBIT_MAX_PAGES = 10


def _fetch_coderabbit_review_state(
    gh_executable: str,
    *,
    owner: str,
    name: str,
    pr_number: int,
) -> Optional[str]:
    """Fetch the latest CodeRabbit review state with pagination.

    Uses GraphQL variables for the owner, name, and PR number so the
    query is well-formed regardless of how the CLI was invoked. Walks
    every page of ``latestReviews`` via ``pageInfo.hasNextPage`` until
    a CodeRabbit review is found or the inventory is exhausted, so
    even an early review at position N is reachable. Returns the
    state of the matching review, or ``None`` if no CodeRabbit review
    exists in the inventory (fail-closed C-25).
    """
    query = (
        "query($owner:String!,$name:String!,$pr:Int!,$cursor:String){"
        "repository(owner:$owner,name:$name){"
        "pullRequest(number:$pr){"
        "latestReviews(first:100, after:$cursor){"
        "pageInfo { hasNextPage endCursor }"
        "nodes { author { login } state }"
        "}}}"
    )
    # ``cursor`` is None on the first request and the prior
    # page's endCursor on subsequent requests. ``gh api
    # graphql -F cursor=...`` sends the raw string; the
    # GraphQL server expects a JSON null for the first
    # page. Calling with ``-F cursor="null"`` (the four
    # character string) makes the server reject the first
    # request, so the path is conditioned on cursor being
    # set.
    #
    # The path fails CLOSED on every incomplete-inventory
    # signal (subprocess error, JSON error, missing page,
    # page-limit, or hasNextPage without endCursor). A
    # partial inventory that already shows an APPROVED
    # state from an earlier round must NOT satisfy the
    # merge guard when a later page cannot be confirmed.
    cursor: Optional[str] = None
    has_next = True
    page_count = 0
    matched_state: Optional[str] = None
    while has_next:
        page_count += 1
        if page_count > _CODERABBIT_MAX_PAGES:
            # Defensive: refuse if more than 10 pages of
            # reviews exist. An incomplete inventory is a
            # guard failure.
            return None
        cmd = [
            gh_executable, "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}",
            "-F", f"name={name}",
            "-F", f"pr={pr_number}",
        ]
        if cursor is not None:
            cmd.extend(["-F", f"cursor={cursor}"])
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return None
        try:
            doc = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        # Round-29 P1#6: fail closed on partial GraphQL
        # responses. Top-level ``errors`` means the page is
        # partial even if ``data`` is present.
        if isinstance(doc, dict) and isinstance(doc.get("errors"), list) and doc["errors"]:
            return None
        page = (
            doc.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("latestReviews", {})
        )
        if not page:
            return None
        # Round-29 P1#6: ``nodes`` MUST be a list. Missing
        # ``nodes`` / ``pageInfo`` is a partial response.
        if not isinstance(page.get("nodes"), list):
            return None
        if not isinstance(page.get("pageInfo"), dict):
            return None
        # Filter for CodeRabbit matches on this page.
        match = _filter_coderabbit_review_state(doc)
        if match is not None and matched_state is None:
            matched_state = match
        page_info = page.get("pageInfo", {})
        has_next = bool(page_info.get("hasNextPage"))
        # If hasNextPage is set but endCursor is missing,
        # the inventory is incomplete: fail closed.
        if has_next and not page_info.get("endCursor"):
            return None
        # ``endCursor`` is None on the final page; the next
        # iteration's cursor is None so the first-page
        # logic above runs again (which is correct: the
        # loop terminates via ``has_next``).
        cursor = page_info.get("endCursor") or None
    return matched_state


def _resolve_evidence_root(args: argparse.Namespace, store) -> Path:
    """Resolve the canonical evidence root for this CLI invocation.

    Per C-24 the three named roots must be independent. The persisted
    ``RunContext.evidence_root`` is the canonical source — the CLI
    reads it from the run-state store when available. The operator
    may pass ``--evidence-root`` to override the persisted path;
    conflicting overrides are rejected so a non-canonical evidence
    root can never be silently used.

    Fail-closed behavior (per PR #4 round-2 review):

    * If the run context exists but cannot be read (I/O failure),
      parsed (malformed JSON), or trusted (``StateStoreError``),
      the call MUST NOT silently substitute a fallback evidence
      root. Falling back to ``state_root.parent / "evidence"`` on
      a transient read failure would route ``cmd_merge`` to a
      different evidence root than the one ``cmd_merge_authorize``
      bound into the authorization, defeating the digest contract.
      Instead, the failure is propagated as ``StateStoreError`` so
      the CLI's existing error handler returns a controlled state
      failure with exit code EXIT_STATE.

    * The fallback derivation is permitted only when the run
      context is genuinely absent (``read_optional`` returns
      ``None``). A missing run context is the documented "first
      invocation" path: there is no persisted evidence root to
      diverge from.
    """
    # Distinguish "absent" from "unreadable". ``read_optional``
    # returns None only when the file is missing. Any other failure
    # (parse error, I/O error, StateStore validation failure) must
    # propagate so the CLI fails closed.
    rc = store.read_optional("run_context.json")
    persisted: Optional[str] = None
    if rc is not None:
        persisted = rc.get("evidence_root") if isinstance(rc, dict) else None

    override = getattr(args, "evidence_root", None)
    if override and persisted and Path(str(override)).resolve() != Path(str(persisted)).resolve():
        raise MergeInputsCollide(
            f"--evidence-root {override!r} conflicts with persisted "
            f"RunContext.evidence_root {persisted!r}"
        )

    if override:
        return Path(str(override))
    if persisted:
        return Path(str(persisted))
    # Genuinely absent run context: the documented first-invocation
    # fallback is permitted because no persisted evidence root can
    # diverge from the requested root. State this explicitly so the
    # audit log records the derivation.
    state_root_path = Path(str(store.state_root))
    return state_root_path.parent / "evidence"


def _emit(payload: Dict[str, Any], *, json_mode: bool, exit_code: int) -> int:
    if json_mode:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        for key, value in payload.items():
            sys.stdout.write(f"{key}: {value}\n")
    return exit_code


def _read_state(store: StateStore) -> StateMachine:
    payload = store.read_optional("state.json")
    if payload is None:
        raise StateStoreError("state.json not found")
    return StateMachine.from_dict(payload)


def cmd_status(args: argparse.Namespace) -> int:
    store = StateStore(args.state_root)
    sm = _read_state(store)
    payload = {
        "run_id": args.run_id,
        "state_root": store.state_root,
        "current_state": sm.current_state,
        "revision": sm.revision,
        "journal_entries": len(sm.journal),
    }
    return _emit(payload, json_mode=args.json, exit_code=EXIT_OK)


def cmd_gates(args: argparse.Namespace) -> int:
    """Inspect gate failures for the current readiness decision."""
    store = StateStore(args.state_root)
    cert_payload = store.read_optional("readiness.json")
    if cert_payload is None:
        return _emit(
            {"error": "no readiness certificate on file"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    cert = ReadinessCertificate.from_dict(cert_payload)
    failed = cert.decision.failed_gates()
    payload = {
        "run_id": args.run_id,
        "overall_passed": cert.decision.overall_passed,
        "failed_gates": [g.to_dict() for g in failed],
        "evaluated_at": cert.decision.evaluated_at,
    }
    return _emit(payload, json_mode=args.json, exit_code=EXIT_OK)


def cmd_initialize(args: argparse.Namespace) -> int:
    """Create a new run: writes the run context and an initial state machine."""
    required = [
        "owner", "repo", "local_checkout", "base_branch",
        "authorized_base_sha", "feature_branch",
        "taskspec_path", "taskspec_sha256",
    ]
    for r in required:
        if not getattr(args, r, None):
            return _emit(
                {"error": f"missing required flag: --{r}"},
                json_mode=args.json,
                exit_code=EXIT_INVARG,
            )
    # Required CI jobs precedence (round-591):
    # 1. --required-ci-jobs flag from operator (explicit).
    # 2. Existing RunContext's required_ci_jobs (persisted
    #    by a prior initialize call or supervisor config).
    # 3. Operator-policy SEVEN-NAME default (the exact seven
    #    GitHub check-run names for PR #5):
    #       test (3.10), test (3.11), test (3.12),
    #       package-smoke,
    #       provenance, committed-state-scan,
    #       full-suite.
    # The persisted RunContext MUST take precedence over
    # the default set when no explicit flag is provided.
    # A configured gate such as ``security-scan`` MUST
    # be able to block qualification/merge.
    required_ci_jobs: list = []
    if args.required_ci_jobs:
        required_ci_jobs = args.required_ci_jobs.split(",")
    else:
        # Read the persisted RunContext if it exists.
        # The persisted RunContext MUST take precedence
        # over the default set when no explicit flag is
        # provided. A configured gate such as
        # ``security-scan`` MUST be able to block
        # qualification/merge.
        existing_ctx_dict = StateStore(args.state_root).read_optional(
            "run_context.json",
        )
        if (
            existing_ctx_dict is not None
            and existing_ctx_dict.get("required_ci_jobs")
        ):
            required_ci_jobs = list(
                existing_ctx_dict["required_ci_jobs"],
            )
        else:
            # round-591: SEVEN-NAME operator policy (default).
            # The previous six-job default omitted ``full-suite``,
            # which is the seventh authoritative required check
            # on PR #5.
            required_ci_jobs = [
                "test (3.10)", "test (3.11)", "test (3.12)",
                "package-smoke", "provenance",
                "committed-state-scan",
                "full-suite",
            ]
    impl_cmd = tuple(args.impl_worker_command.split()) if args.impl_worker_command else (
        "/usr/bin/env", "true", "{prompt}", "{session_id}"
    )
    run_id = args.run_id or generate_run_id()
    ctx = make_run_context(
        repo_owner=args.owner,
        repo_name=args.repo,
        local_checkout=args.local_checkout,
        base_branch=args.base_branch,
        authorized_base_sha=args.authorized_base_sha,
        feature_branch=args.feature_branch,
        task_specification_path=args.taskspec_path,
        task_specification_sha256=args.taskspec_sha256,
        required_ci_jobs=list(required_ci_jobs),
        implementation_worker_command=list(impl_cmd),
        evidence_root=args.evidence_root,
        state_root=args.state_root,
        run_id=run_id,
    )
    Path(ctx.state_path).mkdir(parents=True, exist_ok=True)
    store = StateStore(ctx.state_path)
    store.write_atomic("run_context.json", ctx.to_dict())
    sm = StateMachine()
    store.write_atomic("state.json", sm.to_dict())
    payload = {
        "run_id": run_id,
        "state_path": ctx.state_path,
    }
    return _emit(payload, json_mode=args.json, exit_code=EXIT_OK)


def cmd_migrate_required_ci_jobs(args: argparse.Namespace) -> int:
    """Round-591: audited, narrow migration of the persisted
    ``required_ci_jobs`` set when a stale run was initialized
    with non-operator-policy identities (e.g. ``['test', 'lint']``
    from an early bootstrap before the GitHub workflow added
    ``full-suite``).

    Engineering bootstrap authorization: this is the ONLY
    canonical path for changing a persisted
    ``required_ci_jobs``. The supervisor, the CLI, and the
    tests cannot silently rewrite the persisted policy.

    Invariants (all enforced; fail-closed):

    - ``--expected-old`` MUST equal the persisted
      ``required_ci_jobs`` exactly (both length and order).
      If the persisted value differs, the call is REJECTED
      so a stale caller cannot accidentally narrow the
      operator policy.
    - ``--new-required-ci-jobs`` MUST be a non-empty list.
    - The migration is atomic via the
      ``StateStore.compare_and_swap`` path; on success,
      the persisted ``run_context.json`` revision is
      incremented and the durable audit trail
      ``required_ci_jobs_migrations.json`` records the
      before/after/by/at pair.
    - Run identity, PR identity, authorized head, and worker /
      generation history are preserved — only the
      ``required_ci_jobs`` field changes.
    - Idempotent: re-running with the already-applied
      ``--expected-old`` will fail-closed (expected_old
      does not match the NEW value).
    """
    state_store = StateStore(args.state_root)
    # Read strictly; missing run_context fails closed.
    if not state_store.exists("run_context.json"):
        return _emit(
            {"error": "run_context.json does not exist; nothing to migrate"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    rev_marker = "run_context.json"
    expected_rev = -1
    ctx_dict = None
    try:
        # First read is informational (revision lookup).
        ctx_dict = state_store.read_strict(rev_marker)
        expected_rev = int(ctx_dict.get("_revision", 0))
    except StateStoreError as exc:
        return _emit(
            {"error": f"cannot read run_context.json: {exc}"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    # Required set BEFORE the migration.
    persisted = list(ctx_dict.get("required_ci_jobs") or [])
    expected_old = [
        name.strip() for name in (args.expected_old or "").split(",") if name.strip()
    ]
    if tuple(persisted) != tuple(expected_old):
        return _emit(
            {
                "error": (
                    "expected-old mismatch: persisted "
                    f"{persisted!r} != --expected-old {expected_old!r}; "
                    "refusing to migrate to avoid silent policy change"
                ),
                "persisted_required_ci_jobs": persisted,
                "expected_old": expected_old,
            },
            json_mode=args.json,
            exit_code=EXIT_INVARG,
        )
    new_required_ci_jobs = [
        name.strip()
        for name in (args.new_required_ci_jobs or "").split(",")
        if name.strip()
    ]
    if not new_required_ci_jobs:
        return _emit(
            {"error": "--new-required-ci-jobs must be non-empty"},
            json_mode=args.json,
            exit_code=EXIT_INVARG,
        )
    # Safety: refuse silent reduction relative to operator
    # policy intent. Every entry in the new set must already
    # be the seven-name operator policy, OR the operation
    # must specify a complete replacement. A narrowing of
    # the seven-name operator-policy set is REJECTED.
    if set(new_required_ci_jobs) != set(SEVEN_NAME_OPERATOR_POLICY):
        return _emit(
            {
                "error": (
                    "--new-required-ci-jobs MUST equal the "
                    "exact seven-name operator-policy set; "
                    "narrowing is forbidden by round-591"
                ),
                "expected_seven_name": list(SEVEN_NAME_OPERATOR_POLICY),
                "got": new_required_ci_jobs,
            },
            json_mode=args.json,
            exit_code=EXIT_INVARG,
        )
    # Atomically rewrite run_context.json via CAS so concurrent
    # relaunches can't interleave a stale read.
    new_ctx = dict(ctx_dict)
    new_ctx.pop("_revision", None)
    new_ctx.pop("_written_at", None)
    new_ctx["required_ci_jobs"] = list(new_required_ci_jobs)
    try:
        new_rev = state_store.compare_and_swap(
            rev_marker, new_ctx, expected_revision=expected_rev
        )
    except StateStoreError as exc:
        return _emit(
            {"error": f"compare_and_swap failed: {exc}"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    # Persist a durable audit trail.
    audit_entry = {
        "migration_id": (
            f"rcimgr-{new_rev.path.replace('/', '_')}@{new_rev.revision}"
        ),
        "from_required_ci_jobs": persisted,
        "to_required_ci_jobs": list(new_required_ci_jobs),
        "by": args.by,
        "reason": args.reason,
        "at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        ),
        "run_id": ctx_dict.get("run_id"),
        "pr_number": ctx_dict.get("pr_number"),
        "rev_before": expected_rev,
        "rev_after": new_rev.revision,
    }
    try:
        state_store.append_journal("required_ci_jobs_migrations.json", audit_entry)
    except (StateStoreError, OSError) as exc:
        # Migration succeeded but audit-trail append failed.
        # ``append_journal`` calls ``chmod`` / ``open`` / ``write``
        # directly on the filesystem, so a real out-of-space
        # or permission failure propagates as ``OSError`` rather
        # than as ``StateStoreError`` — both must surface the same
        # non-zero exit code so callers that check exit status
        # alone do not record the migration as fully audited.
        # The durable run_context.json change is the canonical
        # record (do not roll back), BUT the audit trail is the
        # stated control for this command, so the loss MUST be
        # visible: signal a non-zero exit code and an explicit
        # ``error`` key so callers that check exit status alone
        # do not record the migration as fully audited.
        return _emit(
            {
                "error": (
                    f"migration committed (rev {new_rev.revision}) "
                    f"but audit-trail append failed: {exc}"
                ),
                "warning": (
                    f"migration committed (rev {new_rev.revision}) "
                    f"but audit-trail append failed: {exc}"
                ),
                "migration": audit_entry,
            },
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    return _emit(
        {
            "migration": audit_entry,
            "new_revision": new_rev.revision,
            "new_required_ci_jobs": list(new_required_ci_jobs),
        },
        json_mode=args.json,
        exit_code=EXIT_OK,
    )


def cmd_run(args: argparse.Namespace) -> int:
    """Run a no-op execution cycle: print the current state.

    The CLI is intentionally a thin wrapper. The actual execution
    is performed by the controller via the run context. This
    command is a self-hosting entry point and a smoke test.
    """
    store = StateStore(args.state_root)
    sm = _read_state(store)
    return _emit(
        {"run_id": args.run_id, "state": sm.current_state},
        json_mode=args.json,
        exit_code=EXIT_OK,
    )


def cmd_observe(args: argparse.Namespace) -> int:
    """Run the strict readiness observer with a stub data source.

    The CLI returns a JSON summary of the observation log. The
    caller is expected to use the Library :class:`StrictObserver`
    for real readiness qualification.
    """
    return _emit(
        {"error": "observe command requires a Live data source; use the StrictObserver library directly"},
        json_mode=args.json,
        exit_code=EXIT_INVARG,
    )


def cmd_build_candidate(args: argparse.Namespace) -> int:
    """Build the candidate from the readiness certificate.

    Refuses to build without a readiness certificate.
    """
    store = StateStore(args.state_root)
    cert_payload = store.read_optional("readiness.json")
    if cert_payload is None:
        return _emit(
            {"error": "no readiness certificate on file"},
            json_mode=args.json,
            exit_code=EXIT_GUARD,
        )
    cert = ReadinessCertificate.from_dict(cert_payload)
    if not cert.decision.overall_passed:
        return _emit(
            {"error": "readiness certificate has failed gates"},
            json_mode=args.json,
            exit_code=EXIT_GUARD,
        )
    rc = store.read_optional("run_context.json")
    if rc is None:
        return _emit(
            {"error": "no run context on file"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    ctx = RunContext.from_dict(rc)
    # Build candidate using provided expected inputs
    file_paths = args.file_paths.split(",") if args.file_paths else []
    aed_paths = args.aed_paths.split(",") if args.aed_paths else []
    builder = CandidateBuilder(
        run_id=ctx.run_id,
        repo=f"{ctx.repo_owner}/{ctx.repo_name}",
        pr_number=ctx.pr_number,
        expected_head=ctx.current_authorized_head or "",
        base_sha=ctx.authorized_base_sha,
        base_branch=ctx.base_branch,
        task_specification_sha256=ctx.task_specification_sha256,
        ci_inventory=[g.to_dict() for g in cert.decision.gate_results],
        review_inventory=[],
        thread_inventory={},
        strict_observation_log_hash="",
        controller_state_revision=0,
        controller_state_path="state.json",
        process_identity={},
        lock_release_evidence={},
        expected_input_hashes={},
        file_paths_to_attach=file_paths,
        aed_source_paths=aed_paths,
        aed_source_commit="b57fcaad806c68b93668bcd318fa26ab15a8ab40",
        aed_repo_root=args.aed_repo,
    )
    try:
        candidate = builder.build(cert, args.local_checkout)
    except CandidateError as e:
        return _emit(
            {"error": f"candidate build failed: {e!r}"},
            json_mode=args.json,
            exit_code=EXIT_GUARD,
        )
    # Single canonical producer. ``Controller.build_candidate``
    # writes the canonical evidence-root ``candidate.json`` (with
    # the ``_sha256``-enriched payload the merge transaction
    # expects) and the state-root ``candidate.json`` audit copy.
    # It also performs the ``READY_FOR_CANDIDATE -> CANDIDATE_FROZEN``
    # state transition through the established safe order. Routing
    # through the Controller guarantees identical canonical bytes
    # for identical candidate data, regardless of which public
    # surface produced them. The duplicate canonical write that
    # used to follow is removed.
    controller = Controller(ctx, store)
    try:
        controller.build_candidate(
            candidate, head_observed=str(ctx.current_authorized_head or ""),
        )
    except (ControllerError, StateStoreError, StateError,
            ArtifactError) as e:
        # Pre-merge failures return controlled state / guard
        # exits. A canonical-write failure (``ArtifactError``) is
        # a state failure: the run has not transitioned and a
        # retry can resume cleanly.
        if isinstance(e, ArtifactError):
            return _emit(
                {"error": f"canonical candidate write failed: {e!r}"},
                json_mode=args.json,
                exit_code=EXIT_STATE,
            )
        return _emit(
            {"error": f"candidate transition rejected: {e!r}"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    # Capture the canonical artifact digest for the response
    # payload from the canonical evidence root that the merge
    # transaction will re-read. This is a single read; no
    # ``write_artifact`` call is duplicated.
    from .artifacts import read_artifact as _read_canonical
    from .canonical_paths import canonical_paths as _cp
    canonical_candidate_path = _cp(Path(ctx.evidence_root))["candidate"]
    canonical_candidate_digest = _read_canonical(
        canonical_candidate_path,
    ).digest
    return _emit(
        {
            "run_id": args.run_id,
            "candidate_sha256": candidate.compute_sha256(),
            "canonical_candidate_path": str(canonical_candidate_path),
            "canonical_candidate_sha256": canonical_candidate_digest,
        },
        json_mode=args.json,
        exit_code=EXIT_OK,
    )


def cmd_handoff_verifier(args: argparse.Namespace) -> int:
    """Write a verifier handoff record for the current run.

    The verifier handoff is COORDINATION metadata. It records
    which external verifier worker was asked to run, against
    which exact candidate digest, and from which trusted
    verifier package version. The verifier worker reads this
    handoff, runs its independent verification, and writes a
    separate canonical evidence-root ``verifier.json`` (the
    authoritative merge input).

    The handoff is NEVER read by ``cmd_merge_authorize`` or
    ``cmd_merge``. ``cmd_merge_authorize`` binds the digest of
    the canonical evidence-root ``verifier.json``; the
    merge transaction re-verifies that digest. The handoff
    therefore cannot influence merge authorization under any
    sequence of writes. This is the architectural separation
    that makes the handoff safe to remain in state-root.
    """
    store = StateStore(args.state_root)
    cert_payload = store.read_optional("readiness.json")
    if cert_payload is None:
        return _emit(
            {"error": "no readiness certificate on file"},
            json_mode=args.json,
            exit_code=EXIT_GUARD,
        )
    cert = ReadinessCertificate.from_dict(cert_payload)
    cand_payload = store.read_optional("candidate.json")
    if cand_payload is None:
        return _emit(
            {"error": "no candidate on file"},
            json_mode=args.json,
            exit_code=EXIT_GUARD,
        )
    cand = Candidate.from_dict(cand_payload)
    rc = store.read_optional("run_context.json")
    if rc is None:
        return _emit(
            {"error": "no run context on file"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    ctx = RunContext.from_dict(rc)
    handoff = VerifierHandoff(
        schema_version="autocoder.verifier_handoff.v1",
        run_id=args.run_id,
        repo=f"{ctx.repo_owner}/{ctx.repo_name}",
        pr_number=ctx.pr_number,
        exact_head=ctx.current_authorized_head or "",
        base_sha=ctx.authorized_base_sha,
        base_branch=ctx.base_branch,
        task_specification_sha256=ctx.task_specification_sha256,
        candidate_path="candidate.json",
        candidate_sha256=cand.compute_sha256(),
        readiness_certificate_id=cert.certificate_id,
        observation_log_path="observations.jsonl",
        observation_log_sha256="",
        strict_window_first_utc=None,
        strict_window_last_utc=None,
        strict_window_observation_count=0,
        strict_window_duration_monotonic=0.0,
        controller_state_revision=0,
        controller_state_path="state.json",
        trusted_verifier_source_commit=args.trusted_verifier_source_commit,
        trusted_verifier_package_version="autocoder-orchestration-1.0.0",
        implementation_worker_identity=None,
        verifier_record_path="verifier-record.json",
        created_at="",
    )
    write_handoff(store, handoff)
    return _emit(
        {
            "run_id": args.run_id,
            "handoff_sha256": handoff.compute_sha256(),
        },
        json_mode=args.json,
        exit_code=EXIT_OK,
    )


def cmd_apply_verifier_result(args: argparse.Namespace) -> int:
    """Apply a verifier record to the controller."""
    store = StateStore(args.state_root)
    rc = store.read_optional("run_context.json")
    if rc is None:
        return _emit(
            {"error": "no run context on file"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    ctx = RunContext.from_dict(rc)
    controller = Controller(ctx, store)
    verifier_record = json.loads(args.verifier_record.read_text())
    verdict = verifier_record.get("verdict", "FAILED")
    if verdict == "VERIFIED":
        sm = controller.verifier_passed(
            head_observed=ctx.current_authorized_head or "",
            verifier_record=verifier_record,
        )
    else:
        sm = controller.verifier_failed(
            head_observed=ctx.current_authorized_head or "",
            verifier_record=verifier_record,
        )
    return _emit(
        {"run_id": args.run_id, "state": sm.current_state},
        json_mode=args.json,
        exit_code=EXIT_OK,
    )


def cmd_merge_authorize(args: argparse.Namespace) -> int:
    """Record a human merge authorization.

    The candidate and verifier artifacts that bound this
    authorization MUST be the canonical evidence-root artifacts
    that ``cmd_merge`` later consumes. The state-root copies
    (``<state_root>/candidate.json`` and ``<state_root>/verifier-record.json``)
    exist solely as secondary audit observables and are NOT merge
    authorization inputs; this command MUST NOT read them when
    building the authorization. Reading the canonical artifacts
    via ``read_artifact`` validates the exact-file digest and the
    sidecar; that digest is the value bound into the
    authorization. Authorization therefore binds the same digests
    that the merge transaction will re-read.

    Safe ordering (per PR #4 round-2 review):

    1. Validate the authorization preconditions (state machine is
       in AWAITING_MERGE_AUTHORIZATION, authorized head matches
       ``ctx.current_authorized_head``, candidate + verifier carry
       ``verdict == VERIFIED`` and a qualifying verdict binding).
    2. Build ``MergeAuthorization`` only after every precondition
       passes.
    3. Persist the canonical authorization artifact via
       ``Controller.authorize_merge(auth)`` — which itself
       validates the transition BEFORE writing.
    4. Persist the canonical evidence-root ``authorization.json``
       only after the controller call returns successfully.

    A rejected authorization leaves NO canonical
    ``authorization.json`` on disk. ``cmd_merge`` cannot consume
    rejected authorization evidence.
    """
    store = StateStore(args.state_root)
    rc = store.read_optional("run_context.json")
    if rc is None:
        return _emit(
            {"error": "no run context on file"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    ctx = RunContext.from_dict(rc)

    # Resolve the canonical evidence root BEFORE reading any
    # candidate/verifier evidence. Per C-24 the evidence root is
    # independent of the state root, so this resolution MUST happen
    # before any read. All four control-plane artifacts live here.
    evidence_root = _resolve_evidence_root(args, store)
    paths = _canonical_artifact_paths(evidence_root)

    # Read the candidate from the canonical evidence root through
    # the canonical artifact reader. ``read_artifact`` verifies
    # the sidecar digest and raises on any failure.
    from .artifacts import (
        ArtifactError,
        read_artifact as _read_canonical_artifact,
    )
    try:
        candidate_result = _read_canonical_artifact(paths["candidate"])
    except FileNotFoundError:
        return _emit(
            {
                "error": (
                    f"no canonical candidate at {paths['candidate']}; "
                    "authorization cannot bind a non-canonical artifact"
                ),
            },
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    except ArtifactError as e:
        return _emit(
            {"error": f"canonical candidate unreadable: {e!r}"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    cand_payload = candidate_result.payload
    try:
        cand = Candidate.from_dict(cand_payload)
    except (ValueError, TypeError, KeyError) as e:
        # The canonical artifact bytes have been validated against
        # the sidecar by ``read_artifact``, but the payload schema
        # may still be malformed (wrong schema_version, missing
        # fields, mistyped values). Convert this to a controlled
        # state failure with EXIT_STATE; a correctly signed
        # malformed candidate MUST NEVER cause an uncaught
        # traceback.
        return _emit(
            {"error": f"canonical candidate schema invalid: {e!r}"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    if cand.exact_head != ctx.current_authorized_head:
        return _emit(
            {"error": "candidate head does not match current authorized head"},
            json_mode=args.json,
            exit_code=EXIT_GUARD,
        )

    # Read the verifier record from the canonical evidence root.
    try:
        verifier_result = _read_canonical_artifact(paths["verifier"])
    except FileNotFoundError:
        return _emit(
            {
                "error": (
                    f"no canonical verifier at {paths['verifier']}; "
                    "authorization cannot bind a non-canonical artifact"
                ),
            },
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    except ArtifactError as e:
        return _emit(
            {"error": f"canonical verifier unreadable: {e!r}"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )

    # Semantic verdict gate (per PR #4 round-2 review).
    #
    # ``canonical verifier.json`` may be the LAST WRITTEN record,
    # irrespective of verdict. ``verifier_failed`` writes the same
    # canonical path; a failing attempt can therefore overwrite a
    # passing one if a verifier process retries. ``cmd_merge_authorize``
    # must independently inspect the record's verdict and reject any
    # artifact whose ``verdict`` is not exactly ``VERIFIED``.
    #
    # The two acceptable shapes for a passing record are:
    #   ``{"verdict": "VERIFIED", ...}`` (top-level), or
    #   ``{"verdict": "VERIFIED", "defects": [], ...}``.
    # Any other verdict value (including missing, "FAILED",
    # "ERROR", "INCONCLUSIVE", or the ``_verdict_failed`` flag set
    # by ``Controller.verifier_failed``) is rejected.
    verdict = verifier_result.payload.get("verdict")
    if verdict != "VERIFIED":
        return _emit(
            {
                "error": (
                    f"canonical verifier at {paths['verifier']} has "
                    f"verdict {verdict!r}; authorization requires "
                    "exactly verdict == 'VERIFIED'; a failed or "
                    "absent verification MUST NOT become merge "
                    "evidence"
                ),
                "verifier_verdict": verdict,
            },
            json_mode=args.json,
            exit_code=EXIT_GUARD,
        )
    if verifier_result.payload.get("_verdict_failed") is True:
        return _emit(
            {
                "error": (
                    f"canonical verifier at {paths['verifier']} is "
                    "tagged as a failed attempt; authorization "
                    "MUST NOT bind its digest"
                ),
            },
            json_mode=args.json,
            exit_code=EXIT_GUARD,
        )

    # Bind the EXACT-FILE DIGESTS returned by ``read_artifact``
    # (already validated against the on-disk sidecar). These are
    # the same digests the merge transaction will re-read; the
    # state-root copies are intentionally ignored.
    candidate_digest = candidate_result.digest
    verifier_digest = verifier_result.digest
    auth = MergeAuthorization(
        schema_version="autocoder.merge_authorization.v1",
        run_id=args.run_id,
        repo=f"{ctx.repo_owner}/{ctx.repo_name}",
        pr_number=args.pr_number,
        authorized_head=args.authorized_head,
        candidate_sha256=candidate_digest,
        verifier_record_sha256=verifier_digest,
        merge_method=args.method,
        delete_branch=not args.keep_branch,
        require_match_head_commit=True,
        authorization_timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        author=args.author,
        next_wave_authorization=None,
        notes=args.notes,
        required_ci_jobs=tuple(ctx.required_ci_jobs or ()),
    )
    auth_payload = auth.to_dict()
    auth_payload["_sha256"] = auth.compute_sha256()
    # The Controller owns the complete safe transaction:
    # validate inputs -> validate state -> validate head ->
    # canonical write -> state-root write -> state transition.
    # A failure at ANY step leaves the run at
    # AWAITING_MERGE_AUTHORIZATION so a retry can succeed
    # without re-entering an already-committed transition.
    controller = Controller(ctx, store)
    try:
        sm = controller.authorize_merge(auth)
    except (ControllerError, StateStoreError, StateError,
            ArtifactError) as e:
        # Catch every precondition / write failure. The
        # canonical authorization.json MUST NOT appear when
        # authorization is rejected, so a follow-up retry
        # can re-authorize cleanly.
        if isinstance(e, ArtifactError):
            return _emit(
                {"error": f"authorization canonical write failed: {e!r}"},
                json_mode=args.json,
                exit_code=EXIT_STATE,
            )
        return _emit(
            {"error": f"authorization transition rejected: {e!r}"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    # Controller wrote both canonical authorization.json and
    # the state-root merge-authorization.json, then committed
    # MERGE_AUTHORIZED. No duplicate canonical write here.
    return _emit(
        {
            "run_id": args.run_id,
            "state": sm.current_state,
            "candidate_digest_source": "canonical_evidence_root",
            "verifier_digest_source": "canonical_evidence_root",
            "candidate_digest": candidate_digest,
            "verifier_digest": verifier_digest,
        },
        json_mode=args.json,
        exit_code=EXIT_OK,
    )


def cmd_merge(args: argparse.Namespace) -> int:
    """Execute the guarded merge via the production transaction.

    This command delegates to ``execute_guarded_merge_transaction``,
    which is the single checked-in path that loads and verifies the
    authorization / candidate / verifier artifacts, fetches live
    GitHub evidence, repeats every exact-head and integrity guard,
    invokes the guarded ``gh pr merge`` command once with a finite
    timeout, reconciles post-merge state, and writes the merge
    record through the canonical artifact writer.

    After the transaction returns, ``cmd_merge`` itself persists
    the COMPLETE state transition by calling
    ``Controller.report_complete()``. The transaction does NOT
    transition to COMPLETE; the durable state change is this
    command's responsibility. If ``report_complete`` raises, the
    merge record remains durable for retry and ``cmd_merge``
    returns EXIT_STATE.

    Production flows MUST NOT bypass this function. No production code
    may call ``MergeExecutor().compute_command`` and ``_run`` directly.
    """
    store = StateStore(args.state_root)
    rc = store.read_optional("run_context.json")
    if rc is None:
        return _emit(
            {"error": "no run context on file"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    ctx = RunContext.from_dict(rc)
    # Resolve the canonical evidence root once per invocation. All four
    # control-plane artifacts share this root (per C-24).
    evidence_root = _resolve_evidence_root(args, store)
    paths = _canonical_artifact_paths(evidence_root)

    # Read the canonical authorization artifact from the evidence root.
    from .artifacts import read_artifact
    try:
        auth_result = read_artifact(paths["authorization"])
    except FileNotFoundError:
        return _emit(
            {"error": f"no merge authorization at {paths['authorization']}"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    except Exception as e:
        return _emit(
            {"error": f"merge authorization unreadable: {e!r}"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    try:
        auth = MergeAuthorization.from_dict(auth_result.payload)
    except (ValueError, TypeError, MergeAuthorizationMalformed) as e:
        return _emit(
            {"error": f"merge authorization malformed: {e!r}"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    # Round-28 P2: the merge authorization MUST carry the run's
    # configured required CI jobs so the locked mutable-gate
    # cross-binding guard can compare the human-signed
    # approval against the persisted run policy. Production
    # code injects ``ctx.required_ci_jobs`` so the auth always
    # binds to the same set as the persisted ``RunContext``.
    # ``MergeAuthorization`` is a frozen dataclass; we use
    # ``object.__setattr__`` to override the artifact value
    # with the persisted run policy.
    target_ci = tuple(ctx.required_ci_jobs or ())
    if auth.required_ci_jobs != target_ci:
        try:
            object.__setattr__(auth, "required_ci_jobs", target_ci)
        except Exception:
            # If the frozen dataclass somehow refuses
            # ``__setattr__``, the merge gate's cross-binding
            # guard below sees the divergence and fails closed.
            pass
    authorization_path = paths["authorization"]
    candidate_path = paths["candidate"]
    verifier_path = paths["verifier"]
    merge_record_path = paths["merge_record"]

    # Fetch live PR identity via gh using the production helper.
    try:
        live_pr_payload = fetch_live_pr_payload("gh", auth.repo, auth.pr_number)
    except Exception as e:
        return _emit(
            {"error": f"failed to fetch live PR state: {e!r}"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )

    # Fetch live thread inventory via gh graphql with pagination.
    # An incomplete inventory is treated as a guard failure (C-22):
    # missing data must never weaken merge authorization. We follow
    # reviewThreads.pageInfo.hasNextPage and fail closed if another
    # page exists or if the query fails.
    live_thread_inventory = None
    try:
        all_nodes = []
        has_next = True
        # ``cursor`` is None on the first request; the
        # ``-F cursor=...`` argument is only added when a
        # cursor exists. ``-F cursor="null"`` (the string)
        # would make the GraphQL server reject the first
        # request, so the path is conditioned on cursor
        # being set.
        cursor: Optional[str] = None
        page_count = 0
        while has_next:
            page_count += 1
            if page_count > 10:
                # Defensive: refuse if more than 10 pages of review
                # threads exist (well above realistic limits).
                raise RuntimeError(
                    f"reviewThreads pagination exceeded {page_count} pages"
                )
            q = (
                "query($owner:String!,$name:String!,$pr:Int!,$cursor:String){"
                "repository(owner:$owner,name:$name){"
                "pullRequest(number:$pr){"
                "reviewThreads(first:100, after:$cursor){"
                "pageInfo { hasNextPage endCursor }"
                "nodes { isResolved isOutdated }"
                "}}}"
            )
            thread_cmd = [
                "gh", "api", "graphql",
                "-f", f"query={q}",
                "-F", f"owner={ctx.repo_owner}",
                "-F", f"name={ctx.repo_name}",
                "-F", f"pr={auth.pr_number}",
            ]
            if cursor is not None:
                thread_cmd.extend(["-F", f"cursor={cursor}"])
            thread_proc = subprocess.run(
                thread_cmd, capture_output=True, text=True, timeout=30,
            )
            if thread_proc.returncode != 0:
                raise RuntimeError(
                    f"gh graphql reviewThreads page {page_count} "
                    f"failed: rc={thread_proc.returncode} "
                    f"stderr={thread_proc.stderr.strip()}"
                )
            td = json.loads(thread_proc.stdout)
            page = (
                td.get("data", {}).get("repository", {})
                  .get("pullRequest", {}).get("reviewThreads", {})
            )
            if not page:
                raise RuntimeError(
                    f"gh graphql reviewThreads page {page_count} "
                    f"returned no data: {thread_proc.stdout[:200]!r}"
                )
            # Round-171 P1: ``nodes`` MUST be a list. Missing
            # ``nodes`` / ``pageInfo`` is a partial response —
            # the merge guard MUST NOT accept it as empty.
            # Top-level GraphQL ``errors`` (with nonempty data)
            # is also a partial response; refuse it.
            top_errors = td.get("errors")
            if isinstance(top_errors, list) and top_errors:
                raise RuntimeError(
                    f"gh graphql reviewThreads page {page_count} "
                    f"returned partial response with errors: "
                    f"{thread_proc.stdout[:200]!r}"
                )
            if not isinstance(page.get("nodes"), list):
                raise RuntimeError(
                    f"gh graphql reviewThreads page {page_count} "
                    f"omitted ``nodes`` (partial response): "
                    f"{thread_proc.stdout[:200]!r}"
                )
            all_nodes.extend(page["nodes"])
            page_info = page.get("pageInfo", {})
            has_next = bool(page_info.get("hasNextPage"))
            # If hasNextPage is set but endCursor is missing,
            # the inventory is incomplete: fail closed.
            # A partial thread response MUST NOT be treated
            # as empty.
            if has_next and not page_info.get("endCursor"):
                raise RuntimeError(
                    f"gh graphql reviewThreads page {page_count} "
                    "reported hasNextPage=True but endCursor is "
                    "missing; inventory is incomplete"
                )
            # ``endCursor`` is None on the final page; the
            # loop terminates via ``has_next``. We do NOT
            # default to the string "null" — that would
            # re-send the broken first-page cursor.
            cursor = page_info.get("endCursor") or None

        unresolved_current = sum(
            1 for n in all_nodes
            if not n.get("isResolved") and not n.get("isOutdated")
        )
        unresolved_outdated = sum(
            1 for n in all_nodes
            if not n.get("isResolved") and n.get("isOutdated")
        )
        live_thread_inventory = {
            "unresolved_current": unresolved_current,
            "unresolved_outdated": unresolved_outdated,
        }
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError, RuntimeError) as exc:
        # Timeout, network error, or malformed JSON: incomplete thread
        # evidence cannot represent zero unresolved threads. Fail closed.
        return _emit(
            {"error": f"failed to fetch complete thread inventory: {exc!r}"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )

    # Fetch live CI inventory.
    live_ci_state = {"all_required_passing": False, "coderabbit_passing": False,
                     "checks": []}
    try:
        ci_proc = subprocess.run(
            ["gh", "pr", "checks", str(auth.pr_number), "--repo", auth.repo, "--json", "name,state"],
            capture_output=True, text=True, timeout=30,
        )
        if ci_proc.returncode == 0:
            checks = json.loads(ci_proc.stdout)
            live_ci_state["checks"] = checks
            required = {"test (3.10)", "test (3.11)", "test (3.12)",
                        "package-smoke", "provenance", "committed-state-scan"}
            passing = {
                name for c in checks
                if c.get("state") == "SUCCESS"
                # A check object without a ``name`` key cannot
                # contribute to the passing set; skip it so a
                # KeyError does not escape this handler.
                for name in [c.get("name")]
                if name
            }
            live_ci_state["all_required_passing"] = required.issubset(passing)
            live_ci_state["coderabbit_passing"] = "CodeRabbit" in passing
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        # Timeout or transient failure: retain the failing CI state
        # (all_required_passing stays False). Fail closed downstream.
        pass

    # Fetch the latest CodeRabbit review state. Filter reviews by the
    # configured CodeRabbit author login so a human review cannot
    # satisfy the CodeRabbit guard. Leave the field unavailable when
    # no matching review exists. The filter normalizes "coderabbitai"
    # and "coderabbitai[bot]" to the same identity.
    live_review_state: Dict[str, Optional[str]] = {"latest_coderabbit_state": None}
    try:
        match_state = _fetch_coderabbit_review_state(
            "gh",
            owner=ctx.repo_owner,
            name=ctx.repo_name,
            pr_number=auth.pr_number,
        )
        if match_state is not None:
            live_review_state["latest_coderabbit_state"] = match_state
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        # Timeout or transient failure: keep latest_coderabbit_state None
        # so the CodeRabbit guard fails closed.
        pass

    # Working tree clean? Measured BEFORE execute_guarded_merge_transaction
    # runs -- the measurement is a pre-flight guard, not a post-merge
    # reconciliation check. A post-merge dirty tree is detected by the
    # reconciliation inside execute_guarded_merge_transaction itself,
    # and surfaced via the merge record's working_tree_clean field.
    working_tree_clean = False
    try:
        wt_clean_proc = subprocess.run(
            ["git", "-C", str(ctx.local_checkout), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        working_tree_clean = (
            wt_clean_proc.returncode == 0 and wt_clean_proc.stdout.strip() == ""
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        # Timeout or transient failure: dirty is the safe default.
        working_tree_clean = False

    # Resolve canonical artifact paths for the production transaction.
    # All four named roots are independent (C-24); the canonical evidence
    # root was resolved once at the top of this function.
    authorization_path = paths["authorization"]
    candidate_path = paths["candidate"]
    verifier_path = paths["verifier"]
    merge_record_path = paths["merge_record"]

    inputs = MergeTransactionInputs(
        authorization_artifact_path=authorization_path,
        candidate_artifact_path=candidate_path,
        verifier_artifact_path=verifier_path,
        merge_record_artifact_path=merge_record_path,
        repository_checkout=Path(str(ctx.local_checkout)),
        run_state_root=Path(str(store.state_root)),
        evidence_root=Path(str(evidence_root)),
        live_pr_payload=live_pr_payload,
        live_ci_state=live_ci_state,
        live_review_state=live_review_state,
        live_thread_inventory=live_thread_inventory,
        working_tree_clean=working_tree_clean,
        # Round-28 P2: the run's configured required CI jobs are
        # the policy the locked mutable gate MUST enforce.
        # ``ctx.required_ci_jobs`` comes from the persisted
        # ``RunContext`` (set at orch init time) and is the
        # authoritative policy. Production code MUST pass it
        # into the guarded transaction; an empty tuple is only
        # acceptable when the persisted run policy explicitly
        # says there are zero required jobs.
        required_ci_names=tuple(ctx.required_ci_jobs or ()),
    )

    try:
        record, rec_digest = execute_guarded_merge_transaction(inputs)
    except (MergeAuthorizationMissing, MergeAuthorizationMalformed, MergeInputsCollide,
            MergeError, MergeSubprocessFailed, MergeAmbiguousOutcome) as e:
        return _emit(
            {"error": f"merge transaction failed: {e!r}"},
            json_mode=args.json,
            exit_code=EXIT_GUARD,
        )

    # Persist the COMPLETE state transition on the run-state side, so a
    # subsequent restart sees the controller in COMPLETE even if the
    # CLI process exits before a separate post-merge verify invocation.
    #
    # Per the explicit fail-closed COMPLETE contract:
    # * The merge record was already durably written above.
    # * If ``report_complete()`` raises (ControllerError / StateStoreError /
    #   any persistence failure), the CLI returns a NONZERO exit code.
    # * The merge record remains durable so a subsequent retry can
    #   recover by re-applying report_complete() idempotently.
    # * The CLI does NOT report success in this case.
    final_state = record.final_state
    complete_state_warning: Optional[str] = None
    try:
        controller = Controller(ctx, store)
        sm = controller.report_complete()
        final_state = sm.current_state
    except (ControllerError, StateStoreError, StateError, OSError) as e:
        # The merge record is durable on disk; the durable COMPLETE
        # transition failed. This is NOT a successful completion —
        # return a controlled nonzero result so the caller can retry.
        # The durable merge record preserves the recovery information
        # so a follow-up ``report_complete()`` retry can persist the
        # COMPLETE transition idempotently.
        complete_state_warning = (
            f"durable COMPLETE transition failed: {e!r}; merge record "
            f"at {merge_record_path} is durable; "
            f"merge_record_digest={rec_digest}; "
            "retry report_complete() to persist COMPLETE idempotently."
        )

    if complete_state_warning is not None:
        return _emit(
            {
                "error": complete_state_warning,
                "run_id": auth.run_id,
                "squash_merge_commit": record.squash_merge_commit,
                "merge_record_path": str(merge_record_path),
                "merge_record_digest": rec_digest,
                "recovery_required": True,
                "recovery_action": (
                    "merge_record is durable on disk; "
                    "COMPLETE transition must be re-applied "
                    "(e.g. retry cmd_post_merge_verify) before "
                    "downstream automation can treat the run as "
                    "completed"
                ),
                "complete_state_persisted": False,
            },
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )

    payload = {
        "run_id": auth.run_id,
        "state": final_state,
        "squash_merge_commit": record.squash_merge_commit,
        "merge_record_digest": rec_digest,
    }
    return _emit(
        payload,
        json_mode=args.json,
        exit_code=EXIT_OK,
    )


def cmd_post_merge_verify(args: argparse.Namespace) -> int:
    """Verify the post-merge state."""
    store = StateStore(args.state_root)
    rc = store.read_optional("run_context.json")
    if rc is None:
        return _emit(
            {"error": "no run context on file"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    ctx = RunContext.from_dict(rc)
    # Read the canonical merge-record artifact from the canonical evidence
    # root. Per C-24, the post-merge verify command must use the same
    # path as the merge transaction.
    evidence_root = _resolve_evidence_root(args, store)
    paths = _canonical_artifact_paths(evidence_root)
    from .artifacts import read_artifact
    try:
        record_result = read_artifact(paths["merge_record"])
    except FileNotFoundError:
        return _emit(
            {"error": f"no merge record at {paths['merge_record']}"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    except Exception as e:
        return _emit(
            {"error": f"merge record unreadable: {e!r}"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    try:
        record = MergeRecord.from_dict(record_result.payload)
    except (ValueError, TypeError, MergeAuthorizationMalformed) as e:
        return _emit(
            {"error": f"merge record malformed: {e!r}"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    if not record.local_main_equals_origin_main:
        return _emit(
            {"error": "local main does not match origin/main"},
            json_mode=args.json,
            exit_code=EXIT_GUARD,
        )
    if not record.aed_clean_post_merge:
        return _emit(
            {"error": "autodev working tree not clean"},
            json_mode=args.json,
            exit_code=EXIT_GUARD,
        )
    rc = store.read_optional("run_context.json")
    if rc is None:
        return _emit(
            {"error": "no run context on file"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    ctx = RunContext.from_dict(rc)
    controller = Controller(ctx, store)
    try:
        sm = controller.report_complete()
    except (ControllerError, StateStoreError, StateError, OSError) as e:
        # The merge record is durable on disk; the durable
        # COMPLETE transition failed. The recovery payload
        # identifies both the merge-record PATH and the verified
        # exact-file DIGEST (returned by ``read_artifact``, which
        # compares the body against its sidecar) so a follow-up
        # retry can locate the durable evidence without
        # inspecting the state store directly. Round-5 finding
        # PRRT_kwDOTtyQLc6XSGfP.
        return _emit(
            {
                "error": f"durable COMPLETE transition failed: {e!r}",
                "merge_record_path": str(paths["merge_record"]),
                "merge_record_sha256": record_result.digest,
                "recovery_required": True,
                "recovery_action": (
                    "merge_record is durable on disk; "
                    "COMPLETE transition must be re-applied "
                    "(e.g. retry cmd_post_merge_verify) before "
                    "downstream automation can treat the run as "
                    "completed"
                ),
            },
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    return _emit(
        {"run_id": args.run_id, "state": sm.current_state},
        json_mode=args.json,
        exit_code=EXIT_OK,
    )


def cmd_review_repair_round(args: argparse.Namespace) -> int:
    """Run one bounded round of the autonomous review/repair relay.

    The command is the operator-facing entry point for the relay.
    It reads the run context (and the persisted snapshot JSON
    when ``--snapshot-file`` is supplied), runs one round via
    ``RelayLoop.run_once``, and prints the ``RoundDecision``.

    The decision tells the caller what to do:
    - ``action == "launch_worker"``: the relay built a directive
      and persisted it. The caller (typically the supervisor)
      should launch the worker with the directive prompt.
    - ``action == "enter_qualifying_readiness"``: the head is
      clean. The caller should invoke the existing readiness gate.
    - ``action == "escalate_to_human"``: the relay found a P0
      finding or an escalation keyword. The run is now BLOCKED;
      the operator must inspect and direct.

    Exit codes follow the conventional mapping:
    - 0: round ran; the action field tells the caller what to do.
    - 2: invalid arguments.
    - 4: state error (e.g. controller in wrong state).
    - 5: internal error (EscalateToHuman, RelayError, ...).
    """
    store = StateStore(args.state_root)
    rc = store.read_optional("run_context.json")
    if rc is None:
        return _emit(
            {"error": "no run context on file"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    ctx = RunContext.from_dict(rc)
    # Resolve the snapshot: either from --snapshot-file or stdin.
    snapshot: Optional[dict] = None
    if getattr(args, "snapshot_file", None):
        try:
            snapshot = json.loads(args.snapshot_file.read_text())
        except (OSError, json.JSONDecodeError) as e:
            return _emit(
                {"error": f"snapshot file unreadable: {e!r}"},
                json_mode=args.json,
                exit_code=EXIT_INVARG,
            )
    elif getattr(args, "snapshot_stdin", False):
        try:
            snapshot = json.loads(sys.stdin.read())
        except json.JSONDecodeError as e:
            return _emit(
                {"error": f"stdin is not valid JSON: {e!r}"},
                json_mode=args.json,
                exit_code=EXIT_INVARG,
            )
    if snapshot is None:
        return _emit(
            {"error": "must supply --snapshot-file or --snapshot-stdin"},
            json_mode=args.json,
            exit_code=EXIT_INVARG,
        )
    head_sha = args.head_sha or ctx.current_authorized_head
    if not head_sha:
        return _emit(
            {"error": "no head_sha available; pass --head-sha or set context"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    evidence_root = args.evidence_root or str(ctx.evidence_root)
    directive_store = DirectiveStore(store, evidence_root)
    controller = Controller(ctx, store)
    repo = f"{ctx.repo_owner}/{ctx.repo_name}"
    # Read required_check_names from the supervisor config if
    # present; otherwise accept the caller-supplied list.
    # Fall back to ``ctx.required_ci_jobs`` (the persisted
    # ``RunContext`` policy) when the operator omits
    # ``--required-check-names`` so a head with no review
    # findings still drives pending/failing required checks
    # through the CI-finding collector rather than silently
    # calling the head clean.
    #
    # Distinguish the three cases the operator can express:
    #   - flag absent (``args.required_check_names is None``):
    #     fall back to the persisted ``ctx.required_ci_jobs``
    #     policy so older checks do not silently reappear.
    #   - flag present with explicit empty value (``""``): the
    #     operator is overriding the policy with an intentionally
    #     empty required-check set; do NOT resurrect
    #     ``ctx.required_ci_jobs`` — keep the override empty.
    #   - flag present with comma-separated names: use them
    #     verbatim, splitting on ``,`` and dropping empties.
    raw_required = getattr(args, "required_check_names", None)
    if raw_required is None:
        cli_required_check_names: Tuple[str, ...] = tuple(
            ctx.required_ci_jobs or ()
        )
    else:
        # Round-1064 P2: trim explicit names before use. The
        # raw ``--required-check-names "ci-A, ci-B"`` value
        # arrived with embedded whitespace, so without the
        # ``strip()`` the second name became ``" ci-B"`` and
        # the relay treated the actual ``ci-B`` check as
        # missing — launching unnecessary repair rounds.
        cli_required_check_names = tuple(
            name.strip() for name in raw_required.split(",") if name.strip()
        )
    required_check_names = cli_required_check_names
    max_rounds = int(args.max_rounds) if args.max_rounds else DEFAULT_MAX_ROUNDS
    loop = RelayLoop(
        context=ctx,
        store=store,
        directive_store=directive_store,
        controller=controller,
        required_check_names=required_check_names,
        max_rounds=max_rounds,
    )
    try:
        decision = loop.run_once(
            snapshot, head_sha=head_sha,
            repo=repo, pr_number=int(ctx.pr_number or 0),
            focused_thread_id=getattr(args, "focused_thread_id", None),
        )
    except EscalateToHuman as e:
        # Round-29 review: only ``EscalateToHuman`` carries
        # the protected-authority escalation signal
        # (EXIT_OK + structured ``escalate_to_human``
        # decision). Generic ``RelayError`` is an internal
        # failure that MUST NOT be misclassified as a
        # human-authority escalation; the supervisor needs
        # the non-zero exit to retry / recover.
        return _emit(
            {
                "action": "escalate_to_human",
                "escalate_reasons": [str(e)],
                "error": f"{type(e).__name__}: {e}",
            },
            json_mode=args.json,
            exit_code=EXIT_OK,
        )
    except RelayError as e:
        # Generic ``RelayError`` is an internal /
        # recoverable relay failure. Surface it with
        # EXIT_INTERNAL semantics so the supervisor's
        # retry / recover path can pick it up; do NOT
        # misclassify it as a protected-authority
        # escalation.
        return _emit(
            {
                "error": f"{type(e).__name__}: {e}",
                "action": "internal_error",
            },
            json_mode=args.json,
            exit_code=EXIT_INTERNAL,
        )
    payload = decision.to_dict()
    # When the action is "launch_worker", also render the worker
    # prompt so the caller can pass it to whatever worker
    # launcher they prefer. The prompt is large (it contains the
    # full directive JSON) — printing it twice is fine for
    # operator-facing CLI output.
    if decision.action == "launch_worker":
        try:
            payload["worker_prompt"] = build_worker_prompt(decision)
        except Exception as e:  # pragma: no cover - defensive
            payload["worker_prompt_error"] = repr(e)
    # Round-31: incomplete evidence → EXIT_OK + structured
    # decision so the supervisor's wiring routes to
    # ``recoverable_retry`` rather than ``no_action``.
    if decision.outcome == "incomplete_evidence":
        return _emit(payload, json_mode=args.json, exit_code=EXIT_OK)
    return _emit(payload, json_mode=args.json, exit_code=EXIT_OK)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Bounded, read-only installation health check.

    Delegates to :func:`autocoder_orchestration.doctor.doctor_main`,
    which is the single source of truth for the doctor behavior.
    The CLI wrapper is intentionally thin: it only adapts CLI
    args and forwards to ``doctor_main``.
    """
    from .doctor import doctor_main
    # ``args.json`` is the repo-canonical top-level --json flag;
    # ``args.doctor_json`` is the doctor-subcommand ergonomic
    # variant (``autocoder-orchestration doctor --json``). Both
    # are honored; either one enables JSON mode.
    json_mode = bool(getattr(args, "doctor_json", False)) or bool(args.json)
    return doctor_main(
        json_mode=json_mode,
        state_root_parent=args.state_root_parent,
        repo_root=args.repo_root,
    )


def cmd_review_repair_status(args: argparse.Namespace) -> int:
    """Print the relay's progress (round index, last decision, journal).

    The evidence root is resolved from the run context when
    available, falling back to the operator-supplied
    ``--evidence-root``. The literal ``/var/tmp/...`` is no
    longer the default; the status command MUST report the
    same location the relay writes to.
    """
    store = StateStore(args.state_root)
    # Resolve the evidence root through the same helper used
    # by cmd_review_repair_round so the status command and
    # the round command agree on the canonical location.
    evidence_root = _resolve_evidence_root(args, store)
    ds = DirectiveStore(store, str(evidence_root))
    last = ds.last_round_index()
    directive = ds.read_directive()
    payload = {
        "run_id": args.run_id,
        "evidence_root": str(evidence_root),
        "last_round_index": last,
        "directive_present": directive is not None,
        "transcript_count": len(ds.read_transcript()),
    }
    if directive is not None:
        payload["directive_head_sha"] = directive.head_sha
        payload["directive_summary"] = directive.summary
        payload["directive_id"] = directive.directive_id
    return _emit(payload, json_mode=args.json, exit_code=EXIT_OK)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="autocoder-orchestration")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-root", default="/var/tmp/autodev-evidence/state")
    common.add_argument("--run-id", required=True)

    sub.add_parser("status", parents=[common])
    sub.add_parser("gates", parents=[common])
    sub.add_parser("run", parents=[common])
    sub.add_parser("observe", parents=[common])
    sub.add_parser("post-merge-verify", parents=[common])

    init = sub.add_parser("initialize", parents=[common])
    init.add_argument("--owner", required=True)
    init.add_argument("--repo", required=True)
    init.add_argument("--local-checkout", required=True)
    init.add_argument("--base-branch", required=True)
    init.add_argument("--authorized-base-sha", required=True)
    init.add_argument("--feature-branch", required=True)
    init.add_argument("--taskspec-path", required=True)
    init.add_argument("--taskspec-sha256", required=True)
    init.add_argument("--required-ci-jobs", default="")
    init.add_argument("--impl-worker-command", default="")
    init.add_argument("--evidence-root", default="/var/tmp/autodev-evidence")

    # Round-591: audited required-CI-policy migration. THIS IS
    # the ONLY canonical path for changing the persisted
    # ``required_ci_jobs`` field. It enforces a fail-closed
    # ``--expected-old`` precondition before rewriting.
    migr = sub.add_parser("migrate-required-ci-jobs", parents=[common])
    migr.add_argument(
        "--expected-old", required=True,
        help=(
            "Comma-separated required_ci_jobs that MUST exactly "
            "match the persisted value before the migration is "
            "allowed to proceed. Refuses to commit silently."
        ),
    )
    migr.add_argument(
        "--new-required-ci-jobs", required=True,
        help=(
            "Comma-separated replacement required_ci_jobs. The "
            "full operator-policy set (seven-name) should be "
            "passed verbatim to avoid silent narrowing."
        ),
    )
    migr.add_argument(
        "--by", default="operator",
        help="Identity recorded in the migration audit trail.",
    )
    migr.add_argument(
        "--reason", default="",
        help="Free-form reason recorded in the migration audit trail.",
    )

    build = sub.add_parser("build-candidate", parents=[common])
    build.add_argument("--file-paths", default="")
    build.add_argument("--aed-paths", default="")
    build.add_argument("--aed-repo", default="")
    build.add_argument("--local-checkout", required=True)

    h = sub.add_parser("handoff-verifier", parents=[common])
    h.add_argument("--trusted-verifier-source-commit", required=True)

    av = sub.add_parser("apply-verifier-result", parents=[common])
    av.add_argument("--verifier-record", type=argparse.FileType("r"), required=True)

    ma = sub.add_parser("merge-authorize", parents=[common])
    ma.add_argument("--pr-number", type=int, required=True)
    ma.add_argument("--authorized-head", required=True)
    ma.add_argument("--author", required=True)
    ma.add_argument("--method", default="squash")
    ma.add_argument("--keep-branch", action="store_true")
    ma.add_argument("--notes", default="")

    m = sub.add_parser("merge", parents=[common])
    m.add_argument("--evidence-root", default="")

    rr = sub.add_parser("review-repair-round", parents=[common])
    rr.add_argument("--snapshot-file", type=Path, default=None,
                    help="Path to a JSON snapshot file (mutually exclusive with --snapshot-stdin)")
    rr.add_argument("--snapshot-stdin", action="store_true",
                    help="Read the snapshot JSON from stdin")
    rr.add_argument("--head-sha", default=None,
                    help="Override the head SHA from the run context")
    rr.add_argument("--evidence-root", default=None,
                    help="Override the evidence root from the run context")
    rr.add_argument("--required-check-names", default=None,
                    help=(
                        "Comma-separated CI check names that must pass "
                        "for the head to be clean. Default (omitted) "
                        "falls back to the persisted ctx.required_ci_jobs "
                        "policy; pass an empty string to override the "
                        "policy with an intentionally empty set."
                    ))
    rr.add_argument("--max-rounds", default=str(DEFAULT_MAX_ROUNDS),
                    help="Outer bound on relay rounds before BLOCKED")
    rr.add_argument("--focused-thread-id", default=None,
                    help="Round-45 C13: scope this round's directive to a SINGLE "
                         "targeted review thread (PRRT_kw... id). The directive "
                         "contains exactly one finding for that thread and bypasses "
                         "the max_findings cap. The supervisor uses this on the "
                         "durable-thread-drain path so the worker evaluates the "
                         "specific thread instead of the historical backlog.")

    rs = sub.add_parser("review-repair-status", parents=[common])
    rs.add_argument("--evidence-root", default=None)

    # Doctor: bounded, read-only installation health check.
    # Does NOT require --state-root / --run-id because it is a
    # stateless installation probe, not a run-state command.
    doc = sub.add_parser("doctor")
    doc.add_argument(
        "--state-root-parent",
        default="/var/tmp/autodev-evidence/state",
        help="Parent directory for the intended AutoDev state root. "
             "Doctor probes that the parent is creatable/writable/readable "
             "with a temporary file (no persistent artifact).",
    )
    doc.add_argument(
        "--repo-root",
        default=None,
        help="Optional explicit repository root. Defaults to the current "
             "directory's git toplevel.",
    )
    # The repo's canonical pattern is ``--json`` at the top level
    # (autocoder-orchestration --json doctor). The doctor also
    # accepts ``--json`` AFTER the subcommand for ergonomics; the
    # audit explicitly tests the post-subcommand form.
    doc.add_argument(
        "--json",
        dest="doctor_json",
        action="store_true",
        default=False,
        help="Emit JSON instead of human-readable output.",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            return cmd_status(args)
        if args.command == "gates":
            return cmd_gates(args)
        if args.command == "initialize":
            return cmd_initialize(args)
        if args.command == "migrate-required-ci-jobs":
            return cmd_migrate_required_ci_jobs(args)
        if args.command == "run":
            return cmd_run(args)
        if args.command == "observe":
            return cmd_observe(args)
        if args.command == "build-candidate":
            return cmd_build_candidate(args)
        if args.command == "handoff-verifier":
            return cmd_handoff_verifier(args)
        if args.command == "apply-verifier-result":
            return cmd_apply_verifier_result(args)
        if args.command == "merge-authorize":
            return cmd_merge_authorize(args)
        if args.command == "merge":
            return cmd_merge(args)
        if args.command == "post-merge-verify":
            return cmd_post_merge_verify(args)
        if args.command == "review-repair-round":
            return cmd_review_repair_round(args)
        if args.command == "review-repair-status":
            return cmd_review_repair_status(args)
        if args.command == "doctor":
            return cmd_doctor(args)
    except (ControllerError, StateStoreError, CandidateError, MergeError) as e:
        return _emit(
            {"error": f"{type(e).__name__}: {e}"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    return _emit(
        {"error": "unknown command"},
        json_mode=args.json,
        exit_code=EXIT_INVARG,
    )


if __name__ == "__main__":
    sys.exit(main())
