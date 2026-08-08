"""Canonical evidence-root artifact paths used across the CLI.

All four control-plane artifacts share one filename convention and one
canonical evidence root so the same canonical helper is used by:

- `cmd_merge_authorize` (creates `authorization.json`)
- `cmd_merge` (loads `authorization.json`, `candidate.json`,
  `verifier.json`, and writes `merge-record.json`)
- `cmd_post_merge_verify` (loads `merge-record.json`)

The repository, run-state root and evidence root remain independent
per C-24; this module only names the artifact filenames, not their
parent directory.
"""
from __future__ import annotations

from pathlib import Path
from typing import TypedDict

AUTHORIZATION_FILENAME = "authorization.json"
CANDIDATE_FILENAME = "candidate.json"
VERIFIER_FILENAME = "verifier.json"
MERGE_RECORD_FILENAME = "merge-record.json"


class CanonicalPaths(TypedDict):
    """TypedDict of the four canonical artifact paths.

    The same four keys are used everywhere a canonical path is
    needed: ``cli.py``, ``controller.py``, ``merge_authorization.py``,
    and the regression tests.
    """
    authorization: Path
    candidate: Path
    verifier: Path
    merge_record: Path


def canonical_paths(evidence_root: Path) -> CanonicalPaths:
    """Return the canonical artifact paths for a given evidence root."""
    p = Path(evidence_root)
    return {
        "authorization": p / AUTHORIZATION_FILENAME,
        "candidate": p / CANDIDATE_FILENAME,
        "verifier": p / VERIFIER_FILENAME,
        "merge_record": p / MERGE_RECORD_FILENAME,
    }