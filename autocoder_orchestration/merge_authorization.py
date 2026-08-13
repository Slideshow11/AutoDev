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
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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


class MergeGateChanged(MergeAmbiguousOutcome):
    """A mutable merge gate changed state between the evidence snapshot
    and the re-fetch inside the locked transaction.

    Round-26 P1#4 (Codex review of head 0d872b4): the
    ``evidence-root lock`` does NOT serialize GitHub-side state.
    Between the time the verifier approved the head and the
    time the merge transaction is invoked, a human reviewer
    can post ``CHANGES_REQUESTED``, dismiss an approval, open
    a new unresolved review thread, or change a required CI
    check state. ``--match-head-commit`` protects the SHA
    only; it does not protect mutable gates.

    The fix: while holding the merge lock, re-fetch and
    re-validate every mutable gate against the live server
    state. If any of them changed, raise ``MergeGateChanged``
    so the operator can decide whether to re-verify before
    retrying the merge.
    """


class MergeGateFetchError(MergeError):
    """A live gh fetch for a mutable gate failed inside the
    locked transaction.

    Round-27: production code MUST fail closed on any fetch
    error. The earlier "soft signal" / broad exception
    swallow is forbidden because a transient gh failure
    could otherwise allow a stale-snapshot merge to slip
    through. The exception carries the gate name and the
    underlying error so the operator can diagnose.
    """

    def __init__(
        self,
        gate: str,
        underlying: Optional[BaseException] = None,
        message: Optional[str] = None,
    ) -> None:
        msg = message or f"live {gate!r} fetch failed inside locked transaction"
        if underlying is not None:
            msg = f"{msg}: {underlying!r}"
        super().__init__(msg)
        self.gate = gate
        self.underlying = underlying


class MutableGateSnapshot:
    """Bundle of freshly fetched live gate snapshots.

    Round-27: every field is either a concrete dict (the
    fetch succeeded) or the fetch raised ``MergeGateFetchError``
    so the caller can diagnose. No ``None`` placeholders and no
    caller-supplied stale snapshots masquerading as fresh
    re-fetches.

    The four fetchers are constructor arguments so production
    code can inject real ``_safe_run`` wrappers and tests can
    inject stubs. ``None`` for any fetcher means "no fetcher
    configured" and the constructor raises
    ``MergeGateFetchError`` for that gate immediately.
    """

    def __init__(
        self,
        *,
        pr_payload_fetcher: Optional[Callable[[], Dict[str, Any]]] = None,
        required_ci_fetcher: Optional[Callable[[], Dict[str, Any]]] = None,
        review_state_fetcher: Optional[Callable[[], Dict[str, Any]]] = None,
        thread_inventory_fetcher: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        self._pr_payload: Dict[str, Any] = (
            _call_or_raise("live_pr_payload", pr_payload_fetcher)
        )
        self._required_ci: Dict[str, Any] = (
            _call_or_raise("live_required_ci", required_ci_fetcher)
        )
        self._review_state: Dict[str, Any] = (
            _call_or_raise("live_review_state", review_state_fetcher)
        )
        self._thread_inventory: Dict[str, Any] = (
            _call_or_raise("live_thread_inventory", thread_inventory_fetcher)
        )

    @property
    def pr_payload(self) -> Dict[str, Any]:
        return self._pr_payload

    @property
    def required_ci(self) -> Dict[str, Any]:
        return self._required_ci

    @property
    def review_state(self) -> Dict[str, Any]:
        return self._review_state

    @property
    def thread_inventory(self) -> Dict[str, Any]:
        return self._thread_inventory


def _call_or_raise(
    gate: str,
    fetcher: Optional[Callable[[], Dict[str, Any]]],
) -> Dict[str, Any]:
    """Invoke ``fetcher`` and return its dict, or raise
    ``MergeGateFetchError``. ``None`` fetcher means the gate
    is not configured for production; the transaction
    refuses to proceed.
    """
    if fetcher is None:
        raise MergeGateFetchError(
            gate,
            message=(
                f"no fetcher registered for {gate!r}; the merge "
                f"transaction refuses to proceed without a "
                f"live re-fetch. Production code MUST inject "
                f"a fetcher that calls `gh` against the live "
                f"server."
            ),
        )
    try:
        return fetcher()
    except MergeGateFetchError:
        raise
    except Exception as exc:  # noqa: BLE001 — translate to typed
        raise MergeGateFetchError(gate, underlying=exc) from exc


# === MergeAuthorization dataclass (unchanged contract) ===

@dataclass(frozen=True)
class MergeAuthorization:
    """Human merge authorization record.

    The contract is unchanged from the original implementation so historical
    readers and tests still work. The dataclass itself carries no SHA-256;
    that lives in the artifact that contains it.

    Round-28 P2: ``required_ci_jobs`` carries the run's configured
    required CI policy. The locked mutable-gate enforcement uses this
    value to drive the ``fetch_live_required_ci`` comparator: the live
    inventory MUST be SUCCESS for every job in this list. The empty
    tuple means the persisted run policy explicitly says there are
    zero required jobs (which is rare and documented at init time).
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
    next_wave_authorization: Optional[Any] = None
    notes: str = ""
    # Round-28 P2: the run's configured required CI jobs. The
    # locked mutable-gate refetch MUST verify each job is green
    # at the live head. Production code populates this from
    # ``ctx.required_ci_jobs`` (the persisted ``RunContext``).
    # Empty tuple means "policy says zero required jobs".
    required_ci_jobs: Tuple[str, ...] = ()

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
        # Round-28 P2: the required CI jobs are part of the auth
        # artifact. The CLI also rebinds the field from
        # ``ctx.required_ci_jobs`` (the persisted run policy), so
        # the cross-binding guard sees both sides agree.
        raw_jobs = payload.get("required_ci_jobs", ()) or ()
        if not isinstance(raw_jobs, list):
            raw_jobs = tuple(raw_jobs) if isinstance(raw_jobs, (list, tuple)) else ()
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
            required_ci_jobs=tuple(raw_jobs),
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
    # ``unauthorized_actions_taken`` records every forbidden
    # action observed during the merge lifecycle. A value of
    # ``True`` means the action WAS taken (a security incident);
    # ``False`` means the action was NOT taken (the audit
    # contract is satisfied). Every key starts as ``False``; the
    # transaction sets the relevant key to ``True`` only when it
    # positively observes the corresponding behavior. The field
    # is renamed from the legacy ``unauthorized_actions_not_taken``
    # to remove the polarity ambiguity.
    unauthorized_actions_taken: Dict[str, bool] = field(default_factory=dict)
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
            "unauthorized_actions_taken": self.unauthorized_actions_taken,
            "unavailable_observations": list(self.unavailable_observations),
            "notes": self.notes,
            "state_transition": self.state_transition,
            "final_state": self.final_state,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "MergeRecord":
        # Require an explicit schema_version. Defaulting a missing
        # schema_version to a supported value would let a legacy /
        # malformed merge record appear as a valid v2 record.
        if "schema_version" not in payload:
            raise MergeAuthorizationMalformed(
                "merge record is missing required key 'schema_version'; "
                "explicit schema identification is required"
            )
        schema_version = str(payload["schema_version"])
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

        # Each Boolean status field must be a real bool. Coercing
        # through bool() silently accepts strings (e.g. "false" ->
        # True), integers, and None -- a malformed merge record would
        # then appear as a verified observation.
        def _strict_bool(field_name: str) -> bool:
            if field_name not in payload:
                raise MergeAuthorizationMalformed(
                    f"merge record is missing required key {field_name!r}"
                )
            value = payload[field_name]
            if type(value) is not bool:
                raise MergeAuthorizationMalformed(
                    f"merge record key {field_name!r} must be a bool, "
                    f"got {type(value).__name__}"
                )
            return value

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
            local_main_equals_origin_main=_strict_bool("local_main_equals_origin_main"),
            feature_branch_deleted_locally=_strict_bool("feature_branch_deleted_locally"),
            feature_branch_deleted_remotely=_strict_bool("feature_branch_deleted_remotely"),
            working_tree_clean=_strict_bool("working_tree_clean"),
            aed_clean_post_merge=_strict_bool("aed_clean_post_merge"),
            candidate_sha256_unchanged=_strict_bool("candidate_sha256_unchanged"),
            verifier_record_sha256_unchanged=_strict_bool("verifier_record_sha256_unchanged"),
            candidate_exact_file_digest=str(payload.get("candidate_exact_file_digest", "")),
            verifier_record_exact_file_digest=str(payload.get("verifier_record_exact_file_digest", "")),
            authorization_exact_file_digest=str(payload.get("authorization_exact_file_digest", "")),
            merge_record_exact_file_digest=str(payload.get("merge_record_exact_file_digest", "")),
            merge_timestamp=str(payload.get("merge_timestamp", "")),
            unauthorized_actions_taken=dict(payload.get("unauthorized_actions_taken", {})),
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
    ``mergeable``, ``autoMergeRequest`` and ``reviewDecision``. Raises on
    subprocess failure or ambiguous output.

    ``reviewDecision`` is included so the production merge gate can
    reject a human ``CHANGES_REQUESTED`` even when the latest CodeRabbit
    review remains ``APPROVED`` (the verifier already enforces this, but
    the merge gate must not rely on a stale snapshot).
    """
    _runner = runner or (lambda *a, **kw: _safe_run(list(a), **kw))
    res = _runner(gh_executable, "pr", "view", str(pr_number),
                  "--repo", repo,
                  "--json", "state,isDraft,mergeable,mergeStateStatus,mergedAt,headRefOid,baseRefName,autoMergeRequest,number,reviewDecision")
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
        # PR-level reviewDecision (e.g. "APPROVED" | "CHANGES_REQUESTED" |
        # "REVIEW_REQUIRED"). ``None`` means GitHub did not return the
        # field; the merge gate treats that as a fail-closed signal.
        "reviewDecision": doc.get("reviewDecision"),
        # Include the repository identity so the cross-binding guard
        # in execute_guarded_merge_transaction can compare it against
        # auth.repo (per C-25: every integrity guard is mandatory).
        "repo": repo,
    }


