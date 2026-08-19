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


def _good_kwargs(expected_head: str = HEAD) -> dict:
    return dict(
        expected_head=expected_head,
        expected_base_sha="a79bb613a70db3d3bd659a5c8985ffcfc0835984",
        expected_base_branch="main",
        required_ci_jobs=("test (3.10)", "test (3.11)", "test (3.12)", "package-smoke", "provenance", "committed-state-scan"),
        live_pr_payload=_open_pr_payload(head=expected_head),
        live_check_runs=_check_runs(head=expected_head),
        live_threads=_threads(),
        live_reviews=_reviews_at_head(head=expected_head),
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


def _good_cert(expected_head: str = HEAD) -> ReadinessCertificate:
    from autocoder_orchestration.readiness import ReadinessEngine
    from datetime import datetime, timezone, timedelta
    eng = ReadinessEngine(run_id="r1", repo="o/r", pr_number=2)
    kwargs = _good_kwargs(expected_head)
    decision = eng.evaluate(**kwargs)
    assert decision.overall_passed
    now = datetime.now(tz=timezone.utc)
    return ReadinessCertificate(
        decision=decision,
        issued_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        certificate_id="cert-1",
        issuer="observer",
    )


def _builder_kwargs(*, aed_repo_root: str | None = None, aed_source_commit: str | None = None) -> dict:
    # Pre-canary round-281 §4: the AED-side candidate build no
    # longer requires the historical /home/max/Automated-Edge-Discovery
    # checkout. Callers MUST pass an ``aed_repo_root`` (and optional
    # ``aed_source_commit``) pointing at a hermetic
    # tmp_path-style AED-like source tree. CI no longer skips
    # negative/fail-closed coverage because the AED path is
    # hermetic.
    if aed_repo_root is None:
        # Back-compat default for callers that don't pass one.
        aed_repo_root = os.environ.get(
            "AUTODEV_AED_REPO_PATH"
        ) or str(Path("/home") / "max" / "Automated-Edge-Discovery")
    if aed_source_commit is None:
        aed_source_commit = AED_HEAD
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
        aed_source_commit=aed_source_commit,
        aed_repo_root=aed_repo_root,
    )


def _tmp_aed_repo(tmp_path: Path) -> tuple[str, str]:
    """Create a hermetic AED-like source tree under ``tmp_path``.

    Returns ``(aed_repo_root, aed_source_commit)``. The repo has
    one initial commit with a single tracked file
    (``aed_lifecycle/__init__.py``) so source-attachment code can
    ``git show`` it without needing the historical
    ``Automated-Edge-Discovery`` checkout.
    """
    aed_root = tmp_path / "aed_repo"
    aed_root.mkdir()
    _git(aed_root, "init", "-q")
    _git(aed_root, "config", "user.email", "test@example.com")
    _git(aed_root, "config", "user.name", "Test")
    pkg = aed_root / "aed_lifecycle"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("# hermetic AED-side fixture\n")
    _git(aed_root, "add", "-A")
    _git(aed_root, "commit", "-q", "-m", "aed-initial")
    head = _git(aed_root, "rev-parse", "HEAD", check=False).stdout.strip()
    return str(aed_root), head


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=check,
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
    # Pre-canary round-281 §4: negative/fail-closed coverage is
    # required in CI. The hermetic tmp_path AED fixture removes
    # the historical-checkout dependency. No skip.

    def test_refuses_without_readiness(self, tmp_path: Path) -> None:
        # Pre-canary round-281 §4: avoid the hardcoded
        # /home/max/AutoDev path; build a hermetic candidate
        # against a tmp_path AED root.
        aed_root, aed_commit = _tmp_aed_repo(tmp_path)
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
        b = CandidateBuilder(
            **_builder_kwargs(
                aed_repo_root=aed_root,
                aed_source_commit=aed_commit,
            )
        )
        with pytest.raises(CandidateNotReady):
            b.build(cert, aed_root)

    def test_refuses_head_mismatch(self, tmp_git_repo, tmp_path: Path) -> None:
        aed_root, aed_commit = _tmp_aed_repo(tmp_path)
        cert = _good_cert(tmp_git_repo.head_full())
        kwargs = _builder_kwargs(
            aed_repo_root=aed_root,
            aed_source_commit=aed_commit,
        )
        kwargs["expected_head"] = "z" * 40
        b = CandidateBuilder(**kwargs)
        with pytest.raises(CandidateHeadMismatch):
            b.build(cert, tmp_git_repo.root)

    def test_refuses_run_id_mismatch(self, tmp_git_repo, tmp_path: Path) -> None:
        aed_root, aed_commit = _tmp_aed_repo(tmp_path)
        cert = _good_cert(tmp_git_repo.head_full())
        kwargs = _builder_kwargs(
            aed_repo_root=aed_root,
            aed_source_commit=aed_commit,
        )
        kwargs["run_id"] = "different-run"
        b = CandidateBuilder(**kwargs)
        with pytest.raises(CandidateNotReady):
            b.build(cert, tmp_git_repo.root)


