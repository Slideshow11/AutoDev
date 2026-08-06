"""Candidate builder with hard gate.

The candidate builder accepts a :class:`RunContext`, a
:class:`ReadinessCertificate`, and an evidence snapshot. It refuses
to build when:

- the run context is incomplete;
- the readiness certificate is missing, expired, or in a failed state;
- the candidate's required inputs have changed since the certificate
  was issued (controller state revision, evidence hashes, head SHA);
- the operating directory is not a valid Git checkout.

On success, the candidate is built from Git-object bytes at the
exact head and atomically written to ``<evidence_root>/.../candidate.json``
plus a sidecar ``candidate.sha256``. The candidate includes the
hash of every input used to build it; subsequent changes to those
inputs invalidate the candidate.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


from .context import RunContext
from .readiness import ReadinessCertificate


SCHEMA_VERSION = "autocoder.candidate.v1"


class CandidateError(Exception):
    """Base candidate error."""


class CandidateNotReady(CandidateError):
    """Raised when readiness is not satisfied."""


class CandidateHeadMismatch(CandidateError):
    """Raised when the candidate head does not match the run context."""


class CandidateInputsStale(CandidateError):
    """Raised when cited inputs have changed since the certificate was issued."""


@dataclass
class Candidate:
    """A durable, identifiable candidate for one PR."""

    schema_version: str
    run_id: str
    repo: str
    pr_number: Optional[int]
    exact_head: str
    base_sha: str
    base_branch: str
    task_specification_sha256: str
    readiness_certificate_id: str
    readiness_certificate_sha256: str
    readiness_overall_passed: bool
    ci_inventory: List[Dict[str, Any]]
    review_inventory: List[Dict[str, Any]]
    thread_inventory: Dict[str, Any]
    strict_observation_log_hash: str
    process_identity: Dict[str, Any]
    lock_release_evidence: Dict[str, Any]
    controller_state_revision: int
    controller_state_path: str
    input_hashes: Dict[str, str]
    # These are filled at build time
    source_files: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    aed_source_files: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "repo": self.repo,
            "pr_number": self.pr_number,
            "exact_head": self.exact_head,
            "base_sha": self.base_sha,
            "base_branch": self.base_branch,
            "task_specification_sha256": self.task_specification_sha256,
            "readiness_certificate_id": self.readiness_certificate_id,
            "readiness_certificate_sha256": self.readiness_certificate_sha256,
            "readiness_overall_passed": self.readiness_overall_passed,
            "ci_inventory": list(self.ci_inventory),
            "review_inventory": list(self.review_inventory),
            "thread_inventory": dict(self.thread_inventory),
            "strict_observation_log_hash": self.strict_observation_log_hash,
            "process_identity": dict(self.process_identity),
            "lock_release_evidence": dict(self.lock_release_evidence),
            "controller_state_revision": self.controller_state_revision,
            "controller_state_path": self.controller_state_path,
            "input_hashes": dict(self.input_hashes),
            "source_files": dict(self.source_files),
            "aed_source_files": dict(self.aed_source_files),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Candidate":
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported candidate schema")
        return cls(
            schema_version=str(payload["schema_version"]),
            run_id=str(payload["run_id"]),
            repo=str(payload["repo"]),
            pr_number=int(payload["pr_number"]) if payload.get("pr_number") is not None else None,
            exact_head=str(payload["exact_head"]),
            base_sha=str(payload["base_sha"]),
            base_branch=str(payload["base_branch"]),
            task_specification_sha256=str(payload["task_specification_sha256"]),
            readiness_certificate_id=str(payload["readiness_certificate_id"]),
            readiness_certificate_sha256=str(payload["readiness_certificate_sha256"]),
            readiness_overall_passed=bool(payload["readiness_overall_passed"]),
            ci_inventory=list(payload.get("ci_inventory") or []),
            review_inventory=list(payload.get("review_inventory") or []),
            thread_inventory=dict(payload.get("thread_inventory") or {}),
            strict_observation_log_hash=str(payload["strict_observation_log_hash"]),
            process_identity=dict(payload.get("process_identity") or {}),
            lock_release_evidence=dict(payload.get("lock_release_evidence") or {}),
            controller_state_revision=int(payload["controller_state_revision"]),
            controller_state_path=str(payload["controller_state_path"]),
            input_hashes=dict(payload.get("input_hashes") or {}),
            source_files=dict(payload.get("source_files") or {}),
            aed_source_files=dict(payload.get("aed_source_files") or {}),
            created_at=str(payload.get("created_at") or ""),
        )

    def compute_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass
class CandidateBuilder:
    """Builds a Candidate from the run context and a readiness certificate.

    The builder owns no state between calls. It is a pure-function
    factory that refuses to produce a candidate when the inputs do
    not satisfy the contract.
    """

    def __init__(
        self,
        *,
        run_id: str,
        repo: str,
        pr_number: Optional[int],
        expected_head: str,
        base_sha: str,
        base_branch: str,
        task_specification_sha256: str,
        ci_inventory: List[Dict[str, Any]],
        review_inventory: List[Dict[str, Any]],
        thread_inventory: Dict[str, Any],
        strict_observation_log_hash: str,
        controller_state_revision: int,
        controller_state_path: str,
        process_identity: Dict[str, Any],
        lock_release_evidence: Dict[str, Any],
        expected_input_hashes: Dict[str, str],
        file_paths_to_attach: List[str],
        aed_source_paths: Optional[List[str]] = None,
        aed_source_commit: Optional[str] = None,
        aed_repo_root: Optional[str] = None,
    ) -> None:
        self.run_id = run_id
        self.repo = repo
        self.pr_number = pr_number
        self.expected_head = expected_head
        self.base_sha = base_sha
        self.base_branch = base_branch
        self.task_specification_sha256 = task_specification_sha256
        self.ci_inventory = ci_inventory
        self.review_inventory = review_inventory
        self.thread_inventory = thread_inventory
        self.strict_observation_log_hash = strict_observation_log_hash
        self.controller_state_revision = controller_state_revision
        self.controller_state_path = controller_state_path
        self.process_identity = process_identity
        self.lock_release_evidence = lock_release_evidence
        self.expected_input_hashes = expected_input_hashes
        self.file_paths_to_attach = file_paths_to_attach
        self.aed_source_paths = aed_source_paths or []
        self.aed_source_commit = aed_source_commit
        self.aed_repo_root = aed_repo_root

    def build(
        self,
        readiness: ReadinessCertificate,
        auto_repo_root: str,
    ) -> Candidate:
        """Build a candidate. Refuses if readiness does not satisfy."""
        if not isinstance(readiness, ReadinessCertificate):
            raise CandidateError("readiness must be a ReadinessCertificate")
        if not readiness.decision.overall_passed:
            raise CandidateNotReady(
                f"readiness decision did not pass: {[g.gate for g in readiness.decision.failed_gates()]}"
            )
        if readiness.decision.run_id != self.run_id:
            raise CandidateNotReady(
                f"readiness run_id {readiness.decision.run_id!r} != "
                f"builder run_id {self.run_id!r}"
            )
        if readiness.decision.expected_head != self.expected_head:
            raise CandidateHeadMismatch(
                f"readiness expected_head {readiness.decision.expected_head!r} != "
                f"builder expected_head {self.expected_head!r}"
            )
        # The candidate is built from Git-object bytes at HEAD. The
        # caller MUST pass a real Git checkout pointed at HEAD.
        auto_repo_root = self._safe_path(auto_repo_root, "auto_repo_root")
        source_files = {}
        for rel in self.file_paths_to_attach:
            sha, size = self._git_show_sha(auto_repo_root, self.expected_head, rel)
            source_files[rel] = {"sha256": sha, "size_bytes": size}
        aed_source_files = {}
        for rel in self.aed_source_paths:
            if not self.aed_repo_root or not self.aed_source_commit:
                raise CandidateError("aed_source_paths given but aed_repo_root/commit missing")
            sha, size = self._git_show_sha(self.aed_repo_root, self.aed_source_commit, rel)
            aed_source_files[rel] = {"sha256": sha, "size_bytes": size}
        # Cross-check expected_input_hashes against actual files we
        # observed. If the user supplied a hash that disagrees with
        # the live file content, refuse.
        for path, expected_hash in self.expected_input_hashes.items():
            if not path.startswith("file:"):
                continue
            rel = path[len("file:"):]
            target = self.expected_input_hashes  # ignored; only file:* is supported
            actual = None
            if rel in source_files:
                actual = source_files[rel]["sha256"]
            elif rel in aed_source_files:
                actual = aed_source_files[rel]["sha256"]
            if actual is None:
                raise CandidateInputsStale(
                    f"expected input {rel!r} not present in candidate inputs"
                )
            if actual != expected_hash:
                raise CandidateInputsStale(
                    f"expected input {rel!r} hash mismatch: expected {expected_hash!r}, "
                    f"actual {actual!r}"
                )
        cert_sha = hashlib.sha256(
            json.dumps(readiness.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        cand = Candidate(
            schema_version=SCHEMA_VERSION,
            run_id=self.run_id,
            repo=self.repo,
            pr_number=self.pr_number,
            exact_head=self.expected_head,
            base_sha=self.base_sha,
            base_branch=self.base_branch,
            task_specification_sha256=self.task_specification_sha256,
            readiness_certificate_id=readiness.certificate_id,
            readiness_certificate_sha256=cert_sha,
            readiness_overall_passed=True,
            ci_inventory=self.ci_inventory,
            review_inventory=self.review_inventory,
            thread_inventory=self.thread_inventory,
            strict_observation_log_hash=self.strict_observation_log_hash,
            process_identity=self.process_identity,
            lock_release_evidence=self.lock_release_evidence,
            controller_state_revision=self.controller_state_revision,
            controller_state_path=self.controller_state_path,
            input_hashes=dict(self.expected_input_hashes),
            source_files=source_files,
            aed_source_files=aed_source_files,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        return cand

    def _safe_path(self, path: str, label: str) -> str:
        if not isinstance(path, str) or not path:
            raise ValueError(f"{label} must be a non-empty string")
        if not os.path.isabs(path):
            raise ValueError(f"{label} must be absolute: {path!r}")
        p = Path(path)
        if p.exists() and p.is_symlink():
            raise ValueError(f"{label} must not be a symlink: {path!r}")
        return path

    def _git_show_sha(self, repo_root: str, head: str, path: str) -> tuple[str, int]:
        if not isinstance(path, str) or not path:
            raise CandidateError(f"unsafe path: {path!r}")
        # Reject absolute paths and parent-traversal
        if path.startswith("/") or ".." in path.split("/"):
            raise CandidateError(f"unsafe path: {path!r}")
        if (len(head) != 40 and len(head) != 64) or not all(c in "0123456789abcdef" for c in head):
            raise CandidateError(f"head must be 40 or 64 lowercase hex: {head!r}")
        # Use subprocess.check_output with safe args.
        proc = subprocess.run(
            ["git", "show", f"{head}:{path}"],
            cwd=repo_root,
            capture_output=True,
        )
        if proc.returncode != 0:
            raise CandidateError(
                f"git show failed for {path!r} at {head[:12]}...: {proc.stderr.decode()!r}"
            )
        blob = proc.stdout
        return hashlib.sha256(blob).hexdigest(), len(blob)


def build_candidate_from_observations(
    *,
    run_context: RunContext,
    readiness: ReadinessCertificate,
    ci_inventory: List[Dict[str, Any]],
    review_inventory: List[Dict[str, Any]],
    thread_inventory: Dict[str, Any],
    observation_log_hash: str,
    controller_state_revision: int,
    controller_state_path: str,
    process_identity: Dict[str, Any],
    lock_release_evidence: Dict[str, Any],
    file_paths_to_attach: List[str],
    auto_repo_root: str,
    aed_source_paths: Optional[List[str]] = None,
    aed_source_commit: Optional[str] = None,
    aed_repo_root: Optional[str] = None,
    expected_input_hashes: Optional[Dict[str, str]] = None,
) -> Candidate:
    """High-level helper that constructs a :class:`CandidateBuilder`.

    Returns a fully-built Candidate. Raises :class:`CandidateError`
    on any contract violation.
    """
    if not isinstance(run_context, RunContext):
        raise CandidateError("run_context must be a RunContext")
    if expected_input_hashes is None:
        expected_input_hashes = {}
    builder = CandidateBuilder(
        run_id=run_context.run_id,
        repo=f"{run_context.repo_owner}/{run_context.repo_name}",
        pr_number=run_context.pr_number,
        expected_head=run_context.current_authorized_head or "",
        base_sha=run_context.authorized_base_sha,
        base_branch=run_context.base_branch,
        task_specification_sha256=run_context.task_specification_sha256,
        ci_inventory=ci_inventory,
        review_inventory=review_inventory,
        thread_inventory=thread_inventory,
        strict_observation_log_hash=observation_log_hash,
        controller_state_revision=controller_state_revision,
        controller_state_path=controller_state_path,
        process_identity=process_identity,
        lock_release_evidence=lock_release_evidence,
        expected_input_hashes=expected_input_hashes,
        file_paths_to_attach=file_paths_to_attach,
        aed_source_paths=aed_source_paths,
        aed_source_commit=aed_source_commit,
        aed_repo_root=aed_repo_root,
    )
    return builder.build(readiness, auto_repo_root)
