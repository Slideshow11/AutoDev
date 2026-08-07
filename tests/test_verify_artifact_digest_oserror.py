"""Regression tests for defect 3.5: ``_verify_artifact_digest_unchanged``
must catch ``OSError`` so an unreadable artifact path degrades to
``unavailable`` evidence rather than escaping before the durable
merge record is written.

The merge transaction calls ``_verify_artifact_digest_unchanged``
after the irreversible remote merge and before writing the merge
record. If the helper raised ``OSError`` to the caller, the
durable merge record would never be written. The helper MUST
catch ``OSError`` and append an unavailable observation.
"""
from __future__ import annotations

import builtins
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autocoder_orchestration.artifacts import write_artifact
from autocoder_orchestration.merge_authorization import (
    _verify_artifact_digest_unchanged,
)


class VerifyArtifactDigestUnchangedCatchesOsErrorTests(unittest.TestCase):
    """§3.5: ``_verify_artifact_digest_unchanged`` swallows ``OSError``
    and degrades to ``unavailable`` evidence."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="aed-oserr-"))
        self.artifact = self.tmpdir / "candidate.json"
        write_artifact(self.artifact, {"hello": "world"})

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _with_open_raising(self, exc: BaseException, fn):
        """Run ``fn`` with ``builtins.open`` patched to raise ``exc``."""
        original_open = builtins.open
        def fail_open(*args, **kwargs):
            raise exc
        builtins.open = fail_open
        try:
            return fn()
        finally:
            builtins.open = original_open

    def test_permission_error_is_caught(self) -> None:
        """A ``PermissionError`` from ``read_artifact`` is caught and
        degrades to ``unavailable``; the helper returns ``False``
        and the unavailable list contains a descriptive note."""
        unavailable: list[str] = []
        def call():
            return _verify_artifact_digest_unchanged(
                self.artifact, "any-digest", unavailable,
            )
        result = self._with_open_raising(
            PermissionError(13, "Permission denied", str(self.artifact)),
            call,
        )
        self.assertFalse(result)
        self.assertEqual(len(unavailable), 1)
        self.assertIn("candidate.json", unavailable[0])
        self.assertIn("PermissionError", unavailable[0])

    def test_is_a_directory_error_is_caught(self) -> None:
        """An ``IsADirectoryError`` from ``read_artifact`` is caught."""
        unavailable: list[str] = []
        def call():
            return _verify_artifact_digest_unchanged(
                self.artifact, "any-digest", unavailable,
            )
        result = self._with_open_raising(
            IsADirectoryError(21, "Is a directory", str(self.artifact)),
            call,
        )
        self.assertFalse(result)
        self.assertEqual(len(unavailable), 1)
        self.assertIn("IsADirectoryError", unavailable[0])

    def test_os_error_subclass_is_caught(self) -> None:
        """Any ``OSError`` subclass (e.g. ``FileNotFoundError``) is
        caught — the handler must be permissive enough to cover the
        full ``OSError`` family."""
        unavailable: list[str] = []
        def call():
            return _verify_artifact_digest_unchanged(
                self.artifact, "any-digest", unavailable,
            )
        result = self._with_open_raising(
            FileNotFoundError(2, "No such file", str(self.artifact)),
            call,
        )
        self.assertFalse(result)
        self.assertEqual(len(unavailable), 1)

    def test_digest_mismatch_still_returns_false(self) -> None:
        """When the file IS readable but the digest changed (artifact
        was tampered with after the merge), the helper returns
        ``False`` and adds a digest-mismatch note (not an OSError
        note)."""
        from autocoder_orchestration.artifacts import read_artifact
        actual_digest = read_artifact(self.artifact).digest
        wrong_digest = "0" * 64 if actual_digest != "0" * 64 else "1" * 64
        unavailable: list[str] = []
        result = _verify_artifact_digest_unchanged(
            self.artifact, wrong_digest, unavailable,
        )
        self.assertFalse(result)
        self.assertEqual(len(unavailable), 1)
        self.assertIn("digest changed", unavailable[0])

    def test_matching_digest_returns_true(self) -> None:
        """When the file IS readable AND the digest matches, the
        helper returns ``True`` and the unavailable list stays empty."""
        from autocoder_orchestration.artifacts import read_artifact
        actual_digest = read_artifact(self.artifact).digest
        unavailable: list[str] = []
        result = _verify_artifact_digest_unchanged(
            self.artifact, actual_digest, unavailable,
        )
        self.assertTrue(result)
        self.assertEqual(unavailable, [])


if __name__ == "__main__":
    unittest.main()