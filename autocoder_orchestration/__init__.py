"""AutoDev autonomous execution control plane.

This package owns the orchestration, authoritative state, evidence
gates, and worker/verifier role separation for an AutoDev run. It
cooperates with (but does not duplicate) the existing
``autocoder_supervisor`` and ``autocoder_lifecycle`` packages.

Public entry points:

- :class:`autocoder_orchestration.context.RunContext` — immutable
  run manifest.
- :class:`autocoder_orchestration.state_machine.StateMachine` —
  the canonical state machine.
- :class:`autocoder_orchestration.store.StateStore` — durable atomic
  state store.
- :class:`autocoder_orchestration.readiness.ReadinessEngine` —
  the canonical readiness evaluator.
- :class:`autocoder_orchestration.observer.StrictObserver` —
  the canonical strict readiness observer.
- :class:`autocoder_orchestration.reconciliation.Reconciler` —
  review-thread reconciliation contract.
- :class:`autocoder_orchestration.candidate.CandidateBuilder` —
  candidate construction with hard gate.
- :class:`autocoder_orchestration.verifier_handoff.VerifierHandoff` —
  typed handoff to a fresh verifier.
- :class:`autocoder_orchestration.merge_authorization.MergeAuthorization` —
  human merge authorization record.
- :class:`autocoder_orchestration.controller.Controller` — the
  orchestrator driving one run.
"""
from __future__ import annotations

from .context import (
    RunContext,
    make_run_context,
    generate_run_id,
    ACTOR_CONTROLLER,
    ACTOR_HUMAN,
    ACTOR_IMPL_WORKER,
    ACTOR_VERIFIER,
    ACTOR_OBSERVER,
    ACTOR_CANDIDATE_BUILDER,
)
from .state_machine import (
    StateMachine,
    StateConflict,
    InvalidTransition,
    STATE_PLANNED,
    STATE_IMPLEMENTING,
    STATE_AWAITING_CI,
    STATE_REPAIRING_REVIEW_FINDINGS,
    STATE_QUALIFYING_READINESS,
    STATE_READY_FOR_CANDIDATE,
    STATE_CANDIDATE_FROZEN,
    STATE_AWAITING_INDEPENDENT_VERIFICATION,
    STATE_VERIFYING,
    STATE_VERIFICATION_FAILED,
    STATE_VERIFICATION_REPAIR,
    STATE_AWAITING_MERGE_AUTHORIZATION,
    STATE_MERGE_AUTHORIZED,
    STATE_POST_MERGE_VERIFYING,
    STATE_COMPLETE,
    STATE_BLOCKED,
)
from .store import (
    StateStore,
    StateStoreError,
    StateCorruption,
    StateRevision,
    Lease,
    ProcessIdentity,
    open_lease,
    current_process_identity,
)
from .readiness import (
    ReadinessEngine,
    ReadinessCertificate,
    ReadinessDecision,
    GateResult,
    ReadOnlyGithubClient,
)
from .observer import (
    StrictObserver,
    Observation,
    ObservationLog,
)
from .reconciliation import (
    Reconciler,
    Finding,
    FindingDisposition,
    ThreadResolution,
)
from .candidate import (
    CandidateBuilder,
    CandidateError,
    Candidate,
    build_candidate_from_observations,
)
from .verifier_handoff import (
    VerifierHandoff,
    HandoffError,
    VerifierRoleGuard,
)
from .merge_authorization import (
    MergeAuthorization,
    MergeError,
    MergeExecutor,
    MergeRecord,
)
from .controller import Controller

__all__ = [
    "RunContext",
    "make_run_context",
    "generate_run_id",
    "ACTOR_CONTROLLER",
    "ACTOR_HUMAN",
    "ACTOR_IMPL_WORKER",
    "ACTOR_VERIFIER",
    "ACTOR_OBSERVER",
    "ACTOR_CANDIDATE_BUILDER",
    "StateMachine",
    "StateConflict",
    "InvalidTransition",
    "State",
    "STATE_PLANNED",
    "STATE_IMPLEMENTING",
    "STATE_AWAITING_CI",
    "STATE_REPAIRING_REVIEW_FINDINGS",
    "STATE_QUALIFYING_READINESS",
    "STATE_READY_FOR_CANDIDATE",
    "STATE_CANDIDATE_FROZEN",
    "STATE_AWAITING_INDEPENDENT_VERIFICATION",
    "STATE_VERIFYING",
    "STATE_VERIFICATION_FAILED",
    "STATE_VERIFICATION_REPAIR",
    "STATE_AWAITING_MERGE_AUTHORIZATION",
    "STATE_MERGE_AUTHORIZED",
    "STATE_POST_MERGE_VERIFYING",
    "STATE_COMPLETE",
    "STATE_BLOCKED",
    "StateStore",
    "StateStoreError",
    "StateCorruption",
    "StateRevision",
    "Lease",
    "ProcessIdentity",
    "open_lease",
    "current_process_identity",
    "ReadinessEngine",
    "ReadinessCertificate",
    "ReadinessDecision",
    "GateResult",
    "ReadOnlyGithubClient",
    "StrictObserver",
    "Observation",
    "ObservationLog",
    "Reconciler",
    "Finding",
    "FindingDisposition",
    "ThreadResolution",
    "CandidateBuilder",
    "CandidateError",
    "Candidate",
    "build_candidate_from_observations",
    "VerifierHandoff",
    "HandoffError",
    "VerifierRoleGuard",
    "MergeAuthorization",
    "MergeError",
    "MergeExecutor",
    "MergeRecord",
    "Controller",
]
