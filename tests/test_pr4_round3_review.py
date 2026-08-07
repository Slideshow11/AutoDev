"""Regression tests for defect 3.x / round-3 review findings.

These tests cover the production defects identified by CodeRabbit
on 8cd967b (PR #4 round-3 review). Each test exercises a single
defect end-to-end through the production code path.

Coverage:

* Authorization canonical-write failure injection --
  ``Controller.authorize_merge`` owns the complete safe
  transaction. A canonical-write failure leaves the run at
  AWAITING_MERGE_AUTHORIZATION so a retry can succeed.

* Authorization retry/recovery -- a retry of
  ``cmd_merge_authorize`` after a prior canonical-write failure
  succeeds.

* CLI does not duplicate canonical write -- the canonical
  authorization.json write happens only inside
  ``Controller.authorize_merge``, never in the CLI module.

* Cross-process merge lock with contender PID actually obtained
  in the subprocess, not interpolated in the parent.

* Verifier record measured evidence lineage -- every field is
  populated from a helper return value, not a hardcoded literal.

Note: the StateError post-merge COMPLETE recovery regression
that round-3 documentation mentioned is implemented in
``tests/test_pr4_round4_review.py::StateErrorRecoveryTests``.
That is where the corresponding coverage lives.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autocoder_orchestration import cli as cli_module
from autocoder_orchestration.artifacts import (
    ArtifactError,
    read_artifact,
    write_artifact,
)
from autocoder_orchestration.canonical_paths import canonical_paths
from autocoder_orchestration.context import SCHEMA_VERSION as RC_SCHEMA
from autocoder_orchestration.context import RunContext
from autocoder_orchestration.merge_authorization import (
    MergeAuthorization,
)
from autocoder_orchestration.store import StateStore


def _make_run_context(evidence_root, run_state_root):
    return RunContext(
        schema_version=RC_SCHEMA,
        run_id="test-pr4-round3",
        created_at="2026-08-07T15:00:00Z",
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        local_checkout=str(run_state_root / "repo"),
        base_branch="main",
        authorized_base_sha="a6bd5f63c3e4bad2b91661ffb75a59d2afd5f38d",
        feature_branch="fix/test",
        pr_number=4,
        current_authorized_head="2a8e4e9c1f3a4b5d6e7f8091a2b3c4d5e40ffe0d",
        task_specification_path=str(evidence_root / "task.txt"),
        task_specification_sha256="a" * 64,
        required_ci_jobs=("test (3.10)", "test (3.11)", "test (3.12)",
                          "package-smoke", "provenance",
                          "committed-state-scan"),
        reviewer_policy="approve-only",
        quiet_window_seconds=180,
        implementation_worker_command=("echo", "worker"),
        verifier_command=None,
        verifier_handoff_policy="strict",
        permitted_mutations=("candidate.json", "verifier.json",
                             "merge-record.json"),
        human_only_actions=("merge",),
        evidence_root=str(evidence_root),
        state_root=str(run_state_root),
        next_wave_policy="none",
    )


def _seed_state(store, target_state):
    from autocoder_orchestration.state_machine import StateMachine
    sm = StateMachine(current_state=target_state)
    store.write_atomic("state.json", sm.to_dict())


def _make_args(ctx, run_state_root, evidence_root):
    from argparse import Namespace
    return Namespace(
        run_id=ctx.run_id,
        state_root=str(run_state_root),
        evidence_root=str(evidence_root),
        json=True,
        pr_number=ctx.pr_number,
        authorized_head=ctx.current_authorized_head,
        method="squash",
        keep_branch=False,
        author="Slideshow11",
        notes="",
    )


class AuthorizationCanonicalWriteFailureTests(unittest.TestCase):
    """A canonical ``authorization.json`` write failure MUST leave
    the run at ``AWAITING_MERGE_AUTHORIZATION`` and return
    controlled EXIT_STATE. A retry of ``cmd_merge_authorize``
    after a prior canonical-write failure MUST succeed and
    commit MERGE_AUTHORIZED with a fresh canonical artifact."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="aed-r3-auth-"))
        self.run_state_root = self.tmpdir / "state"
        self.evidence_root = self.tmpdir / "evidence"
        self.run_state_root.mkdir(parents=True)
        self.evidence_root.mkdir(parents=True)
        self.ctx = _make_run_context(self.evidence_root, self.run_state_root)
        self.store = StateStore(str(self.run_state_root))
        self.store.write_atomic("run_context.json", self.ctx.to_dict())
        from autocoder_orchestration.state_machine import (
            STATE_AWAITING_MERGE_AUTHORIZATION,
        )
        _seed_state(self.store, STATE_AWAITING_MERGE_AUTHORIZATION)
        self.paths = canonical_paths(self.evidence_root)
        for p in self.paths.values():
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._seed_valid_artifacts()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed_valid_artifacts(self):
        cand = {
            "schema_version": "autocoder.candidate.v1",
            "run_id": self.ctx.run_id,
            "repo": f"{self.ctx.repo_owner}/{self.ctx.repo_name}",
            "pr_number": self.ctx.pr_number,
            "exact_head": self.ctx.current_authorized_head,
            "base_sha": self.ctx.authorized_base_sha,
            "base_branch": self.ctx.base_branch,
            "task_specification_sha256": self.ctx.task_specification_sha256,
            "readiness_certificate_id": "cert-001",
            "readiness_certificate_sha256": "f" * 64,
            "readiness_overall_passed": True,
            "ci_inventory": [], "review_inventory": [],
            "thread_inventory": {"unresolved_current": 0,
                                 "unresolved_outdated": 0},
            "strict_observation_log_hash": "",
            "process_identity": {}, "lock_release_evidence": {},
            "controller_state_revision": 1,
            "controller_state_path": "state.json",
            "input_hashes": {}, "source_files": {},
            "aed_source_files": {},
            "created_at": "2026-08-07T15:00:00Z",
        }
        write_artifact(self.paths["candidate"], cand)
        write_artifact(self.paths["verifier"], {
            "schema_version": "autocoder.verifier_record.v1",
            "candidate_sha256": "c" * 64,
            "verdict": "VERIFIED",
            "defects": [],
        })

    def test_canonical_write_failure_leaves_run_recoverable(self):
        """Inject a write failure into ``Controller._write_canonical``
        for the authorization artifact. The run MUST stay at
        AWAITING_MERGE_AUTHORIZATION, the canonical
        authorization.json MUST NOT exist, and the state-root
        copy MUST NOT exist either (no partial commit).
        """
        # Patch ``_write_canonical`` so the authorization write
        # fails before any state transition commits.
        from autocoder_orchestration.controller import Controller

        def boom(self, kind, payload):
            if kind == "authorization":
                raise ArtifactError(
                    "simulated canonical authorization write failure",
                )
            # Allow verifier and candidate canonical writes.
            from autocoder_orchestration.canonical_paths import (
                canonical_paths as _cp,
            )
            from autocoder_orchestration.artifacts import (
                write_artifact as _write,
            )
            from pathlib import Path as _P
            target = _cp(_P(self.context.evidence_root))[kind]
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _write(target, payload)
            return "fake-digest"

        args = _make_args(self.ctx, self.run_state_root, self.evidence_root)
        with mock.patch.object(
            Controller, "_write_canonical", boom,
        ):
            exit_code = cli_module.cmd_merge_authorize(args)

        # The CLI MUST return a controlled nonzero exit code.
        self.assertEqual(
            exit_code, cli_module.EXIT_STATE,
            f"canonical-write failure must return EXIT_STATE; got {exit_code}",
        )
        # The canonical authorization.json MUST NOT exist.
        self.assertFalse(
            self.paths["authorization"].exists(),
            "canonical authorization.json was written despite the "
            "canonical-write failure; the run cannot recover",
        )
        # The state-root merge-authorization.json MUST NOT exist
        # either; no partial commit to either location.
        state_auth = self.run_state_root / "merge-authorization.json"
        self.assertFalse(
            state_auth.exists(),
            "state-root merge-authorization.json was written despite "
            "the canonical-write failure; the run cannot recover",
        )
        # The state machine MUST still be at
        # AWAITING_MERGE_AUTHORIZATION, NOT MERGE_AUTHORIZED.
        sm_payload = self.store.read_optional("state.json")
        self.assertEqual(
            sm_payload["current_state"],
            "AWAITING_MERGE_AUTHORIZATION",
            "state machine transitioned despite canonical-write "
            "failure; retry cannot succeed",
        )

    def test_retry_after_canonical_failure_succeeds(self):
        """A retry of ``cmd_merge_authorize`` after a prior
        canonical-write failure MUST succeed. The original
        canonical-write failure is transient (simulated); the
        retry completes the safe transaction successfully and
        the run moves to MERGE_AUTHORIZED.
        """
        from autocoder_orchestration.controller import Controller

        # First attempt: simulate failure on the FIRST
        # authorization write only.
        original = Controller._write_canonical
        attempt = {"n": 0}

        def flaky(self, kind, payload):
            attempt["n"] += 1
            if kind == "authorization" and attempt["n"] == 1:
                raise ArtifactError(
                    "simulated transient authorization write failure",
                )
            return original(self, kind, payload)

        args = _make_args(self.ctx, self.run_state_root, self.evidence_root)
        with mock.patch.object(Controller, "_write_canonical", flaky):
            exit_code = cli_module.cmd_merge_authorize(args)
        self.assertEqual(exit_code, cli_module.EXIT_STATE)

        # Retry with the original (unflaky) write -- must succeed.
        with mock.patch.object(Controller, "_write_canonical", original):
            exit_code = cli_module.cmd_merge_authorize(args)
        self.assertEqual(
            exit_code, cli_module.EXIT_OK,
            f"retry after canonical-write failure must succeed; got {exit_code}",
        )

        # Both writes should now exist.
        self.assertTrue(self.paths["authorization"].exists())
        self.assertTrue(
            (self.run_state_root / "merge-authorization.json").exists(),
        )
        # State MUST be MERGE_AUTHORIZED.
        sm_payload = self.store.read_optional("state.json")
        self.assertEqual(sm_payload["current_state"], "MERGE_AUTHORIZED")


