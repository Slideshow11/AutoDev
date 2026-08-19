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


# === Phase vocabulary (Round-C23R1) ===

#: The first reviewable head on the PR. CodeRabbit's
#: initial-only budget applies; the planner may consume one
#: explicit request for the providers that auto-review
#: ``on_pr_creation``.
PHASE_INITIAL_HEAD = "INITIAL_HEAD"
#: A head reached after a worker pushed a repair commit.
#: CodeRabbit becomes OPTIONAL by default (its quota was
#: already consumed on the initial head); Codex remains
#: REQUIRED because Codex's freshness is the operator's
#: trust signal for code-correctness across repair heads.
PHASE_REPAIR_HEAD = "REPAIR_HEAD"


# === Phase signal ===

# The phase is durable: the controller's state-machine
# journal records ``control_plane.repair_pushed`` events at
# the moment ``report_repair_pushed`` fires
# (``StateMachine._apply`` writes the transition row with
# ``event=control_plane.repair_pushed``). The canonical
# per-run ``state.json`` carries the journal entries. The
# phase resolver counts those entries on the current PR
# lineage:
#
#   - 0 entries -> INITIAL_HEAD (no repair push observed yet
#     for the current PR run)
#   - 1+ entries -> REPAIR_HEAD
#
# The ``old_head_sha`` of the most recent repair push is also
# exposed so callers can correlate the phase signal with the
# relay's FindingLedger supersession evidence (round-C22R2).
#
# NOTE: the canary / test environments can pass a synthetic
# phase signal explicitly (the planner does not require the
# per-run state root to be readable).


def resolve_phase_from_state_root(
    *,
    state_root: Optional["Path"],
    run_id: Optional[str] = None,
) -> Tuple[str, Optional[str], int]:
    """Inspect the per-run ``state.json`` journal and return:

    ``(phase, last_old_head_sha, repair_pushed_count)``

    ``phase`` is one of ``PHASE_INITIAL_HEAD`` /
    ``PHASE_REPAIR_HEAD``. ``last_old_head_sha`` is the
    ``head_observed`` of the most recent
    ``control_plane.repair_pushed`` row (or ``None`` when no
    repair push has been observed). The third element is the
    total number of repair-push entries on the current PR
    lineage — useful for diagnostics.

    The function NEVER raises. When ``state.json`` is missing,
    malformed, or unreachable the resolver returns
    ``(PHASE_INITIAL_HEAD, None, 0)`` (the conservative
    default). Tests that require a non-default phase must
    seed ``state.json`` or pass an explicit phase override.

    A two-arg overload accepts a ``run_id`` for callers that
    prefer to keep the JSON reading path generic; the
    resolver does not actually consume ``run_id`` because
    ``state.json`` is the canonical per-run document.
    """
    if state_root is None:
        return (PHASE_INITIAL_HEAD, None, 0)
    try:
        path = Path(state_root) / "state.json"
        if not path.exists():
            return (PHASE_INITIAL_HEAD, None, 0)
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return (PHASE_INITIAL_HEAD, None, 0)
    if not isinstance(payload, dict):
        return (PHASE_INITIAL_HEAD, None, 0)
    journal = payload.get("journal") or []
    if not isinstance(journal, list):
        return (PHASE_INITIAL_HEAD, None, 0)
    repair_pushed_rows: List[dict] = []
    for entry in journal:
        if not isinstance(entry, dict):
            continue
        if entry.get("event") == "control_plane.repair_pushed":
            repair_pushed_rows.append(entry)
    if not repair_pushed_rows:
        return (PHASE_INITIAL_HEAD, None, 0)
    last_row = repair_pushed_rows[-1]
    last_old = (
        last_row.get("head_observed")
        or last_row.get("head_required")
        or last_row.get("head_required_sha")
        or None
    )
    return (PHASE_REPAIR_HEAD, last_old, len(repair_pushed_rows))


