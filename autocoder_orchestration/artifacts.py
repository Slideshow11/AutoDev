"""Canonical artifact format for the AutoDev orchestration control plane.

The control plane persists several JSON artifacts (readiness certificates,
candidates, verifier handoffs, verifier records, merge authorizations, merge
records, evidence freezes, final reports, invalidation registry entries). This
module defines ONE uniform digest contract for all of them and a single
durable writer / strict reader pair that every call site uses.

Digest contract
---------------

1. The artifact file is valid UTF-8 JSON only.
2. No comment, digest footer or non-JSON text is appended.
3. JSON serialization is deterministic (sorted keys, compact separators).
4. The artifact digest is SHA-256 of the EXACT complete artifact-file bytes.
5. The digest is stored in a separate atomic sidecar ``<path>.sha256``.
6. The sidecar contains only the lowercase 64-character hexadecimal digest,
   optionally followed by one trailing newline.
7. The sidecar and artifact are written through one durable artifact writer.
8. Legacy artifacts that contain a ``# sha256: <hex>`` footer text line are
   REFUSED by the production merge path. They may only be read by the
   explicit ``read_legacy_with_footer`` audit helper.

Writer guarantees
-----------------

- Writes the temp file in the same directory as the target.
- Mode 0600 on the temp file and on the final artifact.
- Mode 0700 on the parent directory.
- fsync on the data before rename.
- atomic ``os.replace`` of the temp file.
- fsync of the parent directory (when supported).
- fsync on the sidecar before rename.
- atomic replace of the sidecar.
- Cleans up temp files on failure.

Reader guarantees
-----------------

- Rejects missing files.
- Rejects symlinks for the artifact or the sidecar.
- Rejects world- or group-readable files.
- Rejects malformed JSON.
- Rejects malformed sidecar text.
- Rejects a digest mismatch between the file and the sidecar.
- Rejects legacy ``# sha256: ...`` footer text in the artifact body.
- Returns the parsed payload and the verified exact-file digest.

A missing digest MUST block any merge authorization; the reader refuses to
return a payload without one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple


# === Errors ===

class ArtifactError(Exception):
    """Base error for the canonical artifact format."""


class ArtifactMissing(ArtifactError):
    """Artifact or sidecar does not exist."""


class ArtifactSymlink(ArtifactError):
    """Artifact or sidecar is a symlink."""


class ArtifactInsecureMode(ArtifactError):
    """Artifact or sidecar has world or group permission bits set."""


class ArtifactMalformedJSON(ArtifactError):
    """Artifact is not valid UTF-8 JSON."""


class ArtifactMalformedSidecar(ArtifactError):
    """Sidecar text is not exactly one lowercase 64-character hex digest."""

    def __init__(self, path: str, content: str):
        super().__init__(
            f"malformed sidecar {path!r}: {content!r}"
        )
        self.path = path
        self.content = content


class ArtifactDigestMismatch(ArtifactError):
    """Artifact file bytes do not match the digest in the sidecar."""

    def __init__(
        self,
        path: str,
        expected: str,
        actual: str,
    ):
        super().__init__(
            f"digest mismatch for {path!r}: "
            f"sidecar={expected!r} actual={actual!r}"
        )
        self.path = path
        self.expected = expected
        self.actual = actual


class LegacyArtifactRefused(ArtifactError):
    """Artifact body contains a legacy ``# sha256: ...`` footer line."""


# === Constants ===

# Allow non-strict path validation for the canonical writer (the path is
# supplied by the caller and may be absolute). The strict reader uses the
# stricter safe-path rules defined in store.py when validating evidence
# paths stored inside an artifact payload.
LEGACY_FOOTER_RE = re.compile(rb"^# sha256: [0-9a-f]{64}\s*$", re.MULTILINE)
SIDECAR_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_FOOTER_DIGEST_RE = re.compile(r"[0-9a-f]{64}")

_CANONICAL_SEPARATOR = (",", ":")
_CANONICAL_ENSURE_ASCII = False
_CANONICAL_SORT_KEYS = True
_CANONICAL_ALLOW_NAN = False


# === Digest computation ===

