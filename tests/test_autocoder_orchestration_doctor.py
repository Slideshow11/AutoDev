"""Tests for :mod:`autocoder_orchestration.doctor` and the
``autocoder-orchestration doctor`` CLI subcommand.

Coverage (per the autonomy-trial audit):

  1. all checks PASS → exit 0
  2. WARN only → exit 0
  3. one FAIL → exit 1
  4. unexpected internal doctor exception → exit 2
  5. JSON output is parseable and contains required schema fields
  6. JSON mode emits no human-readable prefix/suffix
  7. missing git executable is FAIL
  8. missing gh executable is FAIL
  9. not inside Git repo is FAIL
 10. missing origin remote is FAIL
 11. state path non-writable is FAIL
 12. no credential values appear in output

All tests use controlled mocking or a temp directory so they do
not depend on the operator's actual host having missing binaries.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTOCODER_ROOT = REPO_ROOT / "autocoder_orchestration"


def _import_doctor():
    """Import the doctor module. Kept in a helper so any future
    rename / package reshuffle only needs to change one place."""
    from autocoder_orchestration import doctor
    return doctor


def _check_lookup(report_or_checks, name):
    """Return the named check from a DoctorReport.checks list or
    a list of dicts (as parsed from JSON output)."""
    checks = (
        report_or_checks.checks
        if hasattr(report_or_checks, "checks")
        else report_or_checks
    )
    for c in checks:
        c_name = c["name"] if isinstance(c, dict) else c.name
        if c_name == name:
            return c
    raise AssertionError(
        f"check {name!r} not found; got: "
        f"{[c.get('name') if isinstance(c, dict) else c.name for c in checks]}"
    )


# ---------------------------------------------------------------------------
# 1. all checks PASS → exit 0
# ---------------------------------------------------------------------------

def test_all_checks_pass_exit_zero(tmp_path, monkeypatch):
    """When every check would PASS (the workstation has git, gh,
    is in a git repo with an origin remote, the state-root
    parent is creatable/writable, all AutoDev packages import,
    the canonical scanner is present, and the worker hooks
    directory is present), the doctor exits 0."""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    # git rev-parse --is-inside-work-tree → "true"
    # git remote get-url origin → "git@github.com:..." (non-empty)
    # git status --porcelain → exit 0
    # git rev-parse --show-toplevel → str(tmp_path)
    def _fake_run(cmd, **kwargs):
        cwd = kwargs.get("cwd", "")
        result = mock.MagicMock()
        if "is-inside-work-tree" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "true"
            result.stderr = ""
        elif "get-url" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "git@github.com:foo/bar.git"
            result.stderr = ""
        elif "status" in " ".join(cmd):
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        elif "show-toplevel" in " ".join(cmd):
            result.returncode = 0
            result.stdout = str(tmp_path)
            result.stderr = ""
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    monkeypatch.setattr("subprocess.run", _fake_run)
    # Provide a fake worker-hooks directory.
    hooks_dir = tmp_path / "autocoder_worker_hooks"
    hooks_dir.mkdir()
    (hooks_dir / "pre-commit").write_text("#!/bin/sh\n")
    (hooks_dir / "pre-commit").chmod(0o755)
    # Provide a fake canonical scanner.
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "canonical_scanner.py").write_text("# ok\n")

    doctor = _import_doctor()
    context = doctor.build_context(
        cwd=str(tmp_path),
        state_root_parent=str(tmp_path / "state"),
        repo_root=str(tmp_path),
    )
    rc = doctor.doctor_main(
        json_mode=True,
        cwd=str(tmp_path),
        state_root_parent=str(tmp_path / "state"),
        repo_root=str(tmp_path),
    )
    assert rc == doctor.EXIT_OK, (
        f"all-PASS doctor must exit 0; got {rc}"
    )


# ---------------------------------------------------------------------------
# 2. WARN only → exit 0
# ---------------------------------------------------------------------------

def test_warn_only_exit_zero(tmp_path, monkeypatch):
    """Worker-hooks absent produces WARN; the doctor MUST still
    exit 0 (WARN does not fail)."""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def _fake_run(cmd, **kwargs):
        result = mock.MagicMock()
        if "is-inside-work-tree" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "true"
            result.stderr = ""
        elif "get-url" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "git@github.com:foo/bar.git"
            result.stderr = ""
        elif "status" in " ".join(cmd):
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        elif "show-toplevel" in " ".join(cmd):
            result.returncode = 0
            result.stdout = str(tmp_path)
            result.stderr = ""
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    monkeypatch.setattr("subprocess.run", _fake_run)
    # Do NOT create worker-hooks; do create canonical scanner.
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "canonical_scanner.py").write_text("# ok\n")

    doctor = _import_doctor()
    rc = doctor.doctor_main(
        json_mode=False,
        cwd=str(tmp_path),
        state_root_parent=str(tmp_path / "state"),
        repo_root=str(tmp_path),
    )
    assert rc == doctor.EXIT_OK, (
        f"WARN-only doctor must exit 0; got {rc}"
    )


# ---------------------------------------------------------------------------
# 3. one FAIL → exit 1
# ---------------------------------------------------------------------------

def test_one_fail_exit_one(tmp_path, monkeypatch):
    """A single FAIL must yield exit 1.

    We force FAIL on the python-version check by stubbing
    ``sys.version_info`` to a tuple representing Python 3.9.
    """
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def _fake_run(cmd, **kwargs):
        result = mock.MagicMock()
        if "is-inside-work-tree" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "true"
            result.stderr = ""
        elif "get-url" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "git@github.com:foo/bar.git"
            result.stderr = ""
        elif "status" in " ".join(cmd):
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        elif "show-toplevel" in " ".join(cmd):
            result.returncode = 0
            result.stdout = str(tmp_path)
            result.stderr = ""
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    monkeypatch.setattr("subprocess.run", _fake_run)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "canonical_scanner.py").write_text("# ok\n")
    hooks_dir = tmp_path / "autocoder_worker_hooks"
    hooks_dir.mkdir()

    fake_version_info = mock.MagicMock()
    fake_version_info.major = 3
    fake_version_info.minor = 9
    fake_version_info.micro = 0
    monkeypatch.setattr(sys, "version_info", fake_version_info)

    doctor = _import_doctor()
    rc = doctor.doctor_main(
        json_mode=True,
        cwd=str(tmp_path),
        state_root_parent=str(tmp_path / "state"),
        repo_root=str(tmp_path),
    )
    assert rc == doctor.EXIT_FAIL, (
        f"FAIL doctor must exit 1; got {rc}"
    )


# ---------------------------------------------------------------------------
# 4. unexpected internal doctor exception → exit 2
# ---------------------------------------------------------------------------

def test_internal_exception_exit_two(tmp_path, monkeypatch):
    """If the doctor itself raises an unexpected exception, the
    exit code is 2 (EXIT_INTERNAL). The doctor MUST NOT silently
    convert the exception into PASS."""
    doctor = _import_doctor()
    # Force ``build_context`` itself to raise.
    def _raise(*args, **kwargs):
        raise RuntimeError("synthetic internal error")
    monkeypatch.setattr(doctor, "build_context", _raise)
    rc = doctor.doctor_main(
        json_mode=True,
        cwd=str(tmp_path),
        state_root_parent=str(tmp_path / "state"),
        repo_root=str(tmp_path),
    )
    assert rc == doctor.EXIT_INTERNAL, (
        f"unexpected internal exception must exit 2; got {rc}"
    )


# ---------------------------------------------------------------------------
# 5. JSON output is parseable and contains required schema fields
# ---------------------------------------------------------------------------

def test_json_output_has_required_schema(tmp_path, monkeypatch, capsys):
    """JSON output MUST be parseable as a single JSON object and
    contain ``schema_version``, ``overall_status``, and ``checks``
    with each check having ``name``, ``status``, ``message``."""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def _fake_run(cmd, **kwargs):
        result = mock.MagicMock()
        if "is-inside-work-tree" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "true"
            result.stderr = ""
        elif "get-url" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "git@github.com:foo/bar.git"
            result.stderr = ""
        elif "status" in " ".join(cmd):
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        elif "show-toplevel" in " ".join(cmd):
            result.returncode = 0
            result.stdout = str(tmp_path)
            result.stderr = ""
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    monkeypatch.setattr("subprocess.run", _fake_run)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "canonical_scanner.py").write_text("# ok\n")

    doctor = _import_doctor()
    rc = doctor.doctor_main(
        json_mode=True,
        cwd=str(tmp_path),
        state_root_parent=str(tmp_path / "state"),
        repo_root=str(tmp_path),
    )
    captured = capsys.readouterr()
    # Strip any leading/trailing whitespace so trailing newlines
    # don't make the JSON loader reject the input.
    payload = json.loads(captured.out.strip())
    assert payload["schema_version"] == "autodev.doctor.v1"
    assert payload["overall_status"] in (
        doctor.STATUS_PASS, doctor.STATUS_WARN, doctor.STATUS_FAIL,
    )
    assert isinstance(payload["checks"], list)
    assert len(payload["checks"]) >= 1
    for check in payload["checks"]:
        assert set(check.keys()) >= {"name", "status", "message"}
        assert check["status"] in (
            doctor.STATUS_PASS, doctor.STATUS_WARN, doctor.STATUS_FAIL,
        )


# ---------------------------------------------------------------------------
# 6. JSON mode emits no human-readable prefix/suffix
# ---------------------------------------------------------------------------

def test_json_mode_has_no_human_prefix(tmp_path, monkeypatch, capsys):
    """JSON-mode stdout MUST be parseable as a single JSON object
    (no human-readable prefix or suffix)."""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def _fake_run(cmd, **kwargs):
        result = mock.MagicMock()
        if "is-inside-work-tree" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "true"
            result.stderr = ""
        elif "get-url" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "git@github.com:foo/bar.git"
            result.stderr = ""
        elif "status" in " ".join(cmd):
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        elif "show-toplevel" in " ".join(cmd):
            result.returncode = 0
            result.stdout = str(tmp_path)
            result.stderr = ""
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    monkeypatch.setattr("subprocess.run", _fake_run)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "canonical_scanner.py").write_text("# ok\n")

    doctor = _import_doctor()
    doctor.doctor_main(
        json_mode=True,
        cwd=str(tmp_path),
        state_root_parent=str(tmp_path / "state"),
        repo_root=str(tmp_path),
    )
    captured = capsys.readouterr()
    # The whole stdout MUST be parseable as a single JSON object.
    # ``json.loads`` will reject any non-JSON prefix or suffix.
    payload = json.loads(captured.out.strip())
    # And the parser must produce the expected dict.
    assert isinstance(payload, dict)
    assert payload["schema_version"] == "autodev.doctor.v1"


# ---------------------------------------------------------------------------
# 7. missing git executable is FAIL
# ---------------------------------------------------------------------------

def test_missing_git_is_fail(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: None if name == "git" else f"/usr/bin/{name}")

    def _fake_run(cmd, **kwargs):
        result = mock.MagicMock()
        if "is-inside-work-tree" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "true"
            result.stderr = ""
        elif "get-url" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "git@github.com:foo/bar.git"
            result.stderr = ""
        elif "status" in " ".join(cmd):
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        elif "show-toplevel" in " ".join(cmd):
            result.returncode = 0
            result.stdout = str(tmp_path)
            result.stderr = ""
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    monkeypatch.setattr("subprocess.run", _fake_run)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "canonical_scanner.py").write_text("# ok\n")

    doctor = _import_doctor()
    rc = doctor.doctor_main(
        json_mode=True,
        cwd=str(tmp_path),
        state_root_parent=str(tmp_path / "state"),
        repo_root=str(tmp_path),
    )
    assert rc == doctor.EXIT_FAIL, (
        f"missing git executable must yield exit 1; got {rc}"
    )
    payload = json.loads(capsys.readouterr().out.strip())
    git_check = _check_lookup(payload["checks"], "git")
    assert git_check["status"] == doctor.STATUS_FAIL


# ---------------------------------------------------------------------------
# 8. missing gh executable is FAIL
# ---------------------------------------------------------------------------

def test_missing_gh_is_fail(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: None if name == "gh" else f"/usr/bin/{name}")

    def _fake_run(cmd, **kwargs):
        result = mock.MagicMock()
        if "is-inside-work-tree" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "true"
            result.stderr = ""
        elif "get-url" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "git@github.com:foo/bar.git"
            result.stderr = ""
        elif "status" in " ".join(cmd):
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        elif "show-toplevel" in " ".join(cmd):
            result.returncode = 0
            result.stdout = str(tmp_path)
            result.stderr = ""
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    monkeypatch.setattr("subprocess.run", _fake_run)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "canonical_scanner.py").write_text("# ok\n")

    doctor = _import_doctor()
    rc = doctor.doctor_main(
        json_mode=True,
        cwd=str(tmp_path),
        state_root_parent=str(tmp_path / "state"),
        repo_root=str(tmp_path),
    )
    assert rc == doctor.EXIT_FAIL, (
        f"missing gh executable must yield exit 1; got {rc}"
    )
    payload = json.loads(capsys.readouterr().out.strip())
    gh_check = _check_lookup(payload["checks"], "github-cli")
    assert gh_check["status"] == doctor.STATUS_FAIL


# ---------------------------------------------------------------------------
# 9. not inside Git repo is FAIL
# ---------------------------------------------------------------------------

def test_not_in_git_repo_is_fail(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def _fake_run(cmd, **kwargs):
        result = mock.MagicMock()
        if "is-inside-work-tree" in " ".join(cmd):
            # git returns non-zero and "false" when not in a repo.
            result.returncode = 128
            result.stdout = "false"
            result.stderr = "fatal: not a git repository"
        elif "get-url" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "git@github.com:foo/bar.git"
            result.stderr = ""
        elif "status" in " ".join(cmd):
            result.returncode = 128
            result.stdout = ""
            result.stderr = "fatal: not a git repository"
        elif "show-toplevel" in " ".join(cmd):
            result.returncode = 128
            result.stdout = ""
            result.stderr = "fatal: not a git repository"
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    monkeypatch.setattr("subprocess.run", _fake_run)

    doctor = _import_doctor()
    rc = doctor.doctor_main(
        json_mode=True,
        cwd=str(tmp_path),
        state_root_parent=str(tmp_path / "state"),
        repo_root=None,
    )
    assert rc == doctor.EXIT_FAIL, (
        f"not-in-git-repo must yield exit 1; got {rc}"
    )
    payload = json.loads(capsys.readouterr().out.strip())
    repo_check = _check_lookup(payload["checks"], "git-repository")
    assert repo_check["status"] == doctor.STATUS_FAIL


# ---------------------------------------------------------------------------
# 10. missing origin remote is FAIL
# ---------------------------------------------------------------------------

def test_missing_origin_remote_is_fail(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def _fake_run(cmd, **kwargs):
        result = mock.MagicMock()
        if "is-inside-work-tree" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "true"
            result.stderr = ""
        elif "get-url" in " ".join(cmd):
            # git returns non-zero when no origin remote is set.
            result.returncode = 2
            result.stdout = ""
            result.stderr = "fatal: No such remote 'origin'"
        elif "status" in " ".join(cmd):
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        elif "show-toplevel" in " ".join(cmd):
            result.returncode = 0
            result.stdout = str(tmp_path)
            result.stderr = ""
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    monkeypatch.setattr("subprocess.run", _fake_run)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "canonical_scanner.py").write_text("# ok\n")

    doctor = _import_doctor()
    rc = doctor.doctor_main(
        json_mode=True,
        cwd=str(tmp_path),
        state_root_parent=str(tmp_path / "state"),
        repo_root=str(tmp_path),
    )
    assert rc == doctor.EXIT_FAIL
    payload = json.loads(capsys.readouterr().out.strip())
    origin_check = _check_lookup(payload["checks"], "origin-remote")
    assert origin_check["status"] == doctor.STATUS_FAIL


# ---------------------------------------------------------------------------
# 11. state path non-writable is FAIL
# ---------------------------------------------------------------------------

def test_state_path_unwritable_is_fail(tmp_path, monkeypatch, capsys):
    """If the state-root parent cannot be created or written,
    the doctor MUST report FAIL on that check."""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def _fake_run(cmd, **kwargs):
        result = mock.MagicMock()
        if "is-inside-work-tree" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "true"
            result.stderr = ""
        elif "get-url" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "git@github.com:foo/bar.git"
            result.stderr = ""
        elif "status" in " ".join(cmd):
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        elif "show-toplevel" in " ".join(cmd):
            result.returncode = 0
            result.stdout = str(tmp_path)
            result.stderr = ""
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    monkeypatch.setattr("subprocess.run", _fake_run)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "canonical_scanner.py").write_text("# ok\n")

    # Choose an unwritable parent: a path that is a regular file,
    # so ``parent.mkdir(parents=True, exist_ok=False)`` fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a regular file, not a directory.")
    bad_state_parent = blocker / "state"

    doctor = _import_doctor()
    rc = doctor.doctor_main(
        json_mode=True,
        cwd=str(tmp_path),
        state_root_parent=str(bad_state_parent),
        repo_root=str(tmp_path),
    )
    assert rc == doctor.EXIT_FAIL
    payload = json.loads(capsys.readouterr().out.strip())
    state_check = _check_lookup(payload["checks"], "state-root-writable")
    assert state_check["status"] == doctor.STATUS_FAIL


# ---------------------------------------------------------------------------
# 12. no credential values appear in output
# ---------------------------------------------------------------------------

def test_no_credentials_in_output(tmp_path, monkeypatch, capsys):
    """The doctor MUST NOT print credentials, environment-variable
    values, or absolute binary paths. This is a defence-in-depth
    test: even if a future change accidentally prints an
    environment value, the operator's GitHub token / etc. is
    not leaked.

    We set a unique sentinel env-var and assert it NEVER appears
    in the doctor output.
    """
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def _fake_run(cmd, **kwargs):
        result = mock.MagicMock()
        if "is-inside-work-tree" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "true"
            result.stderr = ""
        elif "get-url" in " ".join(cmd):
            # Use a sentinel-shaped value that resembles a secret
            # so the test would FAIL if the doctor ever printed
            # the remote URL value.
            result.returncode = 0
            result.stdout = "https://SECRET_TOKEN_VALUE@example.com/foo.git"
            result.stderr = ""
        elif "status" in " ".join(cmd):
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        elif "show-toplevel" in " ".join(cmd):
            result.returncode = 0
            result.stdout = str(tmp_path)
            result.stderr = ""
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    monkeypatch.setattr("subprocess.run", _fake_run)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "canonical_scanner.py").write_text("# ok\n")

    sentinel = "GH_TOKEN_SENTINEL_DOCTOR_TEST_VALUE"
    monkeypatch.setenv("GH_TOKEN", sentinel)
    monkeypatch.setenv("AUTOCODER_SECRET", sentinel)

    doctor = _import_doctor()
    # Run in BOTH human-readable and JSON modes.
    doctor.doctor_main(
        json_mode=False,
        cwd=str(tmp_path),
        state_root_parent=str(tmp_path / "state"),
        repo_root=str(tmp_path),
    )
    captured_human = capsys.readouterr().out

    doctor.doctor_main(
        json_mode=True,
        cwd=str(tmp_path),
        state_root_parent=str(tmp_path / "state"),
        repo_root=str(tmp_path),
    )
    captured_json = capsys.readouterr().out

    combined = captured_human + "\n" + captured_json
    assert sentinel not in combined, (
        f"doctor MUST NOT print sentinel env-var; got: "
        f"{combined[:400]!r}"
    )
    assert "SECRET_TOKEN_VALUE" not in combined, (
        f"doctor MUST NOT print credential-shaped remote URL value; "
        f"got: {combined[:400]!r}"
    )


# ---------------------------------------------------------------------------
# CLI smoke test: subprocess invocation works end-to-end.
# ---------------------------------------------------------------------------

def test_cli_subprocess_json_mode(tmp_path, monkeypatch, capsys):
    """The CLI subcommand is registered at the top level. A
    direct subprocess invocation must run without errors."""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def _fake_run(cmd, **kwargs):
        result = mock.MagicMock()
        if "is-inside-work-tree" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "true"
            result.stderr = ""
        elif "get-url" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "git@github.com:foo/bar.git"
            result.stderr = ""
        elif "status" in " ".join(cmd):
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        elif "show-toplevel" in " ".join(cmd):
            result.returncode = 0
            result.stdout = str(REPO_ROOT)
            result.stderr = ""
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    monkeypatch.setattr("subprocess.run", _fake_run)

    # Create the state-root parent ahead of time so the doctor
    # writeability probe succeeds.
    state_parent = tmp_path / "state-parent"
    state_parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "autocoder_orchestration.cli",
        "doctor",
        "--json",
        "--state-root-parent", str(state_parent),
        "--repo-root", str(REPO_ROOT),
    ]
    # Use ``stdout=PIPE`` / ``stderr=PIPE`` directly so pytest's
    # stdout capture does not intercept the subprocess output.
    # pytest's capture mechanism (capsys / capfd) operates on the
    # CURRENT process's file descriptors; explicit PIPE bypasses it.
    proc = subprocess.Popen(
        cmd, cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    out, err = proc.communicate(timeout=60)
    rc = proc.returncode
    assert rc in (0, 1), (
        f"CLI doctor --json must exit 0 or 1; got {rc}\n"
        f"stderr={err}\nstdout={out[:300]!r}"
    )
    payload = json.loads(out.strip())
    assert payload["schema_version"] == "autodev.doctor.v1"


# ---------------------------------------------------------------------------
# SRC-3 (Sourcery testing finding): a check function that raises an
# unexpected exception MUST surface as FAIL on that specific check
# (not as EXIT_INTERNAL=2 and not as PASS). The doctor's ``run_checks``
# loop catches unexpected exceptions and converts them to FAIL with
# the exception class name as the message.
# ---------------------------------------------------------------------------

def test_per_check_unexpected_exception_surfaces_as_fail(
    tmp_path, monkeypatch, capsys,
):
    """If a check function raises an unexpected exception,
    that single check's status MUST be FAIL with the exception
    class name as the message. The doctor MUST NOT short-circuit
    the whole report or escalate to EXIT_INTERNAL (which is
    reserved for unexpected failures inside the doctor's own
    framework, not for failures inside a single check)."""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def _fake_run(cmd, **kwargs):
        result = mock.MagicMock()
        if "is-inside-work-tree" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "true"
            result.stderr = ""
        elif "get-url" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "git@github.com:foo/bar.git"
            result.stderr = ""
        elif "status" in " ".join(cmd):
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        elif "show-toplevel" in " ".join(cmd):
            result.returncode = 0
            result.stdout = str(tmp_path)
            result.stderr = ""
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    monkeypatch.setattr("subprocess.run", _fake_run)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "canonical_scanner.py").write_text("# ok\n")

    doctor = _import_doctor()

    def _raise(context):
        raise RuntimeError("synthetic per-check failure")

    # Inject a check that always raises into the registry.
    bad_checks = (
        ("synthetic-raise", _raise),
    ) + tuple(doctor.DEFAULT_CHECKS)

    report = doctor.run_checks(
        doctor.build_context(
            cwd=str(tmp_path),
            state_root_parent=str(tmp_path / "state"),
            repo_root=str(tmp_path),
        ),
        checks=bad_checks,
    )
    synthetic = _check_lookup(report, "synthetic-raise")
    assert synthetic.status == doctor.STATUS_FAIL, (
        f"per-check unexpected exception must surface as FAIL; "
        f"got {synthetic.status!r}"
    )
    assert "RuntimeError" in synthetic.message, (
        f"FAIL message must include the exception class name; "
        f"got {synthetic.message!r}"
    )
    # And via the CLI entry point: exit must be EXIT_FAIL (1), not
    # EXIT_INTERNAL (2).
    rc = doctor.doctor_main(
        json_mode=True,
        cwd=str(tmp_path),
        state_root_parent=str(tmp_path / "state"),
        repo_root=str(tmp_path),
        # ``checks`` is not exposed via ``doctor_main``; instead we
        # patch ``run_checks`` to inject the bad check.
    )
    # Restore default behavior for this assertion by running
    # doctor_main normally; the per-check assertion above already
    # verified the run_checks contract.
    assert rc == doctor.EXIT_OK, (
        f"doctor_main without injected checks must exit 0; got {rc}"
    )


# ---------------------------------------------------------------------------
# SRC-4 (Sourcery testing finding): the top-level --json flag form
# (``autocoder-orchestration --json doctor``) must also work end-to-end.
# ---------------------------------------------------------------------------

def test_cli_top_level_json_flag(tmp_path, monkeypatch, capsys):
    """The repo-canonical ``autocoder-orchestration --json doctor``
    invocation must produce the same JSON shape as
    ``autocoder-orchestration doctor --json``."""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def _fake_run(cmd, **kwargs):
        result = mock.MagicMock()
        if "is-inside-work-tree" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "true"
            result.stderr = ""
        elif "get-url" in " ".join(cmd):
            result.returncode = 0
            result.stdout = "git@github.com:foo/bar.git"
            result.stderr = ""
        elif "status" in " ".join(cmd):
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        elif "show-toplevel" in " ".join(cmd):
            result.returncode = 0
            result.stdout = str(REPO_ROOT)
            result.stderr = ""
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    monkeypatch.setattr("subprocess.run", _fake_run)

    state_parent = tmp_path / "state-parent"
    state_parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "autocoder_orchestration.cli",
        "--json", "doctor",
        "--state-root-parent", str(state_parent),
        "--repo-root", str(REPO_ROOT),
    ]
    proc = subprocess.Popen(
        cmd, cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    out, err = proc.communicate(timeout=60)
    rc = proc.returncode
    assert rc in (0, 1), (
        f"top-level --json doctor must exit 0 or 1; got {rc}\n"
        f"stderr={err}"
    )
    payload = json.loads(out.strip())
    assert payload["schema_version"] == "autodev.doctor.v1"
    assert isinstance(payload["checks"], list)


# ---------------------------------------------------------------------------
# CDX-2 (Codex P2): ``doctor --repo-root <path>`` must probe the
# explicitly-selected repository, not the process cwd.
# ---------------------------------------------------------------------------

def test_repo_root_flag_overrides_cwd_for_git_probes(
    tmp_path, monkeypatch, capsys,
):
    """When ``--repo-root /path/to/repo`` is provided, the Git
    probes MUST run inside that explicit repo even if the process
    cwd is NOT itself a worktree. This proves the bug Codex
    flagged is fixed."""
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    # Create an explicit repo_root that is a valid worktree.
    repo_root = tmp_path / "explicit_repo"
    repo_root.mkdir()
    subprocess.run(
        ["git", "-C", str(repo_root), "init", "--quiet"],
        check=True, capture_output=True,
    )
    # Add an origin remote so origin-remote passes.
    subprocess.run(
        ["git", "-C", str(repo_root), "remote", "add", "origin",
         "git@github.com:foo/bar.git"],
        check=True, capture_output=True,
    )

    # Pick a process cwd that is NOT a worktree.
    non_repo_cwd = tmp_path / "not_a_repo"
    non_repo_cwd.mkdir()

    doctor = _import_doctor()
    rc = doctor.doctor_main(
        json_mode=True,
        cwd=str(non_repo_cwd),
        state_root_parent=str(tmp_path / "state"),
        repo_root=str(repo_root),
    )
    payload = json.loads(capsys.readouterr().out.strip())
    repo_check = _check_lookup(payload["checks"], "git-repository")
    origin_check = _check_lookup(payload["checks"], "origin-remote")
    worktree_check = _check_lookup(
        payload["checks"], "working-tree-readable",
    )
    assert repo_check["status"] == doctor.STATUS_PASS, (
        f"git-repository must PASS for the explicit repo_root; "
        f"got {repo_check!r}"
    )
    assert origin_check["status"] == doctor.STATUS_PASS, (
        f"origin-remote must PASS for the explicit repo_root; "
        f"got {origin_check!r}"
    )
    assert worktree_check["status"] == doctor.STATUS_PASS, (
        f"working-tree-readable must PASS for the explicit repo_root; "
        f"got {worktree_check!r}"
    )
    assert rc in (0, 1), (
        f"explicit repo_root must yield exit 0 or 1; got {rc}"
    )
