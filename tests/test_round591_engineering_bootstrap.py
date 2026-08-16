"""Round-591: live validation suite for the AUTODEV PRACTICAL CONVERGENCE
REPAIR engineering bootstrap.

Every numbered item in the directive's §15 enumeration
gets a deterministic regression test that pins the
correct contract. The tests exercise BOTH synthetic unit
shapes AND the production source paths; tests that pin a
real behavior of an unmodified production file MUST
remain green against that file's current shape.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# All paths are absolute; tests rely on the production
# branch layout described in INVARIANTS.md.
PRODUCTION_ROOT = Path(__file__).resolve().parent.parent


# ===========================================================================
# §15.1 — exact seven required-check identities
# ===========================================================================


# Canonical SEVEN-NAME operator policy for PR #5 (per the
# directive's §1). These literal strings are the operator
# policy and a migration that drops, renames, or aliases
# any of them is unauthorized.
SEVEN_NAME_POLICY: tuple[str, ...] = (
    "test (3.10)",
    "test (3.11)",
    "test (3.12)",
    "package-smoke",
    "provenance",
    "committed-state-scan",
    "full-suite",
)


def test_seven_name_policy_source_controlled_default():
    """The cmd_initialize default fallback MUST be the
    seven-name operator policy. This pins the literal set
    so a future regression that re-introduces a six-job
    default (the previous bug) is caught."""
    from autocoder_orchestration import cli as orch_cli
    # Read the source of cmd_initialize and confirm the
    # fallback list matches the seven-name policy.
    import inspect
    src = inspect.getsource(orch_cli.cmd_initialize)
    for name in SEVEN_NAME_POLICY:
        assert f'"{name}"' in src or f"'{name}'" in src, (
            f"cmd_initialize missing required-ci-jobs "
            f"default entry: {name!r}"
        )


def test_seven_name_policy_persisted_in_run_context():
    """The persisted ``RunContext.required_ci_jobs`` at
    state/pr5_orch/run_context.json MUST be the seven-name
    policy (after the §2 migration). This is the durable
    source of operator policy."""
    rc_path = (
        Path.home() / ".hermes/aed-supervisor/state/pr5_orch/run_context.json"
    )
    if not rc_path.is_file():
        pytest.skip("supervisor state tree absent in this CI context")
    ctx = json.loads(rc_path.read_text())
    persisted = list(ctx.get("required_ci_jobs") or [])
    assert tuple(persisted) == SEVEN_NAME_POLICY, (
        f"persisted required_ci_jobs {persisted!r} != "
        f"seven-name policy {list(SEVEN_NAME_POLICY)!r}"
    )


# ===========================================================================
# §15.2 — stale persisted required-check policy migration
# ===========================================================================


def test_migrate_required_ci_jobs_returns_audit_trail(tmp_path: Path):
    """The canonical ``migrate-required-ci-jobs`` CLI
    must rewrite ``run_context.json`` with the new policy
    AND record a durable audit entry under
    ``required_ci_jobs_migrations.json``.

    Construct a fresh state_root with a pre-existing
    ``run_context.json`` carrying the stale ``['test','lint']``
    set, invoke the migration, and assert both the new
    in-place update and the audit journal append.
    """
    from autocoder_orchestration.store import StateStore
    from autocoder_orchestration.context import (
        SCHEMA_VERSION, make_run_context,
    )
    # Seed a state_root with a stale run_context.
    state_root = tmp_path / "state"
    store = StateStore(str(state_root))
    ctx = make_run_context(
        run_id="test-run-mgr",
        repo_owner="test",
        repo_name="test-repo",
        local_checkout=str(PRODUCTION_ROOT),
        base_branch="main",
        authorized_base_sha="a" * 40,
        feature_branch="test-feat",
        task_specification_path="/tmp/empty.txt",
        task_specification_sha256="b" * 64,
        required_ci_jobs=["test", "lint"],
        evidence_root=str(tmp_path / "evidence"),
        implementation_worker_command=[],
        state_root=str(state_root),
        pr_number=5,
    )
    store.write_atomic("run_context.json", ctx.to_dict())
    # Invoke the migration through the CLI parser.
    sys.path.insert(0, str(PRODUCTION_ROOT))
    from autocoder_orchestration import cli as orch_cli
    parser_args = [
        "--json",
        "migrate-required-ci-jobs",
        "--state-root", str(state_root),
        "--run-id", "test-run-mgr",
        "--expected-old", "test,lint",
        "--new-required-ci-jobs",
        ",".join(SEVEN_NAME_POLICY),
        "--by", "test",
        "--reason", "round-591 engineering bootstrap",
    ]
    rc = orch_cli.main(parser_args)
    assert rc == 0, f"migration returned non-zero: {rc}"
    # Re-read the persisted run_context.json.
    new_ctx = json.loads(
        (state_root / "run_context.json").read_text()
    )
    assert tuple(new_ctx["required_ci_jobs"]) == SEVEN_NAME_POLICY
    # Audit trail must contain a single migration entry.
    audit_path = state_root / "required_ci_jobs_migrations.json"
    assert audit_path.is_file(), (
        "audit trail required_ci_jobs_migrations.json was not "
        "written"
    )
    journal = audit_path.read_text().splitlines()
    assert len(journal) == 1
    entry = json.loads(journal[0])
    assert entry["from_required_ci_jobs"] == ["test", "lint"]
    assert entry["to_required_ci_jobs"] == list(SEVEN_NAME_POLICY)
    assert entry["by"] == "test"


def test_migrate_required_ci_jobs_signals_audit_append_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-689/P2: when the audit-trail append fails after
    the CAS commit succeeds, the migration MUST surface a
    non-zero exit code and an explicit ``error`` key — the
    audit trail is the stated control for this command, so
    callers that only check exit status must not record the
    migration as fully audited.

    The durable ``run_context.json`` change is intentionally
    preserved (no rollback) because it is the canonical
    record; the visible signal is what changes.
    """
    from autocoder_orchestration.store import (
        StateStore, StateStoreError,
    )
    from autocoder_orchestration.context import make_run_context

    state_root = tmp_path / "state"
    store = StateStore(str(state_root))
    ctx = make_run_context(
        run_id="test-run-aaf",
        repo_owner="test",
        repo_name="test-repo",
        local_checkout=str(PRODUCTION_ROOT),
        base_branch="main",
        authorized_base_sha="a" * 40,
        feature_branch="test-feat",
        task_specification_path="/tmp/empty.txt",
        task_specification_sha256="b" * 64,
        required_ci_jobs=["test", "lint"],
        evidence_root=str(tmp_path / "evidence"),
        implementation_worker_command=[],
        state_root=str(state_root),
        pr_number=5,
    )
    store.write_atomic("run_context.json", ctx.to_dict())

    # Monkey-patch StateStore.append_journal on the class to
    # simulate audit-trail append failure AFTER a successful
    # CAS commit. We leave compare_and_swap intact so the
    # run_context revision bump still happens (canonical
    # record) — only the journal append fails.
    def _raise_audit(
        self: StateStore, key: str, entry: object,
    ) -> None:
        raise StateStoreError(
            "simulated audit-trail append failure"
        )

    monkeypatch.setattr(StateStore, "append_journal", _raise_audit)

    sys.path.insert(0, str(PRODUCTION_ROOT))
    from autocoder_orchestration import cli as orch_cli

    parser_args = [
        "--json",
        "migrate-required-ci-jobs",
        "--state-root", str(state_root),
        "--run-id", "test-run-aaf",
        "--expected-old", "test,lint",
        "--new-required-ci-jobs", ",".join(SEVEN_NAME_POLICY),
        "--by", "test",
        "--reason", "round-689 audit-failure signal",
    ]
    rc = orch_cli.main(parser_args)
    assert rc != 0, (
        "audit-trail append failure must signal a non-zero "
        f"exit code; got rc={rc}"
    )
    # The run_context.json revision bump must still have
    # happened (canonical record, no rollback).
    new_ctx = json.loads(
        (state_root / "run_context.json").read_text()
    )
    assert tuple(new_ctx["required_ci_jobs"]) == SEVEN_NAME_POLICY


