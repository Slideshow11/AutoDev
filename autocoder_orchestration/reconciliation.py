"""Review-thread reconciliation contract.

The reconciler represents each review finding as a typed object
and decides whether the controller is allowed to resolve the
corresponding thread. Provider-specific parsing is intentionally
NOT performed here; callers translate raw thread data into
:class:`Finding` objects.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple


class FindingDisposition(str, Enum):
    OPEN_VALID = "OPEN_VALID"
    REPAIRED = "REPAIRED"
    SUPERSEDED = "SUPERSEDED"
    INVALID = "INVALID"
    INCONCLUSIVE = "INCONCLUSIVE"
    # Round-591: terminal supervisor-side dispositions for
    # ``no_changes_required_proof`` and worker ``claim.disposition``.
    # REAL_REPAIR_REQUIRED: worker has authority+evidence to fix
    # and HAS produced/repaired; the finding is repaired and
    # the work is on the current head.
    # ALREADY_SATISFIED: terminal no-change disposition with
    # proof; the observed evidence confirms the finding is moot
    # in current state.
    # SUPERSEDED: terminal disposition requiring ACTUAL
    # supersession proof (e.g. the subject SHA advanced past
    # the finding head); ``I think this is a fetch/config gap``
    # is NOT a supersession proof.
    # INCOMPLETE_EVIDENCE: NONTERMINAL — re-observe on next
    # round; never accepted as a NO_CHANGES_REQUIRED
    # disposition alone.
    # STILL_ACTIONABLE: NONTERMINAL — the finding is real and
    # the worker has authority+evidence to repair but chose
    # not to (e.g. round-39 anti-churn while CI is red);
    # this MUST leave the work executable for a later attempt.
    REAL_REPAIR_REQUIRED = "REAL_REPAIR_REQUIRED"
    ALREADY_SATISFIED = "ALREADY_SATISFIED"
    INCOMPLETE_EVIDENCE = "INCOMPLETE_EVIDENCE"
    STILL_ACTIONABLE = "STILL_ACTIONABLE"


@dataclass(frozen=True)
class Finding:
    provider: str
    review_id: str
    thread_id: str
    path: str
    line: Optional[int]
    head_sha: str
    is_outdated: bool
    severity: str
    description: str
    description_hash: str
    disposition: FindingDisposition
    repair_commit_sha: Optional[str] = None
    evidence_references: tuple = ()

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, FindingDisposition):
            raise ValueError(f"invalid disposition: {self.disposition!r}")
        # Validate description_hash matches description
        actual_hash = hashlib.sha256(self.description.encode()).hexdigest()
        if actual_hash != self.description_hash:
            raise ValueError(
                f"description_hash mismatch: recorded {self.description_hash!r}, "
                f"computed {actual_hash!r}"
            )
        if self.path and not isinstance(self.path, str):
            raise ValueError("path must be a string")
        if "/" in self.provider or not self.provider:
            raise ValueError(f"invalid provider: {self.provider!r}")
        if "/" in self.review_id or not self.review_id:
            raise ValueError(f"invalid review_id: {self.review_id!r}")
        if "/" in self.thread_id or not self.thread_id:
            raise ValueError(f"invalid thread_id: {self.thread_id!r}")
        # Accept either 40-character SHA-1 or 64-character object ID.
        if (len(self.head_sha) != 40 and len(self.head_sha) != 64) or not all(c in "0123456789abcdef" for c in self.head_sha):
            raise ValueError("head_sha must be 40 or 64 lowercase hex chars")

    def is_resolvable(self) -> bool:
        return self.disposition in (
            FindingDisposition.REPAIRED,
            FindingDisposition.SUPERSEDED,
            FindingDisposition.INVALID,
        )

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "review_id": self.review_id,
            "thread_id": self.thread_id,
            "path": self.path,
            "line": self.line,
            "head_sha": self.head_sha,
            "is_outdated": self.is_outdated,
            "severity": self.severity,
            "description": self.description,
            "description_hash": self.description_hash,
            "disposition": self.disposition.value,
            "repair_commit_sha": self.repair_commit_sha,
            "evidence_references": list(self.evidence_references),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Finding":
        if not isinstance(payload, dict):
            raise ValueError("finding payload must be a dict")
        return cls(
            provider=str(payload["provider"]),
            review_id=str(payload["review_id"]),
            thread_id=str(payload["thread_id"]),
            path=str(payload.get("path", "")),
            line=payload.get("line"),
            head_sha=str(payload["head_sha"]),
            is_outdated=bool(payload.get("is_outdated", False)),
            severity=str(payload.get("severity", "unknown")),
            description=str(payload["description"]),
            description_hash=str(payload["description_hash"]),
            disposition=FindingDisposition(payload["disposition"]),
            repair_commit_sha=payload.get("repair_commit_sha"),
            evidence_references=tuple(payload.get("evidence_references") or []),
        )


@dataclass
class ThreadResolution:
    """A controller decision to resolve a thread."""

    finding: Finding
    resolved_by: str
    resolved_at: str
    rationale: str

    def __post_init__(self) -> None:
        if not isinstance(self.finding, Finding):
            raise ValueError("finding must be a Finding")
        if not self.finding.is_resolvable():
            raise ValueError(
                f"thread {self.finding.thread_id!r} disposition is "
                f"{self.finding.disposition.value!r}, not resolvable"
            )

    def to_dict(self) -> dict:
        return {
            "finding": self.finding.to_dict(),
            "resolved_by": self.resolved_by,
            "resolved_at": self.resolved_at,
            "rationale": self.rationale,
        }


@dataclass
class Reconciler:
    """Reconciles review findings into resolution decisions."""

    def __init__(self, current_head: str) -> None:
        if len(current_head) != 64:
            raise ValueError("current_head must be 64 lowercase hex")
        self.current_head = current_head

    def classify(
        self,
        *,
        raw_thread: dict,
        provider: str,
        review_id: str,
        description: str,
        path: str = "",
        line: Optional[int] = None,
        severity: str = "unknown",
        is_outdated: bool = False,
        repair_commit_sha: Optional[str] = None,
        evidence_references: tuple = (),
    ) -> Finding:
        """Build a typed Finding from raw thread data."""
        desc_hash = hashlib.sha256(description.encode()).hexdigest()
        thread_id = str(raw_thread.get("id", ""))
        head = str(raw_thread.get("head_sha") or raw_thread.get("commit_oid") or self.current_head)
        return Finding(
            provider=provider,
            review_id=review_id,
            thread_id=thread_id,
            path=path,
            line=line,
            head_sha=head,
            is_outdated=is_outdated,
            severity=severity,
            description=description,
            description_hash=desc_hash,
            disposition=self._classify_disposition(
                raw_thread=raw_thread,
                is_outdated=is_outdated,
                head=head,
                repair_commit_sha=repair_commit_sha,
            ),
            repair_commit_sha=repair_commit_sha,
            evidence_references=evidence_references,
        )

    def _classify_disposition(
        self,
        *,
        raw_thread: dict,
        is_outdated: bool,
        head: str,
        repair_commit_sha: Optional[str],
    ) -> FindingDisposition:
        """Decide disposition from raw observation.

        Naive policy:
        - outdated thread + repair_commit head in current_head ancestry:
          SUPERSEDED
        - outdated thread without repair_commit: SUPERSEDED
        - resolved by user: REPAIRED (regardless of current content)
        - ongoing and the current head still contains the evidence:
          OPEN_VALID
        - ongoing and the current head inverts the evidence: INVALID
        """
        if raw_thread.get("isResolved"):
            return FindingDisposition.REPAIRED
        if is_outdated:
            return FindingDisposition.SUPERSEDED
        # Outlive the dissenter: the description's claim is not
        # verified against the current head. The controller should
        # snapshot this and call the readiness engine; the engine
        # does the revalidation.
        return FindingDisposition.OPEN_VALID

    def is_resolution_eligible(
        self,
        finding: Finding,
        current_head: str,
        later_comment_reopened: bool,
    ) -> Tuple[bool, str]:
        """Decide whether the controller may resolve ``finding``.

        Resolution requirements:
        - the finding is REPAIRED, SUPERSEDED, or INVALID;
        - the current head is the same head recorded on the
          finding (or the derived head_stability rule allows it);
        - no later comment has reopened the finding.
        """
        if not finding.is_resolvable():
            return (
                False,
                f"disposition {finding.disposition.value!r} is not resolvable",
            )
        if later_comment_reopened:
            return (
                False,
                "a later comment has reopened this finding",
            )
        if finding.head_sha != current_head and finding.disposition != FindingDisposition.SUPERSEDED:
            # SUPERSEDED is allowed even if head drift happened (the
            # original code is no longer present).
            return (
                False,
                f"current head {current_head!r} does not match recorded "
                f"head_sha {finding.head_sha!r}",
            )
        return True, "eligible"

    def build_resolution(
        self,
        finding: Finding,
        *,
        resolved_by: str,
        rationale: str,
    ) -> ThreadResolution:
        return ThreadResolution(
            finding=finding,
            resolved_by=resolved_by,
            resolved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            rationale=rationale,
        )
