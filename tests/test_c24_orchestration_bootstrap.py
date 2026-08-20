from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from autocoder_orchestration.context import RunContext
from autocoder_orchestration.state_machine import StateMachine
from autocoder_supervisor.orchestration_bootstrap import (
    bootstrap_orchestration_state_root,
)
from autocoder_supervisor.orchestration_state_root import (
    OrchestrationRootError,
    OrchestrationRootUnverified,
    resolve_orchestration_state_root,
)


HEAD = "fc3661fbc79618c8d734561cbbf2dae87938bf7b"
OWNER = "Slideshow11"
REPO = "AutoDev"
PR = 9
BRANCH = "feat/c23-fresh-review-requests"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _writer(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _args(tmp_path: Path) -> dict:
    state_dir = tmp_path / "supervisor-state"
    return {
        "state_dir": state_dir,
        "state_root_parent": tmp_path / "orchestration-runs",
        "repo_owner": OWNER,
        "repo_name": REPO,
        "pr_number": PR,
        "branch": BRANCH,
        "run_state_path": state_dir / "run_state.json",
        "current_authorized_head": HEAD,
        "authorized_base_sha": HEAD,
        "writer": _writer,
        "local_checkout": REPO_ROOT,
    }


def _bootstrap(tmp_path: Path) -> Path:
    return Path(bootstrap_orchestration_state_root(**_args(tmp_path)))


def test_clean_new_run_creates_dedicated_root(tmp_path: Path) -> None:
    root = _bootstrap(tmp_path)
    assert root.is_dir()
    assert root != (_args(tmp_path)["state_dir"])


def test_created_root_contains_valid_run_context(tmp_path: Path) -> None:
    root = _bootstrap(tmp_path)
    context = RunContext.from_dict(json.loads((root / "run_context.json").read_text()))
    assert context.repo_owner == OWNER
    assert context.repo_name == REPO
    assert context.pr_number == PR
    assert context.current_authorized_head == HEAD
    assert context.feature_branch == BRANCH
    assert Path(context.state_path) == root


def test_created_root_contains_valid_controller_state(tmp_path: Path) -> None:
    root = _bootstrap(tmp_path)
    state = StateMachine.from_dict(json.loads((root / "state.json").read_text()))
    assert state.current_state == "PLANNED"


def test_concrete_root_persisted_to_run_state(tmp_path: Path) -> None:
    root = _bootstrap(tmp_path)
    run_state = json.loads(_args(tmp_path)["run_state_path"].read_text())
    assert run_state["orchestration_state_root"] == str(root)
    assert run_state["last_bound_repo_owner"] == OWNER
    assert run_state["last_bound_pr_number"] == PR


def test_resolver_returns_same_verified_root(tmp_path: Path) -> None:
    root = _bootstrap(tmp_path)
    run_state_path = _args(tmp_path)["run_state_path"]
    run_state = json.loads(run_state_path.read_text())
    assert resolve_orchestration_state_root(
        env={},
        run_state_path=run_state_path,
        expected_repo=f"{OWNER}/{REPO}",
        expected_run_id=run_state["last_bound_run_id"],
        expected_pr_number=PR,
    ) == str(root)


def test_restart_reuses_same_root(tmp_path: Path) -> None:
    first = _bootstrap(tmp_path)
    second = _bootstrap(tmp_path)
    assert second == first
    roots = [
        p for p in (_args(tmp_path)["state_root_parent"] / OWNER / REPO / f"pr-{PR}").iterdir()
        if p.is_dir() and not p.name.startswith(".")
    ]
    assert roots == [first]


def _mutate_context(root: Path, **changes) -> None:
    path = root / "run_context.json"
    payload = json.loads(path.read_text())
    payload.update(changes)
    _writer(path, payload)


def test_corrupt_existing_root_blocks_without_replacement(tmp_path: Path) -> None:
    root = _bootstrap(tmp_path)
    (root / "run_context.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(OrchestrationRootUnverified):
        _bootstrap(tmp_path)
    assert json.loads(_args(tmp_path)["run_state_path"].read_text())[
        "orchestration_state_root"
    ] == str(root)


@pytest.mark.parametrize(
    "changes",
    [
        {"repo_name": "DifferentRepo"},
        {"pr_number": 10},
        {"run_id": "different-run-id"},
    ],
    ids=["mismatched-repo", "mismatched-pr", "mismatched-run-id"],
)
def test_mismatched_existing_binding_blocks(tmp_path: Path, changes: dict) -> None:
    root = _bootstrap(tmp_path)
    _mutate_context(root, **changes)
    with pytest.raises(OrchestrationRootUnverified):
        _bootstrap(tmp_path)


def test_concurrent_initialization_has_one_authoritative_root(tmp_path: Path) -> None:
    args = _args(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: bootstrap_orchestration_state_root(**args), range(2)))
    assert results[0] == results[1]
    run_state = json.loads(args["run_state_path"].read_text())
    assert run_state["orchestration_state_root"] == results[0]


def test_state_dir_is_never_used_as_orchestration_root(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args["state_root_parent"] = args["state_dir"]
    with pytest.raises(OrchestrationRootError):
        bootstrap_orchestration_state_root(**args)


def test_explicit_root_continues_to_win(tmp_path: Path) -> None:
    root = _bootstrap(tmp_path)
    assert resolve_orchestration_state_root(
        env={"AED_ORCHESTRATION_STATE_ROOT": str(root)},
        run_state_path=tmp_path / "absent.json",
        expected_repo=f"{OWNER}/{REPO}",
        expected_pr_number=PR,
    ) == str(root)


def test_invalid_unpersisted_candidate_blocks_replacement(tmp_path: Path) -> None:
    args = _args(tmp_path)
    candidate = (
        args["state_root_parent"] / OWNER / REPO / f"pr-{PR}" / "stale-run"
    )
    candidate.mkdir(parents=True)
    (candidate / "run_context.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(OrchestrationRootUnverified):
        bootstrap_orchestration_state_root(**args)