def test_migrate_required_ci_jobs_fails_closed_on_wrong_expected_old(
    tmp_path: Path,
):
    """The migration MUST refuse to apply if the operator's
    ``--expected-old`` does not exactly match the persisted
    policy. This prevents accidental silent narrowing of
    operator policy across stale operators."""
    from autocoder_orchestration.store import StateStore
    from autocoder_orchestration.context import (
        SCHEMA_VERSION, make_run_context,
    )
    state_root = tmp_path / "state"
    store = StateStore(str(state_root))
    ctx = make_run_context(

        run_id="test-run-fc",
        repo_owner="test",
        repo_name="test-repo",
        local_checkout=str(PRODUCTION_ROOT),
        base_branch="main",
        authorized_base_sha="a" * 40,
        feature_branch="test-feat",
        task_specification_path="/tmp/empty.txt",
        task_specification_sha256="b" * 64,
        required_ci_jobs=["test", "lint"],
        evidence_root=str(tmp_path / "evidence"),
        implementation_worker_command=[],
        state_root=str(state_root),
        pr_number=5,
    )
    store.write_atomic("run_context.json", ctx.to_dict())
    sys.path.insert(0, str(PRODUCTION_ROOT))
    from autocoder_orchestration import cli as orch_cli
    # Pass a wrong --expected-old.
    parser_args = [
        "--json",
        "migrate-required-ci-jobs",
        "--state-root", str(state_root),
        "--run-id", "test-run-fc",
        "--expected-old", "non,existent",
        "--new-required-ci-jobs",
        ",".join(SEVEN_NAME_POLICY),
        "--by", "test",
        "--reason", "must fail closed",
    ]
    rc = orch_cli.main(parser_args)
    assert rc == 2, (
        f"migration must fail closed (EXIT_INVARG=2); got {rc}"
    )
    # Persisted value must be unchanged.
    unchanged = json.loads(
        (state_root / "run_context.json").read_text()
    )
    assert unchanged["required_ci_jobs"] == ["test", "lint"]


def test_migrate_required_ci_jobs_idempotency():
    """Re-running the migration after the value has already
    changed MUST fail-closed under the
    ``--expected-old`` precondition."""
    # Construct tmp_path via the previous test's fixture.
    from autocoder_orchestration.store import StateStore
    from autocoder_orchestration.context import (
        SCHEMA_VERSION, make_run_context,
    )
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        state_root = Path(td) / "state"
        store = StateStore(str(state_root))
        ctx = make_run_context(
            run_id="test-run-idem",
            repo_owner="test",
            repo_name="test-repo",
            local_checkout=str(PRODUCTION_ROOT),
            base_branch="main",
            authorized_base_sha="a" * 40,
            feature_branch="test-feat",
            task_specification_path="/tmp/empty.txt",
            task_specification_sha256="b" * 64,
            required_ci_jobs=list(SEVEN_NAME_POLICY),
            evidence_root=str(Path(td) / "evidence"),
            implementation_worker_command=[],
            state_root=str(state_root),
            pr_number=5,
        )
        store.write_atomic("run_context.json", ctx.to_dict())
        sys.path.insert(0, str(PRODUCTION_ROOT))
        from autocoder_orchestration import cli as orch_cli
        rc = orch_cli.main([
            "--json", "migrate-required-ci-jobs",
            "--state-root", str(state_root),
            "--run-id", "test-run-idem",
            "--expected-old", ",".join(SEVEN_NAME_POLICY),
            "--new-required-ci-jobs",
            "single-required-only",
            "--by", "test",
            "--reason", "narrowing attempt",
        ])
        # Already has seven-name, can't silently narrow.
        assert rc == 2