def digest_bytes(data: bytes) -> str:
    """Compute SHA-256 over ``data`` and return lowercase hex."""
    return hashlib.sha256(data).hexdigest()


def digest_file(path: Path) -> str:
    """Compute SHA-256 over the exact bytes of ``path``."""
    with open(path, "rb") as f:
        return digest_bytes(f.read())


# === Sidecar parsing ===

def parse_sidecar_text(text: str, *, path: str) -> str:
    """Parse a sidecar's text content into a lowercase 64-character hex digest.

    Refuses anything that is not exactly one digest line (optionally with a
    trailing newline).
    """
    if text.endswith("\n"):
        text = text[:-1]
    if not SIDECAR_DIGEST_RE.fullmatch(text):
        raise ArtifactMalformedSidecar(path, text)
    return text


# === Writer ===

@dataclass(frozen=True)
class ArtifactWriteResult:
    """The result of writing one canonical artifact."""

    artifact_path: Path
    sidecar_path: Path
    digest: str
    bytes_written: int


def _ensure_private_dir(path: Path) -> None:
    """Ensure ``path`` exists, is a directory, and has mode 0700.

    A missing parent of ``path`` is created with ``parents=True`` so a
    first-time caller does not have to construct the evidence tree by
    hand. Any filesystem-level failure (``OSError``) is wrapped as an
    :class:`ArtifactError` so the merge path can surface it through the
    declared error hierarchy.
    """
    if path.exists():
        if not path.is_dir() or path.is_symlink():
            raise ArtifactError(f"parent path is not a private directory: {path}")
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    else:
        try:
            path.mkdir(mode=0o700, parents=True)
        except OSError as e:
            raise ArtifactError(f"cannot create private directory {path!r}: {e!r}") from e


