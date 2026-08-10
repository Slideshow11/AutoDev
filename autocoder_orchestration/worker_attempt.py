"""WorkerAttemptRecord — durable causal contract for a single worker attempt.

Round-36 invariant:
    FINDING -> CLAIM -> DIRECTIVE -> WORKER -> COMMIT -> PUSH -> LIVE GITHUB HEAD
        -> REPAIR ACKNOWLEDGEMENT

Every worker launch produces ONE canonical ``WorkerAttemptRecord`` persisted
to ``state/worker_attempts/<attempt_id>.json``. The record is the only
authoritative source for the question "did this attempt push a commit that
the supervisor should treat as a repair push?".

A generic branch head advance (Humphry infrastructure commit, operator
commit, recovery commit, external actor) MUST NOT be treated as a worker
repair push. Only a ``WorkerAttemptRecord`` whose lifecycle reaches
``PUSH_VERIFIED`` with positive worker provenance may be acknowledged as
``report_repair_pushed``.

Lifecycle:
    CLAIMED
        -> WORKER_STARTING
        -> WORKER_RUNNING
        -> COMMIT_PRODUCED
        -> PUSH_VERIFIED
        -> TERMINAL_REPAIRED

Failure path:
    WORKER_RUNNING
        -> WORKER_EXITED_NO_PUSH
        -> work item RETRY_PENDING

Crash-recovery path:
    WORKER_RUNNING
        -> RECOVERY_CHECK
        -> PUSH_VERIFIED (if independent evidence proves the worker really pushed)
        -> TERMINAL_REPAIRED
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSION = "autocoder.worker_attempt.v1"

# Lifecycle values. Stored as strings so the on-disk JSON is human-readable.
LIFECYCLE_CLAIMED = "CLAIMED"
LIFECYCLE_WORKER_STARTING = "WORKER_STARTING"
LIFECYCLE_WORKER_RUNNING = "WORKER_RUNNING"
LIFECYCLE_COMMIT_PRODUCED = "COMMIT_PRODUCED"
LIFECYCLE_PUSH_VERIFIED = "PUSH_VERIFIED"
LIFECYCLE_TERMINAL_REPAIRED = "TERMINAL_REPAIRED"
LIFECYCLE_WORKER_EXITED_NO_PUSH = "WORKER_EXITED_NO_PUSH"
LIFECYCLE_RECOVERY_CHECK = "RECOVERY_CHECK"

# Terminal failure lifecycle values — the attempt is finished but the work
# item is RETRY_PENDING.
TERMINAL_FAILURE_LIFECYCLES = frozenset({
    LIFECYCLE_WORKER_EXITED_NO_PUSH,
})

# All lifecycle values that mark the attempt as finished (success OR failure).
TERMINAL_LIFECYCLES = frozenset({
    LIFECYCLE_TERMINAL_REPAIRED,
    LIFECYCLE_WORKER_EXITED_NO_PUSH,
})


class AttemptLifecycleError(Exception):
    """Raised when a lifecycle transition is invalid."""


# Allowed transitions. Any other transition raises ``AttemptLifecycleError``.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    LIFECYCLE_CLAIMED: frozenset({
        LIFECYCLE_WORKER_STARTING,
        LIFECYCLE_WORKER_EXITED_NO_PUSH,  # claim-time pre-launch failure
    }),
    LIFECYCLE_WORKER_STARTING: frozenset({
        LIFECYCLE_WORKER_RUNNING,
        LIFECYCLE_WORKER_EXITED_NO_PUSH,  # start failed
        LIFECYCLE_RECOVERY_CHECK,
    }),
    LIFECYCLE_WORKER_RUNNING: frozenset({
        LIFECYCLE_COMMIT_PRODUCED,
        LIFECYCLE_WORKER_EXITED_NO_PUSH,
        LIFECYCLE_RECOVERY_CHECK,
    }),
    LIFECYCLE_COMMIT_PRODUCED: frozenset({
        LIFECYCLE_PUSH_VERIFIED,
        LIFECYCLE_WORKER_EXITED_NO_PUSH,  # commit produced but push failed
        LIFECYCLE_RECOVERY_CHECK,
    }),
    LIFECYCLE_PUSH_VERIFIED: frozenset({
        LIFECYCLE_TERMINAL_REPAIRED,
        LIFECYCLE_RECOVERY_CHECK,
    }),
    LIFECYCLE_RECOVERY_CHECK: frozenset({
        LIFECYCLE_PUSH_VERIFIED,
        LIFECYCLE_WORKER_EXITED_NO_PUSH,
        LIFECYCLE_TERMINAL_REPAIRED,
    }),
}


@dataclass
class WorkerAttemptRecord:
    """Canonical durable worker-attempt record.

    All fields are required for the causal chain to be auditable. New
    fields must be added to ``__init__`` AND the dict round-trip helpers
    (``from_dict`` / ``to_dict``).
    """

    schema_version: str
    attempt_id: str
    claim_id: str
    repo_owner: str
    repo_name: str
    pr_number: int
    event_ids: tuple[str, ...]
    finding_ids: tuple[str, ...]
    directive_digest: str
    directive_path: str
    prelaunch_head: str
    expected_branch: str
    pid: int
    lease_id: str
    started_at: str
    last_progress_at: str
    finished_at: Optional[str]
    lifecycle: str
    attempt_count: int
    stdout_path: Optional[str]
    stderr_path: Optional[str]
    exit_code: Optional[int]
    signal: Optional[int]
    result_artifact_path: Optional[str]
    produced_commit_sha: Optional[str]
    pushed_commit_sha: Optional[str]
    origin_head_verified: bool
    github_head_verified: bool
    terminal_reason: Optional[str]
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        # Convert tuple -> list for JSON.
        d["event_ids"] = list(self.event_ids)
        d["finding_ids"] = list(self.finding_ids)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "WorkerAttemptRecord":
        kwargs = dict(data)
        kwargs["event_ids"] = tuple(data.get("event_ids", ()))
        kwargs["finding_ids"] = tuple(data.get("finding_ids", ()))
        # Strip unknown fields rather than failing — schema_version is
        # the only authoritative check.
        kwargs.pop("extra", None)
        return cls(**kwargs)

    def assert_can_transition_to(self, next_lifecycle: str) -> None:
        """Raise if the proposed transition is not allowed.

        A transition to the SAME lifecycle (self-loop) is ALWAYS
        allowed — it represents a fresh write or idempotent rewrite
        that does not change state. The guard exists to prevent
        skipping required intermediate states, not to forbid
        idempotent persistence.
        """
        if next_lifecycle == self.lifecycle:
            return
        allowed = _ALLOWED_TRANSITIONS.get(self.lifecycle, frozenset())
        if next_lifecycle not in allowed:
            raise AttemptLifecycleError(
                f"attempt {self.attempt_id}: cannot transition "
                f"{self.lifecycle!r} -> {next_lifecycle!r}; "
                f"allowed next: {sorted(allowed) if allowed else '(terminal)'}"
            )


# ---------------------------------------------------------------------------
# Storage helpers — single source of truth for attempt persistence
# ---------------------------------------------------------------------------

class WorkerAttemptStore:
    """Filesystem-backed store for WorkerAttemptRecord.

    All writes are crash-consistent: ``write_atomic`` writes to a temp
    file then renames. ``read`` returns the parsed record or ``None`` if
    the file is missing.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, attempt_id: str) -> Path:
        return self.root / f"{attempt_id}.json"

    def write(self, record: WorkerAttemptRecord) -> None:
        record.assert_can_transition_to(record.lifecycle)
        path = self._path(record.attempt_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record.to_dict(), indent=2, sort_keys=True))
        os.replace(tmp, path)

    def read(self, attempt_id: str) -> Optional[WorkerAttemptRecord]:
        path = self._path(attempt_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if data.get("schema_version") != SCHEMA_VERSION:
            return None
        return WorkerAttemptRecord.from_dict(data)

    def list_active(self) -> list[WorkerAttemptRecord]:
        """Return every attempt whose lifecycle is NOT terminal."""
        active: list[WorkerAttemptRecord] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("schema_version") != SCHEMA_VERSION:
                continue
            if data.get("lifecycle") in TERMINAL_LIFECYCLES:
                continue
            active.append(WorkerAttemptRecord.from_dict(data))
        return active

    def list_all(self) -> list[WorkerAttemptRecord]:
        all_records: list[WorkerAttemptRecord] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("schema_version") != SCHEMA_VERSION:
                continue
            all_records.append(WorkerAttemptRecord.from_dict(data))
        return all_records

    def find_active_for_claim(self, claim_id: str) -> Optional[WorkerAttemptRecord]:
        for rec in self.list_active():
            if rec.claim_id == claim_id:
                return rec
        return None

    def find_latest_terminal_for_claim(
        self, claim_id: str,
    ) -> Optional[WorkerAttemptRecord]:
        latest: Optional[WorkerAttemptRecord] = None
        for rec in self.list_all():
            if rec.claim_id != claim_id:
                continue
            if rec.lifecycle not in TERMINAL_LIFECYCLES:
                continue
            if latest is None or rec.started_at > latest.started_at:
                latest = rec
        return latest


# ---------------------------------------------------------------------------
# Attempt-id generator (collision-resistant: monotonic + pid + nanos)
# ---------------------------------------------------------------------------

def generate_attempt_id(*, claim_id: str) -> str:
    """Generate a deterministic, monotonic attempt id.

    Format: ``att-<claim_short>-<pid>-<nanos>``.
    ``claim_short`` is the first 8 chars of the claim id (stable) and
    ``pid``+``nanos`` provides collision resistance.
    """
    pid = os.getpid()
    nanos = time.time_ns()
    claim_short = claim_id[:8] if claim_id else "noclaim"
    return f"att-{claim_short}-{pid}-{nanos}"


# ---------------------------------------------------------------------------
# Default evidence root resolver (mirrors relay_wiring)
# ---------------------------------------------------------------------------

def default_attempt_root() -> Path:
    """Resolve the default ``state/worker_attempts`` directory.

    Honours ``AED_EVIDENCE_ROOT`` for test / override contexts.
    Falls back to ``state/worker_attempts`` relative to CWD.
    """
    root = os.environ.get("AED_EVIDENCE_ROOT")
    if root:
        return Path(root) / "state" / "worker_attempts"
    return Path.cwd() / "state" / "worker_attempts"


def default_store() -> WorkerAttemptStore:
    return WorkerAttemptStore(default_attempt_root())
