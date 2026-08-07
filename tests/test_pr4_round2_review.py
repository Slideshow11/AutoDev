"""Regression tests for defect 3.x / round-2 review findings.

These tests cover the production defects identified by CodeRabbit
on 7e855aa (PR #4 round-2 review). Each test exercises a single
defect end-to-end through the production code path; no test
asserts a hardcoded literal alone.

Coverage:
* Evidence-root fail-closed behavior in
  ``_resolve_evidence_root`` -- I/O / parse / StateStore failures
  must propagate, not silently substitute a fallback root.
* Verdict gating in ``cmd_merge_authorize`` -- a non-VERIFIED
  verifier verdict cannot become merge authorization evidence,
  even if the sidecar digest is valid.
* Malformed candidate schema -- a correctly signed malformed
  candidate causes a controlled state failure, never an
  uncaught traceback.
* Cross-process merge lock holder PID -- the contender observes
  the actual holder's PID, never its own.
* Authorization preconditions are validated BEFORE the canonical
  authorization.json is written; a rejected authorization leaves
  no artifact on the canonical evidence path.
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
from autocoder_orchestration.artifacts import read_artifact, write_artifact
from autocoder_orchestration.canonical_paths import canonical_paths
from autocoder_orchestration.candidate import Candidate
from autocoder_orchestration.context import SCHEMA_VERSION as RC_SCHEMA
from autocoder_orchestration.context import RunContext
from autocoder_orchestration.merge_authorization import MergeAuthorization
from autocoder_orchestration.merge_lock import (
    LockUnavailable,
    merge_lock,
)
from autocoder_orchestration.store import StateStore, StateStoreError


def _make_run_context(evidence_root, run_state_root):
    return RunContext(
        schema_version=RC_SCHEMA,
        run_id="test-pr4-round2",
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


def _make_args(ctx, run_state_root, evidence_root, **overrides):
    base = {
        "run_id": ctx.run_id,
        "state_root": str(run_state_root),
        "evidence_root": str(evidence_root),
        "json": True,
        "pr_number": ctx.pr_number,
        "authorized_head": ctx.current_authorized_head,
        "method": "squash",
        "keep_branch": False,
        "author": "Slideshow11",
        "notes": "",
    }
    base.update(overrides)
    return type("Args", (), base)()


class EvidenceRootFailClosedTests(unittest.TestCase):
    """``_resolve_evidence_root`` must fail closed when the persisted
    run context cannot be read, parsed, or trusted."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="aed-ev-fc-"))
        self.run_state_root = self.tmpdir / "state"
        self.evidence_root = self.tmpdir / "evidence"
        self.run_state_root.mkdir(parents=True)
        self.evidence_root.mkdir(parents=True)
        self.ctx = _make_run_context(self.evidence_root, self.run_state_root)
        self.store = StateStore(str(self.run_state_root))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_persisted_evidence_root_is_used(self):
        """The persisted ``RunContext.evidence_root`` is the
        canonical source. No fallback is permitted when the run
        context exists."""
        self.store.write_atomic("run_context.json", self.ctx.to_dict())
        args = _make_args(self.ctx, self.run_state_root, self.evidence_root)
        resolved = cli_module._resolve_evidence_root(args, self.store)
        self.assertEqual(resolved, Path(str(self.evidence_root)))

    def test_io_failure_propagates(self):
        """If reading the run context raises StateStoreError (a
        corruption failure during StateStore validation), the
        call MUST propagate rather than silently substitute a
        fallback evidence root."""
        # The StateStore's strict reader rejects files with
        # world- or group-readable permission bits (mode 0600
        # required). Writing the run context with mode 0o644
        # produces a StateCorruption -> StateStoreError when
        # the file exists and is read. The new
        # ``_resolve_evidence_root`` MUST propagate that error
        # rather than silently substitute
        # ``state_root.parent / "evidence"``.
        ctx_path = self.run_state_root / "run_context.json"
        ctx_path.write_text(self.ctx.to_dict().__class__.__name__)
        ctx_path.chmod(0o644)  # group/world readable; rejected by StateStore.
        args = _make_args(self.ctx, self.run_state_root, self.evidence_root)
        with self.assertRaises(StateStoreError):
            cli_module._resolve_evidence_root(args, self.store)

    def test_malformed_run_context_propagates(self):
        """If the run context exists but contains invalid JSON,
        the call MUST propagate as a StateStoreError, not silently
        fall back to ``state_root.parent / "evidence"``."""
        ctx_path = self.run_state_root / "run_context.json"
        ctx_path.parent.mkdir(parents=True, exist_ok=True)
        ctx_path.write_text("{ this is not valid json")
        ctx_path.chmod(0o600)
        args = _make_args(self.ctx, self.run_state_root, self.evidence_root)
        with self.assertRaises((StateStoreError, json.JSONDecodeError)):
            cli_module._resolve_evidence_root(args, self.store)

    def test_conflicting_override_fails(self):
        """A conflicting ``--evidence-root`` override raises
        ``MergeInputsCollide`` even when the persisted root
        can be read."""
        self.store.write_atomic("run_context.json", self.ctx.to_dict())
        # Note: ``_make_args`` already sets ``evidence_root`` to
        # ``str(self.evidence_root)``; override via a fresh build
        # so we can supply a different value.
        args = _make_args(
            self.ctx, self.run_state_root, self.evidence_root,
        )
        # Manually set the override.
        args.evidence_root = "/tmp/some-other-evidence-root"
        from autocoder_orchestration.merge_authorization import MergeInputsCollide
        with self.assertRaises(MergeInputsCollide):
            cli_module._resolve_evidence_root(args, self.store)


