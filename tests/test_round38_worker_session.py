"""Round-38 regression tests — worker session lifecycle,
zombie worker detection, and 401 credential-reload recovery.

Each test exercises the real production module:
    autocoder_supervisor.worker_session
    autocoder_supervisor.supervisor

No subprocess invocation against the live hermes CLI is
performed in this suite; hermes subprocess calls are
stubbed so the suite is hermetic and runs offline. The
real hermes session-create behaviour is verified manually
in the round-38 forensic table (section 4 of the
directive).
"""
from __future__ import annotations

import json
import os
import urllib.error
from pathlib import Path

import pytest


# Resolve the package imports once at module load.
from autocoder_supervisor.worker_session import (
    SessionResolution,
    _SESSION_ID_LINE_RE,
    _SESSION_NOT_FOUND_RE,
    resolve_worker_session,
)
from autocoder_supervisor.supervisor import (
    _read_proc_state,
    get_github_token,
    github_get,
    github_token_source,
    pgid_alive,
    pid_alive,
)


# ---------------------------------------------------------------------------
# TEST 1 — configured session exists → resume path
# ---------------------------------------------------------------------------

def test_resolve_worker_session_uses_configured_when_it_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When the configured session id exists in the hermes
    state.db, ``resolve_worker_session`` MUST return it
    unchanged and persist it for restart resilience.
    """
    import autocoder_supervisor.worker_session as ws

    fake_hermes = tmp_path / "fake_hermes"
    fake_hermes.write_text("#!/bin/sh\necho ok\n")
    fake_hermes.chmod(0o755)

    monkeypatch.setattr(
        ws, "_hermes_session_exists", lambda bin, sid, **k: sid == "ses_EXISTS"
    )
    monkeypatch.setattr(ws, "_create_fresh_session", lambda *a, **k: "FRESH")

    persist = tmp_path / "session.json"
    result = resolve_worker_session(
        hermes_bin=str(fake_hermes),
        configured_session_id="ses_EXISTS",
        persist_path=persist,
        attempt_id="att-test",
        pr_number=5,
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        feature_branch="feat/test",
        workspace_cwd=tmp_path,
    )
    assert isinstance(result, SessionResolution)
    assert result.session_id == "ses_EXISTS"
    assert not result.was_replaced
    assert persist.exists()
    payload = json.loads(persist.read_text(encoding="utf-8"))
    assert payload["session_id"] == "ses_EXISTS"
    assert payload["replacement_reason"] == "bootstrap_persistence"


# ---------------------------------------------------------------------------
# TEST 2 — configured session missing → fresh isolated session created
# ---------------------------------------------------------------------------

def test_resolve_worker_session_creates_fresh_when_configured_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When the configured session id does NOT exist,
    ``resolve_worker_session`` MUST create a fresh isolated
    hermes session, persist it, and return it with
    ``was_replaced=True``.
    """
    import autocoder_supervisor.worker_session as ws

    fake_hermes = tmp_path / "fake_hermes"
    fake_hermes.write_text("#!/bin/sh\necho ok\n")
    fake_hermes.chmod(0o755)

    monkeypatch.setattr(
        ws, "_hermes_session_exists", lambda bin, sid, **k: False,
    )
    monkeypatch.setattr(
        ws, "_create_fresh_session", lambda *a, **k: "ses_FRESH_9999",
    )

    persist = tmp_path / "session.json"
    result = resolve_worker_session(
        hermes_bin=str(fake_hermes),
        configured_session_id="ses_DOES_NOT_EXIST",
        persist_path=persist,
        attempt_id="att-test",
        pr_number=5,
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        feature_branch="feat/test",
        workspace_cwd=tmp_path,
    )
    assert isinstance(result, SessionResolution)
    assert result.session_id == "ses_FRESH_9999"
    assert result.was_replaced
    assert persist.exists()
    payload = json.loads(persist.read_text(encoding="utf-8"))
    assert payload["session_id"] == "ses_FRESH_9999"
    assert "configured session missing" in payload["replacement_reason"]


# ---------------------------------------------------------------------------
# TEST 3 — persisted identity takes precedence over stale env
# ---------------------------------------------------------------------------

