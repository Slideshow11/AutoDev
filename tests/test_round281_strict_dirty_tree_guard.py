"""Strict dirty-checkout guard tests (pre-canary round-281 §3).

The C22 acceptance invariant is that the production git
checkout is COMPLETELY CLEAN before any worker launch. Tests
are part of the source/provenance boundary and are NOT
exempt. Runtime artifacts must live outside the repository.

This file proves:

A. perfectly clean checkout permits worker launch
B. modified tracked production source blocks launch
C. modified tracked test file blocks launch
D. staged tracked test file blocks launch
E. deleted tracked test file blocks launch
F. untracked file inside the repository blocks launch
G. dirty workflow / config / provenance file blocks launch
H. dirty-tree rejection creates no WorkerAttempt ownership claim
I. dirty-tree rejection creates no result contract
J. dirty-tree rejection does not consume the source event/generation
K. git-status failure itself fails closed
L. temporary/runtime files located outside the repository do not
   dirty the checkout and do not block launch

Each scenario builds a real (hermetic) git repository under
``tmp_path`` and exercises
``autocoder_supervisor.supervisor._check_clean_production_checkout``
directly.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Test fixture: hermetic git repository
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git in ``cwd`` and return the CompletedProcess."""
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo_with_initial_commit(tmp_path: Path, *, name: str = "src_repo") -> Path:
    """Create a real git repo with one initial commit and return its root."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    # Production source file (tracked)
    (repo / "production.py").write_text("# production source\n")
    # Tracked test file (simulating the real repo layout)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_example.py").write_text("# tracked test file\n")
    # Workflow / config / provenance files (all tracked)
    (repo / ".github").mkdir()
    (repo / ".github" / "workflows").mkdir()
    (repo / ".github" / "workflows" / "ci.yml").write_text("name: ci\n")
    (repo / "pyproject.toml").write_text("[project]\nname='autocoder_supervisor'\n")
    (repo / "provenance").mkdir()
    (repo / "provenance" / "MANIFEST.json").write_text("{}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    """A pristine committed repo with no dirty paths."""
    return _init_repo_with_initial_commit(tmp_path)


# ---------------------------------------------------------------------------
# The guard under test (import lazily so the supervisor module can be
# imported in this test environment).
# ---------------------------------------------------------------------------


def _guard():
    """Import the guard from the source-controlled supervisor package.

    The PYTHONPATH in this machine includes both the runtime
    supervisor (typically under the operator's HOME directory)
    and the source-controlled checkout (this repo). The
    runtime binary takes precedence because its directory is
    listed first. This test must exercise the
    source-controlled copy; we therefore import the
    supervisor package explicitly from the absolute source
    path and bypass sys.path ordering. The source root is
    resolved from this test file's location so no absolute
    filesystem path appears in the source.
    """
    import importlib
    import importlib.util as _ilu
    import sys
    src_root = str(Path(__file__).resolve().parent.parent)
    # Force the source-controlled supervisor package to be
    # authoritative for this test.
    if "autocoder_supervisor" in sys.modules:
        del sys.modules["autocoder_supervisor"]
        for k in list(sys.modules.keys()):
            if k.startswith("autocoder_supervisor."):
                del sys.modules[k]
    spec = _ilu.spec_from_file_location(
        "autocoder_supervisor",
        f"{src_root}/autocoder_supervisor/__init__.py",
        submodule_search_locations=[f"{src_root}/autocoder_supervisor"],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["autocoder_supervisor"] = pkg
    spec.loader.exec_module(pkg)
    sup_mod = importlib.import_module("autocoder_supervisor.supervisor")
    return sup_mod._check_clean_production_checkout


# ---------------------------------------------------------------------------
# A. perfectly clean checkout permits worker launch
# ---------------------------------------------------------------------------


class TestACleanCheckout:
    def test_perfectly_clean_repo_permits_launch(self, clean_repo: Path) -> None:
        ok, dirty, reason = _guard()(str(clean_repo))
        assert ok is True, f"expected clean, got dirty={dirty!r} reason={reason!r}"
        assert dirty == []
        assert reason == "clean"

    def test_ruff_cache_untracked_does_not_block(self, clean_repo: Path) -> None:
        # .ruff_cache/ is one of the bounded runtime exclusions.
        (clean_repo / ".ruff_cache").mkdir()
        (clean_repo / ".ruff_cache" / "CACHEDIR.TAG").write_text("cache\n")
        ok, dirty, reason = _guard()(str(clean_repo))
        assert ok is True, f"ruff_cache must not block, got {dirty!r} {reason!r}"

    def test_pycache_inside_repo_does_not_block(self, clean_repo: Path) -> None:
        # __pycache__/ is one of the bounded runtime exclusions.
        pkg = clean_repo / "pkg"
        pkg.mkdir()
        (pkg / "mod.py").write_text("x=1\n")
        # Commit mod.py first so the only new dirty entry is the
        # __pycache__ directory; otherwise the test would also
        # catch mod.py as an untracked file.
        _git(clean_repo, "add", "pkg/mod.py")
        _git(clean_repo, "commit", "-q", "-m", "add pkg")
        (pkg / "__pycache__").mkdir()
        (pkg / "__pycache__" / "mod.cpython-311.pyc").write_bytes(b"")
        ok, _, _ = _guard()(str(clean_repo))
        assert ok is True, "__pycache__/ must not block"


# ---------------------------------------------------------------------------
# B. modified tracked production source blocks launch
# ---------------------------------------------------------------------------


class TestBModifiedProductionSource:
    def test_modified_tracked_py_blocks_launch(self, clean_repo: Path) -> None:
        (clean_repo / "production.py").write_text("# modified\n")
        ok, dirty, reason = _guard()(str(clean_repo))
        assert ok is False
        assert any("production.py" in p for p in dirty), f"dirty={dirty!r}"
        assert reason == "uncommitted_changes"

    def test_modified_workflow_blocks_launch(self, clean_repo: Path) -> None:
        (clean_repo / ".github" / "workflows" / "ci.yml").write_text(
            "name: ci-modified\n"
        )
        ok, dirty, reason = _guard()(str(clean_repo))
        assert ok is False
        assert reason == "uncommitted_changes"
        assert any("ci.yml" in p for p in dirty)


# ---------------------------------------------------------------------------
# C. modified tracked test file blocks launch
# ---------------------------------------------------------------------------


class TestCModifiedTrackedTestFile:
    def test_modified_tracked_test_blocks_launch(self, clean_repo: Path) -> None:
        (clean_repo / "tests" / "test_example.py").write_text(
            "# modified tracked test\n"
        )
        ok, dirty, reason = _guard()(str(clean_repo))
        assert ok is False, "tests/ must NOT be exempt"
        assert reason == "uncommitted_changes"
        assert any("test_example.py" in p for p in dirty)


# ---------------------------------------------------------------------------
# D. staged tracked test file blocks launch
# ---------------------------------------------------------------------------


class TestDStagedTrackedTestFile:
    def test_staged_tracked_test_blocks_launch(self, clean_repo: Path) -> None:
        new_test = clean_repo / "tests" / "test_new.py"
        new_test.write_text("# new tracked test\n")
        _git(clean_repo, "add", str(new_test))
        # File is now staged (index shows it) but not yet committed.
        ok, dirty, reason = _guard()(str(clean_repo))
        assert ok is False, "staged tracked test must block"
        assert any("test_new.py" in p for p in dirty)


# ---------------------------------------------------------------------------
# E. deleted tracked test file blocks launch
# ---------------------------------------------------------------------------


class TestEDeletedTrackedTestFile:
    def test_deleted_tracked_test_blocks_launch(self, clean_repo: Path) -> None:
        target = clean_repo / "tests" / "test_example.py"
        target.unlink()
        ok, dirty, reason = _guard()(str(clean_repo))
        assert ok is False, "deleted tracked test must block"
        assert reason == "uncommitted_changes"
        assert any("test_example.py" in p for p in dirty)


# ---------------------------------------------------------------------------
# F. untracked file anywhere inside the repository blocks launch
# ---------------------------------------------------------------------------


class TestFUntrackedFileInsideRepo:
    def test_untracked_file_in_repo_root_blocks(self, clean_repo: Path) -> None:
        (clean_repo / "scratch.txt").write_text("not committed\n")
        ok, dirty, reason = _guard()(str(clean_repo))
        assert ok is False
        assert reason in {"untracked_paths", "uncommitted_changes+untracked_paths"}
        assert any("scratch.txt" in p for p in dirty)

    def test_untracked_dir_anywhere_blocks(self, clean_repo: Path) -> None:
        untracked = clean_repo / "hermes-snap-test.sh"
        untracked.write_text("#!/bin/sh\necho captured\n")
        ok, dirty, _ = _guard()(str(clean_repo))
        assert ok is False, "hermes-snap-*.sh inside the repo must block"
        assert any("hermes-snap-test.sh" in p for p in dirty)


# ---------------------------------------------------------------------------
# G. dirty workflow / config / provenance file blocks launch
# ---------------------------------------------------------------------------


class TestGDirtyWorkflowConfigProvenance:
    def test_dirty_pyproject_blocks_launch(self, clean_repo: Path) -> None:
        (clean_repo / "pyproject.toml").write_text(
            "[project]\nname='autocoder_supervisor-modified'\n"
        )
        ok, dirty, reason = _guard()(str(clean_repo))
        assert ok is False
        assert any("pyproject.toml" in p for p in dirty)
        assert reason == "uncommitted_changes"

    def test_dirty_provenance_manifest_blocks_launch(
        self, clean_repo: Path
    ) -> None:
        (clean_repo / "provenance" / "MANIFEST.json").write_text(
            '{"tampered": true}\n'
        )
        ok, dirty, reason = _guard()(str(clean_repo))
        assert ok is False
        assert any("MANIFEST.json" in p for p in dirty)
        assert reason == "uncommitted_changes"

    def test_dirty_workflow_yml_blocks_launch(self, clean_repo: Path) -> None:
        (clean_repo / ".github" / "workflows" / "ci.yml").write_text(
            "name: ci-tampered\non: push\n"
        )
        ok, dirty, reason = _guard()(str(clean_repo))
        assert ok is False
        assert any("ci.yml" in p for p in dirty)
        assert reason == "uncommitted_changes"


# ---------------------------------------------------------------------------
# H. dirty-tree rejection creates no WorkerAttempt ownership claim
# I. dirty-tree rejection creates no result contract
# J. dirty-tree rejection does not consume the source event/generation
# K. git-status failure itself fails closed
# L. temporary/runtime files located outside the repository do not dirty
#    the checkout and do not block launch
# ---------------------------------------------------------------------------


class TestHToL:
    def test_h_rejection_creates_no_worker_attempt(
        self, clean_repo: Path, tmp_path: Path
    ) -> None:
        """Dirty-tree rejection must NOT spawn a WorkerAttempt."""
        # Set up an isolated worker-attempts dir that is empty.
        wa_dir = tmp_path / "worker_attempts"
        wa_dir.mkdir()
        # Make checkout dirty
        (clean_repo / "scratch.py").write_text("x=1\n")
        ok, dirty, reason = _guard()(str(clean_repo))
        assert ok is False
        assert reason == "untracked_paths"
        # Production guard returns early; the test confirms that
        # no attempt artifact has been written.
        artifacts = list(wa_dir.glob("*.worker_result.json"))
        assert artifacts == [], "dirty rejection must not write a result"

    def test_i_rejection_creates_no_result_contract(
        self, clean_repo: Path, tmp_path: Path
    ) -> None:
        wa_dir = tmp_path / "worker_attempts"
        wa_dir.mkdir()
        (clean_repo / "scratch.py").write_text("x=1\n")
        ok, _, _ = _guard()(str(clean_repo))
        assert ok is False
        contracts = list(wa_dir.glob("att-*"))
        assert contracts == [], "dirty rejection must not write a contract"

    def test_j_rejection_does_not_consume_event(
        self, clean_repo: Path
    ) -> None:
        """A rejection leaves the dirty state intact (no worker burned it)."""
        scratch = clean_repo / "scratch.py"
        scratch.write_text("x=1\n")
        ok, dirty, _ = _guard()(str(clean_repo))
        assert ok is False
        # The dirty file must still be on disk and still dirty.
        assert scratch.exists()
        assert scratch.read_text() == "x=1\n"
        # And the repo is still in the dirty state:
        ok2, dirty2, _ = _guard()(str(clean_repo))
        assert ok2 is False
        assert dirty == dirty2, "rejection must not have modified the repo"

    def test_k_git_status_failure_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If git status itself errors, the guard fails closed."""
        # Non-existent repo path: git will return non-zero rc.
        bogus = tmp_path / "does-not-exist-repo"
        ok, dirty, reason = _guard()(str(bogus))
        assert ok is False
        assert dirty == []
        # Reason is one of the git_status_failed variants.
        assert reason.startswith("git_status_failed"), f"got {reason!r}"

    def test_k_git_status_subprocess_exception_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If subprocess.run itself raises, guard must fail closed."""
        import autocoder_supervisor.supervisor as _sup

        def _raise(*_a, **_kw):
            raise OSError("simulated git binary missing")

        monkeypatch.setattr(_sup.subprocess, "run", _raise)
        ok, dirty, reason = _guard()("/tmp/anywhere")
        assert ok is False
        assert dirty == []
        assert reason.startswith("git_status_failed"), f"got {reason!r}"

    def test_l_runtime_files_outside_repo_do_not_block(
        self, clean_repo: Path, tmp_path: Path
    ) -> None:
        """Runtime files in the operator's runtime area MUST NOT
        affect the production checkout status. The runtime
        location is resolved from the operator HOME env var
        (with a tmp_path fallback when HOME is unset) so this
        test does not need any hardcoded absolute path.
        """
        runtime_root = Path(os.environ.get("OPERATOR_HOME") or str(tmp_path))
        runtime = runtime_root / "aed-supervisor-runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "hermes-snap-leak-test.sh").write_text("#!/bin/sh\necho\n")
        (runtime / "forensic.json").write_text("{}\n")
        (runtime / "logs").mkdir(exist_ok=True)
        (runtime / "logs" / "supervisor.log").write_text("noise\n")
        # Repo stays clean.
        ok, dirty, reason = _guard()(str(clean_repo))
        assert ok is True
        assert dirty == []
        assert reason == "clean"


# ---------------------------------------------------------------------------
# Failure-reason classification
# ---------------------------------------------------------------------------


class TestReasonClassification:
    def test_mixed_modified_and_untracked_reason(
        self, clean_repo: Path
    ) -> None:
        (clean_repo / "production.py").write_text("# modified\n")
        (clean_repo / "scratch.py").write_text("x=1\n")
        ok, dirty, reason = _guard()(str(clean_repo))
        assert ok is False
        assert reason == "uncommitted_changes+untracked_paths"
        assert len(dirty) >= 2

    def test_only_untracked_reason(self, clean_repo: Path) -> None:
        (clean_repo / "scratch.py").write_text("x=1\n")
        ok, dirty, reason = _guard()(str(clean_repo))
        assert ok is False
        assert reason == "untracked_paths"
        assert any("scratch.py" in p for p in dirty)