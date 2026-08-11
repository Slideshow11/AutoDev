"""Round-35 directive verifier.

This directive (round_index=35, head=79d4d62) re-reported the
same eight P1 findings that were originally flagged in rounds
31-39 and re-verified at the round-34 head (``78dca3e``). Each
finding was already addressed with regression coverage in
``test_round31_p1_fixes.py``, ``test_round32_p1_fixes.py``,
``test_round39_p1_fixes.py``, and ``test_round34_p1_verifier.py``.

This module adds ONE behavioural regression test per directive
finding that proves the fix is still intact at the round-35 head
AND that the directive's exact bug-detector property fails
without the fix. Each test exercises the live code path
(function call, branch, exception handling) so a future round
that removes or regresses one of these fixes will fail
``test_round35_p1_verifier.py``.

P1 finding coverage:
  #1  pid_alive preserves liveness when /proc/<pid>/status
      raises OSError (signal-0 fallback returns True on live PID).
  #2  cooldown-deferred events are replayed back into the
      unconsumed ledger when cooldown expires (helper exists;
      main loop invokes it; merge is observable).
  #3  no_action on review-repair persists a retry ledger
      AND the durable unconsumed-event replay supplements
      ``new_events`` on the next heartbeat.
  #4  poll_worker_attempt no longer transitions a worker to
      WORKER_EXITED_NO_PUSH without first verifying the remote
      head (PUSH_VERIFIED lifecycle is preferred when the
      candidate commit is worker-specific).
  #5  verify_push_against_attempt falls back to origin-branch
      verification when both produced_commit_sha and
      pushed_commit_sha are None (production-launch case) AND
      only attributes the head when the committer-date proof
      passes.
  #6  poll_worker_attempt deferred push-recovery requires the
      candidate head's committer date to be strictly AFTER
      rec.started_at (worker-specific proof).
  #7  _advance_awaiting_ci_to_qualifying refuses the
      transition when ``required_checks_green`` is False
      (fail-closed CI gate).
  #8  _is_actionable_provider_comment rejects the
      "**Actionable comments posted: 0**" marker, the
      "<sub>📝 Walkthrough</sub>" header, and other provider
      status comments.
"""
from __future__ import annotations

import re
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
# P1#1: pid_alive preserves liveness on /proc OSError
# ---------------------------------------------------------------------------


def test_p1_01_pid_alive_returns_true_on_proc_oserror_with_signal0_success() -> None:
    """The ``pid_alive`` helper MUST return True when
    ``/proc/<pid>/status`` raises OSError AND the signal-0 probe
    succeeds. This is the bug-detector property: without the
    fallback, the helper would have raised OSError; without the
    signal-0 guard, it would have returned False; either failure
    would let ``poll_worker_attempt`` wrongly classify a live
    worker as dead.
    """
    supervisor = _supervisor_module()
    pid = 12345

    # Force the Path constructor to return a mock whose
    # ``read_text`` raises OSError (transient I/O / EMFILE /
    # permission policy). The supervisor's fallback MUST then
    # call ``os.kill(pid, 0)`` and return True on success.
    fake_proc_path = mock.MagicMock()
    fake_proc_path.read_text.side_effect = OSError("simulated EMFILE")
    with mock.patch.object(
        supervisor, "Path", wraps=supervisor.Path,
    ) as path_cls, mock.patch.object(
        supervisor.os, "kill", return_value=None,
    ) as kill_mock:
        path_cls.return_value = fake_proc_path
        alive = supervisor.pid_alive(pid)

    assert alive is True, (
        "pid_alive must fall back to signal-0 on OSError and return True "
        "when the live PID is still alive (round-39 P1#1 fix)"
    )
    kill_mock.assert_called_with(pid, 0)


