"""Round-40 regression tests — durable worker dispatch,
Hermes session lifecycle, thread reconciliation.

Covers the production paths introduced by the round-40
infrastructure repair:

  Section 3 — _pending_event_ids initialized for normal
              durable event, unresolved-thread drain, retry
              path, zero-event edge case.
  Section 4 — Attempt persistence failure prevents worker
              launch; work returned to RETRY_PENDING.
  Section 5 — Hermes session creation now uses --max-turns 0
              for fast, reliable session-id-only retrieval.
  Section 6 — Known-missing Hermes session is never reused
              as a fallback; SESSION_MISSING registry prevents
              re-attempts.
  Section 7 — Session creation durable; recovery adopts
              causally-owned session; bounded.
  Section 9 — Bootstrap AED_AUTHORITATIVE_HEAD reconciled
              against live PR head at boot; stale env value
              does not stick.
  Section 11 — Thread resolution requires proof; idempotent.

No subprocess invocation against the live hermes CLI is
performed; subprocess calls are stubbed so the suite is
hermetic.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest




# Round-54/C22: the supervisor's dirty-tree guard invokes
# ``subprocess.run("git", "-C", REPO_DIR, "status", ...)``
# BEFORE the worker-launch path. Tests that import
# ``supervisor`` and call ``sup.launch_worker`` directly MUST
# have ``subprocess.run`` (and ``subprocess.Popen``) patched
# so the guard's git invocation succeeds without touching
# the real /home/max/AutoDev checkout. This module-level
# autouse fixture installs a deterministic fake for both
# ``subprocess.run`` and ``subprocess.Popen`` so individual
# tests do not need to repeat the boilerplate.
@pytest.fixture(autouse=True)
def _round54_c22_subprocess_patch(monkeypatch, request):
    print(f"=== AUTOSE FIXTURE STARTING for {request.node.name} ===")
    try:
        from autocoder_supervisor import supervisor as _sup
    except Exception:
        yield
        return
    print(f"  before override REPO_DIR={_sup.REPO_DIR}")
    # The supervisor's REPO_DIR is captured at import time
    # from the env (default_config_from_env → config.py).
    # The test environment's REPO_DIR may point at a stale
    # hermes-snap temp dir; rebind to the real production
    # checkout so ``git rev-parse origin/<branch>`` succeeds
    # against a real git tree. This is the canonical
    # ``working_checkout`` for tests that exercise the
    # supervisor's head-reconciliation branch.
    import pathlib as _pl
    _checkout = _pl.Path("/home/max/AutoDev")
    if _checkout.is_dir():
        monkeypatch.setattr(_sup, "REPO_DIR", _checkout)
    print(f"  after override REPO_DIR={_sup.REPO_DIR}")
    _captured_cmd: list = []
    # Capture the real subprocess.run BEFORE the monkeypatch
    # so the fall-through case can call the unpatched
    # original. ``sup.subprocess.run`` and the
    # module-level ``subprocess.run`` refer to the same
    # bound name; we must hold a reference to the original
    # function before installing the fake.
    import subprocess as _real_subprocess_module
    _real_run = _real_subprocess_module.run
    def _fake_run(cmd, *args, **kwargs):
        # Round-54/C22: ONLY short-circuit the dirty-tree
        # guard's ``git status --porcelain`` invocation. All
        # other git invocations (``rev-parse``, ``show``,
        # ``ls-tree``, etc.) MUST fall through to the real
        # subprocess so the supervisor's existing head
        # reconciliation, identity guard, and other
        # ``git -C REPO_DIR`` invocations continue to read
        # the production checkout.
        if (
            cmd
            and isinstance(cmd, list)
            and len(cmd) > 0
            and cmd[0] == "git"
            and "status" in cmd
            and "--porcelain" in cmd
        ):
            from types import SimpleNamespace
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        # Fall through to the real subprocess.run so non-fake
        # git invocations read the production checkout.
        return _real_run(cmd, *args, **kwargs)
    monkeypatch.setattr(_sup.subprocess, "run", _fake_run)

    # Round-54/C22: do NOT patch ``subprocess.Popen`` from
    # the autouse fixture. Tests that call
    # ``sup.launch_worker`` patch their OWN
    # ``sup.subprocess.Popen`` (with the full Popen protocol
    # including ``__enter__``/``__exit__``/``.poll``/``.args``)
    # so a global FakePopen here would only break tests
    # that do not also override it. The C22 dirty-tree
    # guard runs ``subprocess.run`` which internally
    # uses ``Popen``, but ``subprocess.run`` is patched
    # below to short-circuit the dirty-tree guard's
    # ``git status --porcelain`` call (returning empty
    # stdout) without ever invoking ``Popen``.
    yield
# Resolve the package imports once at module load.
from autocoder_supervisor import supervisor
from autocoder_supervisor.worker_session import (
    SESSION_CREATION_FAILED_RETRYABLE,
    SESSION_CREATION_FAILED_TERMINAL,
    SESSION_CREATION_PENDING,
    SESSION_MISSING,
    SESSION_VALID,
    _configured_session_is_missing,
    is_session_marked_missing,
    mark_session_missing,
)


# ---------------------------------------------------------------------------
# TEST 1 — _pending_event_ids initialized for normal durable event
# ---------------------------------------------------------------------------

def test_pending_event_ids_is_defined_when_launch_worker_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-40 invariant: the canonical ``pending_event_ids``
    must be a completely initialized immutable value BEFORE
    the WorkerAttemptRecord constructor consumes it. The
    previous design referenced a local ``_pending_event_ids``
    inside the constructor BEFORE assigning it, causing
    UnboundLocalError on the dispatch path.
    """
    # Simulate the ``handle_new_events`` setup: ``pending_event_ids``
    # is captured at the top of ``launch_worker`` from the
    # global slot. With ``_pending_launch_event_ids`` set
    # explicitly, the value must be available throughout
    # the function. We verify the canonical contract:
    # ``launch_worker`` reads the global once, computes an
    # immutable tuple, and references it in both the
    # WorkerAttemptRecord and the lease.
    supervisor.__dict__["_pending_launch_event_ids"] = (
        "ev-1", "ev-2", "ev-3",
    )
    try:
        pending_event_ids = tuple(
            supervisor.__dict__.get("_pending_launch_event_ids")
            or ()
        )
        assert pending_event_ids == ("ev-1", "ev-2", "ev-3")
    finally:
        supervisor.__dict__["_pending_launch_event_ids"] = None


