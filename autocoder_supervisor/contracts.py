"""Supervisor contracts — strongly-typed definitions for every
supervisor concept.

These contracts are the canonical machine-readable description
of the supervisor's data model. The behavioural invariants in
``INVARIANTS.md`` are enforced by the implementation in
``supervisor.py`` and asserted by tests in
``tests/test_autocoder_supervisor.py``.

The contracts are intentionally narrow: they describe the
*shape* of each artifact, not its semantic behaviour. Behaviour
lives in the invariant ledger and in the supervisor code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, TypedDict


# ---------------------------------------------------------------------------
# 1. Supervisor configuration
# ---------------------------------------------------------------------------


class SupervisorConfigDict(TypedDict, total=False):
    """TOML-friendly configuration schema for the supervisor.

    All fields are required unless marked optional. The loader
    (``config.load_config``) validates the values and rejects
    unsafe entries (tokens, absolute user-specific paths).
    """

    schema_version: str
    instance_id: str
    state_dir: str
    working_checkout: str
    log_path: str
    heartbeat_path: str
    lock_path: str

    # Worker-launch configuration
    worker_command: List[str]
    worker_session_id: str
    worker_session_name: str
    cooldown_seconds: int
    resume_prompt_template: str

    # Policy
    human_boundary: Literal["merge_only"]
    required_review_providers: List[str]
    optional_review_providers: List[str]
    provider_states_are_independent: bool
    post_codex_recovery_request: bool

    # Cadence
    heartbeat_seconds: int
    quiet_window_seconds: int
    quota_retry_initial_seconds: int
    quota_retry_backoff_seconds: int
    quota_backoff_after_retry_count: int

    # Per-provider quota reset times (optional; absent = never paused by time)
    provider_quota_reset: Dict[str, str]


@dataclass
class SupervisorConfig:
    """Strongly-typed supervisor configuration object.

    ``from_dict`` validates the input and constructs the object;
    ``from_file`` reads a TOML file and calls ``from_dict``.

    The supervisor module populates its module-level globals
    from a ``SupervisorConfig`` instance at import time so that
    existing tests that monkeypatch those globals continue to
    work unchanged.
    """

    schema_version: str
    instance_id: str
    state_dir: str
    working_checkout: str
    log_path: str
    heartbeat_path: str
    lock_path: str

    worker_command: List[str]
    worker_session_id: str
    worker_session_name: str
    cooldown_seconds: int
    resume_prompt_template: str

    human_boundary: str
    required_review_providers: List[str]
    optional_review_providers: List[str]
    provider_states_are_independent: bool
    post_codex_recovery_request: bool

    heartbeat_seconds: int
    quiet_window_seconds: int
    quota_retry_initial_seconds: int
    quota_retry_backoff_seconds: int
    quota_backoff_after_retry_count: int

    provider_quota_reset: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls,
        data: SupervisorConfigDict,
        *,
        reject_user_paths: bool = True,
    ) -> "SupervisorConfig":
        """Validate and construct a ``SupervisorConfig``.

        ``reject_user_paths`` defaults to True. The strict
        guard is the canonical contract: any *persisted*
        configuration (e.g. one loaded from a TOML file via
        ``load_config``) is rejected if it points at an
        absolute user-specific path. ``default_config_from_env``
        and tests pass ``reject_user_paths=False`` because
        their in-process configurations are never persisted
        to the source tree.

        Raises ``ValueError`` on any invalid input. The
        validator explicitly rejects tokens, cookies, and
        absolute user-specific paths so an accidentally
        committed secret cannot land in the source tree.
        """
        from .config import validate_config_dict

        validate_config_dict(data, reject_user_paths=reject_user_paths)
        return cls(
            schema_version=data["schema_version"],
            instance_id=data["instance_id"],
            state_dir=data["state_dir"],
            working_checkout=data["working_checkout"],
            log_path=data["log_path"],
            heartbeat_path=data["heartbeat_path"],
            lock_path=data["lock_path"],
            worker_command=list(data["worker_command"]),
            worker_session_id=data["worker_session_id"],
            worker_session_name=data["worker_session_name"],
            cooldown_seconds=int(data["cooldown_seconds"]),
            resume_prompt_template=data["resume_prompt_template"],
            human_boundary=data["human_boundary"],
            required_review_providers=list(
                data["required_review_providers"]
            ),
            optional_review_providers=list(
                data["optional_review_providers"]
            ),
            provider_states_are_independent=bool(
                data["provider_states_are_independent"]
            ),
            post_codex_recovery_request=bool(
                data["post_codex_recovery_request"]
            ),
            heartbeat_seconds=int(data["heartbeat_seconds"]),
            quiet_window_seconds=int(data["quiet_window_seconds"]),
            quota_retry_initial_seconds=int(
                data["quota_retry_initial_seconds"]
            ),
            quota_retry_backoff_seconds=int(
                data["quota_retry_backoff_seconds"]
            ),
            quota_backoff_after_retry_count=int(
                data["quota_backoff_after_retry_count"]
            ),
            provider_quota_reset=dict(data.get("provider_quota_reset", {})),
        )


# ---------------------------------------------------------------------------
# 2. Provider policy
# ---------------------------------------------------------------------------


class ProviderPolicy(TypedDict, total=False):
    """Per-provider policy entry persisted into readiness state."""

    bot_logins: List[str]
    trigger_handle: str
    quota_patterns: List[str]
    use_reviews_api: bool
    required_for_current_repair_round: bool
    required_for_final_merge: bool
    quota_reset_at: Optional[str]


# ---------------------------------------------------------------------------
# 3. Readiness state
# ---------------------------------------------------------------------------


# The state machine is intentionally narrow:
#   ACTIVE_REPAIR -> PROVISIONAL_READY -> AWAITING_MERGE_AUTHORIZATION
# The terminal state ``MERGED`` is only written by the merge
# authorization command and is reached from AWAITING_MERGE_AUTHORIZATION
# only after the operator authorizes the merge.
StateLiteral = Literal[
    "ACTIVE_REPAIR",
    "PROVISIONAL_READY",
    "AWAITING_MERGE_AUTHORIZATION",
    "MERGED",
]


class ReadinessStateDict(TypedDict, total=False):
    """Persisted readiness state.

    ``state`` is the current machine position;
    ``achieved_at`` / ``revoked_at`` are ISO timestamps of the
    most recent transition; ``head_sha`` is the PR head that
    the readiness was measured against; ``reason`` is the
    durable revocation reason.
    """

    state: StateLiteral
    achieved_at: Optional[str]
    revoked_at: Optional[str]
    head_sha: Optional[str]
    head_sha_at_revoke: Optional[str]
    reason: Optional[str]
    policy: Dict[str, Any]
    terminal_transition: Dict[str, Any]


# ---------------------------------------------------------------------------
# 4. Exact-head evidence snapshot
# ---------------------------------------------------------------------------


class ReviewerFormalReviewDict(TypedDict, total=False):
    id: int
    submitted_at: str
    commit_id: str
    provider: Optional[str]
    login: str


class ReviewerCommentDict(TypedDict, total=False):
    id: int
    created_at: str
    login: str
    body: str


class ReviewerCheckDict(TypedDict, total=False):
    conclusion: Optional[str]
    status: Optional[str]
    run_id: Optional[str]


class ReviewerThreadDict(TypedDict, total=False):
    resolved: bool
    outdated: bool


class ReviewerProviderSnapshotDict(TypedDict, total=False):
    paused: bool
    in_progress: bool
    latest_review_ts: Optional[str]
    latest_comment_id: Optional[int]


class ExactHeadSnapshotDict(TypedDict, total=False):
    """Snapshot of the live PR and provider surfaces."""

    captured_at: str
    head_sha: Optional[str]
    head_match: bool
    mergeable: bool
    formal_reviews: List[ReviewerFormalReviewDict]
    review_threads: Dict[str, ReviewerThreadDict]
    issue_comments: List[ReviewerCommentDict]
    required_checks: Dict[str, ReviewerCheckDict]
    providers: Dict[str, ReviewerProviderSnapshotDict]
    unconsumed_event_ids: List[str]


# ---------------------------------------------------------------------------
# 5. Actionable reviewer event
# ---------------------------------------------------------------------------


class ActionableEventDict(TypedDict, total=False):
    """An actionable reviewer event detected by the supervisor.

    Each event carries a stable ``id`` so dedup across
    heartbeats is straightforward and so the durable
    consumption record (``launched_events.json``) can be
    authoritative.
    """

    id: str
    kind: str
    review_id: Optional[int]
    comment_id: Optional[int]
    thread_id: Optional[str]
    check: Optional[str]
    provider: Optional[str]


class UnconsumedEventsDict(TypedDict):
    """Envelope persisted into ``unconsumed_events.json``."""

    events: List[ActionableEventDict]


class LaunchedEventsDict(TypedDict):
    """Envelope persisted into ``launched_events.json``.

    This is the durable dedup record. Even if the unconsumed
    events list is cleared after the worker successfully repairs
    the underlying issue, the launched_events record persists
    so the supervisor never launches a duplicate worker for the
    same event.
    """

    ids: List[str]


# ---------------------------------------------------------------------------
# 6. Durable event-consumption record
# ---------------------------------------------------------------------------


class EventConsumptionRecordDict(TypedDict, total=False):
    """A single durable record of an event being actioned.

    Written when ``mark_event_launched`` is called. Used for
    post-hoc audit and for the invariant that any event can be
    traced from detection to worker-launch to outcome.
    """

    event_id: str
    consumed_at: str
    head_sha: str
    worker_pid: Optional[int]


# ---------------------------------------------------------------------------
# 7. Worker lease
# ---------------------------------------------------------------------------


class WorkerLeaseDict(TypedDict, total=False):
    """Durable worker lease written to ``state/worker_lease.json``.

    The lease guarantees that exactly one writer is alive per
    PR/run. The supervisor validates the lease on every
    heartbeat (PID alive, start-time evidence matches, cmdline
    contains the recorded session id) so PID reuse or a
    runaway unrelated process can never steal the lease.
    """

    supervisor_instance_id: str
    run_id: str
    pr_number: int
    session_id: str
    session_name: str
    authoritative_head_at_launch: str
    pid: int
    pgid: int
    start_time_evidence: Dict[str, Any]
    launched_at: str
    heartbeat_at: str
    cmd: List[str]


# ---------------------------------------------------------------------------
# 8. Worker-launch receipt
# ---------------------------------------------------------------------------


class WorkerLaunchReceiptDict(TypedDict, total=False):
    """Receipt written immediately after worker launch.

    The receipt is the canonical proof that a worker was
    launched in response to a specific event id. If the
    supervisor crashes between marking the event actionable
    and the worker launch, the receipt is missing and the
    next heartbeat treats the event as still actionable (so
    it is launched again on the next iteration — see
    invariant #12).
    """

    event_id: str
    launched_at: str
    worker_pid: int
    head_sha: str
    supervisor_instance_id: str


# ---------------------------------------------------------------------------
# 9. Cooldown state
# ---------------------------------------------------------------------------


class CooldownStateDict(TypedDict):
    """Persisted cooldown timestamp.

    The cooldown prevents the supervisor from launching
    back-to-back workers. The timestamp is persisted so a
    supervisor restart cannot bypass the cooldown window.
    """

    last_resume_at: str


# ---------------------------------------------------------------------------
# 10. Terminal merge evidence
# ---------------------------------------------------------------------------


class TerminalMergeEvidenceDict(TypedDict, total=False):
    """Evidence persisted at the moment the PR is merged.

    This file is the durable terminal-state record. It records
    the authorized head, the resulting merge commit, and the
    proof that the supervisor was the sole writer and the
    merge authorization was bound to the exact head.
    """

    schema: str
    captured_utc: str
    repository: str
    pr_number: int
    branch: str
    authorized_head_sha: str
    merge_commit_sha: str
    merge_method: str
    merge_commit_parents: List[str]
    merge_commit_reachable_from_main: bool
    pre_merge_guard_evidence: Dict[str, Any]
    post_merge_evidence: Dict[str, Any]