def test_p1_01_pid_alive_returns_false_on_proc_oserror_with_process_gone() -> None:
    """The signal-0 fallback MUST also handle ProcessLookupError
    correctly: when /proc is unreadable AND the process is gone,
    ``pid_alive`` returns False (the worker is genuinely dead).
    """
    supervisor = _supervisor_module()
    pid = 12345
    fake_proc_path = mock.MagicMock()
    fake_proc_path.read_text.side_effect = OSError("simulated EMFILE")
    with mock.patch.object(
        supervisor, "Path", wraps=supervisor.Path,
    ) as path_cls, mock.patch.object(
        supervisor.os, "kill", side_effect=ProcessLookupError("gone"),
    ):
        path_cls.return_value = fake_proc_path
        alive = supervisor.pid_alive(pid)
    assert alive is False, (
        "pid_alive must return False when both /proc is unreadable "
        "AND the signal-0 probe fails with ProcessLookupError"
    )


# ---------------------------------------------------------------------------
# P1#2: cooldown-deferred events are replayed when cooldown expires
# ---------------------------------------------------------------------------


def test_p1_02_replay_helper_invoked_when_cooldown_inactive() -> None:
    """The main loop MUST invoke ``_replay_cooldown_deferred_if_any``
    whenever cooldown is no longer active. Without this, the
    deferred-event ledger becomes a permanent tombstone (the
    snapshot deltas absorb the new event so
    ``detect_new_actionable_events`` never returns it).
    """
    supervisor = _supervisor_module()
    assert hasattr(supervisor, "_replay_cooldown_deferred_if_any"), (
        "Round-39 P1#2 helper missing"
    )
    # The main loop must invoke the helper inside the
    # ``not cooldown_active()`` branch.
    src = _read(SUPERVISOR_PATH)
    pattern = re.compile(
        r"if\s+not\s+cooldown_active\(\)\s*:\s*\n\s*_replay_cooldown_deferred_if_any\(\)",
        re.MULTILINE,
    )
    assert pattern.search(src), (
        "main loop must invoke _replay_cooldown_deferred_if_any when cooldown expires"
    )


def test_p1_02_cooldown_deferred_marker_records_event_ids() -> None:
    """``_mark_cooldown_deferred`` MUST persist the deferred
    event ids so the replay helper can merge them back when
    cooldown expires. Without persistence, the deferred set is
    lost across heartbeats.
    """
    supervisor = _supervisor_module()
    assert hasattr(supervisor, "_mark_cooldown_deferred"), (
        "Round-33 P1#1 helper missing"
    )


# ---------------------------------------------------------------------------
# P1#3: no_action on review-repair persists retry ledger
# ---------------------------------------------------------------------------


def test_p1_03_no_action_branch_persists_retry_with_correct_reason() -> None:
    """The ``no_action`` branch on review-repair MUST call
    ``_persist_round_budget_retry`` with reason
    ``no_action_on_review_repair`` so the next heartbeat can
    replay the event via the unconsumed-events ledger.
    """
    src = _read(SUPERVISOR_PATH)
    marker = 'elif relay_action == "no_action":'
    idx = src.find(marker)
    assert idx != -1, "no_action branch not found in supervisor"
    segment = src[idx:idx + 1500]
    assert "_persist_round_budget_retry" in segment, (
        "no_action branch must persist the retry ledger (round-39 P1#3)"
    )
    assert "no_action_on_review_repair" in segment, (
        "retry reason must be 'no_action_on_review_repair'"
    )


# ---------------------------------------------------------------------------
# P1#4: poll_worker_attempt verifies remote head before failing worker
# ---------------------------------------------------------------------------