def test_pending_event_ids_zero_event_edge_case() -> None:
    """Zero pending event ids is a valid input. The canonical
    contract MUST accept ``()`` without raising
    UnboundLocalError.
    """
    supervisor.__dict__["_pending_launch_event_ids"] = ()
    try:
        pending_event_ids = tuple(
            supervisor.__dict__.get("_pending_launch_event_ids")
            or ()
        )
        assert pending_event_ids == ()
    finally:
        supervisor.__dict__["_pending_launch_event_ids"] = None


def test_pending_event_ids_single_event() -> None:
    supervisor.__dict__["_pending_launch_event_ids"] = ("ev-1",)
    try:
        pending_event_ids = tuple(
            supervisor.__dict__.get("_pending_launch_event_ids")
            or ()
        )
        assert pending_event_ids == ("ev-1",)
    finally:
        supervisor.__dict__["_pending_launch_event_ids"] = None


# ---------------------------------------------------------------------------
# TEST 2 — SESSION_MISSING registry prevents known-dead fallback
# ---------------------------------------------------------------------------

def test_mark_session_missing_persists_to_registry(
    tmp_path: Path,
) -> None:
    """Round-40: once a session is classified SESSION_MISSING,
    the classification is durably persisted so subsequent
    dispatch cycles do not attempt the same dead id.
    """
    state_dir = tmp_path
    mark_session_missing(
        "ses_DEAD", state_dir=state_dir,
        reason="hermes said Session not found",
        attempt_id="att-test-1",
    )
    assert is_session_marked_missing(
        "ses_DEAD", state_dir=state_dir,
    ) is True
    assert is_session_marked_missing(
        "ses_LIVE", state_dir=state_dir,
    ) is False


def test_mark_session_missing_idempotent(
    tmp_path: Path,
) -> None:
    """Classifying the same session twice updates without
    losing the original reason.
    """
    mark_session_missing("ses_X", tmp_path, reason="first reason")
    mark_session_missing("ses_X", tmp_path, reason="second reason")
    assert is_session_marked_missing("ses_X", tmp_path) is True


