"""Provenance maintenance helpers.

Closure III + IV:

The supervisor MUST detect provenance drift after every verified
REPAIR_PUSHED, regardless of whether the verification came from
normal completion or orphan reconciliation. Drift MUST be detected
fail-closed: a missing or unreadable controlled-source file is a
DRIFT (not a silent skip), and a missing manifest is a
PROVENANCE_CHECK_ERROR (not a partial enumeration).

The supervisor MUST NOT commit; it only registers durable work
for the next worker round.

Manifest path resolution:
The source of truth for the canonical working-checkout is the
``REPO_DIR`` explicit parameter passed by the supervisor. The
module-level ``__file__`` is NOT used because the runtime
deployment lives under ``~/.hermes/aed-supervisor/`` while the
canonical manifests live in the source-controlled checkout.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Iterable, Optional, Sequence


# ---------------------------------------------------------------------------
# Head-advance state model (Closure IV §2)
# ---------------------------------------------------------------------------


# State values for the canonical head-advance classifier. These
# are intentionally NOT Optional[str] — the type system enforces
# that the operator's "NONE / Provenance failed" conflation is
# impossible.
HEAD_ADVANCE_UNRELATED = "HEAD_ADVANCE_UNRELATED"
HEAD_ADVANCE_WORKER_PROVENANCE_VERIFIED = "HEAD_ADVANCE_WORKER_PROVENANCE_VERIFIED"
HEAD_ADVANCE_WORKER_PROVENANCE_BLOCKED = "HEAD_ADVANCE_WORKER_PROVENANCE_BLOCKED"
HEAD_ADVANCE_WORKER_PUSH_INVALID = "HEAD_ADVANCE_WORKER_PUSH_INVALID"

HEAD_ADVANCE_STATES: frozenset[str] = frozenset({
    HEAD_ADVANCE_UNRELATED,
    HEAD_ADVANCE_WORKER_PROVENANCE_VERIFIED,
    HEAD_ADVANCE_WORKER_PROVENANCE_BLOCKED,
    HEAD_ADVANCE_WORKER_PUSH_INVALID,
})


# States that DO NOT enter QUALIFYING_READINESS.
HEAD_ADVANCE_STATES_THAT_BLOCK_QUALIFYING: frozenset[str] = frozenset({
    HEAD_ADVANCE_UNRELATED,
    HEAD_ADVANCE_WORKER_PROVENANCE_BLOCKED,
    HEAD_ADVANCE_WORKER_PUSH_INVALID,
})


class HeadAdvanceResult:
    """Typed head-advance classification.

    Carries the classification PLUS the preserved worker
    identity fields so a PROVENANCE_BLOCKED outcome never
    silently discards the attempt identity.
    """
    __slots__ = (
        "state",
        "attempt_id",
        "claim_id",
        "result_contract_id",
        "produced_sha",
        "pushed_sha",
        "origin_head_verified",
        "github_head_verified",
        "provenance_status",
        "provenance_error",
    )

    def __init__(
        self,
        *,
        state: str,
        attempt_id: Optional[str] = None,
        claim_id: Optional[str] = None,
        result_contract_id: Optional[str] = None,
        produced_sha: Optional[str] = None,
        pushed_sha: Optional[str] = None,
        origin_head_verified: bool = False,
        github_head_verified: bool = False,
        provenance_status: str = "OK",
        provenance_error: Optional[str] = None,
    ):
        if state not in HEAD_ADVANCE_STATES:
            raise ValueError(f"unknown head-advance state: {state!r}")
        self.state = state
        self.attempt_id = attempt_id
        self.claim_id = claim_id
        self.result_contract_id = result_contract_id
        self.produced_sha = produced_sha
        self.pushed_sha = pushed_sha
        self.origin_head_verified = origin_head_verified
        self.github_head_verified = github_head_verified
        self.provenance_status = provenance_status
        self.provenance_error = provenance_error

    @property
    def blocks_qualifying(self) -> bool:
        return self.state in HEAD_ADVANCE_STATES_THAT_BLOCK_QUALIFYING

    @property
    def is_worker_push(self) -> bool:
        return self.state in (
            HEAD_ADVANCE_WORKER_PROVENANCE_VERIFIED,
            HEAD_ADVANCE_WORKER_PROVENANCE_BLOCKED,
        )

    @property
    def is_provenance_verified(self) -> bool:
        return self.state == HEAD_ADVANCE_WORKER_PROVENANCE_VERIFIED


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
                pass
        os.replace(tmp, path)
    except OSError as e:
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
    existing = _read_drift_ledger_or_failclosed(path)
    existing.append(entry)
    _atomic_write_json(path, existing)


def _atomic_update_drift(path: Path, *, where, set_: dict) -> int:
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
    """Yield (path_to_record, record, dest, sha_field) tuples."""
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
    p = Path(dest)
    if p.is_absolute():
        raise ValueError(f"manifest dest is absolute: {dest!r}")
    if ".." in p.parts:
        raise ValueError(f"manifest dest contains '..': {dest!r}")
    return repo_root / p


# ---------------------------------------------------------------------------
# Source-of-truth enumeration (Closure IV §4 — fail closed)
# ---------------------------------------------------------------------------


class ManifestEnumerationError(RuntimeError):
    """Raised when a manifest cannot be read or decoded during
    controlled-source enumeration. The supervisor MUST treat
    this as fail-closed: an acceptance-critical source-of-truth
    manifest cannot disappear and produce a smaller controlled
    set.
    """


def enumerate_controlled_destinations_strict(
    manifest_paths: Iterable[Path],
) -> set:
    """Walk every canonical provenance manifest and return the
    set of every destination path. Fails closed on any read or
    decode failure: missing manifest, unreadable manifest, or
    malformed JSON. The supervisor MUST use this helper on the
    production acceptance path.
    """
    out: set = set()
    for mp in manifest_paths:
        if not mp.exists():
            raise ManifestEnumerationError(
                f"required manifest does not exist: {mp}"
            )
        try:
            manifest = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ManifestEnumerationError(
                f"manifest {mp} cannot be parsed: {e}"
            ) from e
        for _, _, dest, _ in _iter_records(manifest):
            out.add(dest)
    if not out:
        raise ManifestEnumerationError(
            "controlled-destination enumeration produced an "
            "empty set; manifests have no records or are "
            "wrong-format"
        )
    return out


def enumerate_controlled_destinations(
    manifest_paths: Iterable[Path],
) -> set:
    """Diagnostic helper. Production callers MUST use
    enumerate_controlled_destinations_strict instead. This
    helper silently skips unreadable manifests and may
    therefore return a partial set; it exists for
    inspection-only use.
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


