"""Tests for autocoder_orchestration.verifier_handoff and merge_authorization."""
from __future__ import annotations

import pytest

from autocoder_orchestration import (
    VerifierHandoff,
    MergeAuthorization,
)
from autocoder_orchestration.verifier_handoff import (
    write_handoff,
    read_handoff,
    VerifierRoleGuard,
)
from autocoder_orchestration.merge_authorization import (
    MergeExecutor,
)
from autocoder_orchestration.store import (
    StateStore,
    ProcessIdentity,
    current_process_identity,
)


HEAD = "a9501bae8fd0c449be6bb4d57bcf006a8d833474"
H1 = HEAD
H2 = "b" * 40


def _handoff() -> VerifierHandoff:
    return VerifierHandoff(
        schema_version="autocoder.verifier_handoff.v1",
        run_id="r1",
        repo="o/r",
        pr_number=2,
        exact_head=H1,
        base_sha="a79bb613a70db3d3bd659a5c8985ffcfc0835984",
        base_branch="main",
        task_specification_sha256="b" * 64,
        candidate_path="candidate.json",
        candidate_sha256="c" * 64,
        readiness_certificate_id="cert-1",
        observation_log_path="observations.jsonl",
        observation_log_sha256="d" * 64,
        strict_window_first_utc="2026-08-05T22:00:00Z",
        strict_window_last_utc="2026-08-05T22:03:00Z",
        strict_window_observation_count=12,
        strict_window_duration_monotonic=185.0,
        controller_state_revision=1,
        controller_state_path="state.json",
        trusted_verifier_source_commit="z" * 64,
        trusted_verifier_package_version="autocoder-orchestration-1.0.0",
        implementation_worker_identity=None,
        verifier_record_path="verifier-record.json",
        created_at="2026-08-05T22:05:00Z",
    )


# === Handoff ===
class TestHandoff:
    def test_handoff_constructs(self) -> None:
        h = _handoff()
        assert h.run_id == "r1"

    def test_handoff_roundtrip(self) -> None:
        h = _handoff()
        d = h.to_dict()
        restored = VerifierHandoff.from_dict(d)
        assert restored.run_id == h.run_id
        assert restored.exact_head == h.exact_head

    def test_handoff_sha256_stable(self) -> None:
        h = _handoff()
        sha1 = h.compute_sha256()
        sha2 = h.compute_sha256()
        assert sha1 == sha2


# === Write + read handoff ===
class TestHandoffStore:
    def test_write_and_read_handoff(self, tmp_path) -> None:
        # tmp_path is a pytest fixture for a fresh temp directory
        from autocoder_orchestration.store import StateStore
        store = StateStore(str(tmp_path / "state"))
        h = _handoff()
        write_handoff(store, h)
        restored = read_handoff(store)
        assert restored is not None
        assert restored.run_id == h.run_id

    def test_read_missing_handoff_returns_none(self, tmp_path) -> None:
        from autocoder_orchestration.store import StateStore
        store = StateStore(str(tmp_path / "state"))
        assert read_handoff(store) is None


