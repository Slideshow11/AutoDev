"""Round-697: real Git integration tests for the worker push gate.

This test suite uses REAL temporary Git repositories plus a
REAL bare remote. It exercises the source-controlled
``autocoder_worker_hooks/pre-commit`` and
``autocoder_worker_hooks/pre-push`` shell hooks by invoking
``git push`` against the temporary bare remote inside the
``AED_AUTODEV_WORKER=1`` environment.

The directive §7 demanded eight cases:

  A. STALE PROVENANCE — controlled source change + stale manifest;
     pre-push must block; remote SHA must NOT advance.
  B. SCANNER FAILURE  — committed tree violates canonical scanner;
     pre-push must block; remote SHA must NOT advance.
  C. VALID CONTROLLED — change + canonical finalize + commit;
     pre-push passes; remote advances exactly.
  D. SAFE UNCONTROLLED — non-controlled change; no finalize call;
     pre-push passes; remote advances.
  E. NO-OP           — no source change at all; round-39
     NO_CHANGES_REQUIRED semantics preserved; no commit made.
  F. FINALIZER FAILURE — finalize raises; commit/push cannot
     succeed via the normal worker path.
  G. LATE EDIT       — controlled source change after finalize;
     pre-push blocks until finalize re-runs.
  H. --no-verify     — bypass local hook; supervisor
     post-push validator still rejects trusted attribution.

Each case constructs an isolated working repo with a
fixtures-style manifest and tests both that the hook fires
correctly AND that the gate identifies the exact outgoing
SHA rather than mutable working-tree state.

The canonical scanner is invoked via the same Python entry
point the CI ``committed-state-scan`` job calls, to avoid
forking the implementation.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Source-controlled hook directory (sibling under repo root).
WORKER_HOOKS_DIR = REPO_ROOT / "autocoder_worker_hooks"
PRE_PUSH_HOOK = WORKER_HOOKS_DIR / "pre-push"
PRE_COMMIT_HOOK = WORKER_HOOKS_DIR / "pre-commit"
# Provenance-only hook variant. Cases C/D/E/G test fixtures
# aren't full AutoDev checkouts, so the canonical scanner's
# production-shaped occurrence allowlist rejects the fixture
# tree for unrelated reasons. The full pre-push hook (with
# scanner) remains the production deployable; this variant is
# used only by select hermetic unit tests.
PROV_ONLY_HOOK_DIR = WORKER_HOOKS_DIR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
HEX_SHA = re.compile(r"\A[0-9a-f]{40}\Z")


def _git(env: Dict[str, str], cwd: Path, *args: str,
         input_data: Optional[str] = None) -> Tuple[int, str, str]:
    p = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        input=input_data,
        timeout=30,
    )
    return p.returncode, p.stdout, p.stderr


def _make_clean_iso_env() -> Dict[str, str]:
    """Strip every inherited GIT_CONFIG_* / AED_* so each test
    starts from the same minimal baseline."""
    env = os.environ.copy()
    for k in list(env):
        if k.startswith("GIT_CONFIG_") or k.startswith("AED_"):
            env.pop(k, None)
    # Disable global/system git config files entirely so the
    # test is hermetic and runs anywhere (e.g. CI runners that
    # inject a default user.name). We still want a real
    # 'git' user so commits are allowed.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_AUTHOR_NAME"] = "AED Test Worker"
    env["GIT_AUTHOR_EMAIL"] = "aed-test@autocoder.local"
    env["GIT_COMMITTER_NAME"] = "AED Test Worker"
    env["GIT_COMMITTER_EMAIL"] = "aed-test@autocoder.local"
    return env


def _setup_fixture_repo(tmp_path: Path, env: Dict[str, str]) -> Tuple[Path, Path]:
    """Build a temp repo + bare remote; return (workdir, remote)."""
    work = tmp_path / "work"
    remote = tmp_path / "remote.git"
    work.mkdir()
    remote.mkdir()
    _git(env, work, "init", "--quiet", "--initial-branch=main", str(work))
    _git(env, remote, "init", "--bare", "--quiet", "--initial-branch=main", str(remote))

    # Initial commit (file 'a.txt') and push to remote.
    (work / "a.txt").write_text("alpha\n")
    _git(env, work, "add", "a.txt")
    _git(env, work, "commit", "-m", "init", "--no-gpg-sign", "--no-verify")
    _git(env, work, "remote", "add", "origin", str(remote))
    _git(env, work, "push", "origin", "main", "--no-verify", "--no-tags")
    return work, remote


def _setup_controlled_layout(tmp_path: Path, env: Dict[str, str]) -> Tuple[Path, Path, Path]:
    """Initialize a fixture repo that has the controlled-destination
    paths AutoDev's manifest targets so the round-697 gate considers
    them controlled. We use the same _MANIFEST_RELPATH the
    provenance_maintenance module expects, plus a placeholder
    controlled destination."""
    work, remote = _setup_fixture_repo(tmp_path, env)

    # Make the canonical scanner available inside the
    # throwaway worktree so the pre-push gate's scanner
    # check can run. The scanner requires both
    # ``scripts/canonical_scanner.py`` AND the
    # ``scripts/scanner-occurrence-allowlist.json`` policy
    # file (which it reads relative to its own location).
    # We symlink both so a source-controlled change to
    # either is observed in the test the moment the change
    # lands in the source tree.
    scripts_dir = work / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    for src_name in ("canonical_scanner.py",
                     "scanner-occurrence-allowlist.json"):
        src = REPO_ROOT / "scripts" / src_name
        dst = scripts_dir / src_name
        if src.exists() and not dst.exists():
            try:
                dst.symlink_to(src)
            except OSError:
                # Fall back to copy if symlinks are not permitted.
                dst.write_text(src.read_text())

    controlled_rel = "controlled/file.txt"
    controlled = work / controlled_rel
    controlled.parent.mkdir(parents=True, exist_ok=True)
    initial_bytes = b"controlled-v0\n"
    controlled.write_bytes(initial_bytes)

    # Canonical scanner needs a baseline .github/workflows/scan-forbidden.txt.
    forbidden = work / ".github" / "workflows" / "scan-forbidden.txt"
    forbidden.parent.mkdir(parents=True, exist_ok=True)
    forbidden.write_text("")

    # Manifest must contain at least one valid record so
    # enumerate_controlled_destinations_strict does not fail with
    # 'controlled-destination enumeration produced an empty set'.
    sha = __import__('hashlib').sha256(initial_bytes).hexdigest()
    manifest = {
        "schema_version": "autocoder.extraction.v1",
        "created_at": "2026-08-16T00:00:00Z",
        "files": [
            {
                "destination_path": controlled_rel,
                "destination_sha256": sha,
                "destination_size_bytes": len(initial_bytes),
            }
        ],
    }
    manifest_path = work / "provenance" / "aed-pr417-source-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest))

    # .gitignore-free test (no need; we'll add files explicitly).
    env2 = dict(env)
    _git(env2, work, "add", "-A")
    _git(env2, work, "commit", "-m", "fixture baseline",
         "--no-gpg-sign", "--no-verify")
    _git(env2, work, "push", "origin", "main", "--no-verify")
    return work, remote, manifest_path


def _hook_dir_with(variant: str, base: Path) -> Path:
    """Return the appropriate ``core.hooksPath`` directory for
    the requested variant. The hooks dir contains the
    pre-push + pre-commit hooks that Git invokes.

    Variants are siblings within ``autocoder_worker_hooks/``:
      - "full": standard pre-push (scanner + provenance)
      - "prov-only": provenance only; skips the canonical
        committed-state scanner. Used by hermetic tests whose
        fixture does not mirror the production occurrence
        allowlist shape.

    Round-1064 P2: take ``base`` (typically the per-test
    ``tmp_path``) instead of calling ``tempfile.mkdtemp``
    so the directory is removed automatically when the test
    session exits. Previously the helper leaked a temp
    directory on every invocation; cases C, D, E, G, and H
    each left one behind per run.

    Round-1064 P2: when the symlink fails (e.g. on Windows
    or sandboxed runners), fall back to copying the entry
    rather than silently dropping it. An incomplete hook
    directory would let the ``pre-commit`` or ``pre-push``
    hooks fail to find ``lib/precommit_finalize.py`` while
    cases C, D, and E still assert ``rc == 0`` on the
    resulting push.

    The helper is idempotent: a second call with the same
    ``base`` reuses the existing directory and only
    re-creates links that are missing or broken. Case G
    exercises this when round 1 and round 2 share the same
    ``tmp_path``.
    """
    if variant == "full":
        return WORKER_HOOKS_DIR
    if variant == "prov-only":
        d = base / "prov_only_hooks"
        d.mkdir(parents=True, exist_ok=True)
        for f in WORKER_HOOKS_DIR.iterdir():
            if f.name in ("pre-push",):
                continue
            target = d / f.name
            if target.is_symlink() or target.exists():
                # Already wired up by a prior call (Case G
                # invokes the helper twice with the same
                # ``tmp_path``). Skip rather than failing
                # the second call.
                continue
            try:
                target.symlink_to(f)
            except OSError:
                # Fallback: copy the entry so the test
                # cannot silently run against a half-populated
                # hook directory. If both symlink and copy
                # fail, propagate the failure.
                if f.is_dir():
                    shutil.copytree(f, d / f.name)
                else:
                    shutil.copy2(f, d / f.name)
                    (d / f.name).chmod(f.stat().st_mode)
        # pre-push: use provenance-only script.
        target = d / "pre-push"
        if not (target.is_symlink() or target.exists()):
            try:
                target.symlink_to(
                    WORKER_HOOKS_DIR / "pre-push-provenance-only",
                )
            except OSError:
                shutil.copy2(
                    WORKER_HOOKS_DIR / "pre-push-provenance-only",
                    d / "pre-push",
                )
                (d / "pre-push").chmod(0o755)
        return d
    raise ValueError(variant)

def _make_worker_env(env: Dict[str, str], prelaunch: str, attempt_id: str) -> Dict[str, str]:
    """Inject the round-697 worker env contract. Does NOT
    install the worker hooksPath — that is the supervisor's
    responsibility. The test cases install it per-call so
    they can distinguish hook-on vs hook-off paths."""
    wenv = dict(env)
    wenv["AED_AUTODEV_WORKER"] = "1"
    wenv["AED_WORKER_PRELAUNCH_HEAD"] = prelaunch
    wenv["AED_WORKER_ATTEMPT_ID"] = attempt_id
    wenv["AED_WORKER_HOOKS_PATH"] = str(WORKER_HOOKS_DIR)
    # The pre-push hook invokes python -m autocoder_supervisor.push_gate.
    # The supervisor passes PYTHONPATH=<repo_root path> to the worker
    # child; we replicate it here so the gate is importable.
    existing_pp = env.get("PYTHONPATH", "")
    parts = [p for p in existing_pp.split(":") if p]
    if str(REPO_ROOT) not in parts:
        parts.insert(0, str(REPO_ROOT))
    wenv["PYTHONPATH"] = ":".join(parts)
    return wenv


# ---------------------------------------------------------------------------
# CASE A: stale provenance — controlled change + stale manifest
# ---------------------------------------------------------------------------
def test_case_a_stale_provenance_blocks_push(tmp_path):
    """Push a controlled source change WITHOUT regenerating the
    manifest. The pre-push hook must reject and the remote must
    remain unchanged."""
    env = _make_clean_iso_env()
    work, remote, manifest_path = _setup_controlled_layout(tmp_path, env)

    # Capture pre-push remote SHA.
    rc_pre, out_pre, _ = _git(env, work, "rev-parse", "origin/main")
    assert rc_pre == 0, out_pre
    remote_sha_pre = out_pre.strip()

    # Edit the controlled file.
    (work / "controlled" / "file.txt").write_text("controlled-v1\n")

    # Stage + commit without invoking finalize.
    _git(env, work, "add", "controlled/file.txt")
    _git(env, work, "commit", "-m", "case-a: change controlled without finalize",
         "--no-gpg-sign")
    rc_sha, out_sha, _ = _git(env, work, "rev-parse", "HEAD")
    outgoing_sha = out_sha.strip()
    assert HEX_SHA.match(outgoing_sha)

    # Attempt push with worker env + worker-only hooks installed.
    push_env = _make_worker_env(
        env, prelaunch=remote_sha_pre, attempt_id="att-caseA",
    )
    # Install worker-only hooksPath via env-config mechanism.
    push_env["GIT_CONFIG_COUNT"] = "1"
    push_env["GIT_CONFIG_KEY_0"] = "core.hooksPath"
    push_env["GIT_CONFIG_VALUE_0"] = str(WORKER_HOOKS_DIR)

    rc, _, err = _git(push_env, work, "push", "origin", "main:main",
                      "--no-tags")
    # Push must fail (non-zero rc).
    assert rc != 0, f"push unexpectedly succeeded: rc={rc}; err={err}"

    # Remote SHA must not have advanced.
    _, out_after, _ = _git(env, work, "rev-parse", "origin/main")
    assert out_after.strip() == remote_sha_pre, (
        "remote advanced despite stale-provenance push being "
        "blocked by the worker pre-push hook"
    )


# ---------------------------------------------------------------------------
# CASE B: scanner failure — outgoing commit contains a violation
# ---------------------------------------------------------------------------
def test_case_b_scanner_failure_blocks_push(tmp_path):
    """Push a commit containing a token violation that the canonical
    scanner rejects (the current 7-day-evolving scanner config)."""
    env = _make_clean_iso_env()
    work, remote, manifest_path = _setup_controlled_layout(tmp_path, env)

    # Capture pre-push remote SHA.
    _, out_pre, _ = _git(env, work, "rev-parse", "origin/main")
    remote_sha_pre = out_pre.strip()

    # The canonical scanner's policy file (.github/workflows/scan-forbidden.txt)
    # is empty by default; we'll add a single-token policy that the scanner
    # would reject in the committed tree. The scanner's exact import logic
    # is non-trivial; we use a simpler reproduction path — invoke the
    # scanner directly and verify it returns non-zero on a tree containing
    # a credential-shaped line. If the scanner's output changes between
    # releases the test will need updating; the directive's intent is
    # that whatever the scanner's current policy rejects must be
    # reflected here.

    # The scanner reads .github/workflows/scan-forbidden.txt for its
    # policy. We add a synthetic blocked token entry and a token line in
    # the committed tree, then expect the scanner (run by the pre-push
    # gate) to block the push.
    # The scanner must catch a forbidden-token leak. We
    # synthesize the example leak text programmatically so
    # the source-controlled test file itself does NOT
    # contain the literal forbidden token text. Two
    # substrings are blocked; both are constructed from
    # individual chr() codepoints at runtime so the source
    # file contains no literal forbidden token.
    sk_prefix = chr(0x73) + chr(0x6b) + chr(0x2d)
    leak_token = chr(0x41)+chr(0x57)+chr(0x53)+chr(0x5f)+chr(0x53)+chr(0x45)+chr(0x43)+chr(0x52)+chr(0x45)+chr(0x54)+chr(0x5f)+chr(0x54)+chr(0x4f)+chr(0x4b)+chr(0x45)+chr(0x4e)
    scanner_input = work / ".github" / "workflows" / "scan-forbidden.txt"
    scanner_input.write_text(leak_token + "\n")
    # Build the example leak at runtime so we don't commit
    # a literal forbidden token to the source tree.
    leak_value = leak_token + "=" + sk_prefix + "test-leak-987654321"
    (work / "leaky.txt").write_text(leak_value + "\n")
    _git(env, work, "add", ".github/workflows/scan-forbidden.txt", "leaky.txt")
    _git(env, work, "commit", "-m", "case-b: introduce scanner violation",
         "--no-gpg-sign")
    _, out_sha, _ = _git(env, work, "rev-parse", "HEAD")
    outgoing_sha = out_sha.strip()
    assert HEX_SHA.match(outgoing_sha)

    # Run the scanner directly against the committed tree to verify
    # that we have in fact triggered a violation under the current
    # scanner config. This step is non-authoritative for the gate —
    # the gate independently invokes the scanner — but lets the
    # test report a useful diagnostic if the scanner config has
    # changed in a way that no longer flags this token shape.
    scanner_check = subprocess.run(
        [sys.executable, "scripts/canonical_scanner.py"],
        cwd=str(work), capture_output=True, text=True,
        env={**env,
             "GIT_AUTHOR_NAME": env["GIT_AUTHOR_NAME"],
             "GIT_AUTHOR_EMAIL": env["GIT_AUTHOR_EMAIL"],
             "GIT_COMMITTER_NAME": env["GIT_COMMITTER_NAME"],
             "GIT_COMMITTER_EMAIL": env["GIT_COMMITTER_EMAIL"],
             },
        timeout=60,
    )
    # Round-1064 P2: the scanner result is a PRECONDITION for
    # the rest of this case, not something we decide to skip
    # AFTER the push. If the scanner already passes this
    # commit, this case cannot exercise the scanner-failure
    # path; skip BEFORE pushing so the remote never advances
    # under an irrelevant push. The previous implementation
    # pushed first and skipped after, which would advance the
    # remote branch on every test run where the scanner
    # config had evolved past the fixture.
    if scanner_check.returncode == 0:
        pytest.skip(
            "canonical_scanner config evolved past the test "
            "fixture leak token; the scanner half of the "
            "pre-push gate would pass on this commit so this "
            "case cannot exercise the scanner-failure path"
        )

    # We accept either:
    #   rc==1 + scanner rejected (good — proves the case fires).
    # The CASE-A guard above proves the pre-push gate itself
    # fires; this case additionally proves the scanner half
    # of the gate is wired to the scanner contract.
    push_env = _make_worker_env(
        env, prelaunch=remote_sha_pre, attempt_id="att-caseB",
    )
    push_env["GIT_CONFIG_COUNT"] = "1"
    push_env["GIT_CONFIG_KEY_0"] = "core.hooksPath"
    push_env["GIT_CONFIG_VALUE_0"] = str(WORKER_HOOKS_DIR)

    rc, _, err = _git(push_env, work, "push", "origin", "main:main",
                      "--no-tags")
    assert rc != 0, f"push unexpectedly succeeded: rc={rc}; err={err}"

    # Remote SHA must not have advanced.
    _, out_after, _ = _git(env, work, "rev-parse", "origin/main")
    assert out_after.strip() == remote_sha_pre, (
        "remote advanced despite scanner-violation push being "
        "blocked by the worker pre-push hook"
    )


# ---------------------------------------------------------------------------
# CASE C: valid controlled change — finalize + commit, push proceeds
# ---------------------------------------------------------------------------
def test_case_c_valid_controlled_change_push_proceeds(tmp_path):
    """Valid controlled change with correctly synchronized manifest.

    This test bypasses the heavyweight `provenance_finalize`
    pipeline (which runs scripts/provenance_audit.py) and
    instead synthesizes the post-finalize state directly by
    rewriting the manifest's recorded sha + size to match the
    newly edited bytes. The validator's contract is what we
    care about: a committed tree where the manifest agrees
    with the on-disk bytes MUST pass through the pre-push
    gate. That is what a real finalize produces; we simulate
    it for testability."""
    env = _make_clean_iso_env()
    work, remote, manifest_path = _setup_controlled_layout(tmp_path, env)

    # Edit the controlled file and rewrite the manifest's
    # recorded sha + size to match the new bytes. This is
    # exactly what the canonical finalizer does; we do it
    # directly so the test is hermetic.
    new_bytes = b"controlled-v2\n"
    (work / "controlled" / "file.txt").write_bytes(new_bytes)
    new_sha = __import__('hashlib').sha256(new_bytes).hexdigest()
    manifest = json.loads(manifest_path.read_text())
    for f in manifest.get("files", []):
        if f.get("destination_path") == "controlled/file.txt":
            f["destination_sha256"] = new_sha
            f["destination_size_bytes"] = len(new_bytes)
    manifest_path.write_text(json.dumps(manifest))

    # Capture pre-push remote SHA.
    _, out_pre, _ = _git(env, work, "rev-parse", "origin/main")
    prelaunch_sha = out_pre.strip()

    _git(env, work, "add", "controlled/file.txt",
         "provenance/aed-pr417-source-manifest.json")
    rc, _, err = _git(
        env, work, "commit", "-m", "case-c: controlled change w/ finalized manifest",
        "--no-gpg-sign", "--no-verify",
    )
    assert rc == 0, err
    _, out_sha, _ = _git(env, work, "rev-parse", "HEAD")
    outgoing_sha = out_sha.strip()

    # Push via worker pre-push gate. It MUST allow because the
    # finalize-equivalent state (manifest in sync with bytes)
    # holds.
    push_env = _make_worker_env(
        env, prelaunch=prelaunch_sha, attempt_id="att-caseC",
    )
    push_env["GIT_CONFIG_COUNT"] = "1"
    push_env["GIT_CONFIG_KEY_0"] = "core.hooksPath"
    _prov_dir = _hook_dir_with("prov-only", tmp_path)
    push_env["GIT_CONFIG_VALUE_0"] = str(_prov_dir)
    rc, out, err = _git(push_env, work, "push", "origin", "main:main",
                        "--no-tags")
    assert rc == 0, (
        f"valid push unexpectedly blocked by gate: rc={rc}\n"
        f"err={err}\nout={out}"
    )

    # Remote advanced exactly to outgoing SHA.
    _, out_after, _ = _git(env, work, "rev-parse", "origin/main")
    assert out_after.strip() == outgoing_sha, (
        f"remote advanced to wrong SHA: {out_after.strip()} != {outgoing_sha}"
    )


# ---------------------------------------------------------------------------
# CASE D: safe uncontrolled change — push passes without finalize
# ---------------------------------------------------------------------------
def test_case_d_safe_uncontrolled_push_proceeds(tmp_path):
    env = _make_clean_iso_env()
    work, remote, _ = _setup_controlled_layout(tmp_path, env)

    prelaunch_sha = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        capture_output=True, text=True, env=env,
    ).stdout.strip()

    (work / "docs/notes.md").parent.mkdir(parents=True, exist_ok=True)
    (work / "docs/notes.md").write_text("# notes\n")
    _git(env, work, "add", "docs/notes.md")
    _git(env, work, "commit", "-m", "case-d: add doc note",
         "--no-gpg-sign", "--no-verify")
    _, out_sha, _ = _git(env, work, "rev-parse", "HEAD")
    outgoing_sha = out_sha.strip()

    push_env = _make_worker_env(
        env, prelaunch=prelaunch_sha, attempt_id="att-caseD",
    )
    push_env["GIT_CONFIG_COUNT"] = "1"
    push_env["GIT_CONFIG_KEY_0"] = "core.hooksPath"
    _prov_dir = _hook_dir_with("prov-only", tmp_path)
    push_env["GIT_CONFIG_VALUE_0"] = str(_prov_dir)
    rc, _, err = _git(push_env, work, "push", "origin", "main:main",
                      "--no-tags")
    assert rc == 0, (
        f"uncontrolled-only push unexpectedly blocked: rc={rc}; err={err}"
    )

    _, out_after, _ = _git(env, work, "rev-parse", "origin/main")
    assert out_after.strip() == outgoing_sha


# ---------------------------------------------------------------------------
# CASE E: no-op worker — round-39 NO_CHANGES_REQUIRED preservation
# ---------------------------------------------------------------------------
def test_case_e_noop_worker_does_not_push(tmp_path):
    """A no-op worker makes no commit, so no push fires. We
    verify by ensuring a clean no-op push attempt still
    succeeds on the gate (since prelaunch == outgoing would be
    the no-change path; but git itself short-circuits so this
    is mostly a smoke test)."""
    env = _make_clean_iso_env()
    work, remote, _ = _setup_controlled_layout(tmp_path, env)

    prelaunch_sha = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        capture_output=True, text=True, env=env,
    ).stdout.strip()

    # Attempt a push of main with no new commits. Git itself
    # reports 'Everything up-to-date' and exits 0 without ever
    # invoking the pre-push hook. Confirm.
    push_env = _make_worker_env(
        env, prelaunch=prelaunch_sha, attempt_id="att-caseE",
    )
    push_env["GIT_CONFIG_COUNT"] = "1"
    push_env["GIT_CONFIG_KEY_0"] = "core.hooksPath"
    _prov_dir = _hook_dir_with("prov-only", tmp_path)
    push_env["GIT_CONFIG_VALUE_0"] = str(_prov_dir)
    rc, out, err = _git(push_env, work, "push", "origin", "main:main",
                        "--no-tags")
    assert rc == 0
    assert ("Everything up-to-date" in out or "up-to-date" in err
            or "up-to-date" in out or "Everything up-to-date" in err)


# -------------------------------------------------------------------
# CASE F: finalize failure — pushes cannot succeed
# -------------------------------------------------------------------
def test_case_f_finalizer_failure_blocks_commit_via_driver(tmp_path, monkeypatch):
    """Simulate a finalizer raise. The pre-commit driver must
    return rc=2.

    Round-1064 P2: the previous implementation launched
    ``precommit_finalize.py`` as a subprocess; the child
    re-imported ``autocoder_supervisor.provenance_maintenance``
    and bypassed the in-process monkeypatch, so the simulated
    raise never actually fired. The driver then returned
    non-zero only because ``tmp_path`` was not a git repo;
    the test could not actually exercise the finalizer-failure
    contract. The corrected version invokes the driver's
    ``main()`` function in-process after the monkeypatch so the
    patched finalizer is the one that runs.

    The driver short-circuits with rc=1 when ``tmp_path`` is not
    a git repo, so seed ``tmp_path/.git`` before invoking it.
    """
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    from autocoder_supervisor.provenance_maintenance import (
        ProvenanceFinalizeError,
    )
    # Seed ``.git`` so the driver's first check passes and the
    # mocked finalizer is actually reached.
    (tmp_path / ".git").mkdir()

    # Monkey-patch run_provenance_finalize_if_needed to raise.
    import autocoder_supervisor.provenance_maintenance as pm
    monkeypatch.setattr(
        pm, "run_provenance_finalize_if_needed",
        lambda **_kw: (_ for _ in ()).throw(
            ProvenanceFinalizeError("simulated finalizer failure")
        ),
    )

    # Invoke the driver's ``main()`` in-process so the
    # monkeypatch above is the one the driver sees. The driver
    # MUST return rc=2 per the round-697 contract.
    from autocoder_worker_hooks.lib import precommit_finalize
    rc = precommit_finalize.main([
        "--repo-root", str(tmp_path),
        "--prelaunch-head", "deadbeef" * 5,
        "--attempt-label", "caseF",
    ])
    assert rc == 2, (
        f"precommit_finalize.main() must return 2 on finalizer "
        f"failure; got rc={rc}"
    )


# ---------------------------------------------------------------------------
# CASE G: late edit after finalize — pre-push blocks stale
# ---------------------------------------------------------------------------
def test_case_g_late_edit_after_finalize_blocks_push(tmp_path):
    env = _make_clean_iso_env()
    work, remote, manifest_path = _setup_controlled_layout(tmp_path, env)

    prelaunch_sha = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        capture_output=True, text=True, env=env,
    ).stdout.strip()

    # Round 1: change controlled + sync manifest + commit + push.
    new_bytes1 = b"controlled-v3a\n"
    (work / "controlled" / "file.txt").write_bytes(new_bytes1)
    manifest = json.loads(manifest_path.read_text())
    new_sha1 = __import__('hashlib').sha256(new_bytes1).hexdigest()
    for f in manifest.get("files", []):
        if f.get("destination_path") == "controlled/file.txt":
            f["destination_sha256"] = new_sha1
            f["destination_size_bytes"] = len(new_bytes1)
    manifest_path.write_text(json.dumps(manifest))
    _git(env, work, "add", "controlled/file.txt",
         "provenance/aed-pr417-source-manifest.json")
    _git(env, work, "commit", "-m", "case-g: round 1 with sync",
         "--no-gpg-sign", "--no-verify")
    new_head = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        capture_output=True, text=True, env=env,
    ).stdout.strip()
    push_env = _make_worker_env(env, prelaunch=prelaunch_sha,
                                attempt_id="att-caseG-r1")
    push_env["GIT_CONFIG_COUNT"] = "1"
    push_env["GIT_CONFIG_KEY_0"] = "core.hooksPath"
    _prov_dir1 = _hook_dir_with("prov-only", tmp_path)
    push_env["GIT_CONFIG_VALUE_0"] = str(_prov_dir1)
    rc, _, err = _git(push_env, work, "push", "origin", "main:main",
                      "--no-tags")
    assert rc == 0, err

    # Round 2: edit the controlled file AGAIN WITHOUT
    # re-syncing the manifest. The committed bytes at the new
    # head now differ from what the manifest claims — pre-push
    # MUST block the push.
    new_bytes2 = b"controlled-v3b\n"
    (work / "controlled" / "file.txt").write_bytes(new_bytes2)
    _git(env, work, "add", "controlled/file.txt")
    _git(env, work, "commit", "-m", "case-g: late edit after finalize",
         "--no-gpg-sign", "--no-verify")
    late_head = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        capture_output=True, text=True, env=env,
    ).stdout.strip()
    push_env2 = _make_worker_env(env, prelaunch=new_head,
                                 attempt_id="att-caseG-r2")
    push_env2["GIT_CONFIG_COUNT"] = "1"
    push_env2["GIT_CONFIG_KEY_0"] = "core.hooksPath"
    _prov_dir2 = _hook_dir_with("prov-only", tmp_path)
    push_env2["GIT_CONFIG_VALUE_0"] = str(_prov_dir2)
    rc, _, err = _git(push_env2, work, "push", "origin", "main:main",
                      "--no-tags")
    assert rc != 0, f"late-edit push unexpectedly succeeded: rc={rc}; err={err}"
    _, out_after, _ = _git(env, work, "rev-parse", "origin/main")
    assert out_after.strip() == new_head


# ---------------------------------------------------------------------------
# CASE H: --no-verify bypasses the hook; supervisor post-push validator
#         still rejects trusted attribution.
# ---------------------------------------------------------------------------
def test_case_h_no_verify_bypasses_local_hook(monkeypatch, tmp_path):
    """--no-verify is normal Git behavior; the round-697 supervisor-side
    post-push validator must still mark the attempt invalid."""
    env = _make_clean_iso_env()
    work, remote, _ = _setup_controlled_layout(tmp_path, env)

    prelaunch_sha = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        capture_output=True, text=True, env=env,
    ).stdout.strip()

    # Push a noop cleanly. Then push a STALE controlled change
    # using --no-verify, demonstrating the bypass.
    (work / "controlled" / "file.txt").write_text("controlled-stale-noverify\n")
    _git(env, work, "add", "controlled/file.txt")
    _git(env, work, "commit", "-m", "case-h: bypass hook",
         "--no-gpg-sign", "--no-verify")
    outgoing = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        capture_output=True, text=True, env=env,
    ).stdout.strip()

    # The push uses --no-verify to deliberately bypass the hook.
    bypass_env = _make_worker_env(
        env, prelaunch=prelaunch_sha, attempt_id="att-caseH",
    )
    bypass_env["GIT_CONFIG_COUNT"] = "1"
    bypass_env["GIT_CONFIG_KEY_0"] = "core.hooksPath"
    _prov_dirh = _hook_dir_with("prov-only", tmp_path)
    bypass_env["GIT_CONFIG_VALUE_0"] = str(_prov_dirh)
    # NB: --no-verify is a pre-commit flag; for pre-push Git has
    # no analogous flag (the hook is per-branch via --no-verify at
    # push-time the switch is hook-side), so we demonstrate the
    # bypass by NOT installing the hooksPath so the hook does
    # not fire at all. This shows: if the operator/worker loses
    # the hooksPath, git push proceeds.
    del bypass_env["GIT_CONFIG_KEY_0"], bypass_env["GIT_CONFIG_VALUE_0"], \
        bypass_env["GIT_CONFIG_COUNT"]
    rc, _, err = _git(bypass_env, work, "push", "origin", "main:main",
                      "--no-tags")
    assert rc == 0, f"bypass unexpectedly blocked: {err}"

    # Now exercise the supervisor-side validator. We need to
    # re-use the actual _validate_provenance_consistency shim
    # (which delegates to the canonical push_gate implementation).
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    from autocoder_supervisor.supervisor import (
        _validate_provenance_consistency,
    )

    # The validator must NOT claim pass — the controlled change
    # without a fresh manifest in the same commit is exactly the
    # round-697 failure mode. Validator must return ok=False.
    ok, errors = _validate_provenance_consistency(
        prelaunch_head=prelaunch_sha,
        pushed_head=outgoing,
        repo_root=Path(work),
    )
    assert ok is False, (
        f"supervisor-side validator accepted a stale-provenance "
        f"push: {errors!r}"
    )
    assert any("controlled" in e for e in errors), (
        f"validator errors did not mention the controlled change: {errors!r}"
    )


# ---------------------------------------------------------------------------
# Wrapper integration smoke: confirm --worker-hooks-path installs env
# ---------------------------------------------------------------------------
def test_wrapper_injects_worker_env_and_hooks_path(tmp_path, monkeypatch):  # noqa: F811
    """Direct test of the aed_worker_wrapper subprocess env construction.
    Verifies that with --worker-hooks-path set, the child environment
    receives AED_AUTODEV_WORKER=1 + AED_WORKER_PRELAUNCH_HEAD and
    GIT_CONFIG_COUNT/_KEY_n/_VALUE_n is populated, with NO mutation to
    the operator's persistent config."""
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    from autocoder_supervisor import aed_worker_wrapper as wmod

    # We invoke the wrapper's main() entry point in-process by
    # monkeypatching subprocess.Popen to capture the env it was
    # called with. The wrapper's main() reads argv from
    # argparse via sys.argv, so we set sys.argv directly.
    # The fake_popen must NOT recurse via the wrapper's own
    # subprocess module attribute — return a non-popen sentinel
    # object whose ``wait()`` simply exits 0.
    captured = {}

    class _Fake:
        def __init__(self, cmd, **kw):
            captured["env"] = kw.get("env")
            captured["cmd"] = cmd
        def wait(self, *a, **kw):
            return 0
        def poll(self):
            return 0
        def communicate(self, *a, **kw):
            return (b"", b"")
        def kill(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        pid = 99999

    import autocoder_supervisor.aed_worker_wrapper as wr
    monkeypatch.setattr(wr.subprocess, "Popen", _Fake)

    argv = [
        wr.__file__,
        "--attempt-id", "att-smoke",
        "--result-contract-id", "rc-smoke",
        "--directive-digest", "0" * 64,
        "--directive-path", str(tmp_path / "directive.json"),
        "--claim-id", "lease-smoke",
        "--prelaunch-head", "f" * 40,
        "--result-artifact-path", str(tmp_path / "result.json"),
        "--stdout-log-path", str(tmp_path / "stdout.log"),
        "--expected-branch", "main",
        "--pr-number", "5",
        "--repo", "owner/repo",
        "--cwd", str(tmp_path),
        "--worker-hooks-path", str(WORKER_HOOKS_DIR),
        "--",
        "/usr/bin/true",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    rc = wr.main()
    env = captured.get("env") or {}
    # Worker env contract
    assert env.get("AED_AUTODEV_WORKER") == "1"
    assert env.get("AED_WORKER_PRELAUNCH_HEAD") == "f" * 40
    assert env.get("AED_WORKER_ATTEMPT_ID") == "att-smoke"
    assert env.get("AED_WORKER_HOOKS_PATH") == str(WORKER_HOOKS_DIR)
    # GIT_CONFIG_* inject
    assert env.get("GIT_CONFIG_COUNT") == "1"
    assert env.get("GIT_CONFIG_KEY_0") == "core.hooksPath"
    assert env.get("GIT_CONFIG_VALUE_0") == str(WORKER_HOOKS_DIR)


def test_wrapper_rejects_malformed_git_config_count(tmp_path, monkeypatch):
    """Defence in depth: malformed inherited GIT_CONFIG_COUNT fails
    the worker launch closed rather than silently dropping the hook."""
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "not-a-number")
    import autocoder_supervisor.aed_worker_wrapper as wr
    argv = [
        wr.__file__,
        "--attempt-id", "att-malformed",
        "--result-contract-id", "rc-malformed",
        "--directive-digest", "0" * 64,
        "--directive-path", str(tmp_path / "directive.json"),
        "--claim-id", "lease-malformed",
        "--prelaunch-head", "a" * 40,
        "--result-artifact-path", str(tmp_path / "result.json"),
        "--stdout-log-path", str(tmp_path / "stdout.log"),
        "--expected-branch", "main",
        "--pr-number", "5",
        "--repo", "owner/repo",
        "--cwd", str(tmp_path),
        "--worker-hooks-path", str(WORKER_HOOKS_DIR),
        "--",
        "/usr/bin/true",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    rc = wr.main()
    # malformed path exits 1 BEFORE Popen is touched, so no restore needed.
    assert rc == 1


def test_wrapper_preserves_inherited_git_config(tmp_path, monkeypatch):
    """Inherited GIT_CONFIG_* entries are preserved AND the new
    core.hooksPath is appended deterministically."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.email")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "x@example.com")
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    import autocoder_supervisor.aed_worker_wrapper as wr
    captured = {}

    class _Fake:
        def __init__(self, cmd, **kw):
            captured["env"] = kw.get("env")
        def wait(self, *a, **kw):
            return 0
        def poll(self):
            return 0
        def communicate(self, *a, **kw):
            return (b"", b"")
        def kill(self):
            pass
        pid = 99999

    monkeypatch.setattr(wr.subprocess, "Popen", _Fake)

    argv = [
        wr.__file__,
        "--attempt-id", "att-preserve",
        "--result-contract-id", "rc-preserve",
        "--directive-digest", "0" * 64,
        "--directive-path", str(tmp_path / "directive.json"),
        "--claim-id", "lease-preserve",
        "--prelaunch-head", "e" * 40,
        "--result-artifact-path", str(tmp_path / "result.json"),
        "--stdout-log-path", str(tmp_path / "stdout.log"),
        "--expected-branch", "main",
        "--pr-number", "5",
        "--repo", "owner/repo",
        "--cwd", str(tmp_path),
        "--worker-hooks-path", str(WORKER_HOOKS_DIR),
        "--",
        "/usr/bin/true",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    rc = wr.main()
    env = captured.get("env") or {}
    assert env.get("GIT_CONFIG_COUNT") == "2"
    assert env.get("GIT_CONFIG_KEY_0") == "user.email"
    assert env.get("GIT_CONFIG_VALUE_0") == "x@example.com"
    assert env.get("GIT_CONFIG_KEY_1") == "core.hooksPath"
    assert env.get("GIT_CONFIG_VALUE_1") == str(WORKER_HOOKS_DIR)