class ProvenanceCheckError(RuntimeError):
    """Raised when drift detection cannot complete. The supervisor
    MUST treat this as fail-closed: a missing controlled file
    is a DRIFT (recorded); a git command failure or unreadable
    manifest is a CHECK ERROR (recorded as PROVENANCE_BLOCKED).
    """


def is_manifest_stale(
    manifest_path: Path,
    repo_root: Path,
    *,
    allowed_paths: Optional[Iterable[str]] = None,
) -> bool:
    """True iff any tracked record's sha256 disagrees with bytes."""
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
    preserved exactly as-is.
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
    return not is_manifest_stale(
        manifest_path, repo_root, allowed_paths=allowed_paths
    )


# ---------------------------------------------------------------------------
# Commit-based comparison (Closure IV §3 — fail closed on deletion)
# ---------------------------------------------------------------------------


_HEX_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


def _committed_bytes(repo_root: Path, head_sha: str, rel_path: str) -> bytes:
    """Return the bytes of ``rel_path`` at commit ``head_sha``.

    Raises ``ProvenanceCheckError`` on any failure (the path
    is missing at that commit, or git failed). Callers MUST
    treat this as fail-closed; a missing controlled file is
    DRIFT, not a silent skip.
    """
    if not _HEX_SHA_RE.match(head_sha or ""):
        raise ProvenanceCheckError(
            f"invalid head_sha format: {head_sha!r}"
        )
    r = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{head_sha}:{rel_path}"],
        capture_output=True,
    )
    if r.returncode != 0:
        err = r.stderr.decode()[:200]
        # The path may genuinely have been deleted from the
        # tree at this commit. That is a DELETION DRIFT, not
        # an error. Raise a specific subtype so the caller
        # can distinguish "git-show exit 128 on missing path"
        # from "git-show exit 1 on invalid commit".
        if "exists on disk, not in" in err or "fatal: path" in err or "did not match any files" in err:
            raise FileNotFoundError(
                f"controlled path {rel_path!r} is not present at "
                f"head {head_sha[:12]}..."
            )
        raise ProvenanceCheckError(
            f"git show failed for {head_sha}:{rel_path}: {err!r}"
        )
    return r.stdout