# === Successful build ===
class _TmpGitRepo:
    """A fixture that creates a real, committed Git repository."""

    def __init__(self, tmp_path):
        self.root = str(tmp_path / "src_repo")
        os.makedirs(self.root)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                       cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"],
                       cwd=self.root, check=True)
        # Add an origin remote so the candidate builder can validate it.
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/o/r"],
            cwd=self.root, check=True,
        )
        # Create a sample file and commit it
        sample = os.path.join(self.root, "sample.py")
        with open(sample, "w") as f:
            f.write("# sample file\n")
        subprocess.run(["git", "add", "sample.py"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=self.root, check=True)
        self.head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.root, text=True
        ).strip()

    def head_full(self):
        return self.head


@pytest.fixture
def tmp_git_repo(tmp_path):
    return _TmpGitRepo(tmp_path)


class TestCandidateBuild:
    # Pre-canary round-281 §4: the candidate build is exercised
    # against a hermetic AED-like source tree under tmp_path.
    # CI no longer skips these tests.

    def test_build_from_exact_head(self, tmp_git_repo, tmp_path: Path) -> None:
        aed_root, aed_commit = _tmp_aed_repo(tmp_path)
        cert = _good_cert(tmp_git_repo.head_full())
        kwargs = _builder_kwargs(
            aed_repo_root=aed_root,
            aed_source_commit=aed_commit,
        )
        kwargs["expected_head"] = tmp_git_repo.head_full()
        kwargs["file_paths_to_attach"] = ["sample.py"]
        b = CandidateBuilder(**kwargs)
        cand = b.build(cert, tmp_git_repo.root)
        assert cand.exact_head == tmp_git_repo.head_full()
        assert "sample.py" in cand.source_files
        sf = cand.source_files["sample.py"]
        assert len(sf["sha256"]) == 64
        assert sf["size_bytes"] > 0

    def test_build_refuses_unsafe_path(self, tmp_git_repo, tmp_path: Path) -> None:
        aed_root, aed_commit = _tmp_aed_repo(tmp_path)
        cert = _good_cert(tmp_git_repo.head_full())
        kwargs = _builder_kwargs(
            aed_repo_root=aed_root,
            aed_source_commit=aed_commit,
        )
        kwargs["expected_head"] = tmp_git_repo.head_full()
        kwargs["file_paths_to_attach"] = ["../escape.py"]
        b = CandidateBuilder(**kwargs)
        with pytest.raises(CandidateError):
            b.build(cert, tmp_git_repo.root)

    def test_build_refuses_unsafe_head(self, tmp_git_repo, tmp_path: Path) -> None:
        aed_root, aed_commit = _tmp_aed_repo(tmp_path)
        cert = _good_cert(tmp_git_repo.head_full())
        kwargs = _builder_kwargs(
            aed_repo_root=aed_root,
            aed_source_commit=aed_commit,
        )
        kwargs["expected_head"] = "not_sha"
        kwargs["file_paths_to_attach"] = ["sample.py"]
        b = CandidateBuilder(**kwargs)
        with pytest.raises(CandidateError):
            b.build(cert, tmp_git_repo.root)

    def test_build_writes_files(self, tmp_git_repo, tmp_path: Path) -> None:
        aed_root, aed_commit = _tmp_aed_repo(tmp_path)
        import hashlib
        cert = _good_cert(tmp_git_repo.head_full())
        kwargs = _builder_kwargs(
            aed_repo_root=aed_root,
            aed_source_commit=aed_commit,
        )
        kwargs["expected_head"] = tmp_git_repo.head_full()
        kwargs["file_paths_to_attach"] = ["sample.py"]
        b = CandidateBuilder(**kwargs)
        cand = b.build(cert, tmp_git_repo.root)
        out = subprocess.check_output(
            ["git", "show", f"{tmp_git_repo.head_full()}:sample.py"],
            cwd=tmp_git_repo.root,
        )
        assert hashlib.sha256(out).hexdigest() == cand.source_files["sample.py"]["sha256"]