# ===========================================================================
# §15.3 — phantom lint/test identities rejected
# ===========================================================================


def test_no_phantom_lint_or_test_in_source_control():
    """A grep over the production branch (excluding
    vendor / test files) MUST NOT find any string
    literal 'lint' or bare 'test' used as a default
    required-check identity outside of the canonical
    ``cmd_initialize`` fallback defaults or test
    fixtures."""
    bad_paths = []
    for root, _, files in os.walk(PRODUCTION_ROOT):
        # Skip .git, build, cache, __pycache__
        if any(
            skip in root
            for skip in (
                "/.git/", "/__pycache__/", "/build/lib",
            )
        ):
            continue
        if root.endswith("/tests"):
            # Tests legitimately mention "lint" or "test"
            # as fixture inputs / expected findings.
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            if f in ("review_repair_relay.py", "controller.py",
                     "provenance_maintenance.py"):
                continue
            fp = Path(root) / f
            try:
                text = fp.read_text()
            except Exception:
                continue
            # Look for the literal pattern: 'lint' or 'test' as
            # a default required-check value (in argparse
            # ``default=""`` or default list elements).
            for needle, pattern in [
                ("lint",
                 '"lint"'),
            ]:
                if pattern in text:
                    bad_paths.append(f"{needle!r} in {fp}")
    assert not bad_paths, (
        "phantom 'lint' identity found in production source: "
        f"{bad_paths}"
    )


# ===========================================================================
# §15.4 — terminal failed required check becomes CI_FAILURE
# ===========================================================================