def _manifest_committed_sha(manifest: dict, dest: str) -> Optional[str]:
    for _, rec, rec_dest, sha_field in _iter_records(manifest):
        if rec_dest == dest:
            v = rec.get(sha_field)
            if isinstance(v, str):
                return v
    return None


def find_drift_at_head(
    *,
    repo_root: Path,
    head_sha: str,
    manifest_paths: Iterable[Path],
) -> list:
    """Compare the bytes of every controlled destination at
    commit ``head_sha`` against the manifest records. Returns a
    list of drift entries; empty list means no drift.

    The controlled-destination set is derived from the
    manifests themselves (no hand-maintained partial list).

    CLOSURE IV §3 — fail closed on deletion:
      - If a manifested path is missing at ``head_sha``
        (worker deleted the file), a DRIFT entry is recorded
        with ``actual_sha256 = ""`` and
        ``expected_sha256 = <manifest-recorded-hash>``. This
        is a DELETION drift.
      - If git show fails for any other reason
        (operational failure), a ``ProvenanceCheckError`` is
        raised so the supervisor's fail-closed path
        activates.
      - If a manifest is missing or malformed, a
        ``ProvenanceCheckError`` is raised.
    """
    drifts: list = []
    seen_destinations: set = set()
    for mp in manifest_paths:
        if not mp.exists():
            raise ProvenanceCheckError(
                f"required manifest does not exist: {mp}"
            )
        try:
            manifest = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ProvenanceCheckError(
                f"manifest {mp} cannot be parsed: {e}"
            ) from e
        for _, _, dest, _ in _iter_records(manifest):
            if dest in seen_destinations:
                continue
            seen_destinations.add(dest)
            expected = _manifest_committed_sha(manifest, dest)
            if expected is None:
                continue
            try:
                actual_bytes = _committed_bytes(
                    repo_root, head_sha, dest
                )
            except FileNotFoundError:
                # DEL control path at head — record a
                # deletion drift. The manifest recorded a
                # hash for a file that no longer exists at
                # the current head.
                drifts.append({
                    "manifest": str(mp),
                    "destination": dest,
                    "expected_sha256": expected,
                    "actual_sha256": "",
                    "drift_kind": "DELETION",
                })
                continue
            except ProvenanceCheckError:
                # Operational failure — propagate so the
                # caller fails closed.
                raise
            actual_sha = hashlib.sha256(actual_bytes).hexdigest()
            if actual_sha != expected:
                drifts.append({
                    "manifest": str(mp),
                    "destination": dest,
                    "expected_sha256": expected,
                    "actual_sha256": actual_sha,
                    "drift_kind": "MODIFIED",
                })
    return drifts


# ---------------------------------------------------------------------------
# Drift ownership lifecycle
# ---------------------------------------------------------------------------