# === Input hash checks ===
class TestCandidateInputHashes:
    # Pre-canary round-281 §4: hermetic AED fixture; no skip.

    def test_input_hash_mismatch_rejected(
        self, tmp_git_repo, tmp_path: Path,
    ) -> None:
        aed_root, aed_commit = _tmp_aed_repo(tmp_path)
        cert = _good_cert(tmp_git_repo.head_full())
        kwargs = _builder_kwargs(
            aed_repo_root=aed_root,
            aed_source_commit=aed_commit,
        )
        kwargs["expected_head"] = tmp_git_repo.head_full()
        kwargs["file_paths_to_attach"] = ["sample.py"]
        kwargs["expected_input_hashes"] = {"file:sample.py": "z" * 64}
        b = CandidateBuilder(**kwargs)
        with pytest.raises(CandidateError):
            b.build(cert, tmp_git_repo.root)

    def test_input_hash_match_accepted(
        self, tmp_git_repo, tmp_path: Path,
    ) -> None:
        aed_root, aed_commit = _tmp_aed_repo(tmp_path)
        import hashlib
        cert = _good_cert(tmp_git_repo.head_full())
        out = subprocess.check_output(
            ["git", "show", f"{tmp_git_repo.head_full()}:sample.py"],
            cwd=tmp_git_repo.root,
        )
        actual = hashlib.sha256(out).hexdigest()
        kwargs = _builder_kwargs(
            aed_repo_root=aed_root,
            aed_source_commit=aed_commit,
        )
        kwargs["expected_head"] = tmp_git_repo.head_full()
        kwargs["file_paths_to_attach"] = ["sample.py"]
        kwargs["expected_input_hashes"] = {"file:sample.py": actual}
        b = CandidateBuilder(**kwargs)
        cand = b.build(cert, tmp_git_repo.root)
        assert cand is not None


# === Repository isolation ===
class TestRepoIsolation:
    # Pre-canary round-281 §4: hermetic AED fixture; no skip.

    """The candidate binds the exact head from a specific repository.
    Mixing repositories must fail."""

    def test_repo_owner_used_in_candidate(
        self, tmp_git_repo, tmp_path: Path,
    ) -> None:
        """When self.repo is "DifferentOwner/DifferentRepo" but the checkout
        is for "o/r", the build must fail with CandidateError.
        """
        aed_root, aed_commit = _tmp_aed_repo(tmp_path)
        from autocoder_orchestration.candidate import CandidateError
        cert = _good_cert(tmp_git_repo.head_full())
        kwargs = _builder_kwargs(
            aed_repo_root=aed_root,
            aed_source_commit=aed_commit,
        )
        kwargs["expected_head"] = tmp_git_repo.head_full()
        kwargs["file_paths_to_attach"] = ["sample.py"]
        kwargs["repo"] = "DifferentOwner/DifferentRepo"
        b = CandidateBuilder(**kwargs)
        with pytest.raises(CandidateError):
            b.build(cert, tmp_git_repo.root)

    def test_repo_owner_matched_in_candidate(
        self, tmp_git_repo, tmp_path: Path,
    ) -> None:
        """When self.repo matches the checkout origin, the build succeeds."""
        aed_root, aed_commit = _tmp_aed_repo(tmp_path)
        cert = _good_cert(tmp_git_repo.head_full())
        kwargs = _builder_kwargs(
            aed_repo_root=aed_root,
            aed_source_commit=aed_commit,
        )
        kwargs["expected_head"] = tmp_git_repo.head_full()
        kwargs["file_paths_to_attach"] = ["sample.py"]
        kwargs["repo"] = "o/r"
        b = CandidateBuilder(**kwargs)
        cand = b.build(cert, tmp_git_repo.root)
        assert cand.repo == "o/r"