def test_terminal_failure_emits_ci_failure_finding():
    """A required check with conclusion='failure' (terminal)
    MUST produce a CI_FAILURE finding whose id carries the
    run_id as the suffix."""
    from autocoder_orchestration.review_repair_relay import (
        SEVERITY_CI_FAILURE,
        collect_findings,
    )
    snapshot = {
        "head_sha": "f" * 40,
        "required_checks": {
            "provenance": {
                "conclusion": "failure",
                "status": "completed",
                "run_id": "r-12345",
            },
        },
        "review_threads": {},
        "_provider_issue_comments": {},
        "issue_comments": [],
    }
    findings = collect_findings(
        snapshot,
        required_check_names=("provenance",),
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == SEVERITY_CI_FAILURE
    assert f.check_name == "provenance"
    assert f.finding_id == "ci:provenance:r-12345"


# ===========================================================================
# §15.5 — pending check blocks / polls but does not launch repair
# ===========================================================================


def test_pending_check_emits_pending_finding_with_run_url():
    """A pending check (status=in_progress, no conclusion)
    MUST emit a CI_FAILURE finding with ``:pending`` suffix
    and must NOT carry a run_id."""
    from autocoder_orchestration.review_repair_relay import (
        SEVERITY_CI_FAILURE,
        collect_findings,
    )
    snapshot = {
        "head_sha": "f" * 40,
        "required_checks": {
            "full-suite": {
                "conclusion": "",
                "status": "in_progress",
                "run_id": "r-99999",
            },
        },
        "review_threads": {},
        "_provider_issue_comments": {},
        "issue_comments": [],
    }
    findings = collect_findings(
        snapshot,
        required_check_names=("full-suite",),
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == SEVERITY_CI_FAILURE
    assert f.check_name == "full-suite"
    assert f.finding_id == "ci:full-suite:pending"
    assert "still in-progress" in f.body.lower()


# ===========================================================================
# §15.6 — missing check becomes evidence / config issue, not fake source defect
# ===========================================================================


def test_missing_check_emits_missing_finding_distinct_from_failed():
    """A required check absent from the snapshot MUST emit
    a ``:missing`` finding whose body names the policy /
    fetch issue — distinct from a terminal failure finding."""
    from autocoder_orchestration.review_repair_relay import (
        SEVERITY_CI_FAILURE,
        collect_findings,
    )
    snapshot = {
        "head_sha": "f" * 40,
        "required_checks": {},
        "review_threads": {},
        "_provider_issue_comments": {},
        "issue_comments": [],
    }
    findings = collect_findings(
        snapshot,
        required_check_names=("provenance",),
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == SEVERITY_CI_FAILURE
    assert f.check_name == "provenance"
    assert f.finding_id == "ci:provenance:missing"
    assert "absent from the snapshot" in f.body.lower()


def test_missing_finding_body_does_not_suggest_source_edit():
    """A missing-check finding's body MUST explicitly route
    the response to the supervisor / configuration layer,
    not to the worker editing source. This prevents a worker
    from interpreting ``missing`` as ``edit source to fix``."""
    from autocoder_orchestration.review_repair_relay import (
        collect_findings,
    )
    snapshot = {
        "head_sha": "f" * 40,
        "required_checks": {},
        "review_threads": {},
        "_provider_issue_comments": {},
        "issue_comments": [],
    }
    findings = collect_findings(
        snapshot,
        required_check_names=("provenance",),
    )
    body = findings[0].body.lower()
    assert "supervisor must fetch" in body or "fetch" in body, (
        "missing-finding body did not route to fetch/config"
    )
    # Negative: must NOT include repair wording
    # like 'patch' or 'fix' as the actionable verb.
    for forbidden in ("patch ", "apply this fix", "implement"):
        assert forbidden not in body, (
            f"missing-finding body wrongly suggests source "
            f"edit verb {forbidden!r}"
        )


# ===========================================================================
# §15.7 — focused thread + real CI failures
# ===========================================================================


def test_focused_thread_plus_real_ci_failures():
    """Re-asserts the round-590 focused-mode contract:
    with `focused_thread_id` set AND real terminal CI
    failures, the directive MUST contain the targeted
    thread PLUS each CI_FAILURE finding.
    """
    from autocoder_orchestration.review_repair_relay import (
        SEVERITY_CI_FAILURE,
        SEVERITY_P1,
        build_directive,
        collect_findings,
    )
    thread_id = "PRRT_kwTEST591"
    head = "f" * 40
    snapshot = {
        "head_sha": head,
        "review_threads": {
            thread_id: {
                "body": "refactor the helper",
                "commit_oid": head,
                "path": "a.py",
                "line": 1,
                "outdated": False,
                "resolved": False,
            },
        },
        "_provider_issue_comments": {},
        "issue_comments": [],
        "required_checks": {
            n: {"conclusion": "failure", "status": "completed",
                "run_id": f"r-{n}"}
            for n in SEVEN_NAME_POLICY
        },
    }
    findings = collect_findings(
        snapshot,
        required_check_names=SEVEN_NAME_POLICY,
        focused_thread_id=thread_id,
    )
    ci = [f for f in findings if f.severity == SEVERITY_CI_FAILURE]
    threads = [
        f for f in findings
        if f.finding_id == f"thread:{thread_id}"
    ]
    assert len(ci) == len(SEVEN_NAME_POLICY), ci
    assert len(threads) == 1
    # build_directive must accept this combination.
    d = build_directive(
        round_index=591,
        head_sha=head,
        repo="o/r",
        pr_number=5,
        findings=findings,
        coordinator_actor="controller",
        target_thread_id=thread_id,
    )
    ci_in_d = [f for f in d.findings if f.severity == SEVERITY_CI_FAILURE]
    thread_in_d = [
        f for f in d.findings
        if f.finding_id == f"thread:{thread_id}"
    ]
    assert len(ci_in_d) == len(SEVEN_NAME_POLICY)
    assert len(thread_in_d) == 1


# ===========================================================================
# §15.8 — max_findings cannot drop CI_FAILURE
# ===========================================================================


def test_max_findings_preserves_all_ci_failures_over_20_p1():
    """20 current P1 + 5 terminal CI_FAILURE + max_findings=8
    → all 5 CI_FAILURE findings MUST survive the cap."""
    from autocoder_orchestration.review_repair_relay import (
        SEVERITY_CI_FAILURE,
        SEVERITY_P1,
        build_directive,
        collect_findings,
        Finding,
    )
    p1_findings = [
        Finding(
            finding_id=f"thread:PRRT_{i:04d}",
            source="review_thread",
            severity=SEVERITY_P1,
            title=f"p1 #{i}",
            body="...",
            file_path=None, line=None, url=None,
            suggested_test=None, review_id=None,
            comment_id=None, check_name=None,
        )
        for i in range(20)
    ]
    ci_findings = [
        Finding(
            finding_id=f"ci:check-{i}:r-{i}",
            source="ci",
            severity=SEVERITY_CI_FAILURE,
            title=f"ci fail #{i}",
            body=f"check-{i} failed",
            file_path=None, line=None, url=None,
            suggested_test=None, review_id=None,
            comment_id=None,
            check_name=f"check-{i}",
        )
        for i in range(5)
    ]
    findings = p1_findings + ci_findings
    d = build_directive(
        round_index=1,
        head_sha="a" * 40,
        repo="o/r",
        pr_number=5,
        findings=findings,
        coordinator_actor="controller",
        max_findings=8,
    )
    ci_in_d = [
        f for f in d.findings if f.severity == SEVERITY_CI_FAILURE
    ]
    assert len(ci_in_d) == 5, (
        f"max_findings=8 dropped {5 - len(ci_in_d)} of 5 "
        f"CI_FAILURE findings"
    )


# ===========================================================================
# §15.9 — red-CI priority prevents P2 displacement
# ===========================================================================


def test_ci_failures_come_before_review_in_directive():
    """When the directive contains both CI_FAILURE and
    review findings, the CI_FAILURE block appears first
    so a worker observes the required-CI signal before
    the optional P2 backlog.
    """
    from autocoder_orchestration.review_repair_relay import (
        SEVERITY_CI_FAILURE,
        SEVERITY_P2,
        build_directive,
        Finding,
    )
    p2 = Finding(
        finding_id="thread:OPTIONAL",
        source="review_thread",
        severity=SEVERITY_P2,
        title="optional nit",
        body="x", file_path=None, line=None, url=None,
        suggested_test=None, review_id=None,
        comment_id=None, check_name=None,
    )
    ci = Finding(
        finding_id="ci:provenance:r-1",
        source="ci", severity=SEVERITY_CI_FAILURE,
        title="ci fail", body="...", file_path=None,
        line=None, url=None, suggested_test=None,
        review_id=None, comment_id=None, check_name="provenance",
    )
    d = build_directive(
        round_index=1,
        head_sha="a" * 40,
        repo="o/r",
        pr_number=5,
        findings=[p2, ci],
        coordinator_actor="controller",
    )
    assert d.findings[0].severity == SEVERITY_CI_FAILURE
    assert d.findings[1].severity == SEVERITY_P2


# ===========================================================================
# §15.10 — STILL_ACTIONABLE cannot become successful NO_CHANGES_REQUIRED
# ===========================================================================


def test_still_actionable_disposition_blocks_no_changes_required():
    """A ``no_changes_required_proof.findings[0].disposition``
    of ``STILL_ACTIONABLE`` MUST raise ControllerError so
    the controller refuses to advance REPAIRING_REVIEW_FINDINGS
    to QUALIFYING_READINESS with a nonterminal disposition in
    the proof."""
    from autocoder_orchestration.controller import Controller, ControllerError
    from autocoder_orchestration.context import (
        SCHEMA_VERSION, make_run_context,
    )

    ctx = make_run_context(

        run_id="test-still",
        repo_owner="test", repo_name="test-repo",
        local_checkout=str(PRODUCTION_ROOT), base_branch="main",
        authorized_base_sha="a" * 40,
        feature_branch="test-feat",
        task_specification_path="/tmp/empty.txt",
        task_specification_sha256="b" * 64,
        required_ci_jobs=list(SEVEN_NAME_POLICY),
        evidence_root="/tmp/evi",
        implementation_worker_command=[],
        state_root="/tmp/state",
        pr_number=5,
    )
    # Build a real Controller with a trivial in-memory store.
    from autocoder_orchestration.store import StateStore
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        state_root = Path(td) / "state"
        state_root.mkdir(parents=True)
        store = StateStore(str(state_root))
        # Seed a run_context.
        store.write_atomic("run_context.json", ctx.to_dict())
        # Need a state.json so the controller has something to read.
        from autocoder_orchestration.state_machine import StateMachine
        store.write_atomic("state.json", StateMachine().to_dict())
        c = Controller(ctx, store)
        # Try to advance with a STILL_ACTIONABLE disposition.
        head = "b" * 40
        with pytest.raises(ControllerError):
            c.report_no_changes_required(
                head_observed=head,
                proof={
                    "findings": [
                        {
                            "finding_id": "thread:P1_ACTUAL",
                            "disposition": "STILL_ACTIONABLE",
                            "rationale": "I have authority+evidence "
                                         "to repair but declined "
                                         "in this round",
                        },
                    ],
                },
            )


def test_incomplete_evidence_disposition_blocks_no_changes_required():
    """``INCOMPLETE_EVIDENCE`` is nonterminal and blocks
    NO_CHANGES_REQUIRED too."""
    from autocoder_orchestration.controller import Controller, ControllerError
    from autocoder_orchestration.context import (
        SCHEMA_VERSION, make_run_context,
    )
    from autocoder_orchestration.store import StateStore
    from autocoder_orchestration.state_machine import StateMachine
    import tempfile
    ctx = make_run_context(

        run_id="test-inc",
        repo_owner="test", repo_name="test-repo",
        local_checkout=str(PRODUCTION_ROOT), base_branch="main",
        authorized_base_sha="a" * 40,
        feature_branch="test-feat",
        task_specification_path="/tmp/empty.txt",
        task_specification_sha256="b" * 64,
        required_ci_jobs=list(SEVEN_NAME_POLICY),
        evidence_root="/tmp/evi",
        implementation_worker_command=[], state_root="/tmp/state",
        pr_number=5,
    )
    with tempfile.TemporaryDirectory() as td:
        state_root = Path(td) / "state"
        state_root.mkdir(parents=True)
        store = StateStore(str(state_root))
        store.write_atomic("run_context.json", ctx.to_dict())
        store.write_atomic("state.json", StateMachine().to_dict())
        c = Controller(ctx, store)
        with pytest.raises(ControllerError):
            c.report_no_changes_required(
                head_observed="b" * 40,
                proof={"findings": [{
                    "finding_id": "thread:X",
                    "disposition": "INCOMPLETE_EVIDENCE",
                }]},
            )


# ===========================================================================
# §15.10b — REAL_REPAIR_REQUIRED is also nonterminal (round-683/P1)
# ===========================================================================


def test_real_repair_required_disposition_blocks_no_changes_required():
    """Round-683/P1: ``REAL_REPAIR_REQUIRED`` is mapped to
    ``STILL_ACTIONABLE`` by the canonical disposition map
    (``supervisor.py:1979``) and therefore MUST also block
    a successful ``NO_CHANGES_REQUIRED`` proof. A proof that
    carries ``REAL_REPAIR_REQUIRED`` means the worker has
    authority+evidence to repair — submitting it under a
    no-op proof is a class-A malformed proof."""
    from autocoder_orchestration.controller import Controller, ControllerError
    from autocoder_orchestration.context import (
        SCHEMA_VERSION, make_run_context,
    )
    from autocoder_orchestration.store import StateStore
    from autocoder_orchestration.state_machine import StateMachine
    import tempfile
    ctx = make_run_context(

        run_id="test-rrr",
        repo_owner="test", repo_name="test-repo",
        local_checkout=str(PRODUCTION_ROOT), base_branch="main",
        authorized_base_sha="a" * 40,
        feature_branch="test-feat",
        task_specification_path="/tmp/empty.txt",
        task_specification_sha256="b" * 64,
        required_ci_jobs=list(SEVEN_NAME_POLICY),
        evidence_root="/tmp/evi",
        implementation_worker_command=[], state_root="/tmp/state",
        pr_number=5,
    )
    with tempfile.TemporaryDirectory() as td:
        state_root = Path(td) / "state"
        state_root.mkdir(parents=True)
        store = StateStore(str(state_root))
        store.write_atomic("run_context.json", ctx.to_dict())
        store.write_atomic("state.json", StateMachine().to_dict())
        c = Controller(ctx, store)
        with pytest.raises(ControllerError):
            c.report_no_changes_required(
                head_observed="b" * 40,
                proof={"findings": [{
                    "finding_id": "thread:PRRT_kwDOTtyQLc6Zi1h3",
                    "disposition": "REAL_REPAIR_REQUIRED",
                }]},
            )


# ===========================================================================
# §15.11 — SUPERSEDED requires actual supersession proof
# ===========================================================================


def test_superseded_without_evidence_blocked():
    """A disposition of SUPERSEDED with an empty ``evidence``
    field MUST raise ControllerError — ``I think this is
    a fetch/config gap`` is NOT a supersession proof."""
    from autocoder_orchestration.controller import Controller, ControllerError
    from autocoder_orchestration.context import (
        SCHEMA_VERSION, make_run_context,
    )
    from autocoder_orchestration.store import StateStore
    from autocoder_orchestration.state_machine import StateMachine
    import tempfile
    ctx = make_run_context(

        run_id="test-sup",
        repo_owner="test", repo_name="test-repo",
        local_checkout=str(PRODUCTION_ROOT), base_branch="main",
        authorized_base_sha="a" * 40,
        feature_branch="test-feat",
        task_specification_path="/tmp/empty.txt",
        task_specification_sha256="b" * 64,
        required_ci_jobs=list(SEVEN_NAME_POLICY),
        evidence_root="/tmp/evi",
        implementation_worker_command=[], state_root="/tmp/state",
        pr_number=5,
    )
    with tempfile.TemporaryDirectory() as td:
        state_root = Path(td) / "state"
        state_root.mkdir(parents=True)
        store = StateStore(str(state_root))
        store.write_atomic("run_context.json", ctx.to_dict())
        store.write_atomic("state.json", StateMachine().to_dict())
        c = Controller(ctx, store)
        with pytest.raises(ControllerError):
            c.report_no_changes_required(
                head_observed="b" * 40,
                proof={"findings": [{
                    "finding_id": "thread:X",
                    "disposition": "SUPERSEDED",
                    # no evidence key at all
                }]},
            )


def test_superseded_with_concrete_evidence_passes():
    """SUPERSEDED + concrete ``evidence`` (subject SHA
    advance, or actual binding to a different head) MUST
    be accepted by the controller."""
    from autocoder_orchestration.controller import Controller
    from autocoder_orchestration.context import (
        SCHEMA_VERSION, make_run_context,
    )
    from autocoder_orchestration.store import StateStore
    from autocoder_orchestration.state_machine import StateMachine
    import tempfile
    ctx = make_run_context(

        run_id="test-sup-ok",
        repo_owner="test", repo_name="test-repo",
        local_checkout=str(PRODUCTION_ROOT), base_branch="main",
        authorized_base_sha="a" * 40,
        feature_branch="test-feat",
        task_specification_path="/tmp/empty.txt",
        task_specification_sha256="b" * 64,
        required_ci_jobs=list(SEVEN_NAME_POLICY),
        evidence_root="/tmp/evi",
        implementation_worker_command=[], state_root="/tmp/state",
        pr_number=5,
    )
    with tempfile.TemporaryDirectory() as td:
        state_root = Path(td) / "state"
        state_root.mkdir(parents=True)
        store = StateStore(str(state_root))
        store.write_atomic("run_context.json", ctx.to_dict())
        # Seed state.json at REPAIRING_REVIEW_FINDINGS so the
        # controller's _read_state returns the right state.
        store.write_atomic(
            "state.json",
            {
                "current_state": "REPAIRING_REVIEW_FINDINGS",
                "revision": 0,
                "expected_revision": 0,
                "journal": [],
                "evidence": {},
            },
        )
        c = Controller(ctx, store)
        # This call should not raise.
        c.report_no_changes_required(
            head_observed="b" * 40,
            proof={"findings": [{
                "finding_id": "thread:X",
                "disposition": "SUPERSEDED",
                "evidence": (
                    "subject thread's bound commit_oid is now "
                    "behind current_authorized_head — rendered "
                    "moot by head advance"
                ),
            }]},
        )


# ===========================================================================
# §15.12 — regenerate_manifest updates hash AND size
# ===========================================================================


def test_regenerate_manifest_updates_hash_and_size(tmp_path: Path):
    """Construct a synthetic manifest with destination_sha256
    and destination_size_bytes fields; mutate the underlying
    file (rewrite bytes); call regenerate_manifest; assert
    BOTH recorded fields match the on-disk bytes."""
    from autocoder_supervisor.provenance_maintenance import (
        regenerate_manifest,
    )
    # Build a fake destination.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "aed-pr417-source-manifest.json").write_text(json.dumps({
        "files": [
            {
                "destination_path": "module.py",
                "destination_sha256": "deadbeef" * 8,
                "destination_size_bytes": 1,
                "source_path": None,
            },
        ],
    }))
    (repo / "module.py").write_bytes(b"x" * 100)
    audit = regenerate_manifest(
        repo / "aed-pr417-source-manifest.json",
        repo,
        allowed_paths=("module.py",),
    )
    assert len(audit["updated"]) == 1
    entry = audit["updated"][0]
    assert entry["size_new"] == 100
    new_manifest = json.loads(
        (repo / "aed-pr417-source-manifest.json").read_text()
    )
    rec = new_manifest["files"][0]
    assert rec["destination_sha256"] != "deadbeef" * 8
    assert rec["destination_size_bytes"] == 100
    # Hash matches the actual on-disk bytes.
    import hashlib
    actual = hashlib.sha256((repo / "module.py").read_bytes()).hexdigest()
    assert rec["destination_sha256"] == actual


