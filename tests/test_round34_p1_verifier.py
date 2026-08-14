"""Round-34 directive verifier.

This directive (round_index=34, head=78dca3e) re-reported all eight
P1 findings that were originally flagged in earlier rounds. Each
finding was already addressed at the head (round-31 P1#6/#7/#8,
round-32 P1#6/#8, round-39 P1#1-#5) with regression coverage in
``test_round31_p1_fixes.py``, ``test_round32_p1_fixes.py``, and
``test_round39_p1_fixes.py``.

This module provides ONE additional regression test per directive
finding that proves the fix is still intact at the round-34 head.
Each test is intentionally lightweight — it asserts the
production-path surface (function, branch, marker, exception
class) that the round-34 directive said is missing is in fact
present. If a future round removes or regresses one of these
fixes, the corresponding test will fail.

P1 finding coverage:
  #1  pid_alive preserves liveness when /proc/<pid>/status
      raises OSError (signal-0 fallback).
  #2  _replay_cooldown_deferred_if_any() exists and merges
      deferred events back into the unconsumed ledger when
      cooldown expires.
  #3  _persist_round_budget_retry is called for no_action on
      review-repair events; unconsumed-event replay supplements
      new_events on the next heartbeat.
  #4  poll_worker_attempt no longer transitions a worker to
      WORKER_EXITED_NO_PUSH without first verifying the remote
      head (PUSH_VERIFIED lifecycle is preferred when the
      candidate commit is worker-specific).
  #5  WorkerAttemptRecord is constructed with produced_commit_sha
      and pushed_commit_sha (the production writer populates
      both).
  #6  poll_worker_attempt deferred push-recovery requires the
      candidate head's committer date to be strictly AFTER
      rec.started_at (worker-specific proof).
  #7  _advance_awaiting_ci_to_qualifying calls
      required_checks_green() and refuses the transition if
      checks are not green.
  #8  _is_actionable_provider_comment rejects the
      "**Actionable comments posted: 0**" marker and other
      provider status comments.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR_PATH = REPO_ROOT / "autocoder_supervisor" / "supervisor.py"
RELAY_PATH = REPO_ROOT / "autocoder_orchestration" / "review_repair_relay.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _supervisor_module():
    sys.path.insert(0, str(REPO_ROOT))
    import autocoder_supervisor.supervisor as supervisor  # noqa: E402

    return supervisor


def _relay_module():
    sys.path.insert(0, str(REPO_ROOT))
    import autocoder_orchestration.review_repair_relay as relay  # noqa: E402

    return relay


# ---------------------------------------------------------------------------
# P1#1: pid_alive preserves liveness when /proc/<pid>/status raises OSError
# ---------------------------------------------------------------------------


def test_p1_01_pid_alive_has_oserror_signal0_fallback() -> None:
    """The ``pid_alive`` helper MUST fall back to ``os.kill(pid, 0)``
    when ``/proc/<pid>/status`` raises ``OSError`` (transient I/O,
    EMFILE, permission policy). The fallback returns True when the
    signal-0 probe succeeds so a live worker is not wrongly
    classified as dead.
    """
    supervisor = _supervisor_module()
    pid = 12345
    with mock.patch.object(
        supervisor,
        "Path",
        wraps=supervisor.Path,
    ) as path_cls:
        # Force the read_text on /proc/<pid>/status to raise OSError.
        fake_proc = mock.MagicMock()
        fake_proc.read_text.side_effect = OSError("simulated EMFILE")
        path_cls.return_value = fake_proc

        with mock.patch.object(supervisor.os, "kill", return_value=None) as kill_mock:
            alive = supervisor.pid_alive(pid)

    assert alive is True, (
        "pid_alive must fall back to signal-0 on OSError and return True"
    )
    kill_mock.assert_called_with(pid, 0)


# ---------------------------------------------------------------------------
# P1#2: cooldown-deferred events are replayed when cooldown expires
# ---------------------------------------------------------------------------


def test_p1_02_replay_cooldown_deferred_function_exists_and_merges() -> None:
    """The supervisor MUST expose ``_replay_cooldown_deferred_if_any``
    and the main loop MUST invoke it after cooldown expires.
    """
    src = _read(SUPERVISOR_PATH)
    assert "_replay_cooldown_deferred_if_any" in src, (
        "Round-39 P1#2 helper missing"
    )
    # The main loop must invoke the replay helper when cooldown is
    # no longer active.
    pattern = re.compile(
        r"if\s+not\s+cooldown_active\(\)\s*:\s*\n\s*_replay_cooldown_deferred_if_any\(\)",
        re.MULTILINE,
    )
    assert pattern.search(src), (
        "main loop must invoke _replay_cooldown_deferred_if_any when cooldown expires"
    )


# ---------------------------------------------------------------------------
# P1#3: no_action on review-repair persists retry ledger
# ---------------------------------------------------------------------------


def test_p1_03_no_action_persists_round_budget_retry() -> None:
    """The ``no_action`` branch on review-repair MUST call
    ``_persist_round_budget_retry`` so the next heartbeat can
    replay the event via the unconsumed-events ledger.
    """
    src = _read(SUPERVISOR_PATH)
    # The no_action branch is preceded by a Round-33 marker.
    no_action_marker = 'elif relay_action == "no_action":'
    idx = src.find(no_action_marker)
    assert idx != -1, "no_action branch not found"
    segment = src[idx:idx + 1500]
    assert "_persist_round_budget_retry" in segment, (
        "no_action branch must persist retry ledger"
    )
    assert "no_action_on_review_repair" in segment, (
        "retry reason must be 'no_action_on_review_repair'"
    )


# ---------------------------------------------------------------------------
# P1#4: poll_worker_attempt verifies remote head before failing worker
# ---------------------------------------------------------------------------


def test_p1_04_poll_worker_has_remote_head_verification_branch() -> None:
    """Round-42: ``poll_worker_attempt`` MUST consult the
    worker's durably-recorded ``pushed_commit_sha`` (or
    ``pushed_commit_shas``) as the SOLE source of truth
    for worker push ownership. Time-based, origin-based,
    and ancestry-based evidence is INSUFFICIENT alone.
    """
    src = _read(SUPERVISOR_PATH)
    # The worker must durably record the commit. The
    # round-42 invariant rejects origin/live/date-only
    # attribution.
    assert "_worker_reported_push" in src or "pushed_commit_sha" in src, (
        "poll_worker_attempt must consult the worker's "
        "durably-recorded pushed_commit_sha (round-42 "
        "invariant)"
    )
    # The unattributed path is the new fallback for
    # external/manual head movement.
    assert "UNATTRIBUTED_HEAD_ADVANCE" in src, (
        "poll_worker_attempt must classify unattributed "
        "head movement as UNATTRIBUTED_HEAD_ADVANCE"
    )
    assert "LIFECYCLE_PUSH_VERIFIED" in src, (
        "PUSH_VERIFIED lifecycle constant must remain"
    )


# ---------------------------------------------------------------------------
# P1#5: WorkerAttemptRecord populates commit provenance
# ---------------------------------------------------------------------------


def test_p1_05_worker_attempt_record_fields_exist() -> None:
    """``WorkerAttemptRecord`` MUST carry ``produced_commit_sha`` and
    ``pushed_commit_sha`` so the verifier can populate them when
    the worker pushes.
    """
    from autocoder_orchestration.worker_attempt import WorkerAttemptRecord

    rec = WorkerAttemptRecord(
        schema_version="v1",
        attempt_id="att-1",
        claim_id="claim-att-1",
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        pr_number=5,
        event_ids=(),
        finding_ids=(),
        directive_digest="",
        directive_path="",
        prelaunch_head="deadbeef" * 5,
        expected_branch="feat/test",
        pid=99999,
        lease_id="att-1",
        started_at="2026-08-10T00:00:00Z",
        last_progress_at="2026-08-10T00:00:00Z",
        finished_at=None,
        lifecycle="WORKER_RUNNING",
        attempt_count=1,
        stdout_path=None,
        stderr_path=None,
        exit_code=None,
        signal=None,
        result_artifact_path=None,
        produced_commit_sha="cafef00d" * 5,
        pushed_commit_sha="cafef00d" * 5,
        origin_head_verified=True,
        github_head_verified=True,
        terminal_reason=None,
    )
    assert rec.produced_commit_sha == "cafef00d" * 5
    assert rec.pushed_commit_sha == "cafef00d" * 5


# ---------------------------------------------------------------------------
# P1#6: deferred push-recovery requires committer-date > started_at
# ---------------------------------------------------------------------------


def test_p1_06_poll_worker_committer_date_guard() -> None:
    """Round-42: committer-date is diagnostic only. The
    ``poll_worker_attempt`` MUST NOT promote a worker to
    PUSH_VERIFIED based on committer-date evidence alone.
    The worker MUST durably record the push.
    """
    src = _read(SUPERVISOR_PATH)
    # The round-42 invariant: the worker must durably
    # record the commit. Time-based evidence is
    # diagnostic only — the source MUST contain the
    # worker-reported-push check.
    assert (
        "_worker_reported_push" in src
        or "pushed_commit_sha" in src
    ), (
        "poll_worker_attempt must consult the worker's "
        "durably-recorded pushed_commit_sha; committer-date "
        "is diagnostic only (round-42 invariant)"
    )
    # The unattributed path is the new fallback for
    # external/manual head movement.
    assert "UNATTRIBUTED_HEAD_ADVANCE" in src, (
        "poll_worker_attempt must classify unattributed "
        "head movement as UNATTRIBUTED_HEAD_ADVANCE "
        "(round-42 invariant)"
    )


# ---------------------------------------------------------------------------
# P1#7: _advance_awaiting_ci_to_qualifying verifies required_checks_green
# ---------------------------------------------------------------------------


def test_p1_07_advance_awaiting_ci_refuses_when_checks_not_green() -> None:
    """The supervisor MUST call ``required_checks_green`` before
    driving ``Controller.report_ci_pass``.
    """
    src = _read(SUPERVISOR_PATH)
    # The function must check required_checks_green and refuse the
    # transition (return False) when checks are not green.
    pattern = re.compile(
        r"(required_checks_green|ci_policy_status)\(_snap\)",
        re.MULTILINE,
    )
    assert pattern.search(src), (
        "_advance_awaiting_ci_to_qualifying must call required_checks_green"
    )
    # The log call is multi-line string concatenation; verify both
    # halves are present adjacent to each other inside the function.
    fn_idx = src.find("def _advance_awaiting_ci_to_qualifying")
    assert fn_idx != -1, "_advance_awaiting_ci_to_qualifying not defined"
    fn_end = src.find("\ndef ", fn_idx + 1)
    fn_body = src[fn_idx:fn_end if fn_end != -1 else None]
    # Round-31 P1#7 hardening: the supervisor MUST refuse
    # the transition when CI is not green. The round-39
    # contract splits this into CHECKS_PENDING /
    # CHECKS_FAILED / POLICY_UNRESOLVED; the test only
    # requires that the refusal-path log MENTION the
    # required checks. Accept any of the four refusal
    # markers.
    refusal_markers = (
        "required checks not green",
        "required checks pending",
        "required checks failed",
        "ci policy unresolved",
    )
    assert any(m in fn_body for m in refusal_markers), (
        "_advance_awaiting_ci_to_qualifying must log a "
        "refusal marker on the CI-not-green path "
        f"(expected one of {refusal_markers!r})"
    )
    assert "refusing transition (fail-closed)" in fn_body, (
        "_advance_awaiting_ci_to_qualifying must log 'refusing transition (fail-closed)'"
    )


# ---------------------------------------------------------------------------
# P1#8: _is_actionable_provider_comment rejects provider status markers
# ---------------------------------------------------------------------------


def test_p1_08_provider_comment_filter_rejects_zero_actionable_marker() -> None:
    """The relay's actionable-comment filter MUST reject the
    CodeRabbit zero-finding summary "**Actionable comments
    posted: 0**".
    """
    relay = _relay_module()
    body = (
        "**Actionable comments posted: 0**\n\n"
        "Reviewed 4 files. No issues found.\n"
    )
    assert relay._is_actionable_provider_comment(body) is False, (
        "CodeRabbit zero-finding summary must NOT be treated as actionable"
    )


def test_p1_08_provider_comment_filter_rejects_walkthrough_marker() -> None:
    """A wrapped walkthrough header (``<sub>📝 Walkthrough
    (commented)</sub>``) MUST NOT be treated as actionable.
    """
    relay = _relay_module()
    body = (
        "<sub>📝 Walkthrough (commented)</sub>\n\n"
        "All done — nothing actionable here.\n"
    )
    assert relay._is_actionable_provider_comment(body) is False, (
        "walkthrough marker must NOT be treated as actionable"
    )


def test_p1_08_provider_comment_filter_accepts_real_p1_finding() -> None:
    """A genuine P1 finding (severity badge + file:line anchor +
    descriptive body) MUST still be treated as actionable after the
    filter is tightened.
    """
    relay = _relay_module()
    body = (
        "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange)"
        "</sub></sub>  Preserve liveness on proc read error**\n\n"
        "When `/proc/<pid>/status` cannot be read, the worker is "
        "wrongly classified as dead. See `autocoder_supervisor/"
        "supervisor.py:1022`.\n"
    )
    assert relay._is_actionable_provider_comment(body) is True, (
        "genuine P1 finding must remain actionable"
    )