DRIFT_STATE_DETECTED = "DETECTED"
DRIFT_STATE_QUEUED = "QUEUED"
DRIFT_STATE_CLAIMED = "CLAIMED"
DRIFT_STATE_REPAIRING = "REPAIRING"
DRIFT_STATE_PUSH_VERIFIED = "PUSH_VERIFIED"
DRIFT_STATE_CI_VERIFIED = "CI_VERIFIED"
DRIFT_STATE_MANIFEST_VALIDATED = "MANIFEST_VALIDATED"
DRIFT_STATE_TERMINAL = "TERMINAL"
DRIFT_STATE_SUPERSEDED = "SUPERSEDED"
DRIFT_STATE_PROVENANCE_BLOCKED = "PROVENANCE_BLOCKED"

DRIFT_TERMINAL_STATES: frozenset[str] = frozenset({
    DRIFT_STATE_TERMINAL,
    DRIFT_STATE_SUPERSEDED,
})


def register_drift(
    *,
    head_sha: str,
    attempt_id: str,
    drifts: list,
    ledger_path: Optional[Path] = None,
) -> dict:
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


def register_provenance_block(
    *,
    head_sha: str,
    attempt_id: str,
    error: str,
    ledger_path: Optional[Path] = None,
) -> dict:
    """Closure IV §2: durably record that a verified worker
    push had its provenance check fail. The attempt identity is
    preserved; the operator-visible readiness gate stays
    FALSE; the next round re-tries.
    """
    entry = {
        "state": DRIFT_STATE_PROVENANCE_BLOCKED,
        "detected_at": _now_iso(),
        "head_sha": head_sha,
        "attempt_id": attempt_id,
        "error": error,
        "owner": "next_worker_round_autonomous",
    }
    path = ledger_path or _drift_ledger_path()
    _atomic_append_drift(path, entry)
    return entry


def transition_drift_state(
    *,
    ledger_path: Optional[Path],
    where,
    new_state: str,
    extra: Optional[dict] = None,
) -> int:
    if new_state not in (
        DRIFT_STATE_QUEUED, DRIFT_STATE_CLAIMED, DRIFT_STATE_REPAIRING,
        DRIFT_STATE_PUSH_VERIFIED, DRIFT_STATE_CI_VERIFIED,
        DRIFT_STATE_MANIFEST_VALIDATED, DRIFT_STATE_TERMINAL,
        DRIFT_STATE_SUPERSEDED, DRIFT_STATE_PROVENANCE_BLOCKED,
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
    ledger_path: Optional[Path] = None,
) -> int:
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
# Drift lifecycle reader
# ---------------------------------------------------------------------------


def migrate_test_sentinel_to_terminated(
    sentinel_id: str,
    *,
    ledger_path: Path | None = None,
    classification: str = "INVALID_TEST_ARTIFACT",
) -> dict:
    """Closure IV §10: durably migrate a leaked test sentinel
    through the production migration lifecycle:

      INVALID_TEST_ARTIFACT -> SUPERSEDED -> CONSUMED

    Used by the supervisor to terminate durable work that
    originated from test code writing into the production
    ledger. Atomic: writes via ``_atomic_write_json``.
    """
    path = ledger_path or _drift_ledger_path()
    # Read the cooldown ledger (legacy ids shape). If the
    # caller supplied an explicit ledger_path, use that as
    # the cooldown path; otherwise derive from the
    # supervisor state dir.
    if ledger_path is not None:
        coold_path = ledger_path
    else:
        coold_path = _state_dir_default() / "cooldown_deferred_events.json"
    if not coold_path.exists():
        return {"migrated": False, "reason": "cooldown ledger absent"}
    try:
        raw = coold_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        return {"migrated": False, "reason": f"read failed: {e}"}
    if not isinstance(data, dict):
        return {"migrated": False, "reason": "ledger not a dict"}
    entries = data.get("entries", [])
    legacy_ids = data.get("ids", [])
    # Migrate the entries shape.
    if isinstance(entries, list):
        for e in entries:
            if (
                isinstance(e, dict)
                and e.get("id") == sentinel_id
                and e.get("state") not in DRIFT_TERMINAL_STATES
            ):
                e["state"] = DRIFT_STATE_SUPERSEDED
                e["state_updated_at"] = _now_iso()
                e["superseded_by_head"] = classification
                e["classification"] = classification
    # Migrate the legacy ids shape.
    if isinstance(legacy_ids, list) and sentinel_id in legacy_ids:
        legacy_ids.remove(sentinel_id)
    # Write back atomically.
    new_data = dict(data)
    new_data["entries"] = entries
    new_data["ids"] = legacy_ids
    new_data.setdefault("migrations", []).append({
        "sentinel_id": sentinel_id,
        "classification": classification,
        "migrated_at": _now_iso(),
        "via": "migrate_test_sentinel_to_terminated",
    })
    try:
        _atomic_write_json(coold_path, new_data)
    except AtomicWriteError as e:
        return {"migrated": False, "reason": f"write failed: {e}"}
    return {
        "migrated": True,
        "sentinel_id": sentinel_id,
        "classification": classification,
    }