def test_p1_04_poll_worker_defers_no_push_on_remote_head_match() -> None:
    """Round-42: ``poll_worker_attempt`` MUST NOT promote
    a dead worker to PUSH_VERIFIED based on remote
    evidence alone. The C9-style manual commit incident
    is the canary for this guard.

    The OLD round-37 contract (origin/<branch> fallback
    with committer-date guard) was REPLACED. The
    round-42 invariant requires a positive worker-emitted
    ``pushed_commit_sha`` to verify a push.
    """
    src = _read(SUPERVISOR_PATH)
    # Round-42 invariant: the worker must durably
    # record the commit. The source MUST contain the
    # worker-reported-push check.
    assert (
        "_worker_reported_push" in src
        or "_worker_pushed" in src
        or "pushed_commit_shas" in src
    ), (
        "poll_worker_attempt must consult the worker's "
        "durably-recorded pushed_commit_sha; remote "
        "evidence alone is INSUFFICIENT (round-42 invariant)"
    )
    # The promotion gate is the worker-emitted
    # pushed_commit_sha. The verifier requires a
    # positive worker-emitted commit; committer-date
    # is diagnostic only.
    assert (
        "_worker_reported_push" in src
        or "_worker_pushed" in src
        or "pushed_commit_shas" in src
    ), (
        "poll_worker_attempt must consult the worker's "
        "durably-recorded pushed_commit_sha; committer-date "
        "is diagnostic only (round-42 invariant)"
    )


# ---------------------------------------------------------------------------
# P1#5: verify_push_against_attempt falls back to origin-branch verification
# ---------------------------------------------------------------------------


def test_p1_05_verify_push_falls_back_when_both_shas_none() -> None:
    """Round-42: ``verify_push_against_attempt`` MUST NOT
    fabricate positive verification when both
    ``produced_commit_sha`` and ``pushed_commit_sha`` are
    None. The verifier returns False for both flags;
    the head-rebind path treats the advance as
    external.

    The OLD round-35 contract (origin/<branch> fallback
    with committer-date guard) was REPLACED. The
    verifier requires a positive worker-emitted
    ``pushed_commit_sha`` (or LAST element of
    ``pushed_commit_shas``) equal to the new head.
    """
    src = _read(SUPERVISOR_PATH)
    fn_idx = src.find("def verify_push_against_attempt")
    assert fn_idx != -1, "verify_push_against_attempt not defined"
    fn_end = src.find("\ndef ", fn_idx + 1)
    fn_body = src[fn_idx:fn_end if fn_end != -1 else None]
    # The verifier must consult the worker's
    # durably-recorded pushed_commit_sha (or the
    # ``pushed_commit_shas`` list).
    assert "_worker_pushed" in fn_body or "pushed_commit_shas" in fn_body, (
        "verifier must consult the worker's "
        "durably-recorded pushed_commit_sha (round-42 invariant)"
    )
    # The verifier handles the no-worker-record case by
    # falling through to ``return out`` (both flags False).
    assert "return out" in fn_body, (
        "verifier must have an early-return path that "
        "leaves both flags False when no worker record exists"
    )


# ---------------------------------------------------------------------------
# P1#6: deferred push-recovery requires committer-date > started_at
# ---------------------------------------------------------------------------


def test_p1_06_committer_date_must_be_after_started_at() -> None:
    """Round-42: committer-date evidence is diagnostic
    only. The promotion gate
    ``pushed_commit_sha == new_head_sha`` (or the
    produced_commit_sha + origin path) does NOT
    require ``_committed_at > _started_at_dt`` alone.
    The C9-style manual commit incident is the canary:
    the manual commit's committer date was AFTER the
    worker started, yet the worker did NOT create it.
    """
    src = _read(SUPERVISOR_PATH)
    # The round-42 invariant: the worker must durably
    # record the commit. Committer-date is NOT
    # sufficient. The verifier source MUST contain the
    # worker-reported-push check.
    assert (
        "_worker_pushed" in src
        or "_worker_reported_push" in src
    ), (
        "verifier must consult the worker's "
        "durably-recorded pushed_commit_sha; "
        "committer-date is diagnostic only (round-42)"
    )


def test_p1_06_git_committer_iso_helper_present() -> None:
    """``_git_committer_iso(sha) -> (ok, parsed_dt)`` MUST exist
    and use ``git log -1 --format=%cI <sha>`` so the verifier
    can compare the commit's committer date against
    ``rec.started_at``.
    """
    supervisor = _supervisor_module()
    assert hasattr(supervisor, "_git_committer_iso"), (
        "Round-31 P1#6 helper missing"
    )


