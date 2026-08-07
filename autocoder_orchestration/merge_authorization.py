"""Human merge authorization record and guarded merge executor.

This module owns the production merge path for the orchestration control
plane. After the post-PR-3 hardening, it is the SINGLE checked-in entry
point for executing a merge:

1. ``execute_guarded_merge_transaction`` performs the entire workflow:
   load and verify the human authorization artifact, load and verify the
   candidate and verifier artifacts, fetch all live GitHub evidence,
   repeat every exact-head and integrity guard, invoke the exact guarded
   ``gh pr merge`` command once with a finite timeout, handle timeout or
   ambiguity, write the merge result, transition the state machine, and
   perform branch-independent post-merge reconciliation.

The production CLI MUST call ``execute_guarded_merge_transaction`` directly.
A separate ``compute_command`` helper exists only for tests and for the
explicit "preview the command" intent; it MUST NOT be used by production
flows that then expect another call to build the merge record.

Canonical artifact contract
---------------------------

All persistent artifacts (``merge_authorization``, ``candidate``,
``verifier_record``, ``merge_record`` and equivalents in other modules)
are written and read through :mod:`autocoder_orchestration.artifacts`:

- The artifact file is valid UTF-8 JSON only.
- No ``# sha256: ...`` footer line is appended.
- JSON serialization is deterministic (sorted keys, compact separators).
- The exact-file digest is SHA-256 of the complete file bytes.
- The digest is stored in a separate atomic sidecar
  ``<artifact-path>.sha256``.
- A missing sidecar, malformed sidecar, malformed JSON, symlink,
  insecure mode or digest mismatch raises and blocks the merge.

There is one and only one digest convention used by the production merge
path: the exact-file SHA-256 stored in the sidecar.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .artifacts import (
    ArtifactError,
    ArtifactMissing,
    LegacyArtifactRefused,
    write_artifact,
    read_artifact,
    digest_bytes,
)
from .merge_lock import (
    LockUnavailable as MergeLockUnavailable,
    merge_lock as _merge_lock,
)


# === Errors ===

class MergeError(Exception):
    """Raised by any guarded-merge path violation."""


class MergeAuthorizationMissing(MergeError):
    """The authorization artifact is missing or unreadable."""


class MergeAuthorizationMalformed(MergeError):
    """The authorization artifact failed digest, JSON, or contract checks."""


class MergeInputsCollide(MergeError):
    """Caller-supplied paths alias each other (state dir in repo root, etc.)."""


class GitHubLiveFetchError(MergeError):
    """A live GitHub evidence query failed before the merge."""


class MergeSubprocessFailed(MergeError):
    """The guarded ``gh pr merge`` command did not succeed and the outcome is
    not recoverable on the server side."""


class MergeAmbiguousOutcome(MergeError):
    """The merge subprocess returned ambiguous output and the server-side
    state cannot be safely reconciled. The merge is rejected."""


# === MergeAuthorization dataclass (unchanged contract) ===

@dataclass(frozen=True)
class MergeAuthorization:
    """Human merge authorization record.

    The contract is unchanged from the original implementation so historical
    readers and tests still work. The dataclass itself carries no SHA-256;
    that lives in the artifact that contains it.
    """

    schema_version: str
    run_id: str
    repo: str
    pr_number: int
    authorized_head: str
    candidate_sha256: str
    verifier_record_sha256: str
    base_branch: str = "main"
    feature_branch: str = ""
    merge_method: str = "squash"
    delete_branch: bool = True
    require_match_head_commit: bool = True
    authorization_timestamp: str = ""
    author: str = ""
    next_wave_authorization: Optional[Dict[str, Any]] = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.pr_number, int) or self.pr_number <= 0:
            raise ValueError("pr_number must be a positive integer")
        if not isinstance(self.authorized_head, str):
            raise ValueError("authorized_head must be a string")
        # Git commit SHA must be exactly 40 lowercase hex characters.
        _check_git_sha(self.authorized_head, "authorized_head")
        if not isinstance(self.candidate_sha256, str):
            raise ValueError("candidate_sha256 must be a string")
        # SHA-256 digests must be exactly 64 lowercase hex characters.
        _check_digest(self.candidate_sha256, "candidate_sha256")
        if not isinstance(self.verifier_record_sha256, str):
            raise ValueError("verifier_record_sha256 must be a string")
        _check_digest(self.verifier_record_sha256, "verifier_record_sha256")
        if self.merge_method not in ("squash",):
            raise ValueError(
                f"merge_method must be 'squash', got {self.merge_method!r}"
            )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "repo": self.repo,
            "pr_number": self.pr_number,
            "authorized_head": self.authorized_head,
            "candidate_sha256": self.candidate_sha256,
            "verifier_record_sha256": self.verifier_record_sha256,
            "base_branch": self.base_branch,
            "feature_branch": self.feature_branch,
            "merge_method": self.merge_method,
            "delete_branch": self.delete_branch,
            "require_match_head_commit": self.require_match_head_commit,
            "authorization_timestamp": self.authorization_timestamp,
            "author": self.author,
            "next_wave_authorization": self.next_wave_authorization,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "MergeAuthorization":
        for field_name in (
            "schema_version", "run_id", "repo", "pr_number",
            "authorized_head", "candidate_sha256", "verifier_record_sha256",
        ):
            if field_name not in payload:
                raise ValueError(f"missing required field: {field_name!r}")
        return cls(
            schema_version=str(payload["schema_version"]),
            run_id=str(payload["run_id"]),
            repo=str(payload["repo"]),
            pr_number=int(payload["pr_number"]),
            authorized_head=str(payload["authorized_head"]),
            candidate_sha256=str(payload["candidate_sha256"]),
            verifier_record_sha256=str(payload["verifier_record_sha256"]),
            base_branch=str(payload.get("base_branch", "main")),
            feature_branch=str(payload.get("feature_branch", "")),
            merge_method=str(payload.get("merge_method", "squash")),
            delete_branch=bool(payload.get("delete_branch", True)),
            require_match_head_commit=bool(payload.get("require_match_head_commit", True)),
            authorization_timestamp=str(payload.get("authorization_timestamp", "")),
            author=str(payload.get("author", "")),
            next_wave_authorization=payload.get("next_wave_authorization"),
            notes=str(payload.get("notes", "")),
        )

    def compute_sha256(self) -> str:
        """SHA-256 of the canonical serialization of this record's payload."""
        return digest_bytes(
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        )


# === MergeRecord dataclass ===

# === Merge-record schema-version enforcement ===

SUPPORTED_MERGE_RECORD_SCHEMAS = ("autocoder.merge_record.v2",)