class VerdictGatingTests(unittest.TestCase):
    """``cmd_merge_authorize`` MUST reject any verifier record whose
    ``verdict`` is not exactly ``VERIFIED``, even when the sidecar
    digest is valid."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="aed-vg-"))
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

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _seed_candidate(self):
        cand = Candidate.from_dict({
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
            "ci_inventory": [],
            "review_inventory": [],
            "thread_inventory": {"unresolved_current": 0,
                                 "unresolved_outdated": 0},
            "strict_observation_log_hash": "",
            "process_identity": {},
            "lock_release_evidence": {},
            "controller_state_revision": 1,
            "controller_state_path": "state.json",
            "input_hashes": {},
            "source_files": {},
            "aed_source_files": {},
            "created_at": "2026-08-07T15:00:00Z",
        })
        write_artifact(self.paths["candidate"], cand.to_dict())

    def test_failed_verdict_blocks_authorization(self):
        """A canonical verifier.json with verdict='FAILED' MUST NOT
        produce a valid merge authorization."""
        self._seed_candidate()
        write_artifact(
            self.paths["verifier"], {
                "schema_version": "autocoder.verifier_record.v1",
                "candidate_sha256": "c" * 64,
                "verdict": "FAILED",
                "defects": ["synthetic"],
            },
        )
        args = _make_args(self.ctx, self.run_state_root, self.evidence_root)
        # cmd_merge_authorize catches controller errors and emits
        # them via _emit; a verdict gate failure must not raise.
        exit_code = cli_module.cmd_merge_authorize(args)
        self.assertNotEqual(exit_code, 0)
        self.assertFalse(
            self.paths["authorization"].exists(),
            "authorization.json must not be written for a "
            "FAILED-verdict verifier record",
        )

    def test_missing_verdict_blocks_authorization(self):
        """A canonical verifier.json without a verdict field MUST
        NOT produce a valid merge authorization."""
        self._seed_candidate()
        write_artifact(
            self.paths["verifier"], {
                "schema_version": "autocoder.verifier_record.v1",
                "candidate_sha256": "c" * 64,
                "defects": [],
            },
        )
        args = _make_args(self.ctx, self.run_state_root, self.evidence_root)
        exit_code = cli_module.cmd_merge_authorize(args)
        self.assertNotEqual(exit_code, 0)
        self.assertFalse(self.paths["authorization"].exists())

    def test_verdict_failed_tag_blocks_authorization(self):
        """``Controller.verifier_failed`` writes a tag marking the
        record as a failed attempt. Authorization MUST reject any
        record carrying the ``_verdict_failed`` flag regardless of
        the verdict string."""
        self._seed_candidate()
        # A record whose verdict is ``VERIFIED`` but that carries
        # the failed-tag MUST be rejected -- the verdict tag is the
        # producer's authoritative signal.
        write_artifact(
            self.paths["verifier"], {
                "schema_version": "autocoder.verifier_record.v1",
                "candidate_sha256": "c" * 64,
                "verdict": "VERIFIED",
                "defects": [],
                "_verdict_failed": True,
            },
        )
        args = _make_args(self.ctx, self.run_state_root, self.evidence_root)
        exit_code = cli_module.cmd_merge_authorize(args)
        self.assertNotEqual(exit_code, 0)
        self.assertFalse(self.paths["authorization"].exists())


class AuthorizationPreconditionOrderingTests(unittest.TestCase):
    """``cmd_merge_authorize`` MUST validate transition eligibility
    BEFORE writing the canonical authorization.json. A rejected
    authorization must leave no canonical artifact on disk."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="aed-auth-ord-"))
        self.run_state_root = self.tmpdir / "state"
        self.evidence_root = self.tmpdir / "evidence"
        self.run_state_root.mkdir(parents=True)
        self.evidence_root.mkdir(parents=True)
        self.ctx = _make_run_context(self.evidence_root, self.run_state_root)
        self.store = StateStore(str(self.run_state_root))
        self.store.write_atomic("run_context.json", self.ctx.to_dict())
        self.paths = canonical_paths(self.evidence_root)
        for p in self.paths.values():
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_wrong_state_blocks_authorization_without_writing_artifact(self):
        """A controller state that does not support
        ``authorize_merge`` must cause the CLI to return a
        controlled error and MUST NOT write the canonical
        authorization.json."""
        # Seed state at QUALIFYING_READINESS -- NOT
        # AWAITING_MERGE_AUTHORIZATION -- so the controller's
        # transition guard will reject the authorization.
        _seed_state(self.store, "QUALIFYING_READINESS")
        # Seed valid candidate + verifier so the authorization
        # would only fail at the transition guard.
        write_artifact(self.paths["candidate"], {
            "schema_version": "autocoder.candidate.v1",
            "exact_head": self.ctx.current_authorized_head,
            "run_id": self.ctx.run_id,
            "repo": f"{self.ctx.repo_owner}/{self.ctx.repo_name}",
            "pr_number": self.ctx.pr_number,
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
        })
        write_artifact(self.paths["verifier"], {
            "schema_version": "autocoder.verifier_record.v1",
            "candidate_sha256": "c" * 64,
            "verdict": "VERIFIED",
            "defects": [],
        })
        args = _make_args(self.ctx, self.run_state_root, self.evidence_root)
        # The CLI's ``authorize_merge`` path catches controller
        # errors and emits them via _emit; a transition failure
        # must NOT raise. It MUST also leave the canonical
        # authorization.json absent.
        exit_code = cli_module.cmd_merge_authorize(args)
        self.assertNotEqual(exit_code, 0)
        self.assertFalse(
            self.paths["authorization"].exists(),
            "canonical authorization.json was written despite "
            "the controller state rejecting the transition; the "
            "merge transaction could consume rejected evidence",
        )

    def test_wrong_authorized_head_blocks_authorization(self):
        """A wrong authorized head must cause the controller to
        reject the transition and the CLI MUST NOT write the
        canonical authorization.json."""
        from autocoder_orchestration.state_machine import (
            STATE_AWAITING_MERGE_AUTHORIZATION,
        )
        _seed_state(self.store, STATE_AWAITING_MERGE_AUTHORIZATION)
        write_artifact(self.paths["candidate"], {
            "schema_version": "autocoder.candidate.v1",
            "exact_head": self.ctx.current_authorized_head,
            "run_id": self.ctx.run_id,
            "repo": f"{self.ctx.repo_owner}/{self.ctx.repo_name}",
            "pr_number": self.ctx.pr_number,
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
        })
        write_artifact(self.paths["verifier"], {
            "schema_version": "autocoder.verifier_record.v1",
            "candidate_sha256": "c" * 64,
            "verdict": "VERIFIED",
            "defects": [],
        })
        # authorized_head that does NOT match
        # ctx.current_authorized_head must trigger the head guard
        # and prevent any artifact write.
        args = _make_args(
            self.ctx, self.run_state_root, self.evidence_root,
            authorized_head="ff" * 20,  # 40 chars but wrong head
        )
        exit_code = cli_module.cmd_merge_authorize(args)
        self.assertNotEqual(exit_code, 0)
        self.assertFalse(
            self.paths["authorization"].exists(),
            "canonical authorization.json was written despite "
            "an authorized-head mismatch; the merge transaction "
            "could consume rejected evidence",
        )


