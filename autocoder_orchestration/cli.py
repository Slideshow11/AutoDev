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
    """Execute the guarded merge."""
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
    executor = MergeExecutor()
    record = executor.merge(
        auth,
        live_pr_payload={"merged": False, "state": "open", "head": {"sha": auth.authorized_head}},
        live_ci_state={},
        live_thread_inventory={"unresolved_current": 0, "unresolved_outdated": 0},
        live_review_state={},
        candidate_sha256_actual=auth.candidate_sha256,
        verifier_record_sha256_actual=auth.verifier_record_sha256,
        auto_repo_root=ctx.local_checkout,
    )
    store.write_atomic("merge-record.json", record.to_dict())
    controller = Controller(ctx, store)
    sm = controller.report_merged(record)
    return _emit(
        {
            "run_id": args.run_id,
            "state": sm.current_state,
            "squash_merge_commit": record.squash_merge_commit,
        },
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

    sub.add_parser("merge", parents=[common])

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
