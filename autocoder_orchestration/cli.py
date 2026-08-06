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
            {"error": f"candidate build failed: {e}"},
            json_mode=args.json,
            exit_code=EXIT_GUARD,
        )
    store.write_atomic("candidate.json", candidate.to_dict())
    store.write_atomic("candidate.sha256", {"sha256": candidate.compute_sha256()})
    return _emit(
        {
            "run_id": args.run_id,
            "candidate_sha256": candidate.compute_sha256(),
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
    store.write_atomic("merge-authorization.json", auth_payload)
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
    auth_payload = store.read_optional("merge-authorization.json")
    if auth_payload is None:
        return _emit(
            {"error": "no merge authorization on file"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    auth = MergeAuthorization.from_dict(auth_payload)
    rc = store.read_optional("run_context.json")
    if rc is None:
        return _emit(
            {"error": "no run context on file"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    ctx = RunContext.from_dict(rc)

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
    except (subprocess.CalledProcessError, OSError, json.JSONDecodeError, RuntimeError) as exc:
        # Fail closed: incomplete thread evidence blocks the merge.
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
    except (subprocess.CalledProcessError, OSError, json.JSONDecodeError):
        pass

    # Fetch the latest CodeRabbit review state.
    live_review_state = {"latest_coderabbit_state": None}
    try:
        rev_proc = subprocess.run(
            ["gh", "api", "graphql", "-f",
             "query={{repository(owner:{owner},name:{repo}){{pullRequest(number:{pr}){{latestReviews(first:1){{nodes{{author{{login}} state}}}}}}}}}}".format(
                 owner=ctx.repo_owner, repo=ctx.repo_name, pr=auth.pr_number,
             )],
            capture_output=True, text=True, timeout=30,
        )
        if rev_proc.returncode == 0:
            rd = json.loads(rev_proc.stdout)
            nodes = (rd.get("data", {}).get("repository", {}).get("pullRequest", {})
                       .get("latestReviews", {}).get("nodes", []))
            if nodes:
                live_review_state["latest_coderabbit_state"] = nodes[0].get("state")
    except (subprocess.CalledProcessError, OSError, json.JSONDecodeError):
        pass

    # Working tree clean?
    wt_clean_proc = subprocess.run(
        ["git", "-C", str(ctx.local_checkout), "status", "--porcelain"],
        capture_output=True, text=True, timeout=10,
    )
    working_tree_clean = (wt_clean_proc.returncode == 0 and wt_clean_proc.stdout.strip() == "")

    # Resolve canonical artifact paths for the production transaction.
    # Per C-24, the three named roots must be independent. When the
    # operator does not pass --evidence-root, default it to a peer
    # directory under the same parent as run_state_root; the validator
    # refuses paths that resolve to the same directory.
    state_root_path = Path(str(store.state_root))
    if hasattr(args, "evidence_root") and args.evidence_root:
        evidence_root = Path(str(args.evidence_root))
    else:
        evidence_root = state_root_path.parent / "evidence"
    authorization_path = evidence_root / "authorization.json"
    candidate_path = evidence_root / "candidate.json"
    verifier_path = evidence_root / "verifier.json"
    merge_record_path = evidence_root / "merge-record.json"

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
    final_state = record.final_state
    state_warning: Optional[str] = None
    try:
        controller = Controller(ctx, store)
        sm = controller.report_complete()
        final_state = sm.current_state
    except (ControllerError, StateStoreError) as e:
        # The merge record is already durable. The state transition is
        # best-effort bookkeeping; surface as a warning but still
        # report success because the merge was completed.
        state_warning = f"state-machine transition failed: {e!r}"

    payload = {
        "run_id": auth.run_id,
        "state": final_state,
        "squash_merge_commit": record.squash_merge_commit,
        "merge_record_digest": rec_digest,
    }
    if state_warning is not None:
        payload["warning"] = state_warning
    return _emit(
        payload,
        json_mode=args.json,
        exit_code=EXIT_OK,
    )


def cmd_post_merge_verify(args: argparse.Namespace) -> int:
    """Verify the post-merge state."""
    store = StateStore(args.state_root)
    record_payload = store.read_optional("merge-record.json")
    if record_payload is None:
        return _emit(
            {"error": "no merge record on file"},
            json_mode=args.json,
            exit_code=EXIT_STATE,
        )
    record = MergeRecord.from_dict(record_payload)
    if not record.local_main_equals_origin_main:
        return _emit(
            {"error": "local main does not match origin/main"},
            json_mode=args.json,
            exit_code=EXIT_GUARD,
        )
    if not record.autodev_clean_post_merge:
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
    sm = controller.report_complete()
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
    m.add_argument("--pr-number", type=int, default=0)

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
