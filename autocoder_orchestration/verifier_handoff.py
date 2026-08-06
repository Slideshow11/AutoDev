"""Verifier handoff and verifier role guard.

The handoff is a typed record written by the controller to a
specific path. A fresh worker reads the handoff, verifies the
candidate, and writes a separate verifier record. The controller
must NOT itself write the verifier record.

The verifier role guard rejects verification attempts when:

- the verifier process identity matches the implementation worker
  identity (same PID + start_id);
- the implementation lease is still active;
- the verifier executable comes from the target branch under review;
- the verifier has write credentials configured.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


from .context import RunContext
from .store import ProcessIdentity, StateStore, StateStoreError


SCHEMA_VERSION = "autocoder.verifier_handoff.v1"


class HandoffError(Exception):
    """Base handoff error."""


@dataclass
class VerifierHandoff:
    """Typed handoff record.

    The handoff is written atomically by the controller when the run
    reaches ``AWAITING_INDEPENDENT_VERIFICATION``. The verifier
    reads the handoff, writes a separate verifier record, and is
    then finished.
    """

    schema_version: str
    run_id: str
    repo: str
    pr_number: Optional[int]
    exact_head: str
    base_sha: str
    base_branch: str
    task_specification_sha256: str
    candidate_path: str
    candidate_sha256: str
    readiness_certificate_id: str
    observation_log_path: str
    observation_log_sha256: str
    strict_window_first_utc: Optional[str]
    strict_window_last_utc: Optional[str]
    strict_window_observation_count: int
    strict_window_duration_monotonic: float
    controller_state_revision: int
    controller_state_path: str
    # Trust root for the verifier executable
    trusted_verifier_source_commit: str
    trusted_verifier_package_version: str
    # Caller-supplied process identity of the implementation worker
    # at the time the candidate was frozen. The verifier must NOT
    # share this identity.
    implementation_worker_identity: Optional[Dict[str, Any]]
    # Output path the verifier must write to
    verifier_record_path: str
    created_at: str

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
            "candidate_path": self.candidate_path,
            "candidate_sha256": self.candidate_sha256,
            "readiness_certificate_id": self.readiness_certificate_id,
            "observation_log_path": self.observation_log_path,
            "observation_log_sha256": self.observation_log_sha256,
            "strict_window_first_utc": self.strict_window_first_utc,
            "strict_window_last_utc": self.strict_window_last_utc,
            "strict_window_observation_count": self.strict_window_observation_count,
            "strict_window_duration_monotonic": self.strict_window_duration_monotonic,
            "controller_state_revision": self.controller_state_revision,
            "controller_state_path": self.controller_state_path,
            "trusted_verifier_source_commit": self.trusted_verifier_source_commit,
            "trusted_verifier_package_version": self.trusted_verifier_package_version,
            "implementation_worker_identity": self.implementation_worker_identity,
            "verifier_record_path": self.verifier_record_path,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "VerifierHandoff":
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported handoff schema")
        return cls(
            schema_version=str(payload["schema_version"]),
            run_id=str(payload["run_id"]),
            repo=str(payload["repo"]),
            pr_number=int(payload["pr_number"]) if payload.get("pr_number") is not None else None,
            exact_head=str(payload["exact_head"]),
            base_sha=str(payload["base_sha"]),
            base_branch=str(payload["base_branch"]),
            task_specification_sha256=str(payload["task_specification_sha256"]),
            candidate_path=str(payload["candidate_path"]),
            candidate_sha256=str(payload["candidate_sha256"]),
            readiness_certificate_id=str(payload["readiness_certificate_id"]),
            observation_log_path=str(payload["observation_log_path"]),
            observation_log_sha256=str(payload["observation_log_sha256"]),
            strict_window_first_utc=payload.get("strict_window_first_utc"),
            strict_window_last_utc=payload.get("strict_window_last_utc"),
            strict_window_observation_count=int(payload.get("strict_window_observation_count", 0)),
            strict_window_duration_monotonic=float(payload.get("strict_window_duration_monotonic", 0.0)),
            controller_state_revision=int(payload["controller_state_revision"]),
            controller_state_path=str(payload["controller_state_path"]),
            trusted_verifier_source_commit=str(payload["trusted_verifier_source_commit"]),
            trusted_verifier_package_version=str(payload["trusted_verifier_package_version"]),
            implementation_worker_identity=payload.get("implementation_worker_identity"),
            verifier_record_path=str(payload["verifier_record_path"]),
            created_at=str(payload["created_at"]),
        )

    def compute_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def write_handoff(store: StateStore, handoff: VerifierHandoff) -> str:
    """Write the handoff to the state store. Returns the path."""
    rel_path = "verifier-handoff.json"
    payload = handoff.to_dict()
    payload["_sha256"] = handoff.compute_sha256()
    store.write_atomic(rel_path, payload)
    return rel_path


def read_handoff(store: StateStore) -> Optional[VerifierHandoff]:
    payload = store.read_optional("verifier-handoff.json")
    if payload is None:
        return None
    return VerifierHandoff.from_dict(payload)


# === Verifier role guard ===
@dataclass
class VerifierRoleGuard:
    """Validates that a verifier attempt is acceptable.

    The guard reads the handoff and the store, then enforces:
    - the verifier process identity is NOT the implementation worker
      identity (different PID or different start_id);
    - the implementation lease is NOT still active in the store;
    - the verifier executable path is NOT under the target branch
      (caller must pass the executable path; the guard checks it is
      not inside the local checkout for the target branch);
    - the verifier does NOT have write credentials (the guard
      inspects an environment variable set by the controller; the
      verifier writes only to the verifier_record_path).
    """

    def __init__(self, handoff: VerifierHandoff, store: StateStore) -> None:
        self.handoff = handoff
        self.store = store

    def validate(
        self,
        *,
        verifier_identity: ProcessIdentity,
        verifier_executable_path: Optional[str],
        write_credentials_present: bool = False,
    ) -> Tuple[bool, str]:
        """Return (ok, reason). On False, ``reason`` explains why."""
        # 1. Same process identity as the implementation worker?
        impl_id = self.handoff.implementation_worker_identity
        if isinstance(impl_id, dict):
            try:
                impl_pid = int(impl_id["pid"])
                impl_start = str(impl_id["start_id"])
            except (KeyError, TypeError, ValueError):
                impl_pid = None
                impl_start = None
            if (
                impl_pid is not None
                and impl_start is not None
                and verifier_identity.pid == impl_pid
                and verifier_identity.start_id == impl_start
            ):
                return False, (
                    f"verifier shares process identity with implementation "
                    f"worker (pid={impl_pid}, start_id={impl_start[:16]}...)"
                )

        # 2. Implementation lease still active?
        try:
            lease = self.store.read_lease()
        except StateStoreError:
            lease = None
        if isinstance(lease, ProcessIdentity):
            if (
                lease.pid == verifier_identity.pid
                and lease.start_id == verifier_identity.start_id
            ):
                # The verifier could still be holding the lease from
                # before the controller opened it. The controller
                # must release the lease before invoking the handoff.
                return False, (
                    "verifier process identity matches active lease "
                    "— implementation lease not yet released"
                )

        # 3. Verifier executable from the target branch?
        if verifier_executable_path:
            try:
                run_context_repo = self.store.read_optional("run_context.json")
            except StateStoreError:
                run_context_repo = None
            if isinstance(run_context_repo, dict):
                local_checkout = run_context_repo.get("local_checkout")
                if (
                    isinstance(local_checkout, str)
                    and os.path.isabs(local_checkout)
                    and os.path.isabs(verifier_executable_path)
                ):
                    lc = os.path.realpath(local_checkout)
                    vp = os.path.realpath(verifier_executable_path)
                    if os.path.commonpath([lc, vp]) == lc:
                        return False, (
                            f"verifier executable {verifier_executable_path!r} "
                            f"is inside the implementation local checkout "
                            f"{local_checkout!r}; this is the target branch under review"
                        )

        # 4. Write credentials present?
        if write_credentials_present:
            return False, "verifier has write credentials configured"

        return True, "verifier role checks passed"
