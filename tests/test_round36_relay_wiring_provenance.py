"""Round-36 integration test — guard the relay_wiring
``mark_head_advanced_public`` provenance check end-to-end.

The relay_wiring helper is the SINGLE point at which
``report_repair_pushed`` is invoked. The tests in this
file exercise the helper's guard logic with a stub
controller state so the test does NOT require the real
AutoDev orchestration controller to be running.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest


# We import via the package path so the supervisor's
# ``log`` symbol is resolvable when the helper imports it.
from autocoder_orchestration.worker_attempt import (
    LIFECYCLE_PUSH_VERIFIED,
    LIFECYCLE_TERMINAL_REPAIRED,
    LIFECYCLE_WORKER_EXITED_NO_PUSH,
    LIFECYCLE_WORKER_RUNNING,
    SCHEMA_VERSION,
    WorkerAttemptRecord,
    WorkerAttemptStore,
)


def _seed_attempt(
    *,
    store_dir: Path,
    attempt_id: str,
    lifecycle: str,
    prelaunch_head: str,
    pushed_commit_sha: str | None = None,
    github_head_verified: bool = False,
) -> WorkerAttemptRecord:
    rec = WorkerAttemptRecord(
        schema_version=SCHEMA_VERSION,
        attempt_id=attempt_id,
        claim_id="claim-xyz",
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        pr_number=5,
        event_ids=(),
        finding_ids=(),
        directive_digest="",
        directive_path="",
        prelaunch_head=prelaunch_head,
        expected_branch="feat/review-repair-relay-v1",
        pid=999_999,
        lease_id=attempt_id,
        started_at="2026-08-10T14:00:00Z",
        last_progress_at="2026-08-10T14:00:00Z",
        finished_at=None,
        lifecycle=lifecycle,
        attempt_count=1,
        stdout_path=None,
        stderr_path=None,
        exit_code=None,
        signal=None,
        result_artifact_path=None,
        produced_commit_sha=pushed_commit_sha,
        pushed_commit_sha=pushed_commit_sha,
        origin_head_verified=False,
        github_head_verified=github_head_verified,
        terminal_reason=None,
    )
    WorkerAttemptStore(store_dir).write(rec)
    return rec


def test_relay_wiring_helper_rejects_missing_attempt_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When ``attempt_id`` is None, the helper returns False
    without touching the controller."""
    from autocoder_supervisor import relay_wiring
    from autocoder_supervisor import supervisor as sup_mod
    # Monkey-patch the run-state resolver so the helper does
    # not need the real orchestration state root. The
    # missing-attempt-id guard runs BEFORE the state-root
    # resolution, so the resolver is never called.
    monkeypatch.setattr(sup_mod, "RUN_STATE", str(tmp_path / "rs.json"))
    result = relay_wiring.mark_head_advanced_public(
        old_head_sha="a" * 40,
        new_head_sha="b" * 40,
        attempt_id=None,
    )
    assert result is False


def test_relay_wiring_helper_rejects_unknown_attempt_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A bogus ``attempt_id`` returns False; the controller is
    not touched."""
    from autocoder_supervisor import relay_wiring
    from autocoder_supervisor import supervisor as sup_mod
    monkeypatch.setattr(sup_mod, "RUN_STATE", str(tmp_path / "rs.json"))
    result = relay_wiring.mark_head_advanced_public(
        old_head_sha="a" * 40,
        new_head_sha="b" * 40,
        attempt_id="att-does-not-exist",
    )
    assert result is False


def test_relay_wiring_helper_rejects_unverified_lifecycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An attempt whose lifecycle is WORKER_RUNNING (not yet
    PUSH_VERIFIED) cannot ack a head advance."""
    from autocoder_supervisor import relay_wiring
    from autocoder_supervisor import supervisor as sup_mod
    store_dir = tmp_path / "wa"
    store_dir.mkdir()
    from autocoder_orchestration import worker_attempt as wa_mod
    monkeypatch.setattr(wa_mod, "default_attempt_root", lambda: store_dir)
    _seed_attempt(
        store_dir=store_dir,
        attempt_id="att-running-1",
        lifecycle=LIFECYCLE_WORKER_RUNNING,
        prelaunch_head="a" * 40,
    )
    monkeypatch.setattr(sup_mod, "RUN_STATE", str(tmp_path / "rs.json"))
    result = relay_wiring.mark_head_advanced_public(
        old_head_sha="a" * 40,
        new_head_sha="b" * 40,
        attempt_id="att-running-1",
    )
    assert result is False