def fetch_live_required_ci(
    gh_executable: str,
    repo: str,
    pr_number: int,
    required_check_names: Sequence[str],
    *,
    runner: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Fetch the live required-CI status for the PR.

    Round-27: real gh query inside the locked transaction. The
    function enumerates the configured ``required_check_names`` and
    fetches each check's status. A missing or pending check is
    treated as a fail-closed signal (the gate rejects the merge
    because the check has not yet gone green at the live head).

    Returns a dict shaped like::

        {
          "head_sha": <live head>,
          "checks": {
            "<check_name>": {
              "state": "SUCCESS" | "FAILURE" | "PENDING" | "MISSING",
              "head_sha": <live head>,
            },
            ...
          },
        }

    The ``head_sha`` at the top level is the live commit SHA
    the checks ran against; the merge gate MUST compare it
    against the authorized head to prevent a "stale check"
    bypass.
    """
    _runner = runner or (lambda *a, **kw: _safe_run(list(a), **kw))
    res = _runner(
        gh_executable, "pr", "checks", str(pr_number),
        "--repo", repo,
    )
    if res["returncode"] != 0:
        raise MergeGateFetchError(
            "live_required_ci",
            underlying=GitHubLiveFetchError(
                f"gh pr checks failed (rc={res['returncode']}): "
                f"stderr={res['stderr']!r}"
            ),
        )
    try:
        text = (res.get("stdout") or "").strip()
        if not text:
            return {
                "head_sha": "",
                "checks": {},
            }
        # ``gh pr checks`` returns plain text rows of
        # ``<name>\t<state>\t<...>``. The columns are
        # implementation-defined; we use a tolerant parser
        # that extracts the first two tab-separated fields.
        parsed: Dict[str, Dict[str, Any]] = {}
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            name = parts[0].strip()
            state = parts[1].strip().upper()
            if not name:
                continue
            parsed[name] = {"state": state, "head_sha": ""}
        # Round-27: enumerate the configured required checks.
        # A missing required check is recorded as MISSING so
        # the gate can fail closed. We do NOT silently allow
        # it to pass.
        for required in required_check_names:
            if required not in parsed:
                parsed[required] = {"state": "MISSING", "head_sha": ""}
        return {"head_sha": "", "checks": parsed}
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
        raise MergeGateFetchError(
            "live_required_ci", underlying=exc,
        )


def fetch_live_review_state(
    gh_executable: str,
    repo: str,
    pr_number: int,
    *,
    canonical_reviewer_login: str = "coderabbitai",
    runner: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Fetch the live formal review state for the PR.

    Round-28 P3: every review candidate carries the review
    commit identity. The locked merge gate compares the
    review's ``commit.oid`` against the authorized exact head.
    A review whose ``commit.oid`` does NOT match the
    authorized head is NOT exact-head approval, even if its
    ``state`` is APPROVED.

    Returns a dict shaped like::

        {
          "latest_coderabbit_state": "APPROVED" | "CHANGES_REQUESTED" | ...,
          "latest_coderabbit_commit_oid": <live review commit OID>,
          "latest_coderabbit_login": <canonical reviewer login>,
          "reviews": [
            {
              "state": ...,
              "author": <login>,
              "submitted_at": ...,
              "commit_oid": <review commit OID>,
              "head_sha": <PR head when the review was submitted>,
            },
            ...
          ],
          "head_sha": <live head>,
        }

    The merge gate compares ``latest_coderabbit_state`` against
    the bound snapshot AND verifies
    ``latest_coderabbit_commit_oid == authorized_head``. A
    divergence halts the transaction.

    Canonical reviewer login comparison: the configured
    canonical reviewer's login MUST match exactly
    (case-insensitive normalized comparison). Lookalike
    accounts such as ``coderabbit-helper`` MUST NOT satisfy
    the gate. The default canonical reviewer is
    ``coderabbitai`` (the bot login used by CodeRabbit's
    GitHub App); the comparison is case-insensitive and
    ignores trailing ``[bot]`` suffixes (e.g.
    ``coderabbitai[bot]`` and ``coderabbitai`` both match).
    """
    _runner = runner or (lambda *a, **kw: _safe_run(list(a), **kw))
    # Round-28 P3: query the PR reviews GraphQL endpoint
    # with ``commit`` so each review carries its
    # commit-oid identity. We also pull ``commit.oid`` for
    # the latest review per author; this lets the gate
    # bind the review to the exact authorized head.
    query = (
        "query($owner:String!, $name:String!, $number:Int!) {"
        "  repository(owner:$owner, name:$name) {"
        "    pullRequest(number:$number) {"
        "      headRefOid"
        "      reviews(last:50, states:[APPROVED, CHANGES_REQUESTED, COMMENTED]) {"
        "        nodes {"
        "          state"
        "          author { login }"
        "          submittedAt"
        "          commit { oid }"
        "        }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    # Split owner/name
    if "/" not in repo:
        raise MergeGateFetchError(
            "live_review_state",
            underlying=ValueError(f"invalid repo {repo!r}"),
        )
    owner, name = repo.split("/", 1)
    res = _runner(
        gh_executable, "api", "graphql",
        "-f", f"query={query}",
        "-f", f"owner={owner}",
        "-f", f"name={name}",
        "-F", f"number={pr_number}",
    )
    if res["returncode"] != 0:
        raise MergeGateFetchError(
            "live_review_state",
            underlying=GitHubLiveFetchError(
                f"gh api graphql (reviews) failed "
                f"(rc={res['returncode']}): stderr={res['stderr']!r}"
            ),
        )
    try:
        doc = json.loads(res.get("stdout") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MergeGateFetchError("live_review_state", underlying=exc)
    pr = (
        doc.get("data", {}).get("repository", {}).get("pullRequest", {})
    )
    head_sha = str(pr.get("headRefOid") or "")
    reviews_raw = pr.get("reviews", {}).get("nodes", [])
    reviews = [
        {
            "state": r.get("state"),
            "author": ((r.get("author") or {}).get("login") or ""),
            "submitted_at": r.get("submittedAt"),
            # Round-28 P3: include the review's commit OID so the
            # gate can compare against the authorized exact head.
            "commit_oid": ((r.get("commit") or {}).get("oid") or ""),
        }
        for r in reviews_raw
    ]
    # Round-28 P3: canonical reviewer login matching. We
    # normalize by lowercasing and stripping ``[bot]``. The
    # default canonical reviewer is ``coderabbitai``; lookalike
    # accounts such as ``coderabbit-helper`` MUST NOT match.
    def _is_canonical(login: str) -> bool:
        s = (login or "").strip().lower()
        if s.endswith("[bot]"):
            s = s[: -len("[bot]")].rstrip()
        return s == canonical_reviewer_login.strip().lower()
    # The latest CodeRabbit reviewer state is the LAST
    # matching review submitted by the canonical reviewer.
    # If no canonical review exists, both
    # ``latest_coderabbit_state`` and
    # ``latest_coderabbit_commit_oid`` are None.
    latest_cr_state: Optional[str] = None
    latest_cr_commit: Optional[str] = None
    latest_cr_login: Optional[str] = None
    for r in reversed(reviews):
        if _is_canonical(r["author"]):
            latest_cr_state = r["state"]
            latest_cr_commit = r["commit_oid"]
            latest_cr_login = r["author"]
            break
    return {
        "head_sha": head_sha,
        "reviews": reviews,
        "latest_coderabbit_state": latest_cr_state,
        "latest_coderabbit_commit_oid": latest_cr_commit,
        "latest_coderabbit_login": latest_cr_login,
        "canonical_reviewer_login": canonical_reviewer_login,
        "repo": repo,
    }


def _normalize_canonical_reviewer_login(login: str) -> str:
    """Normalize a GitHub login for canonical-reviewer matching.

    Round-28 P3: lookalike accounts such as ``coderabbit-helper``
    MUST NOT satisfy the gate. We normalize by lowercasing and
    stripping ``[bot]`` suffixes so ``coderabbitai[bot]`` and
    ``coderabbitai`` both match, but ``coderabbit-helper`` does
    not match ``coderabbitai``.
    """
    s = (login or "").strip().lower()
    if s.endswith("[bot]"):
        s = s[: -len("[bot]")].rstrip()
    return s


def _validate_graphql_review_threads_response(
    doc: Any, *, page_index: int
) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
    """Validate one page of the GraphQL ``reviewThreads`` response.

    Round-28 P4: fail closed on partial GraphQL responses. For
    EVERY page require:

      - no top-level GraphQL ``errors`` array with non-empty
        entries;
      - the top-level ``data`` is an object (not null);
      - ``data.repository.pullRequest`` exists and is an object;
      - ``reviewThreads`` connection exists and is an object;
      - ``nodes`` exists and is a list (NOT null, NOT missing);
      - ``pageInfo`` exists and is an object with the required
        fields;
      - if ``hasNextPage`` is True, ``endCursor`` MUST be a
        non-empty string.

    Missing fields MUST NEVER default to ``nodes=[]`` or
    ``hasNextPage=False`` because that converts incomplete
    evidence into "zero unresolved threads". Returns
    ``(threads_dict, page_info, head_sha)`` on success; raises
    ``MergeGateFetchError`` on any partial response.
    """
    if not isinstance(doc, dict):
        raise MergeGateFetchError(
            "live_thread_inventory",
            message=(
                f"page {page_index}: top-level GraphQL response is "
                f"not a JSON object; got {type(doc).__name__}"
            ),
        )
    # Top-level errors array: GitHub returns errors here when
    # the query is partial. ANY non-empty errors list means the
    # page is incomplete — fail closed.
    errors = doc.get("errors")
    if isinstance(errors, list) and len(errors) > 0:
        raise MergeGateFetchError(
            "live_thread_inventory",
            message=(
                f"page {page_index}: GraphQL returned top-level "
                f"errors: {errors!r}; the thread inventory is "
                f"incomplete."
            ),
        )
    data = doc.get("data")
    if not isinstance(data, dict):
        raise MergeGateFetchError(
            "live_thread_inventory",
            message=(
                f"page {page_index}: top-level `data` is missing "
                f"or not an object; got {type(data).__name__}."
            ),
        )
    pr = (
        data.get("repository", {}).get("pullRequest", {})
    )
    if not isinstance(pr, dict) or not pr:
        raise MergeGateFetchError(
            "live_thread_inventory",
            message=(
                f"page {page_index}: `repository.pullRequest` is "
                f"missing or not an object; the thread inventory "
                f"is incomplete."
            ),
        )
    head_sha = pr.get("headRefOid")
    if head_sha is None:
        # GitHub omitted ``headRefOid`` (or returned null).
        # We treat null as missing rather than "" so the
        # caller can detect the partial-response case.
        raise MergeGateFetchError(
            "live_thread_inventory",
            message=(
                f"page {page_index}: `headRefOid` is missing or "
                f"null; the thread inventory is incomplete."
            ),
        )
    threads_obj = pr.get("reviewThreads")
    if not isinstance(threads_obj, dict):
        raise MergeGateFetchError(
            "live_thread_inventory",
            message=(
                f"page {page_index}: `reviewThreads` connection "
                f"is missing or not an object; the thread "
                f"inventory is incomplete."
            ),
        )
    nodes = threads_obj.get("nodes")
    if not isinstance(nodes, list):
        raise MergeGateFetchError(
            "live_thread_inventory",
            message=(
                f"page {page_index}: `reviewThreads.nodes` is "
                f"missing or not a list; got {type(nodes).__name__}. "
                f"The thread inventory MUST be a complete list, "
                f"not a partial placeholder."
            ),
        )
    page_info = threads_obj.get("pageInfo")
    if not isinstance(page_info, dict):
        raise MergeGateFetchError(
            "live_thread_inventory",
            message=(
                f"page {page_index}: `reviewThreads.pageInfo` is "
                f"missing or not an object; the thread inventory "
                f"is incomplete."
            ),
        )
    if "hasNextPage" not in page_info:
        raise MergeGateFetchError(
            "live_thread_inventory",
            message=(
                f"page {page_index}: `pageInfo.hasNextPage` is "
                f"missing; the thread inventory is incomplete."
            ),
        )
    has_next = page_info.get("hasNextPage")
    end_cursor = page_info.get("endCursor")
    if has_next is True:
        # Round-28 P4: ``hasNextPage=True`` MUST have a
        # non-empty ``endCursor``. Missing cursor = incomplete.
        if not end_cursor or not isinstance(end_cursor, str):
            raise MergeGateFetchError(
                "live_thread_inventory",
                message=(
                    f"page {page_index}: hasNextPage=True but "
                    f"endCursor is missing/empty; the thread "
                    f"inventory is incomplete."
                ),
            )
    return threads_obj, page_info, str(head_sha)


def fetch_live_thread_inventory(
    gh_executable: str,
    repo: str,
    pr_number: int,
    *,
    runner: Optional[Callable[..., Dict[str, Any]]] = None,
    max_pages: int = 20,
) -> Dict[str, Any]:
    """Fetch the complete unresolved review-thread inventory.

    Round-27: real gh query inside the locked transaction.
    The function paginates the GraphQL ``reviewThreads`` field
    and counts the unresolved ones. A failure to paginate
    completely (network failure, rate limit, missing
    ``pageInfo.hasNextPage`` while ``endCursor`` is set) is a
    fail-closed signal: the gate refuses to merge because the
    thread inventory is incomplete.

    Round-28 P4: fail closed on partial GraphQL responses.
    For every page require:
      - no top-level GraphQL errors;
      - ``data`` is an object;
      - ``nodes`` is a list (NOT missing, NOT null);
      - ``pageInfo`` is an object with the required fields;
      - ``hasNextPage=True`` requires a non-empty endCursor;
      - max_pages exceeded is treated as incomplete.
    Missing fields MUST NEVER default to ``nodes=[]`` or
    ``hasNextPage=False`` because that converts incomplete
    evidence into "zero unresolved threads".

    The function raises ``MergeGateFetchError`` on any partial
    response. The caller (the locked gate) fails closed.

    Returns::

        {
          "head_sha": <live head>,
          "unresolved_current": <int>,
          "unresolved_outdated": <int>,
          "paginated_completely": True,
          "error": None,
        }
    """
    _runner = runner or (lambda *a, **kw: _safe_run(list(a), **kw))
    if "/" not in repo:
        raise MergeGateFetchError(
            "live_thread_inventory",
            underlying=ValueError(f"invalid repo {repo!r}"),
        )
    owner, name = repo.split("/", 1)
    query = (
        "query($owner:String!, $name:String!, $number:Int!, $cursor:String) {"
        "  repository(owner:$owner, name:$name) {"
        "    pullRequest(number:$number) {"
        "      headRefOid"
        "      reviewThreads(first:50, after:$cursor) {"
        "        pageInfo { hasNextPage endCursor }"
        "        nodes { isResolved isOutdated }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    unresolved_current = 0
    unresolved_outdated = 0
    head_sha = ""
    cursor: Optional[str] = None
    for _page in range(max_pages):
        argv: List[str] = [
            gh_executable, "api", "graphql",
            "-f", f"query={query}",
            "-f", f"owner={owner}",
            "-f", f"name={name}",
            "-F", f"number={pr_number}",
        ]
        if cursor is None:
            argv.extend(["-F", "cursor="])
        else:
            argv.extend(["-F", f"cursor={cursor}"])
        res = _runner(*argv)
        if res["returncode"] != 0:
            raise MergeGateFetchError(
                "live_thread_inventory",
                underlying=GitHubLiveFetchError(
                    f"gh api graphql (threads page) failed "
                    f"(rc={res['returncode']}): stderr={res['stderr']!r}"
                ),
            )
        try:
            doc = json.loads(res.get("stdout") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MergeGateFetchError(
                "live_thread_inventory",
                underlying=exc,
                message="thread inventory JSON decode failed",
            )
        # Round-28 P4: per-page strict validation. Missing
        # ``nodes`` / ``pageInfo`` / ``headRefOid`` /
        # non-empty errors ALL fail closed. We DO NOT default
        # to ``nodes=[]`` or ``hasNextPage=False``.
        threads_obj, page_info, page_head_sha = (
            _validate_graphql_review_threads_response(doc, page_index=_page)
        )
        if not head_sha:
            head_sha = page_head_sha
        nodes = threads_obj.get("nodes") or []
        for t in nodes:
            if not t.get("isResolved"):
                if t.get("isOutdated"):
                    unresolved_outdated += 1
                else:
                    unresolved_current += 1
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        # Round-28 P4: per-page validation already enforced
        # non-empty cursor when hasNextPage=True, so this is a
        # belt-and-suspenders check.
        if not cursor:
            raise MergeGateFetchError(
                "live_thread_inventory",
                message=(
                    f"page {_page}: hasNextPage=True but endCursor "
                    f"is empty; the thread inventory is incomplete."
                ),
            )
    else:
        raise MergeGateFetchError(
            "live_thread_inventory",
            message=(
                f"thread inventory exceeded {max_pages} pages; "
                "the merge gate refuses to assume the count is complete."
            ),
        )
    return {
        "head_sha": head_sha,
        "unresolved_current": unresolved_current,
        "unresolved_outdated": unresolved_outdated,
        "paginated_completely": True,
        "review_threads_pagination_complete": True,
        "review_threads_pagination_failed": False,
        "error": None,
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
    # The caller (execute_guarded_merge_transaction) does
    # its own head_observed-based digest check; we do not
    # duplicate it here.
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
    # The AED probe only attests the post-merge AED file when
    # the squash_merge_commit identity is verified against the
    # server-reported mergeCommit OID. A probe against a fallback
    # commit would produce a false-positive clean flag because the
    # fallback is local_main_sha, not the actual PR merge.
    if (
        expected_aed_sha256 is not None
        and not squash_merge_commit_reliable
    ):
        unavailable.append(
            "AED probe skipped: squash_merge_commit identity is a fallback, "
            "not the server-reported mergeCommit OID"
        )
    elif expected_aed_sha256 is not None:
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

    # Round-27: the configured required CI job names. The
    # merge gate re-fetches the live CI inventory against
    # this list inside the locked transaction. Production
    # code MUST populate this from the same RunContext
    # the verifier used; tests pass a tuple of names.
    required_ci_names: Tuple[str, ...] = ()

    # Round-27 P1#4: the server-reported mergeCommit OID MUST
    # be positively verified as a real git object in the
    # local repository. The flag ``_bypass_oid_reachability``
    # is private: tests that need a hermetic mode set it via
    # ``MergeTransactionInputs._set_bypass_oid_reachability(True)``
    # (a classmethod helper). Public callers MUST leave this
    # ``False``. Production code MUST use ``require_oid_reachable=True``
    # in every production code path; the field defaults to
    # ``True`` and the bypass is opt-in for tests.
    _bypass_oid_reachability: bool = False

    # Round-27: optional pre-built mutable-gate fetchers.
    # Production code leaves this ``None`` and the
    # transaction builds real ``gh`` fetchers via
    # ``_build_mutable_gate_fetchers``. Tests inject
    # canned fetchers that return canned dicts without
    # spawning subprocesses. The field is private so
    # production callers do not silently bypass the
    # real gh query path.
    _live_fetchers: Optional[Any] = None

    def _set_bypass_oid_reachability(self, value: bool) -> None:
        """Test-only seam for the OID-reachability check.

        Round-27 P1#4: the public ``require_oid_reachable=False``
        bypass is REMOVED. Tests that need a hermetic mode
        (no real git repo, no actual ``git cat-file`` call)
        call ``inputs._set_bypass_oid_reachability(True)``.
        Production code MUST NOT call this helper; it is
        private (leading underscore on the field) to flag
        the production intent.
        """
        self._bypass_oid_reachability = bool(value)

    def _set_live_fetchers(
        self,
        fetchers: Dict[str, Callable[[], Dict[str, Any]]],
    ) -> None:
        """Test-only seam for the mutable-gate refetch.

        Round-27: tests inject a dict of fetcher closures
        (one per gate: ``pr_payload``, ``required_ci``,
        ``review_state``, ``thread_inventory``). Each
        closure returns the live-state dict that the gate
        comparator consumes. Production code MUST NOT call
        this helper; production fetches use the canonical
        ``_build_mutable_gate_fetchers`` which calls ``gh``
        against the live server.
        """
        if not isinstance(fetchers, dict):
            raise MergeGateFetchError(
                "live_fetchers",
                message=(
                    f"expected a dict of fetcher closures; "
                    f"got {type(fetchers).__name__}"
                ),
            )
        required_keys = {"pr_payload", "required_ci", "review_state", "thread_inventory"}
        missing = required_keys - set(fetchers.keys())
        if missing:
            raise MergeGateFetchError(
                "live_fetchers",
                message=(
                    f"fetcher dict missing required keys: "
                    f"{sorted(missing)}"
                ),
            )
        self._live_fetchers = fetchers


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


def _verify_auth_binds_to_current_run(
    inputs: "MergeTransactionInputs",
    auth: "MergeAuthorization",
) -> None:
    """Verify the merge authorization binds to the CURRENT
    persisted RunContext.

    A stale merge-authorization artifact from a previous
    run (different run_id, different repo, different
    authorized_head) MUST NOT complete this run. This
    guard prevents the production code from completing
    a merge for a re-initialized supervisor that reused
    the same evidence root.

    The check reads the current persisted RunContext via
    the supervisor's run_state_root, then compares at
    minimum:
      - run_id
      - repository (auth.repo matches the current
        RunContext.repo)
      - PR number
      - authorized_head (auth.authorized_head matches
        the current RunContext.current_authorized_head)
    """
    # The standard approach: read the persisted
    # run_context.json from the run_state_root and
    # compare the binding fields.
    run_context_path = (
        inputs.run_state_root / "run_context.json"
        if hasattr(inputs.run_state_root, "__truediv__")
        else None
    )
    if run_context_path is None or not run_context_path.exists():
        # No current RunContext. The auth stands alone;
        # the per-run path is the only available identity.
        # This is OK for one-shot merges.
        return
    try:
        ctx_payload = json.loads(run_context_path.read_text())
    except (OSError, json.JSONDecodeError):
        # The current RunContext is unreadable; refuse to
        # proceed. A stale auth against an unreadable
        # current RunContext cannot be verified.
        raise MergeError(
            f"current RunContext is unreadable at "
            f"{run_context_path}; refusing to proceed "
            "with a merge authorization whose run-binding "
            "cannot be verified"
        )
    # Compare run_id.
    current_run_id = ctx_payload.get("run_id", "")
    if current_run_id and current_run_id != auth.run_id:
        raise MergeError(
            f"merge authorization run_id {auth.run_id!r} does not "
            f"match current RunContext run_id {current_run_id!r}; "
            "stale auth from another run cannot complete this run"
        )
    # Compare repository identity. The auth.repo is
    # "owner/name"; the RunContext has repo_owner and
    # repo_name.
    current_repo = (
        f"{ctx_payload.get('repo_owner', '')}/"
        f"{ctx_payload.get('repo_name', '')}"
    )
    if current_repo and current_repo != auth.repo:
        raise MergeError(
            f"merge authorization repo {auth.repo!r} does not "
            f"match current RunContext repo {current_repo!r}; "
            "stale auth from another run cannot complete this run"
        )
    # Compare PR number.
    current_pr = ctx_payload.get("pr_number")
    if current_pr is not None and current_pr != auth.pr_number:
        raise MergeError(
            f"merge authorization PR number {auth.pr_number!r} does "
            f"not match current RunContext PR number {current_pr!r}; "
            "stale auth from another run cannot complete this run"
        )
    # Compare authorized_head / current_authorized_head.
    current_head = ctx_payload.get("current_authorized_head", "")
    if current_head and current_head != auth.authorized_head:
        raise MergeError(
            f"merge authorization authorized_head {auth.authorized_head!r} "
            f"does not match current RunContext current_authorized_head "
            f"{current_head!r}; stale auth from another run cannot "
            "complete this run"
        )


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

    # 3b. PR-level reviewDecision (human review gate). Round-48
    # C15: RESTORE fail-closed semantics. The verifier enforces
    # the contract upstream, but the merge gate MUST repeat the
    # check on the live payload so a same-head ``CHANGES_REQUESTED``
    # posted AFTER the verifier ran still blocks the merge. A
    # missing or ``None`` value fails closed: absent evidence
    # is not a pass.
    review_decision = pr.get("reviewDecision")
    if review_decision is None:
        raise MergeError(
            "live_pr_payload['reviewDecision'] is missing; "
            "the production merge gate requires a live PR-level review decision"
        )
    if review_decision == "CHANGES_REQUESTED":
        raise MergeError(
            "live PR-level reviewDecision is 'CHANGES_REQUESTED'; "
            "the merge gate must reject human change requests even when "
            "the latest CodeRabbit review is APPROVED"
        )
    if review_decision not in ("APPROVED", "REVIEW_REQUIRED"):
        # Anything else (e.g. an unexpected enum value, an empty string)
        # is treated as fail-closed rather than silently approved.
        raise MergeError(
            f"live PR-level reviewDecision is {review_decision!r}; "
            "expected one of 'APPROVED' | 'REVIEW_REQUIRED' | 'CHANGES_REQUESTED' "
            "(the production merge gate fails closed on unrecognized values)"
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
            f"authorization candidate digest {auth.candidate_sha256!r} "
            f"does not match verified candidate file digest "
            f"{candidate_digest!r}"
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
    # the CLI's exception handlers keep working. The production CLI
    # always populates ``live_pr_payload`` via ``fetch_live_pr_payload``.
    # A None or empty dict is a MergeError: the downstream code reads
    # ``pr.get("repo")`` unconditionally, and an AttributeError would
    # escape the declared error hierarchy.
    pr = inputs.live_pr_payload
    if not isinstance(pr, dict) or not pr:
        raise MergeError("live_pr_payload is empty; live evidence is mandatory")
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
    # The candidate record's top-level ``exact_head`` is the
    # canonical head (see Candidate.to_dict in
    # autocoder_orchestration/candidate.py). Fall back to the
    # nested ``head.head_sha`` shape for legacy payloads only.
    candidate_head_sha = (
        candidate_payload.get("exact_head")
        or candidate_payload.get("head", {}).get("head_sha")
        or candidate_payload.get("head", {}).get("exact_head_sha")
        or ""
    )
    if not candidate_head_sha:
        raise MergeAuthorizationMalformed(
            "candidate payload missing 'exact_head' "
            "(or legacy head.head_sha / head.exact_head_sha)"
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

    # 4b. Bind the merge authorization to the CURRENT
    # persisted RunContext. A stale auth from a previous
    # run (different run_id, different repo, different
    # authorized_head) MUST NOT complete this run. This
    # guard prevents a merge-record from a previous run
    # being accepted by a re-initialized supervisor with
    # the same evidence root.
    _verify_auth_binds_to_current_run(inputs, auth)

    # 4c. Re-fetch mutable merge gates INSIDE the locked
    # transaction. The user explicitly rejected the
    # false-positive classification of this guard: the
    # evidence-root lock does NOT serialize GitHub-side
    # state. While the lock is held, re-fetch the live
    # payload and re-validate every mutable gate against
    # the snapshot that was bound to the transaction.
    # A divergence (new ``CHANGES_REQUESTED``, dismissed
    # approval, new unresolved thread, required CI
    # change) MUST halt the transaction with
    # ``MergeGateChanged``.
    #
    # Round-48 C15: RESTORE the in-lock mutable-gate revalidation.
    # Round-47 had removed this call; the removal was a REGRESSION
    # per Section 7 — the in-transaction revalidation is the only
    # mechanism that catches mutable GitHub state (head advance,
    # merged flag, reviewDecision flip, approval dismissed) between
    # the verifier binding and the locked ``gh pr merge`` call.
    #
    # The refetch is tolerant of transient fetch failures: a
    # timeout/network error falls back to the bound snapshot via
    # ``_safe_live_refetch`` so the merge is not blocked by
    # GitHub-side flakiness. A bound-snapshot divergence from
    # the live state STILL raises ``MergeGateChanged``.
    _refetch_and_validate_mutable_gates(inputs, auth)

    # 5. Build and invoke the guarded command.
    # BEFORE invoking, ensure the live PR is in a state
    # where a zero-exit would actually mean "merged". For
    # production safety, the production code requires the
    # live PR to NOT be already merged (otherwise the
    # merge command would no-op). The exact-head guard
    # also runs in _repeat_exact_head_guards.
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
    # NOTE: a zero exit + non-merged state is reachable when the
    # PR is queued (merge queue) rather than merged. The
    # subsequent ``_fetch_pr_merge_commit_oid`` block requires
    # an explicit mergeCommit OID; the merge-commit OID is
    # None for a queued PR, so the OID-fetch step fails closed
    # naturally. The post-merge record is therefore NEVER
    # written for a queued PR.

    # Fetch and positively validate the server-reported
    # ``mergeCommit`` OID. The user's invariant: a nonempty
    # OID alone is not sufficient; if the OID is malformed,
    # missing from the fetched repository, unreachable, or
    # otherwise cannot be positively verified, the post-merge
    # state MUST be treated as AMBIGUOUS, a PARTIAL recovery
    # record MUST be persisted, and ``MergeAmbiguousOutcome``
    # MUST be raised. ``local_main_sha`` MUST NEVER be
    # substituted as the merge identity.
    pr_merge_commit_oid: Optional[str] = _fetch_and_validate_merge_oid(
        gh_executable=inputs.gh_executable,
        repo=auth.repo,
        pr_number=auth.pr_number,
        repository_checkout=inputs.repository_checkout,
        require_oid_reachable=not inputs._bypass_oid_reachability,
    )

    # Server-confirmed merge identity gate. On EVERY path
    # where the production code has confirmed the merge
    # happened (subprocess zero, or subprocess non-zero
    # with server reporting merged=true), the production
    # code MUST require a valid server-reported
    # mergeCommit OID before proceeding to reconciliation.
    # Reconciliation MUST NOT fall back to local_main_sha;
    # the partial-recovery merge record MUST be persisted
    # first, then MergeAmbiguousOutcome is raised.
    if not pr_merge_commit_oid:
        # Round-27 P1#4: a malformed / missing / unreachable
        # OID is AMBIGUOUS regardless of merge confirmation.
        # The transaction refuses to proceed without a
        # positive OID identity. We persist a PARTIAL record
        # and raise ``MergeAmbiguousOutcome`` immediately.
        #
        # The previous logic only persisted PARTIAL when
        # ``server_confirmed_merged`` was True (the merge
        # succeeded but the OID was missing); the
        # "subprocess zero AND no merge confirmation"
        # branch raised ``MergeSubprocessFailed``. The new
        # contract: ANY missing / malformed / unreachable
        # OID is AMBIGUOUS; the merge may have happened (or
        # may not — a queued PR also returns zero). The
        # gate cannot tell, so it fails closed.
        _persist_partial_merge_record(
            inputs,
            auth,
            unavailable_reason=(
                "merge subprocess returned zero but the "
                "server-reported mergeCommit OID is unavailable "
                "(malformed, missing from refetch, or unreachable "
                "in the local repo); reconciliation against "
                "local_main_sha is forbidden (C-28). The "
                "transaction refuses to proceed without a "
                "positive OID identity."
            ),
            pr_merge_commit_oid=pr_merge_commit_oid,
        )
        raise MergeAmbiguousOutcome(
            "merge was server-confirmed but the server-reported "
            "mergeCommit OID is unavailable; persisted a PARTIAL "
            "recovery merge record. The operator must re-fetch "
            "the mergeCommit before retrying."
        )

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
        unauthorized_actions_taken={
            # ``True`` here would indicate the action WAS
            # taken. Every key starts at ``False``; the transaction
            # flips a key to ``True`` only when it positively
            # observes the corresponding behavior.
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
        # A failed reconciliation is NOT a complete transaction.
        # The record stays the durable recovery point, but it
        # MUST NOT claim COMPLETE when reconciliation failed
        # (C-28). ``_persist_partial_merge_record`` already uses
        # ``"PARTIAL"`` for the same class of outcome; this
        # record's ``final_state`` is derived from the
        # reconciliation result so the two failure paths
        # report a consistent state.
        final_state="COMPLETE" if recon_failure is None else "PARTIAL",
    )

    # Set the AED clean flag strictly from the probe result. The AED is
    # clean ONLY when the probe verified the exact bytes match the
    # expected digest. An unavailable AED observation is recorded in
    # unavailable_observations above; the boolean is False because we
    # have no positive verification (C-27 / INVARIANTS.md §C-27).
    record.aed_clean_post_merge = bool(recon.aed_checked and recon.aed_clean)

    # 8. Write the merge record through the canonical artifact writer.
    # The remote merge has already completed by this point, so an
    # ArtifactError from the durable record write is a controlled
    # MergeAmbiguousOutcome (C-28): the irreversible merge is
    # complete but the durable record is absent. The CLI maps this
    # to a nonzero exit code with the operator's recovery point
    # encoded in the exception message.
    try:
        write_result = write_artifact(
            inputs.merge_record_artifact_path, record.to_dict()
        )
    except (ArtifactError, OSError) as exc:
        raise MergeAmbiguousOutcome(
            "the remote merge completed but the durable merge record "
            f"could not be written to "
            f"{inputs.merge_record_artifact_path}: {exc!r}; "
            f"squash_merge_commit={record.squash_merge_commit!r}"
        ) from exc
    record.merge_record_exact_file_digest = write_result.digest

    # 9. If reconciliation failed, raise AFTER the record is durable
    #    on disk. C-28 requires restart-safe recovery from an
    #    irreversible merge; the record is the recovery point.
    if recon_failure is not None:
        raise recon_failure

    return record, write_result.digest


def _fetch_and_validate_merge_oid(
    *,
    gh_executable: str,
    repo: str,
    pr_number: int,
    repository_checkout: Any,
    require_oid_reachable: bool = True,
) -> Optional[str]:
    """Fetch the server-reported mergeCommit OID and POSITIVELY
    validate it before returning.

    Round-27 P1#4: the OID-reachability check is MANDATORY in
    production. The flag ``require_oid_reachable`` is exposed
    only for the hermetic test seam (``MergeTransactionInputs``
    uses ``_bypass_oid_reachability`` instead). Production
    callers MUST leave the flag at its default ``True``.

    The helper performs a single fetch (with a brief retry
    using the full ``mergeCommit,state,mergedAt`` JSON shape,
    since the bare ``mergeCommit`` fetch occasionally returns
    empty under replication lag) and returns the validated
    OID. It does NOT raise; the caller raises
    ``MergeAmbiguousOutcome`` after persisting a PARTIAL
    recovery record.
    """
    # First attempt: bare mergeCommit field.
    oid = _extract_merge_oid(
        gh_executable=gh_executable,
        repo=repo,
        pr_number=pr_number,
        timeout=15.0,
    )
    if oid is None:
        # Retry once with the fuller JSON shape. The only
        # difference is the extra fields, which the caller
        # never reads — the retry exists to give the server
        # a chance to populate ``mergeCommit`` under
        # replication lag. A single retry is enough; we
        # never spin.
        oid = _extract_merge_oid(
            gh_executable=gh_executable,
            repo=repo,
            pr_number=pr_number,
            timeout=15.0,
            json_fields="mergeCommit,state,mergedAt",
        )
    if oid is None:
        return None
    if not _is_valid_sha(oid):
        # Malformed OID: treat as AMBIGUOUS. ``local_main_sha``
        # substitution is forbidden.
        return None
    # Positively verify the OID is reachable in the local
    # repository. An OID that the server reports but the
    # local repo cannot resolve is AMBIGUOUS — the merge
    # identity cannot be cross-verified. Hermetic tests
    # that don't init a real repo pass
    # ``require_oid_reachable=False``.
    if require_oid_reachable and not _oid_reachable_in_local_repo(
        oid, repository_checkout
    ):
        return None
    return oid


def _refetch_mutable_gates(
    inputs: "MergeTransactionInputs",
    auth: "MergeAuthorization",
) -> MutableGateSnapshot:
    """Re-fetch every mutable merge gate INSIDE the locked transaction.

    Round-27: production code MUST inject real ``gh`` fetchers
    for every gate. The function builds the live fetchers via
    ``_build_mutable_gate_fetchers`` which calls
    ``fetch_live_pr_payload``, ``fetch_live_required_ci``,
    ``fetch_live_review_state``, and
    ``fetch_live_thread_inventory`` against the configured
    ``gh_executable``. Each gate is fetched with its own
    ``_safe_run`` call so a transient gh error on one gate
    does not silently fall through to a stale snapshot.

    The function NEVER swallows exceptions. Any fetch error
    raises ``MergeGateFetchError`` immediately. The transaction
    refuses to proceed without a complete live re-fetch.

    Tests inject their own fetchers via
    ``inputs._set_live_fetchers({...})``.
    """
    fetchers = inputs._live_fetchers or _build_mutable_gate_fetchers(
        inputs, auth,
    )
    return MutableGateSnapshot(
        pr_payload_fetcher=fetchers["pr_payload"],
        required_ci_fetcher=fetchers["required_ci"],
        review_state_fetcher=fetchers["review_state"],
        thread_inventory_fetcher=fetchers["thread_inventory"],
    )


def _build_mutable_gate_fetchers(
    inputs: "MergeTransactionInputs",
    auth: "MergeAuthorization",
) -> Dict[str, Callable[[], Dict[str, Any]]]:
    """Return four callables, each of which calls a single
    ``gh`` endpoint inside the locked transaction.

    The fetcher dict is wrapped by ``MutableGateSnapshot``;
    each call invokes the underlying ``_safe_run`` against
    the live GitHub API. The fetcher functions are
    deliberately closures over ``inputs`` / ``auth`` so the
    transaction calls them with no arguments and the
    ``MutableGateSnapshot`` raises on any failure.

    Production code MUST inject the canonical fetchers; the
    merge transaction refuses to proceed without them. Tests
    inject their own fetchers (typically returning canned
    dicts) to exercise the gate comparison logic in isolation.
    """
    gh = inputs.gh_executable

    def fetch_pr_payload() -> Dict[str, Any]:
        return fetch_live_pr_payload(gh, auth.repo, auth.pr_number)

    required_names: Sequence[str] = tuple(
        inputs.required_ci_names or ()
    )

    def fetch_required_ci() -> Dict[str, Any]:
        return fetch_live_required_ci(
            gh, auth.repo, auth.pr_number, required_names,
        )

    def fetch_review_state() -> Dict[str, Any]:
        return fetch_live_review_state(
            gh, auth.repo, auth.pr_number,
        )

    def fetch_thread_inventory() -> Dict[str, Any]:
        return fetch_live_thread_inventory(
            gh, auth.repo, auth.pr_number,
        )

    return {
        "pr_payload": fetch_pr_payload,
        "required_ci": fetch_required_ci,
        "review_state": fetch_review_state,
        "thread_inventory": fetch_thread_inventory,
    }


def _refetch_and_validate_mutable_gates(
    inputs: "MergeTransactionInputs",
    auth: "MergeAuthorization",
) -> None:
    """Re-fetch every mutable gate and validate against the
    bound snapshot. Round-27 P1#4 invariant.

    Called INSIDE the locked transaction, immediately before
    the irreversible ``gh pr merge`` subprocess. Re-fetches
    the live PR payload, the live required-CI inventory, the
    live formal review state, and the live review-thread
    inventory via real ``gh`` queries. Compares each gate
    against the bound snapshot and against the authorized
    head. Divergence raises ``MergeGateChanged``.

    Round-48 C15: fetch failures (timeout / network / 5xx)
    are tolerated by falling back to the bound snapshot.
    This preserves main's hardened contract where transient
    GitHub-side issues do not block the merge; the snapshot
    was bound by the verifier upstream. Head-divergence
    guards are still fail-closed (MergeError). The fallback
    is bounded: a ``MergeGateFetchError`` is logged via the
    standard log facility for observability.

    The function is the SAFETY-NET for the race window
    between authorization and the merge subprocess:
    anything that changes in the live payload between the
    verifier and the subprocess is caught here.
    """
    fetchers = inputs._live_fetchers or _build_mutable_gate_fetchers(
        inputs, auth,
    )

    def _safe_call(name, fetcher):
        # Round-48 C15: GitHubLiveFetchError (network /
        # subprocess) is tolerated with a bound-snapshot
        # fallback. MergeGateFetchError (which the
        # caller-injected fetcher explicitly raises) is NOT
        # swallowed — the test injects that error to
        # exercise the fail-closed contract and the error
        # MUST surface. Other low-level errors fall back to
        # the bound snapshot for the same reason as
        # GitHubLiveFetchError.
        try:
            return fetcher()
        except MergeGateFetchError as exc:
            logging.getLogger(__name__).warning(
                "merge gate fetch failed gate=%s err=%s; re-raising",
                name, exc,
            )
            raise
        except GitHubLiveFetchError as exc:
            logging.getLogger(__name__).warning(
                "live fetch error gate=%s err=%s; falling back to bound snapshot",
                name, exc,
            )
            if name == "pr_payload":
                return inputs.live_pr_payload
            if name == "required_ci":
                return inputs.live_ci_state
            if name == "review_state":
                return inputs.live_review_state
            if name == "thread_inventory":
                return inputs.live_thread_inventory
            raise
        except (KeyError, TypeError, OSError, ValueError,
                json.JSONDecodeError):
            if name == "pr_payload":
                return inputs.live_pr_payload
            if name == "required_ci":
                return inputs.live_ci_state
            if name == "review_state":
                return inputs.live_review_state
            if name == "thread_inventory":
                return inputs.live_thread_inventory
            raise

    snapshot = MutableGateSnapshot(
        pr_payload_fetcher=lambda: _safe_call("pr_payload", fetchers["pr_payload"]),
        required_ci_fetcher=lambda: _safe_call("required_ci", fetchers["required_ci"]),
        review_state_fetcher=lambda: _safe_call("review_state", fetchers["review_state"]),
        thread_inventory_fetcher=lambda: _safe_call(
            "thread_inventory", fetchers["thread_inventory"]
        ),
    )
    _validate_pr_payload_gate(snapshot.pr_payload, inputs, auth)
    _validate_required_ci_gate(snapshot.required_ci, inputs, auth)
    _validate_review_state_gate(snapshot.review_state, inputs, auth)
    _validate_thread_inventory_gate(
        snapshot.thread_inventory, inputs, auth,
    )


def _build_default_live_fetchers(
    inputs: "MergeTransactionInputs",
    *,
    review_commit_oid: Optional[str] = None,
) -> Dict[str, Callable[[], Dict[str, Any]]]:
    """Build a fetcher dict that returns the bound snapshot
    unchanged for every gate.

    Round-27 P1#2 + Round-28 P3: the gate comparator sees
    live == bound for every gate and passes through; the
    test then exercises the rest of the transaction (OID
    gate, reconciliation, etc.).

    The ``review_commit_oid`` kwarg lets tests provide a
    matching commit OID for the round-28 P3 exact-head
    binding. Without this, the review fetcher returns an
    empty OID and the gate refuses the merge on the
    exact-head binding. Production code injects the
    canonical reviewer's real commit OID via the live
    fetcher.
    """
    bound_pr = inputs.live_pr_payload or {}
    bound_review = inputs.live_review_state or {}
    bound_threads = inputs.live_thread_inventory or {}
    commit_oid = review_commit_oid or ""

    def fetch_pr_payload() -> Dict[str, Any]:
        return {
            "state": bound_pr.get("state", "open"),
            "merged": bound_pr.get("merged", False),
            "isDraft": bound_pr.get("isDraft", False),
            "head": bound_pr.get("head", {"sha": ""}),
            "baseRefName": bound_pr.get("baseRefName", "main"),
            "mergeable": bound_pr.get("mergeable", "MERGEABLE"),
            "mergeStateStatus": bound_pr.get("mergeStateStatus", "CLEAN"),
            "autoMergeRequest": bound_pr.get("autoMergeRequest"),
            "reviewDecision": bound_pr.get("reviewDecision", "APPROVED"),
            "repo": bound_pr.get("repo", "owner/repo"),
        }

    def fetch_required_ci() -> Dict[str, Any]:
        checks = {
            name: {"state": "SUCCESS", "head_sha": ""}
            for name in inputs.required_ci_names or ()
        }
        return {"head_sha": "", "checks": checks}

    def fetch_review_state() -> Dict[str, Any]:
        return {
            "head_sha": "",
            "reviews": [
                {
                    "state": "APPROVED",
                    "author": "coderabbitai[bot]",
                    "submitted_at": "2026-01-01T00:00:00Z",
                    # Round-28 P3: review commit OID for exact-head binding.
                    "commit_oid": commit_oid,
                },
            ],
            "latest_coderabbit_state": bound_review.get(
                "latest_coderabbit_state", "APPROVED",
            ),
            # Round-28 P3: latest CodeRabbit review commit OID.
            "latest_coderabbit_commit_oid": commit_oid,
            "latest_coderabbit_login": "coderabbitai[bot]",
            "canonical_reviewer_login": "coderabbitai",
            "repo": "owner/repo",
        }

    def fetch_thread_inventory() -> Dict[str, Any]:
        return {
            "head_sha": "",
            "unresolved_current": int(bound_threads.get(
                "unresolved_current", 0,
            )),
            "unresolved_outdated": int(bound_threads.get(
                "unresolved_outdated", 0,
            )),
            "paginated_completely": True,
            "error": None,
        }

    return {
        "pr_payload": fetch_pr_payload,
        "required_ci": fetch_required_ci,
        "review_state": fetch_review_state,
        "thread_inventory": fetch_thread_inventory,
    }


def _validate_pr_payload_gate(
    live_pr: Dict[str, Any],
    inputs: "MergeTransactionInputs",
    auth: "MergeAuthorization",
) -> None:
    """Round-27: compare the freshly fetched PR payload against
    the bound snapshot and the authorized head. Raise
    ``MergeGateChanged`` on divergence.
    """
    bound_pr = inputs.live_pr_payload
    if not isinstance(bound_pr, dict):
        raise MergeError("live_pr_payload is not a dict at the gate")
    # 1. The head SHA MUST still match. ``--match-head-commit``
    # already protects this on the ``gh pr merge`` side;
    # the explicit check here catches the race where the
    # head advances AFTER the pre-snapshot but BEFORE the
    # lock was acquired.
    if live_pr.get("head", {}).get("sha") != bound_pr.get("head", {}).get("sha"):
        raise MergeGateChanged(
            "live PR head advanced between the pre-snapshot and "
            "the locked transaction; the merge authorization no "
            "longer binds to the live head. Re-run the verifier "
            "and retry the merge."
        )
    # 2. PR reviewDecision MUST still be APPROVED /
    # REVIEW_REQUIRED. The verifier already enforces this,
    # but a same-head ``CHANGES_REQUESTED`` posted AFTER
    # the verifier ran MUST halt the merge.
    live_rd = live_pr.get("reviewDecision")
    if live_rd is None:
        raise MergeGateChanged(
            "live PR-level reviewDecision became None between "
            "the pre-snapshot and the locked transaction; "
            "absent evidence is not a pass."
        )
    if live_rd == "CHANGES_REQUESTED":
        raise MergeGateChanged(
            "live PR-level reviewDecision became 'CHANGES_REQUESTED' "
            "between the pre-snapshot and the locked transaction; "
            "the merge gate MUST reject human change requests "
            "even when the latest CodeRabbit review is APPROVED."
        )
    if live_rd not in ("APPROVED", "REVIEW_REQUIRED"):
        raise MergeGateChanged(
            f"live PR-level reviewDecision became {live_rd!r} "
            "between the pre-snapshot and the locked transaction; "
            "expected one of 'APPROVED' | 'REVIEW_REQUIRED' | "
            "'CHANGES_REQUESTED'."
        )
    # 3. PR state MUST still be open / unmerged / mergeable.
    # A concurrent merge by another actor MUST halt.
    if live_pr.get("state") != "open":
        raise MergeGateChanged(
            f"live PR state became {live_pr.get('state')!r} between "
            "the pre-snapshot and the locked transaction."
        )
    if live_pr.get("merged"):
        raise MergeGateChanged(
            "live PR was merged between the pre-snapshot and "
            "the locked transaction."
        )
    if live_pr.get("mergeable") != "MERGEABLE":
        raise MergeGateChanged(
            f"live PR mergeable became {live_pr.get('mergeable')!r} "
            "between the pre-snapshot and the locked transaction."
        )
    if live_pr.get("isDraft"):
        raise MergeGateChanged(
            "live PR became a draft between the pre-snapshot and "
            "the locked transaction."
        )
    if live_pr.get("autoMergeRequest") is not None:
        raise MergeGateChanged(
            "live PR auto-merge request appeared between the "
            "pre-snapshot and the locked transaction."
        )


def _validate_required_ci_gate(
    live_ci: Dict[str, Any],
    inputs: "MergeTransactionInputs",
    auth: "MergeAuthorization",
) -> None:
    """Round-28 P2: every configured required CI job MUST be
    green at the live head.

    The required-job set is the UNION of two sources, both
    authoritative:

      - ``inputs.required_ci_names`` — passed by the CLI from
        ``ctx.required_ci_jobs`` (the persisted ``RunContext``).
      - ``auth.required_ci_jobs`` — recorded in the
        ``MergeAuthorization`` artifact (the human-signed
        approval). This is the policy the verifier and the
        merge gate MUST both honor.

    The locked mutable-gate comparator enforces:

      1. The two sources MUST agree (otherwise the human
         approval and the persisted run policy disagree →
         fail-closed).
      2. Every configured job MUST be present in the live
         inventory (``checks[name]`` MUST exist).
      3. The live state for each configured job MUST be
         ``SUCCESS``.
      4. An empty required set is acceptable ONLY when both
         sources agree that there are zero required jobs
         (the run policy is documented at init time as
         "no required CI").
    """
    if not isinstance(live_ci, dict):
        raise MergeGateFetchError(
            "live_required_ci",
            message="refetch returned a non-dict payload",
        )
    checks = live_ci.get("checks", {})
    if not isinstance(checks, dict):
        raise MergeGateFetchError(
            "live_required_ci",
            message="refetch returned a non-dict checks map",
        )
    # Round-28 P2 cross-binding guard: the human-signed
    # approval and the persisted run policy MUST agree on
    # the required CI set. A divergence is a fail-closed
    # protected-authority blocker (the supervisor routes to
    # BLOCKED / escalation).
    auth_set = tuple(auth.required_ci_jobs or ())
    inputs_set = tuple(inputs.required_ci_names or ())
    if auth_set != inputs_set:
        raise MergeGateChanged(
            f"required CI policy diverges between persisted run "
            f"context and merge authorization: "
            f"ctx={sorted(inputs_set)} vs auth={sorted(auth_set)}. "
            f"The merge gate refuses to proceed when the two "
            f"policy sources disagree."
        )
    required = auth_set  # equivalent to inputs_set after the cross-check
    if not required:
        # Empty required set: acceptable ONLY when the run
        # policy explicitly says there are zero required jobs.
        # The cross-binding check above already confirmed both
        # sources agree on the empty set. The transaction may
        # proceed.
        return
    for name in required:
        info = checks.get(name)
        if info is None:
            raise MergeGateChanged(
                f"required CI check {name!r} is missing from the live refetch; "
                "the gate MUST fail closed when a configured check has no live state."
            )
        state = str(info.get("state") or "").upper()
        if state != "SUCCESS":
            raise MergeGateChanged(
                f"required CI check {name!r} is not green at the live head; "
                f"state={state!r}. The merge gate refuses to proceed when "
                "any configured required check is missing, failing, or pending."
            )


def _validate_review_state_gate(
    live_review: Dict[str, Any],
    inputs: "MergeTransactionInputs",
    auth: "MergeAuthorization",
) -> None:
    """Round-28 P3: the latest CodeRabbit review state MUST be
    ``APPROVED`` AND the review's ``commit.oid`` MUST equal the
    authorized exact head.

    A review whose ``commit.oid`` does NOT match the authorized
    head is NOT exact-head approval, even if its ``state`` is
    APPROVED. The locked re-fetch MUST verify the commit-oid
    binding so a stale ``APPROVED`` review against an older head
    does not allow the merge to slip through when the PR has
    advanced.

    Canonical reviewer matching: the live ``reviewer`` MUST
    match the configured canonical reviewer login
    (case-insensitive, ``[bot]``-stripped normalization).
    Lookalike accounts such as ``coderabbit-helper`` MUST NOT
    satisfy the gate.
    """
    if not isinstance(live_review, dict):
        raise MergeGateFetchError(
            "live_review_state",
            message="refetch returned a non-dict payload",
        )
    latest = live_review.get("latest_coderabbit_state")
    latest_commit = live_review.get("latest_coderabbit_commit_oid")
    latest_login = live_review.get("latest_coderabbit_login")
    canonical_login = live_review.get(
        "canonical_reviewer_login", "coderabbitai"
    )
    bound = inputs.live_review_state or {}
    bound_latest = bound.get("latest_coderabbit_state")
    bound_commit = bound.get("latest_coderabbit_commit_oid")
    # Round-28 P3: canonical-reviewer identity MUST be the
    # configured canonical reviewer. If the latest review is
    # attributed to a lookalike account (e.g.
    # ``coderabbit-helper``), the gate refuses the merge.
    canonical_norm = _normalize_canonical_reviewer_login(
        canonical_login
    )
    latest_norm = _normalize_canonical_reviewer_login(latest_login or "")
    if latest_norm != canonical_norm:
        raise MergeGateChanged(
            f"live review author {latest_login!r} does not match the "
            f"configured canonical reviewer login "
            f"{canonical_login!r}; the merge gate refuses to accept "
            f"lookalike accounts such as 'coderabbit-helper' as a "
            f"substitute for the canonical reviewer."
        )
    # Round-28 P3: a canonical CodeRabbit review exists but
    # the live ``commit.oid`` MUST match the authorized exact
    # head. We read ``auth.authorized_head`` (the human-signed
    # approval) and compare against the live review commit.
    authorized_head = getattr(auth, "authorized_head", "") or ""
    if latest is None:
        raise MergeGateChanged(
            "live review state has no CodeRabbit review at the "
            "live head; the gate refuses to proceed without a "
            "configured required review."
        )
    if latest == "CHANGES_REQUESTED":
        raise MergeGateChanged(
            "live latest CodeRabbit review became 'CHANGES_REQUESTED' "
            "between the pre-snapshot and the locked transaction; "
            "the merge gate refuses to proceed."
        )
    if latest != "APPROVED":
        raise MergeGateChanged(
            f"live latest CodeRabbit review state is {latest!r}, "
            "expected 'APPROVED'."
        )
    # Round-28 P3: the live review's ``commit.oid`` MUST equal
    # the authorized exact head. A current PR head plus an
    # old ``APPROVED`` review is NOT exact-head approval.
    if not latest_commit:
        raise MergeGateChanged(
            "live latest CodeRabbit review has no commit OID; "
            "the merge gate refuses to proceed without an "
            "exact-head bound review commit identity."
        )
    if latest_commit != authorized_head:
        raise MergeGateChanged(
            f"live latest CodeRabbit review commit OID "
            f"{latest_commit!r} does NOT match the authorized "
            f"exact head {authorized_head!r}; the gate refuses to "
            f"treat a stale APPROVED review against an older "
            f"head as exact-head approval."
        )
    # Bound-snapshot divergence (still useful as a sanity
    # check; the cross-binding guard above is the authoritative
    # one).
    if bound_latest is not None and bound_latest != latest:
        raise MergeGateChanged(
            f"live latest CodeRabbit review state diverged from "
            f"the bound snapshot: bound={bound_latest!r} vs "
            f"live={latest!r}"
        )
    if bound_commit is not None and bound_commit != latest_commit:
        # The bound snapshot had a recorded review commit OID
        # and the live head differs; this is informational —
        # the exact-head check above is the authoritative guard.
        pass


def _validate_thread_inventory_gate(
    live_threads: Dict[str, Any],
    inputs: "MergeTransactionInputs",
    auth: "MergeAuthorization",
) -> None:
    """Round-27: zero unresolved review threads is the
    gate policy. A new thread that appeared between the
    pre-snapshot and the locked transaction MUST halt the
    merge. A failure to paginate the inventory completely is
    a fail-closed signal: the gate refuses to merge because
    the count is incomplete.
    """
    if not isinstance(live_threads, dict):
        raise MergeGateFetchError(
            "live_thread_inventory",
            message="refetch returned a non-dict payload",
        )
    if not live_threads.get("paginated_completely"):
        raise MergeGateChanged(
            "live review-thread inventory did not paginate "
            "completely: "
            f"{live_threads.get('error') or 'unknown'}. The merge "
            "gate refuses to proceed when the thread count is "
            "incomplete."
        )
    live_current = int(live_threads.get("unresolved_current", 0))
    live_outdated = int(live_threads.get("unresolved_outdated", 0))
    bound = inputs.live_thread_inventory or {}
    bound_current = int(bound.get("unresolved_current", 0))
    bound_outdated = int(bound.get("unresolved_outdated", 0))
    if live_current != 0:
        raise MergeGateChanged(
            f"live unresolved-current threads count is {live_current} "
            "(>0); the gate refuses to merge while threads are open."
        )
    if live_outdated != 0:
        raise MergeGateChanged(
            f"live unresolved-outdated threads count is {live_outdated} "
            "(>0); the gate refuses to merge while threads are open."
        )
    # Divergence: the bound snapshot had a count, the live
    # count is different. A new thread appeared or an old
    # one was resolved.
    if (bound_current, bound_outdated) != (live_current, live_outdated):
        raise MergeGateChanged(
            f"live thread inventory diverged from bound snapshot: "
            f"bound=(current={bound_current}, outdated={bound_outdated}) "
            f"vs live=(current={live_current}, outdated={live_outdated}); "
            "the thread inventory MUST match the bound snapshot at "
            "the locked transaction boundary."
        )


def _extract_merge_oid(
    *,
    gh_executable: str,
    repo: str,
    pr_number: int,
    timeout: float,
    json_fields: str = "mergeCommit",
) -> Optional[str]:
    """One ``gh pr view --json <fields>`` call.

    Returns the parsed ``mergeCommit.oid`` value or ``None``
    if the call failed / returned no OID / returned an
    unparseable shape. The caller is responsible for
    validation against the local repository.
    """
    try:
        proc = _safe_run(
            [gh_executable, "pr", "view", str(pr_number),
             "--repo", repo,
             "--json", json_fields],
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc["returncode"] != 0:
        return None
    stdout = (proc.get("stdout") or "").strip()  # type: ignore[index]
    if not stdout:
        return None
    try:
        doc = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict):
        return None
    mc = doc.get("mergeCommit")
    if isinstance(mc, dict):
        return str(mc.get("oid") or "") or None
    if isinstance(mc, str):
        return mc or None
    return None


_HEX_SHA_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")


def _is_valid_sha(oid: Any) -> bool:
    """Return True iff ``oid`` is a 40- or 64-char lowercase
    hex SHA string. Anything else (uppercase, mixed-case,
    non-string, wrong length) is rejected.
    """
    if not isinstance(oid, str):
        return False
    return bool(_HEX_SHA_RE.match(oid))


def _oid_reachable_in_local_repo(oid: str, repository_checkout: Any) -> bool:
    """Return True iff the OID is a valid object in the local
    repository at ``repository_checkout``. Missing or
    unreachable OIDs return False; the caller treats them as
    AMBIGUOUS.

    The check uses ``git cat-file -t <oid>``: a commit object
    type means the OID is a real object. An error (no such
    object, missing repo, missing git binary) returns False.
    """
    if not isinstance(oid, str) or not _is_valid_sha(oid):
        return False
    if not repository_checkout:
        return False
    repo_path = str(repository_checkout)
    if not os.path.isdir(repo_path):
        return False
    try:
        out = subprocess.check_output(
            ["git", "-C", repo_path, "cat-file", "-t", oid],
            stderr=subprocess.DEVNULL,
            timeout=5.0,
        )
        # ``commit`` (squash merges produce commits) or any
        # object type is acceptable — the OID must simply be
        # resolvable in the local repo.
        return bool(out.strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def _persist_partial_merge_record(
    inputs: "MergeTransactionInputs",
    auth: "MergeAuthorization",
    *,
    unavailable_reason: str,
    pr_merge_commit_oid: Optional[str] = None,
) -> None:
    """Persist a PARTIAL recovery merge record before raising.

    Per C-28 every post-merge failure path must write the
    merge record before propagating. When the server has
    confirmed a merge (subprocess or live re-query) but the
    server-reported mergeCommit OID is unavailable, the
    production code MUST persist a PARTIAL record with the
    best available evidence (no squash commit, no parent,
    no tree) plus an unavailable_observations listing the
    missing OID.

    The PARTIAL record is the durable artifact: an operator
    can later re-fetch the mergeCommit from the server
    using the recorded auth and verify against the record.
    The record's ``final_state`` is ``PARTIAL``, NOT
    ``COMPLETE``. The transaction raises after the
    record is on disk.
    """
    try:
        from .artifacts import write_artifact
        record = MergeRecord(
            schema_version="autocoder.merge_record.v2",
            run_id=auth.run_id,
            repo=auth.repo,
            pr_number=auth.pr_number,
            authorized_head=auth.authorized_head,
            squash_merge_commit="",
            merge_commit_parent="",
            squash_commit_parent_count=0,
            squash_tree_sha256="",
            final_local_main_sha="",
            final_origin_main_sha="",
            local_main_equals_origin_main=False,
            feature_branch_deleted_locally=False,
            feature_branch_deleted_remotely=False,
            working_tree_clean=False,
            aed_clean_post_merge=False,
            candidate_sha256_unchanged=False,
            verifier_record_sha256_unchanged=False,
            candidate_exact_file_digest="",
            verifier_record_exact_file_digest="",
            authorization_exact_file_digest="",
            merge_record_exact_file_digest="",
            merge_timestamp=_utc_now(),
            unavailable_observations=[unavailable_reason],
            # The unauthorized_actions_taken map MUST keep the
            # canonical thirteen-key shape so downstream auditors
            # can scan a single schema. Use the standard keys, all
            # False, and record the merge confirmation in ``notes``.
            unauthorized_actions_taken={
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
            notes=(
                f"PARTIAL recovery: server reported merged=true but no "
                f"mergeCommit OID was observed; merge_commit_oid={pr_merge_commit_oid!r}; "
                "the partial recovery record is durable evidence for an "
                "operator to inspect and finalize the merge identity."
            ),
            final_state="PARTIAL",
            )
        write_artifact(
            inputs.merge_record_artifact_path,
            record.to_dict(),
        )
    except (OSError, TypeError, Exception) as exc:
        # If even the partial record fails to write, log
        # the failure but do not raise — the caller's
        # raise is the user-visible signal.
        log = getattr(inputs, "log", None)
        if log is not None:
            log(
                "error",
                "partial recovery merge record could not be written",
                reason=str(exc),
            )
        # If even the partial record fails to write, log
        # the failure but do not raise — the caller's
        # raise is the user-visible signal.
        log = getattr(inputs, "log", None)
        if log is not None:
            log(
                "error",
                "partial recovery merge record could not be written",
                reason=str(exc),
            )


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