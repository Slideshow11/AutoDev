"""Tests for autocoder_orchestration.context.RunContext."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from autocoder_orchestration import (
    RunContext,
    make_run_context,
    generate_run_id,
    ACTOR_CONTROLLER,
    ACTOR_IMPL_WORKER,
    ACTOR_VERIFIER,
    ACTOR_HUMAN,
    ACTOR_OBSERVER,
    ACTOR_CANDIDATE_BUILDER,
)
from autocoder_orchestration.context import (
    SCHEMA_VERSION,
    ALL_ACTORS,
    _check_path,
)


# === Helpers ===
def _dummy_context(**overrides):
    base = dict(
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        local_checkout="/tmp/AutoDev",
        base_branch="main",
        authorized_base_sha="a" * 64,
        feature_branch="feat/test",
        task_specification_path="/tmp/AutoDev/task.md",
        task_specification_sha256="b" * 64,
        required_ci_jobs=("test (3.10)", "test (3.11)", "test (3.12)"),
        implementation_worker_command=("/usr/bin/env", "true"),
        evidence_root="/tmp/evidence",
        state_root="/tmp/state",
    )
    base.update(overrides)
    return make_run_context(**base)


# === Construction ===
class TestRunContextConstruction:
    def test_minimal_context_constructs(self) -> None:
        ctx = _dummy_context()
        assert ctx.schema_version == SCHEMA_VERSION
        assert ctx.run_id and isinstance(ctx.run_id, str)
        assert ctx.repo_owner == "Slideshow11"
        assert ctx.repo_name == "AutoDev"

    def test_invalid_repo_owner_rejected(self) -> None:
        with pytest.raises(ValueError, match="repo_owner"):
            _dummy_context(repo_owner="bad/owner")

    def test_invalid_repo_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="repo_name"):
            _dummy_context(repo_name="bad/name")

    def test_invalid_base_sha_rejected(self) -> None:
        with pytest.raises(ValueError, match="authorized_base_sha"):
            _dummy_context(authorized_base_sha="not_sha")

    def test_empty_task_spec_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="task_specification_path"):
            _dummy_context(task_specification_path="")

    def test_invalid_task_spec_sha_rejected(self) -> None:
        with pytest.raises(ValueError, match="task_specification_sha256"):
            _dummy_context(task_specification_sha256="bad")

    def test_command_must_be_tuple_of_strings(self) -> None:
        with pytest.raises(ValueError, match="implementation_worker_command"):
            _dummy_context(implementation_worker_command=["valid", 123])

    def test_command_strings_required(self) -> None:
        with pytest.raises(ValueError, match="implementation_worker_command"):
            _dummy_context(implementation_worker_command=("string", 123))

    def test_absolute_path_required(self) -> None:
        with pytest.raises(ValueError, match="must be absolute"):
            _dummy_context(local_checkout="relative/path")


# === Round-trip ===
class TestRunContextRoundtrip:
    def test_to_dict_from_dict_roundtrip(self) -> None:
        ctx = _dummy_context()
        d = ctx.to_dict()
        restored = RunContext.from_dict(d)
        assert restored.run_id == ctx.run_id
        assert restored.repo_owner == ctx.repo_owner
        assert restored.repo_name == ctx.repo_name
        assert restored.feature_branch == ctx.feature_branch
        assert restored.task_specification_sha256 == ctx.task_specification_sha256
        assert restored.required_ci_jobs == ctx.required_ci_jobs

    def test_invalid_schema_rejected(self) -> None:
        with pytest.raises(ValueError, match="unsupported run context schema"):
            RunContext.from_dict({})

    def test_invalid_schema_version_rejected(self) -> None:
        with pytest.raises(ValueError, match="schema"):
            RunContext.from_dict({"schema_version": "wrong"})

    def test_sha256_stable(self) -> None:
        ctx = _dummy_context()
        sha1 = ctx.sha256()
        sha2 = ctx.sha256()
        assert sha1 == sha2
        assert len(sha1) == 64


# === Path promotion ===
class TestPathPromotion:
    def test_pending_path_uses_pending_in_path(self) -> None:
        ctx = _dummy_context()
        assert "pending" in ctx.state_path
        assert "pending" in ctx.evidence_path

    def test_pr_scoped_path_after_promotion(self) -> None:
        ctx = _dummy_context()
        promoted = ctx.with_promoted_pr(pr_number=42, pr_head="c" * 64)
        assert promoted.pr_number == 42
        assert promoted.current_authorized_head == "c" * 64
        assert "pr-42" in promoted.state_path
        assert "pr-42" in promoted.evidence_path

    def test_invalid_pr_number_rejected(self) -> None:
        ctx = _dummy_context()
        with pytest.raises(ValueError, match="pr_number"):
            ctx.with_promoted_pr(pr_number=0, pr_head="c" * 64)
        with pytest.raises(ValueError, match="pr_number"):
            ctx.with_promoted_pr(pr_number=-1, pr_head="c" * 64)

    def test_invalid_pr_head_rejected(self) -> None:
        ctx = _dummy_context()
        with pytest.raises(ValueError, match="pr_head"):
            ctx.with_promoted_pr(pr_number=42, pr_head="not_sha")


class TestHeadUpdate:
    def test_new_head_preserves_all_other_fields(self) -> None:
        ctx = _dummy_context()
        promoted = ctx.with_promoted_pr(pr_number=42, pr_head="c" * 64)
        updated = promoted.with_new_head("d" * 64)
        assert updated.current_authorized_head == "d" * 64
        assert updated.pr_number == 42
        assert updated.repo_owner == promoted.repo_owner


class TestActorSet:
    def test_all_actor_ids_distinct(self) -> None:
        assert len(ALL_ACTORS) == 6

    def test_documented_actors_present(self) -> None:
        for actor in [
            ACTOR_CONTROLLER,
            ACTOR_IMPL_WORKER,
            ACTOR_VERIFIER,
            ACTOR_HUMAN,
            ACTOR_OBSERVER,
            ACTOR_CANDIDATE_BUILDER,
        ]:
            assert actor in ALL_ACTORS


class TestRunId:
    def test_generated_run_id_is_unique(self) -> None:
        ids = {generate_run_id() for _ in range(20)}
        assert len(ids) == 20

    def test_generated_run_id_format(self) -> None:
        rid = generate_run_id()
        assert isinstance(rid, str)
        assert len(rid) >= 16
