"""Round-31 P1 regression tests.

Each test exercises the production fix for one of the three
fresh P1 findings from the round-31 directive (commit
7e09b44, fixture ba3db08f-fd40-4327-afdf-f4b24f476b88). The
tests are hermetic: no subprocess invocation against the live
hermes CLI or the live GitHub API; subprocess calls are stubbed
so the suite runs offline.

P1 finding coverage:
  #6  verify_push_against_attempt requires committer-date
      strictly AFTER attempt.started_at so external-actor
      pushes are NOT attributed to this attempt.
  #7  _advance_awaiting_ci_to_qualifying refuses the
      AWAITING_CI -> QUALIFYING_READINESS transition when
      required CI checks are not green.
  #8  launch_worker identity guard parses the GitHub remote
      URL into canonical ``{owner}/{repo}`` segments and
      compares them EXACTLY (no substring match).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from autocoder_orchestration.worker_attempt import (
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
        origin_head_verified=False,
        github_head_verified=False,
        terminal_reason=None,
    )
    WorkerAttemptStore(store_dir).write(rec)
    return rec


# ---------------------------------------------------------------------------
# P1#8: identity guard parses {owner}/{repo} exactly
# ---------------------------------------------------------------------------


def test_p1_08_parse_github_remote_identity_ssh_form() -> None:
    """SSH form: ``git@github.com:owner/repo.git``."""
    from autocoder_supervisor.supervisor import (
        _parse_github_remote_identity,
    )

    out = _parse_github_remote_identity(
        "git@github.com:Slideshow11/AutoDev.git"
    )
    assert out == {"owner": "Slideshow11", "repo": "AutoDev"}


def test_p1_08_parse_github_remote_identity_https_form() -> None:
    """HTTPS form: ``https://github.com/owner/repo.git``."""
    from autocoder_supervisor.supervisor import (
        _parse_github_remote_identity,
    )

    out = _parse_github_remote_identity(
        "https://github.com/Slideshow11/AutoDev.git"
    )
    assert out == {"owner": "Slideshow11", "repo": "AutoDev"}


def test_p1_08_parse_github_remote_identity_https_no_dotgit() -> None:
    """HTTPS form without ``.git`` suffix."""
    from autocoder_supervisor.supervisor import (
        _parse_github_remote_identity,
    )

    out = _parse_github_remote_identity(
        "https://github.com/Slideshow11/AutoDev"
    )
    assert out == {"owner": "Slideshow11", "repo": "AutoDev"}


def test_p1_08_parse_rejects_hyphenated_substring_attack() -> None:
    """Round-31 P1#8: the substring attack
    ``git@github.com:evil/Slideshow11-AutoDev.git`` MUST be
    rejected. The previous round-37 substring match would have
    accepted it because the URL contains the substring
    ``slideshow11-autodev``.
    """
    from autocoder_supervisor.supervisor import (
        _parse_github_remote_identity,
    )

    out = _parse_github_remote_identity(
        "git@github.com:evil/Slideshow11-AutoDev.git"
    )
    assert out == {"owner": "evil", "repo": "Slideshow11-AutoDev"}, (
        "the parser MUST distinguish the repo segment from the "
        "owner; the hyphenated URL is its own identity."
    )
    # The downstream guard must reject it: owner "evil" is
    # not the expected "Slideshow11" regardless of how the
    # repo string happens to contain the expected substring.
    assert out["owner"] != "Slideshow11"


def test_p1_08_parse_rejects_case_mismatched_owner() -> None:
    """A misspelled / case-mismatched owner like
    ``slideshow12/AutoDev`` MUST be parsed distinctly so the
    identity guard catches the mismatch.
    """
    from autocoder_supervisor.supervisor import (
        _parse_github_remote_identity,
    )

    out = _parse_github_remote_identity(
        "git@github.com:slideshow12/AutoDev.git"
    )
    # The owner segment is case-preserving; the guard uses
    # case-INSENSITIVE compare on owner only. But the
    # *parsed* form must remain ``slideshow12``, NOT the
    # expected ``Slideshow11``.
    assert out == {"owner": "slideshow12", "repo": "AutoDev"}


def test_p1_08_parse_rejects_non_github_host() -> None:
    """A non-GitHub host (e.g. ``gitlab.com``) MUST return
    empty segments so the identity guard refuses the launch.
    """
    from autocoder_supervisor.supervisor import (
        _parse_github_remote_identity,
    )

    out = _parse_github_remote_identity(
        "git@gitlab.com:Slideshow11/AutoDev.git"
    )
    assert out == {"owner": "", "repo": ""}


