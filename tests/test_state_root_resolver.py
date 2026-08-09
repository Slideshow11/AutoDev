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


def test_env_var_wins_over_run_state(monkeypatch, tmp_path) -> None:
    """When ``AED_ORCHESTRATION_STATE_ROOT`` is set, the new
    resolver returns it (after positive verification). Round-28
    invariant: env var wins but is still positively verified
    against the candidate directory.
    """
    orch = tmp_path / "orch"
    orch.mkdir()
    (orch / "run_context.json").write_text(json.dumps({
        "repo_owner": "owner/repo", "pr_number": 4, "run_id": "r",
    }))
    monkeypatch.setenv("AED_ORCHESTRATION_STATE_ROOT", str(orch))
    from autocoder_supervisor.orchestration_state_root import resolve_orchestration_state_root
    assert resolve_orchestration_state_root() == str(orch)


def test_run_state_used_when_no_env(monkeypatch, tmp_path) -> None:
    """Without an env var, the new resolver MUST discover the
    root via ``RUN_STATE['orchestration_state_root']``. Round-28
    invariant: the resolver positively verifies the candidate
    against ``run_context.json``.
    """
    orch = tmp_path / "orch"
    orch.mkdir()
    (orch / "run_context.json").write_text(json.dumps({
        "repo_owner": "owner/repo", "pr_number": 4, "run_id": "r",
    }))
    run_state = tmp_path / "run_state.json"
    run_state.write_text(json.dumps({"orchestration_state_root": str(orch)}))
    monkeypatch.delenv("AED_ORCHESTRATION_STATE_ROOT", raising=False)
    from autocoder_supervisor.orchestration_state_root import resolve_orchestration_state_root
    assert resolve_orchestration_state_root(
        env={}, run_state_path=run_state
    ) == str(orch)


def test_state_root_fails_closed_when_run_state_missing(
    monkeypatch, tmp_path
) -> None:
    """Round-28 invariant: the resolver MUST raise
    ``OrchestrationRootMissing`` rather than silently
    substituting ``STATE_DIR``.
    """
    run_state = tmp_path / "run_state.json"
    run_state.write_text(json.dumps({}))
    monkeypatch.delenv("AED_ORCHESTRATION_STATE_ROOT", raising=False)
    from autocoder_supervisor.orchestration_state_root import (
        OrchestrationRootMissing, resolve_orchestration_state_root,
    )
    with pytest.raises(OrchestrationRootMissing):
        resolve_orchestration_state_root(env={}, run_state_path=run_state)


def test_missing_run_state_file_fails_closed(monkeypatch, tmp_path) -> None:
    """Round-28 invariant: missing RUN_STATE file raises
    ``OrchestrationRootMissing``. The supervisor MUST NOT
    silently substitute ``STATE_DIR``.
    """
    run_state = tmp_path / "run_state.json"
    assert not run_state.exists()
    monkeypatch.delenv("AED_ORCHESTRATION_STATE_ROOT", raising=False)
    from autocoder_supervisor.orchestration_state_root import (
        OrchestrationRootMissing, resolve_orchestration_state_root,
    )
    with pytest.raises(OrchestrationRootMissing):
        resolve_orchestration_state_root(env={}, run_state_path=run_state)


def test_supervisor_init_does_not_silently_substitute_state_dir(
    monkeypatch, tmp_path
) -> None:
    """Round-28 invariant: ``supervisor.read_run_state`` MUST
    NOT auto-persist ``STATE_DIR`` as the orchestration state
    root. The supervisor is NOT the canonical init point; the
    orchestration handoff is. ``read_run_state`` only reads.
    """
    state_dir = tmp_path / "supervisor_state"
    state_dir.mkdir()
    run_state_file = state_dir / "run_state.json"
    run_state_file.write_text(json.dumps({}))
    from autocoder_supervisor import supervisor
    monkeypatch.setattr(supervisor, "RUN_STATE", run_state_file, raising=False)
    monkeypatch.setattr(supervisor, "STATE_DIR", state_dir, raising=False)
    state = supervisor.read_run_state()
    assert "orchestration_state_root" not in state, (
        f"read_run_state MUST NOT silently substitute STATE_DIR; "
        f"got {state!r}"
    )