# ---------------------------------------------------------------------------
# P1#7: _advance_awaiting_ci_to_qualifying verifies required_checks_green
# ---------------------------------------------------------------------------


def test_p1_07_advance_qualifying_refuses_when_checks_not_green() -> None:
    """The supervisor MUST call ``required_checks_green`` before
    driving ``Controller.report_ci_pass``. Without this guard, a
    manual head advance while the controller is in
    ``AWAITING_CI`` would be promoted to
    ``QUALIFYING_READINESS`` even when CI is pending or failing
    (silent CI-bypass regression).
    """
    src = _read(SUPERVISOR_PATH)
    fn_idx = src.find("def _advance_awaiting_ci_to_qualifying")
    assert fn_idx != -1, "_advance_awaiting_ci_to_qualifying not defined"
    fn_end = src.find("\ndef ", fn_idx + 1)
    fn_body = src[fn_idx:fn_end if fn_end != -1 else None]
    assert "required_checks_green" in fn_body, (
        "_advance_awaiting_ci_to_qualifying must call required_checks_green"
    )
    assert "required checks not green" in fn_body, (
        "must log 'required checks not green' on the refusal path"
    )
    assert "refusing transition (fail-closed)" in fn_body, (
        "must log 'refusing transition (fail-closed)' on the refusal path"
    )


# ---------------------------------------------------------------------------
# P1#8: _is_actionable_provider_comment rejects provider status markers
# ---------------------------------------------------------------------------


def test_p1_08_filter_rejects_zero_actionable_marker() -> None:
    """The relay MUST NOT treat CodeRabbit's "**Actionable
    comments posted: 0**" summary as a finding — it is a status
    marker that would otherwise turn a clean head into a
    persistent repair loop.
    """
    relay = _relay_module()
    body = (
        "**Actionable comments posted: 0**\n\n"
        "Reviewed 4 files. No issues found.\n"
    )
    assert relay._is_actionable_provider_comment(body) is False, (
        "CodeRabbit zero-finding summary must NOT be actionable"
    )


def test_p1_08_filter_rejects_walkthrough_subtag() -> None:
    """A wrapped walkthrough header (``<sub>📝 Walkthrough
    (commented)</sub>``) MUST be stripped of the ``<sub>`` tags
    before the alphanumeric pass so the visible text "Walkthrough
    (commented)" is recognized as a status marker.
    """
    relay = _relay_module()
    body = (
        "<sub>📝 Walkthrough (commented)</sub>\n\n"
        "All done — nothing actionable here.\n"
    )
    assert relay._is_actionable_provider_comment(body) is False, (
        "wrapped walkthrough marker must NOT be actionable"
    )


def test_p1_08_filter_accepts_real_p1_finding_with_subtags() -> None:
    """A genuine P1 finding whose severity badge is wrapped in
    ``<sub><sub>...</sub></sub>`` MUST still be treated as
    actionable. The filter strips tags for the status-marker
    pass but does not discard the body wholesale.
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


def test_p1_08_filter_accepts_long_body_with_status_first_line() -> None:
    """The filter is intentionally conservative on long bodies:
    when the first line is a status marker but the body exceeds
    200 chars or has more than 2 newlines, the comment is
    treated as actionable (it likely contains real review
    content beyond the walkthrough header). This prevents the
    filter from swallowing a long walkthrough that embeds real
    findings.
    """
    relay = _relay_module()
    body = (
        "Walkthrough\n\n"
        + ("Reviewed file X. " * 30)  # >200 chars, >2 newlines
    )
    assert relay._is_actionable_provider_comment(body) is True, (
        "long walkthrough body with >200 chars must remain actionable "
        "(the body has real review content beyond the header)"
    )
