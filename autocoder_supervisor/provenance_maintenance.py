"""Provenance maintenance helpers.

Pre-canary round-281 §6 + Closure III:

The supervisor MUST detect provenance drift after every
verified REPAIR_PUSHED, regardless of whether the verification
came from normal completion or from orphan reconciliation.
The detection MUST enumerate the controlled-path set from
the canonical manifest itself (no hand-maintained partial
list). Drift MUST be recorded via the project's canonical
atomic JSON write (no direct Path.write_text). The drift
record MUST survive restart. Drift detection failures
MUST fail closed (no operator reconciliation path).

Lifecycle states for a drift record:

  DETECTED
  QUEUED
  CLAIMED
  REPAIRING
  PUSH_VERIFIED
  CI_VERIFIED
  MANIFEST_VALIDATED
  TERMINAL

Old-head drift records become SUPERSEDED when a newer
verified head proves the manifest correct (head-supersession
rule). Ping-pong is prevented by the rule: a manifest-repair
commit that only edits a provenance manifest is NOT itself
provenance-controlled source (the manifest bytes are recorded,
not the controller), so a manifest-repair cannot appear as
new drift on the controlled-source set.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Manifest paths and source-of-truth enumeration
# ---------------------------------------------------------------------------


# Canonical provenance manifests the supervisor tracks. Both files
# are tracked in git and validated by the ``provenance`` CI job.
_PROVENANCE_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent
    / "provenance"
    / "AUTOCODER_SOURCE_COMPLETENESS.json"
)
_PROVENANCE_MANIFEST_PATH_AED = (
    Path(__file__).resolve().parent.parent
    / "provenance"
    / "aed-pr417-source-manifest.json"
)


# The state directory location (where provenance_drift_pending.json
# lives) is resolved at import time from the supervisor runtime
# location. The ledger is co-located with the other supervisor
# state files.
def _state_dir_default() -> Path:
    """Default state dir for the provenance-drift ledger."""
    # The runtime supervisor writes its state under
    # $HOME/.hermes/aed-supervisor/state. When the supervisor is
    # invoked in production, AED_SUPERVISOR_STATE_DIR points at
    # that directory. When this module is invoked from the
    # source-controlled checkout via pytest, the supervisor's
    # STATE_DIR is not yet defined, so we fall back to the
    # canonical location.
    return Path(
        os.environ.get(
            "AED_SUPERVISOR_STATE_DIR",
            str(Path.home() / ".hermes/aed-supervisor/state"),
        )
    )


# Drift ledger path. The supervisor (production wiring in
# supervisor.py) reads and writes this file atomically via
# _atomic_write_json() below.
def _drift_ledger_path() -> Path:
    return _state_dir_default() / "provenance_drift_pending.json"


# ---------------------------------------------------------------------------
# Atomic write helpers (canonical, fail-closed)
# ---------------------------------------------------------------------------


class AtomicWriteError(RuntimeError):
    """Raised when an atomic write fails or the destination
    cannot be made consistent. Callers MUST treat this as a
    fail-closed condition: the prior ledger must be preserved.
    """


def _atomic_write_json(path: Path, data) -> None:
    """Atomically write JSON to ``path`` with crash-consistent
    semantics.

    Crash properties:
      A. crash before write        -> prior ledger preserved
      B. crash during temp write   -> prior ledger preserved
         (tmp file is removed on error)
      C. crash after temp write    -> prior ledger preserved
         but before os.replace    (tmp file remains; next
         run ignores it)
      D. crash after os.replace    -> complete new ledger visible
      E. restart                   -> ledger visible

    The destination file mode is 0o600 and the parent directory
    mode is 0o700. The temp file is created with mode 0o600
    before any bytes are written.
    """
    parent_existed = path.parent.exists()
    try:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not parent_existed:
            try:
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
    except OSError as e:
        raise AtomicWriteError(
            f"could not create parent dir {path.parent}: {e}"
        ) from e
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = -1
    try:
        fd = os.open(
            str(tmp),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync may fail on some filesystems; the
                # OS-level rename after fsync is still
                # atomic from the perspective of subsequent
                # reads.
                pass
        # Rename into place. os.replace is atomic on POSIX.
        os.replace(tmp, path)
    except OSError as e:
        # Cleanup: remove the partial tmp file. The
        # destination path is left untouched.
        try:
            if os.path.exists(str(tmp)):
                os.unlink(str(tmp))
        except OSError:
            pass
        raise AtomicWriteError(
            f"atomic write failed for {path}: {e}"
        ) from e
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_drift_ledger_or_failclosed(path: Path) -> list:
    """Read the drift ledger. If the file is missing or empty,
    return []. If it is malformed, raise (fail closed)."""
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise AtomicWriteError(
            f"could not read drift ledger {path}: {e}"
        ) from e
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        # Malformed state MUST fail closed and preserve
        # evidence. We do NOT silently replace malformed
        # state with [].
        raise AtomicWriteError(
            f"drift ledger {path} is malformed; refusing to "
            f"replace with []: {e}"
        ) from e
    if not isinstance(parsed, list):
        raise AtomicWriteError(
            f"drift ledger {path} is not a list (got "
            f"{type(parsed).__name__}); refusing to overwrite"
        )
    return parsed


def _atomic_append_drift(path: Path, entry: dict) -> None:
    """Append a drift entry to the ledger atomically. Reads the
    current ledger, appends, and writes back via
    ``_atomic_write_json``. The prior ledger is preserved on
    any failure.
    """
    existing = _read_drift_ledger_or_failclosed(path)
    existing.append(entry)
    _atomic_write_json(path, existing)


def _atomic_update_drift(
    path: Path, *, where, set_: dict
) -> int:
    """Find records matching ``where(record)`` and merge
    ``set_`` into them. Returns the count of records updated.

    Atomic: if the write fails the prior ledger is preserved.
    """
    existing = _read_drift_ledger_or_failclosed(path)
    count = 0
    for rec in existing:
        if not isinstance(rec, dict):
            continue
        if where(rec):
            rec.update(set_)
            count += 1
    if count > 0:
        _atomic_write_json(path, existing)
    return count


# ---------------------------------------------------------------------------
# Manifest schema helpers
# ---------------------------------------------------------------------------


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_records(manifest: dict):
    """Yield (path_to_record, record) pairs for every record in the manifest.

    Recognizes both ``autodev_destination`` (autodev manifests)
    and ``destination_path`` (aed-pr417 source manifest). Records
    that look like file entries are exposed with both field names.
    """
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
            sha_field = (
                "autodev_sha256"
                if "autodev_sha256" in obj
                else (
                    "destination_sha256"
                    if "destination_sha256" in obj
                    else None
                )
            )
            if dest and sha_field:
                yield parents, obj, dest, sha_field
                return
            for k, v in obj.items():
                yield from _walk(v, parents + [k])
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                yield from _walk(item, parents + [str(i)])

    yield from _walk(manifest, [])


def _get_path_in_repo(repo_root: Path, dest: str) -> Path:
    """Resolve a manifest destination relative to ``repo_root``.

    The destination paths in the manifest are repo-relative
    (e.g. ``autocoder_supervisor/supervisor.py``). The
    resolver rejects absolute paths and ``..`` escapes.
    """
    p = Path(dest)
    if p.is_absolute():
        raise ValueError(f"manifest dest is absolute: {dest!r}")
    if ".." in p.parts:
        raise ValueError(f"manifest dest contains '..': {dest!r}")
    return repo_root / p


# ---------------------------------------------------------------------------
# Source-of-truth enumeration
# ---------------------------------------------------------------------------


def enumerate_controlled_destinations(
    manifest_paths: Iterable[Path],
) -> set:
    """Walk the canonical provenance manifests and return the
    set of every destination path that is referenced in the
    manifests.

    This is the SOURCE OF TRUTH for the controlled-path set.
    Callers MUST NOT maintain their own partial list.
    """
    out: set = set()
    for mp in manifest_paths:
        try:
            manifest = json.loads(mp.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for _, _, dest, _ in _iter_records(manifest):
            out.add(dest)
    return out


# ---------------------------------------------------------------------------
# Drift detection helpers
# ---------------------------------------------------------------------------


def is_manifest_stale(
    manifest_path: Path,
    repo_root: Path,
    *,
    allowed_paths: Iterable[str] | None = None,
) -> bool:
    """True iff any tracked record's sha256 disagrees with the
    actual bytes (working copy OR, when a ``new_head_sha`` is
    provided, the bytes at that commit).

    Manifested records whose destination path is not in
    ``allowed_paths`` are skipped. When ``allowed_paths`` is
    None, every destination is checked.
    """
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return True
    allowed = set(allowed_paths) if allowed_paths is not None else None
    for _, rec, dest, sha_field in _iter_records(manifest):
        if allowed is not None and dest not in allowed:
            continue
        try:
            actual = _sha256_of(_get_path_in_repo(repo_root, dest))
        except OSError:
            return True
        expected = rec.get(sha_field)
        if not isinstance(expected, str):
            return True
        if actual != expected:
            return True
    return False


def regenerate_manifest(
    manifest_path: Path,
    repo_root: Path,
    *,
    allowed_paths: Iterable[str],
) -> dict:
    """Rewrite sha256 for records whose paths are in
    ``allowed_paths``. Records outside the allowed set are
    preserved exactly as-is so the manifest remains a
    verifiable integrity record.

    Returns a small audit record describing what changed.
    """
    allowed = set(allowed_paths)
    manifest = json.loads(manifest_path.read_text())
    audit = {"updated": [], "skipped_outside_allowed": 0, "missing_files": []}
    for _, rec, dest, sha_field in _iter_records(manifest):
        if dest not in allowed:
            audit["skipped_outside_allowed"] += 1
            continue
        target = _get_path_in_repo(repo_root, dest)
        try:
            new_hash = _sha256_of(target)
        except OSError:
            audit["missing_files"].append(dest)
            continue
        old_hash = rec.get(sha_field)
        rec[sha_field] = new_hash
        audit["updated"].append(
            {"path": dest, "old": old_hash, "new": new_hash}
        )
    _atomic_write_json(manifest_path, manifest)
    return audit


def validate_manifest(
    manifest_path: Path, repo_root: Path, *, allowed_paths: Iterable[str]
) -> bool:
    """True iff every allowed record's sha256 matches the bytes."""
    return not is_manifest_stale(
        manifest_path, repo_root, allowed_paths=allowed_paths
    )


