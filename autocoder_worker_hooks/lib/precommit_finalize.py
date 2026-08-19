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
    # The canonical finalizer (``provenance_finalize``)
    # returns ``manifest_path`` and ``audit_path`` as the
    # authoritative list of regenerated artifacts. These are
    # the only files the worker MUST stage as part of a
    # controlled-source-change commit; staging anything else
    # would violate the round-39 no-op contract.
    canonical_artifacts: list[str] = []
    for key in ("manifest_path", "audit_path"):
        relpath = generated.get(key)
        if not relpath:
            # Round-1064 P2: a missing key means the finalizer
            # declared ``ran=True`` but did not regenerate
            # BOTH canonical artifacts. A worker that
            # commits without both pieces would re-fail at
            # the pre-push gate instead of the pre-commit
            # gate; surface the failure here so the commit is
            # blocked at the earliest deterministic point.
            print(
                f"aed-pre-commit driver: finalizer omitted {key}; "
                "blocking commit so the worker cannot publish "
                "an incomplete provenance pair",
                file=sys.stderr,
            )
            return 2
        # ``manifest_path`` / ``audit_path`` may be returned
        # as absolute paths; normalize to a repo-relative
        # form before staging.
        rp = Path(str(relpath))
        try:
            rel = rp.resolve().relative_to(repo_root).as_posix()
        except (OSError, ValueError):
            # Fallback: strip the repo-root prefix as text.
            rp_s = str(rp)
            root_s = str(repo_root)
            if rp_s.startswith(root_s + "/"):
                rel = rp_s[len(root_s) + 1:]
            else:
                # Round-1064 P2: an out-of-repo path means the
                # finalizer is staging something outside the
                # worker's working tree. Block the commit
                # rather than silently skipping.
                print(
                    f"aed-pre-commit driver: {key} outside repo: {rp_s}; "
                    "blocking commit to prevent staging "
                    "out-of-tree paths",
                    file=sys.stderr,
                )
                return 2
        cp = repo_root / rel
        if not cp.is_file():
            # Round-1064 P2: the finalizer said it regenerated
            # this artifact but the file is not on disk. A
            # missing artifact would re-fail at the pre-push
            # gate; surface it here.
            print(
                f"aed-pre-commit driver: {key} missing on disk: {rel}; "
                "blocking commit so the worker cannot publish "
                "an incomplete provenance pair",
                file=sys.stderr,
            )
            return 2
        # Round-1064 P2: a nonzero ``git add`` return code or
        # any subprocess error means the staging step failed;
        # block the commit instead of silently proceeding.
        try:
            stage = subprocess.run(
                ["git", "-C", str(repo_root), "add", "--", rel],
                capture_output=True, text=True, timeout=10,
            )
        except subprocess.SubprocessError as e:
            print(
                f"aed-pre-commit driver: git add subprocess error "
                f"for {rel}: {e!r}",
                file=sys.stderr,
            )
            return 2
        if stage.returncode != 0:
            print(
                "aed-pre-commit driver: git add failed for "
                f"{rel}: rc={stage.returncode} "
                f"stderr={stage.stderr[:200]!r}",
                file=sys.stderr,
            )
            return 2
        canonical_artifacts.append(rel)

    # Report to stdout so the hook log shows what we did.
    print(
        json.dumps({
            "ok": True,
            "attempt_label": args.attempt_label,
            "changed_files": sorted(set(changed_files)),
            "staged_canonical": sorted(set(canonical_artifacts)),
        })
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