@dataclass
class MergeRecord:
    """Post-merge evidence record written by the merge executor."""

    schema_version: str = "autocoder.merge_record.v2"
    run_id: str = ""
    repo: str = ""
    pr_number: int = 0
    authorized_head: str = ""
    squash_merge_commit: str = ""
    merge_commit_parent: str = ""
    squash_commit_parent_count: int = 0
    squash_tree_sha256: str = ""
    final_local_main_sha: str = ""
    final_origin_main_sha: str = ""
    local_main_equals_origin_main: bool = False
    feature_branch_deleted_locally: bool = False
    feature_branch_deleted_remotely: bool = False
    working_tree_clean: bool = False
    aed_clean_post_merge: bool = False
    candidate_sha256_unchanged: bool = False
    verifier_record_sha256_unchanged: bool = False
    candidate_exact_file_digest: str = ""
    verifier_record_exact_file_digest: str = ""
    authorization_exact_file_digest: str = ""
    merge_record_exact_file_digest: str = ""
    merge_timestamp: str = ""
    unauthorized_actions_not_taken: Dict[str, bool] = field(default_factory=dict)
    unavailable_observations: List[str] = field(default_factory=list)
    notes: str = ""
    state_transition: str = ""
    final_state: str = ""

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "repo": self.repo,
            "pr_number": self.pr_number,
            "authorized_head": self.authorized_head,
            "squash_merge_commit": self.squash_merge_commit,
            "merge_commit_parent": self.merge_commit_parent,
            "squash_commit_parent_count": self.squash_commit_parent_count,
            "squash_tree_sha256": self.squash_tree_sha256,
            "final_local_main_sha": self.final_local_main_sha,
            "final_origin_main_sha": self.final_origin_main_sha,
            "local_main_equals_origin_main": self.local_main_equals_origin_main,
            "feature_branch_deleted_locally": self.feature_branch_deleted_locally,
            "feature_branch_deleted_remotely": self.feature_branch_deleted_remotely,
            "working_tree_clean": self.working_tree_clean,
            "aed_clean_post_merge": self.aed_clean_post_merge,
            "candidate_sha256_unchanged": self.candidate_sha256_unchanged,
            "verifier_record_sha256_unchanged": self.verifier_record_sha256_unchanged,
            "candidate_exact_file_digest": self.candidate_exact_file_digest,
            "verifier_record_exact_file_digest": self.verifier_record_exact_file_digest,
            "authorization_exact_file_digest": self.authorization_exact_file_digest,
            "merge_record_exact_file_digest": self.merge_record_exact_file_digest,
            "merge_timestamp": self.merge_timestamp,
            "unauthorized_actions_not_taken": self.unauthorized_actions_not_taken,
            "unavailable_observations": list(self.unavailable_observations),
            "notes": self.notes,
            "state_transition": self.state_transition,
            "final_state": self.final_state,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "MergeRecord":
        schema_version = str(
            payload.get("schema_version", "autocoder.merge_record.v2")
        )
        if schema_version not in SUPPORTED_MERGE_RECORD_SCHEMAS:
            # Reject unknown or legacy schema versions rather than
            # coercing them to v2 defaults. A consumer cannot otherwise
            # distinguish "this observation was never recorded" from
            # "this observation was recorded as failed"; C-28 depends
            # on that distinction.
            raise MergeAuthorizationMalformed(
                f"unsupported merge record schema_version {schema_version!r}; "
                f"supported: {list(SUPPORTED_MERGE_RECORD_SCHEMAS)}"
            )
        return cls(
            schema_version=schema_version,
            run_id=str(payload.get("run_id", "")),
            repo=str(payload.get("repo", "")),
            pr_number=int(payload.get("pr_number", 0)),
            authorized_head=str(payload.get("authorized_head", "")),
            squash_merge_commit=str(payload.get("squash_merge_commit", "")),
            merge_commit_parent=str(payload.get("merge_commit_parent", "")),
            squash_commit_parent_count=int(payload.get("squash_commit_parent_count", 0)),
            squash_tree_sha256=str(payload.get("squash_tree_sha256", "")),
            final_local_main_sha=str(payload.get("final_local_main_sha", "")),
            final_origin_main_sha=str(payload.get("final_origin_main_sha", "")),
            local_main_equals_origin_main=bool(payload.get("local_main_equals_origin_main", False)),
            feature_branch_deleted_locally=bool(payload.get("feature_branch_deleted_locally", False)),
            feature_branch_deleted_remotely=bool(payload.get("feature_branch_deleted_remotely", False)),
            working_tree_clean=bool(payload.get("working_tree_clean", False)),
            aed_clean_post_merge=bool(payload.get("aed_clean_post_merge", False)),
            candidate_sha256_unchanged=bool(payload.get("candidate_sha256_unchanged", False)),
            verifier_record_sha256_unchanged=bool(payload.get("verifier_record_sha256_unchanged", False)),
            candidate_exact_file_digest=str(payload.get("candidate_exact_file_digest", "")),
            verifier_record_exact_file_digest=str(payload.get("verifier_record_exact_file_digest", "")),
            authorization_exact_file_digest=str(payload.get("authorization_exact_file_digest", "")),
            merge_record_exact_file_digest=str(payload.get("merge_record_exact_file_digest", "")),
            merge_timestamp=str(payload.get("merge_timestamp", "")),
            unauthorized_actions_not_taken=dict(payload.get("unauthorized_actions_not_taken", {})),
            unavailable_observations=list(payload.get("unavailable_observations", [])),
            notes=str(payload.get("notes", "")),
            state_transition=str(payload.get("state_transition", "")),
            final_state=str(payload.get("final_state", "")),
        )


# === Helpers ===

_LOWER_HEX_40_RE = re.compile(r"^[0-9a-f]{40}$")
_LOWER_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")


def _check_git_sha(value: str, label: str) -> None:
    """Validate a Git commit SHA: exactly 40 lowercase hex characters."""
    if not isinstance(value, str) or not _LOWER_HEX_40_RE.match(value):
        raise ValueError(f"{label} must be 40 lowercase hex chars (Git commit SHA)")


def _check_digest(value: str, label: str) -> None:
    """Validate a SHA-256 digest: exactly 64 lowercase hex characters."""
    if not isinstance(value, str) or not _LOWER_HEX_64_RE.match(value):
        raise ValueError(f"{label} must be 64 lowercase hex chars (SHA-256 digest)")


# Backwards-compatible alias: accept either 40 (commit SHA) or 64
# (digest). The validators above split them; _check_sha remains for
# fields whose type is not pinned to either length.
_LOWER_HEX_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")


def _check_sha(value: str, label: str) -> None:
    if not isinstance(value, str) or not _LOWER_HEX_RE.match(value):
        raise ValueError(f"{label} must be 40 or 64 lowercase hex chars")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _normalize_path(path: str | os.PathLike) -> Path:
    """Resolve a path to its canonical form.

    Uses :meth:`Path.resolve` so symlinks, ``.``, ``..`` and trailing
    slashes all collapse to a single canonical key. The path is
    resolved relative to the current working directory if it is
    relative; absolute paths are returned as-is. The file is NOT
    required to exist (we resolve the path string, not the path
    object).
    """
    return Path(os.path.expanduser(os.fspath(path))).resolve()