class CanonicalAuthorizationIntegrityTests(unittest.TestCase):
    """The Controller writes the canonical authorization. The CLI
    does NOT duplicate the canonical write. The canonical digest
    read back from the file MUST equal the digest computed by
    the Controller."""

    def test_cli_does_not_duplicate_canonical_write(self):
        """``cmd_merge_authorize`` MUST NOT call
        ``write_artifact`` on the canonical authorization path
        after the Controller returns; the Controller owns the
        complete safe transaction."""
        from autocoder_orchestration.controller import Controller
        # Track every canonical-authorization write (Controller
        # internal). The Controller's ``_write_canonical`` is the
        # ONLY sanctioned producer for canonical artifacts. The
        # CLI MUST NOT call ``write_artifact`` on the
        # authorization path; this spy counts CLI-level calls.
        canonical_writes: list = []
        cli_write_artifact = cli_module.write_artifact

        def cli_spy(*args, **kwargs):
            if args and str(args[0]).endswith("authorization.json"):
                canonical_writes.append(("cli", str(args[0])))
            return cli_write_artifact(*args, **kwargs)

        # Set up minimal state.
        tmpdir = Path(tempfile.mkdtemp())
        try:
            run_state_root = tmpdir / "state"
            evidence_root = tmpdir / "evidence"
            run_state_root.mkdir(parents=True)
            evidence_root.mkdir(parents=True)
            ctx = _make_run_context(evidence_root, run_state_root)
            store = StateStore(str(run_state_root))
            store.write_atomic("run_context.json", ctx.to_dict())
            from autocoder_orchestration.state_machine import (
                STATE_AWAITING_MERGE_AUTHORIZATION,
            )
            _seed_state(store, STATE_AWAITING_MERGE_AUTHORIZATION)
            paths = canonical_paths(evidence_root)
            for p in paths.values():
                p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_artifact(paths["candidate"], {
                "schema_version": "autocoder.candidate.v1",
                "exact_head": ctx.current_authorized_head,
                "run_id": ctx.run_id,
                "repo": f"{ctx.repo_owner}/{ctx.repo_name}",
                "pr_number": ctx.pr_number,
                "base_sha": ctx.authorized_base_sha,
                "base_branch": ctx.base_branch,
                "task_specification_sha256": ctx.task_specification_sha256,
                "readiness_certificate_id": "cert-001",
                "readiness_certificate_sha256": "f" * 64,
                "readiness_overall_passed": True,
                "ci_inventory": [], "review_inventory": [],
                "thread_inventory": {"unresolved_current": 0,
                                     "unresolved_outdated": 0},
                "strict_observation_log_hash": "",
                "process_identity": {}, "lock_release_evidence": {},
                "controller_state_revision": 1,
                "controller_state_path": "state.json",
                "input_hashes": {}, "source_files": {},
                "aed_source_files": {},
                "created_at": "2026-08-07T15:00:00Z",
            })
            write_artifact(paths["verifier"], {
                "schema_version": "autocoder.verifier_record.v1",
                "candidate_sha256": "c" * 64,
                "verdict": "VERIFIED",
                "defects": [],
            })
            args = _make_args(ctx, run_state_root, evidence_root)
            with mock.patch.object(
                cli_module, "write_artifact", side_effect=cli_spy,
            ):
                exit_code = cli_module.cmd_merge_authorize(args)
            self.assertEqual(exit_code, cli_module.EXIT_OK)
            # The CLI MUST NOT call write_artifact directly on the
            # authorization path. The Controller writes the
            # canonical artifact via its own module-level
            # ``_write_artifact`` reference, which the CLI spy
            # cannot see; that is the desired isolation.
            self.assertEqual(
                len(canonical_writes), 0,
                f"CLI must not call write_artifact on the "
                f"authorization path; saw {canonical_writes!r}",
            )
            # But the canonical authorization.json MUST exist
            # because the Controller wrote it.
            self.assertTrue(
                paths["authorization"].exists(),
                "canonical authorization.json was not written; "
                "the Controller's _write_canonical must produce it",
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class ProductionCLIFilterRegressionTests(unittest.TestCase):
    """Regression for the production CLI's CodeRabbit identity
    filter. The verifier-level tests live in
    ``tests/test_pr4_round4_review.py::LiveCodeRabbitGateTests``
    which loads ``scripts/independent_verifier_v2.py`` directly
    and exercises ``_inspect_coderabbit`` with mocked GraphQL
    responses.

    This class retains a smaller CLI-filter regression that
    predates the verifier-level test infrastructure.
    """

    def test_production_cli_filter_rejects_empty_changes_requested(self):
        """The production CLI filter returns ``None`` for an
        empty / CHANGES_REQUESTED payload, so the CodeRabbit
        guard fails closed. This test covers the production CLI
        only -- the verifier-level coverage is in
        ``LiveCodeRabbitGateTests``."""
        from autocoder_orchestration.cli import _filter_coderabbit_review_state
        payload = {
            "data": {"repository": {"pullRequest": {
                "reviewDecision": "CHANGES_REQUESTED",
                "latestReviews": {"nodes": []},
            }}},
        }
        self.assertIsNone(_filter_coderabbit_review_state(payload))


class CrossProcessLockPidIdentityTests(unittest.TestCase):
    """The contender subprocess must report its own PID, not the
    parent test runner's PID. The current implementation must
    import os inside the contender script so os.getpid()
    evaluates inside the subprocess."""

    def test_contender_pid_obtained_in_subprocess(self):
        """Spawn the holder and contender subprocesses. The
        contender's reported PID MUST equal the contender
        subprocess's actual PID (os.getpid() inside the
        subprocess), not the parent test runner's PID. This
        regression will fail if the parent test runner's
        ``os.getpid()`` is interpolated into the script text
        instead of being evaluated inside the child.
        """
        repo_root = str(Path(__file__).resolve().parent.parent)
        tmp = Path(tempfile.mkdtemp(prefix="aed-r3-pid-"))
        try:
            holder_script = (
                "import os, sys, time\n"
                f"sys.path.insert(0, {repo_root!r})\n"
                "from autocoder_orchestration.merge_lock import merge_lock\n"
                f"with merge_lock({str(tmp)!r}):\n"
                "    sys.stdout.write(f'HOLDER_PID={os.getpid()}\\n')\n"
                "    sys.stdout.flush()\n"
                "    time.sleep(3.0)\n"
                "sys.stdout.write('HOLDER_RELEASED\\n')\n"
            )
            contender_script = (
                "import os, sys\n"
                f"sys.path.insert(0, {repo_root!r})\n"
                "from autocoder_orchestration.merge_lock import (\n"
                "    LockUnavailable, merge_lock,\n"
                ")\n"
                "try:\n"
                f"    with merge_lock({str(tmp)!r}):\n"
                "        sys.stdout.write('CONTENDER_ACQUIRED\\n')\n"
                "except LockUnavailable as exc:\n"
                "    sys.stdout.write(\n"
                "        'CONTENDER_BLOCKED '\n"
                "        f'holder_pid={exc.holder_pid} '\n"
                "        f'contender_pid={os.getpid()}\\n'\n"
                "    )\n"
            )
            holder = subprocess.Popen(
                [sys.executable, "-c", holder_script],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            time = __import__("time")
            time.sleep(0.5)
            # Now spawn the contender and remember its PID via
            # a separate, deterministic Python script that
            # prints its own PID before invoking the contender
            # script.
            wrapper_script = (
                "import os, runpy, sys\n"
                f"sys.path.insert(0, {repo_root!r})\n"
                f"with open({str(tmp / 'contender_inner.py')!r}) as f:\n"
                "    inner = f.read()\n"
                "sys.stdout.write(f'OUTER_PID={os.getpid()}\\n')\n"
                "exec(inner, {'__name__': '__main__'})\n"
            )
            inner_path = tmp / "contender_inner.py"
            inner_path.write_text(contender_script)
            contender = subprocess.Popen(
                [sys.executable, "-c", wrapper_script],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            contender_stdout, contender_stderr = contender.communicate(timeout=10)
            holder_stdout, holder_stderr = holder.communicate(timeout=10)
            self.assertEqual(
                holder.returncode, 0,
                f"holder failed: rc={holder.returncode}; "
                f"stdout={holder_stdout!r} stderr={holder_stderr!r}",
            )
            self.assertEqual(
                contender.returncode, 0,
                f"contender failed: rc={contender.returncode}; "
                f"stdout={contender_stdout!r} stderr={contender_stderr!r}",
            )

            # Parse OUTER_PID, contender_pid, and holder_pid.
            outer_pid = None
            contender_pid_reported = None
            holder_pid_reported = None
            for line in contender_stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("OUTER_PID="):
                    outer_pid = int(stripped.split("=", 1)[1])
                elif stripped.startswith("CONTENDER_BLOCKED"):
                    parts = stripped.split()
                    for p in parts:
                        if p.startswith("contender_pid="):
                            contender_pid_reported = int(p.split("=", 1)[1])
                        elif p.startswith("holder_pid="):
                            holder_pid_reported = int(p.split("=", 1)[1])
            for line in holder_stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("HOLDER_PID="):
                    holder_pid_actual = int(stripped.split("=", 1)[1])
                    break
            else:
                holder_pid_actual = None

            # The OUTER pid (the wrapper script) IS the contender
            # subprocess's actual PID; the inner contender_pid
            # MUST equal it because both are os.getpid() calls
            # from the same process.
            self.assertIsNotNone(
                outer_pid,
                f"OUTER_PID not found in contender stdout: {contender_stdout!r}",
            )
            self.assertIsNotNone(
                contender_pid_reported,
                f"contender_pid not found in contender stdout: {contender_stdout!r}",
            )
            self.assertEqual(
                contender_pid_reported, outer_pid,
                f"contender_pid_reported={contender_pid_reported} != "
                f"outer_pid={outer_pid}; the contender reported the "
                f"wrong PID -- the test fixture may have used the "
                f"parent's os.getpid() interpolation",
            )
            # The holder_pid reported by LockUnavailable MUST equal
            # the actual holder subprocess's PID.
            self.assertIsNotNone(
                holder_pid_reported,
                f"holder_pid_reported missing: {contender_stdout!r}",
            )
            self.assertIsNotNone(
                holder_pid_actual,
                f"holder_pid_actual missing: {holder_stdout!r}",
            )
            self.assertEqual(
                holder_pid_reported, holder_pid_actual,
                f"holder_pid_reported={holder_pid_reported} != "
                f"holder_pid_actual={holder_pid_actual}; the "
                f"contender observed a different holder PID than "
                f"the actual holder process",
            )
            # The contender's PID MUST differ from the holder's
            # PID; if they were the same the fixture would be
            # meaningless.
            self.assertNotEqual(
                contender_pid_reported, holder_pid_actual,
                f"contender and holder share PID "
                f"{contender_pid_reported}; fixture is meaningless",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class VerifierObservedEvidenceLineageTests(unittest.TestCase):
    """The independent verifier's verifier_record MUST be
    populated from helper return values, NOT hardcoded literals.
    Inspect the production source to prove no literal 'true'
    value is assigned directly without an assertion backing it."""

    def test_verifier_record_uses_observed_values(self):
        """Static check: ``scripts/independent_verifier_v2.py``
        must NOT assign hardcoded True values to the
        ``checks`` map. Every ``checks`` entry must be derived
        from a helper return value or a measured observation."""
        verifier_path = (
            Path(__file__).resolve().parent.parent
            / "scripts" / "independent_verifier_v2.py"
        )
        text = verifier_path.read_text()
        # Find the checks-dict construction. The 'checks' map
        # entries must compare observed fields (aed_measured ==
        # aed_expected, sw.span_seconds >= 180.0, etc.) rather
        # than assign literal True.
        checks_section = text[text.index('"checks": {'):]
        # Look for ":" followed by "True" or "False" with no
        # comparator -- the buggy pattern from the round-2
        # commit. Allow "==" or ">=" comparisons.
        import re
        literal_assignment = re.findall(
            r'"[a-zA-Z_][a-zA-Z_0-9 ]*":\s*(?:True|False)(?!\s*=)',
            checks_section,
        )
        self.assertEqual(
            literal_assignment, [],
            f"verifier record 'checks' map must be derived from "
            f"observed values, not hardcoded True/False literals; "
            f"found literal assignments: {literal_assignment!r}",
        )
        # Also: aed_actual_sha256 must NOT be assigned
        # args.aed_expected_sha directly.
        self.assertNotIn(
            "aed_actual_sha256 = args.aed_expected_sha",
            text,
            "aed_actual_sha256 must be the MEASURED sha, not the "
            "expected input copied under an 'actual' field name",
        )


if __name__ == "__main__":
    unittest.main()