def test_resolve_worker_session_prefers_persisted_when_valid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """If a prior round persisted a session identity for this
    attempt and that session still exists, ``resolve_worker_session``
    MUST return it even when the configured env session is missing.
    """
    import autocoder_supervisor.worker_session as ws

    fake_hermes = tmp_path / "fake_hermes"
    fake_hermes.write_text("#!/bin/sh\necho ok\n")
    fake_hermes.chmod(0o755)

    persist = tmp_path / "session.json"
    persist.parent.mkdir(parents=True, exist_ok=True)
    persist.write_text(
        json.dumps({"session_id": "ses_PERSISTED"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        ws, "_hermes_session_exists",
        lambda bin, sid, **k: sid == "ses_PERSISTED",
    )

    result = resolve_worker_session(
        hermes_bin=str(fake_hermes),
        configured_session_id="ses_CONFIGURED_MAYBE_STALE",
        persist_path=persist,
        attempt_id="att-test",
        pr_number=5,
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        feature_branch="feat/test",
        workspace_cwd=tmp_path,
    )
    assert result.session_id == "ses_PERSISTED"
    assert not result.was_replaced
    assert result.reason.startswith("persisted session exists")


# ---------------------------------------------------------------------------
# TEST 4 — persisted identity stale → falls back to fresh
# ---------------------------------------------------------------------------

def test_resolve_worker_session_replaces_stale_persisted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """If the persisted session no longer exists (e.g. across
    a restart that lost it), the resolver MUST fall through
    to the configured check, and finally to a fresh session.
    """
    import autocoder_supervisor.worker_session as ws

    fake_hermes = tmp_path / "fake_hermes"
    fake_hermes.write_text("#!/bin/sh\necho ok\n")
    fake_hermes.chmod(0o755)

    persist = tmp_path / "session.json"
    persist.parent.mkdir(parents=True, exist_ok=True)
    persist.write_text(
        json.dumps({"session_id": "ses_STALE_PERSISTED"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        ws, "_hermes_session_exists", lambda bin, sid, **k: False
    )
    monkeypatch.setattr(
        ws, "_create_fresh_session", lambda *a, **k: "ses_FRESH_REPLACEMENT"
    )

    result = resolve_worker_session(
        hermes_bin=str(fake_hermes),
        configured_session_id="ses_CONFIGURED_STALE",
        persist_path=persist,
        attempt_id="att-test",
        pr_number=5,
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        feature_branch="feat/test",
        workspace_cwd=tmp_path,
    )
    assert result.session_id == "ses_FRESH_REPLACEMENT"
    assert result.was_replaced
    payload = json.loads(persist.read_text(encoding="utf-8"))
    assert payload["session_id"] == "ses_FRESH_REPLACEMENT"


# ---------------------------------------------------------------------------
# TEST 5 — fresh-session creation parses ``session_id:`` from stdout
# ---------------------------------------------------------------------------

def test_session_id_line_regex_matches_hermes_output() -> None:
    sample_stdout = (
        "Some preamble text\n"
        "↻ Started session\n"
        "Working...\n"
        "session_id: 20260810_073630_9db7e3d9\n"
    )
    match = _SESSION_ID_LINE_RE.search(sample_stdout)
    assert match is not None
    assert match.group(1) == "20260810_073630_9db7e3d9"

    embedded = "I think session_id: foo123 is the new one"
    assert _SESSION_ID_LINE_RE.search(embedded) is None


def test_session_not_found_regex_matches_hermes_stderr() -> None:
    sample_stderr = (
        "Session not found: 20260810_023800_pr5\n"
        "Use a session ID from a previous CLI run (hermes sessions list).\n"
    )
    match = _SESSION_NOT_FOUND_RE.search(sample_stderr)
    assert match is not None
    assert match.group(1) == "20260810_023800_pr5"


# ---------------------------------------------------------------------------
# TEST 6 — pid_alive correctly classifies a live process
# ---------------------------------------------------------------------------

def test_pid_alive_true_for_self() -> None:
    self_pid = os.getpid()
    state = _read_proc_state(self_pid)
    assert state not in (None, "", "Z", "X")
    assert pid_alive(self_pid) is True


# ---------------------------------------------------------------------------
# TEST 7 — pid_alive False for missing PID
# ---------------------------------------------------------------------------

def test_pid_alive_false_for_missing_pid() -> None:
    assert pid_alive(999_999_999) is False


# ---------------------------------------------------------------------------
# TEST 8 — pgid_alive scans /proc for live members
# ---------------------------------------------------------------------------

def test_pgid_alive_true_for_own_pgid() -> None:
    my_pgid = os.getpgid(0)
    assert pgid_alive(my_pgid) is True


def test_pgid_alive_false_for_missing_pgid() -> None:
    assert pgid_alive(999_999_999) is False


# ---------------------------------------------------------------------------
# TEST 9 — credential source label is non-secret
# ---------------------------------------------------------------------------

def test_github_token_source_returns_no_secret() -> None:
    label = github_token_source()
    assert label in ("env", "hosts_yml", "none")
    assert "gho_" not in label
    assert "ghp_" not in label


# ---------------------------------------------------------------------------
# TEST 10 — get_github_token honours env override
# ---------------------------------------------------------------------------

def test_get_github_token_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN_PR_AUTODEV", "gho_FAKE_TOKEN_FOR_TEST")
    assert get_github_token() == "gho_FAKE_TOKEN_FOR_TEST"
    monkeypatch.delenv("GITHUB_TOKEN_PR_AUTODEV", raising=False)


# ---------------------------------------------------------------------------
# TEST 11 — github_get logs credential source (non-secret)
# ---------------------------------------------------------------------------

def test_github_get_logs_credential_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    class FakeLog:
        def __call__(self, level: str, msg: str, **fields: object) -> None:
            captured.append(f"{level}|{msg}|{fields}")

    from autocoder_supervisor import supervisor as sup
    monkeypatch.setattr(sup, "log", FakeLog())

    github_get("/test", "gho_FAKE", _retry_on_401=False)

    debug_lines = [c for c in captured if "github_get call" in c]
    assert debug_lines, f"expected github_get call debug log, got {captured}"
    fields = debug_lines[-1].split("|", 2)[2]
    assert "credential_source" in fields
    assert "gho_FAKE" not in fields


# ---------------------------------------------------------------------------
# TEST 12 — github_get retry on 401 reloads credential
# ---------------------------------------------------------------------------

def test_github_get_retry_on_401_reloads_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self) -> bytes:
            return b'{"ok":true}'

    class FakeURLHandler:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.first_done = False

        def __call__(self, req, timeout=20):  # noqa: ARG002
            token = req.get_header("Authorization") or ""
            self.calls.append(token)
            if not self.first_done:
                self.first_done = True
                raise urllib.error.HTTPError(
                    url="https://api.github.com/test",
                    code=401,
                    msg="Unauthorized",
                    hdrs={},
                    fp=None,
                )
            return FakeResp()

    captured: list[str] = []

    class FakeLog:
        def __call__(self, level: str, msg: str, **fields: object) -> None:
            captured.append(msg)

    from autocoder_supervisor import supervisor as sup
    monkeypatch.setattr(sup, "log", FakeLog())

    handler = FakeURLHandler()
    monkeypatch.setattr(sup.urllib.request, "urlopen", handler)

    def fake_get_github_token() -> str:
        return "gho_REFRESHED" if handler.first_done else "gho_STALE"

    monkeypatch.setattr(sup, "get_github_token", fake_get_github_token)

    result = github_get("/test", "gho_STALE", _retry_on_401=True)
    assert result == {"ok": True}
    assert handler.calls == ["Bearer gho_STALE", "Bearer gho_REFRESHED"]
    assert any("credential reload" in m for m in captured)


# ---------------------------------------------------------------------------
# TEST 13 — persisted session survives restart
# ---------------------------------------------------------------------------

def test_persisted_session_survives_simulated_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import autocoder_supervisor.worker_session as ws

    fake_hermes = tmp_path / "fake_hermes"
    fake_hermes.write_text("#!/bin/sh\necho ok\n")
    fake_hermes.chmod(0o755)

    persist = tmp_path / "session.json"
    persist.write_text(
        json.dumps({"session_id": "ses_RESTART_PICKUP"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        ws, "_hermes_session_exists", lambda bin, sid, **k: True
    )

    result = resolve_worker_session(
        hermes_bin=str(fake_hermes),
        configured_session_id="ses_STALE_ENV_IGNORED",
        persist_path=persist,
        attempt_id="att-restart-test",
        pr_number=5,
        repo_owner="Slideshow11",
        repo_name="AutoDev",
        feature_branch="feat/test",
        workspace_cwd=tmp_path,
    )
    assert isinstance(result, SessionResolution)
    assert result.session_id == "ses_RESTART_PICKUP"
    assert not result.was_replaced


# ---------------------------------------------------------------------------
# TEST 14 — fresh-session creator uses workspace_cwd
# ---------------------------------------------------------------------------

def test_create_fresh_session_invokes_hermes_with_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from autocoder_supervisor.worker_session import _create_fresh_session

    captured_cwd: list[str] = []

    class FakeProc:
        returncode = 0
        stdout = "session_id: ses_NEW_SESSION\n"
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured_cwd.append(kwargs.get("cwd"))
        return FakeProc()

    monkeypatch.setattr(
        "subprocess.run",
        fake_run,
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _create_fresh_session(
        "/fake/hermes",
        seed_prompt="init",
        cwd=workspace,
    )
    assert captured_cwd and captured_cwd[0] == str(workspace)


# ---------------------------------------------------------------------------
# TEST 15 — fresh-session creator rejects empty stdout
# ---------------------------------------------------------------------------

def test_fresh_session_creator_does_not_pick_arbitrary_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autocoder_supervisor.worker_session import _create_fresh_session

    class FakeProc:
        returncode = 0
        stdout = "Something else\n"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeProc())
    with pytest.raises(RuntimeError):
        _create_fresh_session("/fake/hermes", seed_prompt="init")


# ---------------------------------------------------------------------------
# TEST 16 — pid_alive handles out-of-range PID without crashing
# ---------------------------------------------------------------------------

def test_pid_alive_handles_unreadable_proc() -> None:
    assert pid_alive(2_000_000_000) is False


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