def resolve_phase(
    *,
    state_root: Optional["Path"] = None,
    explicit_phase: Optional[str] = None,
    has_any_superseded_request: bool = False,
) -> str:
    """Round-C23R1 phase resolver.

    Precedence:

    1. ``explicit_phase`` (test fixture override).
    2. ``resolve_phase_from_state_root`` (production path).
    3. Fallback: ``PHASE_REPAIR_HEAD`` when
       ``has_any_superseded_request`` is True (the relay
       has previously promoted a request to SUPERSEDED,
       implying a head advance that is consistent with a
       repair push). Otherwise ``PHASE_INITIAL_HEAD``.
    """
    if isinstance(explicit_phase, str) and explicit_phase in (
        PHASE_INITIAL_HEAD,
        PHASE_REPAIR_HEAD,
    ):
        return explicit_phase
    phase, _, _ = resolve_phase_from_state_root(state_root=state_root)
    if phase == PHASE_REPAIR_HEAD:
        return PHASE_REPAIR_HEAD
    if has_any_superseded_request:
        return PHASE_REPAIR_HEAD
    return PHASE_INITIAL_HEAD


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
        # Round-C23R1: Codex remains REQUIRED on repair heads
        # (operator's freshness trust signal). Budget is
        # unlimited per PR; per-head dedup is the only cap.
        "phase_required": {
            PHASE_INITIAL_HEAD: True,
            PHASE_REPAIR_HEAD: True,
        },
        "phase_budget_per_pr": {
            PHASE_INITIAL_HEAD: None,
            PHASE_REPAIR_HEAD: None,
        },
        "phase_max_requests_per_head": {
            PHASE_INITIAL_HEAD: 1,
            PHASE_REPAIR_HEAD: 1,
        },
        "phase_auto_trigger": {
            PHASE_INITIAL_HEAD: True,
            PHASE_REPAIR_HEAD: True,
        },
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
        # Round-C23R1: Sourcery is optional on both phases
        # and the supervisor never auto-triggers it.
        "phase_required": {
            PHASE_INITIAL_HEAD: False,
            PHASE_REPAIR_HEAD: False,
        },
        "phase_budget_per_pr": {
            PHASE_INITIAL_HEAD: None,
            PHASE_REPAIR_HEAD: None,
        },
        "phase_max_requests_per_head": {
            PHASE_INITIAL_HEAD: 0,
            PHASE_REPAIR_HEAD: 0,
        },
        "phase_auto_trigger": {
            PHASE_INITIAL_HEAD: False,
            PHASE_REPAIR_HEAD: False,
        },
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
        # Round-C23R1: CodeRabbit is REQUIRED on the initial
        # head (the first review it can do) but OPTIONAL on
        # repair heads. The operator's quota is consumed by
        # the initial review; subsequent repair heads
        # MUST NOT issue another explicit CodeRabbit
        # request unless a deployment-specific override
        # re-marks ``phase_required[REPAIR_HEAD] = True``.
        # Stale CodeRabbit evidence on a repair head is
        # therefore OPTIONAL_STALE / NOT_NEEDED — the
        # readiness gate does NOT block.
        "phase_required": {
            PHASE_INITIAL_HEAD: True,
            PHASE_REPAIR_HEAD: False,
        },
        "phase_budget_per_pr": {
            PHASE_INITIAL_HEAD: 1,
            PHASE_REPAIR_HEAD: 0,
        },
        "phase_max_requests_per_head": {
            PHASE_INITIAL_HEAD: 1,
            PHASE_REPAIR_HEAD: 0,
        },
        "phase_auto_trigger": {
            PHASE_INITIAL_HEAD: True,
            PHASE_REPAIR_HEAD: False,
        },
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

    Round-C23R1 phase fields (the policy can vary per PR
    lifecycle phase — initial head vs repair head):

    - ``phase_required``: maps phase to whether the
      provider's fresh review is required for qualification.
      CodeRabbit defaults to ``{INITIAL: True, REPAIR:
      False}``; ``required`` is the legacy alias (the
      INITIAL_HEAD value) so existing test fixtures keep
      working.
    - ``phase_budget_per_pr``: per-phase lifetime budget.
      ``None`` = unlimited; ``0`` = explicitly disabled
      (CodeRabbit on repair heads).
    - ``phase_max_requests_per_head``: per-phase per-head
      dedup cap. ``0`` = no requests on that phase
      regardless of evidence freshness.
    - ``phase_auto_trigger``: per-phase auto-trigger
      switch. CodeRabbit defaults to ``{INITIAL: True,
      REPAIR: False}`` so the planner does not burn
      CodeRabbit quota on every repair push.
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
    # Round-C23R1 phase overrides. When empty dict, the
    # default behavior applies (``required`` /
    # ``auto_trigger`` / ``budget_per_pr`` /
    # ``max_requests_per_head`` are used for both phases).
    # When populated, the planner resolves the value for
    # the current phase via ``policy.value_for_phase``.
    phase_required: Mapping[str, bool] = field(default_factory=dict)
    phase_budget_per_pr: Mapping[str, Optional[int]] = field(
        default_factory=dict
    )
    phase_max_requests_per_head: Mapping[str, int] = field(
        default_factory=dict
    )
    phase_auto_trigger: Mapping[str, bool] = field(
        default_factory=dict
    )

    def value_for_phase(
        self, attr: str, phase: str,
    ) -> Any:
        """Resolve a phase-aware attribute. When the policy
        carries a per-phase override the phase's value wins;
        otherwise the legacy scalar (``required`` /
        ``auto_trigger`` / ``budget_per_pr`` /
        ``max_requests_per_head``) is used. ``attr`` MUST be
        one of: ``required``, ``auto_trigger``,
        ``budget_per_pr``, ``max_requests_per_head``."""
        override_map = {
            "required": self.phase_required,
            "auto_trigger": self.phase_auto_trigger,
            "budget_per_pr": self.phase_budget_per_pr,
            "max_requests_per_head":
                self.phase_max_requests_per_head,
        }
        m = override_map.get(attr) or {}
        if isinstance(m, Mapping) and phase in m:
            return m[phase]
        return getattr(self, attr)


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
    """Find the provider's authoritative review record for
    the current exact head.

    Round-C23R1 fix: the previous implementation iterated
    ``snap["formal_reviews"]`` in snapshot order and
    returned immediately on the first matching provider
    entry. That ordering is NOT a guarantee — GitHub may
    return reviews in ``submitted_at`` order, ``id`` order,
    or arbitrary order depending on pagination. The
    authoritative algorithm MUST walk ALL formal reviews
    for the provider and select the best one by priority:

    1. ANY formal review with ``commit_id == head_sha``
       wins (exact-head anchor). Among multiple exact-head
       matches, the most-recent by ``submitted_at`` wins.
    2. ANY ``provider_surfaces[provider].heads[head_sha]``
       binding is ALSO exact-head authoritative evidence.
       When present alongside a stale formal review, the
       provider_surface wins because the supervisor
       persisted the binding at snapshot time and it
       represents the live evidence. Both #1 and #2 are
       checked before any stale record is returned.
    3. Otherwise the most-recent STALE record (commit_id
       differs from head_sha), selected by ``submitted_at``
       in descending ISO-8601 order.
    4. Otherwise the most-recent UNBOUND record (no
       ``commit_id``; GitHub did not expose a binding
       anchor — typically a pre-round-27 review). Wall-clock
       ordering selects the latest.
    5. Fallback to issue comments (walkthrough-completed
       markers) — only consulted when the formal-review
       branch yields nothing. Issue comments do not expose
       ``commit_id`` so they cannot win priority #1/#2.

    Returns ``None`` when none of the above yields a record.

    The function never raises. Malformed entries are
    silently skipped so a single corrupt review record does
    not break the planner.
    """
    if not isinstance(snap, dict):
        return None
    provider_reviews: List[dict] = []
    for r in snap.get("formal_reviews") or []:
        if not isinstance(r, dict):
            continue
        if r.get("provider") != provider:
            continue
        provider_reviews.append(r)
    # Priority #1: any exact-head match. Among the matches,
    # pick the most recent by ``submitted_at`` so the
    # audit's "freshness must beat ordering" rule wins.
    exact_head_matches = []
    for r in provider_reviews:
        cid = r.get("commit_id")
        if cid is None and r.get("head_sha"):
            cid = r["head_sha"]
        if cid == head_sha:
            exact_head_matches.append(r)
    if exact_head_matches:
        freshest = _most_recent_review(exact_head_matches)
        cid = freshest.get("commit_id")
        if cid is None and freshest.get("head_sha"):
            cid = freshest["head_sha"]
        return {
            "commit_id": cid,
            "submitted_at": freshest.get("submitted_at"),
            "review_id": (
                freshest.get("review_id")
                or freshest.get("id")
            ),
            "kind": "formal_review",
        }
    # Priority #2: provider-surface exact-head binding.
    # The supervisor persisted this at snapshot time; it is
    # authoritative for the current head even when a stale
    # formal review exists alongside.
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
    # Priority #3: most recent STALE record (commit_id
    # differs from head_sha).
    stale_records = []
    for r in provider_reviews:
        cid = r.get("commit_id")
        if cid is None and r.get("head_sha"):
            cid = r["head_sha"]
        if cid and cid != head_sha:
            stale_records.append(r)
    if stale_records:
        freshest = _most_recent_review(stale_records)
        cid = freshest.get("commit_id")
        if cid is None and freshest.get("head_sha"):
            cid = freshest["head_sha"]
        return {
            "commit_id": cid,
            "submitted_at": freshest.get("submitted_at"),
            "review_id": (
                freshest.get("review_id")
                or freshest.get("id")
            ),
            "kind": "formal_review_stale_commit",
        }
    # Priority #4: most recent UNBOUND record (no commit_id,
    # no head_sha binding).
    unbound_records = []
    for r in provider_reviews:
        cid = r.get("commit_id")
        if cid is None and r.get("head_sha"):
            cid = r["head_sha"]
        if cid is None:
            unbound_records.append(r)
    if unbound_records:
        freshest = _most_recent_review(unbound_records)
        return {
            "commit_id": None,
            "submitted_at": freshest.get("submitted_at"),
            "review_id": (
                freshest.get("review_id")
                or freshest.get("id")
            ),
            "kind": "formal_review_unbound",
        }
    # Priority #5: provider issue comments (walkthrough /
    # review-complete markers). The audit prefers the
    # most-recent walkthrough by ``created_at`` since the
    # issue-comment connection does not expose a
    # ``commit_id``. Issue comments are weaker evidence
    # than formal reviews, so they only win when no formal
    # review exists for the provider on the current head.
    issue_comments = (
        snap.get("_provider_issue_comments", {}).get(provider, []) or []
    )
    if issue_comments:
        freshest_comment = _most_recent_comment(issue_comments)
        if freshest_comment is not None:
            return {
                "commit_id": None,  # issue comments don't
                # expose commit_id; bound by wall-clock only
                "submitted_at": freshest_comment.get("created_at"),
                "review_id": freshest_comment.get("id"),
                "kind": "issue_comment",
            }
    return None