# === Verifier role guard ===
class TestVerifierRoleGuard:
    def test_rejects_same_process_identity(self, tmp_path) -> None:
        from autocoder_orchestration.store import StateStore
        store = StateStore(str(tmp_path / "state"))
        h = _handoff()
        import dataclasses
        h2 = dataclasses.replace(
            h,
            implementation_worker_identity={"pid": 42, "start_id": "abc"},
        )
        write_handoff(store, h2)
        guard = VerifierRoleGuard(h2, store)
        ok, reason = guard.validate(
            verifier_identity=ProcessIdentity(pid=42, start_id="abc"),
            verifier_executable_path=None,
            write_credentials_present=False,
        )
        assert not ok
        assert "process identity" in reason

    def test_accepts_different_process_identity(self, tmp_path) -> None:
        from autocoder_orchestration.store import StateStore
        store = StateStore(str(tmp_path / "state"))
        h = _handoff()
        guard = VerifierRoleGuard(h, store)
        ok, reason = guard.validate(
            verifier_identity=ProcessIdentity(pid=42, start_id="abc"),
            verifier_executable_path=None,
            write_credentials_present=False,
        )
        assert ok, reason

    def test_rejects_write_credentials(self, tmp_path) -> None:
        from autocoder_orchestration.store import StateStore
        store = StateStore(str(tmp_path / "state"))
        h = _handoff()
        guard = VerifierRoleGuard(h, store)
        ok, reason = guard.validate(
            verifier_identity=ProcessIdentity(pid=42, start_id="abc"),
            verifier_executable_path=None,
            write_credentials_present=True,
        )
        assert not ok
        assert "write credentials" in reason

    def test_rejects_executable_in_target_checkout(self, tmp_path) -> None:
        from autocoder_orchestration.store import StateStore
        store = StateStore(str(tmp_path / "state"))
        # Write a run_context.json with local_checkout
        run_context = {
            "schema_version": "autocoder.run_context.v1",
            "run_id": "r1",
            "created_at": "2026-08-05T22:00:00Z",
            "repo_owner": "o",
            "repo_name": "r",
            "local_checkout": str(tmp_path),
            "base_branch": "main",
            "authorized_base_sha": "a" * 64,
            "feature_branch": "feat/test",
            "pr_number": 2,
            "current_authorized_head": H1,
            "task_specification_path": "/tmp/task",
            "task_specification_sha256": "b" * 64,
            "required_ci_jobs": ["test"],
            "reviewer_policy": "exact_head_approval",
            "quiet_window_seconds": 180,
            "implementation_worker_command": [],
            "verifier_command": None,
            "verifier_handoff_policy": "fresh_session_required",
            "permitted_mutations": [],
            "human_only_actions": [],
            "evidence_root": "/tmp/evidence",
            "state_root": "/tmp/state",
            "next_wave_policy": "explicit_only",
        }
        store.write_atomic("run_context.json", run_context)
        h = _handoff()
        guard = VerifierRoleGuard(h, store)
        ok, reason = guard.validate(
            verifier_identity=ProcessIdentity(pid=42, start_id="abc"),
            verifier_executable_path=str(tmp_path / "scripts" / "verifier"),
            write_credentials_present=False,
        )
        assert not ok
        assert "target branch" in reason


# === Merge authorization ===
class TestMergeAuthorization:
    def test_authorization_constructs(self) -> None:
        auth = MergeAuthorization(
            schema_version="autocoder.merge_authorization.v1",
            run_id="r1",
            repo="o/r",
            pr_number=2,
            authorized_head=H1,
            candidate_sha256="c" * 64,
            verifier_record_sha256="d" * 64,
            feature_branch="feat/test",
            )
        assert auth.merge_method == "squash"

    def test_invalid_head_rejected(self) -> None:
        with pytest.raises(ValueError):
            MergeAuthorization(
                schema_version="autocoder.merge_authorization.v1",
                run_id="r1",
                repo="o/r",
                pr_number=2,
                authorized_head="bad",
                candidate_sha256="c" * 64,
                verifier_record_sha256="d" * 64,
            feature_branch="feat/test",
            )
    def test_invalid_pr_number_rejected(self) -> None:
        with pytest.raises(ValueError):
            MergeAuthorization(
                schema_version="autocoder.merge_authorization.v1",
                run_id="r1",
                repo="o/r",
                pr_number=0,
                authorized_head=H1,
                candidate_sha256="c" * 64,
                verifier_record_sha256="d" * 64,
            feature_branch="feat/test",
            )
    def test_invalid_method_rejected(self) -> None:
        with pytest.raises(ValueError):
            MergeAuthorization(
                schema_version="autocoder.merge_authorization.v1",
                run_id="r1",
                repo="o/r",
                pr_number=2,
                authorized_head=H1,
                candidate_sha256="c" * 64,
                verifier_record_sha256="d" * 64,
                merge_method="invalid",
            feature_branch="feat/test",
            )
    def test_authorization_roundtrip(self) -> None:
        auth = MergeAuthorization(
            schema_version="autocoder.merge_authorization.v1",
            run_id="r1",
            repo="o/r",
            pr_number=2,
            authorized_head=H1,
            candidate_sha256="c" * 64,
            verifier_record_sha256="d" * 64,
            next_wave_authorization={"next_wave_id": "wave-2"},
            feature_branch="feat/test",
            )
        d = auth.to_dict()
        restored = MergeAuthorization.from_dict(d)
        assert restored.run_id == auth.run_id
        assert restored.next_wave_authorization == {"next_wave_id": "wave-2"}


