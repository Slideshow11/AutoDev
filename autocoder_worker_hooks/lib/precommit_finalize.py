#!/usr/bin/env python3
"""Pre-commit finalize driver for AED worker sessions.

This script is the source-controlled Python driver invoked by
the worker ``pre-commit`` hook. It inspects the current
working tree + index, computes the diff between
``--prelaunch-head`` and HEAD, and delegates to the canonical
``run_provenance_finalize_if_needed`` only when at least one
manifest-controlled destination changed.

Exit codes:

  - ``0`` — finalize ran successfully OR no controlled change
    detected. The commit is allowed to proceed.
  - ``1`` — operator/hard error (bad args, missing repo, etc.).
  - ``2`` — finalize raised; the commit must be blocked.

The driver never stages arbitrary files. If
``run_provenance_finalize_if_needed`` regenerated a manifest
or audit artifact, the driver stages ONLY the canonical
generated artifacts returned in the finalization dict.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo-root", required=True)
    p.add_argument("--prelaunch-head", required=True)
    p.add_argument("--attempt-label", default="worker-precommit")
    args = p.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    if not (repo_root / ".git").exists():
        print(
            f"aed-pre-commit driver: {repo_root} is not a git repo",
            file=sys.stderr,
        )
        return 1

    # Delegate to the canonical finalizer.
    from autocoder_supervisor.provenance_maintenance import (
        run_provenance_finalize_if_needed,
        ProvenanceFinalizeError,
    )

    try:
        result = run_provenance_finalize_if_needed(
            repo_root=repo_root,
            prelaunch_head=args.prelaunch_head,
        )
    except ProvenanceFinalizeError as e:
        print(
            f"aed-pre-commit driver: finalizer raised: {e}",
            file=sys.stderr,
        )
        return 2

    if not result.get("ran"):
        return 0

    # Finalizer did run. Stage ONLY the canonical generated
    # artifacts declared in the result payload. The driver
    # never inspects the working tree to discover additional
    # files to stage; that policy lives entirely inside the
    # canonical finalizer.
    generated = result.get("provenance_finalize", {}) or {}
    changed_files = result.get("changed_files") or []
    # We trust the finalizer to expose the canonical
    # regenerated list under ``changed_files`` (which mirrors
    # ``git diff --name-only HEAD`` plus untracked). For
    # narrow purposes: stage the canonical manifest and audit
    # artifact specifically.
    for relpath in generated.get("canonical_artifacts_staged", []) or []:
        cp = repo_root / relpath
        if not cp.is_file():
            continue
        subprocess.run(
            ["git", "-C", str(repo_root), "add", "--", relpath],
            check=False, timeout=10,
        )

    # Also stage any file marked as regenerated under the
    # finalizer result's ``provenance_finalize.changed_files``
    # if the canonical author chose to expose it. We do not
    # guess.
    for relpath in (generated.get("regenerated") or []):
        cp = repo_root / relpath
        if not cp.is_file():
            continue
        subprocess.run(
            ["git", "-C", str(repo_root), "add", "--", relpath],
            check=False, timeout=10,
        )

    # Report to stdout so the hook log shows what we did.
    print(
        json.dumps({
            "ok": True,
            "attempt_label": args.attempt_label,
            "changed_files": sorted(set(changed_files)),
            "staged_canonical": sorted(set(
                list(generated.get("canonical_artifacts_staged", []) or [])
                + list(generated.get("regenerated", []) or [])
            )),
        })
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