def _most_recent_review(reviews: List[dict]) -> dict:
    """Return the most recent formal review by ``submitted_at``.
    The helper accepts a non-empty list and never raises on
    malformed timestamps: entries without ``submitted_at``
    sort to the bottom; the first entry is returned when the
    list is empty (the caller filters)."""
    if not reviews:
        # Defensive: the planner only calls this when the
        # list is non-empty, but a guard prevents the
        # ``max`` reduction from raising on an empty list.
        return {}
    decorated: List[Tuple[float, int, dict]] = []
    for i, r in enumerate(reviews):
        if not isinstance(r, dict):
            continue
        ts = _parse_iso8601_utc(r.get("submitted_at"))
        if ts is None:
            ts = 0.0
        decorated.append((ts, i, r))
    if not decorated:
        return reviews[0]
    # Sort by timestamp DESC then by index DESC (stable
    # order for entries with identical timestamps). The
    # most recent is at index 0.
    decorated.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return decorated[0][2]


def _most_recent_comment(comments: List[dict]) -> Optional[dict]:
    """Return the most recent issue comment by ``created_at``.
    Mirrors ``_most_recent_review`` for the
    ``_provider_issue_comments`` payload."""
    if not comments:
        return None
    decorated: List[Tuple[float, int, dict]] = []
    for i, c in enumerate(comments):
        if not isinstance(c, dict):
            continue
        ts = _parse_iso8601_utc(c.get("created_at"))
        if ts is None:
            ts = 0.0
        decorated.append((ts, i, c))
    if not decorated:
        return comments[0]
    decorated.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return decorated[0][2]


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
) -> Tuple[int, int]:
    """Count the active request records for ``provider`` and
    split them by ``head_sha`` match.

    Round-C23R1 fix: the previous implementation computed a
    binary ``max(0, 1 - superseded_for_provider)`` which
    conflated per-head cap accounting with PR-lifetime
    budget accounting. The audit requires semantically
    correct accounting:

    - ``active_on_current_head``: number of
      REQUEST_INTENT / REQUEST_SENT / ACKNOWLEDGED /
      REVIEW_COMPLETE records whose ``head_sha`` (stored in
      the canonical ``{provider}__{head}.json`` filename)
      equals the current head. Subtracted by the
      corresponding SUPERSEDED count for the SAME
      (provider, head_sha) pair (defensive: if the
      supervisor moved the active record aside on head
      advance, the per-head count drops to 0; otherwise
      ``max(0, count - superseded_count_for_pair)``).
    - ``active_in_pr_lifecycle``: number of active
      records across ALL heads for ``provider``. This is
      what ``budget_per_pr`` caps. The supervisor moves a
      record to ``*.superseded.json`` on head advance, but
      the canonical ``{provider}__{head}.json`` for the
      PRIOR head remains on disk (no deletion); the helper
      counts only the active lifecycle (not superseded).

    Returns ``(active_on_current_head, active_in_pr_lifecycle)``.

    ``superseded_records`` is the canonical list returned
    by ``list_superseded_records(ledger_path)``. It is used
    only for the per-head subtraction; the PR-lifetime
    counter counts canonical ``{provider}__{head}.json``
    files that have a non-SUPERSEDED lifecycle.
    """
    if ledger_path is None:
        return (0, 0)
    # Group superseded records by (provider, head_sha) for
    # the per-head subtraction.
    superseded_pairs: Dict[Tuple[str, str], int] = {}
    for s in superseded_records:
        if not isinstance(s, dict):
            continue
        s_provider = s.get("provider")
        s_head = (
            s.get("superseded_head")
            or s.get("head_sha")
            or ""
        )
        if not isinstance(s_provider, str) or not s_head:
            continue
        superseded_pairs[(s_provider, s_head)] = (
            superseded_pairs.get((s_provider, s_head), 0) + 1
        )
    on_current = 0
    in_pr = 0
    if not ledger_path.exists():
        return (on_current, in_pr)
    _LIFECYCLES = (
        "REQUEST_INTENT",
        "REQUEST_SENT",
        "ACKNOWLEDGED",
        "REVIEW_COMPLETE",
    )
    # Active records: canonical ``{provider}__{head}.json``
    # with a non-SUPERSEDED lifecycle.
    for p in ledger_path.glob(f"{provider}__*.json"):
        # Skip superseded variants.
        if p.name.endswith(".superseded.json"):
            continue
        try:
            payload = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("lifecycle") not in _LIFECYCLES:
            continue
        # The head_sha is encoded in the filename.
        # {provider}__{head_sha}.json -> head_sha is the
        # third component.
        stem = p.stem  # {provider}__{head_sha}
        parts = stem.split("__", 1)
        if len(parts) != 2:
            continue
        h = parts[1]
        in_pr += 1
        if h == head_sha:
            superseded_for_pair = superseded_pairs.get(
                (provider, h), 0
            )
            on_current = max(0, on_current + 1 - superseded_for_pair)
    return (on_current, in_pr)


