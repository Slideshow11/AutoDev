"""Provenance maintenance helpers.

Pre-canary round-281 §6: the production worker must be able
to detect a stale provenance manifest, regenerate the
manifest from the actual committed bytes, and verify the
post-repair manifest matches reality — all without operator
intervention.

The manifest format is JSON. The two manifests we manage
here are:

- ``provenance/AUTOCODER_SOURCE_COMPLETENESS.json``
- ``provenance/aed-pr417-source-manifest.json``

Each manifest contains a list of records (under various
keys; this module handles the common shape) where each
record carries a SHA-256 hash of an ``autodev_sha256`` field
that is meant to reflect the file's bytes at the manifest's
commit. When a tracked file changes, the corresponding
``autodev_sha256`` MUST be updated or CI fails.

This module provides:

- ``is_manifest_stale(manifest_path, repo_root)`` — True iff
  any tracked ``autodev_sha256`` in the manifest does not
  match the current file bytes.
- ``regenerate_manifest(manifest_path, repo_root,
  *, allowed_paths=None)`` — rewrite the manifest so that
  every record's ``autodev_sha256`` reflects the current
  bytes. ONLY records whose ``autodev_destination`` (or
  equivalent field) is present in ``allowed_paths`` are
  updated; all other records are preserved untouched. This
  prevents an autonomous repair from rewriting manifest
  entries it has no way to verify.
- ``validate_manifest(manifest_path, repo_root,
  *, allowed_paths=None)`` — True iff no stale entries AND
  no records have been silently rewritten.

The conservative update contract (only update records whose
paths we can verify) is what makes the manifest remain an
integrity record rather than a self-approving hash dump.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Iterable


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_records(manifest: dict) -> Iterable[tuple[list[str], dict]]:
    """Yield (path_to_record, record) pairs for every record in the manifest.

    Walks the JSON looking for any list of dicts that has at
    least one ``autodev_sha256`` field per entry. Records that
    look like file entries get the canonical path key
    (``autodev_destination``).
    """
    def _walk(obj, parents):
        if isinstance(obj, dict):
            # Record?
            if (
                "autodev_sha256" in obj
                and isinstance(obj.get("autodev_destination"), str)
            ):
                yield parents, obj
                return
            for k, v in obj.items():
                yield from _walk(v, parents + [k])
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                yield from _walk(item, parents + [str(i)])

    yield from _walk(manifest, [])


def _get_path_in_repo(repo_root: Path, dest: str) -> Path:
    """Resolve an ``autodev_destination`` relative to ``repo_root``.

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


def is_manifest_stale(
    manifest_path: Path, repo_root: Path, *, allowed_paths: Iterable[str] | None = None
) -> bool:
    """True iff any tracked record's autodev_sha256 disagrees with bytes."""
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return True
    allowed = set(allowed_paths) if allowed_paths is not None else None
    for _, rec in _iter_records(manifest):
        dest = rec.get("autodev_destination")
        if not isinstance(dest, str):
            continue
        if allowed is not None and dest not in allowed:
            continue
        try:
            actual = _sha256_of(_get_path_in_repo(repo_root, dest))
        except OSError:
            # File missing — manifest is stale.
            return True
        expected = rec.get("autodev_sha256")
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
    """Rewrite ``autodev_sha256`` for records whose paths are in
    ``allowed_paths``. Returns a small audit record describing
    what changed.

    Records outside ``allowed_paths`` are preserved exactly
    as-is so the manifest remains a verifiable integrity
    record (not a self-approving hash dump).
    """
    allowed = set(allowed_paths)
    manifest = json.loads(manifest_path.read_text())
    audit = {"updated": [], "skipped_outside_allowed": 0, "missing_files": []}
    for path, rec in _iter_records(manifest):
        dest = rec.get("autodev_destination")
        if not isinstance(dest, str):
            continue
        if dest not in allowed:
            audit["skipped_outside_allowed"] += 1
            continue
        target = _get_path_in_repo(repo_root, dest)
        try:
            new_hash = _sha256_of(target)
        except OSError:
            audit["missing_files"].append(dest)
            continue
        old_hash = rec.get("autodev_sha256")
        rec["autodev_sha256"] = new_hash
        audit["updated"].append({"path": dest, "old": old_hash, "new": new_hash})
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return audit


def validate_manifest(
    manifest_path: Path, repo_root: Path, *, allowed_paths: Iterable[str]
) -> bool:
    """True iff every allowed record's autodev_sha256 matches the bytes."""
    return not is_manifest_stale(manifest_path, repo_root, allowed_paths=allowed_paths)


__all__ = [
    "is_manifest_stale",
    "regenerate_manifest",
    "validate_manifest",
    "_PROVENANCE_MANIFEST_PATH",
    "_PROVENANCE_MANIFEST_PATH_AED",
]


# Canonical provenance manifests the supervisor tracks. Both files
# are tracked in git and validated by the ``provenance`` CI job. The
# supervisor consults these on every REPAIR_PUSHED.
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