def test_relay_wiring_helper_rejects_dead_worker_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An attempt that has already transitioned to
    WORKER_EXITED_NO_PUSH MUST NOT ack a head advance — even
    if the branch head matches a produced_commit_sha. This is
    the false-ack guard."""
    from autocoder_supervisor import relay_wiring
    from autocoder_supervisor import supervisor as sup_mod
    store_dir = tmp_path / "wa"
    store_dir.mkdir()
    from autocoder_orchestration import worker_attempt as wa_mod
    monkeypatch.setattr(wa_mod, "default_attempt_root", lambda: store_dir)
    _seed_attempt(
        store_dir=store_dir,
        attempt_id="att-dead-1",
        lifecycle=LIFECYCLE_WORKER_EXITED_NO_PUSH,
        prelaunch_head="a" * 40,
        pushed_commit_sha="b" * 40,
        github_head_verified=False,
    )
    monkeypatch.setattr(sup_mod, "RUN_STATE", str(tmp_path / "rs.json"))
    result = relay_wiring.mark_head_advanced_public(
        old_head_sha="a" * 40,
        new_head_sha="b" * 40,
        attempt_id="att-dead-1",
    )
    assert result is False


def test_relay_wiring_helper_rejects_head_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An attempt with PUSH_VERIFIED lifecycle still cannot
    ack a head advance whose ``old_head_sha`` differs from the
    attempt's ``prelaunch_head``. The attempt's old head is
    fixed at launch time."""
    from autocoder_supervisor import relay_wiring
    from autocoder_supervisor import supervisor as sup_mod
    store_dir = tmp_path / "wa"
    store_dir.mkdir()
    from autocoder_orchestration import worker_attempt as wa_mod
    monkeypatch.setattr(wa_mod, "default_attempt_root", lambda: store_dir)
    _seed_attempt(
        store_dir=store_dir,
        attempt_id="att-prr-1",
        lifecycle=LIFECYCLE_PUSH_VERIFIED,
        prelaunch_head="a" * 40,
        pushed_commit_sha="b" * 40,
        github_head_verified=True,
    )
    monkeypatch.setattr(sup_mod, "RUN_STATE", str(tmp_path / "rs.json"))
    # Pass an unrelated ``old_head_sha`` — the helper must reject.
    result = relay_wiring.mark_head_advanced_public(
        old_head_sha="z" * 40,  # unrelated to prelaunch_head
        new_head_sha="b" * 40,
        attempt_id="att-prr-1",
    )
    assert result is False


def test_relay_wiring_helper_rejects_pushed_head_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An attempt whose ``pushed_commit_sha`` does NOT match
    the new head is rejected — even if the lifecycle is
    PUSH_VERIFIED. This prevents acking a manually-pushed
    unrelated commit as a worker push."""
    from autocoder_supervisor import relay_wiring
    from autocoder_supervisor import supervisor as sup_mod
    store_dir = tmp_path / "wa"
    store_dir.mkdir()
    from autocoder_orchestration import worker_attempt as wa_mod
    monkeypatch.setattr(wa_mod, "default_attempt_root", lambda: store_dir)
    _seed_attempt(
        store_dir=store_dir,
        attempt_id="att-pushed-1",
        lifecycle=LIFECYCLE_PUSH_VERIFIED,
        prelaunch_head="a" * 40,
        pushed_commit_sha="b" * 40,  # worker pushed b
        github_head_verified=True,
    )
    monkeypatch.setattr(sup_mod, "RUN_STATE", str(tmp_path / "rs.json"))
    # The live head is ``c``, NOT ``b`` — the helper must reject
    # even though the lifecycle says PUSH_VERIFIED.
    result = relay_wiring.mark_head_advanced_public(
        old_head_sha="a" * 40,
        new_head_sha="c" * 40,  # unrelated to pushed_commit_sha=b
        attempt_id="att-pushed-1",
    )
    assert result is False


def test_relay_wiring_helper_rejects_github_unverified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An attempt whose ``github_head_verified`` is False is
    rejected even if the head SHA matches."""
    from autocoder_supervisor import relay_wiring
    from autocoder_supervisor import supervisor as sup_mod
    store_dir = tmp_path / "wa"
    store_dir.mkdir()
    from autocoder_orchestration import worker_attempt as wa_mod
    monkeypatch.setattr(wa_mod, "default_attempt_root", lambda: store_dir)
    _seed_attempt(
        store_dir=store_dir,
        attempt_id="att-gh-1",
        lifecycle=LIFECYCLE_PUSH_VERIFIED,
        prelaunch_head="a" * 40,
        pushed_commit_sha="b" * 40,
        github_head_verified=False,  # not yet verified
    )
    monkeypatch.setattr(sup_mod, "RUN_STATE", str(tmp_path / "rs.json"))
    result = relay_wiring.mark_head_advanced_public(
        old_head_sha="a" * 40,
        new_head_sha="b" * 40,
        attempt_id="att-gh-1",
    )
    assert result is False