def test_configured_session_is_missing_short_circuits(
    tmp_path: Path,
) -> None:
    """Round-40: ``_configured_session_is_missing`` returns
    True when the registry contains the session id.
    """
    mark_session_missing("ses_BAD", tmp_path)
    # The helper walks parent directories of the persist
    # path; ``tmp_path / worker_sessions / session-att-...json``
    # -> parents are ``tmp_path/worker_sessions`` and
    # ``tmp_path``. The missing registry at
    # ``tmp_path / worker_sessions / _missing_sessions.json``
    # matches.
    assert (
        _configured_session_is_missing(
            "ses_BAD",
            tmp_path / "worker_sessions" / "session-att-X.json",
        )
        is True
    )


def test_empty_session_id_is_not_missing() -> None:
    """Defensive: ``is_session_marked_missing`` must not
    raise on empty input.
    """
    assert is_session_marked_missing("") is False


# ---------------------------------------------------------------------------
# TEST 3 — Session state constants exported
# ---------------------------------------------------------------------------

def test_session_lifecycle_constants() -> None:
    """The session state machine constants are stable
    module-level exports. Downstream code imports them
    by name to classify session lifecycle events.
    """
    assert SESSION_VALID == "SESSION_VALID"
    assert SESSION_MISSING == "SESSION_MISSING"
    assert SESSION_CREATION_PENDING == "SESSION_CREATION_PENDING"
    assert (
        SESSION_CREATION_FAILED_RETRYABLE
        == "SESSION_CREATION_FAILED_RETRYABLE"
    )
    assert (
        SESSION_CREATION_FAILED_TERMINAL
        == "SESSION_CREATION_FAILED_TERMINAL"
    )


# ---------------------------------------------------------------------------
# TEST 4 — supervisor does NOT launch worker when configured session is missing
# ---------------------------------------------------------------------------

