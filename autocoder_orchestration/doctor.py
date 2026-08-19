"""Operational health check for an AutoDev installation.

The ``doctor`` subcommand runs a small, bounded, deterministic
checklist against the local environment and reports:

  PASS — check succeeded
  WARN — check succeeded with a caveat; review recommended
  FAIL — check failed; corrective action required

This module is **read-only** except for one tightly-scoped
writeability probe: it creates a single tiny file inside the
intended state-root parent and deletes it immediately. No
persistent artifacts are left behind.

The doctor does NOT:

  - mutate git branches, commits, remotes, or config;
  - push to any remote;
  - create PRs;
  - modify AutoDev controller state;
  - launch workers;
  - trigger any review;
  - print credentials or environment-variable values.

Exit contract (consumed by ``autocoder_orchestration.cli``):

  0 — all checks PASS or WARN
  1 — at least one check FAIL
  2 — doctor itself encountered an unexpected internal error
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Sequence


SCHEMA_VERSION = "autodev.doctor.v1"
STATUS_PASS = "PASS"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"

# Exit codes (consumed by the CLI's cmd_doctor wrapper).
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_INTERNAL = 2


@dataclasses.dataclass
class CheckResult:
    """One named health-check outcome."""

    name: str
    status: str  # STATUS_PASS / STATUS_WARN / STATUS_FAIL
    message: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
        }


@dataclasses.dataclass
class DoctorReport:
    """Aggregate doctor report (consumed by both human and JSON modes)."""

    schema_version: str
    overall_status: str
    checks: List[CheckResult]

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "overall_status": self.overall_status,
            "checks": [c.to_dict() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Individual checks
#
# Every check is a small pure function. Each takes a single ``context``
# dict so the function signatures are uniform and the checks are easy
# to unit-test in isolation.
# ---------------------------------------------------------------------------

def _check_python_version(context: dict) -> CheckResult:
    vi = sys.version_info
    current = f"{vi.major}.{vi.minor}.{vi.micro}"
    # AutoDev supervisor requires Python >= 3.10 (see pyproject.toml
    # ``requires-python``).
    if (vi.major, vi.minor) >= (3, 10):
        return CheckResult(
            "python-version",
            STATUS_PASS,
            f"Python {current} >= 3.10 supported by AutoDev",
        )
    return CheckResult(
        "python-version",
        STATUS_FAIL,
        f"Python {current} < 3.10; AutoDev requires >= 3.10",
    )


def _check_executable(context: dict, label: str, name: str, *, required: bool) -> CheckResult:
    """Internal helper: locate ``name`` on PATH.

    If ``required=True`` a missing executable is FAIL; otherwise it is
    WARN. The doctor never prints which PATH directories were
    searched and never prints the resolved absolute path of the
    binary (avoiding accidental credential / PII leakage when the
    operator has custom PATH entries).
    """
    found = shutil.which(name)
    if found:
        return CheckResult(
            label,
            STATUS_PASS,
            f"{name} executable available",
        )
    if required:
        return CheckResult(
            label,
            STATUS_FAIL,
            f"{name} executable not found on PATH",
        )
    return CheckResult(
        label,
        STATUS_WARN,
        f"{name} executable not found on PATH",
    )


def _check_git(context: dict) -> CheckResult:
    return _check_executable(context, "git", "git", required=True)


def _check_github_cli(context: dict) -> CheckResult:
    # ``gh`` is a review-provider dependency. It is required for the
    # canonical review/repair relay, but a workstation that runs
    # only the supervisor does not need it. FAIL when missing, per
    # the audit's explicit contract — the doctor surfaces the
    # requirement clearly so the operator can act before launching
    # an autonomous round.
    return _check_executable(context, "github-cli", "gh", required=True)


def _check_git_repository(context: dict) -> CheckResult:
    """Confirm ``context['git_cwd']`` is inside a git work tree.

    ``git_cwd`` is the directory the git probes run inside. When
    the operator passes ``--repo-root`` explicitly, ``git_cwd``
    is set to that explicit value; otherwise it falls back to
    the process cwd. This guarantees that ``doctor
    --repo-root /path/to/repo`` probes the explicitly-selected
    repository, not whatever directory the operator happened to
    be in when they ran the command.

    Addresses Codex P2 finding: previously the Git probes always
    used the process cwd even when ``--repo-root`` was set.
    """
    git_cwd = context["git_cwd"]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(git_cwd),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return CheckResult(
            "git-repository",
            STATUS_FAIL,
            f"git rev-parse failed: {type(exc).__name__}",
        )
    if out.returncode == 0 and out.stdout.strip() == "true":
        return CheckResult(
            "git-repository",
            STATUS_PASS,
            "current directory is inside a git work tree",
        )
    return CheckResult(
        "git-repository",
        STATUS_FAIL,
        "current directory is not inside a git work tree",
    )


def _check_origin_remote(context: dict) -> CheckResult:
    """Confirm the git repo at ``context['git_cwd']`` has a
    configured ``origin`` remote.

    A repository with no origin cannot receive pushes or trigger
    GitHub-based reviews. Read-only: does not mutate remotes.
    """
    git_cwd = context["git_cwd"]
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(git_cwd),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return CheckResult(
            "origin-remote",
            STATUS_FAIL,
            f"git remote get-url failed: {type(exc).__name__}",
        )
    if out.returncode == 0 and out.stdout.strip():
        return CheckResult(
            "origin-remote",
            STATUS_PASS,
            "origin remote configured",
        )
    return CheckResult(
        "origin-remote",
        STATUS_FAIL,
        "origin remote not configured or empty",
    )


def _check_working_tree_readable(context: dict) -> CheckResult:
    """Confirm ``git status`` can be read from
    ``context['git_cwd']``.

    Read-only: ``git status`` does not mutate repo state.
    """
    git_cwd = context["git_cwd"]
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(git_cwd),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return CheckResult(
            "working-tree-readable",
            STATUS_FAIL,
            f"git status failed: {type(exc).__name__}",
        )
    if out.returncode == 0:
        return CheckResult(
            "working-tree-readable",
            STATUS_PASS,
            "git status readable",
        )
    return CheckResult(
        "working-tree-readable",
        STATUS_FAIL,
        f"git status failed with code {out.returncode}",
    )


def _check_state_root_writable(context: dict) -> CheckResult:
    """Verify the intended state-root parent can be created, written,
    and read back.

    The probe is tightly scoped: it does NOT create the parent
    directory. Instead, it walks up to the nearest existing
    ancestor and performs a single-tiny-file write/read/delete
    probe inside that ancestor. No persistent directory or file
    artifact is left behind by the doctor.

    This addresses two related concerns:

      * Codex P2: ``parent.mkdir`` violated the read-only contract
        when the operator's intended state-root parent did not
        yet exist; the doctor would have created the whole
        ancestor tree as a side effect.
      * Sourcery bug-risk: a TOCTOU race between ``parent.exists()``
        and ``parent.mkdir(exist_ok=False)`` could spuriously
        fail the check on a concurrent creator. The new path
        eliminates the race entirely because the probe never
        creates directories.
    """
    parent = Path(context["state_root_parent"])
    # If the parent already exists and is not writable, FAIL fast
    # without attempting the temporary probe.
    if parent.exists():
        if not parent.is_dir():
            return CheckResult(
                "state-root-writable",
                STATUS_FAIL,
                f"state-root parent {str(parent)!r} exists but is not a directory",
            )
        if not os.access(str(parent), os.W_OK | os.X_OK):
            return CheckResult(
                "state-root-writable",
                STATUS_FAIL,
                f"state-root parent {str(parent)!r} is not writable",
            )
        probe_dir = parent
    else:
        # Read-only path: walk up to the nearest existing ancestor
        # and probe writability there. This satisfies the "doctor
        # must not leave persistent artifacts" contract.
        ancestor = parent
        while not ancestor.exists() and ancestor.parent != ancestor:
            ancestor = ancestor.parent
        if (
            not ancestor.exists()
            or not ancestor.is_dir()
            or not os.access(str(ancestor), os.W_OK | os.X_OK)
        ):
            return CheckResult(
                "state-root-writable",
                STATUS_FAIL,
                f"state-root parent {str(parent)!r} does not exist and "
                f"nearest existing ancestor {str(ancestor)!r} is not "
                f"writable",
            )
        probe_dir = ancestor
    # Tightly-scoped writeability probe: create one tiny file inside
    # ``probe_dir``, confirm we can read it back, then delete it.
    # Best-effort cleanup; leftover autocoder-doctor-* files in
    # ``probe_dir`` can be removed by the operator manually.
    try:
        fd, name = tempfile.mkstemp(
            prefix="autocoder-doctor-", dir=str(probe_dir),
        )
    except OSError as exc:
        return CheckResult(
            "state-root-writable",
            STATUS_FAIL,
            f"cannot create probe file in {str(probe_dir)!r}: "
            f"{type(exc).__name__}",
        )
    tmp_path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("autocoder-doctor-probe\n")
            fh.flush()
        # Confirm we can read it back.
        if not tmp_path.is_file():
            return CheckResult(
                "state-root-writable",
                STATUS_FAIL,
                f"probe file disappeared immediately after write in "
                f"{str(probe_dir)!r}",
            )
        data = tmp_path.read_text(encoding="utf-8")
        if data != "autocoder-doctor-probe\n":
            return CheckResult(
                "state-root-writable",
                STATUS_FAIL,
                f"probe file readback mismatch in {str(probe_dir)!r}",
            )
        if parent == probe_dir:
            return CheckResult(
                "state-root-writable",
                STATUS_PASS,
                f"state-root parent {str(parent)!r} is creatable, "
                f"writable, and readable",
            )
        return CheckResult(
            "state-root-writable",
            STATUS_PASS,
            f"state-root parent {str(parent)!r} does not exist; "
            f"nearest existing ancestor {str(probe_dir)!r} is "
            f"writable and readable",
        )
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            # Best-effort cleanup; the operator can remove any
            # leftover autocoder-doctor-* file in the probe
            # directory manually if cleanup failed.
            pass


def _check_autocoder_import(context: dict) -> CheckResult:
    """Confirm the autocoder_supervisor and autocoder_orchestration
    packages can be imported.

    Read-only: importing does not mutate repo state.
    """
    missing = []
    for module_name in (
        "autocoder_supervisor",
        "autocoder_orchestration",
        "autocoder_lifecycle",
    ):
        try:
            __import__(module_name)
        except ImportError:
            missing.append(module_name)
    if not missing:
        return CheckResult(
            "autocoder-import",
            STATUS_PASS,
            "autocoder_supervisor, autocoder_orchestration, "
            "autocoder_lifecycle import cleanly",
        )
    return CheckResult(
        "autocoder-import",
        STATUS_FAIL,
        f"missing or unimportable packages: {', '.join(missing)}",
    )


def _check_canonical_scanner(context: dict) -> CheckResult:
    """Confirm the canonical scanner entry point is importable or
    executable.

    The probe anchors the file check to ``context['repo_root']``
    when ``repo_root`` is set, so an operator who passed
    ``--repo-root /path/to/repo`` is probed against that explicit
    repo, not the process cwd. The ``cwd``-relative fallback is
    only used when ``repo_root`` is genuinely unknown.

    Read-only: importing the script does not mutate repo state.
    """
    repo_root = context.get("repo_root")
    candidates = []
    if repo_root is not None:
        candidates.append(Path(repo_root) / "scripts" / "canonical_scanner.py")
    else:
        # Fallback: when repo_root is unknown, use cwd-relative
        # scripts path. Anchored to git_cwd (not the bare process
        # cwd) for symmetry with the Git probes.
        git_cwd = context.get("git_cwd")
        anchor = git_cwd if git_cwd is not None else context["cwd"]
        candidates.append(Path(anchor) / "scripts" / "canonical_scanner.py")
    for cand in candidates:
        if cand.is_file():
            return CheckResult(
                "canonical-scanner",
                STATUS_PASS,
                f"canonical scanner script present at {cand}",
            )
    # Fallback: try to import it as a module.
    try:
        __import__("scripts.canonical_scanner")
        return CheckResult(
            "canonical-scanner",
            STATUS_PASS,
            "scripts.canonical_scanner importable",
        )
    except ImportError:
        return CheckResult(
            "canonical-scanner",
            STATUS_WARN,
            "canonical scanner script not found at scripts/canonical_scanner.py "
            "and scripts.canonical_scanner is not importable",
        )


def _check_worker_hooks(context: dict) -> CheckResult:
    """Confirm the worker hook directory exists when configured/expected.

    The worker hooks directory is repo-specific and may live at
    different paths depending on the operator's installation layout.
    The doctor checks for the canonical layout:
    ``<repo_root>/autocoder_worker_hooks``.

    This is WARN when absent (not FAIL) because the operator may
    be running a workstation that does not execute workers, only
    reviews them.
    """
    repo_root = context.get("repo_root")
    if repo_root is None:
        return CheckResult(
            "worker-hooks",
            STATUS_WARN,
            "repo_root not provided; skipping worker-hooks probe",
        )
    hooks_dir = Path(repo_root) / "autocoder_worker_hooks"
    if hooks_dir.is_dir():
        # Count executable hook scripts.
        executable_count = 0
        for entry in hooks_dir.iterdir():
            if entry.is_file() and os.access(str(entry), os.X_OK):
                executable_count += 1
        return CheckResult(
            "worker-hooks",
            STATUS_PASS,
            f"worker hook directory present with "
            f"{executable_count} executable hook script(s)",
        )
    return CheckResult(
        "worker-hooks",
        STATUS_WARN,
        f"worker hook directory not found at {hooks_dir}",
    )


# ---------------------------------------------------------------------------
# Aggregate runner
# ---------------------------------------------------------------------------

# Default check registry. The list order is the display order in
# human-readable mode and the JSON ``checks`` array order. Each entry
# is ``(name, callable)``.
DEFAULT_CHECKS: Sequence[tuple] = (
    ("python-version", _check_python_version),
    ("git", _check_git),
    ("github-cli", _check_github_cli),
    ("git-repository", _check_git_repository),
    ("origin-remote", _check_origin_remote),
    ("working-tree-readable", _check_working_tree_readable),
    ("state-root-writable", _check_state_root_writable),
    ("autocoder-import", _check_autocoder_import),
    ("canonical-scanner", _check_canonical_scanner),
    ("worker-hooks", _check_worker_hooks),
)


def run_checks(
    context: dict,
    *,
    checks: Optional[Sequence[tuple]] = None,
) -> DoctorReport:
    """Run every registered check and return a ``DoctorReport``.

    ``context`` MUST contain:

      - ``cwd`` (str): current working directory for git probes
      - ``state_root_parent`` (str): intended state-root parent directory

    ``context`` MAY contain:

      - ``repo_root`` (str): repository root for canonical-scanner
        and worker-hooks probes.

    An unexpected internal error inside one check does NOT
    short-circuit the whole report — the failing check is reported
    as FAIL with the error class name as the message and the
    doctor continues. This matches the audit's "do not silently
    convert exceptions into PASS" requirement.
    """
    selected = checks if checks is not None else DEFAULT_CHECKS
    results: List[CheckResult] = []
    overall = STATUS_PASS
    for name, fn in selected:
        try:
            result = fn(context)
        except Exception as exc:  # noqa: BLE001
            # Surface unexpected internal errors as FAIL on that
            # specific check (not silent PASS). Do not include the
            # exception message in the doctor output to avoid
            # leaking internal state.
            result = CheckResult(
                name,
                STATUS_FAIL,
                f"check raised {type(exc).__name__}",
            )
        if result.status not in (STATUS_PASS, STATUS_WARN, STATUS_FAIL):
            # Defensive: if a check returns an unexpected status,
            # surface it as FAIL rather than silently accepting it.
            result = CheckResult(
                name,
                STATUS_FAIL,
                f"check returned unrecognised status {result.status!r}",
            )
        if result.status == STATUS_FAIL:
            overall = STATUS_FAIL
        elif result.status == STATUS_WARN and overall == STATUS_PASS:
            overall = STATUS_WARN
        results.append(result)
    return DoctorReport(
        schema_version=SCHEMA_VERSION,
        overall_status=overall,
        checks=results,
    )


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------

def render_human(report: DoctorReport) -> str:
    """Render the report as a stable, deterministic, human-readable
    string.

    No timestamps. No environment variables. No credentials. Each
    check is rendered as a single ``STATUS  name  message`` line.
    """
    lines = ["AutoDev Doctor", ""]
    for check in report.checks:
        lines.append(f"{check.status:<4}  {check.name:<24}  {check.message}")
    lines.append("")
    lines.append(f"Overall: {report.overall_status}")
    return "\n".join(lines)


def render_json(report: DoctorReport) -> str:
    """Render the report as a JSON string with stable key ordering.

    The JSON is the ONLY mode that must be machine-parseable.
    No human-readable prefix or suffix is added.
    """
    # ``sort_keys`` is not relevant here because we use an explicit
    # key order in ``DoctorReport.to_dict``. ``ensure_ascii=False``
    # keeps any non-ASCII messages readable; ``separators`` is the
    # compact default.
    return json.dumps(
        report.to_dict(),
        indent=2,
        ensure_ascii=False,
        sort_keys=False,
    )


def _autodetect_repo_root(cwd: str) -> Optional[str]:
    """Return the absolute path of the git toplevel for ``cwd``.

    Returns ``None`` if ``cwd`` is not inside a git work tree or
    the git invocation fails for any reason. Used as a default
    when the operator did not pass ``--repo-root`` explicitly.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def build_context(
    *,
    cwd: Optional[str] = None,
    state_root_parent: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> dict:
    """Build the context dict passed to every check.

    This is exposed at module scope so tests can construct a
    deterministic context without monkeypatching ``os.getcwd``.

    When ``repo_root`` is None, the doctor attempts to auto-detect
    the git toplevel of ``cwd`` so the canonical-scanner and
    worker-hooks probes can run without an explicit override.

    ``git_cwd`` is the directory the Git probes run inside. When
    ``repo_root`` is provided (explicitly or via auto-detect),
    ``git_cwd`` is set to ``repo_root`` so ``doctor --repo-root
    /path/to/repo`` always probes the explicitly-selected
    repository, not the operator's process cwd.
    """
    resolved_cwd = cwd if cwd is not None else os.getcwd()
    if repo_root is None:
        repo_root = _autodetect_repo_root(resolved_cwd)
    # Git probes run in the explicitly-selected repository root
    # when one was provided, or in the auto-detected repository
    # toplevel, or in the process cwd as a last resort. This
    # avoids the bug where ``doctor --repo-root /path/to/repo``
    # ran the probes against the operator's cwd instead of the
    # selected repo.
    git_cwd = repo_root if repo_root is not None else resolved_cwd
    return {
        "cwd": resolved_cwd,
        "git_cwd": git_cwd,
        "state_root_parent": (
            state_root_parent
            if state_root_parent is not None
            else "/var/tmp/autodev-evidence/state"
        ),
        "repo_root": repo_root,
    }


def doctor_main(
    *,
    json_mode: bool,
    cwd: Optional[str] = None,
    state_root_parent: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> int:
    """Top-level entry point used by ``autocoder_orchestration.cli``.

    Returns the exit code; the CLI wrapper writes the rendered
    output to stdout.
    """
    try:
        context = build_context(
            cwd=cwd,
            state_root_parent=state_root_parent,
            repo_root=repo_root,
        )
        report = run_checks(context)
        rendered = render_json(report) if json_mode else render_human(report)
        sys.stdout.write(rendered + "\n")
    except Exception:  # noqa: BLE001
        # Internal doctor error. Per the audit's exit-code
        # contract: ``2`` is reserved for unexpected doctor
        # internals. We do NOT silently fall through to EXIT_OK.
        # We do NOT print a traceback to stdout (would corrupt
        # JSON mode). We write a single minimal JSON line to
        # stderr when JSON mode is active, and ``Internal error``
        # to stderr otherwise.
        if json_mode:
            sys.stderr.write(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "overall_status": STATUS_FAIL,
                        "error": "internal_error",
                    }
                )
                + "\n"
            )
        else:
            sys.stderr.write("Internal error\n")
        return EXIT_INTERNAL
    if report.overall_status == STATUS_FAIL:
        return EXIT_FAIL
    return EXIT_OK
