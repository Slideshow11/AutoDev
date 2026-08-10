"""Round-38 stderr extraction regression test."""
import sys
from pathlib import Path
sys.path.insert(0, '/home/max/AutoDev')


def test_create_fresh_session_reads_session_id_from_stderr(
    monkeypatch,
) -> None:
    """Round-38 forensic note: hermes emits ``session_id: <id>``
    on STDERR after the session-init banner. The fresh-session
    creator MUST scan stderr first so it doesn't miss the
    marker when the response body is on stdout.
    """
    from autocoder_supervisor.worker_session import _create_fresh_session
    import autocoder_supervisor.worker_session as ws

    class FakeProc:
        returncode = 0
        stdout = "Initialized. Quick read on the situation:\n\nSystem ready — WSL host.\n"
        stderr = "\nsession_id: 20260810_133248_d96940\n"

    captured = {"called": False}

    def fake_run(cmd, **kwargs):
        captured["called"] = True
        return FakeProc()

    monkeypatch.setattr(ws.subprocess, "run", fake_run)
    result = _create_fresh_session(
        "/fake/hermes",
        seed_prompt="init",
        cwd=Path("/tmp"),
    )
    assert captured["called"]
    assert result == "20260810_133248_d96940"


def test_create_fresh_session_falls_back_to_stdout(
    monkeypatch,
) -> None:
    """If a future hermes emits the marker on stdout instead
    of stderr, the round-38 helper MUST still extract it
    (the source-search order tolerates both streams).
    """
    from autocoder_supervisor.worker_session import _create_fresh_session
    import autocoder_supervisor.worker_session as ws

    class FakeProc:
        returncode = 0
        stdout = "session_id: 20260810_FUTURE_STDOUT\nResponse body here.\n"
        stderr = ""  # nothing on stderr

    monkeypatch.setattr(
        ws.subprocess, "run", lambda *a, **k: FakeProc()
    )
    result = _create_fresh_session("/fake/hermes", seed_prompt="x")
    assert result == "20260810_FUTURE_STDOUT"