# === Merge executor ===
class TestMergeExecutor:
    def test_compute_command(self) -> None:
        auth = MergeAuthorization(
            schema_version="autocoder.merge_authorization.v1",
            run_id="r1",
            repo="o/r",
            pr_number=2,
            authorized_head=H1,
            candidate_sha256="c" * 64,
            verifier_record_sha256="d" * 64,
            feature_branch="feat/test",
            )
        exec = MergeExecutor()
        cmd = exec.compute_command(auth)
        assert cmd[0] == "gh"
        assert cmd[1] == "pr"
        assert cmd[2] == "merge"
        assert "2" in cmd
        assert "--squash" in cmd
        assert "--delete-branch" in cmd
        assert "--match-head-commit" in cmd
        assert H1 in cmd

    def test_refuses_admin(self) -> None:
        auth = MergeAuthorization(
            schema_version="autocoder.merge_authorization.v1",
            run_id="r1",
            repo="o/r",
            pr_number=2,
            authorized_head=H1,
            candidate_sha256="c" * 64,
            verifier_record_sha256="d" * 64,
            feature_branch="feat/test",
            )
        exec = MergeExecutor()
        with pytest.raises(Exception, match="admin"):
            exec.compute_command(auth, allow_extra_flags={"admin": True})

    def test_refuses_auto(self) -> None:
        auth = MergeAuthorization(
            schema_version="autocoder.merge_authorization.v1",
            run_id="r1",
            repo="o/r",
            pr_number=2,
            authorized_head=H1,
            candidate_sha256="c" * 64,
            verifier_record_sha256="d" * 64,
            feature_branch="feat/test",
            )
        exec = MergeExecutor()
        with pytest.raises(Exception, match="[Aa]uto"):
            exec.compute_command(auth, allow_extra_flags={"auto": True})

    def test_refuses_merge_commit(self) -> None:
        auth = MergeAuthorization(
            schema_version="autocoder.merge_authorization.v1",
            run_id="r1",
            repo="o/r",
            pr_number=2,
            authorized_head=H1,
            candidate_sha256="c" * 64,
            verifier_record_sha256="d" * 64,
            feature_branch="feat/test",
            )
        exec = MergeExecutor()
        with pytest.raises(Exception, match="[Mm]erge commit"):
            exec.compute_command(auth, allow_extra_flags={"merge": True})

    def test_refuses_rebase(self) -> None:
        auth = MergeAuthorization(
            schema_version="autocoder.merge_authorization.v1",
            run_id="r1",
            repo="o/r",
            pr_number=2,
            authorized_head=H1,
            candidate_sha256="c" * 64,
            verifier_record_sha256="d" * 64,
            feature_branch="feat/test",
            )
        exec = MergeExecutor()
        with pytest.raises(Exception, match="[Rr]ebase"):
            exec.compute_command(auth, allow_extra_flags={"rebase": True})

    def test_refuses_no_match_head_commit(self) -> None:
        auth = MergeAuthorization(
            schema_version="autocoder.merge_authorization.v1",
            run_id="r1",
            repo="o/r",
            pr_number=2,
            authorized_head=H1,
            candidate_sha256="c" * 64,
            verifier_record_sha256="d" * 64,
            require_match_head_commit=False,
        feature_branch="feat/test",
            )
        exec = MergeExecutor()
        with pytest.raises(Exception, match="match_head_commit"):
            exec.compute_command(auth)

    def test_refuses_merge_method_not_approved(self) -> None:
        # If merge_method is "rebase" or "merge", the executor refuses
        # because only "squash" is approved.
        with pytest.raises(Exception):
            MergeAuthorization(
                schema_version="autocoder.merge_authorization.v1",
                run_id="r1",
                repo="o/r",
                pr_number=2,
                authorized_head=H1,
                candidate_sha256="c" * 64,
                verifier_record_sha256="d" * 64,
                merge_method="rebase",
            feature_branch="feat/test",
            )