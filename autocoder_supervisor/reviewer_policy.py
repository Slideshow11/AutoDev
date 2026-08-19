"""Round-C23 reviewer-orchestration policy + freshness + trigger
planner.

The C23 directive surfaced a real autonomy gap: when the relay
observes a new repair head, the reviewer lifecycle was not
automated. AutoDev handled review evidence and repair state, but
the operator had to manually post ``@codex review`` to obtain
fresh exact-head evidence. This module is the smallest reliable
reviewer-orchestration layer that closes the gap without
redesigning the wider review system.

The module exposes three concrete classes:

- :class:`ReviewerPolicy` — declarative per-provider policy
  (required/optional, auto-run behavior, trigger handle, budget,
  freshness check, unavailable behavior).

- :class:`ReviewerFreshnessChecker` — given a snapshot and the
  current exact head, returns a ``{provider: FreshnessResult}``
  map. Freshness is anchored to the EXACT head; a review whose
  submission commit does not match the current head is
  ``STALE``.

- :class:`ReviewerTriggerPlanner` — given the policy map,
  the freshness map, and the per-provider request ledger, decides
  whether to NOT_NEEDED, WAITING_FOR_AUTO, REQUEST, or BLOCK
  each provider. Dedup across supervisor slices is handled by
  reading the durable review-request ledger.

The module deliberately does NOT call ``post_review_request``
itself (the supervisor's canonical review-request seam owns the
remote mutation). The planner returns a ``ReviewerTriggerPlan``
that the supervisor's heartbeat loop applies.

Persistence: per-provider, per-head request records live under
``REVIEW_REQUESTS_DIR / {provider}__{head}.json`` and are
written by :func:`write_review_request` (round-54/C22 §4).
The planner READS that ledger to dedup; the supervisor's
canonical writer is unchanged.

Configuration: the policy defaults are loaded from the
supervisor's ``PROVIDERS`` map (round-32) and may be overridden
per-deployment via environment variables.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple


# === Freshness vocabulary ===

#: A review result is bound to the EXACT current head; nothing
#: missing or stale is allowed.
FRESHNESS_FRESH = "FRESH"
#: A review result exists but its ``commit_id`` / ``reviewed_head``
#: does NOT match the current head; the thread evidence must be
#: re-anchored before qualifying.
FRESHNESS_STALE = "STALE"
#: A review request has been issued; the provider's terminal
#: evidence has not yet arrived. Bound to the same exact head.
FRESHNESS_PENDING = "PENDING"
#: The provider has reported an explicit rate-limit / usage-limit
#: marker; AutoDev must not re-request within the cooldown.
FRESHNESS_BLOCKED_BUDGET = "BLOCKED_BUDGET"
#: Provider is OPTIONAL and the policy says stale evidence is OK
#: (e.g. CodeRabbit on a repair head when policy marks it
#: ``initial_only``).
FRESHNESS_OPTIONAL_STALE = "OPTIONAL_STALE"
#: Provider has no auto-run on push and no in-flight request
#: yet; AutoDev must wait the grace period before requesting.
FRESHNESS_NOT_NEEDED = "NOT_NEEDED"


# === Policy defaults (per Slideshow11/AutoDev observed behavior) ===

# These defaults reflect the C23 directive's observations:
#
# CODEX
#   - required for autonomy qualification
#   - initial PR review may auto-run
#   - new repair head requires a fresh exact-head review
#   - AutoDev may post ``@codex review`` when no fresh
#     exact-head review exists after the grace period
#   - budget: unlimited per repair head; one request per head
#     (dedup at the head boundary)
#
# SOURCERY
#   - observed to auto-run on pushes
#   - AutoDev waits for it; no manual trigger
#   - status configurable (default: OPTIONAL — Sourcery's
#     auto-run is treated as a quality signal, not a hard
#     gate)
#
# CODERABBIT
#   - quota-limited; the C23 directive explicitly forbids
#     requesting on every repair head
#   - default policy: ``initial_only`` (one explicit request
#     per PR lifecycle); subsequent re-reviews require policy
#     authorization
#   - stale / absent CodeRabbit on a repair head is OK when
#     policy marks it optional; the readiness gate's ``REQUIRED``
#     logic is the canonical gate

DEFAULT_POLICY_PROFILES: Dict[str, Dict[str, Any]] = {
    "codex": {
        "required": True,
        "auto_runs_on_pr_creation": True,
        "auto_runs_on_push": False,
        "auto_trigger": True,
        "trigger_handle": "@codex review",
        "budget_per_pr": None,  # unlimited; one per head
        "max_requests_per_head": 1,
        "freshness_grace_seconds": 180,  # bounded grace
        "unavailable_behavior": "BLOCK",  # fail closed
        "request_cooldown_seconds": 600,
    },
    "sourcery": {
        "required": False,
        "auto_runs_on_pr_creation": True,
        "auto_runs_on_push": True,
        "auto_trigger": False,
        "trigger_handle": "@sourcery-ai review",
        "budget_per_pr": None,
        "max_requests_per_head": 0,  # never trigger
        "freshness_grace_seconds": 300,
        "unavailable_behavior": "IGNORE",
        "request_cooldown_seconds": 3600,
    },
    "coderabbit": {
        "required": True,
        "auto_runs_on_pr_creation": True,
        "auto_runs_on_push": False,
        "auto_trigger": True,
        "trigger_handle": "@coderabbitai review",
        "budget_per_pr": 1,  # initial only
        "max_requests_per_head": 1,
        "freshness_grace_seconds": 180,
        "unavailable_behavior": "BLOCK",
        "request_cooldown_seconds": 600,
    },
}


@dataclass(frozen=True)
class ReviewerPolicy:
    """Per-provider reviewer-orchestration policy.

    Each field documents a single dimension of the C23
    decision matrix:

    - ``name``: the provider key (matches ``PROVIDERS``).
    - ``required``: if True, a STALE / MISSING review blocks
      qualification (``evaluate_readiness`` returns False).
    - ``auto_runs_on_pr_creation``: True for providers that
      review the first commit automatically (CodeRabbit / Codex
      on PR creation).
    - ``auto_runs_on_push``: True for providers that
      automatically review every subsequent push (Sourcery on
      GitHub).
    - ``auto_trigger``: True if AutoDev may POST a review
      request when no fresh exact-head review exists.
    - ``trigger_handle``: the canonical mention string
      (``@codex review``, ``@coderabbitai review``,
      ``@sourcery-ai review``). The supervisor's
      ``post_review_request`` reads ``trigger_handle`` from the
      existing ``PROVIDERS`` map; this policy mirrors it for
      the planner to read.
    - ``budget_per_pr``: explicit request budget for the PR
      lifecycle (``None`` = unlimited). CodeRabbit defaults to
      ``1`` per the C23 directive's policy.
    - ``max_requests_per_head``: per-head dedup cap (one
      explicit request per head, regardless of the budget).
      Prevents accidental duplicate triggers.
    - ``freshness_grace_seconds``: wait this long for
      ``auto_runs_on_push`` providers to deliver their
      automatic review before AutoDev posts its own trigger.
      Default 180s; configurable per provider.
    - ``unavailable_behavior``: ``"BLOCK"`` (fail closed) or
      ``"IGNORE"`` (mark stale and proceed).
    - ``request_cooldown_seconds``: minimum gap between two
      requests for the same provider. Prevents accidental
      spam across retries.
    """

    name: str
    required: bool = False
    auto_runs_on_pr_creation: bool = False
    auto_runs_on_push: bool = False
    auto_trigger: bool = False
    trigger_handle: str = ""
    budget_per_pr: Optional[int] = None
    max_requests_per_head: int = 1
    freshness_grace_seconds: int = 180
    unavailable_behavior: str = "BLOCK"
    request_cooldown_seconds: int = 600


@dataclass(frozen=True)
class FreshnessResult:
    """Per-provider freshness assessment for one exact head."""

    provider: str
    state: str  # one of FRESHNESS_*
    reviewed_head: Optional[str] = None
    reviewed_at: Optional[str] = None
    review_id: Optional[str] = None
    reason: str = ""

    @property
    def is_fresh(self) -> bool:
        return self.state == FRESHNESS_FRESH


# === Freshness checker ===

def _parse_iso8601_utc(value: object) -> Optional[float]:
    """Minimal ISO-8601 -> Unix-seconds parser; returns ``None``
    on any malformed input. Used only to compare ``requested_at``
    timestamps against the bounded grace period."""
    if not isinstance(value, str) or not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _provider_review_record_for_head(
    *,
    snap: dict,
    provider: str,
    head_sha: str,
) -> Optional[dict]:
    """Find the provider's most recent review (formal OR
    walkthrough-completed comment) and return a small record
    suitable for freshness comparison.

    Returns ``None`` when no provider evidence is present in the
    snapshot.

    The strongest available evidence is consulted first:

    1. ``snap["formal_reviews"]`` filtered to ``provider``:
       GitHub's ``submitted_at`` plus the ``commit_id`` field
       (round-27+) bind a review to a specific commit. We
       compare ``commit_id`` to the current head.
    2. ``snap["_provider_issue_comments"]`` filtered to the
       provider: a walkthrough-completed comment. The
       ``created_at`` field is the strongest time anchor
       available; ``commit_id`` is not exposed for issue
       comments, so freshness is bound to the wall-clock
       relative to the current head's observed ``head_match``.
       The HEAD timestamp is recorded separately; if the
       provider explicitly tagged the head (e.g. CodeRabbit's
       walkthrough has a per-comment ``commit_id``), the
       snapshot's ``provider_surfaces`` carries it.
    3. ``snap["provider_surfaces"]``: per-provider exact-head
       bindings persisted by ``capture_live_snapshot``.

    The checker prefers formal reviews over issue comments when
    both are available for the same provider.
    """
    if not isinstance(snap, dict):
        return None
    # 1. Formal reviews (GitHub's ``/repos/.../pulls/{n}/reviews``).
    for r in snap.get("formal_reviews") or []:
        if not isinstance(r, dict):
            continue
        if r.get("provider") != provider:
            continue
        cid = r.get("commit_id")
        # ``commit_id`` is the SHA the review was submitted
        # against. When GitHub returns ``None`` we treat it as
        # a pre-binding-anchor review and prefer the
        # ``head_sha`` field if the review was specifically
        # tagged. Otherwise the freshness falls back to
        # wall-clock comparison against the head observation
        # time.
        submitted_at = r.get("submitted_at")
        review_id = r.get("review_id") or r.get("id")
        if cid is None and r.get("head_sha"):
            cid = r["head_sha"]
        if cid == head_sha:
            return {
                "commit_id": cid,
                "submitted_at": submitted_at,
                "review_id": review_id,
                "kind": "formal_review",
            }
        # The review's commit_id is different from the current
        # head; record it as the latest stale record so the
        # caller can decide whether the wall-clock is within
        # the grace period.
        if cid:
            return {
                "commit_id": cid,
                "submitted_at": submitted_at,
                "review_id": review_id,
                "kind": "formal_review_stale_commit",
            }
        # No commit_id at all: treat as worst-case (unknown).
        return {
            "commit_id": None,
            "submitted_at": submitted_at,
            "review_id": review_id,
            "kind": "formal_review_unbound",
        }
    # 2. Provider issue comments (walkthrough / review-complete
    #    markers).
    for c in snap.get("_provider_issue_comments", {}).get(provider, []) or []:
        if not isinstance(c, dict):
            continue
        return {
            "commit_id": None,  # issue comments don't expose
            # commit_id; bound by wall-clock only
            "submitted_at": c.get("created_at"),
            "review_id": c.get("id"),
            "kind": "issue_comment",
        }
    # 3. Provider surfaces (per-head explicit binding).
    surf = (snap.get("provider_surfaces") or {}).get(provider)
    if isinstance(surf, dict):
        for head_key, rec in (surf.get("heads") or {}).items():
            if head_key == head_sha and isinstance(rec, dict):
                return {
                    "commit_id": head_key,
                    "submitted_at": rec.get("reviewed_at"),
                    "review_id": rec.get("review_id"),
                    "kind": "provider_surface",
                }
    return None


def _read_request_ledger(
    ledger_path: Optional[Path],
    provider: str,
    head_sha: str,
) -> Optional[dict]:
    """Read the canonical per-provider, per-head request record
    from the supervisor's review-request ledger. Returns
    ``None`` when the ledger does not exist or is malformed."""
    if ledger_path is None:
        return None
    p = ledger_path / f"{provider}__{head_sha}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _list_superseded_request_records(
    ledger_path: Optional[Path],
    provider: str,
    current_head: str,
) -> List[dict]:
    """Return the list of request records for ``provider``
    whose ``request_head`` was marked SUPERSEDED by the
    supervisor (round-54/C22 §4 lifecycle). Used by the budget
    accounting so an attempted request that was correctly
    superseded at head advance does not consume a budget slot."""
    if ledger_path is None:
        return []
    out: List[dict] = []
    if not ledger_path.exists():
        return out
    for p in ledger_path.glob(f"{provider}__*.superseded.json"):
        try:
            out.append(json.loads(p.read_text()))
        except Exception:
            continue
    return out


def check_provider_freshness(
    *,
    provider: str,
    policy: ReviewerPolicy,
    snap: dict,
    head_sha: str,
    ledger_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> FreshnessResult:
    """Round-C23 freshness check for one provider on the
    current exact head.

    Returns a :class:`FreshnessResult` whose ``state`` is one of
    the ``FRESHNESS_*`` constants.

    Decision order (highest priority first):

    1. ``snap["providers"][provider]["paused"]`` is True
       (provider reported rate-limit): return
       ``BLOCKED_BUDGET``.
    2. ``snap["providers"][provider]["in_progress"]`` is True
       (provider is actively reviewing): return ``PENDING``.
    3. A formal review record exists in the snapshot with
       ``commit_id == head_sha``: return ``FRESH``.
    4. A formal review record exists with a different
       ``commit_id``: return ``STALE`` (anchored to a prior
       head).
    5. A ``provider_surfaces`` entry exists for the current
       head: return ``FRESH``.
    6. A request is in flight (REQUEST_INTENT / REQUEST_SENT /
       ACKNOWLEDGED) for the current head: return ``PENDING``.
    7. Otherwise: return ``NOT_NEEDED`` if the provider is
       optional, or ``PENDING`` if the provider is required and
       AutoDev should request one.
    """
    now_ts = float(now) if now is not None else time.time()
    if not isinstance(head_sha, str) or not head_sha:
        return FreshnessResult(
            provider=provider,
            state=FRESHNESS_PENDING if policy.required else FRESHNESS_OPTIONAL_STALE,
            reason="missing_head_sha",
        )
    # Read the canonical ``snap["providers"]`` block first.
    providers_block = (
        snap.get("providers") if isinstance(snap, dict) else None
    ) or {}
    pblock = providers_block.get(provider) if isinstance(
        providers_block, dict
    ) else None
    if isinstance(pblock, dict):
        if pblock.get("paused"):
            return FreshnessResult(
                provider=provider,
                state=FRESHNESS_BLOCKED_BUDGET,
                reason="provider_reported_paused",
            )
        if pblock.get("in_progress"):
            return FreshnessResult(
                provider=provider,
                state=FRESHNESS_PENDING,
                reason="provider_in_progress",
            )
    # Strongest evidence: formal review with commit_id ==
    # current head.
    record = _provider_review_record_for_head(
        snap=snap, provider=provider, head_sha=head_sha,
    )
    if record is not None:
        if record.get("kind") == "formal_review" and record.get(
            "commit_id"
        ) == head_sha:
            return FreshnessResult(
                provider=provider,
                state=FRESHNESS_FRESH,
                reviewed_head=record["commit_id"],
                reviewed_at=record.get("submitted_at"),
                review_id=record.get("review_id"),
                reason="formal_review_commit_id_match",
            )
        # Formal review exists but commit_id differs from
        # current head; the bound evidence is anchored to a
        # prior head. This is the audit's "anchored to prior
        # head" case.
        if record.get("commit_id") and record["commit_id"] != head_sha:
            return FreshnessResult(
                provider=provider,
                state=FRESHNESS_STALE,
                reviewed_head=record["commit_id"],
                reviewed_at=record.get("submitted_at"),
                review_id=record.get("review_id"),
                reason="formal_review_anchored_to_prior_head",
            )
        # Formal review unbound (no commit_id): treat as
        # STALE only when the policy requires exact-head
        # binding.
        if record.get("kind") == "formal_review_unbound":
            if policy.required:
                return FreshnessResult(
                    provider=provider,
                    state=FRESHNESS_STALE,
                    reviewed_at=record.get("submitted_at"),
                    review_id=record.get("review_id"),
                    reason="formal_review_unbound_no_commit_id",
                )
            return FreshnessResult(
                provider=provider,
                state=FRESHNESS_OPTIONAL_STALE,
                reviewed_at=record.get("submitted_at"),
                review_id=record.get("review_id"),
                reason="formal_review_unbound_optional_provider",
            )
    # Provider surfaces (per-head binding persisted by
    # capture_live_snapshot).
    surf = (snap.get("provider_surfaces") or {}).get(provider)
    if isinstance(surf, dict):
        for head_key, rec in (surf.get("heads") or {}).items():
            if head_key == head_sha and isinstance(rec, dict):
                return FreshnessResult(
                    provider=provider,
                    state=FRESHNESS_FRESH,
                    reviewed_head=head_key,
                    reviewed_at=rec.get("reviewed_at"),
                    review_id=rec.get("review_id"),
                    reason="provider_surface_exact_head_match",
                )
    # A request is in flight for the current head; the
    # provider's terminal evidence is not yet here.
    ledger = _read_request_ledger(ledger_path, provider, head_sha)
    if isinstance(ledger, dict):
        lifecycle = ledger.get("lifecycle") or ""
        if lifecycle in ("REQUEST_INTENT", "REQUEST_SENT", "ACKNOWLEDGED"):
            # Has the requested grace elapsed?
            requested_at_ts = _parse_iso8601_utc(ledger.get("requested_at"))
            if requested_at_ts is None:
                return FreshnessResult(
                    provider=provider,
                    state=FRESHNESS_PENDING,
                    reason="request_in_flight_no_timestamp",
                )
            grace_remaining = (
                policy.freshness_grace_seconds
                - (now_ts - requested_at_ts)
            )
            if grace_remaining > 0:
                return FreshnessResult(
                    provider=provider,
                    state=FRESHNESS_PENDING,
                    reason=f"request_in_flight_within_grace:{int(grace_remaining)}s",
                )
            # Grace elapsed with no ACKNOWLEDGED transition:
            # still PENDING (operator's grace timer); the
            # planner must decide whether to escalate.
            return FreshnessResult(
                provider=provider,
                state=FRESHNESS_PENDING,
                reason="request_in_flight_grace_elapsed",
            )
    # No provider evidence, no in-flight request.
    if policy.required:
        # Required providers missing fresh exact-head evidence
        # return PENDING so the planner knows to issue a
        # request (subject to grace + budget).
        return FreshnessResult(
            provider=provider,
            state=FRESHNESS_PENDING,
            reason="no_exact_head_evidence_required_provider",
        )
    return FreshnessResult(
        provider=provider,
        state=FRESHNESS_OPTIONAL_STALE,
        reason="no_exact_head_evidence_optional_provider",
    )


# === Trigger planner ===

@dataclass(frozen=True)
class ReviewerTriggerPlan:
    """Per-provider trigger decision returned by the planner.

    One entry per provider the planner was asked to evaluate.
    ``action`` is one of:

    - ``"NOT_NEEDED"``: provider has fresh exact-head evidence
      already; no action.
    - ``"WAITING_FOR_AUTO"``: provider may auto-run on the
      next push; planner has not yet exceeded the grace
      period.
    - ``"REQUEST"``: AutoDev should post the provider's
      ``trigger_handle`` as a top-level PR comment. The
      supervisor's heartbeat loop applies this via
      ``post_review_request``.
    - ``"BLOCK"``: a required reviewer cannot be obtained
      (budget exhausted, grace elapsed with no terminal
      evidence). The supervisor routes to BLOCKED; readiness
      gate denies qualification.
    """

    provider: str
    action: str
    reason: str
    last_request_at: Optional[str] = None
    last_lifecycle: Optional[str] = None
    request_id: Optional[str] = None
    freshness: Optional[FreshnessResult] = None


def _count_active_request_records(
    *,
    ledger_path: Optional[Path],
    provider: str,
    head_sha: str,
    superseded_records: List[dict],
) -> int:
    """Count the number of REQUEST_INTENT / REQUEST_SENT /
    ACKNOWLEDGED / REVIEW_COMPLETE records for ``provider`` on
    ``head_sha``, minus the SUPERSEDED set. Used to enforce
    ``max_requests_per_head`` and ``budget_per_pr``."""
    if ledger_path is None:
        return 0
    active = _read_request_ledger(ledger_path, provider, head_sha)
    if not isinstance(active, dict):
        return 0
    lifecycle = active.get("lifecycle")
    if lifecycle not in (
        "REQUEST_INTENT",
        "REQUEST_SENT",
        "ACKNOWLEDGED",
        "REVIEW_COMPLETE",
    ):
        return 0
    # Subtract SUPERSEDED records (which the supervisor moves
    # aside on head advance). The current ledger record
    # survives the SUPERSEDED promotion because the canonical
    # file lives at ``provider__head.json`` while superseded
    # variants live at ``provider__head.superseded.json``.
    superseded_for_provider = sum(
        1 for s in superseded_records
        if s.get("provider") == provider
    )
    return max(0, 1 - superseded_for_provider)


def plan_reviewer_actions(
    *,
    head_sha: str,
    snap: dict,
    policies: Mapping[str, ReviewerPolicy],
    ledger_path: Optional[Path] = None,
    superseded_records: Optional[List[dict]] = None,
    now: Optional[float] = None,
) -> Dict[str, ReviewerTriggerPlan]:
    """Compute the per-provider trigger decision for the
    current head.

    Iterates over ``policies.keys()``; callers can pass any
    subset (typically ``REQUIRED + OPTIONAL`` providers from
    the supervisor's POLICY block).

    Dedup rules:
      - ``max_requests_per_head`` caps per-head triggers
        regardless of budget.
      - ``budget_per_pr`` caps the lifetime triggers per
        provider per PR lifecycle. ``None`` = unlimited.
      - ``request_cooldown_seconds`` rejects immediate
        retries even when budget allows (defensive).

    ``suprseded_records`` is the list returned by
    ``list_review_requests(REVIEW_REQUESTS_DIR / *.superseded.json)``
    so the budget accounting correctly subtracts attempts
    that were correctly moved aside at head advance.
    """
    superseded = list(superseded_records or [])
    plans: Dict[str, ReviewerTriggerPlan] = {}
    for provider, policy in policies.items():
        freshness = check_provider_freshness(
            provider=provider,
            policy=policy,
            snap=snap,
            head_sha=head_sha,
            ledger_path=ledger_path,
            now=now,
        )
        # 1. Fresh -> NOT_NEEDED.
        if freshness.state == FRESHNESS_FRESH:
            plans[provider] = ReviewerTriggerPlan(
                provider=provider,
                action="NOT_NEEDED",
                reason="fresh_exact_head_review",
                freshness=freshness,
            )
            continue
        # 2. BLOCKED_BUDGET -> BLOCK (fail closed).
        if freshness.state == FRESHNESS_BLOCKED_BUDGET:
            plans[provider] = ReviewerTriggerPlan(
                provider=provider,
                action="BLOCK",
                reason="provider_paused_quota",
                freshness=freshness,
            )
            continue
        # 3. Optional + OPTIONAL_STALE -> NOT_NEEDED.
        if freshness.state == FRESHNESS_OPTIONAL_STALE:
            plans[provider] = ReviewerTriggerPlan(
                provider=provider,
                action="NOT_NEEDED",
                reason="optional_provider_stale_acceptable",
                freshness=freshness,
            )
            continue
        # 4. STALE on a required provider -> REQUEST.
        # 5. PENDING on a required provider -> check whether
        #    the existing request is still within budget /
        #    per-head caps.
        ledger = _read_request_ledger(ledger_path, provider, head_sha)
        last_request_at = (
            ledger.get("requested_at") if isinstance(ledger, dict) else None
        )
        last_lifecycle = (
            ledger.get("lifecycle") if isinstance(ledger, dict) else None
        )
        request_id = (
            ledger.get("request_id") if isinstance(ledger, dict) else None
        )
        # Budget accounting: count active non-superseded
        # records on the current head.
        active_count = _count_active_request_records(
            ledger_path=ledger_path,
            provider=provider,
            head_sha=head_sha,
            superseded_records=superseded,
        )
        # Per-head dedup: ``max_requests_per_head`` (default
        # 1) caps how many times the same provider may be
        # triggered for the SAME head.
        if (
            policy.max_requests_per_head is not None
            and active_count >= policy.max_requests_per_head
        ):
            plans[provider] = ReviewerTriggerPlan(
                provider=provider,
                action="BLOCK" if policy.required else "NOT_NEEDED",
                reason="max_requests_per_head_reached",
                last_request_at=last_request_at,
                last_lifecycle=last_lifecycle,
                request_id=request_id,
                freshness=freshness,
            )
            continue
        # Lifetime budget: ``budget_per_pr`` (None = unlimited).
        if (
            policy.budget_per_pr is not None
            and active_count >= policy.budget_per_pr
        ):
            plans[provider] = ReviewerTriggerPlan(
                provider=provider,
                action="BLOCK",
                reason="budget_per_pr_exhausted",
                last_request_at=last_request_at,
                last_lifecycle=last_lifecycle,
                request_id=request_id,
                freshness=freshness,
            )
            continue
        # Cooldown: reject immediate retries when the last
        # request was less than ``request_cooldown_seconds``
        # ago.
        last_ts = _parse_iso8601_utc(last_request_at)
        if (
            last_ts is not None
            and policy.request_cooldown_seconds > 0
            and now is not None
            and (now - last_ts) < policy.request_cooldown_seconds
        ):
            plans[provider] = ReviewerTriggerPlan(
                provider=provider,
                action="WAITING_FOR_AUTO",
                reason="request_cooldown_active",
                last_request_at=last_request_at,
                last_lifecycle=last_lifecycle,
                request_id=request_id,
                freshness=freshness,
            )
            continue
        # 6. The provider may be triggered (auto_trigger)
        #    when its freshness is STALE / PENDING.
        if not policy.auto_trigger:
            plans[provider] = ReviewerTriggerPlan(
                provider=provider,
                action=(
                    "BLOCK" if policy.required else "NOT_NEEDED"
                ),
                reason="policy_disallows_auto_trigger",
                freshness=freshness,
            )
            continue
        # 7. Optional + pending/stale: don't request, but also
        #    don't block readiness.
        if not policy.required:
            plans[provider] = ReviewerTriggerPlan(
                provider=provider,
                action="NOT_NEEDED",
                reason="optional_provider_no_trigger",
                freshness=freshness,
            )
            continue
        # 8. Required + STALE/PENDING + auto_trigger: REQUEST.
        plans[provider] = ReviewerTriggerPlan(
            provider=provider,
            action="REQUEST",
            reason=(
                "freshness_stale"
                if freshness.state == FRESHNESS_STALE
                else "freshness_pending_required"
            ),
            last_request_at=last_request_at,
            last_lifecycle=last_lifecycle,
            request_id=request_id,
            freshness=freshness,
        )
    return plans


# === Convenience: load policies from PROVIDERS map ===

def load_policies_from_providers(
    providers_map: Mapping[str, Mapping[str, Any]],
    *,
    overrides: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, ReviewerPolicy]:
    """Build :class:`ReviewerPolicy` instances from the
    supervisor's existing ``PROVIDERS`` map (round-32) plus the
    per-provider profile overrides.

    ``overrides`` may supply any subset of
    ``DEFAULT_POLICY_PROFILES[provider]`` keys to override the
    conservative defaults. Common keys: ``required``,
    ``auto_trigger``, ``budget_per_pr``,
    ``freshness_grace_seconds``, ``unavailable_behavior``.
    """
    overrides = overrides or {}
    out: Dict[str, ReviewerPolicy] = {}
    for name in providers_map:
        cfg = providers_map.get(name) or {}
        profile: Dict[str, Any] = {
            "name": name,
        }
        # Apply the conservative defaults first.
        default_profile = DEFAULT_POLICY_PROFILES.get(name, {})
        profile.update(default_profile)
        # Apply caller overrides.
        profile.update(overrides.get(name, {}))
        # Inherit trigger_handle from the existing
        # ``PROVIDERS`` map when the profile did not override
        # it.
        if not profile.get("trigger_handle") and cfg.get(
            "trigger_handle"
        ):
            profile["trigger_handle"] = cfg["trigger_handle"]
        # ``required_for_final_merge`` is the existing
        # required flag; honor it when the profile did not
        # override.
        if (
            "required" not in overrides.get(name, {})
            and "required_for_final_merge" in cfg
        ):
            profile["required"] = bool(
                cfg["required_for_final_merge"]
            )
        out[name] = ReviewerPolicy(**profile)
    return out