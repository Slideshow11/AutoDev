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
import hashlib
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

from .context import RunContext, make_run_context, generate_run_id
from .canonical_paths import canonical_paths as _canonical_artifact_paths
from .state_machine import (
    StateMachine,
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
from .store import StateStore, StateStoreError, ProcessIdentity, current_process_identity
from .controller import Controller, ControllerError
from .readiness import ReadinessEngine, ReadinessDecision, ReadinessCertificate
from .observer import ObservationLog, Observation
from .candidate import (
    Candidate,
    CandidateBuilder,
    CandidateError,
    build_candidate_from_observations,
)
from .verifier_handoff import (
    VerifierHandoff,
    VerifierRoleGuard,
    write_handoff,
    read_handoff,
)
from .merge_authorization import (
    MergeAuthorization,
    MergeExecutor,
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


EXIT_OK = 0
EXIT_INVARG = 2
EXIT_GUARD = 3
EXIT_STATE = 4
EXIT_INTERNAL = 5

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
        author_login = node.get("author", {}).get("login") or ""
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
    cursor = "null"
    has_next = True
    page_count = 0
    matched_state: Optional[str] = None
    while has_next:
        page_count += 1
        if page_count > _CODERABBIT_MAX_PAGES:
            break
        proc = subprocess.run(
            [
                gh_executable, "api", "graphql",
                "-f", f"query={query}",
                "-F", f"owner={owner}",
                "-F", f"name={name}",
                "-F", f"pr={pr_number}",
                "-F", f"cursor={cursor}",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return matched_state
        try:
            doc = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return matched_state
        page = (
            doc.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("latestReviews", {})
        )
        if not page:
            return matched_state
        # Filter for CodeRabbit matches on this page.
        match = _filter_coderabbit_review_state(doc)
        if match is not None and matched_state is None:
            matched_state = match
        page_info = page.get("pageInfo", {})
        has_next = bool(page_info.get("hasNextPage"))
        cursor = page_info.get("endCursor") or "null"
    return matched_state


def _resolve_evidence_root(args: argparse.Namespace, store) -> Path:
    """Resolve the canonical evidence root for this CLI invocation.

    Per C-24 the three named roots must be independent. The persisted
    ``RunContext.evidence_root`` is the canonical source — the CLI
    reads it from the run-state store when available. The operator
    may pass ``--evidence-root`` to override the persisted path;
    conflicting overrides are rejected so a non-canonical evidence
    root can never be silently used.
    """
    persisted: Optional[str] = None
    try:
        rc = store.read_optional("run_context.json")
        if rc is not None:
            persisted = rc.get("evidence_root") if isinstance(rc, dict) else None
    except Exception:
        persisted = None

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
    required_ci_jobs = (
        args.required_ci_jobs.split(",") if args.required_ci_jobs else
        ("test (3.10)", "test (3.11)", "test (3.12)",
         "package-smoke", "provenance", "committed-state-scan")
    )
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
    # Persist the canonical candidate artifact to the evidence root
    # via the canonical-paths helper. The guarded merge transaction
    # consumes ``canonical_paths(evidence_root).candidate``; writing
    # only to state_root would break that contract.
    from .artifacts import write_artifact
    rc_for_evidence = store.read_optional("run_context.json")
    ctx_for_evidence = (
        RunContext.from_dict(rc_for_evidence) if rc_for_evidence else None
    )
    if ctx_for_evidence is None:
        return _emit(
            {"error": "no run context on file"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    paths = _canonical_artifact_paths(Path(ctx_for_evidence.evidence_root))
    paths["candidate"].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_artifact(paths["candidate"], candidate.to_dict())
    # Keep the state-root copy as a secondary observable for audit,
    # never as the merge-input source. The canonical evidence root is
    # authoritative; the merge transaction must never read state-root.
    store.write_atomic("candidate.json", candidate.to_dict())
    store.write_atomic("candidate.sha256", {"sha256": candidate.compute_sha256()})
    return _emit(
        {
            "run_id": args.run_id,
            "candidate_sha256": candidate.compute_sha256(),
            "canonical_candidate_path": str(paths["candidate"]),
            "canonical_candidate_sha256": write_artifact(
                paths["candidate"], candidate.to_dict()
            ).digest,
        },
        json_mode=args.json,
        exit_code=EXIT_OK,
    )


def cmd_handoff_verifier(args: argparse.Namespace) -> int:
    """Write a verifier handoff record for the current run."""
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
    """Record a human merge authorization."""
    store = StateStore(args.state_root)
    rc = store.read_optional("run_context.json")
    if rc is None:
        return _emit(
            {"error": "no run context on file"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    ctx = RunContext.from_dict(rc)
    cand_payload = store.read_optional("candidate.json")
    if cand_payload is None:
        return _emit(
            {"error": "no candidate on file"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    cand = Candidate.from_dict(cand_payload)
    if cand.exact_head != ctx.current_authorized_head:
        return _emit(
            {"error": "candidate head does not match current authorized head"},
            json_mode=args.json,
            exit_code=EXIT_GUARD,
        )
    vr = store.read_optional("verifier-record.json")
    if vr is None:
        return _emit(
            {"error": "no verifier record on file"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    vr_sha = hashlib.sha256(
        json.dumps(vr, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    auth = MergeAuthorization(
        schema_version="autocoder.merge_authorization.v1",
        run_id=args.run_id,
        repo=f"{ctx.repo_owner}/{ctx.repo_name}",
        pr_number=args.pr_number,
        authorized_head=args.authorized_head,
        candidate_sha256=cand.compute_sha256(),
        verifier_record_sha256=vr_sha,
        merge_method=args.method,
        delete_branch=not args.keep_branch,
        require_match_head_commit=True,
        authorization_timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        author=args.author,
        next_wave_authorization=None,
        notes=args.notes,
    )
    auth_payload = auth.to_dict()
    auth_payload["_sha256"] = auth.compute_sha256()
    # Persist the canonical authorization artifact under the evidence
    # root (one canonical filename across initialize/authorize/merge/post-merge).
    evidence_root = _resolve_evidence_root(args, store)
    paths = _canonical_artifact_paths(evidence_root)
    paths["authorization"].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    from .artifacts import write_artifact
    write_artifact(paths["authorization"], auth_payload)
    controller = Controller(ctx, store)
    sm = controller.authorize_merge(auth)
    return _emit(
        {"run_id": args.run_id, "state": sm.current_state},
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
    timeout, reconciles post-merge state, writes the merge record
    through the canonical artifact writer, and transitions the state
    machine to ``COMPLETE``.

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
        cursor = "null"
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
            thread_proc = subprocess.run(
                ["gh", "api", "graphql",
                 "-f", f"query={q}",
                 "-F", f"owner={ctx.repo_owner}",
                 "-F", f"name={ctx.repo_name}",
                 "-F", f"pr={auth.pr_number}",
                 "-F", f"cursor={cursor}"],
                capture_output=True, text=True, timeout=30,
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
            all_nodes.extend(page.get("nodes", []))
            page_info = page.get("pageInfo", {})
            has_next = bool(page_info.get("hasNextPage"))
            cursor = page_info.get("endCursor") or "null"

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
            passing = {c["name"] for c in checks if c.get("state") == "SUCCESS"}
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

    # Working tree clean? Re-measure after the branch switch and fast-forward
    # so a post-merge dirty tree is reported as post-merge dirty.
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
    except (ControllerError, StateStoreError, OSError) as e:
        # The merge record is durable on disk; the durable COMPLETE
        # transition failed. This is NOT a successful completion —
        # return a controlled nonzero result so the caller can retry.
        # The durable merge record preserves the recovery information
        # so a follow-up ``report_complete()`` retry can persist the
        # COMPLETE transition idempotently.
        complete_state_warning = (
            f"durable COMPLETE transition failed: {e!r}; merge record "
            f"at {rec_digest} is durable; retry report_complete() to "
            "persist COMPLETE idempotently."
        )

    if complete_state_warning is not None:
        return _emit(
            {
                "error": complete_state_warning,
                "run_id": auth.run_id,
                "squash_merge_commit": record.squash_merge_commit,
                "merge_record_digest": rec_digest,
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
    except (ControllerError, StateStoreError, OSError) as e:
        return _emit(
            {"error": f"durable COMPLETE transition failed: {e!r}"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    return _emit(
        {"run_id": args.run_id, "state": sm.current_state},
        json_mode=args.json,
        exit_code=EXIT_OK,
    )


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

    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            return cmd_status(args)
        if args.command == "gates":
            return cmd_gates(args)
        if args.command == "initialize":
            return cmd_initialize(args)
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