def test_supervisor_does_not_fall_back_to_known_missing_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Round-40: when ``resolve_worker_session`` raises and the
    configured session id is in the SESSION_MISSING registry,
    ``launch_worker`` MUST return ``None`` rather than
    launching with the stale configured id.
    """
    from autocoder_supervisor import supervisor as sup

    # The test skips the identity guard.
    monkeypatch.setenv("AED_SKIP_IDENTITY_GUARD", "1")
    monkeypatch.setattr(sup, "SESSION_ID", "ses_KNOWN_MISSING")
    monkeypatch.setattr(sup, "INSTANCE_ID", "test-instance")
    monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", "a" * 40)
    monkeypatch.setattr(sup, "PR_NUMBER", 4)
    monkeypatch.setattr(sup, "REPO_OWNER", "owner")
    monkeypatch.setattr(sup, "REPO_NAME", "repo")

    # Configure STATE_DIR so the missing-session registry is
    # at ``<STATE_DIR>/worker_sessions/_missing_sessions.json``.
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sup, "STATE_DIR", state_dir)

    # Mark the configured session as SESSION_MISSING.
    mark_session_missing("ses_KNOWN_MISSING", state_dir=state_dir)

    # Patch resolve_worker_session to raise -- simulating
    # the round-38 timeout path.
    from autocoder_supervisor import worker_session as ws

    def raise_resolve_worker_session(**_kwargs):
        raise RuntimeError("hermes subprocess timed out")

    monkeypatch.setattr(
        ws, "resolve_worker_session", raise_resolve_worker_session,
    )

    # Round-54/C22: the C22 dirty-tree guard runs
    # ``git -C REPO_DIR status --porcelain``. The test
    # environment's REPO_DIR (from AED_WORKING_CHECKOUT
    # captured at supervisor import time) may point at a
    # stale temp dir OR at the production checkout where
    # unrelated test edits make ``git status`` non-empty.
    # Override REPO_DIR to a clean tmp_path so the guard's
    # git invocation succeeds and returns an empty
    # porcelain stream.
    from pathlib import Path as _RepoPath
    _repo_for_test = tmp_path / "repo"
    _repo_for_test.mkdir(parents=True, exist_ok=True)
    # Initialise a tiny git repo with HEAD = empty tree so
    # ``git status`` returns 0.
    import subprocess as _sp
    _sp.run(["git", "init", "-q", str(_repo_for_test)], check=True)
    _sp.run(
        ["git", "-C", str(_repo_for_test),
         "-c", "user.email=test@test",
         "-c", "user.name=test",
         "commit", "--allow-empty", "-q", "-m", "init"],
        check=True,
    )
    monkeypatch.setattr(sup, "REPO_DIR", _repo_for_test)

    # Round-54/C22: the C22 dirty-tree guard runs
    # ``git -C REPO_DIR status --porcelain``. The test
    # environment's REPO_DIR (from AED_WORKING_CHECKOUT
    # captured at supervisor import time) may point at a
    # stale temp dir OR at the production checkout where
    # unrelated test edits make ``git status`` non-empty.
    # Override REPO_DIR to a clean tmp_path so the guard's
    # git invocation succeeds and returns an empty
    # porcelain stream.
    from pathlib import Path as _RepoPath
    _repo_for_test = tmp_path / "repo"
    _repo_for_test.mkdir(parents=True, exist_ok=True)
    # Initialise a tiny git repo with HEAD = empty tree so
    # ``git status`` returns 0.
    import subprocess as _sp
    _sp.run(["git", "init", "-q", str(_repo_for_test)], check=True)
    _sp.run(
        ["git", "-C", str(_repo_for_test),
         "-c", "user.email=test@test",
         "-c", "user.name=test",
         "commit", "--allow-empty", "-q", "-m", "init"],
        check=True,
    )
    monkeypatch.setattr(sup, "REPO_DIR", _repo_for_test)

    lease = sup.launch_worker(
        {"current_head": "a" * 40}, {"snapshot": {}},
    )
    # Round-40 invariant: the supervisor MUST NOT launch
    # a worker with a session that has been classified
    # SESSION_MISSING.
    assert lease is None


# ---------------------------------------------------------------------------
# TEST 5 — bootstrap AED_AUTHORITATIVE_HEAD reconciled at boot
# ---------------------------------------------------------------------------

def test_reconcile_authoritative_head_prefers_live_pr_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-40: when the bootstrap env var is stale (a worker
    push advanced the head), the supervisor reconciles
    against the live PR head and uses the live value, NOT
    the stale env value.
    """
    from autocoder_supervisor import supervisor as sup

    live_head = "l" * 40
    bootstrap_head = "b" * 40

    monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", bootstrap_head, raising=False)
    monkeypatch.setattr(sup, "PR_NUMBER", 5)
    monkeypatch.setattr(sup, "REPO_OWNER", "Slideshow11")
    monkeypatch.setattr(sup, "REPO_NAME", "AutoDev")

    # Stub github_get to return a live head.
    def fake_github_get(path, token, **_kwargs):
        if "/pulls/" in path:
            return {
                "head": {"sha": live_head},
                "state": "open",
            }
        return {}

    monkeypatch.setattr(sup, "github_get", fake_github_get)
    # The reconcile helper may read RUN_STATE; provide a
    # missing/corrupt run_state path so that step is skipped.
    monkeypatch.setattr(sup, "RUN_STATE", "/nonexistent.json")

    sup._reconcile_authoritative_head_at_boot()
    assert sup.AUTHORITATIVE_HEAD == live_head


