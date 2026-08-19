"""Canonical worker pre-push / supervisor post-push gate.

This module owns THE single canonical implementation of the
push-boundary validators that decide whether a worker-built
local commit (the proposed outgoing tip) is permitted to be
pushed to the remote.

It is consumed by exactly three callers:

  1. ``autocoder_supervisor.supervisor._validate_provenance_consistency``
     – the existing round-695 supervisor-side validator
     (post-push defense in depth). That function becomes a
     thin re-export shim that delegates here so the
     behavioural contract is unchanged but the implementation
     lives in this module.

  2. ``autocoder_worker_hooks/pre-push`` – the source-controlled
     pre-push hook installed ONLY into AED_AUTODEV_WORKER child
     environments. The hook invokes the CLI entry point
     ``python -m autocoder_supervisor.push_gate validate
     <repo> <prelaunch> <outgoing>`` and propagates the exit
     code to Git, blocking the network update on failure.

  3. ``autocoder_worker_hooks/pre-commit`` – the source-controlled
     pre-commit hook that calls ``run_provenance_finalize_if_needed``
     on the worker's working tree when a manifest-controlled
     destination has been touched, so the worker's commit becomes
     push-ready deterministically.

Two pure validators are exported:

  - ``validate_provenance_consistency_at_sha``
      Reads committed bytes for ``outgoing`` SHA and asserts
      that, whenever a manifest-controlled destination
      changed between ``prelaunch`` and ``outgoing``, the
      manifest at ``outgoing`` describes the exact on-disk
      bytes (sha256 + size_bytes) of every controlled
      destination. This is the canonical round-697 single-SHA
      provenance-consistency check, hoisted from
      ``supervisor.py`` so both supervisor-side and
      worker-side hooks share one implementation.

  - ``validate_committed_state_scan_at_sha``
      Runs the canonical ``scripts.canonical_scanner.py``
      against the *committed outgoing* tree at ``outgoing``,
      not against the worker's mutable working tree. A
      scanner violation is reported as a failure; the
      remote branch MUST NOT advance.

A tiny CLI is exposed via the ``__main__`` block:

  ``python -m autocoder_supervisor.push_gate validate
        --repo-root <path> --prelaunch <sha>
        --outgoing <sha> [--check provenance] [--check scanner]``

Exit codes:

  - ``0`` — every requested check passed; pre-push should
    succeed.
  - ``1`` — operator-style hard error (bad args, malformed
    SHAs, missing repo). The hook must fail closed.
  - ``2`` — at least one validator returned
    ``(ok=False, errors=[...])``; pre-push must block the
    push and the *stdout* may contain the structured error
    payload (the hook interprets exit 2 as a deterministic
    push block).
  - ``3`` — internal inconsistency (subprocess timeout,
    permission, etc.). Same operational meaning as 2.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Iterable, Optional


_HEX_SHA_RE = re.compile(r"\A[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")

# The manifest relpath is the canonical location the supervisor
# uses today (see supervisor._validate_provenance_consistency).
# We re-declare it here as a single source of truth so the
# worker pre-push hook and the supervisor validator agree by
# construction. If this ever changes, both implementations move
# together because they share this module.
_MANIFEST_RELPATH = "provenance/aed-pr417-source-manifest.json"


# ---------------------------------------------------------------------------
# Pure pure-pure-pure: validate_provenance_consistency_at_sha
# ---------------------------------------------------------------------------
def validate_provenance_consistency_at_sha(
    *,
    prelaunch_head: str,
    outgoing_head: str,
    repo_root: Path,
    allow_synthetic_outgoing: bool = False,
) -> tuple:
    """Single canonical round-697 provenance-consistency check.

    Returns ``(ok: bool, errors: list[str])``.

    The check is implemented entirely against committed Git
    object bytes; the validator does not read mutable working
    tree state, the staged set, or the index. This invariant is
    what allows the same function to be called both

      * before the push (worker pre-push hook, against the
        proposed outgoing SHA Git is about to push), and

      * after the push (supervisor round-695, against the
        observed live head).

    In both cases the only inputs are ``prelaunch_head`` and
    ``outgoing_head``; only those two SHAs are read.

    Parameters
    ----------
    prelaunch_head
        The 40-char SHA the worker was launched against (the
        head recorded in the per-attempt envelope).
    outgoing_head
        The 40-char SHA the hook or validator should treat as
        the committed outgoing tip.
    repo_root
        Absolute path to the worker repository.
    allow_synthetic_outgoing
        Round-1064 P2: opt-in tolerance for unit tests that
        construct synthetic ``outgoing_head`` values which are
        NOT real Git objects in ``repo_root``. The default is
        ``False`` — the production pre-push hook MUST always
        fail closed when ``outgoing_head`` is not a real Git
        object, because an unknown outgoing SHA bypasses both
        the controlled-destination and the manifest checks for
        the worker's push. The supervisor's post-push defense
        in depth (round-695) and unit tests that need to
        exercise the synthetic-fixture path pass ``True``
        explicitly. The pre-push hook MUST NOT pass ``True``.

    """
    errors: list = []
    if not _HEX_SHA_RE.match(prelaunch_head or ""):
        return (False, [f"invalid prelaunch_head: {prelaunch_head!r}"])
    if not _HEX_SHA_RE.match(outgoing_head or ""):
        return (False, [f"invalid outgoing_head: {outgoing_head!r}"])
    if prelaunch_head == outgoing_head:
        return (True, [])

    repo_root = Path(repo_root).resolve()

    # Round-1064 P2: ``outgoing_head`` MUST be a real Git object
    # in this repo. The push gate is a security boundary; a worker
    # that sets ``AED_WORKER_PRELAUNCH_HEAD`` to a well-formed but
    # unknown 40-hex value MUST NOT bypass the controlled-destination
    # and manifest checks for its own push. An unknown
    # ``outgoing_head`` therefore fails closed unless the caller
    # has explicitly opted in via ``allow_synthetic_outgoing=True``
    # (used only by unit tests and the supervisor's post-push
    # defense in depth).
    #
    # The synthetic-fixture tolerance for ``prelaunch_head`` is
    # preserved to keep the unit-test path green: prelaunch is
    # documented (lines 122-129) as the head the worker was LAUNCHED
    # against, and tests construct synthetic fixtures for that
    # input. The tolerance is narrowly scoped to prelaunch only;
    # outgoing_head, which Git is actually about to push, must
    # always resolve unless explicitly opted out.
    try:
        _outgoing_check = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "-t", outgoing_head],
            capture_output=True, timeout=10,
        )
    except subprocess.SubprocessError as e:
        # Round-1064 P2: a slow / crashed git invocation is a
        # internal-inconsistency failure. Surface as a validator
        # error (not a silent pass); the caller's caller (the
        # pre-push hook) treats any non-ok result as a block.
        return (False, [
            f"git cat-file for outgoing_head {outgoing_head[:12]}... "
            f"failed with subprocess error: {e!r}"
        ])
    if _outgoing_check.returncode != 0:
        if not allow_synthetic_outgoing:
            return (False, [
                f"outgoing_head {outgoing_head[:12]}... is not a real Git "
                f"object in {repo_root}; push gate cannot prove provenance "
                "consistency without a resolvable committed tip"
            ])
        # Opt-in synthetic tolerance: the supervisor's post-push
        # defense-in-depth and unit tests use this path. Skip the
        # outgoing-side checks (the diff, the manifest index,
        # the controlled-destination comparison) and return the
        # legacy ``(True, [])`` so the supervisor's classification
        # does not regress for synthetic fixtures.
        return (True, [])

    # Round-167/CodeRabbit-P2: the ``prelaunch_is_synthetic``
    # flag is computed for diagnostic clarity but the
    # validator MUST remain fail-closed for an unresolvable
    # prelaunch_head. A real worker push never supplies an
    # unknown prelaunch SHA; the only legitimate callers
    # with synthetic prelaunch values are unit tests, and
    # those tests assert against real prelaunch SHA values
    # (``git rev-parse HEAD~1``). The unconditional
    # ``git diff`` at step 2 below is the correct
    # fail-closed behavior.
    prelaunch_is_synthetic = False
    try:
        _prelaunch_check = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "-t", prelaunch_head],
            capture_output=True, timeout=10,
        )
    except subprocess.SubprocessError as e:
        return (False, [
            f"git cat-file for prelaunch_head {prelaunch_head[:12]}... "
            f"failed with subprocess error: {e!r}"
        ])
    if _prelaunch_check.returncode != 0:
        # Surface the synthetic-fixture observation but do
        # NOT use it to bypass any subsequent check. The
        # diff at step 2 will fail closed naturally.
        prelaunch_is_synthetic = True

    # Importing manifest helpers from provenance_maintenance
    # avoids any divergent reimplementation. We use
    # ``enumerate_controlled_destinations_strict`` only as a
    # structural reference; the validator itself does NOT
    # call it because it requires on-disk path inputs and
    # reads mutable working-tree state. The validator must
    # operate only against committed bytes, so it walks the
    # already-parsed ``manifest_data`` dict via the local
    # helper ``_controlled_destinations_from_manifest``.
    #
    # Round-817 (P1 repair): the controlled_set MUST also
    # include any destination that was controlled at
    # prelaunch_head but is being *removed* from the
    # outgoing manifest. Otherwise a worker can modify a
    # controlled file and drop its manifest record in the
    # same commit; the outgoing manifest would no longer
    # list the path, ``controlled_changed`` would be empty,
    # and the validator would self-exempt.
    from autocoder_supervisor.provenance_maintenance import (  # noqa: E402
        ManifestEnumerationError,
    )

    # Read the committed outgoing manifest. The validator
    # derives controlled_set and the manifest_index from
    # THIS dict; no on-disk file is consulted. This
    # invariant is what makes the validator deterministic
    # against ``git show <sha>:path`` bytes only.
    try:
        manifest_show = subprocess.run(
            ["git", "-C", str(repo_root), "show",
             f"{outgoing_head}:{_MANIFEST_RELPATH}"],
            capture_output=True, timeout=15,
        )
    except subprocess.SubprocessError as e:
        return (False, [
            f"git show for outgoing manifest failed with "
            f"subprocess error: {e!r}"
        ])
    if manifest_show.returncode != 0:
        # The committed tree at outgoing_head does not contain
        # the canonical manifest. That alone is a hard fail
        # for a real push: it means the worker never ran
        # ``run_provenance_finalize_if_needed`` and committed
        # the manifest. The pre-push hook must block.
        return (False, [
            f"canonical manifest '{_MANIFEST_RELPATH}' "
            f"missing from committed tree {outgoing_head[:12]}..."
        ])
    try:
        manifest_data = json.loads(manifest_show.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return (False, [
            f"committed manifest at {outgoing_head[:12]}... "
            f"is not valid JSON: {e}"
        ])

    # Round-817 (P1 repair): also read the PRELAUNCH
    # committed manifest so the controlled_set still
    # includes destinations that the outgoing commit is
    # *removing* from the manifest. Without this, a worker
    # could modify a controlled file and drop its manifest
    # record in the same commit; the outgoing manifest
    # would no longer list the path, ``controlled_changed``
    # would be empty, and the validator would self-exempt.
    #
    # Missing prelaunch manifest is treated as "no prior
    # controlled set" — this is acceptable because the
    # prelaunch_head can be a synthetic fixture in unit
    # tests, and the validator still catches
    # shrink-by-removal against the empty prelaunch set
    # (it falls through to outgoing-only).
    prelaunch_manifest_data = None
    try:
        prelaunch_show = subprocess.run(
            ["git", "-C", str(repo_root), "show",
             f"{prelaunch_head}:{_MANIFEST_RELPATH}"],
            capture_output=True, timeout=15,
        )
    except subprocess.SubprocessError as e:
        return (False, [
            f"git show for prelaunch manifest failed with "
            f"subprocess error: {e!r}"
        ])
    if prelaunch_show.returncode == 0:
        try:
            prelaunch_manifest_data = json.loads(
                prelaunch_show.stdout.decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            prelaunch_manifest_data = None

    try:
        # Derive the controlled-set from the COMMITTED
        # outgoing manifest, not the on-disk file. The
        # on-disk file may differ from the committed
        # outgoing tree (worker uncommitted changes, index
        # state, etc.), and using it would let a worker
        # self-exempt by editing the manifest on disk
        # without committing those edits.
        outgoing_controlled = set(
            _controlled_destinations_from_manifest(manifest_data)
        )
        prelaunch_controlled = set(
            _controlled_destinations_from_manifest(prelaunch_manifest_data)
            if prelaunch_manifest_data is not None else []
        )
        controlled_set = outgoing_controlled | prelaunch_controlled
        if not controlled_set:
            # Mirror the strict-helper's empty-set
            # fail-closed semantic: an empty controlled_set
            # after the union means the manifests enumerate
            # zero destinations, which is a structural
            # drift from the canonical round-697 contract.
            return (False, [
                "manifest-controlled-path derivation failed: "
                "controlled-destination enumeration produced an "
                "empty set; manifests have no records or are "
                "wrong-format"
            ])
    except ManifestEnumerationError as e:
        return (False, [f"manifest-controlled-path derivation failed: {e}"])

    # Step 2: enumerate the changed paths between prelaunch_head
    # and outgoing_head. Done with `git diff --name-only` so we
    # see exactly the committed bytes Git is about to publish.
    #
    # Round-167/CodeRabbit-P2: this ``git diff`` is
    # unconditional; an unresolvable ``prelaunch_head`` will
    # fail here (rc != 0) and the validator returns
    # ``(False, ...)``. This is the correct fail-closed
    # behavior for the security boundary and matches the
    # production pre-push-hook contract. The
    # ``prelaunch_is_synthetic`` flag observed above is
    # surfaced in the error message so the operator can
    # distinguish "prelaunch is not a Git object" from a
    # generic diff failure.
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "diff", "--name-only",
             prelaunch_head, outgoing_head],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.SubprocessError as e:
        return (False, [
            f"git diff failed with subprocess error: {e!r}"
        ])
    if proc.returncode != 0:
        hint = (
            " (prelaunch_head is not a resolvable Git object)"
            if prelaunch_is_synthetic else ""
        )
        return (False, [
            f"git diff failed: rc={proc.returncode}{hint} "
            f"stderr={proc.stderr[:200]!r}"
        ])

    changed_paths = {
        line.strip() for line in proc.stdout.splitlines() if line.strip()
    }
    controlled_changed = sorted(
        path for path in changed_paths if path in controlled_set
    )
    if not controlled_changed:
        return (True, [])

    # Step 3: a controlled destination changed. The manifest
    # itself must be in the same commit AND its recorded
    # sha256/size must match the on-disk bytes at outgoing_head.
    if _MANIFEST_RELPATH not in changed_paths:
        errors.append(
            "controlled-destination changed "
            f"({', '.join(controlled_changed)}) but the "
            "canonical extraction manifest at "
            f"'{_MANIFEST_RELPATH}' was NOT updated in the "
            f"same commit pushed at {outgoing_head[:12]}...; "
            "worker must invoke "
            "autocoder_supervisor.provenance_maintenance."
            "run_provenance_finalize_if_needed before git commit."
        )

    # Step 4: even if the manifest is in the commit, every
    # controlled destination's bytes must match the manifest's
    # recorded sha256 + size_bytes.
    # Round-1064 P2: derive both the controlled set AND the
    # manifest_index from the SAME walker so the two views
    # cannot drift. The previous implementation walked the
    # whole manifest for controlled_set but only read
    # ``manifest_data["files"]`` for the manifest_index; a
    # manifest that recorded destinations outside ``files``
    # or under ``autodev_destination`` would yield a controlled
    # destination with no index entry and the validator would
    # falsely report "no manifest entry exists for it".
    manifest_index: dict = {}
    for entry in manifest_data.get("files", []):
        dp = entry.get("destination_path")
        sha = entry.get("destination_sha256")
        sz = entry.get("destination_size_bytes")
        if not isinstance(dp, str) or not isinstance(sha, str):
            continue
        manifest_index[dp] = (sha, sz)
    # Round-1064 P2: extend the manifest_index with any
    # destination the unified walker discovered outside
    # ``files`` or under ``autodev_destination`` so future
    # manifest shapes (with destinations declared at other
    # nesting levels) cannot cause spurious "no manifest entry"
    # errors. If a non-``files`` record lacks a sha256/size we
    # skip it — the controlled_changed comparison will catch
    # the missing entry by the controlled_set filter.
    for dest in _controlled_destinations_from_manifest(manifest_data):
        if dest in manifest_index:
            continue
        manifest_index[dest] = (None, None)

    for dest in controlled_changed:
        sha_rec, size_rec = manifest_index.get(dest, (None, None))
        if sha_rec is None:
            errors.append(
                f"controlled destination '{dest}' changed "
                f"in commit {outgoing_head[:12]}... but no "
                "manifest entry exists for it"
            )
            continue
        try:
            show = subprocess.run(
                ["git", "-C", str(repo_root), "show",
                 f"{outgoing_head}:{dest}"],
                capture_output=True, timeout=15,
            )
        except subprocess.SubprocessError as e:
            # Round-1064 P2: TimeoutExpired / OSError must not
            # escape the validator. Surface as a per-destination
            # error; the caller's caller blocks the push on any
            # non-empty errors list.
            errors.append(
                f"git show subprocess error for {dest} at "
                f"{outgoing_head[:12]}...: {e!r}"
            )
            continue
        if show.returncode != 0:
            errors.append(
                f"git show failed for {dest} at "
                f"{outgoing_head[:12]}...: "
                f"{show.stderr.decode()[:200]!r}"
            )
            continue
        actual_bytes = show.stdout
        actual_sha = hashlib.sha256(actual_bytes).hexdigest()
        actual_size = len(actual_bytes)
        if actual_sha != sha_rec:
            errors.append(
                f"controlled destination '{dest}' "
                f"recorded sha256={sha_rec[:16]}... but "
                f"committed bytes sha256={actual_sha[:16]}... "
                f"at {outgoing_head[:12]}..."
            )
        if size_rec is not None and actual_size != size_rec:
            errors.append(
                f"controlled destination '{dest}' "
                f"recorded size_bytes={size_rec} but "
                f"committed size_bytes={actual_size} at "
                f"{outgoing_head[:12]}..."
            )

    return (len(errors) == 0, errors)


def manifest_path_for(repo_root: Path) -> Path:
    """Return the canonical on-disk path to the extraction
    manifest. Kept for backwards-compatibility with the
    ``enumerate_controlled_destinations_strict`` helper from
    ``provenance_maintenance`` (which accepts iterable
    manifest paths and reads them from disk).

    Round-817 (P1 repair): the validator NO LONGER calls
    this function. Reading the manifest from disk leaks
    mutable working-tree state into the validator's
    controlled-set derivation and lets a worker
    self-exempt by editing the manifest on disk without
    committing those edits. The validator now derives
    its controlled_set from the committed outgoing and
    prelaunch manifests via
    ``_controlled_destinations_from_manifest``.
    """
    return Path(repo_root).resolve() / _MANIFEST_RELPATH


def _controlled_destinations_from_manifest(manifest_obj) -> list:
    """Walk a parsed manifest object and yield every destination
    path it enumerates.

    Round-817 (P1 repair): the validator MUST NOT depend on
    the on-disk manifest bytes. The canonical helper
    ``enumerate_controlled_destinations_strict`` requires a
    Path on disk, which leaks mutable working-tree state
    into the validator's controlled-set derivation. This
    helper operates on the *already-parsed* committed manifest
    so the validator stays deterministic and works against
    ``git show <sha>:path`` bytes only.

    The extraction semantics mirror ``provenance_maintenance.
    _iter_records`` so the validator and the rest of the
    supervisor agree on what "controlled" means. If the
    manifest is not a dict (the file was decoded but has the
    wrong shape), the function returns an empty list rather
    than raising — the caller is responsible for the
    fail-closed empty-set handling because that is a
    validator-level policy, not a parsing-level concern.
    """
    if not isinstance(manifest_obj, dict):
        return []
    out: list = []
    def _walk(obj, parents):
        if isinstance(obj, dict):
            dest = (
                obj.get("autodev_destination")
                if isinstance(obj.get("autodev_destination"), str)
                else (
                    obj.get("destination_path")
                    if isinstance(obj.get("destination_path"), str)
                    else None
                )
            )
            if dest is not None:
                out.append(dest)
                return
            for k, v in obj.items():
                _walk(v, parents + [k])
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _walk(item, parents + [str(i)])
    _walk(manifest_obj, [])
    return out


# ---------------------------------------------------------------------------
# Worktree path generation (round-167/P2 helper)
# ---------------------------------------------------------------------------
def _push_gate_wt_dir(repo_root: Path, outgoing_head: str) -> Path:
    """Build the per-invocation throwaway worktree path.

    Round-167/CodeRabbit-P2: include a per-invocation
    discriminator (``uuid.uuid4().hex``) so two concurrent
    invocations on the same outgoing SHA do NOT collide.
    ``uuid.uuid4()`` is preferred over
    ``os.getpid() + uuid.getnode()`` because the latter is
    machine-stable and two concurrent invocations in the
    SAME process still collide on it.

    Exposed at module scope so the uniqueness invariant can
    be unit-tested without spinning up a real git worktree.
    """
    return (
        repo_root / ".git"
        / f"push_gate_wt_{outgoing_head[:12]}_{uuid.uuid4().hex}"
    )


# ---------------------------------------------------------------------------
# validate_committed_state_scan_at_sha
# ---------------------------------------------------------------------------
def validate_committed_state_scan_at_sha(
    *,
    outgoing_head: str,
    repo_root: Path,
    allow_synthetic_outgoing: bool = False,
) -> tuple:
    """Run the canonical committed-state scanner against the
    committed outgoing tree at ``outgoing_head``.

    Implementation strategy:

      * Create a throwaway detached worktree at
        ``outgoing_head`` so we read the exact committed bytes
        — no working tree mutation.
      * Invoke ``scripts/canonical_scanner.py`` inside that
        worktree. The scanner is the same Python entry point
        the CI ``committed-state-scan`` job invokes, so this
        function inherits the policy verbatim.
      * Capture exit code: ``0`` = pass, anything else = fail.
      * Capture stdout/stderr for the diagnostic.

    Returns ``(ok: bool, errors: list[str])``.

    ``allow_synthetic_outgoing`` defaults to ``False`` so the
    production pre-push hook fails closed for an unknown
    outgoing SHA. Round-167/CodeRabbit-P2: the scanner half of
    the gate previously bypassed this check, while the
    provenance half already gated on
    ``allow_synthetic_outgoing``. The asymmetry let a
    well-formed but unknown 40-hex SHA skip the scanner
    silently. The CLI passes ``False``; the param is
    propagated for symmetry with the provenance validator.
    """
    if not _HEX_SHA_RE.match(outgoing_head or ""):
        return (False, [f"invalid outgoing_head: {outgoing_head!r}"])
    repo_root = Path(repo_root).resolve()

    try:
        cat = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "-t", outgoing_head],
            capture_output=True, timeout=10,
        )
    except subprocess.SubprocessError as e:
        return (False, [
            f"git cat-file subprocess error for "
            f"{outgoing_head[:12]}...: {e!r}"
        ])
    if cat.returncode != 0:
        if not allow_synthetic_outgoing:
            # Round-167/CodeRabbit-P2: fail closed by default
            # (same policy as the provenance validator). A
            # well-formed but unknown 40-hex SHA must not skip
            # the committed-state scan. Production callers
            # pass ``False``; only opt-in tests / supervisors
            # pass ``True`` (none today, mirroring the
            # provenance validator's contract).
            return (False, [
                f"outgoing_head {outgoing_head[:12]}... is not a "
                f"real Git object in {repo_root}; committed-state "
                "scan cannot run against an unresolvable SHA"
            ])
        # Opt-in synthetic tolerance: tests construct synthetic
        # ``outgoing_head`` values which are not Git objects.
        # Skip the scanner (no committed tree to scan) and
        # return the legacy ``(True, [])`` so the supervisor's
        # classification does not regress for synthetic
        # fixtures.
        return (True, [])

    # Throwaway worktree at the exact committed outgoing tip.
    # The discriminator is generated by ``_push_gate_wt_dir``
    # (round-167/CodeRabbit-P2): two concurrent invocations
    # on the same outgoing SHA MUST NOT collide. The cleanup
    # ``finally`` removes only the directory this invocation
    # created.
    wt_dir = _push_gate_wt_dir(repo_root, outgoing_head)
    # Best-effort cleanup; previous invocations may have left
    # a stale worktree behind.
    try:
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "remove",
             "--force", str(wt_dir)],
            capture_output=True, timeout=15,
        )
    except subprocess.SubprocessError:
        # Cleanup is best-effort; any failure here is logged
        # by the follow-up ``worktree add`` attempt if it
        # actually matters. Round-1064 P2: do not let a slow
        # ``worktree remove`` kill the validator.
        pass
    try:
        try:
            add = subprocess.run(
                ["git", "-C", str(repo_root), "worktree", "add",
                 "--detach", str(wt_dir), outgoing_head],
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.SubprocessError as e:
            return (False, [
                f"git worktree add subprocess error for "
                f"{outgoing_head[:12]}...: {e!r}"
            ])
        if add.returncode != 0:
            return (False, [
                f"failed to create detached worktree at "
                f"{outgoing_head[:12]}...: "
                f"{add.stderr[:200]!r}"
            ])

        # Run the canonical scanner against the committed tree.
        try:
            scanner = subprocess.run(
                [sys.executable, "scripts/canonical_scanner.py"],
                cwd=str(wt_dir), capture_output=True, text=True, timeout=120,
            )
        except subprocess.SubprocessError as e:
            return (False, [
                f"canonical_scanner subprocess error for "
                f"{outgoing_head[:12]}...: {e!r}"
            ])
        if scanner.returncode != 0:
            return (False, [
                "canonical committed-state scanner rejected the "
                f"outgoing tree at {outgoing_head[:12]}...: "
                f"exit={scanner.returncode} "
                f"stdout[:300]={scanner.stdout[:300]!r} "
                f"stderr[:200]={scanner.stderr[:200]!r}"
            ])
        return (True, [])
    finally:
        try:
            subprocess.run(
                ["git", "-C", str(repo_root), "worktree", "remove",
                 "--force", str(wt_dir)],
                capture_output=True, timeout=15,
            )
        except subprocess.SubprocessError:
            # Round-1064 P2: best-effort cleanup. A leftover
            # ``.git/push_gate_wt`` is recovered by the next
            # invocation's pre-cleanup pass.
            pass


# ---------------------------------------------------------------------------
# CLI entry point — consumed by source-controlled worker pre-push hook
# ---------------------------------------------------------------------------
def _run_validators(args: argparse.Namespace) -> int:
    checks = set(args.check or ["provenance", "scanner"])
    repo_root = Path(args.repo_root).resolve()
    pre = args.prelaunch
    out = args.outgoing

    payload = {
        "prelaunch": pre,
        "outgoing": out,
        "repo_root": str(repo_root),
        "checks": [],
        "ok": True,
    }

    for check in ("provenance", "scanner"):
        if check not in checks:
            continue
        if check == "provenance":
            ok, errors = validate_provenance_consistency_at_sha(
                prelaunch_head=pre, outgoing_head=out, repo_root=repo_root,
            )
        else:
            # Round-167/CodeRabbit-P2: the scanner validator
            # now takes ``allow_synthetic_outgoing`` for
            # symmetry with the provenance validator. The
            # CLI default is fail-closed (False); no current
            # caller passes True, mirroring the provenance
            # validator's CLI surface (which also has the
            # param but no CLI flag).
            ok, errors = validate_committed_state_scan_at_sha(
                outgoing_head=out, repo_root=repo_root,
                allow_synthetic_outgoing=False,
            )
        payload["checks"].append({
            "name": check,
            "ok": ok,
            "errors": errors,
        })
        if not ok:
            payload["ok"] = False

    # The hook cares only about the exit code:
    #   0   – push may proceed
    #   2   – push must be blocked (deterministic validation failure)
    #   1   – hook error (bad args etc.)
    print(json.dumps(payload))
    return 0 if payload["ok"] else 2


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Canonical AED worker pre-push gate. Reads "
            "committed bytes by exact outgoing SHA and "
            "deterministically blocks the push when the "
            "supervisor's validator or the canonical "
            "committed-state scanner would reject the tree."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    val = sub.add_parser("validate", help=(
        "Run the gate against an outgoing SHA. Exit 0 if "
        "the push should proceed; exit 2 if the push must "
        "be blocked."))
    val.add_argument("--repo-root", required=True)
    val.add_argument("--prelaunch", required=True)
    val.add_argument("--outgoing", required=True)
    val.add_argument(
        "--check", action="append", choices=["provenance", "scanner"],
        help=(
            "Which check to run. May be passed twice to "
            "request both. Default: both."
        ),
    )
    val.set_defaults(func=_run_validators)

    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as e:
        # argparse fired its own error path; surface it as
        # an operator-style hard error (exit 1) so the hook
        # never silently drops the validator.
        return 1 if e.code in (None, 2) else int(e.code or 1)
    try:
        return int(args.func(args))
    except subprocess.SubprocessError as e:
        # Round-1064 P2: the module docstring documents exit 3
        # for subprocess timeout / internal-inconsistency. Surface
        # it through the CLI so the supervisor and the pre-push
        # hook can distinguish an operator-style hard error (1)
        # from a validator-side subprocess failure (3).
        print(json.dumps({
            "ok": False, "internal_error": repr(e),
            "subprocess_error": True,
        }))
        return 3
    except Exception as e:
        # Operator-style hard error (bad args etc.).
        print(json.dumps({"ok": False, "internal_error": repr(e)}))
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
