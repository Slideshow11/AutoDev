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

# Round-41: terminal lifecycles that distinguish structured
# worker outcomes from generic failures.
LIFECYCLE_NO_CHANGES_REQUIRED = "NO_CHANGES_REQUIRED"
LIFECYCLE_WORKER_STARTUP_FAILED = "WORKER_STARTUP_FAILED"
LIFECYCLE_WORKER_EXECUTION_FAILED = "WORKER_EXECUTION_FAILED"

# Round-42: the head advanced but the worker attempt
# contains no recorded ``pushed_commit_sha`` for the new
# head. The head movement is unattributed and MUST NOT
# be promoted to PUSH_VERIFIED. The supervisor's
# head-rebind path treats this as an external / manual
# commit (e.g. an operator or another actor pushed while
# the worker happened to be running).
LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE = (
    "UNATTRIBUTED_HEAD_ADVANCE"
)

# Round-50.1 Section 6: the worker exited and the remote
# head advanced, but the worker did NOT write the required
# canonical WorkerResultArtifact. The supervisor MUST
# classify this distinctly from WORKER_EXITED_NO_PUSH (which
# means "no head movement, no result") and from
# UNATTRIBUTED_HEAD_ADVANCE (which means "head moved but we
# don't know whether a worker or an external actor pushed").
# WORKER_RESULT_MISSING means: the worker DID push a commit
# (head moved to a descendant of prelaunch_head during the
# worker's lifetime) but failed to write the result artifact,
# so we cannot attribute the commit to the worker. The
# attempt becomes RETRY_PENDING so a future retry can write
# the canonical result for the same generation.
LIFECYCLE_WORKER_RESULT_MISSING = (
    "WORKER_RESULT_MISSING"
)

# Terminal failure lifecycle values — the attempt is finished
# but the work item is RETRY_PENDING.
TERMINAL_FAILURE_LIFECYCLES = frozenset({
    LIFECYCLE_WORKER_EXITED_NO_PUSH,
    LIFECYCLE_WORKER_STARTUP_FAILED,
    LIFECYCLE_WORKER_EXECUTION_FAILED,
})