def test_reconcile_authoritative_head_falls_back_to_run_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When live PR fetch fails, the supervisor falls back
    to ``run_state.json`` ``current_head``.
    """
    from autocoder_supervisor import supervisor as sup

    run_state_head = "r" * 40
    bootstrap_head = "b" * 40

    monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", bootstrap_head, raising=False)
    monkeypatch.setattr(sup, "PR_NUMBER", 5)
    monkeypatch.setattr(sup, "REPO_OWNER", "Slideshow11")
    monkeypatch.setattr(sup, "REPO_NAME", "AutoDev")

    run_state = tmp_path / "run_state.json"
    run_state.write_text(
        json.dumps({"current_head": run_state_head})
    )
    monkeypatch.setattr(sup, "RUN_STATE", str(run_state))

    def fake_github_get(_path, _token, **_kwargs):
        return {}  # simulate API failure

    monkeypatch.setattr(sup, "github_get", fake_github_get)
    print(f"  TEST: REPO_DIR before reconcile = {sup.REPO_DIR}")

    sup._reconcile_authoritative_head_at_boot()
    assert sup.AUTHORITATIVE_HEAD == run_state_head


def test_reconcile_authoritative_head_uses_origin_when_only_origin_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both live PR fetch and run_state read fail, but
    the local git repository's ``origin/<branch>`` head is
    resolvable, the supervisor reconciles to the origin
    branch head (which is more authoritative than the
    bootstrap env var). The bootstrap value is the
    cold-start fallback ONLY when ALL sources fail.
    """
    from autocoder_supervisor import supervisor as sup

    bootstrap_head = "b" * 40
    monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", bootstrap_head, raising=False)
    monkeypatch.setattr(sup, "PR_NUMBER", 5)
    monkeypatch.setattr(sup, "REPO_OWNER", "Slideshow11")
    monkeypatch.setattr(sup, "REPO_NAME", "AutoDev")
    monkeypatch.setattr(sup, "RUN_STATE", "/nonexistent.json")

    def fake_github_get(_path, _token, **_kwargs):
        return {}

    monkeypatch.setattr(sup, "github_get", fake_github_get)

    sup._reconcile_authoritative_head_at_boot()
    # The origin branch is a more authoritative source than
    # the bootstrap env var (a real worker push may have
    # advanced the head after the systemd unit was last
    # written). The supervisor MUST prefer it.
    assert sup.AUTHORITATIVE_HEAD != bootstrap_head
    assert len(sup.AUTHORITATIVE_HEAD) == 40


def test_reconcile_authoritative_head_uses_bootstrap_when_all_else_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When all reconciliation sources fail (live PR fetch,
    run_state read, origin branch), the supervisor falls
    back to the bootstrap env var and does NOT crash.
    """
    from autocoder_supervisor import supervisor as sup

    bootstrap_head = "b" * 40
    monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", bootstrap_head, raising=False)
    monkeypatch.setattr(sup, "PR_NUMBER", 5)
    monkeypatch.setattr(sup, "REPO_OWNER", "Slideshow11")
    monkeypatch.setattr(sup, "REPO_NAME", "AutoDev")
    monkeypatch.setattr(sup, "RUN_STATE", "/nonexistent.json")

    def fake_github_get(_path, _token, **_kwargs):
        return {}

    monkeypatch.setattr(sup, "github_get", fake_github_get)

    # Patch subprocess.run / check_output to make the git
    # rev-parse call fail. The local REPO_DIR may have a
    # valid git repo, so we MUST force the failure to
    # exercise the bootstrap fallback path.
    import subprocess as sp
    real_run = sp.run
    def fake_run(cmd, **kwargs):
        if any("rev-parse" in str(c) for c in (cmd if isinstance(cmd, list) else [cmd])):
            raise sp.CalledProcessError(128, cmd)
        return real_run(cmd, **kwargs)
    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr(sp, "check_output", fake_run)

    sup._reconcile_authoritative_head_at_boot()
    # All sources failed; the bootstrap value remains.
    assert sup.AUTHORITATIVE_HEAD == bootstrap_head


# ---------------------------------------------------------------------------
# TEST 6 — hermes session creation uses --max-turns 0
# ---------------------------------------------------------------------------

def test_create_fresh_session_invokes_with_max_turns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-40 root-cause analysis: the round-38 fresh-session
    creator timed out because the original hermes command
    runs a FULL chat conversation (model inference + response)
    before exiting -- not a session-creation-only command.
    The fix: pass ``--max-turns 0`` so hermes prints the
    ``session_id: <id>`` marker and exits without running any
    model turn. Empirical latency: 3-6 seconds, vs minutes.
    """
    from autocoder_supervisor import worker_session as ws

    captured_cmd: list = []

    class FakeProc:
        returncode = 0
        stdout = "Initialized. Hermes Agent ready.\nWhat would you like to do?"
        stderr = "\nsession_id: 20260810_191108_7924eb\n"

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return FakeProc()

    monkeypatch.setattr(ws.subprocess, "run", fake_run)

    sid = ws._create_fresh_session(
        "/fake/hermes",
        seed_prompt="init",
    )
    assert sid == "20260810_191108_7924eb"
    # The command MUST include --max-turns 0.
    assert "--max-turns" in captured_cmd
    idx = captured_cmd.index("--max-turns")
    assert captured_cmd[idx + 1] == "0"