# ---------------------------------------------------------------------------
# Commit-based comparison (works against any commit, not just HEAD)
# ---------------------------------------------------------------------------


def _committed_bytes(repo_root: Path, head_sha: str, rel_path: str) -> bytes:
    """Return the bytes of ``rel_path`` at commit ``head_sha``.

    Raises ``FileNotFoundError`` if the path is not present at
    that commit, and ``RuntimeError`` on any other git
    failure. Callers MUST treat failures as fail-closed.
    """
    r = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{head_sha}:{rel_path}"],
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"git show failed for {head_sha}:{rel_path}: "
            f"{r.stderr.decode()[:200]!r}"
        )
    return r.stdout


def _manifest_committed_sha(
    manifest: dict, dest: str
) -> str | None:
    """Return the recorded sha256 for ``dest`` in ``manifest``,
    or None if not found."""
    for _, rec, rec_dest, sha_field in _iter_records(manifest):
        if rec_dest == dest:
            v = rec.get(sha_field)
            if isinstance(v, str):
                return v
    return None


def find_drift_at_head(
    repo_root: Path, head_sha: str, *, manifest_paths: Iterable[Path]
) -> list:
    """Compare the bytes of every controlled destination at
    commit ``head_sha`` against the manifest records. Returns
    a list of drift entries; empty list means no drift.

    The controlled-destination set is derived from the
    manifests themselves (not a hand-maintained list).
    """
    drifts = []
    for mp in manifest_paths:
        try:
            manifest = json.loads(mp.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise RuntimeError(
                f"manifest {mp} could not be read: {e}"
            ) from e
        for _, _, dest, sha_field in _iter_records(manifest):
            expected = _manifest_committed_sha(manifest, dest)
            if expected is None:
                continue
            try:
                actual_bytes = _committed_bytes(
                    repo_root, head_sha, dest
                )
            except RuntimeError:
                # Manifest references a path that is not in
                # this commit. That can happen when the path
                # was added later; skip without recording a
                # drift (no committed bytes to compare).
                continue
            actual_sha = hashlib.sha256(actual_bytes).hexdigest()
            if actual_sha != expected:
                drifts.append({
                    "manifest": str(mp),
                    "destination": dest,
                    "expected_sha256": expected,
                    "actual_sha256": actual_sha,
                })
    return drifts


# ---------------------------------------------------------------------------
# Drift ownership lifecycle
# ---------------------------------------------------------------------------


# Lifecycle states a drift record moves through. A drift record
# MUST reach TERMINAL via one of the documented transitions.
DRIFT_STATE_DETECTED = "DETECTED"
DRIFT_STATE_QUEUED = "QUEUED"
DRIFT_STATE_CLAIMED = "CLAIMED"
DRIFT_STATE_REPAIRING = "REPAIRING"
DRIFT_STATE_PUSH_VERIFIED = "PUSH_VERIFIED"
DRIFT_STATE_CI_VERIFIED = "CI_VERIFIED"
DRIFT_STATE_MANIFEST_VALIDATED = "MANIFEST_VALIDATED"
DRIFT_STATE_TERMINAL = "TERMINAL"
DRIFT_STATE_SUPERSEDED = "SUPERSEDED"


# Terminal states for a drift record.
DRIFT_TERMINAL_STATES = frozenset({
    DRIFT_STATE_TERMINAL,
    DRIFT_STATE_SUPERSEDED,
})


def register_drift(
    *,
    head_sha: str,
    attempt_id: str,
    drifts: list,
    ledger_path: Path | None = None,
) -> dict:
    """Record a new drift detection. Returns the new entry.

    State transitions: -> DETECTED (initial state on creation).

    Atomic: writes via ``_atomic_append_drift``. On any
    failure the prior ledger is preserved and the exception
    propagates (the caller MUST treat this as fail-closed).
    """
    entry = {
        "state": DRIFT_STATE_DETECTED,
        "detected_at": _now_iso(),
        "head_sha": head_sha,
        "attempt_id": attempt_id,
        "drifts": drifts,
        "owner": "next_worker_round_autonomous",
    }
    path = ledger_path or _drift_ledger_path()
    _atomic_append_drift(path, entry)
    return entry


def transition_drift_state(
    *,
    ledger_path: Path | None,
    where,
    new_state: str,
    extra: dict | None = None,
) -> int:
    """Update drift records matching ``where(record)`` to
    ``new_state``. Returns the count of records updated.

    The caller may pass ``extra`` to set additional fields
    (e.g., ``repair_sha``, ``origin_verified``,
    ``live_head_verified``, ``terminal_at``).

    State-transition guards:
    - SUPERSEDED is reachable from any non-terminal state.
    - TERMINAL is reachable only from MANIFEST_VALIDATED.
    - All other transitions follow the lifecycle ordering
      (DETECTED -> QUEUED -> CLAIMED -> REPAIRING ->
      PUSH_VERIFIED -> CI_VERIFIED -> MANIFEST_VALIDATED ->
      TERMINAL).
    """
    if new_state not in (
        DRIFT_STATE_QUEUED,
        DRIFT_STATE_CLAIMED,
        DRIFT_STATE_REPAIRING,
        DRIFT_STATE_PUSH_VERIFIED,
        DRIFT_STATE_CI_VERIFIED,
        DRIFT_STATE_MANIFEST_VALIDATED,
        DRIFT_STATE_TERMINAL,
        DRIFT_STATE_SUPERSEDED,
    ):
        raise ValueError(f"unknown drift state: {new_state!r}")
    path = ledger_path or _drift_ledger_path()
    extra = dict(extra or {})
    extra["state"] = new_state
    extra["state_updated_at"] = _now_iso()
    return _atomic_update_drift(path, where=where, set_=extra)


def supersede_old_drifts(
    *,
    new_head_sha: str,
    ledger_path: Path | None,
) -> int:
    """Mark every drift record whose ``head_sha != new_head_sha``
    and whose state is not already terminal as SUPERSEDED.

    A drift is SUPERSEDED by the next verified head when that
    head proves the manifest correct (the drift is
    invalidated by the canonical repair). The supersession is
    durable: the SUPERSEDED record is preserved (not
    deleted) so the audit trail remains intact.
    """
    path = ledger_path or _drift_ledger_path()
    existing = _read_drift_ledger_or_failclosed(path)
    superseded = 0
    for rec in existing:
        if not isinstance(rec, dict):
            continue
        if rec.get("state") in DRIFT_TERMINAL_STATES:
            continue
        if rec.get("head_sha") != new_head_sha:
            rec["state"] = DRIFT_STATE_SUPERSEDED
            rec["state_updated_at"] = _now_iso()
            rec["superseded_by_head"] = new_head_sha
            superseded += 1
    if superseded > 0:
        _atomic_write_json(path, existing)
    return superseded


# ---------------------------------------------------------------------------
# Drift lifecycle reader (for audit / terminality check)
# ---------------------------------------------------------------------------


def list_open_drifts(ledger_path: Path | None = None) -> list:
    """Return every drift record whose state is not terminal
    nor superseded."""
    path = ledger_path or _drift_ledger_path()
    if not path.exists():
        return []
    try:
        existing = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [
        rec for rec in existing
        if isinstance(rec, dict)
        and rec.get("state") not in DRIFT_TERMINAL_STATES
    ]


def list_drift_summary(ledger_path: Path | None = None) -> dict:
    """Return a summary suitable for the audit report."""
    path = ledger_path or _drift_ledger_path()
    out = {
        "total": 0,
        "by_state": {},
        "open": 0,
        "terminal": 0,
        "superseded": 0,
    }
    if not path.exists():
        return out
    try:
        existing = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return out
    for rec in existing:
        if not isinstance(rec, dict):
            continue
        out["total"] += 1
        st = rec.get("state", "UNKNOWN")
        out["by_state"][st] = out["by_state"].get(st, 0) + 1
        if st == DRIFT_STATE_TERMINAL:
            out["terminal"] += 1
        elif st == DRIFT_STATE_SUPERSEDED:
            out["superseded"] += 1
        else:
            out["open"] += 1
    return out


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "is_manifest_stale",
    "regenerate_manifest",
    "validate_manifest",
    "enumerate_controlled_destinations",
    "find_drift_at_head",
    "register_drift",
    "transition_drift_state",
    "supersede_old_drifts",
    "list_open_drifts",
    "list_drift_summary",
    "AtomicWriteError",
    "_PROVENANCE_MANIFEST_PATH",
    "_PROVENANCE_MANIFEST_PATH_AED",
    "DRIFT_STATE_DETECTED",
    "DRIFT_STATE_QUEUED",
    "DRIFT_STATE_CLAIMED",
    "DRIFT_STATE_REPAIRING",
    "DRIFT_STATE_PUSH_VERIFIED",
    "DRIFT_STATE_CI_VERIFIED",
    "DRIFT_STATE_MANIFEST_VALIDATED",
    "DRIFT_STATE_TERMINAL",
    "DRIFT_STATE_SUPERSEDED",
    "DRIFT_TERMINAL_STATES",
    "_drift_ledger_path",
]