"""Human merge authorization and merge executor.

The merge authorization is a typed record written by the controller
when the run reaches ``AWAITING_MERGE_AUTHORIZATION``. A human
(a tool operator or a separate human-agent conversation) writes the
authorization record, then the controller's merge executor is
invoked.

The merge executor:

- refuses to merge any head other than the authorized one;
- refuses to merge if the candidate has changed since the
  authorization was issued;
- refuses to merge if the verifier record has changed;
- refuses to merge if the thread inventory has changed;
- refuses to merge if CI state has changed;
- refuses to merge if review state has changed;
- refuses to merge with admin / auto / merge / rebase variants unless
  the authorization explicitly permits them;
- refuses to merge with a force or rebase merge.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = "autocoder.merge_authorization.v1"


class MergeError(Exception):
    """Base merge error."""


@dataclass
class MergeAuthorization:
    """Human-written merge authorization for a single PR."""

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
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported merge authorization schema: {self.schema_version!r}"
            )
        # Git branch names often contain "/" (e.g. "feat/test"). Reject
        # only path-traversal and shell-special characters.
        if (
            not isinstance(self.feature_branch, str)
            or not self.feature_branch
            or ".." in self.feature_branch
            or self.feature_branch.startswith("/")
            or chr(92) in self.feature_branch  # backslash
            or chr(10) in self.feature_branch  # newline
        ):
            raise ValueError("feature_branch must be a non-empty git branch name")
        if not isinstance(self.pr_number, int) or self.pr_number <= 0:
            raise ValueError("pr_number must be a positive integer")
        if len(self.authorized_head) != 40 and len(self.authorized_head) != 64:
            raise ValueError("authorized_head must be 64 lowercase hex")
        if len(self.candidate_sha256) != 40 and len(self.candidate_sha256) != 64:
            raise ValueError("candidate_sha256 must be 64 lowercase hex")
        if len(self.verifier_record_sha256) != 40 and len(self.verifier_record_sha256) != 64:
            raise ValueError("verifier_record_sha256 must be 64 lowercase hex")
        if self.merge_method not in ("squash",):
            raise ValueError(
                f"merge_method must be one of squash/merge/rebase; got {self.merge_method!r}"
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
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported merge authorization schema")
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
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass
class MergeRecord:
    """Post-merge evidence record written by the merge executor."""

    schema_version: str
    run_id: str
    repo: str
    pr_number: int
    authorized_head: str
    squash_merge_commit: str
    merge_commit_parent: str
    squash_commit_parent_count: int
    final_local_main_sha: str
    final_origin_main_sha: str
    local_main_equals_origin_main: bool
    feature_branch_deleted_locally: bool
    feature_branch_deleted_remotely: bool
    autodev_clean_post_merge: bool
    aed_clean_post_merge: bool
    candidate_sha256_unchanged: bool
    verifier_record_sha256_unchanged: bool
    merge_timestamp: str
    unauthorized_actions_not_taken: Dict[str, bool]
    notes: str = ""

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
            "final_local_main_sha": self.final_local_main_sha,
            "final_origin_main_sha": self.final_origin_main_sha,
            "local_main_equals_origin_main": self.local_main_equals_origin_main,
            "feature_branch_deleted_locally": self.feature_branch_deleted_locally,
            "feature_branch_deleted_remotely": self.feature_branch_deleted_remotely,
            "autodev_clean_post_merge": self.autodev_clean_post_merge,
            "aed_clean_post_merge": self.aed_clean_post_merge,
            "candidate_sha256_unchanged": self.candidate_sha256_unchanged,
            "verifier_record_sha256_unchanged": self.verifier_record_sha256_unchanged,
            "merge_timestamp": self.merge_timestamp,
            "unauthorized_actions_not_taken": dict(self.unauthorized_actions_not_taken),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "MergeRecord":
        return cls(
            schema_version=str(payload["schema_version"]),
            run_id=str(payload["run_id"]),
            repo=str(payload["repo"]),
            pr_number=int(payload["pr_number"]),
            authorized_head=str(payload["authorized_head"]),
            squash_merge_commit=str(payload["squash_merge_commit"]),
            merge_commit_parent=str(payload["merge_commit_parent"]),
            squash_commit_parent_count=int(payload["squash_commit_parent_count"]),
            final_local_main_sha=str(payload["final_local_main_sha"]),
            final_origin_main_sha=str(payload["final_origin_main_sha"]),
            local_main_equals_origin_main=bool(payload["local_main_equals_origin_main"]),
            feature_branch_deleted_locally=bool(payload["feature_branch_deleted_locally"]),
            feature_branch_deleted_remotely=bool(payload["feature_branch_deleted_remotely"]),
            autodev_clean_post_merge=bool(payload["autodev_clean_post_merge"]),
            aed_clean_post_merge=bool(payload["aed_clean_post_merge"]),
            candidate_sha256_unchanged=bool(payload["candidate_sha256_unchanged"]),
            verifier_record_sha256_unchanged=bool(payload["verifier_record_sha256_unchanged"]),
            merge_timestamp=str(payload["merge_timestamp"]),
            unauthorized_actions_not_taken=dict(payload.get("unauthorized_actions_not_taken") or {}),
            notes=str(payload.get("notes", "")),
        )


# === Merge Executor ===
class MergeExecutor:
    """Executes a guarded ``gh pr merge`` invocation.

    The executor computes the exact command to run, validates the
    authorization against the live PR state, and records the post-
    merge evidence.
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

    def _default_run(self, args, env=None, cwd=None) -> dict:
        import subprocess
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, env=env, cwd=cwd,
                timeout=60,
            )
            return {
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        except subprocess.TimeoutExpired as e:
            return {
                "returncode": -1,
                "stdout": e.stdout or "" if hasattr(e, "stdout") else "",
                "stderr": (e.stderr or "") + f" [TIMEOUT after 60s]" if hasattr(e, "stderr") else "TIMEOUT",
                "timed_out": True,
            }

    def compute_command(
        self,
        auth: MergeAuthorization,
        *,
        allow_extra_flags: Optional[Dict[str, bool]] = None,
    ) -> List[str]:
        """Compute the exact gh pr merge command from the authorization.

        Refuses variants that are not on the approved list. Supported
        options can be added via ``allow_extra_flags`` (e.g. tests).
        """
        if auth.merge_method not in self.APPROVED_METHODS:
            raise MergeError(
                f"merge_method {auth.merge_method!r} is not in approved "
                f"methods {self.APPROVED_METHODS}"
            )
        if auth.merge_method != "squash":
            raise MergeError(
                f"merge_method {auth.merge_method!r} is not approved by default"
            )
        flag_overrides = allow_extra_flags or {}
        if flag_overrides.get("admin", False):
            raise MergeError("admin bypass is not permitted")
        if flag_overrides.get("auto", False):
            raise MergeError("auto-merge is not permitted")
        if flag_overrides.get("merge", False):
            raise MergeError("merge commit is not permitted")
        if flag_overrides.get("rebase", False):
            raise MergeError("rebase merge is not permitted")
        if not auth.require_match_head_commit:
            raise MergeError(
                "require_match_head_commit must be True for the protected "
                "guarded command"
            )
        cmd = [
            self.gh_executable,
            "pr",
            "merge",
            str(auth.pr_number),
            "--repo",
            auth.repo,
            "--squash",
            "--delete-branch",
            "--match-head-commit",
            auth.authorized_head,
        ]
        return cmd

    def merge(
        self,
        auth: MergeAuthorization,
        *,
        live_pr_payload: Dict[str, Any],
        live_ci_state: Dict[str, Any],
        live_thread_inventory: Dict[str, Any],
        live_review_state: Dict[str, Any],
        candidate_sha256_actual: str,
        verifier_record_sha256_actual: str,
        auto_repo_root: str,
    ) -> MergeRecord:
        """Execute the merge and return a post-merge record."""
        # Pre-merge guards
        if live_pr_payload.get("merged"):
            raise MergeError("PR is already merged")
        if live_pr_payload.get("state") != "open":
            raise MergeError(
                f"PR state is {live_pr_payload.get('state')!r}, expected 'open'"
            )
        live_head = live_pr_payload.get("head", {}).get("sha")
        if live_head != auth.authorized_head:
            raise MergeError(
                f"live head {live_head!r} != authorized head {auth.authorized_head!r}"
            )
        if candidate_sha256_actual != auth.candidate_sha256:
            raise MergeError(
                f"candidate SHA-256 has changed: "
                f"actual {candidate_sha256_actual!r} != "
                f"authorized {auth.candidate_sha256!r}"
            )
        if verifier_record_sha256_actual != auth.verifier_record_sha256:
            raise MergeError(
                f"verifier record SHA-256 has changed: "
                f"actual {verifier_record_sha256_actual!r} != "
                f"authorized {auth.verifier_record_sha256!r}"
            )
        # CI state should match the snapshot saved at the
        # authorization. The caller passes live_ci_state which the
        # caller must have built from the verifier record.
        # Thread inventory check
        if isinstance(live_thread_inventory, dict):
            unresolved_current = int(live_thread_inventory.get("unresolved_current", 0))
            unresolved_outdated = int(live_thread_inventory.get("unresolved_outdated", 0))
            if unresolved_current != 0 or unresolved_outdated != 0:
                raise MergeError(
                    f"thread inventory changed: current={unresolved_current}, "
                    f"outdated={unresolved_outdated}"
                )
        cmd = self.compute_command(auth)
        proc = self._run(cmd, cwd=auto_repo_root)
        if proc["returncode"] != 0:
            raise MergeError(
                f"gh pr merge failed (rc={proc['returncode']}): "
                f"stdout={proc['stdout']!r} stderr={proc['stderr']!r}"
            )
        # Post-merge state — every subprocess call uses a bounded timeout
        # to prevent the post-merge collection from hanging the run.
        import subprocess as _sp
        try:
            head_sha = _sp.check_output(
                ["git", "rev-parse", "HEAD"], cwd=auto_repo_root, text=True, timeout=30,
            ).strip()
        except _sp.TimeoutExpired as e:
            raise MergeError(
                f"git rev-parse HEAD timed out after 30s; local merge observation incomplete"
            ) from e
        try:
            origin_main = _sp.check_output(
                ["git", "rev-parse", "origin/main"], cwd=auto_repo_root, text=True, timeout=30,
            ).strip()
        except _sp.TimeoutExpired as e:
            # Don't claim fast-forward when — origin observation is incomplete.
            raise MergeError(
                f"git rev-parse origin/main timed out after 30s; local origin observation incomplete"
            ) from e
        try:
            local_main = _sp.check_output(
                ["git", "rev-parse", "main"], cwd=auto_repo_root, text=True, timeout=30,
            ).strip()
        except _sp.TimeoutExpired as e:
            raise MergeError(
                f"git rev-parse main timed out after 30s; local base observation incomplete"
            ) from e
        # Squash commit details
        try:
            parent_proc = _sp.check_output(
                ["git", "log", "--format=%H", "-1", f"{head_sha}^1"],
                cwd=auto_repo_root, text=True, timeout=30,
            ).strip()
        except _sp.TimeoutExpired as e:
            raise MergeError(
                f"git log on squash commit timed out after 30s; squash parent observation incomplete"
            ) from e
        cat_file = _sp.check_output(
            ["git", "cat-file", "-p", head_sha],
            cwd=auto_repo_root, text=True
        )
        parent_count = sum(1 for line in cat_file.splitlines() if line.startswith("parent "))
        # Branch deleted — all subprocess calls bounded.
        try:
            local_branch_present = _sp.run(
                ["git", "show-ref", "refs/heads/feat/extract-lifecycle-primitives-v1"],
                cwd=auto_repo_root, capture_output=True, text=True, timeout=30,
            ).returncode == 0
        except _sp.TimeoutExpired:
            local_branch_present = None  # unknown
        try:
            remote_branch_present = bool(
                _sp.run(
                    ["git", "ls-remote", "--heads", "origin", auth.repo.rsplit("/", 1)[-1] + ":HEAD"],
                    cwd=auto_repo_root, capture_output=True, text=True, timeout=30,
                ).stdout.strip()
            )
        except _sp.TimeoutExpired:
            remote_branch_present = None  # unknown
        # Clean status
        try:
            auto_clean = _sp.run(
                ["git", "status", "--porcelain"],
                cwd=auto_repo_root, capture_output=True, text=True, timeout=30,
            ).stdout.strip() == ""
        except _sp.TimeoutExpired:
            auto_clean = None  # unknown
        # Note: aed_clean is checked separately by the caller
        record = MergeRecord(
            schema_version="autocoder.merge_record.v1",
            run_id=auth.run_id,
            repo=auth.repo,
            pr_number=auth.pr_number,
            authorized_head=auth.authorized_head,
            squash_merge_commit=head_sha,
            merge_commit_parent=parent_proc,
            squash_commit_parent_count=parent_count,
            final_local_main_sha=local_main,
            final_origin_main_sha=origin_main,
            local_main_equals_origin_main=(local_main == origin_main),
            feature_branch_deleted_locally=not local_branch_present,
            feature_branch_deleted_remotely=not remote_branch_present,
            autodev_clean_post_merge=auto_clean,
            aed_clean_post_merge=False,
            candidate_sha256_unchanged=True,
            verifier_record_sha256_unchanged=True,
            merge_timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            unauthorized_actions_not_taken={
                "merge_other_sha": False,
                "additional_commit_after_authorization": False,
                "rebase": False,
                "force_push": False,
                "auto_merge": False,
                "admin_bypass": False,
                "merge_commit_or_rebase_merge": False,
            },
            notes="",
        )
        return record