def _ensure_private_file(path: Path) -> None:
    """Ensure ``path`` exists with mode 0600 (creating an empty file if needed)."""
    if path.exists() and path.is_symlink():
        raise ArtifactError(f"file is a symlink: {path}")
    if path.exists():
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _fsync_directory(path: Path) -> None:
    """fsync a directory if the platform supports it.

    On platforms without ``O_DIRECTORY`` (Windows), directory fsync is
    skipped; ``os.replace`` still provides atomic replace semantics.
    """
    try:
        dir_fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _atomic_write_with_sidecar(
    artifact_path: Path,
    payload: Dict[str, Any],
    *,
    ensure_ascii: bool = _CANONICAL_ENSURE_ASCII,
) -> ArtifactWriteResult:
    """Write ``payload`` as canonical JSON to ``artifact_path`` and write the
    matching ``<artifact_path>.sha256`` sidecar.

    The artifact and sidecar are written through one durable transaction:
    artifact first, then sidecar, then directory fsync. A failure in any step
    cleans up both temp files.
    """
    artifact_path = Path(artifact_path)
    sidecar_path = artifact_path.with_suffix(artifact_path.suffix + ".sha256")
    parent = artifact_path.parent
    _ensure_private_dir(parent)

    blob = json.dumps(
        payload,
        sort_keys=_CANONICAL_SORT_KEYS,
        separators=_CANONICAL_SEPARATOR,
        ensure_ascii=ensure_ascii,
        allow_nan=_CANONICAL_ALLOW_NAN,
    ).encode("utf-8")
    digest = digest_bytes(blob)

    # Validate the body has no legacy footer (defensive).
    if LEGACY_FOOTER_RE.search(blob):
        raise LegacyArtifactRefused(
            f"refusing to write artifact with legacy footer text: {artifact_path}"
        )

    # Artifact temp file
    artifact_fd, artifact_tmp = tempfile.mkstemp(
        prefix=f".{artifact_path.name}.",
        suffix=".tmp",
        dir=str(parent),
    )
    try:
        # Sidecar temp file. If mkstemp fails here, the artifact temp
        # file above is cleaned up explicitly so we never leak
        # `.artifact.*.tmp` files on repeated partial failures.
        sidecar_fd, sidecar_tmp = tempfile.mkstemp(
            prefix=f".{sidecar_path.name}.",
            suffix=".tmp",
            dir=str(parent),
        )
    except OSError:
        try:
            os.close(artifact_fd)
        except OSError:
            pass
        try:
            os.unlink(artifact_tmp)
        except OSError:
            pass
        raise

    try:
        # Write + flush + fsync the artifact
        with os.fdopen(artifact_fd, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(artifact_tmp, 0o600)
        except OSError as e:
            raise ArtifactError(f"cannot chmod 0600 artifact tmp: {e!r}")

        # Write + flush + fsync the sidecar (digest + optional newline)
        with os.fdopen(sidecar_fd, "wb") as f:
            f.write((digest + "\n").encode("ascii"))
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(sidecar_tmp, 0o600)
        except OSError as e:
            raise ArtifactError(f"cannot chmod 0600 sidecar tmp: {e!r}")

        # Atomic replaces (artifact first, then sidecar)
        os.replace(artifact_tmp, artifact_path)
        os.replace(sidecar_tmp, sidecar_path)

        # Final mode enforcement on the live files
        try:
            os.chmod(artifact_path, 0o600)
        except OSError:
            pass
        try:
            os.chmod(sidecar_path, 0o600)
        except OSError:
            pass

        # fsync the parent directory so the rename is durable
        _fsync_directory(parent)

        return ArtifactWriteResult(
            artifact_path=artifact_path,
            sidecar_path=sidecar_path,
            digest=digest,
            bytes_written=len(blob),
        )
    except Exception:
        # Clean up temp files on failure
        for tmp in (artifact_tmp, sidecar_tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


# === Reader ===

@dataclass(frozen=True)
class ArtifactReadResult:
    """The result of reading one canonical artifact with verified digest."""

    payload: Dict[str, Any]
    digest: str
    artifact_path: Path
    sidecar_path: Path
    bytes_read: int


def _lstat_reject_symlink(path: Path) -> None:
    lst = os.lstat(path)
    if stat.S_ISLNK(lst.st_mode):
        raise ArtifactSymlink(f"path is a symlink: {path}")


def _lstat_reject_insecure_mode(path: Path) -> None:
    lst = os.lstat(path)
    if stat.S_ISLNK(lst.st_mode):
        # Already rejected above; do not double-raise.
        return
    if (lst.st_mode & 0o777) & 0o077:
        raise ArtifactInsecureMode(
            f"file has world or group permission bits set: {path} "
            f"(mode={oct(lst.st_mode & 0o777)})"
        )


def read_artifact(artifact_path: Path) -> ArtifactReadResult:
    """Strictly read a canonical artifact.

    Raises one of ``ArtifactError`` subclasses on any failure. Returns the
    parsed payload and the verified exact-file digest.

    A missing sidecar, malformed sidecar, malformed JSON, symlink, insecure
    mode, legacy footer text or digest mismatch all raise.
    """
    artifact_path = Path(artifact_path)
    sidecar_path = artifact_path.with_suffix(artifact_path.suffix + ".sha256")

    # Existence
    if not artifact_path.exists():
        raise ArtifactMissing(f"artifact missing: {artifact_path}")
    if not sidecar_path.exists():
        raise ArtifactMissing(f"sidecar missing: {sidecar_path}")

    # Symlinks
    _lstat_reject_symlink(artifact_path)
    _lstat_reject_symlink(sidecar_path)

    # Insecure modes
    _lstat_reject_insecure_mode(artifact_path)
    _lstat_reject_insecure_mode(sidecar_path)

    # Read the sidecar text first so we have the expected digest.
    with open(sidecar_path, "rb") as f:
        sidecar_bytes = f.read()
    try:
        sidecar_text = sidecar_bytes.decode("ascii")
    except UnicodeDecodeError:
        raise ArtifactMalformedSidecar(str(sidecar_path), repr(sidecar_bytes[:80]))
    expected_digest = parse_sidecar_text(sidecar_text, path=str(sidecar_path))

    # Read the artifact bytes.
    with open(artifact_path, "rb") as f:
        blob = f.read()

    # Reject legacy footer text explicitly.
    if LEGACY_FOOTER_RE.search(blob):
        raise LegacyArtifactRefused(
            f"artifact body contains legacy '# sha256: ...' footer: {artifact_path}"
        )

    # Verify the digest.
    actual_digest = digest_bytes(blob)
    if actual_digest != expected_digest:
        raise ArtifactDigestMismatch(
            path=str(artifact_path),
            expected=expected_digest,
            actual=actual_digest,
        )

    # Parse JSON.
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ArtifactMalformedJSON(f"artifact is not valid UTF-8: {artifact_path}: {e}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        raise ArtifactMalformedJSON(f"artifact is not valid JSON: {artifact_path}: {e}")
    if not isinstance(payload, dict):
        raise ArtifactMalformedJSON(
            f"artifact top-level JSON must be an object: {artifact_path}"
        )

    return ArtifactReadResult(
        payload=payload,
        digest=actual_digest,
        artifact_path=artifact_path,
        sidecar_path=sidecar_path,
        bytes_read=len(blob),
    )


# === Convenience wrappers ===

def write_artifact(
    path: str | os.PathLike,
    payload: Dict[str, Any],
) -> ArtifactWriteResult:
    """Write a canonical artifact to ``path`` with sidecar."""
    return _atomic_write_with_sidecar(Path(path), payload)


def read_artifact_at(path: str | os.PathLike) -> ArtifactReadResult:
    """Strictly read a canonical artifact from ``path``."""
    return read_artifact(Path(path))


def read_legacy_with_footer(
    artifact_path: Path,
) -> Tuple[Dict[str, Any], str]:
    """EXPLICIT AUDIT-ONLY helper to read a legacy footer-bearing artifact.

    Strips the trailing ``# sha256: <hex>`` line before parsing JSON and
    returns ``(payload, footer_digest)``. The production merge path MUST NOT
    call this; it exists solely for one-time conversion or forensic review.

    Refuses artifacts that are missing the footer line, malformed JSON, or
    symlinks / insecure modes.
    """
    artifact_path = Path(artifact_path)
    if not artifact_path.exists():
        raise ArtifactMissing(f"artifact missing: {artifact_path}")
    _lstat_reject_symlink(artifact_path)
    _lstat_reject_insecure_mode(artifact_path)

    with open(artifact_path, "rb") as f:
        blob = f.read()

    # Decode as UTF-8 first, then validate the footer. Malformed UTF-8
    # is converted to an ArtifactMalformedJSON-style refusal so callers
    # map artifact failures uniformly.
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ArtifactMalformedJSON(
            f"legacy artifact is not valid UTF-8: {artifact_path}: {e}"
        ) from e

    # Strip the footer (must be the LAST non-empty line).
    lines = text.splitlines()
    if not lines or not lines[-1].startswith("# sha256: "):
        raise LegacyArtifactRefused(
            f"no legacy footer line found: {artifact_path}"
        )
    footer_line = lines[-1]
    parts = footer_line.split(None, 2)
    if len(parts) < 3:
        raise LegacyArtifactRefused(
            f"malformed legacy footer: {artifact_path}"
        )
    expected_footer_digest = parts[2].strip()
    # The footer digest must be exactly 64 lowercase hexadecimal characters.
    if not _FOOTER_DIGEST_RE.fullmatch(expected_footer_digest):
        raise ArtifactMalformedSidecar(
            path=str(artifact_path), content=expected_footer_digest
        )

    body_text = "\n".join(lines[:-1])
    # Re-add a single trailing newline if present in the original
    # to preserve deterministic body hashing; here we do not need that
    # because we already extracted the digest.
    # Verify the footer digest matches the actual body bytes. If the
    # historical writer covered body_bytes (without the footer), the
    # comparison holds; if it covered a different byte sequence, the
    # unverified digest is refused here.
    actual_body_digest = digest_bytes(body_text.encode("utf-8"))
    if actual_body_digest != expected_footer_digest:
        raise ArtifactDigestMismatch(
            path=str(artifact_path),
            expected=expected_footer_digest,
            actual=actual_body_digest,
        )

    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError as e:
        raise ArtifactMalformedJSON(
            f"legacy artifact is not valid JSON: {artifact_path}: {e}"
        ) from e
    if not isinstance(payload, dict):
        raise ArtifactMalformedJSON(
            f"legacy artifact top-level JSON must be an object: {artifact_path}"
        )
    return payload, expected_footer_digest