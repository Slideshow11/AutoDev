"""Round-5 hardening regression tests for the independent
verifier.

These tests cover:
* Defense-in-depth: python -O and PYTHONOPTIMIZE cannot run
  the verifier, and any subprocess invocation that bypasses
  the gates fails closed.
* Producer identity: the persisted ``verifier`` field is
  derived from the executing module; no evidence record
  claims a producer that did not generate it.
* Incident-record precise failure modes: missing sidecar,
  wrong digest, malformed sidecar -- each must surface the
  specific verifier/artifact failure with the correct
  diagnostic.
* Real later-page CodeRabbit pagination: the real paginator
  walks both pages against mocked GraphQL responses; the
  second request uses the page-1 cursor.
* Full recovery contract: cmd_post_merge_verify emits a
  machine-readable payload with merge_record_path,
  merge_record_sha256, and recovery_required; the digest
  is independently re-hashed on disk.
* PRRT_kwDOTtyQLc6XPdD0 evidence: a passing verifier record
  exists, authorization binds its exact digest, verifier_failed
  replaces it, the guarded merge sees the digest mismatch,
  and ``_safe_run`` (``gh pr merge``) is never invoked.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# Load the verifier module ONCE at import time so the
# VerificationFailure class identity is shared with the tests.
_verifier_spec = importlib.util.spec_from_file_location(
    "independent_verifier",
    REPO_ROOT / "scripts" / "independent_verifier_v2.py",
)
VERIFIER = importlib.util.module_from_spec(_verifier_spec)
_verifier_spec.loader.exec_module(VERIFIER)
VerificationFailure = VERIFIER.VerificationFailure


class OptimizedPythonRefusalTests(unittest.TestCase):
    """``python -O`` and ``PYTHONOPTIMIZE`` cannot run the
    verifier. Any subprocess invocation that bypasses the
    gates must fail closed: no verifier.json artifact is
    written with verdict VERIFIED."""

    def test_module_import_refuses_python_O(self):
        """Spawning the verifier with ``-O`` exits with a
        controlled SystemExit and emits the refusal banner.
        No verifier artifact can be written."""
        fake_record = Path(tempfile.gettempdir()) / "aed-r5b-fake.json"
        proc = subprocess.run(
            [sys.executable, "-O",
             str(REPO_ROOT / "scripts" / "independent_verifier_v2.py"),
             "--qualification-head", "a" * 40,
             "--incident-record", str(fake_record)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 1,
            f"python -O must refuse the verifier; rc={proc.returncode}; "
            f"stderr={proc.stderr!r}")
        self.assertIn("PYTHONOPTIMIZE", proc.stderr)
        self.assertIn("assert-based gates would be stripped", proc.stderr)

    def test_pythonoptimize_env_var_refuses_run(self):
        """``PYTHONOPTIMIZE=1`` in the environment refuses the
        run at import time."""
        env = os.environ.copy()
        env["PYTHONOPTIMIZE"] = "1"
        fake_record = Path(tempfile.gettempdir()) / "aed-r5b-fake.json"
        proc = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "independent_verifier_v2.py"),
             "--qualification-head", "a" * 40,
             "--incident-record", str(fake_record)],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 1,
            f"PYTHONOPTIMIZE=1 must refuse the verifier; "
            f"rc={proc.returncode}; stderr={proc.stderr!r}")
        self.assertIn("PYTHONOPTIMIZE", proc.stderr)

    def test_optimized_python_produces_no_verified_artifact(self):
        """Even if a hostile caller redirects stdout to an
        evidence root, no verifier.json artifact is written
        under optimized Python. The verifier fails before any
        write."""
        # Set up a fake evidence root.
        tmp = tempfile.mkdtemp(prefix="aed-r5b-opt-")
        try:
            evidence_root = Path(tmp) / "evidence"
            evidence_root.mkdir(parents=True)
            incident_path = Path(tmp) / "incident.json"
            incident_path.write_bytes(b'{"a": 1}')
            env = os.environ.copy()
            env["PYTHONOPTIMIZE"] = "1"
            # Pretend the caller points --evidence-root at the
            # fake directory. The guard must refuse BEFORE any
            # write to that directory.
            proc = subprocess.run(
                [sys.executable, "-O",
                 str(REPO_ROOT / "scripts" / "independent_verifier_v2.py"),
                 "--qualification-head", "a" * 40,
                 "--incident-record", str(incident_path),
                 "--evidence-root", str(evidence_root)],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(proc.returncode, 1)
            # No verifier.json produced under optimized Python.
            # Per round-8 finding PRRT_kwDOTtyQLc6XWVR0: the
            # negative-existence assertion is wrapped so the
            # BinOp is structurally contained inside the
            # assertFalse call (NOT a separate variable).
            self.assertFalse(
                (evidence_root / "verifier.json").exists(),
                f"verifier.json was written under python -O; "
                f"this is a fail-open vulnerability. "
                f"files in evidence_root: "
                f"{[p.name for p in evidence_root.iterdir()]}",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ProducerIdentityTests(unittest.TestCase):
    """The persisted ``verifier`` field must be derived from
    the executing module. No record may claim a producer that
    did not generate it.

    Per round-6 directive 9c, the source-text identity tests
    were removed. The persisted-payload coverage in
    test_no_hardcoded_v3_v4_v5_string is sufficient and
    behavior-preserving (it does not depend on the source-text
    layout of the verifier module)."""

    def test_no_hardcoded_v3_v4_v5_string(self):
        """The verifier record's ``verifier`` field MUST NOT
        contain any hardcoded "scripts.independent_verifier_v3"
        or "v4" or "v5" string. A regression that hardcodes a
        stale producer identity must fail."""
        # Build a synthetic observations dict and call
        # _write_verifier; check the persisted record's
        # ``verifier`` field reflects the actual module path.
        evidence_root = Path(tempfile.mkdtemp())
        from autocoder_orchestration.canonical_paths import canonical_paths
        paths = canonical_paths(evidence_root)
        paths["verifier"].parent.mkdir(parents=True, exist_ok=True)
        try:
            args = type("A", (), {"evidence_root": paths["verifier"].parent})()
            qual = "a" * 40
            observations = {
                "pr": {"headRefOid": qual, "state": "OPEN",
                       "mergedAt": None, "isDraft": False,
                       "mergeable": "MERGEABLE",
                       "mergeStateStatus": "CLEAN",
                       "autoMergeRequest": None},
                "ci": {"runs": [], "total_count": 0},
                "coderabbit": {"review_decision": "APPROVED",
                                "coderabbit_states": ["APPROVED"],
                                "latest_coderabbit_state": "APPROVED",
                                "latest_coderabbit_submitted_at": "2026-08-07T10:00:00Z"},
                "threads": {"nodes": [{"id": "t1", "isResolved": True,
                                       "isOutdated": False}],
                            "count": 1},
                "aed": {"measured_sha256": "f" * 64,
                        "expected_sha256": "f" * 64,
                        "manifest_sha256": "f" * 64},
                "strict_window": {"span_seconds": 200.0,
                                    "observation_count": 5,
                                    "observation_head_sha": qual},
                "candidate": {"candidate_digest": "d" * 64,
                                "candidate_exact_head": qual,
                                "candidate_pr_number": 4},
                "incident": {"incident_digest": "e" * 64,
                              "incident_class": "FORCE_PUSH",
                              "force_push_mechanism": "force-with-lease",
                              "restored_head_sha": "f" * 40,
                              "no_repeat_permitted": True},
            }
            # Mock the gate functions so we don't need real
            # canonical artifacts. The point is to capture the
            # record before any readback gating runs.
            from autocoder_orchestration.artifacts import write_artifact
            from autocoder_orchestration.canonical_paths import canonical_paths

            # Bypass all _gate checks and call _write_verifier.
            with mock.patch.object(VERIFIER, "_gate"):
                VERIFIER._write_verifier(args, qual, observations)
            # Read back the canonical record.
            from autocoder_orchestration.artifacts import read_artifact
            record = read_artifact(paths["verifier"])
            self.assertIn(
                "verifier", record.payload,
                "verifier record must include the producer identity field",
            )
            self.assertEqual(
                record.payload["verifier"],
                "scripts.independent_verifier_v2",
                f"verifier field claims {record.payload['verifier']!r}; "
                f"must be scripts.independent_verifier_v2 (the actual "
                f"executing module). The producer identity must NEVER be "
                f"hardcoded to a different version.",
            )
            self.assertNotIn(
                "scripts.independent_verifier_v3", record.payload.get("verifier", ""),
                "verifier field must not claim v3 (a stale identity)",
            )
            self.assertNotIn(
                "scripts.independent_verifier_v4", record.payload.get("verifier", ""),
                "verifier field must not claim v4 (a stale identity)",
            )
            self.assertEqual(
                record.payload.get("verifier_module_path"),
                str((REPO_ROOT / "scripts" / "independent_verifier_v2.py").resolve()),
                "verifier_module_path must equal the actual executing module path",
            )
        finally:
            shutil.rmtree(evidence_root, ignore_errors=True)


class IncidentRecordPreciseFailureTests(unittest.TestCase):
    """Round-5 hardening: each incident-record failure mode
    must surface the specific verifier/artifact failure with
    the correct diagnostic. Tests must not use
    ``assertRaises(Exception)``."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="aed-r5b-incident-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_artifact(self, path: Path, payload, mode: int = 0o600):
        import os as _os
        fd = _os.open(str(path),
                      _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, mode)
        try:
            if isinstance(payload, str):
                _os.write(fd, payload.encode("ascii"))
            else:
                _os.write(fd, payload)
        finally:
            _os.close(fd)

    def _body(self, restored_head_sha: str = "a" * 40) -> bytes:
        return json.dumps({
            "schema_version": "autocoder.incident.v1",
            "incident_class": "FORCE_PUSH",
            "force_push_mechanism": "git push --force-with-lease",
            "no_repeat_permitted": True,
            "restored_head_sha": restored_head_sha,
        }).encode("utf-8")

    def test_missing_sidecar_fails_with_specific_diagnostic(self):
        """Body is valid (mode 0600); sidecar is missing. The
        verifier MUST raise an exception that names the
        missing sidecar; the diagnostic MUST point at the
        missing sidecar, not a generic failure."""
        body_path = self.tmpdir / "incident.json"
        self._write_artifact(body_path, self._body(), mode=0o600)
        before = sorted(p.name for p in self.tmpdir.iterdir())
        args = type("A", (), {
            "incident_record": body_path,
            "repo": "o/r", "pr_number": 1,
        })()
        with self.assertRaises(VerificationFailure) as ctx:
            VERIFIER._verify_incident_record(args)
        # The diagnostic MUST name the missing sidecar.
        msg = str(ctx.exception).lower()
        self.assertIn("sidecar", msg)
        self.assertIn("missing", msg)
        # No sidecar was created.
        after = sorted(p.name for p in self.tmpdir.iterdir())
        self.assertEqual(before, after)

    def test_digest_mismatch_fails_with_specific_diagnostic(self):
        """Body and sidecar are both mode 0o600; the sidecar
        contains the WRONG digest. The verifier MUST raise an
        exception whose diagnostic is about the DIGEST
        MISMATCH, not about permissions."""
        body_bytes = self._body()
        body_path = self.tmpdir / "incident.json"
        self._write_artifact(body_path, body_bytes, mode=0o600)
        wrong_sidecar = self.tmpdir / "incident.json.sha256"
        # Sidecar with WRONG digest.
        self._write_artifact(wrong_sidecar, "f" * 64 + "\n", mode=0o600)
        before_body = body_path.read_bytes()
        before_sidecar = wrong_sidecar.read_bytes()
        args = type("A", (), {
            "incident_record": body_path,
            "repo": "o/r", "pr_number": 1,
        })()
        from autocoder_orchestration.artifacts import ArtifactError
        # Per round-6 directive 9f the deterministic read_artifact
        # failure path must be asserted as ArtifactError only.
        with self.assertRaises(ArtifactError) as ctx:
            VERIFIER._verify_incident_record(args)
        msg = str(ctx.exception).lower()
        # The failure is specifically about the digest mismatch.
        self.assertIn("digest", msg,
            f"failure diagnostic must reference 'digest', not 'permission' "
            f"or 'mode'; got: {msg!r}")
        # Neither file was modified.
        self.assertEqual(body_path.read_bytes(), before_body)
        self.assertEqual(wrong_sidecar.read_bytes(), before_sidecar)

    def test_correct_incident_record_passes(self):
        """A canonical incident record with mode 0o600 body AND
        sidecar whose digest matches MUST pass."""
        import hashlib
        body_bytes = self._body()
        body_path = self.tmpdir / "incident.json"
        self._write_artifact(body_path, body_bytes, mode=0o600)
        digest = hashlib.sha256(body_bytes).hexdigest()
        sidecar_path = self.tmpdir / "incident.json.sha256"
        self._write_artifact(sidecar_path, digest + "\n", mode=0o600)
        args = type("A", (), {
            "incident_record": body_path,
            "repo": "o/r", "pr_number": 1,
        })()
        result = VERIFIER._verify_incident_record(args)
        self.assertEqual(result["incident_digest"], digest)


