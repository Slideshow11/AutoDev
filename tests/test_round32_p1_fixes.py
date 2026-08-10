"""Round-32 P1 regression tests.

This directive (round_index=32, head=c1ebc46) was generated
by the relay against the round-31 head. Several of the eight
P1 findings have already been addressed by subsequent rounds
(round-31 P1#7 CI-gated advance, round-39 P1#1-5 liveness /
replay / provenance / dispatch, round-31 P1#6 committer-date
in ``verify_push_against_attempt``). The two P1 findings that
are NOT yet covered are:

  P1#6 — ``poll_worker_attempt`` deferred push-recovery
          branch MUST also require committer-date worker-
          specific proof (matching the round-31 P1#6 contract
          in ``verify_push_against_attempt``).
  P1#8 — ``_is_actionable_provider_comment`` MUST filter
          provider status comments whose only content is a
          generic status marker; the existing filter catches
          walkthrough / in-progress / completion but a normal
          CodeRabbit header like "**Actionable comments
          posted: 0**" still slips through as P2.

Each test exercises the production fix in isolation. The
suite is hermetic: subprocess invocations are stubbed so the
tests run offline.

P1 finding coverage:
  #6  poll_worker_attempt deferred push recovery requires
      committer-date > started_at (worker-specific proof)
  #8  provider status-marker comments do not become findings
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from autocoder_orchestration.worker_attempt import (
    LIFECYCLE_PUSH_VERIFIED,
    LIFECYCLE_WORKER_EXITED_NO_PUSH,
    LIFECYCLE_WORKER_RUNNING,
    SCHEMA_VERSION,
    WorkerAttemptRecord,
    WorkerAttemptStore,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _seed_attempt(
    *,
    store_dir: Path,
    attempt_id: str,
    lifecycle: str,
    prelaunch_head: str,
    expected_branch: str = "feat/test",
    event_ids: tuple = (),
    pushed_commit_sha: str | None = None,
    produced_commit_sha: str | None = None,
    github_head_verified: bool = False,
    origin_head_verified: bool = False,
    started_at: str = "2026-08-10T00:00:00Z",
) -> WorkerAttemptRecord:
    rec = WorkerAttemptRecord(
        schema_version=SCHEMA_VERSION,
        attempt_id=attempt_id,
        claim_id=f"claim-{attempt_id}",
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        pr_number=5,
        event_ids=event_ids,
        finding_ids=(),
        directive_digest="",
        directive_path="",
        prelaunch_head=prelaunch_head,
        expected_branch=expected_branch,
        pid=os.getpid(),
        lease_id=attempt_id,
        started_at=started_at,
        last_progress_at=started_at,
        finished_at=None,
        lifecycle=lifecycle,
        attempt_count=1,
        stdout_path=None,
        stderr_path=None,
        exit_code=None,
        signal=None,
        result_artifact_path=None,
        produced_commit_sha=produced_commit_sha,
        pushed_commit_sha=pushed_commit_sha,
        origin_head_verified=origin_head_verified,
        github_head_verified=github_head_verified,
        terminal_reason=None,
    )
    WorkerAttemptStore(store_dir).write(rec)
    return rec


def _make_pr_payload(head_sha: str) -> dict:
    return {
        "head": {"sha": head_sha},
        "mergeable": True,
    }


# ---------------------------------------------------------------------------
# P1#6: poll_worker_attempt deferred push recovery requires committer-date
# ---------------------------------------------------------------------------


def test_p1_06_poll_worker_rejects_external_actor_push_before_started_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-32 P1#6: ``poll_worker_attempt`` deferred
    push-recovery branch MUST require the candidate head's
    committer date to be strictly AFTER ``rec.started_at`` —
    the same worker-specific proof that round-31 P1#6 added
    to ``verify_push_against_attempt``. Without this guard, an
    external actor's push (commit time before the worker
    launched, but matching ``origin/<branch>``) is wrongly
    attributed to this attempt and the worker is promoted to
    PUSH_VERIFIED, leaving the controller stuck on a
    fraudulent repair.
    """
    from autocoder_supervisor import supervisor as sup

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    worker_attempts_dir = state / "worker_attempts"
    monkeypatch.setattr(sup, "STATE_DIR", state, raising=False)
    monkeypatch.setattr(
        sup, "WORKER_ATTEMPTS_DIR", worker_attempts_dir, raising=False,
    )
    # ``pid_alive`` MUST return False so the deferred
    # push-recovery branch runs.
    monkeypatch.setattr(sup, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(sup, "_reap_worker", lambda _pid: (1, None))

    prelaunch_head = "a" * 40
    new_head = "b" * 40

    _seed_attempt(
        store_dir=worker_attempts_dir,
        attempt_id="att-round32-p1-6",
        lifecycle=LIFECYCLE_WORKER_RUNNING,
        prelaunch_head=prelaunch_head,
        expected_branch="feat/test-branch",
        started_at="2026-08-10T00:00:00Z",
    )

    # Stub the GitHub PR probe and the git ``origin`` rev-parse
    # so the deferred-recovery branch's two preconditions pass:
    # (a) live head != prelaunch_head; (b) origin/<branch> ==
    # live head. The third precondition — committer date AFTER
    # ``started_at`` — is the one under test; we mock it to be
    # BEFORE ``started_at`` (1 minute earlier) so a correct
    # implementation MUST reject the push.
    def _fake_github_get(path: str, token: str = "") -> dict | None:
        if "/pulls/" in path and "/reviews" not in path:
            return _make_pr_payload(new_head)
        return None

    monkeypatch.setattr(sup, "github_get", _fake_github_get)
    monkeypatch.setattr(sup, "get_github_token", lambda: "fake-token")

    class _SubResult:
        def __init__(self, stdout: str = "") -> None:
            self.stdout = stdout
            self.returncode = 0

    def _fake_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "rev-parse" in cmd:
            return _SubResult(new_head)
        return _SubResult("")

    monkeypatch.setattr(sup.subprocess, "run", _fake_run)

    # ``git log -1 --format=%cI <sha>`` → 1 minute BEFORE
    # the worker's ``started_at`` (external actor pushed this
    # commit before the worker was launched).
    class _CheckOutput:
        def __init__(self, _out: str) -> None:
            self._out = _out

        def strip(self) -> str:
            return self._out

    def _fake_check_output(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "log" in cmd:
            return _CheckOutput("2026-08-09T23:59:00+00:00")
        return _CheckOutput("")

    monkeypatch.setattr(sup.subprocess, "check_output", _fake_check_output)

    lease = {
        "attempt_id": "att-round32-p1-6",
        "pid": os.getpid(),
        "pgid": os.getpid(),
        "last_dispatched_event_id": "",
        "start_time_evidence": {},
    }

    sup.poll_worker_attempt(
        attempt_id="att-round32-p1-6",
        lease=lease,
    )

    # Reload the record — the deferred-recovery branch must
    # NOT have promoted the attempt to PUSH_VERIFIED because
    # the committer date precedes ``started_at``.
    store = sup._worker_attempt_store()
    rec = store.read("att-round32-p1-6")
    assert rec is not None
    assert rec.lifecycle != LIFECYCLE_PUSH_VERIFIED, (
        "round-32 P1#6: poll_worker_attempt MUST NOT promote "
        "the attempt to PUSH_VERIFIED when the candidate "
        "head's committer date is BEFORE rec.started_at — "
        "that is the signature of an external actor's push "
        "and the worker-specific proof contract is violated."
    )


def test_p1_06_poll_worker_accepts_worker_push_after_started_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion to the rejection test: when the candidate
    head's committer date is AFTER ``rec.started_at`` the
    deferred-recovery branch MUST promote the attempt to
    PUSH_VERIFIED (the round-31 contract still works for
    genuine worker pushes).
    """
    from autocoder_supervisor import supervisor as sup

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    worker_attempts_dir = state / "worker_attempts"
    monkeypatch.setattr(sup, "STATE_DIR", state, raising=False)
    monkeypatch.setattr(
        sup, "WORKER_ATTEMPTS_DIR", worker_attempts_dir, raising=False,
    )
    monkeypatch.setattr(sup, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(sup, "_reap_worker", lambda _pid: (0, None))

    prelaunch_head = "a" * 40
    new_head = "b" * 40

    _seed_attempt(
        store_dir=worker_attempts_dir,
        attempt_id="att-round32-p1-6-accept",
        lifecycle=LIFECYCLE_WORKER_RUNNING,
        prelaunch_head=prelaunch_head,
        expected_branch="feat/test-branch",
        started_at="2026-08-10T00:00:00Z",
    )

    def _fake_github_get(path: str, token: str = "") -> dict | None:
        if "/pulls/" in path and "/reviews" not in path:
            return _make_pr_payload(new_head)
        return None

    monkeypatch.setattr(sup, "github_get", _fake_github_get)
    monkeypatch.setattr(sup, "get_github_token", lambda: "fake-token")

    class _SubResult:
        def __init__(self, stdout: str = "") -> None:
            self.stdout = stdout
            self.returncode = 0

    def _fake_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "rev-parse" in cmd:
            return _SubResult(new_head)
        return _SubResult("")

    monkeypatch.setattr(sup.subprocess, "run", _fake_run)

    class _CheckOutput:
        def __init__(self, _out: str) -> None:
            self._out = _out

        def strip(self) -> str:
            return self._out

    def _fake_check_output(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "log" in cmd:
            # 1 minute AFTER the worker's started_at.
            return _CheckOutput("2026-08-10T00:01:00+00:00")
        return _CheckOutput("")

    monkeypatch.setattr(sup.subprocess, "check_output", _fake_check_output)

    lease = {
        "attempt_id": "att-round32-p1-6-accept",
        "pid": os.getpid(),
        "pgid": os.getpid(),
        "last_dispatched_event_id": "",
        "start_time_evidence": {},
    }

    sup.poll_worker_attempt(
        attempt_id="att-round32-p1-6-accept",
        lease=lease,
    )

    store = sup._worker_attempt_store()
    rec = store.read("att-round32-p1-6-accept")
    assert rec is not None
    assert rec.lifecycle == LIFECYCLE_PUSH_VERIFIED, (
        "round-32 P1#6: poll_worker_attempt MUST promote a "
        "genuine worker push (committer date AFTER "
        "rec.started_at) to PUSH_VERIFIED so the head-rebind "
        "path can route the advance through "
        "mark_head_advanced_public."
    )


# ---------------------------------------------------------------------------
# P1#8: provider status-marker comments are not findings
# ---------------------------------------------------------------------------


def test_p1_08_actionable_comments_posted_zero_is_not_a_finding() -> None:
    """Round-32 P1#8: a CodeRabbit status comment that
    reports zero actionable findings (``**Actionable
    comments posted: 0**``) MUST NOT be turned into a
    finding. The previous filter
    (``_NON_FINDING_COMMENT_RE``) only matched walkthrough /
    in-progress / completion markers, so a normal CodeRabbit
    status header was classified as a P2 finding and kept
    the relay in a persistent repair loop on a clean head.
    """
    from autocoder_orchestration.review_repair_relay import (
        _is_actionable_provider_comment,
    )

    body = (
        "**Actionable comments posted: 0**\n\n"
        "Reviewing files changed since last review.\n"
    )
    assert _is_actionable_provider_comment(body) is False, (
        "round-32 P1#8: a CodeRabbit status comment whose "
        "only intent is reporting zero actionable findings "
        "MUST NOT be treated as an actionable provider "
        "comment. The relay would otherwise dispatch a "
        "worker to fix a non-existent finding."
    )


def test_p1_08_review_request_footer_is_not_a_finding() -> None:
    """Round-32 P1#8: CodeRabbit's ``<sub>📝 ...</sub>``
    style header / footer that wraps the actual review
    findings MUST NOT be classified as a finding on its
    own. The previous filter matched only the literal
    status-marker tokens; a one-line header like
    ``📝 Walkthrough (commented)`` slipped through as P2.
    """
    from autocoder_orchestration.review_repair_relay import (
        _is_actionable_provider_comment,
    )

    body = (
        "<sub>📝 Walkthrough (commented)</sub>\n\n"
        "No actionable comments were posted in this review.\n"
    )
    # The status-marker branch already catches ``walkthrough``
    # at the first line; this test guards the related
    # ``commented``-only variant.
    assert _is_actionable_provider_comment(body) is False, (
        "round-32 P1#8: a CodeRabbit header that contains "
        "only a status-marker token MUST be filtered out so "
        "the relay does not treat it as a P2 finding."
    )


def test_p1_08_real_p1_finding_still_actionable() -> None:
    """Companion to the filter tests: a real P1 finding with
    a file/line anchor MUST remain actionable after the
    filter is tightened. Without this guard, the new filter
    could regress on real findings by swallowing the
    heading line.
    """
    from autocoder_orchestration.review_repair_relay import (
        _is_actionable_provider_comment,
    )

    body = (
        "**P1** Preserve liveness when proc status is unreadable\n\n"
        "When `/proc/<pid>/status` exists but cannot be read "
        "because of a transient I/O error, `Path.read_text()` "
        "raises an `OSError`, and this branch classifies the "
        "live worker as dead without using the historical "
        "signal-0 fallback. `poll_worker_attempt()` can then "
        "terminalize the attempt, release its lease, and launch "
        "a duplicate worker.\n\n"
        "Suggested fix: autocoder_supervisor/supervisor.py:1022\n"
    )
    assert _is_actionable_provider_comment(body) is True, (
        "round-32 P1#8: a real P1 finding (severity marker + "
        "long body + file:line anchor) MUST remain actionable. "
        "Tightening the status-marker filter MUST NOT regress "
        "on legitimate findings."
    )