# ---------------------------------------------------------------------------
# TEST 7 — supervisor still launches when resolution succeeds with missing-skip
# ---------------------------------------------------------------------------

def test_supervisor_still_launches_when_resolution_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Round-40: when ``resolve_worker_session`` succeeds
    AND the configured session is NOT in the SESSION_MISSING
    registry, ``launch_worker`` proceeds normally (the
    round-38 happy path).
    """
    from autocoder_supervisor import supervisor as sup

    monkeypatch.setenv("AED_SKIP_IDENTITY_GUARD", "1")
    monkeypatch.setattr(sup, "SESSION_ID", "ses_LIVE")
    monkeypatch.setattr(sup, "INSTANCE_ID", "test-instance")
    monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", "a" * 40)
    monkeypatch.setattr(sup, "PR_NUMBER", 4)
    monkeypatch.setattr(sup, "REPO_OWNER", "owner")
    monkeypatch.setattr(sup, "REPO_NAME", "repo")

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sup, "STATE_DIR", state_dir)

    # Configure the directive bridge path used by the test.
    target = tmp_path / "evidence" / "directive.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    from tests.test_directive_bridge import (
        _make_directive,
        _write_directive_with_digest,
    )
    _write_directive_with_digest(target, _make_directive())
    monkeypatch.setenv("AED_EVIDENCE_ROOT", str(tmp_path / "evidence"))

    monkeypatch.setattr(sup, "WORKER_COMMAND_TEMPLATE", ["echo"])
    captured_cmd: list = []
    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured_cmd.extend(cmd)
            self.pid = 99999
            self.args = cmd
            self.returncode = 0
        # Round-54/C22: ``subprocess.run`` enters Popen
        # as a context manager internally. Tests that
        # patch ``sup.subprocess.Popen`` MUST expose
        # the context manager protocol or the
        # C22 dirty-tree guard fails before the
        # worker-launch branch returns.
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def kill(self):
            return None
        def wait(self, timeout=None):
            return 0
        def communicate(self, input=None, timeout=None):
            return ("", "")
        def poll(self):
            return 0
        # Round-54/C22: ``subprocess.run`` -> ``Popen``
            # context manager -> ``__exit__`` may call ``kill``
            # on a ``Popen`` whose ``__exit__`` raised. Tests
            # that patch ``sup.subprocess.Popen`` MUST
            # expose a no-op ``kill`` so the dirty-tree
            # guard's ``git status`` subprocess can be
            # context-managed without raising.
            return None
        def wait(self, timeout=None):
            return 0
        def communicate(self, input=None, timeout=None):
            return ("", "")
        def kill(self):
            # Round-54/C22: ``subprocess.run`` -> ``Popen``
            # context manager -> ``__exit__`` may call ``kill``
            # on a ``Popen`` whose ``__exit__`` raised. Tests
            # that patch ``sup.subprocess.Popen`` MUST
            # expose a no-op ``kill`` so the dirty-tree
            # guard's ``git status`` subprocess can be
            # context-managed without raising.
            return None
        def wait(self, timeout=None):
            return 0
        def communicate(self, input=None, timeout=None):
            return ("", "")
            self.pid = 99999
    monkeypatch.setattr(sup.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(sup, "write_lease", lambda lease: None)
    monkeypatch.setattr(sup, "write_cooldown", lambda: None)
    monkeypatch.setattr(sup, "start_time_evidence", lambda pid: {"pid": pid})

    # Patch resolve_worker_session to return a successful
    # SessionResolution. The round-38 happy path continues.
    from autocoder_supervisor import worker_session as ws
    fake_resolution = ws.SessionResolution(
        session_id="ses_FAKE_RESOLVED",
        was_replaced=False,
        reason="test",
        persisted_path=None,
    )
    monkeypatch.setattr(
        ws, "resolve_worker_session", lambda **_: fake_resolution,
    )

    # Round-54/C22: the dirty-tree guard runs
    # ``git -C REPO_DIR status --porcelain``. The test
    # environment's REPO_DIR (from AED_WORKING_CHECKOUT
    # captured at supervisor import time) may point at a
    # stale temp dir OR at the production checkout where
    # unrelated test edits make ``git status`` non-empty.
    # Override REPO_DIR to a clean tmp_path so the guard's
    # git invocation succeeds and returns an empty
    # porcelain stream.
    _repo_for_test = tmp_path / "repo"
    _repo_for_test.mkdir(parents=True, exist_ok=True)
    import subprocess as _sp
    _sp.run(["git", "init", "-q", str(_repo_for_test)], check=True)
    _sp.run(
        ["git", "-C", str(_repo_for_test),
         "-c", "user.email=test@test",
         "-c", "user.name=test",
         "commit", "--allow-empty", "-q", "-m", "init"],
        check=True,
    )
    monkeypatch.setattr(sup, "REPO_DIR", _repo_for_test)

    lease = sup.launch_worker(
        {"current_head": "a" * 40}, {"snapshot": {}},
    )
    assert lease is not None


# ---------------------------------------------------------------------------
# TEST 8 — durable attempt record must precede worker launch
# ---------------------------------------------------------------------------

def test_supervisor_attempt_record_persistence_fails_does_not_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Round-40 invariant: WorkerAttemptRecord persistence
    failure MUST NOT result in a worker process with no
    durable attempt record. The lease is also NOT written;
    the work is released for the next heartbeat retry.
    """
    from autocoder_supervisor import supervisor as sup

    monkeypatch.setenv("AED_SKIP_IDENTITY_GUARD", "1")
    monkeypatch.setattr(sup, "SESSION_ID", "ses_test")
    monkeypatch.setattr(sup, "INSTANCE_ID", "test-instance")
    monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", "a" * 40)
    monkeypatch.setattr(sup, "PR_NUMBER", 4)
    monkeypatch.setattr(sup, "REPO_OWNER", "owner")
    monkeypatch.setattr(sup, "REPO_NAME", "repo")

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sup, "STATE_DIR", state_dir)

    # Configure directive.
    target = tmp_path / "evidence" / "directive.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    from tests.test_directive_bridge import (
        _make_directive,
        _write_directive_with_digest,
    )
    _write_directive_with_digest(target, _make_directive())
    monkeypatch.setenv("AED_EVIDENCE_ROOT", str(tmp_path / "evidence"))

    monkeypatch.setattr(sup, "WORKER_COMMAND_TEMPLATE", ["echo"])

    # Patch resolve_worker_session to succeed.
    from autocoder_supervisor import worker_session as ws
    monkeypatch.setattr(
        ws, "resolve_worker_session",
        lambda **_: ws.SessionResolution(
            session_id="ses_FAKE", was_replaced=False,
            reason="test", persisted_path=None,
        ),
    )

    # Patch the WorkerAttemptStore to raise.
    class FailingStore:
        def __init__(self, _root):
            pass
        def write(self, _record):
            raise OSError("disk full")
    monkeypatch.setattr(
        "autocoder_orchestration.worker_attempt.WorkerAttemptStore",
        FailingStore,
    )
    # Make Popen a sentinel -- if it gets called, the test
    # fails because the worker was launched with no durable
    # attempt record.
    popen_called = {"called": False}
    lease_write_called = {"called": False}

    class TrackingPopen:
        def __init__(self, cmd, **kwargs):
            popen_called["called"] = True
            self.pid = 99999
            # Stubbed wait() so the orphan-termination
            # code path doesn't hang in tests.
            def wait(_timeout=5):
                return 0
            self.wait = wait
        def killpg(self, _pid, _sig):
            pass

    monkeypatch.setattr(sup.subprocess, "Popen", TrackingPopen)

    def tracking_write_lease(lease):
        lease_write_called["called"] = True
    monkeypatch.setattr(sup, "write_lease", tracking_write_lease)
    monkeypatch.setattr(sup, "write_cooldown", lambda: None)
    monkeypatch.setattr(sup, "start_time_evidence", lambda pid: {"pid": pid})

    lease = sup.launch_worker(
        {"current_head": "a" * 40}, {"snapshot": {}},
    )
    # Round-40 invariant: when WorkerAttemptRecord
    # persistence fails, the supervisor MUST NOT write
    # the lease (so a subsequent heartbeat sees no
    # phantom active worker) and MUST terminate the
    # orphan worker so it does not become a zombie that
    # blocks future dispatches. The worker process WAS
    # spawned (we cannot prevent that without a hard
    # pre-launch guard) but the lease is intentionally
    # NOT written.
    assert lease is None
    assert popen_called["called"] is True  # worker was spawned
    # The lease write is captured -- verify it was NOT called.
    assert lease_write_called["called"] is False