# All lifecycle values that mark the attempt as finished
# (success OR failure).
# Round-42: ``UNATTRIBUTED_HEAD_ADVANCE`` is a terminal
# state — the attempt is finished and the head movement
# is external. The worker's finding remains
# RETRY_PENDING (the next dispatch cycle will inspect
# the new head).
TERMINAL_LIFECYCLES = frozenset({
    LIFECYCLE_TERMINAL_REPAIRED,
    LIFECYCLE_NO_CHANGES_REQUIRED,
    LIFECYCLE_WORKER_EXITED_NO_PUSH,
    LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE,
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
        # Round-41: a worker that ran successfully and emitted
        # structured ``NO_CHANGES_REQUIRED`` proof transitions
        # directly to the new terminal-success lifecycle.
        LIFECYCLE_NO_CHANGES_REQUIRED,
        # A worker that started but did not reach
        # ``DIRECTIVE_ACCEPTED`` (e.g. session resume failed
        # after process spawn) is classified
        # ``WORKER_STARTUP_FAILED`` and remains RETRY_PENDING.
        LIFECYCLE_WORKER_STARTUP_FAILED,
        # A worker that ran but errored mid-execution (model
        # failure, exception, malformed directive) is
        # classified ``WORKER_EXECUTION_FAILED`` and remains
        # RETRY_PENDING.
        LIFECYCLE_WORKER_EXECUTION_FAILED,
        # Round-42: the head advanced past the worker's
        # prelaunch head but the worker attempt contains no
        # recorded ``pushed_commit_sha`` for the new head.
        # The head movement is unattributed. The attempt
        # becomes terminal WITHOUT being promoted to
        # PUSH_VERIFIED — the live head is an external
        # commit and the finding remains RETRY_PENDING.
        LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE,
    }),
    LIFECYCLE_COMMIT_PRODUCED: frozenset({
        LIFECYCLE_PUSH_VERIFIED,
        LIFECYCLE_WORKER_EXITED_NO_PUSH,  # commit produced but push failed
        LIFECYCLE_RECOVERY_CHECK,
        # Round-42: a produced-but-not-yet-pushed worker
        # can also be superseded by an external head
        # movement; the attempt becomes terminal as
        # ``UNATTRIBUTED_HEAD_ADVANCE`` (the worker's
        # commit is preserved but the head is owned by
        # another actor).
        LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE,
    }),
    LIFECYCLE_PUSH_VERIFIED: frozenset({
        LIFECYCLE_TERMINAL_REPAIRED,
        LIFECYCLE_RECOVERY_CHECK,
        # Round-42: a verified worker push can be
        # superseded by an external head movement
        # (e.g. the operator pushes again). The attempt
        # becomes terminal as ``UNATTRIBUTED_HEAD_ADVANCE``
        # for the new head, while preserving the worker's
        # recorded ``pushed_commit_sha``.
        LIFECYCLE_UNATTRIBUTED_HEAD_ADVANCE,
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
    # Round-42: multi-commit worker attempts may produce
    # more than one commit. The supervisor records an
    # ORDERED list of produced/pushed SHAs in
    # ``extra.produced_commit_shas`` /
    # ``extra.pushed_commit_shas``. The single-SHA fields
    # above are kept for backward compatibility (the
    # final commit on the chain).
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
        # Round-41: ``extra`` is a known field on the dataclass.
        # Preserve it through the round-trip so structured
        # no-op proof written by the worker survives to the
        # supervisor's poll_worker_attempt.
        kwargs["extra"] = dict(data.get("extra", {}) or {})
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
        allowed = _ALLOWED_TRANSITIONS.get(
            self.lifecycle, frozenset()
        )
        if next_lifecycle not in allowed:
            raise AttemptLifecycleError(
                f"attempt {self.attempt_id}: cannot transition "
                f"{self.lifecycle!r} -> {next_lifecycle!r}; "
                f"allowed next: "
                f"{sorted(allowed) if allowed else '(terminal)'}"
            )


# ---------------------------------------------------------------------------
# Round-42: canonical worker result artifact
# ---------------------------------------------------------------------------
#
# A ``WorkerResultArtifact`` is the durable, causally-bound
# proof that a specific worker attempt executed. The
# artifact is the SOLE source of truth for the worker's
# produced commits and pushed SHAs. The supervisor MUST
# NOT infer produced/pushed SHAs from generic remote
# movement.
#
# The artifact is written by the worker execution path
# immediately after each ``git commit`` and ``git push``
# invocation. The supervisor reads it from
# ``rec.result_artifact_path`` (set at attempt creation)
# after the worker exits.
# ---------------------------------------------------------------------------

# Round-42: worker result type enum.
RESULT_TYPE_REPAIR_COMMIT_PRODUCED = "REPAIR_COMMIT_PRODUCED"
RESULT_TYPE_REPAIR_PUSHED = "REPAIR_PUSHED"
RESULT_TYPE_NO_CHANGES_REQUIRED = "NO_CHANGES_REQUIRED"
RESULT_TYPE_WORKER_EXECUTION_FAILED = "WORKER_EXECUTION_FAILED"
RESULT_TYPE_COMMIT_PRODUCED_NOT_PUSHED = "COMMIT_PRODUCED_NOT_PUSHED"

# Result artifact schema version. Bump when the schema
# changes. The supervisor refuses to read artifacts
# whose schema_version does not match.
WORKER_RESULT_SCHEMA_VERSION = "autocoder.worker_result.v1"


@dataclass
class WorkerResultArtifact:
    """Canonical worker result artifact.

    Required fields (round-42 invariant):
        schema_version — schema identifier; supervisor
                         refuses unknown versions.
        attempt_id     — exact attempt that produced the
                         artifact. Bind a worker result to
                         exactly one attempt.
        claim_id       — exact claim.
        directive_digest — exact directive that the worker
                         executed.
        result_type    — one of RESULT_TYPE_*.
        produced_commit_shas — ORDERED list of commits the
                         worker actually created. Empty
                         list for NO_CHANGES_REQUIRED.
        pushed_commit_shas — ORDERED list of commits the
                         worker actually pushed. Empty
                         list when the worker did not push.
        completed_at   — ISO-8601 UTC.

    Optional fields:
        no_changes_required_proof — per-finding
                         classification + verification
                         summary (only for
                         NO_CHANGES_REQUIRED).
        tests_run / tests_passed — verification evidence.
        attempt_nonce — optional defense against
                         cross-attempt result adoption
                         (round-42 §15).
    """

    schema_version: str
    attempt_id: str
    claim_id: str
    directive_digest: str
    result_type: str
    produced_commit_shas: tuple[str, ...]
    pushed_commit_shas: tuple[str, ...]
    completed_at: str
    no_changes_required_proof: Optional[dict] = None
    tests_run: int = 0
    tests_passed: int = 0
    attempt_nonce: Optional[str] = None
    # round-42 §5: bound these even if not all are set.
    repo: Optional[str] = None
    pr_number: Optional[int] = None
    expected_branch: Optional[str] = None
    prelaunch_head: Optional[str] = None
    worker_session_id: Optional[str] = None
    worker_pid: Optional[int] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["produced_commit_shas"] = list(self.produced_commit_shas)
        d["pushed_commit_shas"] = list(self.pushed_commit_shas)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "WorkerResultArtifact":
        kwargs = dict(data)
        kwargs["produced_commit_shas"] = tuple(
            data.get("produced_commit_shas", ()) or ()
        )
        kwargs["pushed_commit_shas"] = tuple(
            data.get("pushed_commit_shas", ()) or ()
        )
        kwargs["extra"] = dict(data.get("extra", {}) or {})
        return cls(**kwargs)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    @classmethod
    def read(cls, path: Path) -> Optional["WorkerResultArtifact"]:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if data.get("schema_version") != WORKER_RESULT_SCHEMA_VERSION:
            return None
        return cls.from_dict(data)

    def validate_against_attempt(
        self, rec: "WorkerAttemptRecord",
    ) -> list[str]:
        """Return a list of invariant violations.

        The supervisor MUST call this before using the
        artifact to advance the controller state. The
        artifact is the SOLE source of truth for the
        worker's produced/pushed SHAs.
        """
        errors: list[str] = []
        if self.attempt_id != rec.attempt_id:
            errors.append(
                f"attempt_id mismatch: result={self.attempt_id} "
                f"record={rec.attempt_id}"
            )
        if self.claim_id != rec.claim_id:
            errors.append(
                f"claim_id mismatch: result={self.claim_id} "
                f"record={rec.claim_id}"
            )
        if self.directive_digest != rec.directive_digest:
            errors.append(
                f"directive_digest mismatch: result="
                f"{self.directive_digest} record="
                f"{rec.directive_digest}"
            )
        # NO_CHANGES_REQUIRED must have ZERO produced
        # and ZERO pushed commits.
        if self.result_type == RESULT_TYPE_NO_CHANGES_REQUIRED:
            if self.produced_commit_shas:
                errors.append(
                    "NO_CHANGES_REQUIRED has non-empty "
                    "produced_commit_shas"
                )
            if self.pushed_commit_shas:
                errors.append(
                    "NO_CHANGES_REQUIRED has non-empty "
                    "pushed_commit_shas"
                )
        return errors


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