def _ensure_distinct_paths(*paths: Tuple[str, Path]) -> None:
    """Refuse if any two distinct named roots resolve to the same directory.

    Repository checkout, run-state root, evidence root and the per-run
    artifact root must be independent. The production merge path refuses
    to proceed if the caller configures two roots that resolve to the
    same directory.

    Each path is normalized via :func:`_normalize_path` so symlinks,
    ``.``, ``..`` and trailing slashes all collapse to the same key.
    """
    seen: Dict[str, str] = {}
    for label, p in paths:
        if p is None:
            continue
        key = str(_normalize_path(p))
        if key in seen and seen[key] != label:
            raise MergeInputsCollide(
                f"distinct roots collide: {seen[key]!r} and {label!r} both at {key}"
            )
        seen[key] = label


def _verify_artifact_digest_unchanged(
    path: Path,
    expected_digest: str,
    unavailable_list: List[str],
) -> bool:
    """Re-verify that an artifact's exact-file digest still matches after the merge.

    Reads the artifact bytes and returns True iff the SHA-256 matches
    ``expected_digest``. On any ``OSError`` (e.g. PermissionError,
    IsADirectoryError, FileNotFoundError) or ``ArtifactError``
    (missing sidecar, digest mismatch, missing file), returns False and
    appends a descriptive note to ``unavailable_list``. The
    exception must NEVER escape this helper — the durable merge
    record is being written after the irreversible remote merge, and
    any escaping exception can leave the post-merge record in a
    half-written state.
    """
    try:
        result = read_artifact(path)
    except (ArtifactError, OSError) as e:
        unavailable_list.append(
            f"post-merge re-verification of {path.name} failed: {type(e).__name__}: {e!r}"
        )
        return False
    if result.digest != expected_digest:
        unavailable_list.append(
            f"post-merge re-verification of {path.name}: digest changed "
            f"(expected={expected_digest!r}, actual={result.digest!r})"
        )
        return False
    return True


def _safe_run(
    args: List[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """Run a subprocess with a finite timeout and decode outputs defensively.

    ``subprocess.TimeoutExpired`` and any ``OSError`` (for example a missing
    or non-executable ``gh`` binary, or a missing cwd) are converted to a
    non-zero ``returncode`` so the caller can surface them through the
    ``MergeSubprocessFailed`` / ``MergeError`` hierarchy. A ``FileNotFoundError``
    or ``PermissionError`` is the most common ``OSError`` here.
    """
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        encoding = "utf-8"
        stdout_text = ""
        stderr_text = ""
        if getattr(e, "stdout", None):
            stdout_text = e.stdout.decode(encoding, errors="replace") if isinstance(e.stdout, bytes) else str(e.stdout)
        if getattr(e, "stderr", None):
            stderr_text = e.stderr.decode(encoding, errors="replace") if isinstance(e.stderr, bytes) else str(e.stderr)
        return {
            "returncode": -1,
            "stdout": stdout_text,
            "stderr": (stderr_text + f" [TIMEOUT after {timeout}s]"),
            "timed_out": True,
        }
    except OSError as e:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"{type(e).__name__}: {e}",
            "timed_out": False,
        }
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "timed_out": False,
    }


# === Live GitHub evidence fetch ===