def list_open_drifts(ledger_path: Optional[Path] = None) -> list:
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


def list_drift_summary(ledger_path: Optional[Path] = None) -> dict:
    path = ledger_path or _drift_ledger_path()
    out = {"total": 0, "by_state": {}, "open": 0, "terminal": 0, "superseded": 0}
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
# Manifest paths (caller-supplied REPO_DIR; __file__ is NOT used)
# ---------------------------------------------------------------------------


def _state_dir_default() -> Path:
    return Path(
        os.environ.get(
            "AED_SUPERVISOR_STATE_DIR",
            str(Path.home() / ".hermes/aed-supervisor/state"),
        )
    )


def _drift_ledger_path() -> Path:
    return _state_dir_default() / "provenance_drift_pending.json"


def resolve_manifest_paths(repo_root: Path) -> tuple:
    """Return the canonical manifest paths relative to the
    explicit ``repo_root``. Production callers MUST use this
    helper and pass ``REPO_DIR`` explicitly; the module-level
    ``__file__`` is NOT used because the runtime deployment
    location differs from the source-controlled checkout.
    """
    return (
        repo_root / "provenance" / "AUTOCODER_SOURCE_COMPLETENESS.json",
        repo_root / "provenance" / "aed-pr417-source-manifest.json",
    )


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    # Head-advance state model
    "HEAD_ADVANCE_UNRELATED",
    "HEAD_ADVANCE_WORKER_PROVENANCE_VERIFIED",
    "HEAD_ADVANCE_WORKER_PROVENANCE_BLOCKED",
    "HEAD_ADVANCE_WORKER_PUSH_INVALID",
    "HEAD_ADVANCE_STATES",
    "HEAD_ADVANCE_STATES_THAT_BLOCK_QUALIFYING",
    "HeadAdvanceResult",
    # Atomic ledger
    "is_manifest_stale",
    "regenerate_manifest",
    "validate_manifest",
    "find_drift_at_head",
    "register_drift",
    "register_provenance_block",
    "transition_drift_state",
    "supersede_old_drifts",
    "list_open_drifts",
    "list_drift_summary",
    "AtomicWriteError",
    "ManifestEnumerationError",
    "ProvenanceCheckError",
    # Manifest paths
    "resolve_manifest_paths",
    # Strict enumeration
    "enumerate_controlled_destinations_strict",
    # Diagnostic enumeration
    "enumerate_controlled_destinations",
    # Drift states
    "DRIFT_STATE_DETECTED",
    "DRIFT_STATE_QUEUED",
    "DRIFT_STATE_CLAIMED",
    "DRIFT_STATE_REPAIRING",
    "DRIFT_STATE_PUSH_VERIFIED",
    "DRIFT_STATE_CI_VERIFIED",
    "DRIFT_STATE_MANIFEST_VALIDATED",
    "DRIFT_STATE_TERMINAL",
    "DRIFT_STATE_SUPERSEDED",
    "DRIFT_STATE_PROVENANCE_BLOCKED",
    "DRIFT_TERMINAL_STATES",
    "_drift_ledger_path",
]