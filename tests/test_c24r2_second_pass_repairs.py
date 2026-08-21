"""Round-C24-R2 / CodeRabbit second-pass repair regressions.

The second CodeRabbit review (PR #9, review 4992253234, exact head
f360a616bf1928e6a47b87389b7f3daf801b4a50) reported three
stability defects. Each was independently verified against
source before repair:

  SP-1 (Major): ``_git_superseding_repair_committed_at`` indexed
       ``splitlines()[0]`` on a possibly-empty ``git log`` stdout
       (exit 0, empty output == anchor not an ancestor). IndexError
       is not in the helper's except tuple, so it propagated out of
       the documented must-not-raise ``capture_live_snapshot``.
  SP-2 (Minor): ``launch_worker`` bound ``WorkerRepoAuthUnavailable``
       and called the preflight inside ONE try block; an ImportError
       left the name unbound when the ``except`` expression was
       evaluated, replacing the ImportError with a NameError that
       aborted standalone-mode worker launch.
  SP-3 (Minor): ``_reconcile_orchestration_state_root_at_boot``
       re-read ``run_context.json`` unguarded after the resolver had
       verified it; a concurrent removal/truncation raised
       OSError / JSONDecodeError out of the boot path.

Each test below FAILS against the pre-repair code (exception
propagates) and PASSES after the narrow repair.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(os.environ.get("REPO_ROOT", str(Path(__file__).resolve().parent.parent)))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class TestSP1GitLogEmptyOutput:
    def test_empty_git_log_stdout_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``git log --ancestry-path anchor..head`` exits 0 with
        EMPTY stdout when the anchor is not an ancestor (rebased
        away). The helper must return None (fail-closed), never
        raise IndexError."""
        import subprocess

        from autocoder_supervisor import supervisor as sup

        class _FakeCompleted:
            returncode = 0
            stdout = ""

        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _FakeCompleted(),
        )
        monkeypatch.setattr(sup, "REPO_DIR", str(tmp_path))

        result = sup._git_superseding_repair_committed_at(
            "a" * 40, "b" * 40,
        )
        assert result is None, (
            "empty git-log output (non-descendant anchor) must map "
            "to the None fail-closed signal, not IndexError"
        )

    def test_none_stdout_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """stdout=None (possible with capture edge cases) must not
        crash either."""
        import subprocess

        from autocoder_supervisor import supervisor as sup

        class _FakeCompleted:
            returncode = 0
            stdout = None

        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _FakeCompleted(),
        )
        monkeypatch.setattr(sup, "REPO_DIR", str(tmp_path))
        assert sup._git_superseding_repair_committed_at(
            "a" * 40, "b" * 40,
        ) is None