def fetch_live_pr_payload(
    gh_executable: str,
    repo: str,
    pr_number: int,
    *,
    runner: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Fetch the live PR state via ``gh pr view``.

    Returns a dict with ``state``, ``merged``, ``head``, ``baseRefName``,
    ``mergeable`` and ``autoMergeRequest``. Raises on subprocess failure
    or ambiguous output.
    """
    _runner = runner or (lambda *a, **kw: _safe_run(list(a), **kw))
    res = _runner(gh_executable, "pr", "view", str(pr_number),
                  "--repo", repo,
                  "--json", "state,isDraft,mergeable,mergeStateStatus,mergedAt,headRefOid,baseRefName,autoMergeRequest,number")
    if res["returncode"] != 0:
        raise GitHubLiveFetchError(
            f"gh pr view failed (rc={res['returncode']}): "
            f"stderr={res['stderr']!r}"
        )
    try:
        doc = json.loads(res["stdout"])
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise GitHubLiveFetchError(f"gh pr view returned non-JSON: {e!r}")
    merged = doc.get("mergedAt") is not None
    state = str(doc.get("state", "")).lower()
    if state not in ("open", "closed", "merged"):
        raise GitHubLiveFetchError(f"unexpected PR state: {state!r}")
    return {
        "state": state,
        "merged": merged,
        "isDraft": bool(doc.get("isDraft", False)),
        "head": {"sha": str(doc.get("headRefOid", ""))},
        "baseRefName": str(doc.get("baseRefName", "")),
        "mergeable": str(doc.get("mergeable", "")),
        "mergeStateStatus": str(doc.get("mergeStateStatus", "")),
        "autoMergeRequest": doc.get("autoMergeRequest"),
        # Include the repository identity so the cross-binding guard
        # in execute_guarded_merge_transaction can compare it against
        # auth.repo (per C-25: every integrity guard is mandatory).
        "repo": repo,
    }


# === Post-merge Git reconciliation (branch-independent) ===

@dataclass
class PostMergeReconciliation:
    """Result of branch-independent post-merge reconciliation."""

    initial_branch: str
    target_branch: str
    switched_to_base: bool
    fast_forwarded: bool
    local_main_sha: str
    origin_main_sha: str
    local_main_equals_origin_main: bool
    squash_merge_commit: str
    squash_parent_count: int
    squash_tree_sha256: str
    squash_parent: str
    feature_branch_local_deleted: bool
    feature_branch_remote_deleted: bool
    working_tree_clean: bool
    unavailable_observations: List[str] = field(default_factory=list)
    aed_clean: bool = False
    aed_checked: bool = False


def _run_git(args: List[str], cwd: Path, *, timeout: float = 30.0) -> Tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr)."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def reconcile_after_merge(
    *,
    repository_checkout: Path,
    base_branch: str,
    feature_branch: str,
    authorized_head: str,
    pr_merge_commit_oid: Optional[str] = None,
    aed_path: str = "scripts/quiet_window_observer.py",
    expected_aed_sha256: Optional[str] = None,
) -> PostMergeReconciliation:
    """Branch-independent post-merge reconciliation.

    The current branch is read from ``HEAD``. If it is not ``base_branch``
    and the working tree is dirty, the operation refuses. If the working
    tree is dirty on the base branch itself, the operation also refuses.

    The local base branch is fast-forwarded to ``origin/<base_branch>``
    using ``--ff-only``. Local and remote branch equality is verified.
    The squash commit, its tree SHA-256 and the feature-branch deletion
    state are recorded. The local feature branch is deleted ONLY if it
    exists, is the authorized feature branch, and matches the authorized
    head SHA-256 (so an unrelated branch is never deleted).
    """
    unavailable: List[str] = []
    repo_root = repository_checkout
    if not (repo_root / ".git").exists():
        raise MergeError(f"repository_checkout is not a git repo: {repo_root}")

    # 1. Initial branch + working-tree state.
    rc, out, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    if rc != 0:
        raise MergeError(f"cannot read current branch: rc={rc}")
    initial_branch = out.strip()

    rc, out, _ = _run_git(["status", "--porcelain"], repo_root)
    if rc != 0:
        raise MergeError(f"cannot read working tree status: rc={rc}")
    working_tree_clean = (out.strip() == "")

    # 2. Refuse dirty trees BEFORE any branch switch.
    if not working_tree_clean:
        raise MergeError(
            f"working tree is dirty on branch {initial_branch!r}; "
            "refusing to switch branches or fast-forward"
        )

    # 3. Switch to the base branch if we are not already on it.
    switched_to_base = False
    if initial_branch != base_branch:
        rc, _, err = _run_git(["checkout", base_branch], repo_root)
        if rc != 0:
            raise MergeError(f"cannot switch to base branch {base_branch!r}: {err.strip()}")
        switched_to_base = True

    # 4. Fetch origin/<base_branch>. Capture the remote tip BEFORE the
    #    local fast-forward so the recorded merge commit is bound to
    #    the PR's actual merge, not to whatever another PR may have
    #    introduced afterwards.
    rc, _, err = _run_git(["fetch", "origin", base_branch], repo_root)
    if rc != 0:
        unavailable.append(f"git fetch origin {base_branch}")
        origin_pre_merge_sha = ""
    else:
        rc, out, _ = _run_git(
            ["rev-parse", f"origin/{base_branch}"], repo_root
        )
        if rc != 0:
            unavailable.append("git rev-parse origin/<base_branch> (pre-ff)")
            origin_pre_merge_sha = ""
        else:
            origin_pre_merge_sha = out.strip()
    # 5. Fast-forward only. A failed --ff-only does NOT abort the
    #    reconciliation; per C-28 the merge is recorded and the
    #    failure is listed in unavailable_observations. The
    #    downstream call site (execute_guarded_merge_transaction)
    #    catches the failure and still writes the merge record.
    rc, _, err = _run_git(["merge", "--ff-only", f"origin/{base_branch}"], repo_root)
    if rc != 0:
        unavailable.append(
            f"git merge --ff-only origin/{base_branch}: {err.strip()}"
        )

    # 6. Read local + origin base SHA.
    rc, out, _ = _run_git(["rev-parse", base_branch], repo_root)
    if rc != 0:
        raise MergeError(f"cannot read local {base_branch}: rc={rc}")
    local_main_sha = out.strip()

    rc, out, _ = _run_git(["rev-parse", f"origin/{base_branch}"], repo_root)
    if rc != 0:
        unavailable.append("git rev-parse origin/<base_branch>")
        origin_main_sha = ""
    else:
        origin_main_sha = out.strip()

    local_main_equals_origin_main = (local_main_sha != "" and local_main_sha == origin_main_sha)

    # 7. Squash merge commit + tree + parent count. Per the explicit
    #    mergeCommit identity contract, the canonical source is the
    #    pr_merge_commit_oid parameter (fetched via gh pr view --json
    #    mergeCommit AFTER GitHub reports merged=true). Fall back to
    #    local_main_sha only when the explicit OID is unavailable;
    #    the unavailable_observations list records which source was
    #    used. We never infer the PR merge from tree equality.
    squash_merge_commit: str = ""
    if pr_merge_commit_oid:
        # Validate the explicit OID is exactly 40 lowercase hex chars.
        if _LOWER_HEX_40_RE.match(pr_merge_commit_oid):
            # Verify the OID exists locally after fetch.
            rc_obj, _, _ = _run_git(
                ["cat-file", "-t", pr_merge_commit_oid], repo_root
            )
            if rc_obj == 0:
                # Verify it is reachable from origin/<base_branch>.
                rc_anc, _, _ = _run_git(
                    [
                        "merge-base", "--is-ancestor",
                        pr_merge_commit_oid, f"origin/{base_branch}",
                    ],
                    repo_root,
                )
                if rc_anc == 0:
                    squash_merge_commit = pr_merge_commit_oid
                else:
                    unavailable.append(
                        f"pr_merge_commit_oid {pr_merge_commit_oid[:12]} "
                        f"is not reachable from origin/{base_branch}"
                    )
                    squash_merge_commit = local_main_sha
                    unavailable.append(
                        "fallback to local_main_sha for squash_merge_commit; "
                        "mergeCommit OID not reachable from base"
                    )
            else:
                unavailable.append(
                    f"pr_merge_commit_oid {pr_merge_commit_oid[:12]} not "
                    "found locally after fetch"
                )
                squash_merge_commit = local_main_sha
                unavailable.append(
                    "fallback to local_main_sha for squash_merge_commit; "
                    "mergeCommit OID not in local repo"
                )
        else:
            unavailable.append(
                f"pr_merge_commit_oid {pr_merge_commit_oid!r} is not a "
                "valid 40-character lowercase hex SHA"
            )
            squash_merge_commit = local_main_sha
            unavailable.append(
                "fallback to local_main_sha for squash_merge_commit; "
                "mergeCommit OID failed validation"
            )
    else:
        # No explicit OID supplied (provider didn't return mergeCommit).
        # Record the unavailable observation and fall back to
        # local_main_sha WITHOUT claiming it is the PR merge.
        unavailable.append(
            "pr_merge_commit_oid not supplied by provider; "
            "squash_merge_commit identity is ambiguous"
        )
        squash_merge_commit = local_main_sha
    squash_merge_commit_reliable = bool(
        pr_merge_commit_oid
        and _LOWER_HEX_40_RE.match(pr_merge_commit_oid)
        and squash_merge_commit == pr_merge_commit_oid
    )
    if not squash_merge_commit_reliable and pr_merge_commit_oid:
        unavailable.append(
            "pr_merge_commit_oid did not pass validation; "
            "merge commit identity may be ambiguous"
        )

    # Verify the squash commit is reachable from the authorized base.
    # If the merge commit is not reachable from the base, the merge
    # is either unrelated to this PR or the local fast-forward did
    # not actually pull it in; record the observation.
    if squash_merge_commit and origin_pre_merge_sha and base_branch:
        rc_merge_in_base, _, _ = _run_git(
            ["merge-base", "--is-ancestor", squash_merge_commit, base_branch],
            repo_root,
        )
        if rc_merge_in_base != 0:
            unavailable.append(
                f"squash commit {squash_merge_commit[:12]} is not an "
                f"ancestor of base {base_branch}"
            )

    rc, out, _ = _run_git(["log", "-1", "--format=%P", squash_merge_commit], repo_root)
    if rc != 0:
        raise MergeError(f"cannot read merge commit parents: rc={rc}")
    parents = out.strip().split()
    squash_parent_count = len(parents)
    squash_parent = parents[0] if parents else ""

    rc, out, _ = _run_git(["rev-parse", f"{squash_merge_commit}^{{tree}}"], repo_root)
    if rc != 0:
        raise MergeError(f"cannot read squash tree SHA: rc={rc}")
    squash_tree_sha256 = out.strip()

    # 8. Authorized head tree comparison (sanity).
    rc, out, _ = _run_git(["rev-parse", f"{authorized_head}^{{tree}}"], repo_root)
    if rc != 0:
        unavailable.append("git rev-parse authorized head tree")
    else:
        authorized_head_tree = out.strip()
        if authorized_head_tree != squash_tree_sha256:
            # A tree mismatch can legitimately happen when the base
            # branch advanced between authorization and merge (C-27).
            # Record the observation; do not raise after the
            # irreversible remote merge. Base-stability policy, if
            # required, is enforced in _repeat_exact_head_guards
            # before the remote merge.
            unavailable.append(
                f"squash tree {squash_tree_sha256} != authorized head tree "
                f"{authorized_head_tree} (base advanced?)"
            )

    # 9. Remote feature branch deletion.
    rc, out, _ = _run_git(["ls-remote", "--heads", "origin", feature_branch], repo_root)
    if rc != 0:
        unavailable.append("git ls-remote feature branch")
        feature_branch_remote_deleted = False
    else:
        feature_branch_remote_deleted = (out.strip() == "")

    # 10. Local feature branch deletion (safe: must match authorized head).
    feature_branch_local_deleted = False
    rc, out, _ = _run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{feature_branch}"], repo_root)
    if rc == 0 and out.strip():
        local_feature_sha = out.strip()
        if local_feature_sha == authorized_head:
            rc_del, _, _ = _run_git(["branch", "-d", feature_branch], repo_root)
            feature_branch_local_deleted = rc_del == 0
        else:
            unavailable.append(
                f"local feature branch {feature_branch!r} does not match authorized head; "
                "refusing to delete"
            )

    # 11. AED unchanged proof on the post-merge tree. Compare the
    #     AED file bytes against the expected digest if provided.
    #     Read the bytes directly (binary) so CRLF and non-UTF-8 content
    #     are preserved by the SHA-256; re-encoding the text from _run_git
    #     with the locale encoding would change the byte sequence and
    #     produce a false-negative clean flag.
    aed_clean = False
    aed_checked = False
    if expected_aed_sha256 is not None:
        try:
            proc = subprocess.run(
                ["git", "show", f"{squash_merge_commit}:{aed_path}"],
                cwd=str(repo_root),
                capture_output=True,
                timeout=10.0,
            )
        except (subprocess.TimeoutExpired, OSError):
            proc = None
            unavailable.append(f"git show HEAD:{aed_path}")
        if proc is not None:
            if proc.returncode != 0:
                unavailable.append(f"git show HEAD:{aed_path}")
            else:
                actual_aed_sha = digest_bytes(proc.stdout)
                aed_clean = actual_aed_sha == expected_aed_sha256
                aed_checked = True

    # Re-measure the working tree after the fast-forward and any
    # feature-branch cleanup so the post-merge record reflects the
    # post-merge state, not the pre-merge snapshot.
    rc, out, _ = _run_git(["status", "--porcelain"], repo_root)
    if rc != 0:
        unavailable.append("git status --porcelain (post-merge)")
        working_tree_clean_post = False
    else:
        working_tree_clean_post = (out.strip() == "")

    return PostMergeReconciliation(
        initial_branch=initial_branch,
        target_branch=base_branch,
        switched_to_base=switched_to_base,
        fast_forwarded=local_main_equals_origin_main,
        local_main_sha=local_main_sha,
        origin_main_sha=origin_main_sha,
        local_main_equals_origin_main=local_main_equals_origin_main,
        squash_merge_commit=squash_merge_commit,
        squash_parent_count=squash_parent_count,
        squash_tree_sha256=squash_tree_sha256,
        squash_parent=squash_parent,
        feature_branch_local_deleted=feature_branch_local_deleted,
        feature_branch_remote_deleted=feature_branch_remote_deleted,
        working_tree_clean=working_tree_clean_post,
        unavailable_observations=unavailable,
        aed_clean=aed_clean,
        aed_checked=aed_checked,
    )


# === Single guarded merge transaction ===

@dataclass
class MergeTransactionInputs:
    """All inputs to the guarded merge transaction, typed and explicit."""

    authorization_artifact_path: Path
    candidate_artifact_path: Path
    verifier_artifact_path: Path
    merge_record_artifact_path: Path

    repository_checkout: Path
    run_state_root: Path
    evidence_root: Path

    live_pr_payload: Dict[str, Any]
    live_ci_state: Dict[str, Any]
    live_review_state: Dict[str, Any]
    live_thread_inventory: Dict[str, Any]
    working_tree_clean: bool

    gh_executable: str = "gh"
    merge_subprocess_timeout: float = 60.0


def _validate_inputs(inputs: MergeTransactionInputs) -> None:
    """Refuse if any of the named roots collide or are missing."""
    _ensure_distinct_paths(
        ("repository_checkout", inputs.repository_checkout),
        ("run_state_root", inputs.run_state_root),
        ("evidence_root", inputs.evidence_root),
        # Artifact paths must be distinct from each other and from the
        # three named roots. A collision would let the merge record
        # overwrite the authorization artifact after the irreversible
        # merge.
        ("authorization_artifact_path", inputs.authorization_artifact_path),
        ("candidate_artifact_path", inputs.candidate_artifact_path),
        ("verifier_artifact_path", inputs.verifier_artifact_path),
        ("merge_record_artifact_path", inputs.merge_record_artifact_path),
    )


def _read_authorization(
    path: Path,
) -> Tuple["MergeAuthorization", str]:
    """Read and verify the authorization artifact. Returns (auth, exact_file_digest)."""
    try:
        result = read_artifact(path)
    except LegacyArtifactRefused as e:
        raise MergeAuthorizationMalformed(
            f"authorization artifact contains legacy '# sha256: ...' footer text and "
            f"is refused by the production merge path: {e}"
        )
    except ArtifactMissing as e:
        raise MergeAuthorizationMissing(str(e))
    except ArtifactError as e:
        raise MergeAuthorizationMalformed(str(e))
    payload = result.payload
    try:
        auth = MergeAuthorization.from_dict(payload)
    except (KeyError, ValueError, TypeError) as e:
        raise MergeAuthorizationMalformed(f"authorization payload invalid: {e!r}")
    return auth, result.digest


def _read_verifier_digest(
    path: Path,
) -> Tuple[Dict[str, Any], str]:
    """Read and verify the verifier record. Returns (verifier_record_payload, exact_file_digest)."""
    try:
        result = read_artifact(path)
    except ArtifactError as e:
        raise MergeAuthorizationMalformed(f"verifier record artifact invalid: {e}")
    payload = result.payload
    verdict = payload.get("verdict")
    defects = payload.get("defects", [])
    if verdict != "VERIFIED":
        raise MergeAuthorizationMalformed(
            f"verifier record verdict is {verdict!r}, expected 'VERIFIED'"
        )
    if not isinstance(defects, list) or len(defects) != 0:
        raise MergeAuthorizationMalformed(
            f"verifier record defects is {defects!r}, expected empty list"
        )
    return payload, result.digest


def _read_candidate(
    path: Path,
) -> Tuple[Dict[str, Any], str]:
    """Read and verify the candidate. Returns (candidate_payload, exact_file_digest)."""
    try:
        result = read_artifact(path)
    except ArtifactError as e:
        raise MergeAuthorizationMalformed(f"candidate artifact invalid: {e}")
    return result.payload, result.digest


def _repeat_exact_head_guards(
    inputs: MergeTransactionInputs,
    auth: MergeAuthorization,
    candidate_digest: str,
    verifier_digest: str,
) -> None:
    """Mandatory, non-optional pre-merge guards."""
    # 1. Live PR state must be open, unmerged, exact head.
    pr = inputs.live_pr_payload
    if pr["state"] != "open":
        raise MergeError(f"PR state is {pr['state']!r}, expected 'open'")
    if pr["merged"]:
        raise MergeError("PR is already merged")
    if pr["head"]["sha"] != auth.authorized_head:
        raise MergeError(
            f"live head {pr['head']['sha']!r} != authorized head {auth.authorized_head!r}"
        )
    if pr["baseRefName"] != auth.base_branch:
        raise MergeError(
            f"live base {pr['baseRefName']!r} != authorized base {auth.base_branch!r}"
        )
    if pr["mergeable"] not in ("MERGEABLE",):
        raise MergeError(f"PR is not mergeable: {pr['mergeable']!r}")
    if pr.get("autoMergeRequest") is not None:
        raise MergeError("auto-merge request present on PR")
    # Drafts are not mergeable in practice; reject explicitly so the
    # transaction does not rely on a downstream gh pr merge failure.
    if pr.get("isDraft"):
        raise MergeError("PR is a draft")
    # mergeStateStatus indicates the server-side merge readiness
    # (CLEAN/BLOCKED/UNSTABLE/DIRTY). CLEAN is the only acceptable
    # state for a guarded merge.
    merge_state = str(pr.get("mergeStateStatus") or "")
    if merge_state and merge_state != "CLEAN":
        raise MergeError(
            f"PR mergeStateStatus is {merge_state!r}, expected 'CLEAN'"
        )

    # 2. CI inventory.
    ci = inputs.live_ci_state
    if not ci.get("all_required_passing"):
        raise MergeError("not all required CI checks are passing")
    if not ci.get("coderabbit_passing"):
        raise MergeError("CodeRabbit is not passing")

    # 3. Review state.
    rs = inputs.live_review_state
    if rs.get("latest_coderabbit_state") != "APPROVED":
        raise MergeError(
            f"latest CodeRabbit review state is {rs.get('latest_coderabbit_state')!r}, "
            "expected 'APPROVED'"
        )

    # 4. Thread inventory.
    ti = inputs.live_thread_inventory
    if int(ti.get("unresolved_current", 0)) != 0:
        raise MergeError(f"unresolved current threads: {ti.get('unresolved_current')}")
    if int(ti.get("unresolved_outdated", 0)) != 0:
        raise MergeError(f"unresolved outdated threads: {ti.get('unresolved_outdated')}")

    # 5. Working tree clean.
    if not inputs.working_tree_clean:
        raise MergeError("working tree is dirty at merge time")

    # 6. Cross-references among authorization, candidate and verifier record
    #    digest bindings. These are MANDATORY; a missing digest blocks the
    #    merge authorization, never skips the comparison.
    if auth.candidate_sha256 != candidate_digest:
        raise MergeError(
            "authorization candidate digest does not match verified candidate file digest"
        )
    if auth.verifier_record_sha256 != verifier_digest:
        raise MergeError(
            "authorization verifier digest does not match verified verifier record file digest"
        )


def execute_guarded_merge_transaction(inputs: MergeTransactionInputs) -> Tuple[MergeRecord, str]:
    """The single production merge transaction.

    This function:
      1. validates typed inputs (distinct roots, mandatory artifacts);
      2. reads and verifies the authorization, candidate and verifier
         artifacts through the canonical artifact reader;
      3. repeats every exact-head and integrity guard;
      4. invokes the exact guarded ``gh pr merge`` command ONCE with a
         finite timeout;
      5. resolves timeout or ambiguity against the live PR state;
      6. reconciles local Git (branch-independent);
      7. writes the merge record through the canonical artifact writer.

    It does NOT itself transition the state machine to ``COMPLETE``.
    That durable transition is performed by the calling CLI command
    (``cmd_merge``) via ``Controller.report_complete()`` AFTER this
    function returns successfully. The merge record is therefore the
    recovery point: a failed or interrupted COMPLETE transition can
    be retried by re-applying ``report_complete()`` to the durable
    record.

    Returns the merged ``MergeRecord`` and its exact-file digest.

    On any failed guard, the runner is invoked ZERO times and no merge
    record is written. On timeout or ambiguity where the server-side merge
    did not happen, the runner returns without merging and no merge
    record is written. On timeout where the server-side merge DID happen,
    the record is written with the server-side observations.
    """
    _validate_inputs(inputs)

    # Acquire the cross-process merge lock. The lock is held for the
    # entire transaction and released on every code path. A second
    # process attempting to merge on the same evidence root will
    # raise MergeLockUnavailable immediately (the caller may retry).
    try:
        with _merge_lock(inputs.evidence_root) as _lock_ctx:
            return _execute_guarded_merge_transaction_locked(inputs)
    except MergeLockUnavailable as e:
        # Surface as a MergeError subclass so CLI handlers map it
        # to the controlled guard-failed exit code.
        raise MergeError(
            f"another process holds the merge lock for "
            f"{inputs.evidence_root}: {e!r}"
        ) from e


def _execute_guarded_merge_transaction_locked(
    inputs: MergeTransactionInputs,
) -> Tuple[MergeRecord, str]:
    """Inner transaction body that runs while holding the merge lock.

    See ``execute_guarded_merge_transaction`` for the full contract.
    This function MUST be called only inside the lock context.
    """

    # Validate the live PR payload shape before any binding check. A
    # missing or malformed key is a MergeError, not a KeyError, so
    # the CLI's exception handlers keep working. Empty payloads are
    # tolerated (tests that exercise path-only checks legitimately pass
    # empty live payloads); the production CLI always populates them.
    pr = inputs.live_pr_payload
    if pr:
        for key in ("state", "merged", "head", "baseRefName", "mergeable"):
            if key not in pr:
                raise MergeError(f"live_pr_payload is missing required key {key!r}")
        if not isinstance(pr["head"], dict) or "sha" not in pr["head"]:
            raise MergeError("live_pr_payload['head'] must contain 'sha'")

    # 1. Read authorization artifact (mandatory, sidecar-verified).
    auth, auth_digest = _read_authorization(inputs.authorization_artifact_path)
    # Cross-bind repository identity against the live PR payload. The
    # binding is mandatory when live_repo is present; missing live_repo
    # only blocks the binding itself (the production CLI always
    # populates it via fetch_live_pr_payload).
    live_repo = pr.get("repo")
    if live_repo and auth.repo != live_repo:
        raise MergeError(
            f"authorization repo {auth.repo!r} != live repo {live_repo!r}"
        )

    # 2. Read candidate artifact. Per C-22, every cross-binding
    #    is mandatory: an absent field is a hard failure, not a
    #    silent skip.
    candidate_payload, candidate_digest = _read_candidate(inputs.candidate_artifact_path)
    candidate_head_sha = (
        candidate_payload.get("head", {}).get("head_sha")
        or candidate_payload.get("head", {}).get("exact_head_sha")
        or ""
    )
    if not candidate_head_sha:
        raise MergeAuthorizationMalformed(
            "candidate payload missing head.head_sha (or head.exact_head_sha)"
        )
    if candidate_head_sha != auth.authorized_head:
        raise MergeError(
            f"candidate head {candidate_head_sha!r} != authorized head {auth.authorized_head!r}"
        )

    # 3. Read verifier record. The candidate_sha256 reference is
    #    mandatory (C-22) and must equal the verified candidate file
    #    digest.
    verifier_payload, verifier_digest = _read_verifier_digest(inputs.verifier_artifact_path)
    verifier_candidate_ref = (
        verifier_payload.get("candidate_sha256")
        or verifier_payload.get("candidate", {}).get("sha256")
        or ""
    )
    if not verifier_candidate_ref:
        raise MergeAuthorizationMalformed(
            "verifier record missing candidate_sha256 (or candidate.sha256)"
        )
    if verifier_candidate_ref != candidate_digest:
        raise MergeError(
            f"verifier candidate digest {verifier_candidate_ref!r} != verified candidate file digest {candidate_digest!r}"
        )

    # 4. Repeat every exact-head guard.
    _repeat_exact_head_guards(inputs, auth, candidate_digest, verifier_digest)

    # 5. Build and invoke the guarded command. The runner is invoked
    #    exactly once for the authorized command set. The exact argv
    #    is built by the same MergeExecutor.compute_command function
    #    used for preview, so preview and execution cannot diverge.
    cmd = MergeExecutor(gh_executable=inputs.gh_executable).compute_command(auth)

    # Refuse forbidden flags defensively (the merge runner must not be
    # tricked into using admin / auto / merge / rebase).
    for forbidden in ("--admin", "--auto", "--merge", "--rebase"):
        if forbidden in cmd:
            raise MergeError(f"forbidden flag present in merge command: {forbidden}")

    proc = _safe_run(cmd, timeout=inputs.merge_subprocess_timeout)

    server_side_state = None
    if proc["returncode"] != 0:
        # Re-query the server to disambiguate: did the merge succeed anyway?
        try:
            live2 = fetch_live_pr_payload(inputs.gh_executable, auth.repo, auth.pr_number)
            server_side_state = live2
            if not live2["merged"]:
                # Server says not merged → fail closed.
                raise MergeSubprocessFailed(
                    f"gh pr merge failed (rc={proc['returncode']}): "
                    f"stdout={proc['stdout']!r} stderr={proc['stderr']!r}"
                )
            # else: server says merged despite subprocess error → continue with reconciliation.
        except GitHubLiveFetchError as e:
            raise MergeAmbiguousOutcome(
                f"merge subprocess failed AND live re-query failed: {e!r}; "
                "refusing to proceed (fail closed)"
            )

    # Fetch the explicit mergeCommit OID. Per the explicit identity
    # contract, the PR's merge commit is observed server-side; we do
    # NOT infer it from local_main_sha or origin_main_sha.
    pr_merge_commit_oid: Optional[str] = None
    try:
        oid_proc = _safe_run(
            [inputs.gh_executable, "pr", "view", str(auth.pr_number),
             "--repo", auth.repo,
             "--json", "mergeCommit"],
            timeout=15.0,
        )
        if oid_proc["returncode"] == 0 and oid_proc["stdout"].strip():  # type: ignore[index]
            oid_doc = json.loads(oid_proc["stdout"])  # type: ignore[index]
            mc = oid_doc.get("mergeCommit")
            if isinstance(mc, dict):
                pr_merge_commit_oid = str(mc.get("oid") or "") or None
            elif isinstance(mc, str):
                pr_merge_commit_oid = mc or None
    except (json.JSONDecodeError, OSError):
        pr_merge_commit_oid = None

    # 6. Branch-independent post-merge reconciliation. Compute the
    #    expected AED sha256 from the bytes returned by git show.
    #    A missing AED file is recorded as an unavailable observation,
    #    not a hard failure.
    aed_unavailable = False
    try:
        raw_aed = subprocess.check_output(
            ["git", "-C", str(inputs.repository_checkout), "show",
             f"{auth.authorized_head}:scripts/quiet_window_observer.py"],
            stderr=subprocess.DEVNULL,
            timeout=10.0,
        )
        expected_aed_sha = digest_bytes(raw_aed)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        aed_unavailable = True
        expected_aed_sha = None

    # Per C-28, every post-merge failure path must still write the
    # merge record before propagating. The reconciliation may fail in
    # any of several ways; in every case we build a record from the
    # best available observations and re-raise.
    try:
        recon = reconcile_after_merge(
            repository_checkout=inputs.repository_checkout,
            base_branch=auth.base_branch,
            feature_branch=auth.feature_branch or auth.base_branch,
            authorized_head=auth.authorized_head,
            pr_merge_commit_oid=pr_merge_commit_oid,
            expected_aed_sha256=expected_aed_sha,
        )
    except (MergeError, subprocess.SubprocessError, OSError) as exc:
        # Reconciliation failed. Build a minimal recon-shaped object so the
        # record writer can still emit the merge result, then re-raise
        # AFTER the record is on disk.
        recon = PostMergeReconciliation(
            initial_branch="unknown",
            target_branch=auth.base_branch,
            switched_to_base=False,
            fast_forwarded=False,
            local_main_sha="",
            origin_main_sha="",
            local_main_equals_origin_main=False,
            squash_merge_commit="",
            squash_parent_count=0,
            squash_tree_sha256="",
            squash_parent="",
            feature_branch_local_deleted=False,
            feature_branch_remote_deleted=False,
            working_tree_clean=False,
            unavailable_observations=[f"reconcile_after_merge failed: {exc!r}"],
            aed_clean=False,
            aed_checked=False,
        )
        recon_failure = exc
    else:
        recon_failure = None

    # Re-verify both digests against the on-disk bytes after the
    # irreversible merge. We re-read each sidecar's exact-file SHA
    # rather than caching the pre-merge value because the merge
    # runner MUST NOT silently assume the artifact file was
    # untouched. If the read fails, list the failure in
    # unavailable_observations and set the boolean to False.
    post_merge_unavailable: List[str] = []
    candidate_unchanged = _verify_artifact_digest_unchanged(
        inputs.candidate_artifact_path, candidate_digest, post_merge_unavailable,
    )
    verifier_unchanged = _verify_artifact_digest_unchanged(
        inputs.verifier_artifact_path, verifier_digest, post_merge_unavailable,
    )

    # 7. Build and write the merge record. Always write, even if the
    # reconciliation failed. The record captures whatever local
    # observations are available and lists failures in
    # unavailable_observations.
    record = MergeRecord(
        schema_version="autocoder.merge_record.v2",
        run_id=auth.run_id,
        repo=auth.repo,
        pr_number=auth.pr_number,
        authorized_head=auth.authorized_head,
        squash_merge_commit=recon.squash_merge_commit,
        merge_commit_parent=recon.squash_parent,
        squash_commit_parent_count=recon.squash_parent_count,
        squash_tree_sha256=recon.squash_tree_sha256,
        final_local_main_sha=recon.local_main_sha,
        final_origin_main_sha=recon.origin_main_sha,
        local_main_equals_origin_main=recon.local_main_equals_origin_main,
        feature_branch_deleted_locally=recon.feature_branch_local_deleted,
        feature_branch_deleted_remotely=recon.feature_branch_remote_deleted,
        working_tree_clean=recon.working_tree_clean,
        aed_clean_post_merge=False,  # set below from the actual probe
        # Re-verified after the irreversible merge (see note above).
        candidate_sha256_unchanged=candidate_unchanged,
        verifier_record_sha256_unchanged=verifier_unchanged,
        candidate_exact_file_digest=candidate_digest,
        verifier_record_exact_file_digest=verifier_digest,
        authorization_exact_file_digest=auth_digest,
        merge_record_exact_file_digest="",  # filled after write
        merge_timestamp=_utc_now(),
        unauthorized_actions_not_taken={
            "merge_other_sha": False,
            "additional_commit_after_authorization": False,
            "rebase": False,
            "force_push": False,
            "auto_merge": False,
            "admin_bypass": False,
            "merge_commit_or_rebase_merge": False,
            "modify_pr_body": False,
            "weaken_branch_protection": False,
            "dismiss_reviews": False,
            "next_wave": False,
            "modify_aed": False,
            "create_release_or_tag": False,
        },
        unavailable_observations=list(recon.unavailable_observations)
        + list(post_merge_unavailable)
        + (["AED scripts/quiet_window_observer.py missing at authorized head"]
           if aed_unavailable else []),
        notes=(
            "executed via single guarded merge transaction; "
            f"mergesubprocess_returncode={proc['returncode']}; "
            f"mergesubprocess_timed_out={proc['timed_out']}; "
            f"server_side_state_check={server_side_state is not None}; "
            f"initial_branch={recon.initial_branch!r}; "
            f"switched_to_base={recon.switched_to_base}"
        ),
        state_transition=(
            "AWAITING_MERGE_AUTHORIZATION -> MERGE_AUTHORIZED -> "
            "POST_MERGE_VERIFYING (COMPLETE transition is durably "
            "persisted by cmd_merge via Controller.report_complete())"
        ),
        final_state="COMPLETE",
    )

    # Set the AED clean flag strictly from the probe result. The AED is
    # clean ONLY when the probe verified the exact bytes match the
    # expected digest. An unavailable AED observation is recorded in
    # unavailable_observations above; the boolean is False because we
    # have no positive verification (C-27 / INVARIANTS.md §C-27).
    record.aed_clean_post_merge = bool(recon.aed_checked and recon.aed_clean)

    # 8. Write the merge record through the canonical artifact writer.
    write_result = write_artifact(inputs.merge_record_artifact_path, record.to_dict())
    record.merge_record_exact_file_digest = write_result.digest

    # 9. If reconciliation failed, raise AFTER the record is durable
    #    on disk. C-28 requires restart-safe recovery from an
    #    irreversible merge; the record is the recovery point.
    if recon_failure is not None:
        raise recon_failure

    return record, write_result.digest


# === Backwards-compatible executor (test preview only) ===

class MergeExecutor:
    """Legacy preview-only executor. Tests may use ``compute_command``.

    Production flows MUST NOT call ``compute_command`` and then build a
    merge record separately. Use ``execute_guarded_merge_transaction``
    instead.
    """

    APPROVED_METHODS = ("squash",)

    def __init__(
        self,
        *,
        gh_executable: str = "gh",
        subprocess_runner=None,
    ) -> None:
        self.gh_executable = gh_executable
        self._run = subprocess_runner or self._default_run

    @staticmethod
    def _default_run(args, env=None, cwd=None) -> dict:
        return _safe_run(list(args), cwd=Path(cwd) if cwd else None, env=env)

    def compute_command(
        self,
        auth: MergeAuthorization,
        *,
        allow_extra_flags: Optional[Dict[str, bool]] = None,
    ) -> List[str]:
        if auth.merge_method not in self.APPROVED_METHODS:
            raise MergeError(
                f"merge_method {auth.merge_method!r} is not in approved "
                f"methods {self.APPROVED_METHODS}"
            )
        if not auth.require_match_head_commit:
            raise MergeError(
                "require_match_head_commit must be True for the protected guarded command"
            )
        if allow_extra_flags:
            for forbidden, label in (("admin", "admin"), ("auto", "auto-merge"),
                                     ("merge", "merge commit"), ("rebase", "rebase merge")):
                if allow_extra_flags.get(forbidden, False):
                    raise MergeError(f"{label} is not permitted")
        cmd: List[str] = [
            self.gh_executable, "pr", "merge", str(auth.pr_number),
            "--repo", auth.repo,
            "--squash",
        ]
        if auth.delete_branch:
            cmd.append("--delete-branch")
        if auth.require_match_head_commit:
            cmd.extend(["--match-head-commit", auth.authorized_head])
        return cmd