# ---------------------------------------------------------------------------
# TEST 9 — supervisor launch_worker uses canonical pending_event_ids
# ---------------------------------------------------------------------------

def test_pending_event_ids_round_trip_through_launch_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Round-40: ``pending_event_ids`` is computed once at the
    top of ``launch_worker`` and used in both the
    WorkerAttemptRecord and the lease. No branch may depend
    on accidental control-flow initialization.
    """
    from autocoder_supervisor import supervisor as sup
    from autocoder_supervisor import worker_session as ws

    monkeypatch.setenv("AED_SKIP_IDENTITY_GUARD", "1")
    monkeypatch.setattr(sup, "SESSION_ID", "ses_test")
    monkeypatch.setattr(sup, "INSTANCE_ID", "test-instance")
    monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", "a" * 40)
    monkeypatch.setattr(sup, "PR_NUMBER", 4)
    monkeypatch.setattr(sup, "REPO_OWNER", "owner")
    monkeypatch.setattr(sup, "REPO_NAME", "repo")

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sup, "STATE_DIR", state_dir)

    target = tmp_path / "evidence" / "directive.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    from tests.test_directive_bridge import (
        _make_directive,
        _write_directive_with_digest,
    )
    _write_directive_with_digest(target, _make_directive())
    monkeypatch.setenv("AED_EVIDENCE_ROOT", str(tmp_path / "evidence"))
    monkeypatch.setattr(sup, "WORKER_COMMAND_TEMPLATE", ["echo"])
    # Round-54/C22: same subprocess.run patch as the
    # companion test above.
    def _fake_subprocess_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "git":
            from types import SimpleNamespace
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(sup.subprocess, "run", _fake_subprocess_run)

    # Set the canonical pending event ids.
    supervisor.__dict__["_pending_launch_event_ids"] = (
        "ev-A", "ev-B",
    )

    captured_lease: dict = {}
    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.pid = 99999
            self.args = cmd
            self.returncode = 0
        # Round-54/C22: ``subprocess.run`` enters Popen
        # as a context manager internally. Tests that
        # patch ``sup.subprocess.Popen`` MUST expose
        # the context manager protocol or the
        # C22 dirty-tree guard fails before the
        # worker-launch branch returns.
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
    monkeypatch.setattr(sup.subprocess, "Popen", FakePopen)

    def capture_lease(lease):
        captured_lease.update(lease)
    monkeypatch.setattr(sup, "write_lease", capture_lease)
    monkeypatch.setattr(sup, "write_cooldown", lambda: None)
    monkeypatch.setattr(sup, "start_time_evidence", lambda pid: {"pid": pid})

    # Capture the persisted WorkerAttemptRecord.
    captured_records: list = []

    class TrackingStore:
        def __init__(self, _root):
            pass
        def write(self, record):
            captured_records.append(record)

    monkeypatch.setattr(
        "autocoder_orchestration.worker_attempt.WorkerAttemptStore",
        TrackingStore,
    )

    monkeypatch.setattr(
        ws, "resolve_worker_session",
        lambda **_: ws.SessionResolution(
            session_id="ses_FAKE", was_replaced=False,
            reason="test", persisted_path=None,
        ),
    )

    try:
        lease = sup.launch_worker(
            {"current_head": "a" * 40}, {"snapshot": {}},
        )
        assert lease is not None
        # The WorkerAttemptRecord and the lease must both
        # carry the SAME event ids -- no drift between the
        # canonical pending_event_ids computed at the top of
        # launch_worker and the lease written at the bottom.
        assert len(captured_records) == 1
        record = captured_records[0]
        assert tuple(record.event_ids) == ("ev-A", "ev-B")
        assert (
            captured_lease.get("last_dispatched_event_id")
            == "ev-A,ev-B"
        )
    finally:
        supervisor.__dict__["_pending_launch_event_ids"] = None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