def test_p1_08_parse_rejects_unrecognised_form() -> None:
    """A local / malformed URL MUST return empty segments."""
    from autocoder_supervisor.supervisor import (
        _parse_github_remote_identity,
    )

    out = _parse_github_remote_identity("/some/local/path")
    assert out == {"owner": "", "repo": ""}
    out = _parse_github_remote_identity("")
    assert out == {"owner": "", "repo": ""}


def test_p1_08_launch_worker_identity_guard_rejects_substring_attack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: ``launch_worker`` MUST refuse to spawn a
    worker when the origin remote is the substring-attack
    form. The previous round-37 substring match would have
    accepted it; the round-31 parser rejects it.
    """
    from autocoder_supervisor import supervisor as sup

    monkeypatch.setattr(sup, "REPO_OWNER", "Slideshow11", raising=False)
    monkeypatch.setattr(sup, "REPO_NAME", "AutoDev", raising=False)
    monkeypatch.setattr(sup, "PR_NUMBER", 5, raising=False)
    monkeypatch.setattr(sup, "RUN_STATE", tmp_path / "rs.json", raising=False)
    monkeypatch.setattr(sup, "REPO_DIR", tmp_path, raising=False)
    monkeypatch.setattr(sup, "AED_SKIP_IDENTITY_GUARD", "0", raising=False)
    monkeypatch.delenv("AED_SKIP_IDENTITY_GUARD", raising=False)
    monkeypatch.setenv("AED_BRANCH", "feat/test")

    class _FakeRun:
        def __init__(self, _stdout: str) -> None:
            self.stdout = _stdout
            self.stderr = ""

    def _fake_subprocess_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "remote" in cmd and "get-url" in cmd:
            return _FakeRun(
                "git@github.com:evil/Slideshow11-AutoDev.git"
            )
        if isinstance(cmd, list) and "rev-parse" in cmd and "HEAD" in cmd:
            return _FakeRun("feat/test")
        return _FakeRun("")

    monkeypatch.setattr(
        "autocoder_supervisor.supervisor.subprocess.run",
        _fake_subprocess_run,
    )

    # ``launch_worker`` MUST return ``None`` (refused) when
    # the remote identity does not match.
    out = sup.launch_worker({}, {})
    assert out is None, (
        "round-31 P1#8: launch_worker MUST refuse to spawn "
        "when the origin remote URL's parsed owner/repo does "
        "not match the expected Slideshow11/AutoDev exactly."
    )


# ---------------------------------------------------------------------------
# P1#6: verify_push_against_attempt requires committer-date > started_at
# ---------------------------------------------------------------------------


def test_p1_06_verify_rejects_external_actor_push_before_started_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-31 P1#6: when ``origin/<branch> == new_head_sha``
    but the commit's committer date is BEFORE
    ``rec.started_at``, the verifier MUST treat the head as
    external — i.e. both ``origin_head_verified`` and
    ``github_head_verified`` remain False. The previous
    round-39 fallback would have attributed the push to
    this attempt on origin equality alone, which is unsafe.
    """
    from autocoder_supervisor import supervisor as sup

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    worker_attempts_dir = state / "worker_attempts"
    monkeypatch.setattr(sup, "STATE_DIR", state, raising=False)
    monkeypatch.setattr(
        sup, "WORKER_ATTEMPTS_DIR", worker_attempts_dir, raising=False,
    )

    # Seed: attempt started at ``2026-08-10T00:00:00Z``.
    _seed_attempt(
        store_dir=worker_attempts_dir,
        attempt_id="att-round31-p1-6",
        lifecycle=LIFECYCLE_WORKER_RUNNING,
        prelaunch_head="a" * 40,
        expected_branch="feat/test-branch",
        started_at="2026-08-10T00:00:00Z",
    )

    new_head = "b" * 40

    class _R:
        def __init__(self, _out: str) -> None:
            self._out = _out

        def strip(self) -> str:
            return self._out

    def _fake_check_output(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        # ``git rev-parse origin/<branch>`` → matches new_head.
        if isinstance(cmd, list) and "rev-parse" in cmd:
            return _R(new_head)
        # ``git log -1 --format=%cI <sha>`` → 1 minute BEFORE
        # the worker's ``started_at`` (an external actor
        # pushed this commit before the worker was launched).
        if isinstance(cmd, list) and "log" in cmd:
            return _R("2026-08-09T23:59:00+00:00")
        return _R("")

    monkeypatch.setattr(
        "autocoder_supervisor.supervisor.subprocess.check_output",
        _fake_check_output,
    )

    out = sup.verify_push_against_attempt(
        attempt_id="att-round31-p1-6",
        new_head_sha=new_head,
    )
    assert out is not None
    assert out.get("origin_head_verified") is False, (
        "round-31 P1#6: the verifier MUST NOT mark the push "
        "as origin-verified when the committer date is BEFORE "
        "the worker's started_at — that is the signature of an "
        "external actor's push."
    )
    assert out.get("github_head_verified") is False, (
        "round-31 P1#6: github_head_verified is gated on "
        "origin_head_verified and must also remain False."
    )


def test_p1_06_verify_accepts_push_after_started_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion to the rejection test: when the committer
    date is AFTER ``rec.started_at``, the verifier MUST
    mark the push as verified (the round-39 origin
    fallthrough still works for genuine worker pushes).
    """
    from autocoder_supervisor import supervisor as sup

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    worker_attempts_dir = state / "worker_attempts"
    monkeypatch.setattr(sup, "STATE_DIR", state, raising=False)
    monkeypatch.setattr(
        sup, "WORKER_ATTEMPTS_DIR", worker_attempts_dir, raising=False,
    )

    _seed_attempt(
        store_dir=worker_attempts_dir,
        attempt_id="att-round31-p1-6-accept",
        lifecycle=LIFECYCLE_WORKER_RUNNING,
        prelaunch_head="a" * 40,
        expected_branch="feat/test-branch",
        started_at="2026-08-10T00:00:00Z",
    )

    new_head = "b" * 40

    class _R:
        def __init__(self, _out: str) -> None:
            self._out = _out

        def strip(self) -> str:
            return self._out

    def _fake_check_output(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "rev-parse" in cmd:
            return _R(new_head)
        # Committer date 1 minute AFTER started_at.
        if isinstance(cmd, list) and "log" in cmd:
            return _R("2026-08-10T00:01:00+00:00")
        return _R("")

    monkeypatch.setattr(
        "autocoder_supervisor.supervisor.subprocess.check_output",
        _fake_check_output,
    )

    out = sup.verify_push_against_attempt(
        attempt_id="att-round31-p1-6-accept",
        new_head_sha=new_head,
    )
    assert out is not None
    assert out.get("origin_head_verified") is True
    assert out.get("github_head_verified") is True


# ---------------------------------------------------------------------------
# P1#7: _advance_awaiting_ci_to_qualifying verifies CI before transitioning
# ---------------------------------------------------------------------------


def test_p1_07_advance_refuses_when_required_checks_not_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-31 P1#7: ``_advance_awaiting_ci_to_qualifying``
    MUST refuse the AWAITING_CI -> QUALIFYING_READINESS
    transition when ``required_checks_green(snap)`` is
    False (any check pending, failing, or absent). The
    previous implementation called
    ``Controller.report_ci_pass`` unconditionally, which
    bypassed the CI gate and recorded a phantom
    QUALIFYING_READINESS.
    """
    from autocoder_supervisor import supervisor as sup

    # Configure the policy: at least one required check.
    monkeypatch.setattr(
        sup, "POLICY",
        {
            "required_check_names": ["CI"],
            "required_review_providers_for_pr_416": [],
        },
        raising=False,
    )

    # Stub out the controller side so we do not need real
    # orchestration state. The supervisor MUST consult
    # ``required_checks_green`` BEFORE invoking
    # ``Controller.report_ci_pass``.
    report_calls: list = []

    class _FakeStateMachine:
        current_state = "AWAITING_CI"

    class _FakeController:
        def __init__(self, **_kwargs: Any) -> None:
            self.loaded = True

        def load_state_machine(self) -> _FakeStateMachine:
            return _FakeStateMachine()

        def report_ci_pass(self, *, head_observed: str) -> None:
            report_calls.append(head_observed)

    monkeypatch.setattr(
        "autocoder_orchestration.controller.Controller",
        _FakeController,
        raising=False,
    )

    # Snapshot: required check is ``in_progress`` (not green).
    snap = {
        "head_sha": "b" * 40,
        "head_match": True,
        "formal_reviews": [],
        "review_threads": {},
        "issue_comments": [],
        "required_checks": {
            "CI": {
                "status": "in_progress",
                "conclusion": None,
                "run_id": "",
            },
        },
        "providers": {},
        "_provider_issue_comments": {},
        "unconsumed_event_ids": [],
        "provider_surfaces": {},
        "review_comments": [],
        "provider_surface_complete": True,
    }

    class _FakeStore:
        def __init__(self, _root: Any) -> None:
            pass

        def read_optional(self, _name: str) -> dict:
            return {"current_authorized_head": "b" * 40}

    monkeypatch.setattr(
        "autocoder_orchestration.store.StateStore", _FakeStore,
        raising=False,
    )
    monkeypatch.setattr(
        "autocoder_orchestration.context.RunContext",
        type("RC", (), {"from_dict": staticmethod(lambda d: d)}),
        raising=False,
    )

    monkeypatch.setattr(sup, "capture_live_snapshot", lambda *a, **k: snap)
    monkeypatch.setattr(sup, "get_github_token", lambda: "tok")

    # ``read_run_state`` MUST return a non-empty dict so the
    # snapshot path is reachable.
    monkeypatch.setattr(
        sup, "read_run_state",
        lambda: {"current_head": "b" * 40},
    )

    # The orchestration state root MUST resolve.
    monkeypatch.setattr(
        sup, "resolve_orchestration_state_root",
        lambda **_kw: tmp_path,
    )
    monkeypatch.setattr(
        "autocoder_supervisor.orchestration_state_root.resolve_orchestration_state_root",
        lambda **_kw: tmp_path,
        raising=False,
    )

    out = sup._advance_awaiting_ci_to_qualifying()
    assert out is False, (
        "round-31 P1#7: the supervisor MUST refuse the "
        "AWAITING_CI -> QUALIFYING_READINESS transition when "
        "required CI checks are not green."
    )
    assert report_calls == [], (
        "round-31 P1#7: Controller.report_ci_pass MUST NOT be "
        "called when the CI gate is not green."
    )


def test_p1_07_advance_allows_when_required_checks_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion: when the required CI checks ARE green,
    the transition MUST be allowed.
    """
    from autocoder_supervisor import supervisor as sup

    monkeypatch.setattr(
        sup, "POLICY",
        {
            "required_check_names": ["CI"],
            "required_review_providers_for_pr_416": [],
        },
        raising=False,
    )

    report_calls: list = []

    class _FakeStateMachine:
        current_state = "AWAITING_CI"

    class _FakeController:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def load_state_machine(self) -> _FakeStateMachine:
            return _FakeStateMachine()

        def report_ci_pass(self, *, head_observed: str) -> None:
            report_calls.append(head_observed)

    monkeypatch.setattr(
        "autocoder_orchestration.controller.Controller",
        _FakeController,
        raising=False,
    )

    snap = {
        "head_sha": "b" * 40,
        "head_match": True,
        "formal_reviews": [],
        "review_threads": {},
        "issue_comments": [],
        "required_checks": {
            "CI": {
                "status": "completed",
                "conclusion": "success",
                "run_id": "",
            },
        },
        "providers": {},
        "_provider_issue_comments": {},
        "unconsumed_event_ids": [],
        "provider_surfaces": {},
        "review_comments": [],
        "provider_surface_complete": True,
    }

    monkeypatch.setattr(sup, "capture_live_snapshot", lambda *a, **k: snap)
    monkeypatch.setattr(sup, "get_github_token", lambda: "tok")
    monkeypatch.setattr(
        sup, "read_run_state",
        lambda: {"current_head": "b" * 40},
    )
    monkeypatch.setattr(
        sup, "resolve_orchestration_state_root",
        lambda **_kw: tmp_path,
    )
    monkeypatch.setattr(
        "autocoder_supervisor.orchestration_state_root.resolve_orchestration_state_root",
        lambda **_kw: tmp_path,
        raising=False,
    )

    class _FakeStore:
        def __init__(self, _root: Any) -> None:
            pass

        def read_optional(self, _name: str) -> dict:
            return {"current_authorized_head": "b" * 40}

    monkeypatch.setattr(
        "autocoder_orchestration.store.StateStore", _FakeStore,
        raising=False,
    )
    monkeypatch.setattr(
        "autocoder_orchestration.context.RunContext",
        type("RC", (), {"from_dict": staticmethod(lambda d: d)}),
        raising=False,
    )

    out = sup._advance_awaiting_ci_to_qualifying()
    assert out is True, (
        "round-31 P1#7: when required checks are green the "
        "transition MUST be allowed and report_ci_pass called."
    )
    assert len(report_calls) == 1, (
        "round-31 P1#7: Controller.report_ci_pass MUST be "
        "called exactly once with the rebound head."
    )
    assert isinstance(report_calls[0], str) and len(report_calls[0]) == 40
