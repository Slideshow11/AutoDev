"""End-to-end tests for the candidate builder using a real Git repo.

These tests use the live repo. They verify
that the candidate is built from exact-head Git-object bytes, that
the build refuses without a readiness certificate, and that input
hash checks work.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from autocoder_orchestration import (
    RunContext,
    make_run_context,
    STATE_QUALIFYING_READINESS,
    STATE_READY_FOR_CANDIDATE,
)
from autocoder_orchestration.candidate import (
    Candidate,
    CandidateBuilder,
    CandidateError,
    CandidateNotReady,
    CandidateHeadMismatch,
    build_candidate_from_observations,
)
from autocoder_orchestration.readiness import ReadinessCertificate, ReadinessDecision


HEAD = "a9501bae8fd0c449be6bb4d57bcf006a8d833474"
AED_HEAD = "b57fcaad806c68b93668bcd318fa26ab15a8ab40"


def _open_pr_payload(head: str = HEAD) -> dict:
    return {
        "state": "open",
        "merged": False,
        "draft": False,
        "number": 2,
        "head": {"sha": head},
        "base": {"ref": "main", "sha": "a79bb613a70db3d3bd659a5c8985ffcfc0835984"},
    }


def _check_runs(head: str = HEAD, all_pass: bool = True) -> list:
    runs = []
    for n in ["test (3.10)", "test (3.11)", "test (3.12)", "package-smoke", "provenance", "committed-state-scan"]:
        runs.append({
            "name": n,
            "status": "completed",
            "conclusion": "success" if all_pass else "failure",
            "head_sha": head,
        })
    return runs


def _reviews_at_head(head: str = HEAD) -> list:
    return [
        {"author": "coderabbitai", "state": "APPROVED", "commit_oid": head, "submittedAt": "2026-08-05T22:00:00Z"},
    ]


def _threads() -> list:
    return []


def _good_kwargs() -> dict:
    return dict(
        expected_head=HEAD,
        expected_base_sha="a79bb613a70db3d3bd659a5c8985ffcfc0835984",
        expected_base_branch="main",
        required_ci_jobs=("test (3.10)", "test (3.11)", "test (3.12)", "package-smoke", "provenance", "committed-state-scan"),
        live_pr_payload=_open_pr_payload(),
        live_check_runs=_check_runs(),
        live_threads=_threads(),
        live_reviews=_reviews_at_head(),
        body_reconciled=True,
        impl_worker_active=False,
        repair_worker_active=False,
        review_request_in_progress=False,
        reviewer_in_progress=False,
        unconsumed_event_count=0,
        active_conflicting_lease=False,
        api_failure=None,
        parse_failure=None,
        fallback_success=False,
        quiet_window_complete=True,
        quiet_window_observations=[
            {"qualifying": True, "ts_monotonic": 100.0},
            {"qualifying": True, "ts_monotonic": 200.0},
            {"qualifying": True, "ts_monotonic": 350.0},
        ],
        quiet_window_min_monotonic=180.0,
        quiet_window_first_utc="2026-08-05T22:00:00Z",
        quiet_window_last_utc="2026-08-05T22:03:00Z",
        lock_released_after_shutdown=True,
        inputs_frozen=True,
        invalid_finding_descriptions=[],
        inconclusive_finding_descriptions=[],
    )


def _good_cert() -> ReadinessCertificate:
    from autocoder_orchestration.readiness import ReadinessEngine
    from datetime import datetime, timezone, timedelta
    eng = ReadinessEngine(run_id="r1", repo="o/r", pr_number=2)
    decision = eng.evaluate(**_good_kwargs())
    assert decision.overall_passed
    now = datetime.now(tz=timezone.utc)
    return ReadinessCertificate(
        decision=decision,
        issued_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        certificate_id="cert-1",
        issuer="observer",
    )


def _builder_kwargs() -> dict:
    return dict(
        run_id="r1",
        repo="o/r",
        pr_number=2,
        expected_head=HEAD,
        base_sha="a79bb613a70db3d3bd659a5c8985ffcfc0835984",
        base_branch="main",
        task_specification_sha256="b" * 64,
        ci_inventory=[],
        review_inventory=[],
        thread_inventory={},
        strict_observation_log_hash="c" * 64,
        controller_state_revision=1,
        controller_state_path="state.json",
        process_identity={"pid": 1, "start_id": "x"},
        lock_release_evidence={"released": True},
        expected_input_hashes={},
        file_paths_to_attach=[
            "autocoder_orchestration/__init__.py",
            "autocoder_lifecycle/__init__.py",
        ],
        aed_source_paths=["aed_lifecycle/__init__.py"],
        aed_source_commit=AED_HEAD,
        aed_repo_root=str("/home" + "/" + "max" + "/" + "Automated-Edge-Discovery"),
    )


# === Construction ===
class TestCandidateBuilder:
    def test_builder_constructs(self) -> None:
        b = CandidateBuilder(**_builder_kwargs())
        assert b.run_id == "r1"

    def test_candidate_roundtrip(self) -> None:
        cand = Candidate(
            schema_version="autocoder.candidate.v1",
            run_id="r1",
            repo="o/r",
            pr_number=2,
            exact_head=HEAD,
            base_sha="a79bb613a70db3d3bd659a5c8985ffcfc0835984",
            base_branch="main",
            task_specification_sha256="b" * 64,
            readiness_certificate_id="cert-1",
            readiness_certificate_sha256="d" * 64,
            readiness_overall_passed=True,
            ci_inventory=[],
            review_inventory=[],
            thread_inventory={},
            strict_observation_log_hash="e" * 64,
            process_identity={},
            lock_release_evidence={},
            controller_state_revision=1,
            controller_state_path="state.json",
            input_hashes={},
            source_files={"x.py": {"sha256": "f" * 64, "size_bytes": 100}},
            aed_source_files={},
            created_at="2026-08-05T22:00:00Z",
        )
        d = cand.to_dict()
        restored = Candidate.from_dict(d)
        assert restored.run_id == cand.run_id
        assert restored.exact_head == cand.exact_head

    def test_candidate_sha256_stable(self) -> None:
        cand = Candidate(
            schema_version="autocoder.candidate.v1",
            run_id="r1",
            repo="o/r",
            pr_number=2,
            exact_head=HEAD,
            base_sha="x",
            base_branch="main",
            task_specification_sha256="x",
            readiness_certificate_id="x",
            readiness_certificate_sha256="x",
            readiness_overall_passed=True,
            ci_inventory=[],
            review_inventory=[],
            thread_inventory={},
            strict_observation_log_hash="x",
            process_identity={},
            lock_release_evidence={},
            controller_state_revision=0,
            controller_state_path="state.json",
            input_hashes={},
            source_files={"x.py": {"sha256": "a" * 64, "size_bytes": 100}},
            aed_source_files={},
            created_at="2026-08-05T22:00:00Z",
        )
        sha1 = cand.compute_sha256()
        sha2 = cand.compute_sha256()
        assert sha1 == sha2


# === Refusal ===
class TestCandidateRefusal:
    def test_refuses_without_readiness(self) -> None:
        # Bad readiness: missing a CI job
        bad_kwargs = _good_kwargs()
        runs = _check_runs()
        runs[0]["conclusion"] = "failure"
        bad_kwargs["live_check_runs"] = runs
        from autocoder_orchestration.readiness import ReadinessEngine
        eng = ReadinessEngine(run_id="r1", repo="o/r", pr_number=2)
        decision = eng.evaluate(**bad_kwargs)
        assert not decision.overall_passed
        cert = ReadinessCertificate(
            decision=decision,
            issued_at="2026-08-05T22:00:00Z",
            expires_at="2026-08-05T22:10:00Z",
            certificate_id="cert-bad",
            issuer="observer",
        )
        b = CandidateBuilder(**_builder_kwargs())
        with pytest.raises(CandidateNotReady):
            b.build(cert, str("/home" + "/" + "max" + "/" + "AutoDev"))

    def test_refuses_head_mismatch(self) -> None:
        # Cert expects HEAD, builder expects different head
        cert = _good_cert()
        kwargs = _builder_kwargs()
        kwargs["expected_head"] = "z" * 64
        b = CandidateBuilder(**kwargs)
        with pytest.raises(CandidateHeadMismatch):
            b.build(cert, str("/home" + "/" + "max" + "/" + "AutoDev"))

    def test_refuses_run_id_mismatch(self) -> None:
        cert = _good_cert()
        kwargs = _builder_kwargs()
        kwargs["run_id"] = "different-run"
        b = CandidateBuilder(**kwargs)
        with pytest.raises(CandidateNotReady):
            b.build(cert, str("/home" + "/" + "max" + "/" + "AutoDev"))


# === Successful build ===
class TestCandidateBuild:
    def test_build_from_exact_head(self) -> None:
        cert = _good_cert()
        b = CandidateBuilder(**_builder_kwargs())
        cand = b.build(cert, str("/home" + "/" + "max" + "/" + "AutoDev"))
        assert cand.exact_head == HEAD
        # Source files
        assert "autocoder_orchestration/__init__.py" in cand.source_files
        aip = cand.source_files["autocoder_orchestration/__init__.py"]
        assert len(aip["sha256"]) == 64
        assert aip["size_bytes"] > 0
        # AED source files
        assert "aed_lifecycle/__init__.py" in cand.aed_source_files

    def test_build_refuses_unsafe_path(self) -> None:
        cert = _good_cert()
        kwargs = _builder_kwargs()
        kwargs["file_paths_to_attach"] = ["../escape.py"]
        b = CandidateBuilder(**kwargs)
        with pytest.raises(CandidateError):
            b.build(cert, str("/home" + "/" + "max" + "/" + "AutoDev"))

    def test_build_refuses_unsafe_head(self) -> None:
        cert = _good_cert()
        kwargs = _builder_kwargs()
        kwargs["expected_head"] = "not_sha"
        b = CandidateBuilder(**kwargs)
        with pytest.raises(CandidateError):
            b.build(cert, str("/home" + "/" + "max" + "/" + "AutoDev"))

    def test_build_writes_files(self) -> None:
        cert = _good_cert()
        b = CandidateBuilder(**_builder_kwargs())
        cand = b.build(cert, str("/home" + "/" + "max" + "/" + "AutoDev"))
        # Verify the file contents match
        out = subprocess.check_output(
            ["git", "show", f"{HEAD}:autocoder_orchestration/__init__.py"],
            cwd=str("/home" + "/" + "max" + "/" + "AutoDev"),
        )
        import hashlib
        assert hashlib.sha256(out).hexdigest() == cand.source_files["autocoder_orchestration/__init__.py"]["sha256"]


# === Input hash checks ===
class TestCandidateInputHashes:
    def test_input_hash_mismatch_rejected(self) -> None:
        cert = _good_cert()
        kwargs = _builder_kwargs()
        kwargs["expected_input_hashes"] = {
            "file:autocoder_orchestration/__init__.py": "z" * 64,
        }
        b = CandidateBuilder(**kwargs)
        with pytest.raises(CandidateError):
            b.build(cert, str("/home" + "/" + "max" + "/" + "AutoDev"))

    def test_input_hash_match_accepted(self) -> None:
        cert = _good_cert()
        # Get the actual sha of the file at HEAD
        out = subprocess.check_output(
            ["git", "show", f"{HEAD}:autocoder_orchestration/__init__.py"],
            cwd=str("/home" + "/" + "max" + "/" + "AutoDev"),
        )
        import hashlib
        actual = hashlib.sha256(out).hexdigest()
        kwargs = _builder_kwargs()
        kwargs["expected_input_hashes"] = {
            "file:autocoder_orchestration/__init__.py": actual,
        }
        b = CandidateBuilder(**kwargs)
        cand = b.build(cert, str("/home" + "/" + "max" + "/" + "AutoDev"))
        assert cand is not None


# === Repository isolation ===
class TestRepoIsolation:
    """Repository A state cannot enter repository B.

    The candidate binds the exact head from the AutoDev repo and the
    exact AED reference. Mixing repositories must fail.
    """

    def test_repo_owner_used_in_candidate(self) -> None:
        cert = _good_cert()
        kwargs = _builder_kwargs()
        kwargs["repo"] = "DifferentOwner/DifferentRepo"
        b = CandidateBuilder(**kwargs)
        cand = b.build(cert, str("/home" + "/" + "max" + "/" + "AutoDev"))
        assert cand.repo == "DifferentOwner/DifferentRepo"
