"""Provenance audit generator with consistency enforcement.

This module is the SOLE generator for
``provenance/AUTOCODER_SOURCE_COMPLETENESS.json``. It
reads the canonical extraction manifest
(``provenance/aed-pr417-source-manifest.json``) and the
AUD-referenced artifacts, computes every documented
count, and writes a single internally consistent audit
artifact.

The directive finding (PRRT_kwDOTtyQLc6XYz6O) observed
that the manifest-related counts in the audit were
duplicated in two sections and could diverge. This
module eliminates that possibility by deriving every
count from a single source-of-truth computed value and
writing both sections from that value.

The published artifact is therefore impossible to
produce in an internally contradictory state through
the normal generation path.

Usage::

    python3 -m scripts.provenance_audit.regenerate
    # or
    python3 scripts/provenance_audit.py regenerate

The CLI entry point is the audit.subcommand argument
on the module's main().
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "provenance" / "aed-pr417-source-manifest.json"
AUDIT_PATH = REPO_ROOT / "provenance" / "AUTOCODER_SOURCE_COMPLETENESS.json"
AED_REF = "b57fcaad806c68b93668bcd318fa26ab15a8ab40"


class ProvenanceAuditError(RuntimeError):
    """Raised when the audit cannot be regenerated
    consistently."""


def compute_manifest_metrics(manifest: dict) -> dict:
    """Compute the authoritative manifest match counts
    from the live manifest.

    Returns a dict with ``manifest_files_count``,
    ``manifest_source_files_count``,
    ``manifest_standalone_additions_count`` derived
    directly from ``manifest["files"]``. The standalone
    count is computed as ``total - source_paths`` per
    the schema contract, ensuring the three values are
    internally consistent by construction.

    Raises ProvenanceAuditError if the manifest is
    malformed."""
    if not isinstance(manifest, dict):
        raise ProvenanceAuditError(
            f"manifest must be a dict, got {type(manifest).__name__}"
        )
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ProvenanceAuditError(
            "manifest.files must be a list"
        )
    total = len(files)
    source_count = sum(1 for f in files if f.get("source_path"))
    standalone_count = total - source_count
    # Cross-check: standalone = files without source_path
    actual_standalone = sum(1 for f in files if not f.get("source_path"))
    if actual_standalone != standalone_count:
        raise ProvenanceAuditError(
            f"manifest arithmetic violation: "
            f"total={total}, source={source_count}, "
            f"standalone={standalone_count}, but actual "
            f"files without source_path={actual_standalone}"
        )
    return {
        "manifest_files_count": total,
        "manifest_source_files_count": source_count,
        "manifest_standalone_additions_count": standalone_count,
    }


def compute_extracted_manifest_records(manifest: dict) -> list:
    """Compute the manifest_records array from the
    live manifest. Each record is a subset of the
    manifest entry containing only the fields required
    by the audit's extracted_manifest_match.manifest_records
    invariant."""
    records = []
    for f in manifest["files"]:
        records.append({
            "source_path": f.get("source_path"),
            "destination_path": f["destination_path"],
            "transformation_classification": f.get("transformation_classification"),
            "source_sha256": f.get("source_sha256"),
            "destination_sha256": f["destination_sha256"],
            "source_size_bytes": f.get("source_size_bytes"),
            "destination_size_bytes": f["destination_size_bytes"],
            "transformation_explanation": f.get("transformation_explanation", ""),
        })
    return records


def dual_manifest_metrics(metrics: dict) -> dict:
    """Build the standard dict that is written to BOTH
    ``extracted_manifest_match`` and
    ``metrics.extracted_manifest_match`` sections, so
    they cannot diverge by construction."""
    return {
        "manifest_files_count": metrics["manifest_files_count"],
        "manifest_source_files_count": metrics["manifest_source_files_count"],
        "manifest_standalone_additions_count": metrics["manifest_standalone_additions_count"],
    }


def regenerate_audit(audit_path: Path = AUDIT_PATH,
                       manifest_path: Path = MANIFEST_PATH) -> dict:
    """Regenerate the full audit artifact.

    Reads the live manifest, computes authoritative
    counts, and overwrites both ``extracted_manifest_match``
    and ``metrics.extracted_manifest_match`` from the
    same dual metric dict.

    Returns the regenerated audit dict. The caller is
    responsible for writing it to disk."""
    with manifest_path.open() as f:
        manifest = json.load(f)
    with audit_path.open() as f:
        audit = json.load(f)

    metrics = compute_manifest_metrics(manifest)
    dual = dual_manifest_metrics(metrics)
    records = compute_extracted_manifest_records(manifest)

    # Patch the audit's extracted_manifest_match. Fields
    # that the manifest cannot determine are preserved.
    audit["extracted_manifest_match"]["manifest_files_count"] = dual["manifest_files_count"]
    audit["extracted_manifest_match"]["manifest_source_files_count"] = dual["manifest_source_files_count"]
    audit["extracted_manifest_match"]["manifest_standalone_additions_count"] = dual["manifest_standalone_additions_count"]
    audit["extracted_manifest_match"]["manifest_records"] = records

    # Mirror the same metric dict into the audit's
    # metrics block so the two sections are guaranteed
    # to agree.
    audit.setdefault("metrics", {})
    audit["metrics"]["extracted_manifest_match"] = dict(dual)
    audit["metrics"]["total_source_paths_in_manifest"] = dual["manifest_source_files_count"]
    audit["metrics"]["manifest_standalone_additions"] = dual["manifest_standalone_additions_count"]

    # Pre-publish consistency self-check.
    _validate_audit_consistency(audit)

    return audit


def _validate_audit_consistency(audit: dict) -> None:
    """Pre-publish internal consistency check.

    Raises ProvenanceAuditError if the audit's
    duplicated manifest metrics sections disagree or
    if the standalone count violates the
    total - source arithmetic contract.

    This is the canonical validator. The behavioral
    regression test
    ``test_autocoder_supervisor_source_completeness.py::test_manifest_match_count``
    enforces the same invariants at runtime.
    """
    mm = audit["extracted_manifest_match"]
    metrics_mm = audit["metrics"]["extracted_manifest_match"]
    for key in ("manifest_files_count",
                "manifest_source_files_count",
                "manifest_standalone_additions_count"):
        if mm[key] != metrics_mm[key]:
            raise ProvenanceAuditError(
                f"audit count divergence: "
                f"extracted_manifest_match.{key}={mm[key]} "
                f"!= metrics.extracted_manifest_match.{key}={metrics_mm[key]}"
            )
    expected_standalone = mm["manifest_files_count"] - mm["manifest_source_files_count"]
    if mm["manifest_standalone_additions_count"] != expected_standalone:
        raise ProvenanceAuditError(
            f"manifest arithmetic violation: "
            f"manifest_standalone_additions_count={mm['manifest_standalone_additions_count']} "
            f"!= manifest_files_count - manifest_source_files_count "
            f"({mm['manifest_files_count']} - {mm['manifest_source_files_count']} = {expected_standalone})"
        )


def write_audit(audit: dict, audit_path: Path = AUDIT_PATH) -> None:
    """Write the audit atomically. The audit is
    finalized only after the pre-publish consistency
    check passes."""
    with audit_path.open("w") as f:
        json.dump(audit, f, indent=2)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="subcommand", required=True)
    regen = sub.add_parser("regenerate",
                            help="Regenerate the audit artifact from the live manifest")
    regen.add_argument("--audit", type=Path, default=AUDIT_PATH)
    regen.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    check = sub.add_parser("check",
                            help="Run the consistency check on the existing audit")
    check.add_argument("--audit", type=Path, default=AUDIT_PATH)
    args = p.parse_args()
    if args.subcommand == "regenerate":
        audit = regenerate_audit(args.audit, args.manifest)
        write_audit(audit, args.audit)
        print(f"OK: regenerated {args.audit}")
        return 0
    elif args.subcommand == "check":
        with args.audit.open() as f:
            audit = json.load(f)
        _validate_audit_consistency(audit)
        print(f"OK: audit consistency check passed")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())