class TestSP2LaunchWorkerImportGuard:
    def test_unimportable_preflight_module_does_not_raise_nameerror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``worker_auth_preflight`` cannot be imported
        (standalone launch mode), ``launch_worker`` must degrade to
        skip-preflight and still launch the worker — NOT die with
        ``NameError: WorkerRepoAuthUnavailable`` from the except
        clause evaluating an unbound name."""
        from autocoder_supervisor import supervisor as sup

        # Identity guard off (test affordance); preflight skip NOT
        # set so the guarded block actually executes.
        monkeypatch.setenv("AED_SKIP_IDENTITY_GUARD", "1")
        monkeypatch.delenv("AED_SKIP_WORKER_AUTH_PREFLIGHT", raising=False)
        # Force the lazy submodule import to raise ImportError.
        # Binding None in sys.modules makes ``from .x import y``
        # raise ImportError deterministically.
        monkeypatch.setitem(
            sys.modules, "autocoder_supervisor.worker_auth_preflight", None,
        )
        monkeypatch.setenv("AED_EVIDENCE_ROOT", str(tmp_path / "evidence"))
        (tmp_path / "evidence").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            sup, "WORKER_COMMAND_TEMPLATE",
            ["echo", "{prompt}", "{session_id}"],
        )
        monkeypatch.setattr(sup, "SESSION_ID", "test-session")
        monkeypatch.setattr(sup, "INSTANCE_ID", "test-instance")
        monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", "a" * 40)
        monkeypatch.setattr(sup, "PR_NUMBER", 4)
        monkeypatch.setattr(sup, "REPO_OWNER", "owner")
        monkeypatch.setattr(sup, "REPO_NAME", "repo")

        captured_cmd: list = []

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                captured_cmd.extend(cmd)
                self.pid = 99999

        monkeypatch.setattr(sup.subprocess, "Popen", FakePopen)
        monkeypatch.setattr(sup, "write_lease", lambda lease: None)
        monkeypatch.setattr(sup, "write_cooldown", lambda: None)
        monkeypatch.setattr(
            sup, "start_time_evidence", lambda pid: {"pid": pid},
        )
        # Hermeticity: the strict dirty-tree pre-launch guard is
        # orthogonal to SP-2 (covered by its own suite) and would
        # otherwise trip on whatever the developer's worktree looks
        # like at test time. Stub it positively.
        monkeypatch.setattr(
            sup, "_check_clean_production_checkout",
            lambda path: (True, [], ""),
        )

        # Pre-repair this raised NameError out of launch_worker.
        lease = sup.launch_worker(
            {"current_head": "a" * 40}, {"snapshot": {}},
        )
        assert lease is not None, (
            "an unimportable preflight module must degrade to "
            "skip-preflight, not abort the worker launch"
        )


class TestSP3BootReReadGuard:
    @pytest.fixture()
    def orch(self, tmp_path: Path) -> Path:
        """A canonical, positively-verifiable orchestration state
        root (same shape the C24 fixtures use successfully)."""
        orch = tmp_path / "orch_state"
        orch.mkdir(parents=True)
        task_spec = tmp_path / "task.md"
        task_spec.write_text("task")
        (orch / "run_context.json").write_text(json.dumps({
            "schema_version": "autocoder.run_context.v1",
            "run_id": "r-sp3",
            "repo_owner": "owner",
            "repo_name": "repo",
            "local_checkout": str(tmp_path),
            "base_branch": "main",
            "authorized_base_sha": "e" * 64,
            "feature_branch": "feat/test",
            "task_specification_path": str(task_spec),
            "task_specification_sha256": "f" * 64,
            "required_ci_jobs": [],
            "implementation_worker_command": [],
            "evidence_root": str(tmp_path / "evidence"),
            "state_root": str(orch),
            "pr_number": 4,
            "current_authorized_head": "a" * 64,
        }))
        (orch / "state.json").write_text(json.dumps({
            "schema_version": "autocoder.state_machine.v1",
            "current_state": "AWAITING_CI",
        }))
        os.chmod(orch / "run_context.json", 0o600)
        os.chmod(orch / "state.json", 0o600)
        return orch

    def test_run_context_vanishing_mid_boot_fails_closed(
        self, orch: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Simulate the file being removed/truncated between the
        resolver's verification read and the function's own
        re-read: the boot reconciler must return "" (fail closed)
        instead of letting OSError/JSONDecodeError abort boot."""
        from autocoder_supervisor import supervisor as sup

        monkeypatch.setenv(
            "AED_ORCHESTRATION_STATE_ROOT", str(orch),
        )
        # The resolver and persistence helper live in their own
        # module with their own json binding; faking them at the
        # source leaves the function's own unguarded re-read of
        # ``run_context.json`` as the ONLY json.loads caller in
        # this code path — which is exactly the access under test.
        monkeypatch.setattr(
            sup, "read_run_state", lambda: {"last_bound_run_id": None},
        )

        import autocoder_supervisor.orchestration_state_root as osr

        monkeypatch.setattr(
            osr,
            "resolve_orchestration_state_root",
            lambda **kwargs: str(orch),
        )
        persisted: list = []

        def _fake_persist(**kwargs):
            persisted.append(kwargs)
            return True

        monkeypatch.setattr(
            osr, "persist_orchestration_state_root", _fake_persist,
        )

        # Now make ONLY the function's own re-read blow up: patch
        # Path.read_text for run_context.json via a wrapper that
        # raises OSError, while leaving everything else intact.
        import pathlib

        real_read_text = pathlib.Path.read_text

        def exploding_read_text(self, *args, **kwargs):
            if self.name == "run_context.json":
                raise OSError("simulated concurrent truncation")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(
            pathlib.Path, "read_text", exploding_read_text,
        )

        # Pre-repair this raised OSError out of the boot path.
        result = sup._reconcile_orchestration_state_root_at_boot()
        assert result == "", (
            "a vanished run_context.json mid-boot must fail closed "
            "to the documented empty-string contract"
        )
        assert not persisted, (
            "no persistence may occur when the re-read fails closed"
        )
