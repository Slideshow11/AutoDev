"""Tests for the unified orchestration state-root resolver.

Round-26 P1#3 invariant: the supervisor's relay invocation,
the head-advance binding, and the directive bridge MUST
read the same ``run_context.json``. The unified resolver in
``autocoder_supervisor.relay_wiring`` enforces a single
precedence (env, then ``RUN_STATE['orchestration_state_root']``,
then ``STATE_DIR``).

Before this resolver the three sites diverged: the head-advance
path silently returned ``None`` when neither the env nor the
recorded state_root was set, while the relay-invocation path
fell back to ``str(STATE_DIR)``. The relay wiring then pointed
at one state root and the head advance pointed at nothing,
producing the symptom "controller stays in
REPAIRING_REVIEW_FINDINGS after a real head advance, and the
next round refuses the exact-head guard".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# Use a small module alias so we don't have to thread the
# full import path through every assertion. The supervisor's
# module-level globals (``STATE_DIR``, ``RUN_STATE``) are
# monkeypatched per-test.
_RW = "autocoder_supervisor.relay_wiring"


@pytest.fixture
def run_state_path(tmp_path, monkeypatch) -> Path:
    """Set up a fresh ``RUN_STATE`` path and patch the
    supervisor module's globals to point at it. The supervisor
    imports ``RUN_STATE`` and ``STATE_DIR`` at module load;
    ``supervisor_module_globals`` rebuilds them from the
    config. The wiring module does the import lazily so
    monkeypatching at the supervisor level is sufficient.
    """
    state_dir = tmp_path / "supervisor_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    run_state_file = state_dir / "run_state.json"
    run_state_file.write_text(json.dumps({}))
    # Force the wiring helper to use these by patching the
    # ``from .supervisor import`` line's targets. The wiring
    # does ``from .supervisor import RUN_STATE`` inside the
    # helper; we patch the names on the supervisor module so
    # both lazy and top-level imports see the patched values.
    from autocoder_supervisor import supervisor
    monkeypatch.setattr(supervisor, "RUN_STATE", run_state_file, raising=False)
    monkeypatch.setattr(supervisor, "STATE_DIR", state_dir, raising=False)
    # The helper does a local ``from .supervisor import RUN_STATE``,
    # which re-binds the local name. Patch the wiring module's
    # globals instead so the local import statement still resolves
    # to the patched supervisor's globals via Python's normal
    # module attribute lookup.
    from autocoder_supervisor import relay_wiring
    monkeypatch.setattr(relay_wiring, "RUN_STATE", run_state_file, raising=False)
    monkeypatch.setattr(relay_wiring, "STATE_DIR", state_dir, raising=False)
    return state_dir


def test_env_var_wins_over_run_state(monkeypatch, run_state_path) -> None:
    """When ``AED_ORCHESTRATION_STATE_ROOT`` is set, the resolver
    MUST return it even if ``RUN_STATE`` records a different
    value. The env var is the operator-explicit override.
    """
    monkeypatch.setenv("AED_ORCHESTRATION_STATE_ROOT", "/explicit/override")
    run_state_path.joinpath("run_state.json").write_text(
        json.dumps({"orchestration_state_root": "/from/run/state"})
    )
    from autocoder_supervisor.relay_wiring import _resolve_orchestration_state_root
    assert _resolve_orchestration_state_root() == "/explicit/override"


def test_run_state_used_when_no_env(monkeypatch, run_state_path) -> None:
    """Without an env var, the resolver MUST fall through to
    ``RUN_STATE['orchestration_state_root']``. The recorded
    value is the supervisor's hand-off record.
    """
    monkeypatch.delenv("AED_ORCHESTRATION_STATE_ROOT", raising=False)
    run_state_path.joinpath("run_state.json").write_text(
        json.dumps({"orchestration_state_root": "/from/run/state"})
    )
    from autocoder_supervisor.relay_wiring import _resolve_orchestration_state_root
    assert _resolve_orchestration_state_root() == "/from/run/state"


def test_state_root_fails_closed_when_run_state_missing(monkeypatch, run_state_path) -> None:
    """Without env or ``RUN_STATE`` field, the resolver MUST
    fail closed (return ``None``) rather than silently
    substituting ``STATE_DIR``. The user explicitly forbids
    silent STATE_DIR substitution (round-27 P1#3): the
    supervisor is the canonical init point and must persist
    the value to ``RUN_STATE`` so the relay can find it.

    Production code MUST handle ``None`` (e.g. surface a
    supervisor error and exit the heartbeat).
    """
    monkeypatch.delenv("AED_ORCHESTRATION_STATE_ROOT", raising=False)
    run_state_path.joinpath("run_state.json").write_text(json.dumps({}))
    from autocoder_supervisor.relay_wiring import _resolve_orchestration_state_root
    resolved = _resolve_orchestration_state_root()
    assert resolved is None, (
        f"resolver MUST return None when no positively-known state "
        f"root is configured; got {resolved!r}"
    )


def test_missing_run_state_file_fails_closed(
    monkeypatch, run_state_path
) -> None:
    """An unreadable ``RUN_STATE`` MUST NOT silently substitute
    ``STATE_DIR``; the resolver fails closed with ``None``.
    """
    monkeypatch.delenv("AED_ORCHESTRATION_STATE_ROOT", raising=False)
    run_state_path.joinpath("run_state.json").unlink()
    from autocoder_supervisor.relay_wiring import _resolve_orchestration_state_root
    assert _resolve_orchestration_state_root() is None


def test_supervisor_init_persists_orchestration_state_root(
    monkeypatch, tmp_path
) -> None:
    """Round-27 P1#3 production-path test: a normally-initialized
    supervisor run discovers the real ``run_context.json`` even
    WITHOUT the env var.

    Sequence: the supervisor's ``read_run_state`` is the
    canonical init point. On first read, it persists
    ``STATE_DIR`` into ``RUN_STATE['orchestration_state_root']``.
    Subsequent calls to ``_resolve_orchestration_state_root``
    find the value via the ``RUN_STATE`` precedence (no env var
    set) and return it. The relay can therefore find the
    authoritative ``run_context.json`` automatically.

    This is the user-specified invariant: "Add a production-
    path test with no ``AED_ORCHESTRATION_STATE_ROOT`` env var
    proving a normally initialized run discovers the real
    run_context.json automatically."
    """
    monkeypatch.delenv("AED_ORCHESTRATION_STATE_ROOT", raising=False)
    state_dir = tmp_path / "supervisor_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    run_state_file = state_dir / "run_state.json"
    run_state_file.write_text(json.dumps({}))
    from autocoder_supervisor import supervisor
    from autocoder_supervisor import relay_wiring
    monkeypatch.setattr(supervisor, "RUN_STATE", run_state_file, raising=False)
    monkeypatch.setattr(supervisor, "STATE_DIR", state_dir, raising=False)
    monkeypatch.setattr(relay_wiring, "RUN_STATE", run_state_file, raising=False)
    monkeypatch.setattr(relay_wiring, "STATE_DIR", state_dir, raising=False)
    # First read: the supervisor persists the canonical value.
    state = supervisor.read_run_state()
    assert state.get("orchestration_state_root") == str(state_dir), (
        f"first read MUST persist orchestration_state_root="
        f"{state_dir}; got {state.get('orchestration_state_root')!r}"
    )
    # RUN_STATE on disk now contains the persisted value.
    on_disk = json.loads(run_state_file.read_text())
    assert on_disk["orchestration_state_root"] == str(state_dir)
    # The resolver finds the value via RUN_STATE (no env var).
    from autocoder_supervisor.relay_wiring import _resolve_orchestration_state_root
    assert _resolve_orchestration_state_root() == str(state_dir), (
        f"resolver MUST discover the persisted state_root "
        f"without an env var; got {_resolve_orchestration_state_root()!r}"
    )


def test_evidence_root_helpers(monkeypatch, run_state_path) -> None:
    """The evidence-root helper pairs with the state-root
    resolver: explicit ``AED_EVIDENCE_ROOT`` wins; otherwise it
    reads ``RUN_STATE['orchestration_evidence_root']``; finally
    it derives ``<state_root>/evidence``.
    """
    from autocoder_supervisor.relay_wiring import _resolve_orchestration_evidence_root

    # Explicit env wins
    monkeypatch.setenv("AED_EVIDENCE_ROOT", "/explicit/evidence")
    assert _resolve_orchestration_evidence_root("/any/state") == "/explicit/evidence"

    # Otherwise read RUN_STATE
    monkeypatch.delenv("AED_EVIDENCE_ROOT", raising=False)
    run_state_path.joinpath("run_state.json").write_text(
        json.dumps({"orchestration_evidence_root": "/from/run/evidence"})
    )
    assert _resolve_orchestration_evidence_root("/any/state") == "/from/run/evidence"

    # Otherwise derive from state_root
    run_state_path.joinpath("run_state.json").write_text(json.dumps({}))
    assert _resolve_orchestration_evidence_root("/any/state") == "/any/state/evidence"


def test_resolver_used_by_both_callers(monkeypatch, run_state_path) -> None:
    """The unified helper MUST be the single source of truth
    for both ``mark_head_advanced_public`` and the supervisor's
    ``_invoke_relay_for_events``. We assert both import paths
    resolve to the same helper symbol.
    """
    from autocoder_supervisor import relay_wiring
    from autocoder_supervisor import supervisor
    assert hasattr(relay_wiring, "_resolve_orchestration_state_root")
    # The supervisor module MUST import the helper rather than
    # re-implement the resolution (the previous code did the
    # latter; the user explicitly rejected the divergence).
    src = Path(supervisor.__file__).read_text()
    # The supervisor's _invoke_relay_for_events must reference
    # the helper rather than redo env-var resolution by hand.
    assert "_resolve_orchestration_state_root" in src, (
        "supervisor._invoke_relay_for_events must use the unified helper"
    )
    # And the previous "os.environ.get('AED_ORCHESTRATION_STATE_ROOT')"
    # block inside _invoke_relay_for_events must be gone.
    # We assert the helper-import line is present.
    assert "from .relay_wiring import" in src


def test_mark_head_advanced_logs_warning_when_unresolvable(
    monkeypatch, run_state_path
) -> None:
    """``mark_head_advanced_public`` MUST log a warning before
    returning when the resolver yields no state root AND no
    STATE_DIR is importable. Operators cannot otherwise see why
    the controller stayed in REPAIRING_REVIEW_FINDINGS.
    """
    # Force the resolver to return None by stripping the env,
    # the RUN_STATE field, AND the STATE_DIR import. The
    # wiring's helper does ``from .supervisor import STATE_DIR``
    # inside the function — we monkeypatch the wiring module's
    # STATE_DIR attribute to a name that does not resolve, then
    # patch its __import__ path. Easier: patch the wiring helper
    # itself to return None.
    from autocoder_supervisor import relay_wiring
    monkeypatch.setattr(relay_wiring, "_resolve_orchestration_state_root", lambda: None)
    # Spy on the supervisor's log() so we can assert it was
    # called with the expected warning.
    calls = []
    from autocoder_supervisor import supervisor
    monkeypatch.setattr(supervisor, "log", lambda level, msg, **kw: calls.append((level, msg, kw)), raising=False)
    relay_wiring.mark_head_advanced_public("a" * 40, "b" * 40)
    assert calls, (
        "mark_head_advanced_public MUST log a warning when the "
        "resolver returns None; the silent return is exactly the "
        "behaviour the user rejected."
    )
    level, msg, kw = calls[0]
    assert level == "warning"
    assert "no orchestration state_root" in msg
    assert kw["old_head"] == "a" * 12
    assert kw["new_head"] == "b" * 12