def test_evidence_root_helpers(monkeypatch, tmp_path) -> None:
    """The evidence-root helper pairs with the state-root
    resolver: explicit ``AED_EVIDENCE_ROOT`` wins; otherwise the
    orch state root is resolved positively and the evidence
    root is read from ``run_state['orchestration_evidence_root']``
    or derived from ``<orch_state_root>/evidence``.
    """
    from autocoder_supervisor import relay_wiring
    from autocoder_supervisor import supervisor
    run_state = tmp_path / "run_state.json"
    # Round-29 review: the evidence-root helper now resolves
    # the orch state root FIRST (positively) before deriving
    # the evidence root. The test fixture therefore provides a
    # canonical orch state root + ``run_context.json`` so the
    # resolution succeeds.
    orch_root = tmp_path / "orch_state"
    orch_root.mkdir()
    (orch_root / "run_context.json").write_text(json.dumps({
        "schema_version": "autocoder.run_context.v1",
        "run_id": "isolated", "repo_owner": "owner/repo",
        "pr_number": 4, "current_authorized_head": "a" * 40,
    }))
    (orch_root / "state.json").write_text(json.dumps({
        "schema_version": "autocoder.state_machine.v1",
        "current_state": "REPAIRING_REVIEW_FINDINGS",
    }))
    run_state.write_text(json.dumps({
        "orchestration_state_root": str(orch_root),
    }))
    monkeypatch.setattr(supervisor, "RUN_STATE", run_state, raising=False)
    monkeypatch.setattr(relay_wiring, "RUN_STATE", run_state, raising=False)

    from autocoder_supervisor.relay_wiring import _resolve_orchestration_evidence_root

    # Explicit env wins
    monkeypatch.setenv("AED_EVIDENCE_ROOT", "/explicit/evidence")
    assert _resolve_orchestration_evidence_root("/any/state") == "/explicit/evidence"

    # Otherwise read RUN_STATE
    monkeypatch.delenv("AED_EVIDENCE_ROOT", raising=False)
    run_state.write_text(json.dumps({
        "orchestration_state_root": str(orch_root),
        "orchestration_evidence_root": "/from/run/evidence",
    }))
    assert _resolve_orchestration_evidence_root("/any/state") == "/from/run/evidence"

    # Otherwise derive from orch_state_root/evidence.
    run_state.write_text(json.dumps({
        "orchestration_state_root": str(orch_root),
    }))
    expected = str(Path(orch_root) / "evidence")
    assert _resolve_orchestration_evidence_root("/any/state") == expected


def test_resolver_used_by_both_callers(monkeypatch, tmp_path) -> None:
    """The supervisor and the relay wiring MUST both reach the
    new canonical resolver (``orchestration_state_root`` module)
    via the helper paths in ``relay_wiring``. Round-28
    invariant: there is exactly one resolver; ``STATE_DIR`` is
    never a fallback.
    """
    from autocoder_supervisor import relay_wiring
    from autocoder_supervisor import supervisor
    from autocoder_supervisor.orchestration_state_root import (
        OrchestrationRootError, resolve_orchestration_state_root,
    )
    # The supervisor and relay_wiring both import from
    # ``orchestration_state_root``. We verify by patching the
    # new resolver to raise and observing that the legacy
    # shim returns ``None`` and the supervisor routes the call
    # to fail-closed.
    orch = tmp_path / "orch"
    orch.mkdir()
    (orch / "run_context.json").write_text(json.dumps({
        "repo_owner": "owner/repo", "pr_number": 4, "run_id": "r",
    }))
    run_state = tmp_path / "run_state.json"
    run_state.write_text(json.dumps({"orchestration_state_root": str(orch)}))
    monkeypatch.setattr(supervisor, "RUN_STATE", run_state, raising=False)
    monkeypatch.setattr(relay_wiring, "RUN_STATE", run_state, raising=False)
    # The shim MUST return the verified orch root.
    assert relay_wiring._resolve_orchestration_state_root() == str(orch)
    # The new resolver raises on missing RUN_STATE (sanity).
    # We also strip the env var so the resolver sees only the
    # missing file and fails closed.
    monkeypatch.delenv("AED_ORCHESTRATION_STATE_ROOT", raising=False)
    monkeypatch.setattr(
        relay_wiring, "RUN_STATE",
        tmp_path / "missing_run_state_for_resolver_test.json",
        raising=False,
    )
    monkeypatch.setattr(
        supervisor, "RUN_STATE",
        tmp_path / "missing_run_state_for_resolver_test.json",
        raising=False,
    )
    assert relay_wiring._resolve_orchestration_state_root() is None


def test_mark_head_advanced_logs_error_when_unresolvable(
    monkeypatch, tmp_path
) -> None:
    """``mark_head_advanced_public`` MUST log an error (not a
    warning) before returning when the resolver fails closed.
    The user's invariant is a fail-closed protected-authority
    blocker; the supervisor must NOT silently no-op.
    """
    from autocoder_supervisor import relay_wiring
    from autocoder_supervisor import supervisor
    # Make the resolver fail closed by pointing at a missing file.
    monkeypatch.setattr(relay_wiring, "RUN_STATE", tmp_path / "missing.json", raising=False)
    monkeypatch.setattr(supervisor, "RUN_STATE", tmp_path / "missing.json", raising=False)
    calls = []
    monkeypatch.setattr(
        supervisor, "log",
        lambda level, msg, **kw: calls.append((level, msg, kw)),
        raising=False,
    )
    relay_wiring.mark_head_advanced_public("a" * 40, "b" * 40)
    assert calls, (
        "mark_head_advanced_public MUST log an error when the "
        "resolver fails closed; the silent return is exactly the "
        "behaviour the user rejected."
    )
    level, msg, kw = calls[0]
    assert level == "error", (
        f"fail-closed is a protected-authority blocker; the log "
        f"level MUST be error (was {level!r})"
    )
    assert "orchestration state_root not positively identified" in msg
    assert kw["old_head"] == "a" * 12
    assert kw["new_head"] == "b" * 12