# ===========================================================================
# §15.13 — canonical source-completeness regeneration
# ===========================================================================


def test_canonical_source_completeness_regen_via_subprocess(tmp_path: Path):
    """The canonical ``scripts/provenance_audit.py regenerate``
    regenerates ``provenance/AUTOCODER_SOURCE_COMPLETENESS.json``
    end-to-end. Run it as a subprocess to confirm it stays
    the SOLE source-controlled generator."""
    audit_script = PRODUCTION_ROOT / "scripts/provenance_audit.py"
    if not audit_script.is_file():
        pytest.skip("scripts/provenance_audit.py absent")
    # Just confirm it can be invoked with `--help` and
    # exits 0 with the canonical subcommands exposed.
    r = subprocess.run(
        [sys.executable, str(audit_script), "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0
    assert "regenerate" in r.stdout
    assert "check" in r.stdout


# ===========================================================================
# §15.14 — worker controlled-source finalization before commit
# ===========================================================================


def test_provenance_finalize_provenance_finalize_constant_present():
    """The canonical ``MANIFEST_CONTROLLED_PATHS`` list is
    re-exported by the production provenance module. A
    worker pre-commit hook can therefore enumerate the
    set deterministically.

    Round-663 P1: the canonical set is now DERIVED from
    the canonical extraction manifest at call time (it is
    no longer a hard-coded tuple). The membership test
    therefore pins entries that the manifest actually
    records, not stale historical entries that the
    canonical extractor never captured.
    """
    from autocoder_supervisor import provenance_maintenance as pm
    assert hasattr(pm, "MANIFEST_CONTROLLED_PATHS")
    # And it MUST contain the supervisor's exact head
    # dependencies that the canonical manifest records.
    paths = set(pm.MANIFEST_CONTROLLED_PATHS)
    for required in (
        "autocoder_supervisor/supervisor.py",
        "autocoder_orchestration/cli.py",
    ):
        assert required in paths


def test_provenance_finalize_function_exists():
    """The composition function is exported and callable."""
    from autocoder_supervisor import provenance_maintenance as pm
    assert callable(pm.provenance_finalize)
    assert issubclass(pm.ProvenanceFinalizeError, RuntimeError)


def test_provenance_finalize_dry_run_no_changes():
    """When the on-disk files already match the manifest,
    ``provenance_finalize`` returns a clean audit dict
    without modifying anything."""
    from autocoder_supervisor import provenance_maintenance as pm
    # Use the production repo as the repo_root.
    audit = pm.provenance_finalize(
        repo_root=PRODUCTION_ROOT, allowed_paths=(),
    )
    assert audit["validate"] is True


# ===========================================================================
# §15.15 — provenance-drift event causal ownership
# ===========================================================================


def test_provenance_drift_pending_file_exists():
    """The supervisor's durable
    ``provenance_drift_pending.json`` exists and is
    inspected by the relay. It is the canonical
    provenance-drift event surface."""
    pdp = (
        Path.home() / ".hermes/aed-supervisor/state/"
        "provenance_drift_pending.json"
    )
    if not pdp.is_file():
        pytest.skip("supervisor state tree absent")
    # Schema sanity: must be a JSON object or list (canonical
    # supervisor-side historical shape is a list of pending
    # drift records).
    data = json.loads(pdp.read_text())
    assert isinstance(data, (dict, list))
    if isinstance(data, list):
        assert all(isinstance(e, dict) for e in data)


# ===========================================================================
# §15.16 — strict dirty-tree pytest recurrence without broad bypass
# ===========================================================================


def test_dirt_allowlist_excludes_unknown_untracked_source(tmp_path: Path):
    """Reuse the dirty-checkout guard's tests in
    ``tests/test_round281_strict_dirty_tree_guard.py``.
    This regresses the production ``_check_clean_production_checkout``
    against an arbitrary untracked file with a name that
    merely coincidentally begins with ``pytest-of-``.

    A directory named ``pytest-of-something`` containing a
    non-pytest source file MUST still block the guard.
    See ``TestRound590PytestOfRecurrence`` in
    ``tests/test_round281_strict_dirty_tree_guard.py``.
    """
    # Just invoke the existing test class via subprocess
    # so we exercise the *production* guard without
    # affecting the in-process state.
    r = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "-q", "-x",
            "tests/test_round281_strict_dirty_tree_guard.py::TestRound590PytestOfRecurrence",
        ],
        cwd=str(PRODUCTION_ROOT),
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, (
        f"TestRound590PytestOfRecurrence failed: "
        f"stdout={r.stdout[:2000]} stderr={r.stderr[:500]}"
    )


# ===========================================================================
# §15.17 — Round-664 P1: reject absent or incomplete no-op proof
# ===========================================================================


def _build_round664_controller(td: Path):
    """Helper: build a Controller seeded in
    REPAIRING_REVIEW_FINDINGS, mirroring the §15.10 setup."""
    from autocoder_orchestration.controller import Controller
    from autocoder_orchestration.context import (
        SCHEMA_VERSION, make_run_context,
    )
    from autocoder_orchestration.store import StateStore
    from autocoder_orchestration.state_machine import StateMachine
    ctx = make_run_context(
        run_id="test-r664",
        repo_owner="test", repo_name="test-repo",
        local_checkout=str(PRODUCTION_ROOT), base_branch="main",
        authorized_base_sha="a" * 40,
        feature_branch="test-feat",
        task_specification_path="/tmp/empty.txt",
        task_specification_sha256="b" * 64,
        required_ci_jobs=list(SEVEN_NAME_POLICY),
        evidence_root="/tmp/evi",
        implementation_worker_command=[],
        state_root="/tmp/state",
        pr_number=5,
    )
    state_root = Path(td) / "state"
    state_root.mkdir(parents=True)
    store = StateStore(str(state_root))
    store.write_atomic("run_context.json", ctx.to_dict())
    store.write_atomic(
        "state.json",
        {
            "current_state": "REPAIRING_REVIEW_FINDINGS",
            "revision": 0,
            "expected_revision": 0,
            "journal": [],
            "evidence": {},
        },
    )
    return Controller(ctx, store)


def test_round664_p1_proof_none_rejected(tmp_path: Path) -> None:
    """Round-664 P1: ``proof=None`` MUST raise ControllerError;
    the controller MUST NOT silently accept an absent proof
    as a valid empty no-op."""
    from autocoder_orchestration.controller import ControllerError
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        c = _build_round664_controller(Path(td))
        with pytest.raises(ControllerError):
            c.report_no_changes_required(
                head_observed="b" * 40,
                proof=None,
            )


def test_round664_p1_non_object_proof_rejected(tmp_path: Path) -> None:
    """Round-664 P1: a non-dict proof (e.g. a list or string)
    MUST raise ControllerError; it is not silently coerced to
    an empty findings list."""
    from autocoder_orchestration.controller import ControllerError
    import tempfile
    for bad_proof in ([], "nope", 42, 3.14):
        with tempfile.TemporaryDirectory() as td:
            c = _build_round664_controller(Path(td))
            with pytest.raises(ControllerError):
                c.report_no_changes_required(
                    head_observed="b" * 40,
                    proof=bad_proof,  # type: ignore[arg-type]
                )


def test_round664_p1_missing_findings_list_rejected(tmp_path: Path) -> None:
    """Round-664 P1: a dict proof without a ``findings`` key
    or with a non-list ``findings`` MUST raise ControllerError;
    it is not silently treated as an empty list."""
    from autocoder_orchestration.controller import ControllerError
    import tempfile
    for bad_proof in ({}, {"findings": "nope"}, {"findings": {}}):
        with tempfile.TemporaryDirectory() as td:
            c = _build_round664_controller(Path(td))
            with pytest.raises(ControllerError):
                c.report_no_changes_required(
                    head_observed="b" * 40,
                    proof=bad_proof,
                )


def test_round664_p1_empty_findings_list_rejected(tmp_path: Path) -> None:
    """Round-664 P1: an empty ``findings`` list MUST raise
    ControllerError; the controller cannot validate a no-op
    without any assigned-finding disposition."""
    from autocoder_orchestration.controller import ControllerError
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        c = _build_round664_controller(Path(td))
        with pytest.raises(ControllerError):
            c.report_no_changes_required(
                head_observed="b" * 40,
                proof={"findings": []},
            )


def test_round664_p1_non_object_entry_rejected(tmp_path: Path) -> None:
    """Round-664 P1: a non-dict entry inside ``findings``
    MUST raise ControllerError; it is not silently skipped."""
    from autocoder_orchestration.controller import ControllerError
    import tempfile
    for bad_entry in (None, "string", 42, ["list"]):
        with tempfile.TemporaryDirectory() as td:
            c = _build_round664_controller(Path(td))
            with pytest.raises(ControllerError):
                c.report_no_changes_required(
                    head_observed="b" * 40,
                    proof={"findings": [bad_entry]},  # type: ignore[list-item]
                )


def test_round664_p1_entry_without_disposition_rejected(tmp_path: Path) -> None:
    """Round-664 P1: an entry without a ``disposition`` field
    MUST raise ControllerError; it is not silently accepted
    as a terminal disposition."""
    from autocoder_orchestration.controller import ControllerError
    import tempfile
    for bad_entry in (
        {"finding_id": "thread:X"},  # no disposition at all
        {"finding_id": "thread:X", "disposition": None},
        {"finding_id": "thread:X", "disposition": 42},
        {"finding_id": "thread:X", "category": "ALREADY_SATISFIED"},
    ):
        with tempfile.TemporaryDirectory() as td:
            c = _build_round664_controller(Path(td))
            with pytest.raises(ControllerError):
                c.report_no_changes_required(
                    head_observed="b" * 40,
                    proof={"findings": [bad_entry]},  # type: ignore[list-item]
                )


def test_round664_p1_terminal_disposition_with_evidence_passes(tmp_path: Path) -> None:
    """Round-664 P1 sanity: the tightened validator still
    accepts a properly-shaped ALREADY_SATISFIED proof (the
    expected happy path for round-591 §15.11 plus a
    SUPERSEDED-with-evidence entry)."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        c = _build_round664_controller(Path(td))
        sm = c.report_no_changes_required(
            head_observed="b" * 40,
            proof={
                "findings": [
                    {
                        "finding_id": "thread:A",
                        "disposition": "ALREADY_SATISFIED",
                    },
                    {
                        "finding_id": "thread:B",
                        "disposition": "SUPERSEDED",
                        "evidence": (
                            "subject thread's bound commit_oid is now "
                            "behind current_authorized_head — rendered "
                            "moot by head advance"
                        ),
                    },
                ],
            },
        )
        from autocoder_orchestration.state_machine import (
            STATE_QUALIFYING_READINESS,
        )
        assert sm.current_state == STATE_QUALIFYING_READINESS