def plan_reviewer_actions(
    *,
    head_sha: str,
    snap: dict,
    policies: Mapping[str, ReviewerPolicy],
    ledger_path: Optional[Path] = None,
    superseded_records: Optional[List[dict]] = None,
    now: Optional[float] = None,
    phase: str = PHASE_INITIAL_HEAD,
) -> Dict[str, ReviewerTriggerPlan]:
    """Compute the per-provider trigger decision for the
    current head.

    Round-C23R1: the planner resolves the phase via the
    ``phase`` parameter (the caller is responsible for
    deriving it; production callers pass the value
    returned by ``resolve_phase``). Per-phase overrides
    (``policy.phase_required`` / ``phase_auto_trigger`` /
    ``phase_budget_per_pr`` /
    ``phase_max_requests_per_head``) win over the legacy
    scalar fields when populated.

    Iterates over ``policies.keys()``; callers can pass any
    subset (typically ``REQUIRED + OPTIONAL`` providers from
    the supervisor's POLICY block).

    Dedup rules (per-phase):

      - ``phase_max_requests_per_head[phase]`` (or legacy
        ``max_requests_per_head``) caps per-head triggers
        regardless of budget.
      - ``phase_budget_per_pr[phase]`` (or legacy
        ``budget_per_pr``) caps lifetime triggers per
        provider per PR lifecycle. ``None`` = unlimited;
        ``0`` = explicitly disabled on this phase (CodeRabbit
        on repair heads).
      - ``request_cooldown_seconds`` rejects immediate
        retries even when budget allows (defensive).

    ``superseded_records`` is the list returned by
    ``list_superseded_records(ledger_path)`` so the budget
    accounting correctly subtracts attempts that were
    correctly moved aside at head advance. The new
    accounting helper ``_count_active_request_records``
    returns ``(on_current_head, in_pr_lifecycle)``; the
    per-head subtraction only counts SUPERSEDED entries
    for the SAME ``(provider, head_sha)`` pair (round-C23R1
    Defect B fix).
    """
    superseded = list(superseded_records or [])
    plans: Dict[str, ReviewerTriggerPlan] = {}
    for provider, policy in policies.items():
        # Round-C23R1: phase-aware resolution of the four
        # dimension fields. When ``policy.phase_required``
        # (or the analogous field) is empty the legacy
        # scalar wins; otherwise the phase's value wins.
        required = bool(
            policy.value_for_phase("required", phase)
        )
        auto_trigger = bool(
            policy.value_for_phase("auto_trigger", phase)
        )
        # ``budget_per_pr`` may be ``None`` (unlimited) or
        # ``int``. The phase override preserves the same
        # semantics.
        budget_per_pr = policy.value_for_phase(
            "budget_per_pr", phase
        )
        if not isinstance(budget_per_pr, int) and (
            budget_per_pr is not None
        ):
            budget_per_pr = None
        max_per_head = int(
            policy.value_for_phase("max_requests_per_head", phase)
        )
        freshness = check_provider_freshness(
            provider=provider,
            policy=policy,
            snap=snap,
            head_sha=head_sha,
            ledger_path=ledger_path,
            now=now,
        )
        # 1. Fresh -> NOT_NEEDED. The freshness contract is
        # exact-head-only; an exact-head review (priority
        # #1 of the new review-selection algorithm) wins.
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
        # Round-C23R1: semantically correct request
        # accounting. Returns
        # ``(active_on_current_head, active_in_pr_lifecycle)``.
        on_current_head, in_pr_lifecycle = _count_active_request_records(
            ledger_path=ledger_path,
            provider=provider,
            head_sha=head_sha,
            superseded_records=superseded,
        )
        # Per-phase budget = ``0`` means "explicitly disabled
        # on this phase" (e.g. CodeRabbit on repair heads).
        # The planner emits NOT_NEEDED without a request —
        # the readiness gate does NOT block on this
        # provider. Audit's Defect B fix.
        if budget_per_pr == 0:
            if not required:
                plans[provider] = ReviewerTriggerPlan(
                    provider=provider,
                    action="NOT_NEEDED",
                    reason="phase_disabled_optional_provider",
                    freshness=freshness,
                )
                continue
            # ``required=True`` with ``budget_per_pr=0`` is
            # contradictory; the audit requires BLOCK so
            # the operator is notified.
            plans[provider] = ReviewerTriggerPlan(
                provider=provider,
                action="BLOCK",
                reason="phase_required_with_zero_budget",
                freshness=freshness,
            )
            continue
        if max_per_head == 0:
            # ``max_per_head=0`` means "no requests on this
            # phase". Optional providers emit NOT_NEEDED;
            # required providers with no fresh evidence
            # emit BLOCK (the operator MUST re-authorize).
            if not required:
                plans[provider] = ReviewerTriggerPlan(
                    provider=provider,
                    action="NOT_NEEDED",
                    reason="phase_per_head_disabled_optional_provider",
                    freshness=freshness,
                )
                continue
            plans[provider] = ReviewerTriggerPlan(
                provider=provider,
                action="BLOCK",
                reason="phase_per_head_disabled_required_provider",
                freshness=freshness,
            )
            continue
        # Per-head dedup: ``max_per_head`` caps how many
        # times the same provider may be triggered for the
        # SAME head.
        if (
            max_per_head is not None
            and on_current_head >= max_per_head
        ):
            plans[provider] = ReviewerTriggerPlan(
                provider=provider,
                action="BLOCK" if required else "NOT_NEEDED",
                reason="max_requests_per_head_reached",
                last_request_at=last_request_at,
                last_lifecycle=last_lifecycle,
                request_id=request_id,
                freshness=freshness,
            )
            continue
        # Lifetime budget: ``budget_per_pr`` (None = unlimited).
        # ``0`` is handled above as phase-disabled.
        if (
            budget_per_pr is not None
            and in_pr_lifecycle >= budget_per_pr
        ):
            plans[provider] = ReviewerTriggerPlan(
                provider=provider,
                action="BLOCK" if required else "NOT_NEEDED",
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
        if not auto_trigger:
            plans[provider] = ReviewerTriggerPlan(
                provider=provider,
                action=(
                    "BLOCK" if required else "NOT_NEEDED"
                ),
                reason="policy_disallows_auto_trigger",
                freshness=freshness,
            )
            continue
        # 7. Optional + pending/stale: don't request, but also
        #    don't block readiness.
        if not required:
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