class MalformedCandidateSchemaTests(unittest.TestCase):
    """A correctly sidecar-signed but schema-malformed candidate
    must trigger a controlled state failure, not an uncaught
    traceback."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="aed-mc-"))
        self.run_state_root = self.tmpdir / "state"
        self.evidence_root = self.tmpdir / "evidence"
        self.run_state_root.mkdir(parents=True)
        self.evidence_root.mkdir(parents=True)
        self.ctx = _make_run_context(self.evidence_root, self.run_state_root)
        self.store = StateStore(str(self.run_state_root))
        self.store.write_atomic("run_context.json", self.ctx.to_dict())
        self.paths = canonical_paths(self.evidence_root)
        for p in self.paths.values():
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_wrong_schema_version_produces_state_failure(self):
        """A candidate whose ``schema_version`` field is unsupported
        must trigger a controlled ``EXIT_STATE`` response from
        ``cmd_merge_authorize``."""
        # Seed the wrong-schema candidate and a valid verifier.
        malformed_payload = {
            "schema_version": "autocoder.candidate.v999",  # unsupported
            "run_id": self.ctx.run_id,
            "exact_head": self.ctx.current_authorized_head,
            "pr_number": self.ctx.pr_number,
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
        write_artifact(self.paths["candidate"], malformed_payload)
        write_artifact(self.paths["verifier"], {
            "schema_version": "autocoder.verifier_record.v1",
            "candidate_sha256": "c" * 64,
            "verdict": "VERIFIED",
            "defects": [],
        })
        args = _make_args(self.ctx, self.run_state_root, self.evidence_root)
        # Must NOT raise; must return a controlled nonzero exit.
        exit_code = cli_module.cmd_merge_authorize(args)
        self.assertNotEqual(
            exit_code, 0,
            "schema-invalid candidate must produce a controlled "
            "nonzero exit, not EXIT_OK",
        )
        self.assertEqual(
            exit_code, cli_module.EXIT_STATE,
            f"expected EXIT_STATE=4; got {exit_code}",
        )
        self.assertFalse(
            self.paths["authorization"].exists(),
            "authorization.json must not be written for a "
            "schema-invalid candidate",
        )


class CrossProcessLockHolderPidTests(unittest.TestCase):
    """Two real subprocesses: contender reads the actual holder PID,
    never its own."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="aed-lock-pid-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_contender_reads_actual_holder_pid(self):
        """A contender that fails to acquire ``flock`` MUST observe
        the actual holder's PID in the lock file -- not its own.
        This requires ``flock`` to be acquired BEFORE the holder
        PID is written."""
        repo_root = str(Path(__file__).resolve().parent.parent)
        # Spawn the holder as a subprocess. The holder's PID is
        # captured INSIDE the subprocess by ``os.getpid()`` (not
        # in the parent test process); that PID is then written
        # to the lock file after ``flock`` succeeds. The contender
        # subprocess reads that exact PID from the lock file.
        holder_script = (
            "import os, sys, time\n"
            f"sys.path.insert(0, {repo_root!r})\n"
            "from autocoder_orchestration.merge_lock import merge_lock\n"
            f"with merge_lock({str(self.tmpdir)!r}):\n"
            "    sys.stdout.write(f'HOLDER_PID={os.getpid()}\\n')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(3.0)\n"
            "sys.stdout.write('HOLDER_RELEASED\\n')\n"
            "sys.stdout.flush()\n"
        )
        contender_script = (
            "import sys\n"
            f"sys.path.insert(0, {repo_root!r})\n"
            "from autocoder_orchestration.merge_lock import (\n"
            "    LockUnavailable, merge_lock,\n"
            ")\n"
            "try:\n"
            f"    with merge_lock({str(self.tmpdir)!r}):\n"
            "        sys.stdout.write('CONTENDER_ACQUIRED\\n')\n"
            "        sys.stdout.flush()\n"
            "except LockUnavailable as exc:\n"
            "    sys.stdout.write(\n"
            "        'CONTENDER_BLOCKED '\n"
            "        f'holder_pid={exc.holder_pid} '\n"
            f"        f'contender_pid={os.getpid()}\\n'\n"
            "    )\n"
            "    sys.stdout.flush()\n"
            "    sys.exit(0)\n"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        # Wait for the holder to write its PID inside the lock.
        import time
        deadline = time.time() + 2.0
        holder_pid = None
        collected = ""
        while time.time() < deadline:
            line = holder.stdout.readline()
            if not line:
                break
            collected += line
            stripped = line.strip()
            if stripped.startswith("HOLDER_PID="):
                holder_pid = int(stripped.split("=", 1)[1])
                break
        self.assertIsNotNone(
            holder_pid, f"holder never reported PID; output={collected!r}",
        )
        # Run the contender.
        contender = subprocess.run(
            [sys.executable, "-c", contender_script],
            capture_output=True, text=True, timeout=10,
        )
        # Wait for the holder to release.
        holder.communicate(timeout=10)
        self.assertEqual(
            holder.returncode, 0,
            f"holder rc={holder.returncode}",
        )

        # Parse the contender output.
        observed_holder_pid = None
        contender_pid = None
        for line in contender.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("CONTENDER_BLOCKED"):
                parts = stripped.split()
                for p in parts:
                    if p.startswith("holder_pid="):
                        observed_holder_pid = int(p.split("=", 1)[1])
                    elif p.startswith("contender_pid="):
                        contender_pid = int(p.split("=", 1)[1])
        self.assertIsNotNone(
            observed_holder_pid,
            f"contender stdout did not report holder_pid; "
            f"stdout={contender.stdout!r}",
        )
        self.assertIsNotNone(
            contender_pid,
            f"contender stdout did not report contender_pid; "
            f"stdout={contender.stdout!r}",
        )
        # Contender observed the actual holder's PID.
        self.assertEqual(
            observed_holder_pid, holder_pid,
            f"contender observed holder_pid={observed_holder_pid} but "
            f"actual holder_pid={holder_pid}; the contender may have "
            "overwritten the holder's metadata before flock succeeded",
        )
        # Contender is NOT the holder.
        self.assertNotEqual(
            contender_pid, holder_pid,
            "contender_pid == holder_pid; the subprocess reused "
            "the same PID by accident; rerun the test",
        )
        # And the holder PID MUST be the actual holder process,
        # not the contender.
        self.assertNotEqual(
            observed_holder_pid, contender_pid,
            "LockUnavailable reported the contender's own PID as "
            "the holder; the merge_lock contract is broken",
        )


class NullCodeRabbitAuthorTests(unittest.TestCase):
    """``_filter_coderabbit_review_state`` MUST treat a null author
    as 'no matching identity' without raising."""

    def test_null_author_returns_none(self):
        """A review node with ``author: None`` and any state must
        not raise and must return ``None`` because no CodeRabbit
        identity matched."""
        payload = {
            "data": {"repository": {"pullRequest": {
                "latestReviews": {"nodes": [
                    {"author": None, "state": "APPROVED"},
                ]},
            }}},
        }
        result = cli_module._filter_coderabbit_review_state(payload)
        self.assertIsNone(result)

    def test_mixed_null_and_real_authors(self):
        """A mixed list with one null author and one real author
        must yield the real author's state without raising on
        the null."""
        payload = {
            "data": {"repository": {"pullRequest": {
                "latestReviews": {"nodes": [
                    {"author": None, "state": "APPROVED"},
                    {"author": {"login": "coderabbitai"},
                     "state": "CHANGES_REQUESTED"},
                ]},
            }}},
        }
        result = cli_module._filter_coderabbit_review_state(payload)
        self.assertEqual(result, "CHANGES_REQUESTED")


if __name__ == "__main__":
    unittest.main()