class RealLaterPagePaginationTests(unittest.TestCase):
    """``_inspect_coderabbit`` exercises the real paginator
    against multiple mocked pages. The second request uses the
    page-1 cursor."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="aed-r5b-pag-"))
        # Seed incident-record fixture for main() to consume if
        # ever invoked; the inspect_coderabbit helper does not
        # need it.
        self.incident_path = self.tmpdir / "incident.json"
        self.incident_body = json.dumps({
            "schema_version": "autocoder.incident.v1",
            "incident_class": "FORCE_PUSH",
            "force_push_mechanism": "git push --force-with-lease",
            "no_repeat_permitted": True,
            "restored_head_sha": "a" * 40,
        }).encode("utf-8")
        self.incident_path.write_bytes(self.incident_body)
        import hashlib
        digest = hashlib.sha256(self.incident_body).hexdigest()
        sidecar_path = self.tmpdir / "incident.json.sha256"
        fd = os.open(str(sidecar_path),
                      os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (digest + "\n").encode("ascii"))
        finally:
            os.close(fd)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _args(self):
        return type("A", (), {
            "repo": "o/r",
            "pr_number": 4,
            "incident_record": self.incident_path,
            "evidence_root": self.tmpdir,
            "strict_window_obs": self.tmpdir / "obs.jsonl",
            "aed_path": REPO_ROOT / "scripts" / "quiet_window_observer.py",
            "aed_expected_sha": "9897bd3b780fd03561b6d9f10302ced2e549cb5f3288aebadddddaa1c70f42ae",
            "qualification_head": "a" * 40,
        })()

    def _review(self, login, state, submitted_at):
        author = {"login": login} if login is not None else None
        return {"state": state, "submittedAt": submitted_at,
                "author": author}

    def _drive_paginator(self, pages, decision, args):
        """Mock ``_run_gh_graphql`` only; let the real paginator
        run. Track the cursor passed on each call so we can
        prove cursor propagation."""
        calls = []

        def fake_run(query, variables):
            calls.append({
                "cursor": variables.get("cursor"),
                "has_review_decision": "reviewDecision" in query,
            })
            if "reviewDecision" in query:
                return {"data": {"repository": {"pullRequest": {
                    "reviewDecision": decision,
                }}}}
            cursor = variables["cursor"]
            cursor_index = {"null": 0}
            for i in range(1, len(pages)):
                cursor_index[f"CURSOR_{i}"] = i
            if cursor not in cursor_index:
                raise AssertionError(
                    f"paginator sent an unexpected cursor: {cursor!r}"
                )
            idx = cursor_index[cursor]
            return {"data": {"repository": {"pullRequest": {
                "latestReviews": {
                    "pageInfo": {
                        "hasNextPage": idx + 1 < len(pages),
                        "endCursor": (
                            f"CURSOR_{idx + 1}"
                            if idx + 1 < len(pages) else None
                        ),
                    },
                    "totalCount": sum(len(p["nodes"]) for p in pages),
                    "nodes": pages[idx]["nodes"],
                },
            }}}}

        with mock.patch.object(VERIFIER, "_run_gh_graphql",
                               side_effect=fake_run):
            return VERIFIER._inspect_coderabbit(args), calls

    def test_two_pages_cursor_propagation(self):
        """Page 1 has no CodeRabbit; page 2 has the matching
        APPROVED review. The real paginator walks both pages
        using the page-1 cursor."""
        pages = [
            {"nodes": [
                self._review("some-human", "APPROVED",
                              "2026-08-01T09:00:00Z"),
            ]},
            {"nodes": [
                self._review("coderabbitai", "APPROVED",
                              "2026-08-07T10:00:00Z"),
            ]},
        ]
        result, calls = self._drive_paginator(pages, "APPROVED", self._args())
        # Two paginated GraphQL requests occurred (the
        # third call is the one-shot decision query, which is
        # NOT paginated).
        paginated_calls = [c for c in calls if not c["has_review_decision"]]
        self.assertEqual(len(paginated_calls), 2,
            f"expected exactly 2 paginated GraphQL requests; "
            f"calls={calls!r}")
        # The second paginated request used the page-1 cursor.
        self.assertEqual(paginated_calls[0]["cursor"], "null")
        self.assertEqual(paginated_calls[1]["cursor"], "CURSOR_1")
        # The CodeRabbit APPROVED on page 2 was discovered and
        # selected as the newest.
        self.assertEqual(result["latest_coderabbit_state"], "APPROVED")
        self.assertEqual(
            result["latest_coderabbit_submitted_at"],
            "2026-08-07T10:00:00Z",
        )

    def test_older_approved_newer_changes_requested_fails(self):
        pages = [
            {"nodes": [
                self._review("coderabbitai", "CHANGES_REQUESTED",
                              "2026-08-07T10:00:00Z"),
                self._review("coderabbitai", "APPROVED",
                              "2026-08-06T09:00:00Z"),
            ]},
        ]
        with self.assertRaises(VerificationFailure) as ctx:
            self._drive_paginator(pages, "APPROVED", self._args())
        # Per round-6 directive 9a, the failure must identify
        # the latest-review-state gate, not just any failure.
        msg = str(ctx.exception)
        self.assertIn("CHANGES_REQUESTED", msg,
            f"failure must name the latest-review-state gate; "
            f"got: {msg!r}")

    def test_older_changes_requested_newer_approved_passes(self):
        pages = [
            {"nodes": [
                self._review("coderabbitai", "APPROVED",
                              "2026-08-07T10:00:00Z"),
                self._review("coderabbitai", "CHANGES_REQUESTED",
                              "2026-08-06T09:00:00Z"),
            ]},
        ]
        result, _ = self._drive_paginator(pages, "APPROVED", self._args())
        self.assertEqual(result["latest_coderabbit_state"], "APPROVED")


class FullRecoveryContractTests(unittest.TestCase):
    """cmd_post_merge_verify emits a machine-readable payload
    with merge_record_path, merge_record_sha256, and
    recovery_required. The digest is independently re-hashed
    on disk and the test asserts equality."""

    def setUp(self):
        from autocoder_orchestration import cli as cli_module
        from autocoder_orchestration.context import (
            SCHEMA_VERSION as RC_SCHEMA,
        )
        from autocoder_orchestration.context import RunContext
        from autocoder_orchestration.store import StateStore
        from autocoder_orchestration.state_machine import (
            STATE_POST_MERGE_VERIFYING,
            StateMachine,
        )
        from autocoder_orchestration.canonical_paths import canonical_paths
        from autocoder_orchestration.artifacts import write_artifact
        from autocoder_orchestration.controller import Controller
        from autocoder_orchestration.merge_authorization import (
            MergeRecord,
        )
        from argparse import Namespace

        self.tmpdir = Path(tempfile.mkdtemp(prefix="aed-r5b-rec-"))
        self.run_state_root = self.tmpdir / "state"
        self.evidence_root = self.tmpdir / "evidence"
        self.run_state_root.mkdir(parents=True)
        self.evidence_root.mkdir(parents=True)
        self.ctx = RunContext(
            schema_version=RC_SCHEMA,
            run_id="test-r5b-rec",
            created_at="2026-08-07T15:00:00Z",
            repo_owner="Slideshow11", repo_name="AutoDev",
            local_checkout=str(self.run_state_root / "repo"),
            base_branch="main",
            authorized_base_sha="a" * 40,
            feature_branch="fix/test",
            pr_number=4,
            current_authorized_head="b" * 40,
            task_specification_path=str(self.evidence_root / "task.txt"),
            task_specification_sha256="c" * 64,
            required_ci_jobs=("test (3.10)", "test (3.11)",
                              "test (3.12)", "package-smoke",
                              "provenance", "committed-state-scan"),
            reviewer_policy="approve-only",
            quiet_window_seconds=180,
            implementation_worker_command=("echo", "worker"),
            verifier_command=None,
            verifier_handoff_policy="strict",
            permitted_mutations=("candidate.json", "verifier.json",
                                 "merge-record.json"),
            human_only_actions=("merge",),
            evidence_root=str(self.evidence_root),
            state_root=str(self.run_state_root),
            next_wave_policy="none",
        )
        self.store = StateStore(str(self.run_state_root))
        self.store.write_atomic("run_context.json", self.ctx.to_dict())
        sm = StateMachine(current_state=STATE_POST_MERGE_VERIFYING)
        self.store.write_atomic("state.json", sm.to_dict())
        self.paths = canonical_paths(self.evidence_root)
        for p in self.paths.values():
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        rec = MergeRecord(
            schema_version="autocoder.merge_record.v2",
            run_id=self.ctx.run_id,
            repo=f"{self.ctx.repo_owner}/{self.ctx.repo_name}",
            pr_number=self.ctx.pr_number,
            authorized_head=self.ctx.current_authorized_head,
            squash_merge_commit="c1" * 14,
            merge_commit_parent="p1" * 10,
            squash_commit_parent_count=1,
            squash_tree_sha256="t1" * 16,
            final_local_main_sha="m1" * 20,
            final_origin_main_sha="m2" * 20,
            local_main_equals_origin_main=True,
            feature_branch_deleted_locally=False,
            feature_branch_deleted_remotely=False,
            working_tree_clean=True,
            aed_clean_post_merge=True,
            candidate_sha256_unchanged=True,
            verifier_record_sha256_unchanged=True,
            candidate_exact_file_digest="d" * 64,
            verifier_record_exact_file_digest="e" * 64,
            authorization_exact_file_digest="f" * 64,
            merge_record_exact_file_digest="",
            merge_timestamp="2026-08-07T15:00:00Z",
            unauthorized_actions_taken={
                "force_push": False, "auto_merge": False,
            },
            unavailable_observations=[],
            notes="",
            state_transition="MERGE_AUTHORIZED -> POST_MERGE_VERIFYING",
            final_state="COMPLETE",
        )
        write_artifact(self.paths["merge_record"], rec.to_dict())
        self.merge_record_path = self.paths["merge_record"]
        self.cli_module = cli_module
        self.args = Namespace(
            run_id=self.ctx.run_id,
            state_root=str(self.run_state_root),
            evidence_root=str(self.evidence_root),
            json=True,
            pr_number=self.ctx.pr_number,
            authorized_head=self.ctx.current_authorized_head,
        )
        self.Controller = Controller

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_state_error_full_recovery_contract(self):
        """Controller.report_complete raising StateError
        produces an EXIT_STATE with a machine-readable
        payload:
          * error message
          * merge_record_path (canonical evidence-root path)
          * merge_record_sha256 (verified exact-file digest)
          * recovery_required = True
          * recovery_action (human-readable)
        The merge record remains durable. The digest in the
        payload equals the actual on-disk digest (verified
        independently via read_artifact)."""
        from autocoder_orchestration.state_machine import StateError
        from autocoder_orchestration.artifacts import read_artifact

        def raise_state_error(self_):
            raise StateError("simulated state-machine rejection")
        import io, contextlib
        buf = io.StringIO()
        with mock.patch.object(self.Controller, "report_complete",
                                raise_state_error):
            with contextlib.redirect_stdout(buf):
                exit_code = self.cli_module.cmd_post_merge_verify(self.args)
        # EXIT_STATE
        self.assertEqual(exit_code, self.cli_module.EXIT_STATE)
        # Parse the JSON payload.
        payload = json.loads(buf.getvalue())
        self.assertIn("error", payload)
        self.assertIn("merge_record_path", payload)
        self.assertIn("merge_record_sha256", payload)
        self.assertIn("recovery_required", payload)
        self.assertIn("recovery_action", payload)
        self.assertTrue(payload["recovery_required"])
        # The path in the payload is the canonical evidence-root
        # merge-record path.
        self.assertEqual(
            payload["merge_record_path"],
            str(self.merge_record_path),
        )
        # Independently re-hash the on-disk record and confirm
        # equality.
        on_disk = read_artifact(self.merge_record_path)
        self.assertEqual(
            payload["merge_record_sha256"],
            on_disk.digest,
        )
        # Merge record remains durable.
        self.assertTrue(self.merge_record_path.exists())


class FailedVerifierZeroGhInvocationsTests(unittest.TestCase):
    """PRRT_kwDOTtyQLc6XPdD0 evidence: a passing verifier
    artifact exists; authorization binds its exact digest;
    ``verifier_failed`` replaces the canonical verifier;
    the guarded merge re-reads the verifier artifact;
    the digest mismatch and/or failed verdict is detected;
    ``_safe_run`` (``gh pr merge``) is never invoked.

    This is the round-5 evidence that option B (verifier-may-
    replace-but-merge-re-verifies) holds."""

    def test_guard_chain_blocks_gh_pr_merge_on_verifier_replacement(self):
        """Build a passing verifier record, authorize against it,
        then replace the canonical verifier with a failed
        record (verifier_failed stamps _verdict_failed). The
        guarded merge MUST NOT invoke ``_safe_run`` and MUST
        raise MergeError."""
        from autocoder_orchestration.merge_authorization import (
            execute_guarded_merge_transaction,
            MergeTransactionInputs,
            MergeError,
        )
        # Build the inputs.
        import tempfile
        from pathlib import Path
        from autocoder_orchestration.artifacts import write_artifact
        from autocoder_orchestration.canonical_paths import canonical_paths

        tmp = Path(tempfile.mkdtemp(prefix="aed-r5b-dD0-"))
        try:
            repo_path = tmp / "repo"
            state_path = tmp / "state"
            evidence_root = tmp / "evidence"
            repo_path.mkdir()
            state_path.mkdir()
            evidence_root.mkdir()
            paths = canonical_paths(evidence_root)
            for p in paths.values():
                p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Build a passing verifier record.
            passing = {
                "schema_version": "autocoder.verifier_record.v1",
                "candidate_sha256": "c" * 64,
                "verdict": "VERIFIED",
                "defects": [],
            }
            write_artifact(paths["verifier"], passing)
            # Authorization binds the EXACT-FILE digest of the
            # passing verifier.
            from autocoder_orchestration.artifacts import read_artifact
            passing_digest = read_artifact(paths["verifier"]).digest
            # Build the canonical artifacts the merge transaction
            # expects.
            cand = {
                "schema_version": "autocoder.candidate.v1",
                "exact_head": "2a8e4e9c1f3a4b5d6e7f8091a2b3c4d5e40ffe0d",
                "pr_number": 4,
                "run_id": "test",
            }
            write_artifact(paths["candidate"], cand)
            auth = {
                "schema_version": "autocoder.merge_authorization.v1",
                "run_id": "test",
                "repo": "o/r",
                "pr_number": 4,
                "authorized_head": "2a8e4e9c1f3a4b5d6e7f8091a2b3c4d5e40ffe0d",
                "candidate_sha256": read_artifact(paths["candidate"]).digest,
                "verifier_record_sha256": passing_digest,
                "merge_method": "squash",
                "delete_branch": False,
                "require_match_head_commit": True,
                "authorization_timestamp": "2026-08-07T15:00:00Z",
                "author": "test",
            }
            write_artifact(paths["authorization"], auth)
            # Now ``verifier_failed`` replaces the canonical
            # verifier record with a failed verdict (and the
            # _verdict_failed tag).
            failed = {
                "schema_version": "autocoder.verifier_record.v1",
                "candidate_sha256": "c" * 64,
                "verdict": "FAILED",
                "defects": ["synthetic test failure"],
                "_verdict_failed": True,
            }
            write_artifact(paths["verifier"], failed)
            # The guarded merge MUST detect the replacement
            # (digest mismatch) and refuse to invoke _safe_run.
            with mock.patch(
                "autocoder_orchestration.merge_authorization._safe_run"
            ) as safe_run:
                with self.assertRaises(MergeError):
                    execute_guarded_merge_transaction(MergeTransactionInputs(
                        authorization_artifact_path=paths["authorization"],
                        candidate_artifact_path=paths["candidate"],
                        verifier_artifact_path=paths["verifier"],
                        merge_record_artifact_path=paths["merge_record"],
                        repository_checkout=repo_path,
                        run_state_root=state_path,
                        evidence_root=evidence_root,
                        live_pr_payload={
                            "state": "open",
                            "merged": False,
                            "head": {"sha": "2a8e4e9c1f3a4b5d6e7f8091a2b3c4d5e40ffe0d"},
                            "baseRefName": "main",
                            "mergeable": "MERGEABLE",
                            "mergeStateStatus": "CLEAN",
                            "autoMergeRequest": None,
                        },
                        live_ci_state={"all_required_passing": True,
                                         "coderabbit_passing": True},
                        live_review_state={"latest_coderabbit_state": "APPROVED"},
                        live_thread_inventory={"unresolved_current": 0,
                                                 "unresolved_outdated": 0},
                        working_tree_clean=True,
                    ))
                # The critical assertion: _safe_run / gh pr merge
                # was NEVER invoked.
                safe_run.assert_not_called()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()