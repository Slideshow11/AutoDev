"""Autonomous review/repair relay v1.

Goal
----
Eliminate the human copy/paste review-repair loop. The relay drives a
single PR through one bounded sequence:

    1. Collect EXACT-HEAD CI and CodeRabbit evidence.
    2. Identify and classify actionable findings.
    3. Produce a structured repair directive for Humphry.
    4. Let Humphry repair, test, commit, and push.
    5. Wait for EXACT-HEAD CI and a fresh review on the new head.
    6. Ingest any new findings.
    7. Repeat for as many rounds as necessary.
    8. Automatically enter qualification once the head is clean.
    9. Stop at the existing human exact-head merge-authorization
       boundary (the relay does NOT merge).

Design contract
---------------
The relay REUSES existing mechanisms. It does not duplicate any of
them:

- It reads the supervisor's snapshot shape
  (``autocoder_supervisor.supervisor.capture_live_snapshot``), but
  does not import or invoke it directly. The collector accepts the
  snapshot dict from any caller and is therefore testable in
  isolation without spinning up the supervisor.

- It persists the repair directive and round journal through the
  orchestration ``StateStore`` (existing C-11/C-12/C-16) — mode 0600,
  private dir, atomic writes, revision-bumped.

- It drives the orchestration state machine
  (``autocoder_orchestration.state_machine``) through its existing
  transitions:

      REPAIRING_REVIEW_FINDINGS -> AWAITING_CI -> QUALIFYING_READINESS

  No new states are introduced. The relay never places the run into
  ``AWAITING_MERGE_AUTHORIZATION`` directly — the existing readiness
  gate (``autocoder_orchestration.readiness.ReadinessEngine``) is the
  sole authority for that transition.

- It REUSES the existing worker-lease contract
  (``autocoder_orchestration.store.Lease``) — exactly one writer per
  state root, lifecycle bound to PID + start_id.

- It does NOT modify the merge path (``merge_authorization.py``), it
  does NOT weaken any qualification gate, and it does NOT call
  ``gh pr merge`` or any other irreversible GitHub action.

Failure surface
---------------
- The relay halts cleanly on ``EscalateToHuman`` (raised by the
  classifier or the directive builder when a finding is too
  ambiguous for an autonomous directive).
- All round transitions are written to the state journal so a
  restart can resume from the last round.
- The relay refuses to take any action that would alter the
  authoritative head SHA except by waiting for the worker to push
  and updating the run context with the new head (C-15 — the run
  context is the trust root, and the controller's transition
  contract is the only place a new head is bound).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .state_machine import (
    STATE_QUALIFYING_READINESS,
    STATE_AWAITING_CI,
    STATE_REPAIRING_REVIEW_FINDINGS,
)
from .context import (
    ACTOR_CONTROLLER,
    ACTOR_IMPL_WORKER,
)
from .store import (
    StateStore,
    StateCorruption,
    ProcessIdentity,
    current_process_identity,
)
from .artifacts import write_artifact, read_artifact, ArtifactError

# === Error hierarchy ===

class RelayError(Exception):
    """Base class for relay-specific failures."""


class RecoverableRetry(RelayError):
    """Round-30: a recoverable retry signal.

    Distinct from ``EscalateToHuman`` (protected authority).
    A ``RecoverableRetry`` indicates the relay MUST end the
    current execution slice without operator handoff; the
    persistent supervisor / scheduler resumes the SAME
    outstanding work on the next slice. The supervisor's
    recovery path catches ``RecoverableRetry`` (NOT
    ``EscalateToHuman``) and continues polling.
    """


class EscalateToHuman(RelayError):
    """Raised when the relay cannot proceed autonomously.

    The relay NEVER silently escalates; a human-facing message is
    included so the operator can decide what to do next. The run
    transitions to ``BLOCKED`` via the controller so the operator
    has a single, observable halt point.
    """


class InvalidSnapshot(RelayError):
    """The supplied snapshot is missing fields the relay requires."""


class DirectiveContractError(RelayError):
    """The directive failed its own contract check before
    persistence. Raised by ``ReviewDirective.to_dict`` /
    ``from_dict`` chain."""

# === Constants ===

#: Maximum rounds before the relay escalates to a human. This is the
#: outer bound on the autonomous sequence. The inner bound on each
#: round is the existing supervisor lease timeout.
DEFAULT_MAX_ROUNDS = 10

#: Schema version for the relay's persisted artifacts. Bump
#: whenever the durable shape changes.
RELAY_SCHEMA_VERSION = "autocoder.review_repair_relay.v1"

#: Schema version for the finding ledger. The ledger is a per-head
#: record of which provider findings have been observed and on
#: which head. The collector skips findings whose last observed
#: signature matches the live signature at the current head —
#: i.e. a comment that has not changed since the last round on
#: this head is considered "addressed" and is not re-emitted into
#: a new directive. The ledger is the durable proof of the
#: invariant the user spelled out: "finding on A -> repaired in B
#: -> old A finding does not re-enter B directive unless fresh
#: evidence explicitly reopens it".
FINDING_LEDGER_SCHEMA_VERSION = "autocoder.finding_ledger.v2"

# === Finding lifecycle (round-27) ===
# A finding's lifecycle is OBSERVED -> DISPATCHED -> ACTIVE -> {SUPERSEDED, REPAIRED}.
# Round-26 conflated "DISPATCHED" with "addressed": any finding placed in a
# directive was marked as consumed, so a failed/no-op worker launch (which
# NEVER addresses the finding) caused the next round to skip it. Round-27
# explicitly separates the states:
#
#   OBSERVED     - finding collected from a live snapshot but no directive yet
#   DISPATCHED   - finding placed in a directive; worker has not yet responded
#   ACTIVE       - finding emitted on the SAME head and NOT yet addressed
#   SUPERSEDED   - head advanced to a new SHA (the relay saw B != A)
#   REPAIRED     - a fresh evaluation proves the finding no longer applies
#                  (e.g. comment was resolved upstream, code now satisfies
#                  the requirement)
#
# ``is_fresh`` returns True when the finding should be emitted. The contract:
#
#   - No ledger entry for (finding_id, current_head) -> fresh (first sighting)
#   - Entry's state in {ACTIVE} for current_head -> fresh (still unresolved)
#   - Entry's state in {SUPERSEDED, REPAIRED} on current_head -> NOT fresh
#   - Entry's head_sha differs from current_head -> NOT fresh (the head
#     advanced; the relay must call ``mark_superseded_by_head`` explicitly,
#     so a state of OBSERVED/DISPATCHED on a stale head is treated as
#     NOT-fresh ONLY when the caller has advanced the head)
#
# A failed worker launch, worker crash, worker timeout, no-op worker, or
# worker that exits without pushing MUST leave a same-head finding in
# ACTIVE so the next round re-emits it. The relay must NOT promote
# DISPATCHED -> REPAIRED on its own; only positive evidence (head advance,
# explicit resolution, semantic re-evaluation) transitions.
FINDING_STATE_OBSERVED = "OBSERVED"
FINDING_STATE_DISPATCHED = "DISPATCHED"
FINDING_STATE_ACTIVE = "ACTIVE"
FINDING_STATE_SUPERSEDED = "SUPERSEDED"
FINDING_STATE_REPAIRED = "REPAIRED"
ALL_FINDING_STATES = frozenset({
    FINDING_STATE_OBSERVED,
    FINDING_STATE_DISPATCHED,
    FINDING_STATE_ACTIVE,
    FINDING_STATE_SUPERSEDED,
    FINDING_STATE_REPAIRED,
})

#: Severity ranking. P1 must be addressed in the current round; P2
#: is preferred but not blocking. The classifier treats any
#: finding whose body contains a SECRETS / DESTRUCTIVE keyword as
#: ``P0_ESCALATE`` and refuses to build a directive.
SEVERITY_P0_ESCALATE = "P0_ESCALATE"
SEVERITY_P1 = "P1"
SEVERITY_P2 = "P2"
SEVERITY_CI_FAILURE = "CI_FAILURE"

ALL_SEVERITIES = (
    SEVERITY_P0_ESCALATE,
    SEVERITY_P1,
    SEVERITY_P2,
    SEVERITY_CI_FAILURE,
)

#: Words that must NOT appear in a directive body. If a finding's
#: body contains any of these, the relay raises EscalateToHuman
#: rather than producing a directive. This is the lexical guard
#: that prevents the autonomous path from issuing a destructive
#: action that an experienced human reviewer would never sign off.
_ESCALATION_KEYWORDS = frozenset({
    "force push",
    "rewrite history",
    "delete branch",
    "disable tests",
    "skip ci",
    "merge pr",
    "close pr",
    "bypass guard",
    "ignore gate",
})

#: Regex that detects P1/P2 markers in CodeRabbit / Codex bodies.
_SEVERITY_MARKER_RE = re.compile(
    r"\b(?P<sev>P0|P1|P2|priority-high|priority-medium|priority-low)\b",
    re.IGNORECASE,
)

#: Regex that captures ``path:line`` anchors used in review
#: comments. Falls back to just ``path`` if no line is present.
#: The path component MUST end in a known source-file extension;
#: arbitrary dotted prose (e.g. "i.e", "v1.2", "foo.bar()") is
#: rejected so it cannot reach the worker as a fake anchor.
_KNOWN_SOURCE_EXTENSIONS = (
    "py", "pyi", "pyx", "pxd",
    "js", "jsx", "ts", "tsx", "mjs", "cjs",
    "rs", "go", "c", "cc", "cpp", "cxx", "h", "hpp",
    "java", "kt", "scala",
    "rb", "sh", "bash", "zsh",
    "toml", "yaml", "yml", "json", "xml", "html", "css",
    "md", "rst", "txt",
    "sql", "lua", "pl", "php",
    "tf", "hcl",
)
_PATH_ANCHOR_RE = re.compile(
    # Optional path components separated by / then a basename
    # ending in a known source-file extension. The basename
    # may include dots (e.g. ``foo.bar.py``) so we capture the
    # extension as the LAST dot-group, not the first.
    r"(?P<path>(?:[A-Za-z0-9_.\-]+/)*"
    r"(?P<basename>[A-Za-z0-9_.\-]+?)\."
    r"(?P<ext>" + "|".join(_KNOWN_SOURCE_EXTENSIONS) + r"))"
    r"(?::(?P<line>\d+))?",
)

#: Strict lowercase hex SHA-256/1 pattern used by the directive
#: contract (40 or 64 lowercase hexadecimal characters).
_HEX_SHA_RE = re.compile(r"\A[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")

#: Regex that captures a suggested test from the test-gap markers.
_TEST_SUGGEST_RE = re.compile(
    r"(?P<test>test_[A-Za-z0-9_]+|Test[A-Za-z0-9_]+|[A-Za-z0-9_]+Test)\b",
)


# === Finding signature ===

def _finding_signature(finding: "Finding") -> str:
    """Return a stable signature for the finding's content.

    Two findings with the same ``finding_id`` are considered the
    SAME piece of evidence iff their signatures match. A body
    edit (CodeRabbit re-review, new suggestion text) produces a
    different signature and re-emits the finding; a pure
    timestamp change produces the same signature and is skipped.

    The signature deliberately ignores transient metadata that
    GitHub mutates between identical comments (e.g.
    ``updated_at`` timestamps) and focuses on the parts of the
    content that represent the actual review finding.
    """
    if not isinstance(finding, Finding):
        raise DirectiveContractError(
            f"finding_signature requires a Finding, got {type(finding).__name__}"
        )
    body_norm = (finding.body or "").strip()
    title_norm = (finding.title or "").strip()
    parts = (
        finding.finding_id,
        finding.source,
        finding.severity,
        title_norm,
        body_norm,
        finding.file_path or "",
        str(finding.line) if finding.line is not None else "",
        finding.check_name or "",
    )
    raw = "\u0001".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# === Finding ledger ===

class FindingLedger:
    """Durable per-head record of which provider findings have been
    observed, dispatched, and resolved.

    Round-27 lifecycle (vs round-26's brittle ``is_fresh`` filter):

      OBSERVED    -> DISPATCHED -> ACTIVE -> SUPERSEDED  (head advance)
                                   \\-> REPAIRED    (semantic re-eval)

    ``is_fresh(finding)`` returns True iff the finding MUST be emitted
    into the next directive. The rule:

      - No prior entry on the current head -> fresh (first sighting)
      - Prior entry state == ACTIVE on the current head -> fresh
      - Prior entry state == SUPERSEDED on the current head -> NOT fresh
      - Prior entry state == REPAIRED on the current head -> NOT fresh
      - Prior entry state in {OBSERVED, DISPATCHED} on the current head
        -> fresh (the worker has not yet responded; the round
        must re-emit so the failure is visible)
      - Prior entry is on a DIFFERENT head -> fresh (the head advanced;
        the relay must first call ``mark_superseded_by_head`` to move
        stale entries out of ACTIVE before this gate sees them)

    A failed worker launch, worker crash, worker timeout, no-op
    worker, or worker that exits without pushing MUST leave the
    same-head finding ACTIVE so the next round re-emits it. The
    round-26 ledger marked DISPATCHED as consumed and skipped the
    next round; round-27 explicitly forbids that.

    The ledger is persisted as JSONL under the state root so the
    record survives supervisor restarts.
    """

    def __init__(
        self,
        store: "StateStore",
        *,
        head_sha: str,
    ) -> None:
        if not isinstance(head_sha, str) or not _HEX_SHA_RE.match(head_sha):
            raise DirectiveContractError(
                f"FindingLedger requires a 40/64-char lowercase hex head_sha, "
                f"got {head_sha!r}"
            )
        self.store = store
        self.head_sha = head_sha

    def _rel_path(self) -> str:
        return "finding_ledger.jsonl"

    def load(self) -> dict:
        """Return ``{finding_id: {state, signature, head_sha, ...}}`` for
        the last observed entry per finding id.

        Missing entries yield an empty dict. Malformed lines are
        skipped silently — the ledger is an audit aid, not a
        source of truth for the durable state machine.
        """
        out: dict = {}
        try:
            for entry in self.store.read_journal(self._rel_path()):
                if not isinstance(entry, dict):
                    continue
                fid = entry.get("finding_id")
                if not isinstance(fid, str):
                    continue
                # Validate the entry shape; skip invalid rows.
                state = entry.get("state")
                if state not in ALL_FINDING_STATES:
                    continue
                # The last write wins per finding_id.
                out[fid] = entry
        except (OSError, KeyError, AttributeError, StateCorruption):
            return {}
        return out

    def _validate_finding(self, finding: Any) -> None:
        if not isinstance(finding, Finding):
            raise DirectiveContractError(
                f"finding must be a Finding, got {type(finding).__name__}"
            )

    def _signature(self, finding: "Finding") -> str:
        return _finding_signature(finding)

    def _append(self, finding: "Finding", state: str) -> None:
        sig = self._signature(finding)
        entry = {
            "schema_version": FINDING_LEDGER_SCHEMA_VERSION,
            "finding_id": finding.finding_id,
            "source": finding.source,
            "severity": finding.severity,
            "head_sha": self.head_sha,
            "signature": sig,
            "state": state,
            "recorded_at": _now_iso(),
        }
        try:
            self.store.append_journal(self._rel_path(), entry)
        except (OSError, AttributeError):
            # The ledger is advisory; never let a write failure
            # block a round.
            pass

    def is_fresh(self, finding: Any) -> bool:
        """Return True iff the finding should be emitted into the
        next directive.

        The rule (round-28 P5):

          - No prior entry on any head with the same signature
            -> fresh (first sighting).
          - Prior entry state in {ACTIVE, DISPATCHED,
            OBSERVED} on the SAME head -> fresh (the worker
            has not yet responded; the next round must re-emit
            so the failure is visible).
          - Prior entry state in {SUPERSEDED, REPAIRED} on the
            SAME head -> NOT fresh (terminal state on the
            current head).
          - Prior entry state in {SUPERSEDED, REPAIRED} on a
            DIFFERENT head -> fresh UNLESS there is positive
            cross-head resolution evidence (see below).
          - Positive cross-head resolution evidence suppresses
            a finding across heads. Examples:
              - the resolved current thread is closed;
              - the current reviewer no longer reports it after
                a complete fresh review;
              - the exact-head CI failure is cleared;
              - an explicit semantic resolution marker.
            Round-28 P5: the absence of positive cross-head
            resolution evidence MUST NOT silence the finding.
            A SUPERSEDED/REPAIRED entry on a prior head is
            NOT itself positive cross-head evidence.

        ``state_of`` returns the canonical entry for the
        finding on the current head. The cross-head check
        only fires when there is no entry on the current head.
        """
        self._validate_finding(finding)
        sig = self._signature(finding)
        prior = self.state_of(finding.finding_id)
        if prior is not None:
            prior_sig = prior.get("signature")
            if prior_sig != sig:
                # The body/title/severity changed at the same head.
                # Re-emit as a fresh observation; the prior entry
                # is no longer authoritative for the same content.
                return True
            prior_state = prior.get("state")
            if prior_state in (FINDING_STATE_SUPERSEDED, FINDING_STATE_REPAIRED):
                # Round-28 P5: a terminal state on the SAME
                # head still suppresses the finding. (The
                # cross-head case is handled below when no
                # entry exists on the current head.)
                return False
            # OBSERVED, DISPATCHED, or ACTIVE on the same head with the
            # same signature -> the finding is still unresolved; emit.
            return True
        # No entry on the current head.
        # Round-28 P5: SUPERSEDED/REPAIRED on a DIFFERENT
        # head MUST NOT automatically silence a fresh
        # observation of the same finding on the current
        # head. The user explicitly rejected the round-27
        # cross-head shadowing: a stale SUPERSEDED on A does
        # not mean the same finding on B is resolved. Without
        # positive cross-head resolution evidence, the
        # finding on B is FRESH and the directive MUST
        # contain it.
        #
        # Positive cross-head resolution evidence: the
        # ``latest_terminal_state`` API remains available for
        # callers that want to OPT IN to cross-head
        # shadowing with their own positive-evidence policy.
        # ``is_fresh`` itself does NOT consult it; the
        # cross-head case below returns True unconditionally.
        return True

    def latest_terminal_state(
        self,
        finding_id: str,
        signature: str,
    ) -> Optional[Dict[str, Any]]:
        """Walk the journal in reverse for the strongest
        terminal state (SUPERSEDED or REPAIRED) for
        ``finding_id`` with the matching ``signature``.

        ``is_fresh`` consults this when no entry exists on the
        current head. A terminal entry on a PRIOR head means
        the finding has already been resolved at the prior
        head; the new head should NOT re-emit unless fresh
        evidence (different signature) is available.
        """
        try:
            rows = list(self.store.read_journal(self._rel_path()))
        except (OSError, AttributeError):
            return None
        for entry in reversed(rows):
            if not isinstance(entry, dict):
                continue
            if entry.get("finding_id") != finding_id:
                continue
            if entry.get("signature") != signature:
                continue
            state = entry.get("state")
            if state in (FINDING_STATE_SUPERSEDED, FINDING_STATE_REPAIRED):
                return entry
        return None

    def record_observed(self, finding: "Finding") -> None:
        """Mark a finding as OBSERVED on the current head.

        Called when the relay's collector sees the finding in the
        live snapshot. The finding is NOT yet in a directive.
        """
        self._validate_finding(finding)
        self._append(finding, FINDING_STATE_OBSERVED)

    def record_dispatched(self, finding: "Finding") -> None:
        """Mark a finding as DISPATCHED on the current head.

        Called when the relay places the finding into a directive.
        The worker has not yet responded; the next round on the
        same head MUST still emit the finding (this is the
        round-26 bug fixed: DISPATCHED is NOT 'consumed').
        """
        self._validate_finding(finding)
        self._append(finding, FINDING_STATE_DISPATCHED)

    def mark_active(self, finding: "Finding") -> None:
        """Mark a DISPATCHED finding ACTIVE.

        Called when the relay has placed the finding in a directive
        AND the worker has acknowledged receipt (the worker
        process is alive and the directive SHA matches). The
        finding remains ACTIVE until one of:

          - The head advances and ``mark_superseded_by_head``
            promotes it to SUPERSEDED.
          - A subsequent evaluation proves the finding no longer
            applies (resolved upstream) and ``mark_repaired``
            promotes it to REPAIRED.

        A worker that crashes, times out, or exits without
        pushing leaves the finding in DISPATCHED -> ACTIVE on
        the SAME head; the next round re-emits it.
        """
        self._validate_finding(finding)
        self._append(finding, FINDING_STATE_ACTIVE)

    def mark_repaired(
        self, finding: "Finding", *,
        resolution_evidence: Optional[str] = None,
    ) -> None:
        """Mark a finding as REPAIRED on the current head.

        Called when positive evidence proves the finding no
        longer applies (e.g. CodeRabbit marked the thread
        resolved, the CI failure cleared with the same
        signature, or the file under review was rewritten to
        satisfy the requirement). The ``resolution_evidence``
        argument is recorded in the journal for audit.

        REPAIRED is a terminal state for the SAME HEAD. A
        re-introduction of the SAME finding (same signature) at
        the same head does not happen by design — if the body
        changed the signature differs and ``is_fresh`` returns
        True. A re-introduction at a DIFFERENT head is a new
        finding on a new head and starts at OBSERVED.
        """
        self._validate_finding(finding)
        sig = self._signature(finding)
        entry = {
            "schema_version": FINDING_LEDGER_SCHEMA_VERSION,
            "finding_id": finding.finding_id,
            "source": finding.source,
            "severity": finding.severity,
            "head_sha": self.head_sha,
            "signature": sig,
            "state": FINDING_STATE_REPAIRED,
            "resolution_evidence": resolution_evidence or "",
            "recorded_at": _now_iso(),
        }
        try:
            self.store.append_journal(self._rel_path(), entry)
        except (OSError, AttributeError):
            pass

    def mark_superseded_by_head(
        self,
        old_head_sha: str,
        *,
        new_head_sha: Optional[str] = None,
        directive_id: Optional[str] = None,
        superseded_at: Optional[str] = None,
    ) -> int:
        """Mark every ACTIVE finding on ``old_head_sha`` as
        SUPERSEDED.

        Called when the supervisor observes the worker push and
        the head advances from ``old_head_sha`` to a new SHA.
        The ledger rewrites every ACTIVE / DISPATCHED / OBSERVED
        entry for ``old_head_sha`` to SUPERSEDED. SUPERSEDED is
        a terminal state on that head.

        Round-C22R2/P1 (true repair-boundary binding): when
        ``new_head_sha`` is provided, the SUPERSEDED row also
        records:

          - ``superseded_by_head``: the authoritative NEW head
            SHA that replaced the finding's evidence. This is
            NOT a git-ancestry-derived value; it is the worker
            push the controller observed at the moment of the
            transition (the same value ``report_repair_pushed``
            binds to ``AWAITING_CI``).
          - ``superseded_at`` (already exists): ISO 8601
            timestamp of the authoritative verified repair /
            push event. When ``superseded_at`` is supplied
            (Round-C24 / Defect 3) the relay persists that
            value verbatim so a reviewer follow-up posted
            AFTER the actual verified push but BEFORE the
            supervisor observes the push still compares
            correctly against the real transition time. When
            ``superseded_at`` is None, the relay falls back
            to ``_now_iso()`` (the legacy behaviour) — a
            strictly later wall-clock observation time that
            the audit flagged as defect-prone.
          - ``directive_id`` (optional): the relay's directive
            UUID that drove the head advance. The audit's
            preference is to record whatever durable
            authoritative transition evidence is already
            available, so the snapshot's eligibility helper
            can later look up ``superseded_at`` / ``superseded_by_head``
            for a prior finding ID.

        Backward-compat: when ``new_head_sha`` is None, the
        function preserves the legacy behaviour (no
        ``superseded_by_head`` field on the row) so any
        existing call site or test fixture that does not pass
        the new argument continues to work. Production callers
        in ``loop.mark_head_advanced`` MUST pass the new
        head SHA so the durable evidence is recorded.

        Returns the number of entries that were promoted.

        The function NEVER deletes entries; it APPENDS the new
        SUPERSEDED rows to the append-only JSONL journal. The
        ``load`` helper keeps the LAST write per finding_id, so
        the SUPERSEDED row shadows the prior entry. The journal
        remains a valid JSONL for ``read_journal``.
        """
        if not isinstance(old_head_sha, str) or not _HEX_SHA_RE.match(old_head_sha):
            raise DirectiveContractError(
                f"mark_superseded_by_head requires a hex head_sha, "
                f"got {old_head_sha!r}"
            )
        # Round-C22R2/P1: validate new_head_sha so the ledger's
        # SUPERSEDED row never records a malformed SHA. The
        # controller's rebind path validates again later (and
        # raises ControllerError), but we want a malformed
        # value to fail closed before any ledger writes happen.
        # When ``new_head_sha`` is None we preserve the legacy
        # behaviour (no ``superseded_by_head`` field on the
        # row) so old call sites continue to work.
        if new_head_sha is not None and (
            not isinstance(new_head_sha, str)
            or not _HEX_SHA_RE.match(new_head_sha)
        ):
            raise DirectiveContractError(
                f"mark_superseded_by_head new_head_sha must be hex "
                f"(40 or 64 lowercase hex chars), got {new_head_sha!r}"
            )
        if directive_id is not None and not isinstance(directive_id, str):
            raise DirectiveContractError(
                f"mark_superseded_by_head directive_id must be str, "
                f"got {type(directive_id).__name__}"
            )
        try:
            rows = list(self.store.read_journal(self._rel_path()))
        except (OSError, AttributeError):
            return 0
        if not rows:
            return 0
        # Walk the journal in order; for each finding_id on the
        # old head, append a SUPERSEDED row that shadows the
        # prior entry. ``load`` returns the last write per
        # finding_id, so the shadow takes effect immediately.
        # We compute the per-finding latest entry first to
        # avoid stacking multiple SUPERSEDED rows for the same
        # finding (idempotent promotion).
        latest_per_id: Dict[str, Dict[str, Any]] = {}
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            fid = entry.get("finding_id")
            if not isinstance(fid, str):
                continue
            latest_per_id[fid] = entry
        promoted = 0
        for fid, entry in latest_per_id.items():
            if entry.get("head_sha") != old_head_sha:
                continue
            if entry.get("state") not in (
                FINDING_STATE_ACTIVE,
                FINDING_STATE_DISPATCHED,
                FINDING_STATE_OBSERVED,
            ):
                continue
            superseded = dict(entry)
            superseded["state"] = FINDING_STATE_SUPERSEDED
            # Round-C24 / Defect 3: prefer the authoritative
            # verified repair / push event time when the caller
            # supplies ``superseded_at``. Falling back to
            # ``_now_iso()`` (the legacy behaviour) is the audit's
            # documented defect because the supervisor's
            # observation wall clock is strictly later than the
            # verified push it observes.
            # Missing trustworthy worker/push timing fails closed for
            # outdated-thread resurrection. The later ledger observation
            # wall clock must never masquerade as the repair boundary.
            if (
                isinstance(superseded_at, str)
                and superseded_at
                and _parse_iso8601_utc(superseded_at) is not None
            ):
                superseded["superseded_at"] = superseded_at
            else:
                superseded.pop("superseded_at", None)
            # Round-C22R2/P1: durable superseding-head evidence.
            # When the caller passes ``new_head_sha`` we record
            # it; without it we leave the legacy row shape
            # unchanged so old consumers / test fixtures still
            # parse the JSONL correctly.
            if new_head_sha is not None:
                superseded["superseded_by_head"] = new_head_sha
            if directive_id is not None:
                superseded["directive_id"] = directive_id
            try:
                self.store.append_journal(self._rel_path(), superseded)
                promoted += 1
            except (OSError, AttributeError):
                # Ledger writes are advisory; do not block the
                # controller on a journal write failure.
                pass
        return promoted

    def superseded_repair_transition(
        self, finding_id: str,
    ) -> Optional[Dict[str, str]]:
        """Round-C22R2/P1: return the authoritative SUPERSEDED
        transition record for ``finding_id``, or ``None`` when
        no durable evidence exists.

        Returns a dict with these keys (when evidence is
        present):

          - ``superseded_by_head``: the new head SHA recorded
            by ``mark_superseded_by_head(new_head_sha=...)``
            at the moment the worker pushed the repair.
          - ``superseded_at``: the ISO 8601 wall-clock at which
            the transition was recorded.
          - ``directive_id``: the relay's directive UUID (when
            recorded by the caller).

        Returns ``None`` when:

          - ``finding_id`` has no SUPERSEDED entry;
          - the SUPERSEDED entry predates the C22R2 schema and
            therefore lacks a ``superseded_by_head`` field
            (the audit's contract: fail closed rather than
            fall back to a git-ancestry heuristic);
          - any of the values are malformed (non-string
            ``superseded_by_head``, etc.).

        The helper is read-only; it never modifies the
        journal. It walks the JSONL append-only log
        backwards so the most-recent SUPERSEDED row for the
        finding_id wins (in case the worker pushed multiple
        times and the same finding was superseded repeatedly).
        """
        if not isinstance(finding_id, str) or not finding_id:
            return None
        try:
            rows = list(self.store.read_journal(self._rel_path()))
        except (OSError, AttributeError):
            return None
        # Walk in reverse so the most-recent SUPERSEDED row
        # for this finding_id is preferred.
        for entry in reversed(rows):
            if not isinstance(entry, dict):
                continue
            if entry.get("finding_id") != finding_id:
                continue
            if entry.get("state") != FINDING_STATE_SUPERSEDED:
                continue
            sbh = entry.get("superseded_by_head")
            sat = entry.get("superseded_at")
            did = entry.get("directive_id")
            # Fail closed if any required field is missing or
            # malformed. The audit forbids falling back to a
            # git-ancestry heuristic; without the durable
            # ``superseded_by_head`` we must return None.
            if not isinstance(sbh, str) or not sbh:
                return None
            if not _HEX_SHA_RE.match(sbh):
                return None
            if not isinstance(sat, str) or not sat:
                return None
            if did is not None and not isinstance(did, str):
                return None
            out: Dict[str, str] = {
                "superseded_by_head": sbh,
                "superseded_at": sat,
            }
            if did is not None:
                out["directive_id"] = did
            return out
        return None

    def state_of(self, finding_id: str) -> Optional[Dict[str, Any]]:
        """Return the canonical ledger entry for ``finding_id`` on
        the current head, or ``None`` if the finding has no entry.

        Round-27: prefers the latest NON-OBSERVED entry. OBSERVED
        is the weakest state (the relay has seen the finding in
        a snapshot but no directive yet); if a stronger state
        (DISPATCHED, ACTIVE, REPAIRED, SUPERSEDED) exists for
        the same finding on the same head, that stronger state is
        the canonical one and ``is_fresh`` / ``filter_findings_to_current_head``
        MUST consult it.
        """
        prior = self.load().get(finding_id)
        if prior is None:
            return None
        if prior.get("head_sha") != self.head_sha:
            return None
        # ``load`` returns the last write per finding_id; if the
        # last write is OBSERVED but an earlier write is a
        # stronger state, ``load`` does not surface it. Walk the
        # journal in reverse to find the canonical entry.
        if prior.get("state") != FINDING_STATE_OBSERVED:
            return prior
        try:
            rows = list(self.store.read_journal(self._rel_path()))
        except (OSError, AttributeError):
            return prior
        for entry in reversed(rows):
            if not isinstance(entry, dict):
                continue
            if entry.get("finding_id") != finding_id:
                continue
            if entry.get("head_sha") != self.head_sha:
                continue
            state = entry.get("state")
            if state not in ALL_FINDING_STATES:
                continue
            if state != FINDING_STATE_OBSERVED:
                return entry
        return prior


def filter_findings_to_current_head(
    findings: List["Finding"],
    ledger: FindingLedger,
) -> List["Finding"]:
    """Apply the round-27 lifecycle filter.

    The relay's ``collect_findings`` still emits ALL findings
    from the live snapshot; this filter applies the ledger's
    current-head rule (see ``FindingLedger.is_fresh`` for the
    full lifecycle). Findings whose prior entry on the current
    head is ``SUPERSEDED`` or ``REPAIRED`` are dropped; findings
    whose prior entry is ``OBSERVED``, ``DISPATCHED``, or
    ``ACTIVE`` are kept (the worker has not yet responded or
    the finding is still open).

    Side effects: every surviving finding is recorded as
    ``OBSERVED`` on the current head so the next round's
    lifecycle has a fresh entry to compare against. ``ACTIVE``
    and ``DISPATCHED`` transitions are written explicitly by
    ``run_once`` when the directive is built.
    """
    out: List["Finding"] = []
    for f in findings:
        if not isinstance(f, Finding):
            continue
        if ledger.is_fresh(f):
            out.append(f)
            ledger.record_observed(f)
    return out


# === Finding ===

@dataclass(frozen=True)
class Finding:
    """A single actionable finding extracted from the snapshot.

    The collector produces these; the directive builder consumes
    them. Findings are PURE data — no I/O, no subprocess, no state
    mutation. This makes the finding classification testable in
    isolation.
    """

    finding_id: str
    source: str  # "coderabbit" | "codex" | "ci"
    severity: str
    title: str
    body: str
    file_path: Optional[str]
    line: Optional[int]
    url: Optional[str]
    suggested_test: Optional[str]
    review_id: Optional[int]
    comment_id: Optional[int]
    check_name: Optional[str]

    def to_dict(self) -> dict:
        return {
            "finding_id": self.finding_id,
            "source": self.source,
            "severity": self.severity,
            "title": self.title,
            "body": self.body,
            "file_path": self.file_path,
            "line": self.line,
            "url": self.url,
            "suggested_test": self.suggested_test,
            "review_id": self.review_id,
            "comment_id": self.comment_id,
            "check_name": self.check_name,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Finding":
        if not isinstance(payload, dict):
            raise DirectiveContractError(f"finding must be a dict, got {type(payload).__name__}")
        for field_name in ("finding_id", "source", "severity", "title", "body"):
            if field_name not in payload:
                raise DirectiveContractError(f"finding missing required field: {field_name!r}")
        return cls(
            finding_id=str(payload["finding_id"]),
            source=str(payload["source"]),
            severity=str(payload["severity"]),
            title=str(payload["title"]),
            body=str(payload["body"]),
            file_path=payload.get("file_path"),
            line=payload.get("line"),
            url=payload.get("url"),
            suggested_test=payload.get("suggested_test"),
            review_id=payload.get("review_id"),
            comment_id=payload.get("comment_id"),
            check_name=payload.get("check_name"),
        )


# === ReviewDirective ===

@dataclass(frozen=True)
class ReviewDirective:
    """A single structured repair directive for Humphry.

    The directive is the durable contract between the relay (writer)
    and Humphry (reader). It is persisted to ``evidence-root
    directive.json`` PLUS its sidecar digest before the worker is
    launched. The directive body is the entire spec for the round;
    Humphry does not need to re-query CodeRabbit or CI because the
    relay has already done so.

    The directive MUST be deterministic for a given (round_index,
    head_sha, findings) tuple. The ``directive_sha256`` is computed
    on canonical serialization so any tampering is detectable.
    """

    schema_version: str
    directive_id: str
    round_index: int
    head_sha: str
    repo: str
    pr_number: int
    created_at: str
    findings: Tuple[Finding, ...]
    summary: str
    coordinator_actor: str
    target_thread_id: Optional[str] = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.head_sha, str)
            or not _HEX_SHA_RE.match(self.head_sha)
        ):
            raise DirectiveContractError(
                f"head_sha must be 40 or 64 lowercase hex chars: {self.head_sha!r}"
            )
        if self.round_index < 0:
            raise DirectiveContractError(
                f"round_index must be non-negative: {self.round_index!r}"
            )
        if not self.findings:
            raise DirectiveContractError(
                "directive must contain at least one finding"
            )
        for f in self.findings:
            if f.severity not in ALL_SEVERITIES:
                raise DirectiveContractError(
                    f"finding has unknown severity {f.severity!r}"
                )
        if self.target_thread_id is not None:
            expected_id = f"thread:{self.target_thread_id}"
            expected_finding = (
                f"thread:{self.target_thread_id!r}"
            )
            # Round-45 C13 (round-590 correction): when
            # ``target_thread_id`` is set, focused mode scopes
            # the REVIEW portion of the directive to the
            # targeted current-head thread. CI_FAILURE findings
            # are independently required by round-281 C22 §6
            # and are NOT subject to focused scope, so the
            # ``len(findings) == 1`` invariant from round-45
            # is RELAXED to:
            #
            #  - the targeted thread ``finding_id`` MUST appear
            #    in ``self.findings``;
            #  - no other REVIEW finding (severity in
            #    {``SEVERITY_P1``, ``SEVERITY_P2``,
            #    ``SEVERITY_P0_ESCALATE``} whose
            #    ``finding_id != thread:<target>``) MUST
            #    appear (focused review scope is preserved);
            #  - CI_FAILURE findings (severity ==
            #    ``SEVERITY_CI_FAILURE``) MAY appear; they are
            #    the explicit round-590 carve-out.
            targeted_found = any(
                f.finding_id == expected_id for f in self.findings
            )
            if not targeted_found:
                raise DirectiveContractError(
                    f"target_thread_id={expected_finding} requires "
                    f"finding_id={expected_id!r}; missing"
                )
            for f in self.findings:
                if f.finding_id == expected_id:
                    continue
                if f.severity == SEVERITY_CI_FAILURE:
                    continue
                raise DirectiveContractError(
                    f"target_thread_id={expected_finding} scopes "
                    f"review findings to the targeted thread only; "
                    f"unexpected finding {f.finding_id!r} "
                    f"(severity={f.severity!r})"
                )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "directive_id": self.directive_id,
            "round_index": self.round_index,
            "head_sha": self.head_sha,
            "repo": self.repo,
            "pr_number": self.pr_number,
            "created_at": self.created_at,
            "summary": self.summary,
            "coordinator_actor": self.coordinator_actor,
            "findings": [f.to_dict() for f in self.findings],
            # Round-767/P2: persist ``target_thread_id`` so a
            # focused directive survives ``write_directive``
            # -> ``from_dict`` round-trip. Without this field
            # the persisted artifact reconstructs as broad and
            # the worker receives the wrong scope.
            "target_thread_id": self.target_thread_id,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ReviewDirective":
        if not isinstance(payload, dict):
            raise DirectiveContractError(f"directive must be a dict, got {type(payload).__name__}")
        for field_name in (
            "schema_version", "directive_id", "round_index",
            "head_sha", "repo", "pr_number", "created_at",
            "summary", "coordinator_actor", "findings",
        ):
            if field_name not in payload:
                raise DirectiveContractError(f"directive missing required field: {field_name!r}")
        # Round-767/P2: ``target_thread_id`` is optional and
        # absent from broad directives persisted before this
        # round. Tolerate the missing key for backward-compat
        # with older artifacts (the field defaults to ``None``
        # on the dataclass).
        target_thread_id_raw = payload.get("target_thread_id")
        target_thread_id = (
            str(target_thread_id_raw) if target_thread_id_raw is not None else None
        )
        return cls(
            schema_version=str(payload["schema_version"]),
            directive_id=str(payload["directive_id"]),
            round_index=int(payload["round_index"]),
            head_sha=str(payload["head_sha"]),
            repo=str(payload["repo"]),
            pr_number=int(payload["pr_number"]),
            created_at=str(payload["created_at"]),
            summary=str(payload["summary"]),
            coordinator_actor=str(payload["coordinator_actor"]),
            findings=tuple(Finding.from_dict(f) for f in payload["findings"]),
            target_thread_id=target_thread_id,
        )

    def compute_sha256(self) -> str:
        """SHA-256 of the canonical serialization of the directive.

        The canonical form excludes the persisted ``_sha256``
        metadata field so the digest matches the
        write_artifact-sidecar digest that the bridge
        independently verifies (C-22). This also matches the
        supervisor's ``_directive_prompt.compute_directive_sha256``
        so the worker-prompt SHA-256 is the same string the
        bridge sees in its verification step.
        """
        payload = self.to_dict()
        payload_without_digest = {k: v for k, v in payload.items() if k != "_sha256"}
        return hashlib.sha256(
            json.dumps(
                payload_without_digest, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()


# === Round transcript ===

@dataclass(frozen=True)
class RoundTranscript:
    """Durable record of one relay round.

    The relay writes one transcript per round to the state store so
    a restart can resume from the last known position. The
    transcript is the only place round-progress information lives
    (the relay intentionally does not use the supervisor's
    in-memory event-loop state).
    """

    schema_version: str
    round_index: int
    head_sha_before: str
    head_sha_after: Optional[str]
    directive_id: Optional[str]
    started_at: str
    ended_at: Optional[str]
    outcome: str  # "completed" | "blocked" | "escalated" | "in_progress"
    p1_count: int
    p2_count: int
    ci_failure_count: int
    escalate_reasons: Tuple[str, ...]
    # Round-32: slice_epoch scopes the round budget per
    # execution slice. Transcripts from prior slices do
    # NOT count against the current slice's budget.
    # Optional for backward-compat with round-31 (and
    # earlier) transcripts; missing ``slice_epoch`` is
    # treated as ``0`` (the original slice).
    slice_epoch: int = 0

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "round_index": self.round_index,
            "head_sha_before": self.head_sha_before,
            "head_sha_after": self.head_sha_after,
            "directive_id": self.directive_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "outcome": self.outcome,
            "p1_count": self.p1_count,
            "p2_count": self.p2_count,
            "ci_failure_count": self.ci_failure_count,
            "escalate_reasons": list(self.escalate_reasons),
            "slice_epoch": self.slice_epoch,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "RoundTranscript":
        for field_name in (
            "schema_version", "round_index", "head_sha_before",
            "started_at", "outcome", "p1_count", "p2_count",
            "ci_failure_count",
        ):
            if field_name not in payload:
                raise DirectiveContractError(f"transcript missing required field: {field_name!r}")
        return cls(
            schema_version=str(payload["schema_version"]),
            round_index=int(payload["round_index"]),
            head_sha_before=str(payload["head_sha_before"]),
            head_sha_after=payload.get("head_sha_after"),
            directive_id=payload.get("directive_id"),
            started_at=str(payload["started_at"]),
            ended_at=payload.get("ended_at"),
            outcome=str(payload["outcome"]),
            p1_count=int(payload["p1_count"]),
            p2_count=int(payload["p2_count"]),
            ci_failure_count=int(payload["ci_failure_count"]),
            escalate_reasons=tuple(payload.get("escalate_reasons", [])),
            slice_epoch=int(payload.get("slice_epoch", 0)),
        )


# === Snapshot-driven helpers ===

def _classify_severity(body: str) -> str:
    """Classify a review comment body into a severity.

    The classifier is INTENTIONALLY conservative: unknown bodies
    default to ``P2`` (preferred but not blocking). The downstream
    directive builder decides whether to fold them into a round.
    Bodies containing the explicit ``P0`` marker trigger
    escalation.
    """
    m = _SEVERITY_MARKER_RE.search(body)
    if m is None:
        return SEVERITY_P2
    token = m.group("sev").upper()
    if token == "P0":
        return SEVERITY_P0_ESCALATE
    if token in ("P1", "PRIORITY-HIGH"):
        return SEVERITY_P1
    if token in ("P2", "PRIORITY-MEDIUM"):
        return SEVERITY_P2
    if token == "PRIORITY-LOW":
        return SEVERITY_P2
    return SEVERITY_P2


def _extract_anchor(body: str) -> Tuple[Optional[str], Optional[int]]:
    """Extract ``(path, line)`` from a review-comment body.

    Returns ``(None, None)`` if no anchor is detectable. The
    function intentionally does NOT use the comment's API ``path``
    field because CodeRabbit embeds short snippets in the body
    that often include the resolved anchor.
    """
    m = _PATH_ANCHOR_RE.search(body)
    if m is None:
        return None, None
    path = m.group("path")
    line_str = m.group("line")
    line = int(line_str) if line_str is not None else None
    return path, line


def _extract_suggested_test(body: str) -> Optional[str]:
    """Return a heuristic test name suggestion or ``None``."""
    m = _TEST_SUGGEST_RE.search(body)
    return m.group("test") if m is not None else None


# A regex that detects the most common "this is not a
# finding" provider comments. The supervisor's snapshot
# captures every comment authored by a provider across the
# PR history, including walkthrough / in-progress /
# completion markers. These are status updates, not
# actionable findings, and must be filtered out so the
# relay does not repeatedly launch workers for a clean
# head.
#
# Round-32 P1#8: extended the marker set with three extra
# status-marker tokens that appear on real CodeRabbit /
# Codex snapshots. Without them, a clean head's
# "**Actionable comments posted: 0**" summary was turned
# into a persistent P2 finding; the relay then dispatched
# a worker for a non-existent finding, parked the head in
# ACTIVE_REPAIR, and lost the snapshot deltas that would
# have surfaced the real next event.
_NON_FINDING_COMMENT_RE = re.compile(
    r"^\s*(?:"  # Anchor to start of body (first line).
    r"walkthrough\b|"
    r"in progress\b|"
    r"review in progress\b|"
    r"in-review\b|"
    r"review complete\b|"
    r"review completed\b|"
    r"review approved\b|"
    r"review request\b|"
    r"finished review\b|"
    r"commented on your changes\b|"
    r"finished\b|"
    r"started review\b|"
    r"approved these changes\b|"
    r"left a comment\b|"
    r"requested changes\b|"
    # Round-32 P1#8: CodeRabbit's zero-finding summary.
    r"\*+\s*actionable comments posted:\s*0\s*\*+|"
    # Round-32 P1#8: codex zero-finding summary variant.
    r"\*+\s*no actionable issues?\s*(were|found)?\s*\*+|"
    # Round-32 P1#8: codex "no issues found" summary.
    r"\*+\s*no issues (found|to (report|fix))\s*\*+"
    r")[^\n]*$"  # Status markers are short single-line.
    , re.IGNORECASE
)


def _is_actionable_provider_comment(body: str) -> bool:
    """Return False for walkthrough / in-progress / completion
    comments; True for ACTUAL inline-comment findings.

    The supervisor's snapshot captures every provider
    comment across the PR history. Status markers
    (walkthrough, in-progress, completion) are NOT
    findings — they are reviews in progress, not review
    findings. Filtering them out prevents the relay from
    turning a clean head into a persistent repair loop.

    Anchoring: a body is only a status marker when its
    FIRST non-empty line is exactly (or starts with) a
    status marker. A body that merely mentions "walkthrough"
    in passing (e.g. "P1: the walkthrough above is stale")
    is NOT a status marker and is treated as actionable.
    This prevents the filter from dropping real findings.

    Round-32 P1#8: the alphanumeric-strip path also
    covers provider headers wrapped in ``<sub>...</sub>``
    tags (CodeRabbit's standard decoration). We strip
    the tag syntax before the alphanumeric pass so a
    body like ``<sub>📝 Walkthrough (commented)</sub>``
    collapses to ``sub Walkthrough commentedsub`` and the
    status-marker match proceeds on the visible text.
    """
    if not body:
        return False
    stripped = body.strip()
    if not stripped:
        return False
    # The first non-empty line sets the comment's intent.
    first_line = stripped.split("\n", 1)[0].strip().rstrip(".,;:!?")
    # Strip ``<sub>...</sub>`` / ``<sup>...</sup>`` tags
    # that providers use to wrap status headers.
    first_line_no_tags = re.sub(
        r"</?(?:sub|sup)>", "", first_line, flags=re.IGNORECASE,
    ).strip()
    # Strip leading emoji / decorative characters. A
    # status marker may be prefixed with a traffic-light
    # emoji (🚦), a bell (🔔), etc. The alpha-stripped
    # first line is the canonical form.
    alphanumeric_first_line = "".join(
        c for c in first_line_no_tags if c.isalnum() or c.isspace()
    ).strip()
    # If the first line is a status marker (with optional
    # emoji / whitespace prefix), the entire comment is a
    # status marker.
    if (
        _NON_FINDING_COMMENT_RE.match(first_line)
        or _NON_FINDING_COMMENT_RE.match(first_line_no_tags)
        or _NON_FINDING_COMMENT_RE.match(alphanumeric_first_line)
    ):
        # And the body is short (single-line status marker).
        # Long bodies with a status-marker first line are
        # treated as actionable (the body has real content).
        if len(stripped) < 200 and stripped.count("\n") <= 2:
            return False
    return True


#: Default operator-account set used by ``_maybe_resurrect_outdated_thread``
#: when the snapshot does not carry one. ``coderabbitai[bot]``,
#: ``chatgpt-codex-connector[bot]``, and any ``[bot]``-suffixed account
#: are NEVER operator accounts. ``github-actions`` covers CI-bot replies.
#: The list is intentionally conservative: reviewers (CodeRabbit, Codex)
#: are NOT operators. The supervisor may override this via the snapshot's
#: ``operator_logins`` field when richer identity is available.
_C22_DEFAULT_OPERATOR_LOGINS: Tuple[str, ...] = (
    "github-actions",
    "github-actions[bot]",
)


def _parse_iso8601_utc(value: object) -> Optional[int]:
    """Return a timestamp-seconds value for a GitHub-style ISO 8601
    timestamp, or ``None`` when the value is missing / malformed.

    GitHub returns ``createdAt`` / ``updatedAt`` as UTC strings like
    ``"2026-08-19T14:27:14Z"`` or ``"2026-08-19T14:27:14.000Z"``.
    The function is strict: anything unparseable returns ``None`` so
    the caller can fall back to the strict "missing evidence" path
    instead of guessing. The returned value is comparable across
    comments in the same thread.

    Round-C22R1/S2 hardening: a bare date (``"2026-08-19"``) is NOT
    a valid GitHub timestamp; the function rejects it. Only full
    datetime strings (with a time component) are accepted, so the
    eligibility rule cannot accidentally treat a date-only string
    as a coincident-timestamp resurrection.
    """
    if not isinstance(value, str) or not value:
        return None
    # ``fromisoformat`` in 3.11 handles ``Z``; older builds need ``+00:00``.
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        import datetime as _dt
        dt = _dt.datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    # Round-C22R1/S2: ``fromisoformat`` accepts date-only
    # strings like ``"2026-08-19"`` and returns a ``datetime``
    # instance with time ``00:00:00``. GitHub does not emit
    # date-only timestamps; treat them as missing evidence so
    # the eligibility rule fails closed rather than treating
    # a truncated string as a coincident-timestamp match.
    #
    # The heuristic: ``fromisoformat("YYYY-MM-DD")`` always
    # produces a ``datetime`` with
    # ``(hour, minute, second, microsecond) == (0, 0, 0, 0)``.
    # Real GitHub timestamps never have that exact shape.
    if (
        dt.hour == 0
        and dt.minute == 0
        and dt.second == 0
        and dt.microsecond == 0
        and "T" not in raw
    ):
        return None
    if dt.tzinfo is None:
        # Treat naive timestamps as UTC; GitHub always emits Z.
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return int(dt.timestamp())


def _c22_is_followup_eligible(
    *,
    followup: dict,
    repair_transition_ts: Optional[int],
    operator_logins: Tuple[str, ...],
    superseding_head: Optional[str] = None,
) -> bool:
    """Return True iff ``followup`` is a non-operator, post-repair,
    actionable reply that can resurrect an outdated thread.

    The eligibility rule (Autonomy C22-R2):

      R1. followup author must NOT be in ``operator_logins`` (operator
          accounts can explain away a finding without re-elevating it).
      R2. followup must carry a ``createdAt`` strictly later than the
          ``repair_transition_ts`` — the Unix-seconds wall-clock
          timestamp at which the durable FindingLedger recorded the
          authoritative superseding-head transition
          (``FindingLedger.mark_superseded_by_head(new_head_sha=...)``).
          The ledger row is the canonical evidence; it is populated
          by ``loop.mark_head_advanced(old_head_sha, new_head_sha,
          directive_id=...)`` at the moment the controller observes
          the worker push and binds ``report_repair_pushed`` to
          ``AWAITING_CI``.
      R3. followup body must pass ``_is_actionable_provider_comment``
          so status markers (``Walkthrough``, ``In progress``,
          etc.) do not resurrect threads.

    R2 fails closed when:

      - ``repair_transition_ts`` is None (the durable ledger has
        no SUPERSEDED row for the prior finding, or the row's
        ``superseded_at`` is missing / unparseable);
      - ``followup.createdAt`` is None or unparseable.

    The R2 comparison is intentionally STRICT (>) so an
    equal-timestamp followup (e.g. an off-by-second race) does
    not silently resurrect an already-addressed historical thread.

    The audit's contract forbids falling back to a
    git-ancestry-derived boundary when the durable evidence is
    missing. The C22-R1 helper used ``git log --reverse
    --ancestry-path <anchor>..<head>`` and was disproven by the
    Codex review: an unrelated docs commit between the original
    anchor and the actual repair is still a descendant of the
    anchor, and the heuristic would treat the docs commit as
    the boundary. The C22-R2 helper therefore reads the
    authoritative ledger record exclusively.
    """
    if not isinstance(followup, dict):
        return False
    # R1: non-operator author.
    author = followup.get("author")
    if (
        not isinstance(author, str)
        or not author
        or author in operator_logins
    ):
        return False
    # R2: strictly-later-than-repair-transition timestamp.
    # Missing / unparseable createdAt on either side returns False
    # (we cannot claim "post-repair" without a timestamp anchor on
    # both ends).
    followup_ts = _parse_iso8601_utc(followup.get("createdAt"))
    if followup_ts is None or repair_transition_ts is None:
        return False
    # Round-C24-R2 / P1-A: exact-head follow-up binding.
    # When the follow-up evidence is bound to the new
    # superseding head (``commit_id`` or
    # ``original_commit_id`` equals ``superseding_head``),
    # the follow-up is provably post-repair regardless of
    # the wall-clock timestamp. The audit's preferred
    # exact-head identity contract replaces the wall-clock
    # inference that the audit invalidated in §1
    # (repo.pushed_at is the repo-level, not the PR-branch,
    # push time).
    # Round-C24-R2 / CodeRabbit CR-002: R3 is evaluated BEFORE the
    # exact-head binding branch. A status marker bound to the new
    # head (e.g. a "Walkthrough" reply whose ``commit_id`` equals
    # ``superseding_head``) must NOT resurrect an outdated thread;
    # the previous ordering returned True from the exact-head branch
    # before the actionable-body check ran (false-positive
    # resurrection path).
    # R3: actionable body — not a status marker.
    body = str(followup.get("body") or "")
    if not _is_actionable_provider_comment(body):
        return False
    # Round-C24-R2 / P1-A: exact-head follow-up binding.
    # When the follow-up evidence is bound to the new
    # superseding head (``commit_id`` or
    # ``original_commit_id`` equals ``superseding_head``),
    # the follow-up is provably post-repair regardless of
    # the wall-clock timestamp. The audit's preferred
    # exact-head identity contract replaces the wall-clock
    # inference that the audit invalidated in §1
    # (repo.pushed_at is the repo-level, not the PR-branch,
    # push time).
    if superseding_head:
        cmt = (
            followup.get("commit_id")
            or followup.get("original_commit_id")
        )
        if isinstance(cmt, str) and cmt:
            # The follow-up has an explicit commit_id
            # binding. If the binding matches the new
            # superseding head, the follow-up qualifies
            # regardless of timestamp. If the binding is
            # to a DIFFERENT head, the follow-up is provably
            # NOT on the new head — it cannot resurrect.
            if cmt == superseding_head:
                return True
            else:
                return False
    if followup_ts <= repair_transition_ts:
        return False
    return True


def _normalize_operator_logins(
    raw: object,
    *,
    fallback: Tuple[str, ...] = _C22_DEFAULT_OPERATOR_LOGINS,
) -> Tuple[str, ...]:
    """Round-C22R1/S1: normalize the ``operator_logins`` snapshot
    field into a canonical tuple of trimmed, non-empty strings.

    Contract:

    - ``None`` → ``fallback`` (backward-compatible fixture / no-
      operator-override default).
    - ``list`` / ``tuple`` / ``set`` of strings → trimmed, non-empty
      entries preserved; falsy / empty entries dropped; the result
      is a tuple. ``str(...)`` defends against non-string elements.
    - bare ``str`` (e.g. ``"github-actions"``) → ``fallback``.
      Treating it as an iterable would otherwise explode it into a
      tuple of single-character logins, which is the exact bug
      Sourcery flagged. ``len(raw) > some-large-threshold`` would
      also miss cases where the operator's name happens to be a
      short string the user meant as ONE identity; the explicit
      type check is the right contract.
    - any other truthy non-iterable (``int``, ``float``, ``bool``,
      ``None``) → ``fallback``. ``tuple(int)`` raises ``TypeError``,
      which Sourcery flagged.
    - ``dict`` → ``fallback``. ``tuple({"a": 1, "b": 2})`` returns
      ``("a", "b")`` (keys, not values), which would silently
      introduce bogus operator logins. Reject explicitly.

    The normalizer never raises; every malformed input falls back
    to the canonical default.
    """
    if raw is None:
        return tuple(fallback)
    if isinstance(raw, str):
        # Plain string must not be expanded into characters.
        return tuple(fallback)
    if isinstance(raw, dict):
        # Dicts would expose KEYS, not values, if iterated. Reject.
        return tuple(fallback)
    if not isinstance(raw, (list, tuple, set, frozenset)):
        # Truthy non-iterable (int, float, bool, custom object).
        return tuple(fallback)
    out: List[str] = []
    seen: set = set()
    for entry in raw:
        if not isinstance(entry, str):
            continue
        s = entry.strip()
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    if not out:
        return tuple(fallback)
    return tuple(out)


def _maybe_resurrect_outdated_thread(
    thread_data: dict,
    *,
    current_head: Optional[str],
    operator_logins: Tuple[str, ...] = _C22_DEFAULT_OPERATOR_LOGINS,
) -> Optional[dict]:
    """C22-R1: reconsider an outdated, unresolved review thread when a
    NEW non-operator follow-up reply exists AFTER the head-changing
    repair.

    Returns a dict describing the qualifying follow-up, or ``None``
    when the thread is not eligible.

    The eligibility rule (see ``_c22_is_followup_eligible``):

      - the thread itself must be outdated and unresolved (the
        caller has already enforced that — this helper trusts the
        caller);
      - there must be a ``superseding_repair_committed_at`` on
        ``thread_data`` (the supervisor stamps this from
        ``_git_superseding_repair_committed_at``); a missing /
        unparseable boundary fails closed;
      - at least one reply must have ``createdAt`` strictly later
        than ``superseding_repair_committed_at`` (NOT just later
        than the first comment — see Autonomy C22-R1/P1-A);
      - the latest qualifying reply is returned so the caller can
        use its body / author / id as the actionable follow-up
        evidence.

    The ``superseding_repair_committed_at`` evidence is
    authoritative. ``comment.commit`` is NOT re-bound by GitHub
    when a thread goes outdated, so commit-oid evidence is
    unreliable; the supervisor computes the repair boundary by
    walking ``git log --reverse --ancestry-path <anchor>..<head>``
    and reading the committer timestamp of the FIRST descendant
    commit. This matches the audit's expectation that the
    follow-up must post-date the head-changing REPAIR that already
    addressed the original concern, not just any later commit on
    the branch (including documentation / report-only commits).

    Parameters
    ----------
    thread_data:
        The thread dict as captured by ``capture_live_snapshot``
        (``resolved/outdated/path/line/body/commit_oid/author/
        comment_count/top_id/replies`` plus
        ``superseding_repair_committed_at`` and
        ``replies[*].createdAt``). Both the
        ``superseding_repair_committed_at`` field and each
        reply's ``createdAt`` are used by the eligibility rule;
        missing timestamps fail closed (return None).
    current_head:
        The snapshot's exact PR head. Currently informational;
        reserved for the future case where head-anchored follow-ups
        become available on newer GraphQL payloads.
    operator_logins:
        Set of account names whose replies are treated as operator
        explanations and DO NOT resurrect outdated threads. Defaults
        to ``_C22_DEFAULT_OPERATOR_LOGINS`` (``github-actions``
        etc). The supervisor's snapshot may override via the
        ``operator_logins`` field, but this helper does not read it
        directly — callers (``_collect_review_findings``,
        ``collect_findings``) thread the override through their own
        argument plumbing after running it through
        ``_normalize_operator_logins``.

    Returns
    -------
    Optional[dict]
        A ``{"followup": <reply-dict>, "thread_id": <id>,
        "superseding_repair_committed_at": <int|None>}`` triple, or
        ``None``. The reply dict is the raw snapshot entry (NOT a
        shaped Finding) so the caller can preserve the original
        ``createdAt`` / ``author`` / ``databaseId`` provenance.
    """
    if not isinstance(thread_data, dict):
        return None
    # The eligibility rule treats ``resolved`` and ``outdated`` as
    # caller-enforced invariants, but we double-check defensively so a
    # naive caller cannot accidentally resurrect a stale or closed
    # thread.
    if thread_data.get("resolved"):
        return None
    if not thread_data.get("outdated"):
        return None
    replies = thread_data.get("replies") or []
    if not isinstance(replies, list) or not replies:
        return None
    # Round-C22R2/P1: the AUTHORITATIVE repair-transition
    # timestamp is the durable FindingLedger's
    # ``superseded_at`` record for the prior finding identity
    # (``thread:<thread_id>``). The snapshot stamps this on the
    # thread entry from
    # ``FindingLedger.superseded_repair_transition(...)``.
    # When the ledger has no record (or the record is missing
    # ``superseded_at``), the rule MUST fail closed — the
    # audit explicitly forbids falling back to a
    # git-ancestry-derived boundary. C22-R1's
    # ``superseding_repair_committed_at`` field is kept in
    # the thread dict purely as diagnostic provenance (the
    # snapshot still computes it) but is NOT consulted as a
    # fallback by this helper.
    repair_transition_ts: Optional[int] = None
    superseded_at_raw = thread_data.get("superseded_at")
    if isinstance(superseded_at_raw, int):
        repair_transition_ts = superseded_at_raw
    elif isinstance(superseded_at_raw, str) and superseded_at_raw:
        repair_transition_ts = _parse_iso8601_utc(superseded_at_raw)
    # Walk replies in their stored order; the snapshot already sorts
    # GraphQL comments by id and the supervisor preserves that order,
    # so the last entry is the freshest.
    qualifying: Optional[dict] = None
    for reply in replies:
        if not isinstance(reply, dict):
            continue
        if _c22_is_followup_eligible(
            followup=reply,
            repair_transition_ts=repair_transition_ts,
            operator_logins=tuple(operator_logins),
            superseding_head=(
                str(thread_data.get("superseded_by_head") or "")
                or None
            ),
        ):
            qualifying = reply
    if qualifying is None:
        return None
    return {
        "followup": qualifying,
        "thread_id": (
            thread_data.get("id")
            or thread_data.get("thread_id")
            or ""
        ),
        "superseded_at": superseded_at_raw,
        "superseded_by_head": thread_data.get("superseded_by_head"),
        # Round-C22R1 legacy field kept for callers that
        # already read it; ``superseded_at`` /
        # ``superseded_by_head`` are the canonical C22-R2
        # evidence.
        "superseding_repair_committed_at": (
            thread_data.get("superseding_repair_committed_at")
        ),
    }


def _collect_review_findings(snapshot: dict) -> List[Finding]:
    """Extract CodeRabbit-style inline-comment findings from a snapshot.

    The snapshot shape is the supervisor's
    ``capture_live_snapshot`` output. The collector accepts
    the head-bound ``provider_surfaces[provider].issue_comments``
    (preferred — Round-687/P1), the per-provider subset
    ``_provider_issue_comments`` (raw records with only a
    ``login`` field, no ``commit_id`` / ``review_cycle``), and/or
    the unfiltered ``issue_comments`` list. Provider matching is
    done by bot-login substring because the supervisor records bot
    logins under ``[bot]``-suffixed form for GitHub Apps.

    Status markers (walkthrough, in-progress, completion)
    are filtered out so the relay does not turn a clean
    head into a persistent repair loop.
    """
    if not isinstance(snapshot, dict):
        raise InvalidSnapshot("snapshot must be a dict")
    findings: List[Finding] = []
    seen_ids: set = set()
    # Round-C22R1/S3: cross-surface dedup by ``comment_id``. The
    # collector consumes multiple finding-emitting surfaces
    # (provider issue comments, ``review_comments``, C22
    # resurrected thread follow-ups). When the same logical
    # comment is visible through more than one surface (e.g.
    # the C22 resurrected follow-up also appears as an
    # inline-review-comment because GitHub exposes it in both
    # the thread and the PR's review-comment stream), the
    # ``seen_ids`` dedup by ``finding_id`` is INSUFFICIENT — the
    # inline path keys by ``inline:<comment_id>`` and the
    # thread path keys by ``thread:<thread_id>``, so they would
    # both pass the ``seen_ids`` gate. ``seen_comment_ids``
    # catches the duplicate by the underlying GitHub databaseId.
    seen_comment_ids: set = set()
    # Round-29 review (Codex): filter out issue comments
    # that are bound to a previous head. The
    # ``_provider_issue_comments`` list is the supervisor's
    # snapshot of every provider comment across the PR
    # history; an old CodeRabbit comment on commit A is
    # NOT a finding for the current head B. We retain
    # only comments whose bound commit (or, if missing,
    # whose creation timestamp) is consistent with the
    # current head. The ``commit_id`` field is what GitHub
    # emits; absent ``commit_id`` we conservatively keep
    # the comment (the supervisor's snapshot already
    # filters by the current head's review API).
    current_head = snapshot.get("head_sha")
    # Round-687/P1: consume the head-bound provider issue-comment
    # surface first. Production ``capture_live_snapshot()`` records
    # in ``_provider_issue_comments`` are raw GitHub issue-comment
    # dicts with only a top-level ``login`` field and no
    # ``commit_id`` / ``review_cycle`` provenance — selecting that
    # raw index makes the primary loop below reject every such
    # record (the head-binding check at lines 1252-1271 cannot
    # match without those fields). The head-bound copies live in
    # ``provider_surfaces[provider].issue_comments`` and carry
    # ``commit_id`` + ``review_cycle`` for the freshest bot-authored
    # comment in the current review cycle (per Round-31/140 in
    # ``collect_provider_surfaces``). Prefer those; fall back to
    # the raw subset only when a provider entry is absent from
    # the surfaces dict (legacy tests + pre-round-666 snapshots).
    primary: dict = {}
    raw_subset_obj = snapshot.get("_provider_issue_comments")
    raw_subset: dict = (
        raw_subset_obj if isinstance(raw_subset_obj, dict) else {}
    )
    surfaces_obj = snapshot.get("provider_surfaces")
    surfaces: dict = (
        surfaces_obj if isinstance(surfaces_obj, dict) else {}
    )
    # Collect every provider that appears in EITHER source so the
    # primary loop covers the full union without double-emitting.
    provider_keys = set(raw_subset.keys()) | set(surfaces.keys())
    for provider in provider_keys:
        surface_entry = surfaces.get(provider)
        surface_comments: list = []
        if isinstance(surface_entry, dict):
            sc = surface_entry.get("issue_comments", [])
            if isinstance(sc, list):
                surface_comments = sc
        if surface_comments:
            # Head-bound source wins; replace any raw subset
            # entries for this provider so the primary loop only
            # sees the head-bound records (which carry
            # ``commit_id`` / ``review_cycle``).
            primary[provider] = surface_comments
        elif provider in raw_subset:
            # Legacy / test path: no head-bound surface for this
            # provider; use the raw subset (test fixtures still
            # populate ``_provider_issue_comments`` with explicit
            # ``commit_id`` / ``review_cycle``).
            primary[provider] = raw_subset[provider]
    for provider, comments in primary.items():
        if not isinstance(comments, list):
            continue
        for c in comments:
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            if cid is None:
                continue
            # Round-32: skip comments bound to a different
            # commit than the current head OR with no
            # durable per-head review-cycle identity.
            #
            # Production captures (round-30+) carry BOTH
            # ``commit_id`` and ``review_cycle`` (they
            # are not duplicates — the cycle identity is
            # keyed by provider/head/cid, while
            # ``commit_id`` is the intrinsic commit). The
            # comment is bound to the current head when
            # EITHER ``commit_id == head_sha`` (legacy
            # path) OR ``review_cycle`` is present and
            # its cycle key matches the current head
            # (production path). The cycle key is
            # formatted ``provider:head_sha:cid`` so its
            # presence is sufficient proof of
            # current-head binding.
            #
            # Backward-compat: tests / older snapshots
            # that carry only ``commit_id`` matching
            # ``head_sha`` are also accepted.
            cmt = c.get("commit_id") or c.get("commit_oid")
            cycle = c.get("review_cycle")
            cmt_match = (
                isinstance(cmt, str)
                and bool(cmt)
                and bool(current_head)
                and cmt == current_head
            )
            cycle_match = bool(cycle)
            # Round-32: accept the comment when EITHER
            # ``cmt_match`` (legacy/backward-compat path)
            # OR ``cycle_match`` (production path) holds.
            # Requiring both (or neither) was wrong:
            # production captures carry both, so
            # demanding both excludes the production
            # path; demanding neither excludes the
            # legacy path. The comment is current-head-
            # bound when EITHER condition holds.
            if not (cmt_match or cycle_match):
                continue
            finding_id = f"{provider}:{cid}"
            if finding_id in seen_ids:
                continue
            # Round-C22R1/S3: cross-surface dedup.
            if isinstance(cid, int) and cid in seen_comment_ids:
                continue
            seen_ids.add(finding_id)
            if isinstance(cid, int):
                seen_comment_ids.add(cid)
            body = str(c.get("body") or "")
            if not _is_actionable_provider_comment(body):
                continue
            severity = _classify_severity(body)
            path, line = _extract_anchor(body)
            title = body.splitlines()[0] if body else "(no body)"
            findings.append(Finding(
                finding_id=finding_id,
                source=str(provider),
                severity=severity,
                title=title[:120],
                body=body,
                file_path=path,
                line=line,
                url=c.get("html_url"),
                suggested_test=_extract_suggested_test(body),
                review_id=None,
                comment_id=int(cid) if isinstance(cid, int) else None,
                check_name=None,
            ))
    # If no per-provider subset exists, fall back to the filtered
    # list of CodeRabbit issue comments.
    if not findings:
        # Inline review comments (path + line + body) are
        # surfaced when the supervisor's ``use_reviews_api``
        # flag is enabled. These are the headline
        # CodeRabbit / Codex findings — actionable inline
        # comments tied to a specific file/line. Treat
        # them as findings.
        for c in snapshot.get("review_comments", []) or []:
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            if cid is None:
                continue
            finding_id = f"inline:{cid}"
            if finding_id in seen_ids:
                continue
            # Round-C22R1/S3: cross-surface dedup. The same
            # comment may also surface as a C22 resurrected
            # follow-up in ``review_threads``; dedup by
            # the underlying GitHub databaseId.
            if isinstance(cid, int) and cid in seen_comment_ids:
                continue
            seen_ids.add(finding_id)
            if isinstance(cid, int):
                seen_comment_ids.add(cid)
            body = str(c.get("body") or "")
            if not _is_actionable_provider_comment(body):
                continue
            severity = _classify_severity(body)
            path = c.get("path")
            line = c.get("line")
            title = body.splitlines()[0] if body else "(no body)"
            findings.append(Finding(
                finding_id=finding_id,
                source="coderabbit",
                severity=severity,
                title=title[:120],
                body=body,
                file_path=str(path) if path else None,
                line=int(line) if isinstance(line, int) else None,
                url=None,
                suggested_test=None,
                review_id=None,
                comment_id=cid,
                check_name=None,
            ))
        for c in snapshot.get("issue_comments", []) or []:
            if not isinstance(c, dict):
                continue
            login = str((c.get("user") or {}).get("login") or "")
            if "coderabbit" not in login.lower():
                continue
            cid = c.get("id")
            if cid is None:
                continue
            finding_id = f"coderabbit:{cid}"
            if finding_id in seen_ids:
                continue
            # Round-C22R1/S3: cross-surface dedup by comment id.
            if isinstance(cid, int) and cid in seen_comment_ids:
                continue
            seen_ids.add(finding_id)
            if isinstance(cid, int):
                seen_comment_ids.add(cid)
            body = str(c.get("body") or "")
            if not _is_actionable_provider_comment(body):
                continue
            severity = _classify_severity(body)
            path, line = _extract_anchor(body)
            title = body.splitlines()[0] if body else "(no body)"
            findings.append(Finding(
                finding_id=finding_id,
                source="coderabbit",
                severity=severity,
                title=title[:120],
                body=body,
                file_path=path,
                line=line,
                url=c.get("html_url"),
                suggested_test=_extract_suggested_test(body),
                review_id=None,
                comment_id=int(cid) if isinstance(cid, int) else None,
                check_name=None,
            ))
    # Round-35: also collect findings from the durable
    # review_threads inventory. Each unresolved thread
    # that has actionable content (non-empty body or
    # path, OR bound to the current head via
    # ``commit_oid``) IS a real current-head review
    # finding. Without this block, the relay silently
    # missed all 32 inline review comments anchored to
    # bd781d5 because the inline-review pipeline was
    # bound to formal review submissions (which were 0
    # on the current head).
    threads = (
        snapshot.get("review_threads")
        or {}
    )
    current_head = snapshot.get("head_sha")
    # Round-C22/C22: snapshot may carry an ``operator_logins``
    # override; default to the conservative set baked into
    # Round-C22R1/S1: the snapshot's ``operator_logins`` field
    # is normalized through ``_normalize_operator_logins`` so
    # that bare strings, truthy non-iterables, dicts, and other
    # malformed inputs cannot expand into bogus operator
    # identities (the original bug was ``tuple("github-actions")``
    # → ``("g","i","t","h","u","b","-","a","c","t","i","o","n","s")``).
    # The fallback remains the conservative GitHub-Actions default
    # for backward-compatible fixture-only snapshots that omit
    # the field.
    operator_logins: Tuple[str, ...] = _normalize_operator_logins(
        snapshot.get("operator_logins"),
    )
    if isinstance(threads, dict):
        for thread_id, thread_data in threads.items():
            if not isinstance(thread_data, dict):
                continue
            if thread_data.get("resolved"):
                continue
            if thread_data.get("outdated"):
                # Round-C22/C22: do not auto-skip outdated threads.
                # Trial 1B proved an outdated thread can carry a
                # NEW non-operator reviewer reply that re-elevates
                # the finding (its anchor went stale when the head
                # advanced, but the reviewer re-asserted the concern
                # on a newer review). Apply the narrow resurrection
                # helper. If it returns ``None``, the thread has no
                # qualifying follow-up and is skipped (the historical
                # behavior is preserved for the A and B cases).
                resurrected = _maybe_resurrect_outdated_thread(
                    thread_data,
                    current_head=current_head,
                    operator_logins=operator_logins,
                )
                if resurrected is None:
                    continue
                # The finding is built from the QUALIFYING follow-up,
                # not the stale first comment. The thread-level
                # ``path`` / ``line`` are preserved as actionable
                # anchors because GitHub does not re-anchor replies
                # when a thread goes outdated.
                followup = resurrected["followup"]
                followup_db_id_raw = followup.get("id")
                try:
                    followup_db_id = (
                        int(followup_db_id_raw)
                        if followup_db_id_raw not in (None, "")
                        else None
                    )
                except (TypeError, ValueError):
                    followup_db_id = None
                followup_body = str(followup.get("body") or "").strip()
                followup_author = str(followup.get("author") or "")
                followup_created = str(followup.get("createdAt") or "")
                # Provenance prologue: keep the body prefix purely
                # structured so the worker can identify the
                # triggering follow-up without inventing a new
                # Finding schema. The full follow-up body is
                # appended after a blank line so the existing
                # worker heuristics (severity, anchor extraction)
                # operate on the actionable content.
                thread_path = thread_data.get("path") or ""
                thread_line = thread_data.get("line")
                provenance_prologue = (
                    "C22-resurrected follow-up evidence\n"
                    f"thread_id: {thread_id}\n"
                    f"triggering_comment_id: {followup_db_id_raw or ''}\n"
                    f"triggering_author: {followup_author}\n"
                    f"triggering_createdAt: {followup_created}\n"
                    "outdated: true\n"
                    f"current_head: {current_head or ''}\n"
                    f"original_thread_comment_id: {thread_data.get('top_id') or ''}\n"
                    f"thread_path: {thread_path}\n"
                    f"thread_line: {thread_line if thread_line is not None else ''}\n"
                    "\n"
                )
                finding_body = (
                    provenance_prologue + followup_body
                )
                severity = _classify_severity(followup_body)
                title = (
                    followup_body.splitlines()[0]
                    if followup_body
                    else f"(thread {thread_id[-12:]})"
                )
                finding_id = f"thread:{thread_id}"
                if finding_id in seen_ids:
                    continue
                # Round-C22R1/S3: cross-surface dedup. The
                # follow-up's databaseId may have already been
                # emitted via the inline ``review_comments`` or
                # provider issue-comment surface above.
                if (
                    followup_db_id is not None
                    and followup_db_id in seen_comment_ids
                ):
                    continue
                seen_ids.add(finding_id)
                if followup_db_id is not None:
                    seen_comment_ids.add(followup_db_id)
                findings.append(Finding(
                    finding_id=finding_id,
                    source="review_thread",
                    severity=severity,
                    title=title[:120],
                    body=finding_body,
                    file_path=thread_path if thread_path else None,
                    line=(
                        int(thread_line)
                        if isinstance(thread_line, int) else None
                    ),
                    url=None,
                    suggested_test=_extract_suggested_test(followup_body),
                    review_id=None,
                    comment_id=followup_db_id,
                    check_name=None,
                ))
                continue
            thread_body = str(thread_data.get("body") or "").strip()
            thread_path = thread_data.get("path") or ""
            thread_line = thread_data.get("line")
            thread_commit_oid = thread_data.get("commit_oid")
            # Only emit findings from threads that carry
            # real actionable evidence.
            if not thread_body and not thread_path:
                continue
            # Require current-head binding via commit_oid
            # when no other anchor is present. If neither
            # the body nor the path is present, the
            # commit_oid binding is the only signal of
            # current-head applicability.
            if (
                not thread_body
                and not thread_path
                and thread_commit_oid
                and current_head
                and thread_commit_oid != current_head
            ):
                continue
            finding_id = f"thread:{thread_id}"
            if finding_id in seen_ids:
                continue
            # Round-C22R1/S3: cross-surface dedup. The
            # thread's first-comment id (``top_id``) may have
            # already been emitted via the inline
            # ``review_comments`` or provider issue-comment
            # surface above.
            _thread_top_id_raw = thread_data.get("top_id")
            try:
                _thread_top_id = (
                    int(_thread_top_id_raw)
                    if _thread_top_id_raw not in (None, "")
                    else None
                )
            except (TypeError, ValueError):
                _thread_top_id = None
            if (
                _thread_top_id is not None
                and _thread_top_id in seen_comment_ids
            ):
                continue
            seen_ids.add(finding_id)
            if _thread_top_id is not None:
                seen_comment_ids.add(_thread_top_id)
            if not _is_actionable_provider_comment(thread_body):
                # A thread on the current head is by
                # definition actionable even if its
                # body does not contain the standard
                # walkthrough/status markers. Bypass the
                # status-filter so the relay can see
                # genuine inline review findings that
                # were filtered by the prior path-only
                # collection.
                if not thread_path and not thread_commit_oid:
                    continue
            severity = _classify_severity(thread_body)
            title = (
                thread_body.splitlines()[0]
                if thread_body else "(thread)"
            )
            findings.append(Finding(
                finding_id=finding_id,
                source="review_thread",
                severity=severity,
                title=title[:120],
                body=thread_body,
                file_path=(
                    str(thread_path)
                    if thread_path else None
                ),
                line=(
                    int(thread_line)
                    if isinstance(thread_line, int)
                    else None
                ),
                url=None,
                suggested_test=_extract_suggested_test(thread_body),
                review_id=None,
                comment_id=None,
                check_name=None,
            ))
    return findings


def _collect_ci_findings(
    snapshot: dict,
    required_check_names: Tuple[str, ...],
) -> List[Finding]:
    """Extract CI failure findings from ``required_checks``.

    The snapshot stores ``required_checks[name] = {conclusion, status, run_id}``.
    The collector emits one finding per failing required check. CI
    findings are tagged ``CI_FAILURE`` so the directive builder can
    group them with P1 review findings.

    Additional required checks that are absent from the snapshot,
    or whose run is still pending, also emit a finding with
    severity ``CI_FAILURE`` and a body that names the missing
    state. The relay's positive-observation rule requires every
    required check to be EXPLICITLY SUCCESSFUL before the head
    is considered clean; missing or pending evidence is not a
    pass.
    """
    if not isinstance(snapshot, dict):
        raise InvalidSnapshot("snapshot must be a dict")
    findings: List[Finding] = []
    checks = snapshot.get("required_checks") or {}
    if not isinstance(checks, dict):
        checks = {}
    # The required-check positive-observation rule applies
    # ONLY when the operator has named explicit required
    # checks. When ``required_check_names`` is empty, the
    # existing CI-failure scan (failure / failure conclusion /
    # etc.) still emits findings for the failures already
    # present in the snapshot, but the relay does not
    # require checks that the supervisor was not asked to
    # fetch. This preserves the existing contract for
    # callers that do not pass a required-check list.
    for name in required_check_names:
        info = checks.get(name)
        if not isinstance(info, dict):
            # Required check absent from the snapshot — the
            # supervisor did not fetch it. Treat as a CI
            # finding so the relay does not silently call the
            # head clean.
            findings.append(Finding(
                finding_id=f"ci:{name}:missing",
                source="ci",
                severity=SEVERITY_CI_FAILURE,
                title=f"CI required check missing: {name}",
                body=(
                    f"Required CI check {name!r} is absent from the "
                    "snapshot. The supervisor must fetch the check "
                    "before the relay can certify the head."
                ),
                file_path=None,
                line=None,
                url=None,
                suggested_test=None,
                review_id=None,
                comment_id=None,
                check_name=str(name),
            ))
            continue
        conclusion = str(info.get("conclusion") or "").lower()
        status = str(info.get("status") or "").lower()
        # A successful / neutral / skipped conclusion is never
        # a finding. These conclusions all represent a
        # non-actionable terminal state and equate to "the
        # required check produced no work for the operator".
        # NOTE: ``cancelled`` is NOT in the success set. A
        # cancelled check is an UNPLANNED terminal state; the
        # readiness gate does not accept it as a positive
        # observation. The relay surfaces it as a finding so
        # the operator can investigate.
        if conclusion in ("success", "neutral", "skipped"):
            continue
        # A still-running check is not a "failure" finding, but
        # it MUST block the relay from calling the head clean:
        # the existing readiness gate, not the relay, decides
        # whether a pending check is acceptable. The cleanest
        # encoding is a CI failure finding with a clear pending
        # body so the directive tells the operator the check
        # has not yet terminated.
        if conclusion == "" and status not in ("completed", "failure", "failed"):
            findings.append(Finding(
                finding_id=f"ci:{name}:pending",
                source="ci",
                severity=SEVERITY_CI_FAILURE,
                title=f"CI required check pending: {name}",
                body=(
                    f"Required CI check {name!r} is still "
                    f"in-progress (status={status!r}). The relay "
                    "cannot certify the head until the check "
                    "terminates with a positive conclusion."
                ),
                file_path=None,
                line=None,
                url=None,
                suggested_test=None,
                review_id=None,
                comment_id=None,
                check_name=str(name),
            ))
            continue
        # ``conclusion`` is anything other than the accepted
        # terminal states and the run has reached a terminal
        # state. Emit a finding.
        run_id = info.get("run_id")
        findings.append(Finding(
            finding_id=f"ci:{name}:{run_id or 'unknown'}",
            source="ci",
            severity=SEVERITY_CI_FAILURE,
            title=f"CI required check failed: {name}",
            body=(
                f"Required CI check {name!r} failed at run {run_id or 'unknown'}. "
                "Inspect the workflow logs to determine the failing assertion."
            ),
            file_path=None,
            line=None,
            url=None,
            suggested_test=None,
            review_id=None,
            comment_id=None,
            check_name=str(name),
        ))
    # NOTE: non-required check failures are NOT surfaced as
    # repair findings. The relay drives the operator's
    # authoritative required-check list; non-required
    # failures are surfaced through the existing readiness
    # gate, not through the repair loop. Surfacing them
    # here would create permanent repair findings on
    # irrelevant CI fluctuations.
    return findings


def collect_findings(
    snapshot: dict,
    *,
    required_check_names: Tuple[str, ...] = (),
    ledger: Optional["FindingLedger"] = None,
    focused_thread_id: Optional[str] = None,
) -> List[Finding]:
    """Collect all actionable findings from a snapshot.

    The function is the single entry point for evidence ingestion.
    It returns a sorted list of ``Finding`` objects:

    - P0_ESCALATE findings first (these halt the relay);
    - then P1 (review findings + CI failures);
    - then P2.

    The collector never raises on empty snapshots — empty input
    produces an empty list. Invalid snapshot shape raises
    ``InvalidSnapshot`` so the caller can ``BLOCK`` the run.

    If ``ledger`` is supplied, the result is filtered through the
    current-head rule: a finding already observed on the current
    head with the same content is suppressed. The pre-filter
    count is unchanged from the caller's perspective because
    the filter is internal; callers who need the full set can
    pass ``ledger=None`` and filter separately via
    ``filter_findings_to_current_head``.

    Round-45 C13 (round-590 correction): ``focused_thread_id``
    scopes the REVIEW findings portion of the directive to a
    SINGLE targeted review thread. It MUST NOT suppress CI
    failure findings. The focused-mode collector:

    1. Collects the targeted current-head review thread
       (``finding_id == "thread:<focused_thread_id>"``) or
       emits no review finding if the thread is resolved /
       outdated / not bound to the current head.
    2. Independently calls ``_collect_ci_findings`` against the
       same snapshot so every terminal failed required check
       becomes a ``CI_FAILURE`` finding, including when
       ``focused_thread_id`` is set.
    3. Successful / skipped / neutral required checks produce
       no CI failure; pending required checks are still
       surfaced as a CI failure (unified policy; see round-281
       C22 §6).
    4. Unrelated historical review findings remain excluded;
       the focused mode only adds the targeted current-head
       thread (zero or one) to the union.
    5. Truncation by ``max_findings`` is never permitted to
       drop a CI_FAILURE finding — the ``max_findings`` cap
       is applied AFTER the union below, in the directive
       builder, where it already preserves CI failures
       (round-281 C22 §6).

    The historical 8-P1 backlog is therefore still excluded
    when ``focused_thread_id`` is supplied, while required CI
    failures continue to drive the directive through the
    empirical-gate reader path.
    """
    review_findings: List[Finding] = []
    ci_findings: List[Finding] = list(
        _collect_ci_findings(snapshot, required_check_names)
    )
    if focused_thread_id is not None:
        threads = snapshot.get("review_threads") or {}
        thread_data = (
            threads.get(focused_thread_id)
            if isinstance(threads, dict)
            else None
        )
        current_head = snapshot.get("head_sha")
        # Round-C22/C22: resolved threads still emit no finding
        # (a closed thread keeps no actionable evidence). For
        # outdated threads, apply the same narrow resurrection rule
        # as the unfocused collector; the helper is shared so the
        # two paths cannot drift on eligibility semantics.
        # Round-C22R1/S1: same normalizer as the unfocused
        # collector so a string-typed operator_logins snapshot
        # value cannot explode into bogus single-character
        # identities.
        operator_logins_focused: Tuple[str, ...] = (
            _normalize_operator_logins(snapshot.get("operator_logins"))
        )
        if isinstance(thread_data, dict):
            if not thread_data.get("resolved"):
                # Resurrected branch: outdated + qualifying review
                # follow-up. Build the finding from the follow-up,
                # NOT the stale first comment. Same provenance
                # prologue as the unfocused collector.
                if thread_data.get("outdated"):
                    resurrected = _maybe_resurrect_outdated_thread(
                        thread_data,
                        current_head=current_head,
                        operator_logins=operator_logins_focused,
                    )
                    if resurrected is not None:
                        followup = resurrected["followup"]
                        followup_db_id_raw = followup.get("id")
                        try:
                            followup_db_id = (
                                int(followup_db_id_raw)
                                if followup_db_id_raw not in (None, "")
                                else None
                            )
                        except (TypeError, ValueError):
                            followup_db_id = None
                        followup_body = str(
                            followup.get("body") or ""
                        ).strip()
                        followup_author = str(
                            followup.get("author") or ""
                        )
                        followup_created = str(
                            followup.get("createdAt") or ""
                        )
                        thread_path = thread_data.get("path") or ""
                        thread_line = thread_data.get("line")
                        provenance_prologue = (
                            "C22-resurrected follow-up evidence\n"
                            f"thread_id: {focused_thread_id}\n"
                            f"triggering_comment_id: {followup_db_id_raw or ''}\n"
                            f"triggering_author: {followup_author}\n"
                            f"triggering_createdAt: {followup_created}\n"
                            "outdated: true\n"
                            f"current_head: {current_head or ''}\n"
                            f"original_thread_comment_id: {thread_data.get('top_id') or ''}\n"
                            f"thread_path: {thread_path}\n"
                            f"thread_line: {thread_line if thread_line is not None else ''}\n"
                            "\n"
                        )
                        finding_body = (
                            provenance_prologue + followup_body
                        )
                        severity = _classify_severity(followup_body)
                        title = (
                            followup_body.splitlines()[0]
                            if followup_body
                            else f"(thread {focused_thread_id[-12:]})"
                        )
                        review_findings.append(Finding(
                            finding_id=f"thread:{focused_thread_id}",
                            source="review_thread",
                            severity=severity,
                            title=title[:120],
                            body=finding_body,
                            file_path=thread_path if thread_path else None,
                            line=(
                                int(thread_line)
                                if isinstance(thread_line, int) else None
                            ),
                            url=None,
                            suggested_test=_extract_suggested_test(
                                followup_body
                            ),
                            review_id=None,
                            comment_id=followup_db_id,
                            check_name=None,
                        ))
                else:
                    # Current-head path: unchanged behavior. The
                    # historical exact-head guard via ``commit_oid``
                    # is preserved byte-for-byte.
                    thread_body = str(
                        thread_data.get("body") or ""
                    ).strip()
                    thread_path = thread_data.get("path") or ""
                    thread_line = thread_data.get("line")
                    thread_commit_oid = thread_data.get("commit_oid")
                    if (
                        not thread_body
                        and not thread_path
                        and thread_commit_oid
                        and current_head
                        and thread_commit_oid != current_head
                    ):
                        pass
                    else:
                        severity = _classify_severity(thread_body)
                        title = (
                            thread_body.splitlines()[0]
                            if thread_body else f"(thread {focused_thread_id[-12:]})"
                        )
                        review_findings.append(Finding(
                            finding_id=f"thread:{focused_thread_id}",
                            source="review_thread",
                            severity=severity,
                            title=title[:120],
                            body=thread_body,
                            file_path=thread_path if thread_path else None,
                            line=int(thread_line) if isinstance(thread_line, int) else None,
                            url=None,
                            suggested_test=None,
                            review_id=None,
                            comment_id=None,
                            check_name=None,
                        ))
    else:
        review_findings = list(_collect_review_findings(snapshot))
    findings: List[Finding] = list(review_findings) + list(ci_findings)
    if ledger is not None:
        findings = filter_findings_to_current_head(findings, ledger)
    order = {
        SEVERITY_P0_ESCALATE: 0,
        SEVERITY_P1: 1,
        SEVERITY_CI_FAILURE: 1,
        SEVERITY_P2: 2,
    }
    findings.sort(key=lambda f: (order.get(f.severity, 99), f.finding_id))
    return findings


# === Directive builder ===

def build_directive(
    *,
    round_index: int,
    head_sha: str,
    repo: str,
    pr_number: int,
    findings: List[Finding],
    coordinator_actor: str,
    max_findings: Optional[int] = None,
    target_thread_id: Optional[str] = None,
) -> ReviewDirective:
    """Build a structured repair directive from a list of findings.

    The directive body is the entire spec for the round. The
    builder refuses to produce a directive if:

    - any finding has ``P0_ESCALATE`` severity (raises ``EscalateToHuman``);
    - any finding body contains an escalation keyword (raises
      ``EscalateToHuman``).

    The escalation keyword check is the lexical guard that
    prevents the autonomous path from issuing destructive
    instructions that an experienced reviewer would never sign
    off on.

    Round-35: ``max_findings`` caps the per-directive
    payload so a worker is not overwhelmed by 76+
    historical threads. The relay emits ONE finding per
    round; the remainder persist on the durable thread
    inventory and surface on subsequent rounds after the
    current head advances.

    Round-591: a CI_FAILURE finding IS a mandatory
    observation surface for the worker — the worker MUST
    inspect, repair, or evidence-handle every failed
    required check. Truncation of CI_FAILURE findings
    is therefore FORBIDDEN. The cap applies only to the
    per-directive REVIEW workload (P1 + P2); CI_FAILURE
    findings survive any cap and are inserted in their
    own block before P1/P2 in the directive. Promotion
    of CI_FAILURE -> dropped is itself an
    availability-defect.
    """
    if not findings:
        raise DirectiveContractError("build_directive requires at least one finding")
    # Round-35: cap findings per directive.
    # Round-591: split into four partitions:
    #   ci_failures  (mandatory; never truncated)
    #   p0           (mandatory; never truncated; always escalates)
    #   p1           (review work; truncated if cap hit)
    #   p2           (review work; truncated if cap hit)
    # Round-665: P0_ESCALATE must be extracted BEFORE the
    # ``max_findings`` cap is applied. The previous
    # three-way partition removed P0 from ``p2`` (it
    # excluded ``P0_ESCALATE``) and then replaced ``review``
    # with the capped ``p1 + p2`` list, so any P0 escalation
    # in a long finding list silently vanished before the
    # severity check below ran. A repair directive was then
    # launched instead of the required human escalation.
    ci_failures = [
        f for f in findings if f.severity == SEVERITY_CI_FAILURE
    ]
    p0 = [f for f in findings if f.severity == SEVERITY_P0_ESCALATE]
    review = [
        f for f in findings
        if f.severity not in (SEVERITY_CI_FAILURE, SEVERITY_P0_ESCALATE)
    ]
    if max_findings is not None and len(review) > max_findings:
        # Only the REVIEW partition (P1 + P2) is capped.
        # CI_FAILURE and P0_ESCALATE are mandatory
        # observations / escalations and never dropped
        # regardless of the cap.
        p1 = [f for f in review if f.severity == SEVERITY_P1]
        p2 = [f for f in review if f.severity == SEVERITY_P2]
        review = (p1 + p2)[:max_findings]
    findings = list(ci_failures) + list(p0) + list(review)
    if p0:
        raise EscalateToHuman(
            f"round {round_index}: {len(p0)} P0 finding(s) require human review: "
            + "; ".join(f.title for f in p0[:3])
        )
    for f in findings:
        body_lower = f.body.lower()
        for kw in _ESCALATION_KEYWORDS:
            if kw in body_lower:
                raise EscalateToHuman(
                    f"round {round_index}: finding {f.finding_id} contains "
                    f"escalation keyword {kw!r}; refusing to build directive"
                )
    summary = _summarize(findings)
    return ReviewDirective(
        schema_version=RELAY_SCHEMA_VERSION,
        directive_id=str(uuid.uuid4()),
        round_index=round_index,
        head_sha=head_sha,
        repo=repo,
        pr_number=pr_number,
        created_at=_now_iso(),
        findings=tuple(findings),
        summary=summary,
        coordinator_actor=coordinator_actor,
        target_thread_id=target_thread_id,
    )


def _summarize(findings: List[Finding]) -> str:
    p1 = sum(1 for f in findings if f.severity == SEVERITY_P1)
    p2 = sum(1 for f in findings if f.severity == SEVERITY_P2)
    ci = sum(1 for f in findings if f.severity == SEVERITY_CI_FAILURE)
    return f"{len(findings)} findings: P1={p1}, P2={p2}, CI_FAIL={ci}"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# === Directive persistence ===

class DirectiveStore:
    """Persist the latest directive and the round transcript journal.

    The store is intentionally minimal: it writes the directive
    via the canonical artifact writer (so a digest sidecar is
    always produced) and appends to a JSONL transcript under the
    state root. The two writers are kept separate so the
    directive can be re-read independently of the journal.
    """

    def __init__(self, store: StateStore, evidence_root: str) -> None:
        self.store = store
        self.evidence_root = evidence_root

    def write_directive(self, directive: ReviewDirective) -> str:
        """Persist the directive as a canonical artifact.

        Returns the SHA-256 digest of the canonical artifact. The
        digest is the binding id the worker/verifier can use to
        refer to the directive.
        """
        from pathlib import Path
        target = Path(self.evidence_root) / "directive.json"
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifact = directive.to_dict()
        artifact["_sha256"] = directive.compute_sha256()
        result = write_artifact(target, artifact)
        return result.digest

    def read_directive(self) -> Optional[ReviewDirective]:
        from pathlib import Path
        target = Path(self.evidence_root) / "directive.json"
        try:
            result = read_artifact(target)
        except (ArtifactError, FileNotFoundError, OSError):
            return None
        try:
            return ReviewDirective.from_dict(result.payload)
        except DirectiveContractError:
            return None

    def append_transcript(self, transcript: RoundTranscript) -> None:
        """Append a transcript entry to the round journal."""
        self.store.append_journal("relay.jsonl", transcript.to_dict())

    def read_transcript(self) -> List[RoundTranscript]:
        out: List[RoundTranscript] = []
        for entry in self.store.read_journal("relay.jsonl"):
            try:
                out.append(RoundTranscript.from_dict(entry))
            except DirectiveContractError:
                continue
        return out

    def last_round_index(self) -> int:
        entries = self.read_transcript()
        if not entries:
            return -1
        return max(e.round_index for e in entries)


# === Head-equality helper ===

def heads_equal(a: Optional[str], b: Optional[str]) -> bool:
    """Return True iff ``a`` and ``b`` are the same 40/64-char SHA."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return a.lower() == b.lower()


# === State machine bridge ===

def relay_state_for_outcome(outcome: str) -> Tuple[str, str]:
    """Map a relay outcome to the canonical state-machine transition.

    The mapping is conservative — the relay never decides a
    transition on its own; it asks the controller to apply the
    transition through its existing safe-order API
    (``record_evidence`` / ``report_repair_pushed`` / ``record_readiness_certificate``).

    Returns ``(desired_state, actor)``.
    """
    if outcome == "completed":
        return STATE_AWAITING_CI, ACTOR_IMPL_WORKER
    if outcome == "ready":
        return STATE_QUALIFYING_READINESS, ACTOR_CONTROLLER
    return STATE_REPAIRING_REVIEW_FINDINGS, ACTOR_CONTROLLER


# === Loop driver ===

@dataclass(frozen=True)
class RoundDecision:
    """The deterministic output of one bounded round.

    The decision is the single contract the relay exposes to its
    caller. The caller (CLI / daemon) is responsible for:

    - if ``action == "launch_worker"``: invoking the supervisor's
      ``launch_worker`` with the directive's prompt;
    - if ``action == "enter_qualifying_readiness"``: invoking the
      controller's ``record_readiness_certificate``;
    - if ``action == "escalate_to_human"``: invoking the
      controller's ``block`` so the operator has a single halt
      point.

    The decision is a pure value object — no I/O, no subprocess,
    no state mutation.
    """

    action: str  # "launch_worker" | "enter_qualifying_readiness" | "escalate_to_human" | "await_head_change"
    round_index: int
    head_sha: str
    outcome: str  # "completed" | "ready" | "escalated" | "no_findings"
    p1_count: int
    p2_count: int
    ci_failure_count: int
    escalate_reasons: Tuple[str, ...]
    directive: Optional[ReviewDirective]
    directive_digest: Optional[str]

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "round_index": self.round_index,
            "head_sha": self.head_sha,
            "outcome": self.outcome,
            "p1_count": self.p1_count,
            "p2_count": self.p2_count,
            "ci_failure_count": self.ci_failure_count,
            "escalate_reasons": list(self.escalate_reasons),
            "directive": self.directive.to_dict() if self.directive else None,
            "directive_digest": self.directive_digest,
        }


def evaluate_round(
    *,
    snapshot: dict,
    head_sha: str,
    repo: str,
    pr_number: int,
    round_index: int,
    required_check_names: Tuple[str, ...] = (),
    coordinator_actor: str = ACTOR_CONTROLLER,
    directive_store: Optional[DirectiveStore] = None,
    finding_ledger: Optional[FindingLedger] = None,
    focused_thread_id: Optional[str] = None,
) -> RoundDecision:
    """Run one bounded round of evidence collection and classification.

    The function is the SINGLE entry point for the relay's
    decision logic. It:

    1. Validates the snapshot shape and head_sha. The
       snapshot's recorded ``head_sha`` and ``head_match`` are
       verified against the requested head — stale snapshots
       from a previous head are refused so the relay cannot
       issue current-head directives from old review evidence.
    2. Collects findings via ``collect_findings``.
    3. If no findings: returns ``action="enter_qualifying_readiness"``
       so the existing readiness gate can certify the head.
    4. If findings: attempts to build a directive. On
       ``EscalateToHuman`` returns ``action="escalate_to_human"``;
       on success persists the directive (if a ``directive_store``
       is supplied) and returns ``action="launch_worker"``.

    The function never launches a worker, never invokes GitHub,
    and never mutates the state machine. It is the
    deterministic half of the relay; the imperative half is the
    CLI / daemon that calls into the supervisor's existing
    ``launch_worker`` and the controller's existing transitions.
    """
    if not isinstance(snapshot, dict):
        raise InvalidSnapshot("snapshot must be a dict")
    if not isinstance(head_sha, str) or not _HEX_SHA_RE.match(head_sha):
        raise DirectiveContractError(
            f"head_sha must be 40 or 64 lowercase hex chars: {head_sha!r}"
        )
    # The snapshot MUST be bound to the requested head. A
    # stale snapshot from a previous head (or a snapshot whose
    # capture did not match the live head) MUST be rejected;
    # the relay cannot rely on review evidence that does not
    # correspond to the exact head it is acting on.
    snapshot_head = snapshot.get("head_sha")
    if snapshot_head is None:
        # Missing head metadata. The exact-head guard
        # requires a concrete SHA; a snapshot without
        # one cannot be verified. Reject as InvalidSnapshot.
        raise InvalidSnapshot(
            "snapshot is missing head_sha; the relay cannot "
            "verify the exact-head guard without it. The "
            "snapshot MUST be re-captured."
        )
    if snapshot_head != head_sha:
        raise InvalidSnapshot(
            f"snapshot head {snapshot_head!r} != requested head {head_sha!r}; "
            "stale snapshot rejected"
        )
    # Round-29 review P6: ``head_match`` MUST be
    # exactly the literal ``True`` value. ``None``, ``0``,
    # empty strings, ``"True"`` (string), or any other
    # non-True value MUST fail closed. The exact-head
    # evidence is a strict positive observation; absent
    # evidence is not a pass, and partial evidence is
    # not a pass.
    head_match = snapshot.get("head_match")
    if head_match is not True:
        raise InvalidSnapshot(
            f"snapshot head_match={head_match!r} is not exactly True; "
            "the exact-head evidence is a strict positive observation. "
            "None / 0 / string / missing values MUST fail closed. "
            f"requested head: {head_sha!r}"
        )
    findings = collect_findings(
        snapshot,
        required_check_names=required_check_names,
        ledger=finding_ledger,
        focused_thread_id=focused_thread_id,
    )
    # Round-30: if the snapshot's provider-surface
    # collection failed (``provider_surface_complete``
    # is False) the evidence is INCOMPLETE — the relay
    # MUST refuse to enter qualifying-readiness.
    # ``enter_qualifying_readiness`` is the protected
    # path that the readiness gate certifies; incomplete
    # evidence cannot be certified. The relay returns
    # ``await_head_change`` so the supervisor / scheduler
    # continues polling / retrying rather than
    # misclassifying incomplete evidence as a clean head.
    if not snapshot.get("provider_surface_complete", True):
        return RoundDecision(
            action="await_head_change",
            round_index=round_index,
            head_sha=head_sha,
            outcome="incomplete_evidence",
            p1_count=0,
            p2_count=0,
            ci_failure_count=0,
            escalate_reasons=(
                "provider_surface_complete=false; "
                "evidence incomplete; awaiting fresh surfaces",
            ),
            directive=None,
            directive_digest=None,
        )
    if not findings:
        return RoundDecision(
            action="enter_qualifying_readiness",
            round_index=round_index,
            head_sha=head_sha,
            outcome="ready",
            p1_count=0,
            p2_count=0,
            ci_failure_count=0,
            escalate_reasons=(),
            directive=None,
            directive_digest=None,
        )
    p1 = sum(1 for f in findings if f.severity == SEVERITY_P1)
    p2 = sum(1 for f in findings if f.severity == SEVERITY_P2)
    ci = sum(1 for f in findings if f.severity == SEVERITY_CI_FAILURE)
    # Round-684/P1: bind the targeted thread identity into
    # the directive ONLY when its review finding actually
    # survived ``collect_findings``. When the focused
    # thread has been resolved, marked outdated, or
    # disappeared from the live snapshot before this round
    # ran, ``collect_findings`` correctly emits no
    # ``thread:<id>`` finding while still surfacing any
    # CI failures. Passing ``target_thread_id`` in that
    # case would trip ``ReviewDirective.__post_init__``'s
    # contract guard (the targeted ``thread:<id>`` finding
    # is required when ``target_thread_id`` is set), so
    # the directive would fail to build with
    # ``DirectiveContractError`` even though the surviving
    # CI findings are perfectly actionable. Bind the
    # target only when its finding survives.
    bound_target_thread_id: Optional[str] = None
    if focused_thread_id is not None:
        expected_finding_id = f"thread:{focused_thread_id}"
        if any(f.finding_id == expected_finding_id for f in findings):
            bound_target_thread_id = focused_thread_id
    try:
        directive = build_directive(
            round_index=round_index,
            head_sha=head_sha,
            repo=repo,
            pr_number=pr_number,
            findings=findings,
            coordinator_actor=coordinator_actor,
            # Round-676/P2: carry the focused-thread
            # identity into the persisted directive so the
            # JSON and bridge-rendered worker prompt
            # identify the round as activated/thread-scoped
            # rather than broad. ``focused_thread_id`` is
            # already in scope from the bound round entry;
            # passing it preserves the supervisor's scope.
            #
            # Round-684/P1: only when the targeted thread
            # finding survived the snapshot filter. When the
            # thread has been drained and only CI failures
            # remain, leave ``target_thread_id=None`` so the
            # directive contract permits the CI-only payload.
            target_thread_id=bound_target_thread_id,
            # Round-35: cap findings per directive so a
            # worker is not overwhelmed by 76+ historical
            # threads in a single prompt. Subsequent
            # rounds handle the remainder after the
            # current head advances.
            max_findings=8,
        )
    except EscalateToHuman as exc:
        return RoundDecision(
            action="escalate_to_human",
            round_index=round_index,
            head_sha=head_sha,
            outcome="escalated",
            p1_count=p1,
            p2_count=p2,
            ci_failure_count=ci,
            escalate_reasons=(str(exc),),
            directive=None,
            directive_digest=None,
        )
    digest: Optional[str] = None
    if directive_store is not None:
        digest = directive_store.write_directive(directive)
    return RoundDecision(
        action="launch_worker",
        round_index=round_index,
        head_sha=head_sha,
        outcome="completed",
        p1_count=p1,
        p2_count=p2,
        ci_failure_count=ci,
        escalate_reasons=(),
        directive=directive,
        directive_digest=digest,
    )


# === Worker prompt construction ===

# Template for the worker prompt. The placeholder
# ``{directive_json}`` is substituted with the canonical JSON
# serialization of the directive. The worker (Humphry) reads the
# prompt directly and does NOT need to re-query CodeRabbit / CI.
WORKER_PROMPT_TEMPLATE = (
    "[AED-AUTOCODER REPAIR DIRECTIVE — round {round_index}] "
    "You are operating in the autonomous review/repair relay (v1) for "
    "PR {pr_number} ({repo}).\n\n"
    "Authoritative head: {head_sha}\n"
    "Directive ID: {directive_id}\n"
    "Directive SHA-256: {directive_sha256}\n"
    "Target thread: {target_thread_id}\n\n"
    "The relay has already collected exact-head CI and CodeRabbit evidence. Your "
    "job is to apply every P1 finding and the required CI failures. P2 findings "
    "are preferred but not blocking — apply them when straightforward (clean "
    "fix, no architectural change). The directive below is the authoritative "
    "spec for this round — do not re-query the review surface; the relay has "
    "already done so.\n\n"
    "Directive summary: {summary}\n\n"
    "```json\n"
    "{directive_json}\n"
    "```\n\n"
    "Apply every P1 finding. Apply each P2 finding if the fix is "
    "straightforward (single-line, obvious, no behavior change). Verify the "
    "repair by running the focused test suite "
    "and the CI gate. Commit and push. Do NOT amend history. Do NOT force-push. "
    "Do NOT merge. The relay will detect the new head automatically and run the "
    "next round or transition to qualification.\n\n"
    "Standing authorization is already recorded in run_state.json. Stop only when "
    "the relay signals 'enter_qualifying_readiness' or 'escalate_to_human' via "
    "the next round decision.\n\n"
    "ROUND-39 NO-OP CONTRACT (Section 4):\n"
    "Before creating any commit you MUST classify every finding in this directive "
    "as one of:\n"
    "  A. REAL_REPAIR_REQUIRED  - the defect is genuinely present at the current "
    "     exact head and a source edit is required to repair it.\n"
    "  B. ALREADY_SATISFIED    - the defect is genuinely present at the current "
    "     exact head but has already been fixed by an earlier commit on this "
    "     branch (or by a prior round's worker). Inspect the current "
    "     exact-head code; if the relevant code path already implements the "
    "     required behavior, this finding is ALREADY_SATISFIED.\n"
    "  C. SUPERSEDED          - the finding references a path/line/comment that "
    "     no longer exists at the current exact head or that has been "
    "     superseded by a later change. The finding cannot be repaired "
    "     because its target is gone.\n"
    "  D. INSUFFICIENT_EVIDENCE - you cannot determine the disposition with "
    "     the available evidence. Do not fabricate a fix.\n"
    "You MUST answer explicitly, per finding:\n"
    "  WHAT CURRENT DEFECT DOES THIS DIFF REPAIR?\n"
    "If the answer is none (categories B / C / D for every finding), you MUST "
    "NOT create a commit, you MUST NOT push, you MUST NOT modify the repository. "
    "Instead, write a durable worker attempt result that records the disposition "
    "of each finding, exit without changes, and let the supervisor reconcile.\n"
    "Successful work does not require creating a Git commit. The defining "
    "round-39 success signal is that AutoDev stops committing when there is "
    "nothing left to commit. A 'verify all P1 findings are still intact' "
    "commit with no new source edit is NOT convergence and IS a regression: "
    "it changes the head, invalidates exact-head evidence, resets the quiet "
    "window, can trigger provider auto-pause, and creates infinite churn.\n"
    "ROUND-45 C13 SCOPING: when ``Target thread`` is non-empty, the directive is "
    "scoped to that specific review thread. Read the targeted thread's body, "
    "inspect the relevant file/line, classify that exact thread with one of "
    "the dispositions above, and (if REAL_REPAIR_REQUIRED) commit a focused "
    "fix. Do not re-audit the historical 8-P1 backlog unless the targeted "
    "thread references one of those findings; this directive is intentionally "
    "small so the worker can spend its tool budget on the targeted "
    "investigation rather than re-proving already-classified historical "
    "findings. If the targeted thread is not actionable (B / C / D), "
    "persist the per-finding disposition and exit without changes.\n"
)


def build_worker_prompt(decision: RoundDecision) -> str:
    """Render the canonical worker prompt for a round decision.

    The prompt is deterministic for a given ``RoundDecision`` so
    the worker can be restarted against the same directive
    without producing a different prompt. The directive JSON is
    pretty-printed for readability.
    """
    directive = decision.directive
    if directive is None:
        raise DirectiveContractError(
            "build_worker_prompt requires a decision with a directive"
        )
    payload = json.dumps(
        directive.to_dict(), indent=2, sort_keys=True,
    )
    return WORKER_PROMPT_TEMPLATE.format(
        round_index=decision.round_index,
        pr_number=directive.pr_number,
        repo=directive.repo,
        head_sha=directive.head_sha,
        directive_id=directive.directive_id,
        directive_sha256=directive.compute_sha256(),
        summary=directive.summary,
        directive_json=payload,
        target_thread_id=directive.target_thread_id or "(none — broad directive)",
    )


# === Loop controller ===

class RelayLoop:
    """Persistent loop controller for the review/repair relay.

    The loop is the orchestration layer that drives a single
    PR through review → repair → push → CI → review → repair
    until the head is clean. It REUSES the existing
    ``StateStore`` / ``Controller`` / ``Lease`` primitives; every
    state mutation is delegated to the controller; the loop
    only owns the round transcript journal and the persistent
    wait-for-head-change step.

    Design contract
    ---------------

    The relay is **persistent**: it has no bake-in 10-round
    halt. Repairable, autonomous findings (P1 / P2 / CI failures
    that are not protected-authority) drive the relay forward
    through as many rounds as necessary. The ONLY halts are:

    1. **Protected-authority escalation** — P0 findings, body
       strings containing escalation keywords (``force push``,
       ``delete branch``, ``merge pr``, ``bypass guard``, etc.),
       head mismatch (invariant I-08), or directive carrying
       body text the relay cannot safely authorize. The
       controller is driven into BLOCKED and the operator
       must inspect.

    2. **Required-head-resolution boundary** — the head is
       clean; the controller is driven into
       ``QUALIFYING_READINESS`` so the existing readiness gate
       can certify the head. The relay does NOT place the
       run into ``AWAITING_MERGE_AUTHORIZATION``; the
       existing human merge-authorization boundary is
       untouched.

    The ``max_rounds`` parameter is a SAFETY NET only,
       intended to catch runaway case (e.g. the directive
       builder returning P1 findings faster than the worker
       can repair them). It does NOT halt on the first
       reachable round; it halts only after the configured
       number of identical-head rounds without any change.
       The default is disabled (``max_rounds=None``).

    Single-shot vs persistent
    -------------------------

    The CLI invokes ``loop.run_once()`` for one round. The
    supervisor's persistent event loop invokes
    ``loop.run_until_head_advances()`` to drive the relay
    through as many rounds as the head advances. The supervisor
    is the right place to call the loop driver because the
    supervisor already owns the heartbeat, the lease, the
    cooldown, and the head-mismatch detection.
    """

    def __init__(
        self,
        *,
        context: Any,
        store: StateStore,
        directive_store: DirectiveStore,
        controller: Any,
        required_check_names: Tuple[str, ...] = (),
        max_rounds: Optional[int] = None,
        identity: Optional[ProcessIdentity] = None,
    ) -> None:
        self.context = context
        self.store = store
        self.directive_store = directive_store
        self.controller = controller
        self.required_check_names = tuple(required_check_names)
        # Round-30: ``max_rounds`` is an execution-scheduling
        # boundary, NOT a protected-authority escalation. The
        # default is disabled (``None``); the persistent
        # supervisor / scheduler is the canonical retry owner
        # and does not need this bound.
        self.max_rounds = (
            int(max_rounds) if max_rounds is not None else None
        )
        self.identity = identity or current_process_identity()

    def _persist_round_budget(
        self,
        *,
        head_sha: str,
        rounds: int,
        rounds_at_head: int,
        max_rounds: int,
        reason: str = "round_budget_reached",
        slice_epoch: int = 1,
    ) -> None:
        """Round-30/31: persist the recoverable retry state.

        The supervisor / scheduler reads this on the next
        slice to resume the SAME outstanding work. The
        relay MUST NOT call ``controller.block`` (that would
        be a protected-authority escalation). The persistent
        state is durable and idempotent; duplicate retries
        are safe.

        Round-31: per-head retry ledger with bounded
        exponential backoff. Fields:

            head_sha: the head the retry applies to.
            reason: the recoverable failure class.
            attempt_count: total retry attempts so far.
            first_failure_at: ISO timestamp of the first
                attempt that produced this ledger entry.
            last_attempt_at: ISO timestamp of the most
                recent attempt.
            next_eligible_retry_at: ISO timestamp; the
                supervisor MUST NOT retry before this.
            slice_epoch: monotonic counter; the supervisor
                increments it on each new execution slice
                so a fresh slice gets a fresh budget while
                preserving the unresolved findings.

        Most importantly: when ``historical
        completed_round_count >= budget`` on every new
        invocation, the supervisor MUST NOT loop forever
        with no new work possible. The slice epoch
        increment + a fresh budget per slice prevents
        this permanent failure mode.
        """
        # Read the existing ledger to merge the attempt
        # counter (do NOT lose the prior count on rewrite).
        retry_path = (
            Path(self.directive_store.evidence_root)
            / "round_budget_retry.json"
        )
        prior_count = 0
        prior_first_failure_at = _now_iso()
        try:
            if retry_path.is_file():
                prior = json.loads(retry_path.read_text())
                if prior.get("head_sha") == head_sha:
                    prior_count = int(prior.get("attempt_count", 0))
                    prior_first_failure_at = prior.get(
                        "first_failure_at",
                        prior.get("recorded_at", _now_iso()),
                    )
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        now = _now_iso()
        attempt_count = prior_count + 1
        # Bounded exponential backoff: 30s, 60s, 120s,
        # 240s, capped at 600s. The supervisor reads
        # ``next_eligible_retry_at`` to schedule the next
        # retry.
        backoff_seconds = min(
            30 * (2 ** min(attempt_count - 1, 5)),
            600,
        )
        from datetime import datetime, timedelta, timezone
        try:
            now_dt = datetime.fromisoformat(
                now.replace("Z", "+00:00"),
            )
            next_eligible_dt = now_dt + timedelta(
                seconds=backoff_seconds,
            )
            next_eligible_retry_at = (
                next_eligible_dt.isoformat()
            )
        except Exception:  # noqa: BLE001
            next_eligible_retry_at = now
        payload = {
            "head_sha": head_sha,
            "reason": reason,
            "attempt_count": attempt_count,
            "first_failure_at": prior_first_failure_at,
            "last_attempt_at": now,
            "next_eligible_retry_at": next_eligible_retry_at,
            "slice_epoch": slice_epoch,
            "rounds": rounds,
            "rounds_at_head": rounds_at_head,
            "max_rounds": max_rounds,
            "recorded_at": now,
            "owner": "relay_recovery",
            "recoverable": True,
        }
        try:
            retry_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: write to a temp file then
            # rename so a crash mid-write cannot leave a
            # half-truncated ledger.
            tmp_path = retry_path.with_suffix(
                retry_path.suffix + ".tmp",
            )
            tmp_path.write_text(
                json.dumps(payload, sort_keys=True),
            )
            tmp_path.replace(retry_path)
        except OSError:
            # Persistence failure: log the error and
            # continue; the recovery signal is still raised.
            # The relay module does not bind a module-level
            # logger; use the stdlib ``logging`` module so
            # the failure is observable in operator logs
            # while remaining best-effort (any logging
            # failure is swallowed).
            try:
                logging.getLogger(__name__).error(
                    "round-budget retry state persistence "
                    "failed; slice ended, supervisor will "
                    "retry on resume",
                )
            except Exception:  # noqa: BLE001 - defensive
                pass

    def run_once(
        self,
        snapshot: dict,
        head_sha: str,
        *,
        repo: str,
        pr_number: int,
        focused_thread_id: Optional[str] = None,
    ) -> RoundDecision:
        """Run one round.

        The method appends a ``RoundTranscript`` to the journal
        regardless of the outcome so a restart can recover the
        last-known position. The transcript is the only durable
        record of relay progress; the in-memory state of the
        controller is the source of truth for the current state
        machine position.

        The `started_at` timestamp is captured BEFORE
        ``evaluate_round`` runs so it reflects when the round
        began, not when it ended.

        Round-30: the ``max_rounds`` parameter is an
        EXECUTION SCHEDULING BOUNDARY, NOT a protected-authority
        escalation. When the same head produces more than
        ``max_rounds`` directive launches, the relay MUST
        persist the outstanding work + retry state and end
        the current slice. The persistent supervisor /
        scheduler automatically resumes the SAME outstanding
        work on the next slice. Ordinary unresolved bugs do
        NOT become protected authority merely because they
        survived N rounds.
        """
        round_index = self.directive_store.last_round_index() + 1
        # Round-32: read the current slice_epoch from the
        # durable retry ledger so the budget is scoped
        # per slice (not cumulative across slices). Bound
        # at the top of the function so the per-transcript
        # stamp below can reference it.
        retry_state = read_round_budget_retry(
            self.directive_store.evidence_root,
        ) or {}
        current_slice_epoch = int(retry_state.get("slice_epoch", 0))
        if self.max_rounds is not None:
            prior = self.directive_store.read_transcript()
            same_head_consecutive = sum(
                1 for t in prior
                if t.head_sha_before == head_sha
                and t.outcome == "completed"
                and int(getattr(t, "slice_epoch", 0)) == current_slice_epoch
            )
            if same_head_consecutive >= self.max_rounds:
                # Round-32: persist the outstanding work
                # under the CURRENT slice_epoch and signal
                # a recoverable retry. The supervisor's
                # next-slice start (``bump_slice_epoch``)
                # increments the epoch and gives the next
                # slice a fresh budget; prior-slice
                # transcripts no longer count. This
                # prevents the permanent-failure mode
                # where historical completed_round_count >=
                # budget blocks all future work.
                self._persist_round_budget(
                    head_sha=head_sha,
                    rounds=round_index,
                    rounds_at_head=same_head_consecutive,
                    max_rounds=self.max_rounds,
                    slice_epoch=current_slice_epoch,
                )
                raise RecoverableRetry(
                    f"round budget reached "
                    f"({same_head_consecutive} rounds on "
                    f"{head_sha[:12]}.. in slice "
                    f"{current_slice_epoch}); slice ended, "
                    f"retry resumed by supervisor"
                )
        # The controller's state machine is the authority.
        # If the run is not in REPAIRING_REVIEW_FINDINGS, the
        # relay refuses to run.
        sm = self.controller.load_state_machine()
        if sm is not None and sm.current_state != STATE_REPAIRING_REVIEW_FINDINGS:
            raise RelayError(
                f"relay refused to run: controller state is "
                f"{sm.current_state!r}; expected REPAIRING_REVIEW_FINDINGS"
            )
        started_at = _now_iso()
        # The current-head finding ledger is constructed per
        # round from the live head. ``store`` is the durable
        # StateStore; ``head_sha`` is the snapshot's exact head.
        finding_ledger = FindingLedger(self.store, head_sha=head_sha)
        decision = evaluate_round(
            snapshot=snapshot,
            head_sha=head_sha,
            repo=repo,
            pr_number=pr_number,
            round_index=round_index,
            required_check_names=self.required_check_names,
            coordinator_actor=ACTOR_CONTROLLER,
            directive_store=self.directive_store,
            finding_ledger=finding_ledger,
            focused_thread_id=focused_thread_id,
        )
        # Record the findings we just placed into a directive as
        # ``DISPATCHED`` on the current head. The ledger is the
        # durable proof that the directive was issued; the next
        # round on the same head will re-emit (round-27 invariant:
        # DISPATCHED is NOT consumed). CI findings are recorded
        # too so a check that is failing on head A but unrelated
        # to the directive is still tracked. ``filter_findings_to_current_head``
        # already wrote OBSERVED for every surviving finding; the
        # DISPATCHED row is the next state transition.
        if decision.directive is not None:
            for f in decision.directive.findings:
                finding_ledger.record_dispatched(f)
        # Persist the round transcript. The ``head_sha_after``
        # field stays ``None`` until the worker pushes and the
        # next round sees the new head; that is the head-change
        # signal the loop waits on.
        p1 = decision.p1_count
        p2 = decision.p2_count
        ci = decision.ci_failure_count
        reasons = decision.escalate_reasons
        if decision.outcome == "completed":
            outcome = "completed"
        elif decision.outcome == "ready":
            outcome = "ready"
        elif decision.outcome == "incomplete_evidence":
            # Round-31: provider evidence incomplete is a
            # RECOVERABLE state, NOT protected-authority
            # escalation. The transcript MUST persist the
            # actual ``outcome`` (``incomplete_evidence``)
            # so the persistent supervisor / scheduler
            # can retry with fresh surfaces. Mapping this
            # to ``escalated`` made an incomplete-evidence
            # failure look like a protected-authority
            # escalation, which is a liveness defect.
            outcome = "incomplete_evidence"
        else:
            # Round-31: unknown outcomes persist as-is
            # (``decision.outcome``) so the supervisor
            # can route them appropriately. Falling back
            # to ``escalated`` was incorrect — an unknown
            # outcome is NOT necessarily protected
            # authority.
            outcome = decision.outcome or "unknown"
        self.directive_store.append_transcript(RoundTranscript(
            schema_version=RELAY_SCHEMA_VERSION,
            round_index=round_index,
            head_sha_before=head_sha,
            head_sha_after=None,
            directive_id=decision.directive.directive_id if decision.directive else None,
            started_at=started_at,
            ended_at=_now_iso(),
            outcome=outcome,
            p1_count=p1,
            p2_count=p2,
            ci_failure_count=ci,
            escalate_reasons=reasons,
            # Round-32: stamp the transcript with the
            # current slice_epoch so the per-slice budget
            # counts it correctly.
            slice_epoch=int((retry_state or {}).get("slice_epoch", 0)),
        ))
        # Drive the controller through the required state
        # transitions. The relay is the integration point that
        # drives the orchestration state machine from
        # REPAIRING_REVIEW_FINDINGS through AWAITING_CI and
        # into QUALIFYING_READINESS on the qualifying path.
        if decision.action == "launch_worker":
            # The relay does NOT drive the state transition
            # here. The controller's state machine is the
            # only place where a new head is bound; the
            # transition REPAIRING_REVIEW_FINDINGS ->
            # AWAITING_CI fires only when the worker
            # actually pushes and a new head is observed.
            # Per the explicit transition contract: the
            # controller's transition method is the
            # authoritative bond, and the round's
            # head_observed value is the new head SHA.
            #
            # A launch failure leaves the state machine
            # in REPAIRING_REVIEW_FINDINGS — the next round
            # retries with the same repair directive or a
            # refreshed one. The relay preserves the
            # repairable state so the run is recoverable.
            log_attr = getattr(self.controller, "log", None)
            if log_attr is not None:
                log_attr(
                    "info",
                    "relay dispatched worker; controller stays in "
                    "REPAIRING_REVIEW_FINDINGS until new head is observed",
                    round_index=round_index,
                    head_observed=head_sha,
                )
        elif decision.action == "enter_qualifying_readiness":
            # REPAIRING_REVIEW_FINDINGS -> AWAITING_CI -> QUALIFYING_READINESS.
            # The head is clean; the controller enters the
            # qualification path so the existing readiness
            # gate can certify the head.
            try:
                self.controller.report_repair_pushed(
                    head_observed=head_sha,
                )
            except Exception as exc:
                # The state machine may already be past
                # REPAIRING_REVIEW_FINDINGS (e.g. if the CI
                # runner already drove the transition). The
                # report_ci_pass is the authoritative next
                # step; log and continue.
                log = getattr(self.controller, "log", None)
                if log is not None:
                    log(
                        "warning",
                        "relay could not transition REPAIRING_REVIEW_FINDINGS -> AWAITING_CI; "
                        "continuing to QUALIFYING_READINESS",
                        round_index=round_index,
                        error=str(exc),
                    )
            try:
                self.controller.report_ci_pass(
                    head_observed=head_sha,
                )
            except Exception as exc:
                log = getattr(self.controller, "log", None)
                if log is not None:
                    log(
                        "error",
                        "relay failed to drive AWAITING_CI -> QUALIFYING_READINESS",
                        round_index=round_index,
                        error=str(exc),
                    )
                raise
        # If the decision is to escalate, drive the controller
        # into BLOCKED so the operator has a single halt point.
        if decision.action == "escalate_to_human":
            self.controller.block(
                reason=next(iter(reasons), "relay escalated")
            )
            # NOTE: round-29 originally added a redundant
            # ``raise EscalateToHuman`` here. The canonical
            # wire contract is the returned ``RoundDecision``
            # (whose ``action == "escalate_to_human"`` carries
            # the signal); the CLI surfaces that as a
            # structured JSON decision. Raising here would
            # make the round-29 P0 escalation path round-trip
            # through both an exception AND a structured
            # decision, complicating the supervisor's wire
            # contract. The relay's caller MUST inspect
            # ``decision.action`` to know the round halted.
        return decision

    def run_until_head_advances(
        self,
        snapshot_provider: Any,
        head_sha: str,
        *,
        repo: str,
        pr_number: int,
        on_action: Any = None,
    ) -> RoundDecision:
        """Persistent loop: keep running ``run_once`` until the
        head advances (the worker pushed a new commit) or the
        head is clean (the relay returns the
        ``enter_qualifying_readiness`` action).

        The ``snapshot_provider`` is a callable taking the
        current head_sha and returning the live snapshot dict.
        The supervisor's ``capture_live_snapshot`` is the
        canonical provider; tests pass a stub.

        The ``on_action`` callable is invoked after each round
        with the ``RoundDecision``. The supervisor uses this
        to invoke its existing worker-launch machinery on
        ``action == "launch_worker"`` and its existing
        readiness gate on ``action ==
        "enter_qualifying_readiness"``.

        The loop is unbounded; only the protected-authority
        escalations above can halt it. The human
        exact-head merge-authorization boundary remains
        outside the relay's reach.
        """
        current_head = head_sha
        while True:
            snapshot = snapshot_provider(current_head)
            decision = self.run_once(
                snapshot, head_sha=current_head,
                repo=repo, pr_number=pr_number,
            )
            if on_action is not None:
                on_action(decision)
            if decision.action == "escalate_to_human":
                # Protected-authority escalation. The controller
                # is already in BLOCKED; the loop halts. Raise
                # the typed exception so the caller can catch the
                # protected-authority signal.
                raise EscalateToHuman(
                    "; ".join(decision.escalate_reasons)
                    or "relay escalated to human"
                )
            if decision.action == "enter_qualifying_readiness":
                # Head is clean. The supervisor's readiness gate
                # is the next step; the loop halts.
                return decision
            if decision.action == "await_head_change":
                # Round-31: provider evidence is incomplete
                # (provider_surface_complete=False). The
                # loop MUST NOT spin a tight loop and MUST
                # NOT escalate. We return the decision so
                # the persistent supervisor / scheduler
                # can schedule the next retry via the
                # bounded exponential backoff in
                # ``round_budget_retry.json``. The caller
                # is responsible for the schedule; this
                # method MUST NOT sleep.
                return decision
            if decision.action == "recoverable_retry":
                # Round-31: typed recoverable retry signal.
                # Same handling as ``await_head_change`` —
                # return the decision so the supervisor
                # schedules the next attempt via the
                # bounded retry state.
                return decision
            if decision.action == "launch_worker":
                # The supervisor will launch the worker. The
                # loop awaits the worker completion (head_sha
                # advances) before the next round. The
                # ``on_action`` callable is responsible for
                # waiting for the worker push and for returning
                # the new head_sha as ``current_head``.
                # The supervisor's event loop calls this in a
                # heartbeat-with-cooldown cadence so the head
                # advance is observed on the next iteration.
                new_head = self._await_head_advance(current_head)
                if new_head is None:
                    # Worker did not advance the head; the
                    # supervisor's next heartbeat will retry.
                    # We return the decision so the supervisor
                    # can ``time.sleep`` its cooldown.
                    return decision
                current_head = new_head
                continue
            # Round-31: unknown / future action MUST fail
            # closed rather than loop indefinitely. Raise
            # ``RecoverableRetry`` so the supervisor
            # schedules the next attempt via the bounded
            # retry state. Continuing here would silently
            # spin a tight loop on a value the supervisor
            # doesn't know how to route.
            raise RecoverableRetry(
                f"unknown decision action: {decision.action!r}; "
                f"fail-closed; supervisor schedules retry"
            )

    def mark_head_advanced(
        self,
        old_head_sha: str,
        new_head_sha: str,
        *,
        directive_id: Optional[str] = None,
        superseded_at: Optional[str] = None,
    ) -> None:
        """Bind the worker push to the state machine.

        The supervisor calls this when the worker's push
        is observed (the live PR reports a new head SHA
        that differs from the head the relay was acting
        on). The transition fires only on a real head
        advance — the controller's transition method is
        the authoritative bond between the previous
        REPAIRING_REVIEW_FINDINGS round and the new
        AWAITING_CI observation.

        Round-27: the finding ledger is advanced too.
        Every ACTIVE / DISPATCHED / OBSERVED entry for the
        old head is rewritten to SUPERSEDED on the new head.
        SUPERSEDED is a terminal state for that head; the
        next round on the new head evaluates each finding
        from scratch (a finding with the same signature is
        NOT re-emitted at the new head because the ledger
        marks it SUPERSEDED; a finding with a different
        signature — body edit, new code under review — is
        emitted because the signature differs).

        The transition is conditional: if the controller
        is not in REPAIRING_REVIEW_FINDINGS, the
        transition is a no-op (the controller may have
        already advanced via a manual operator action,
        or the CI runner drove the transition). The
        head_observed is the new head SHA.

        Round-C22R2/P1: the ledger's SUPERSEDED rows
        also record ``superseded_by_head=new_head_sha``
        and (when supplied) ``directive_id`` so the
        snapshot's C22-R2 eligibility helper can look up
        the authoritative repair-boundary evidence by
        finding ID instead of falling back to a
        git-ancestry heuristic.
        """
        if new_head_sha == old_head_sha:
            return
        # Round-C22R2/P1: validate new_head_sha shape so the
        # ledger's SUPERSEDED row never records a malformed
        # SHA. The controller's rebind path validates again
        # later (and raises ControllerError), but we want a
        # malformed value to fail closed BEFORE any ledger
        # writes happen. Mirrors the controller's existing
        # validation so existing tests that catch
        # ``ControllerError`` continue to pass.
        # Importing here to avoid a circular import at
        # module-load time.
        from autocoder_orchestration.controller import (
            ControllerError as _CtrlError,
        )
        if not isinstance(new_head_sha, str) or not _HEX_SHA_RE.match(new_head_sha):
            raise _CtrlError(
                f"mark_head_advanced new_head_sha must be 40 or 64 "
                f"lowercase hex chars: {new_head_sha!r}"
            )
        # Round-27: advance the finding ledger so the prior
        # head's findings are not re-emitted on the new head
        # unless fresh evidence explicitly reopens them.
        # Round-C22R2/P1: pass ``new_head_sha`` and the
        # optional ``directive_id`` so the SUPERSEDED row
        # carries the durable superseding-head evidence.
        old_ledger = FindingLedger(self.store, head_sha=old_head_sha)
        # Round-C24 / Defect 3: forward the authoritative
        # verified repair / push event timestamp so the
        # SUPERSEDED rows compare correctly against a
        # reviewer follow-up that was posted after the actual
        # push but before the supervisor observed the push.
        # The caller (``mark_head_advanced_public`` in
        # ``relay_wiring.py``) reads ``attempt.finished_at``
        # which is the canonical wall-clock the worker
        # recorded after verifying its own push to the live
        # PR. That timestamp is strictly EARLIER than the
        # supervisor's heartbeat observation time, which is
        # the value ``_now_iso()`` would otherwise produce.
        promoted = old_ledger.mark_superseded_by_head(
            old_head_sha,
            new_head_sha=new_head_sha,
            directive_id=directive_id,
            superseded_at=superseded_at,
        )
        log_attr = getattr(self.controller, "log", None)
        if log_attr is not None and promoted:
            log_attr(
                "info",
                "relay promoted findings to SUPERSEDED on head advance",
                old_head=old_head_sha[:12] if old_head_sha else "",
                promoted_count=promoted,
            )
        result = self.controller.report_repair_pushed(
            head_observed=new_head_sha,
        )
        if log_attr is not None:
            log_attr(
                "info",
                "relay bound worker push to AWAITING_CI",
                old_head=old_head_sha[:12] if old_head_sha else "",
                new_head=new_head_sha[:12],
                new_state=result.current_state,
            )

    def _await_head_advance(self, head_sha: str) -> Optional[str]:
        """Hook for the supervisor's worker-completion wait.

        The supervisor polls the live PR head until the head
        SHA differs from ``head_sha``. The default is a no-op
        (the supervisor implements the wait); the loop
        framework treats ``None`` as "no advance observed yet".
        The supervisor MAY override this hook to provide
        its own wait semantics; the relay's ``run_until_head_advances``
        loop is the persistent harness the supervisor drives.
        """
        return None

    def head_clean(self, snapshot: dict) -> bool:
        """Return True iff the head has no actionable findings."""
        findings = collect_findings(
            snapshot, required_check_names=self.required_check_names,
        )
        return len(findings) == 0


def read_round_budget_retry(evidence_root: Any) -> Optional[dict]:
    """Round-31: read the recoverable retry state for the
    supervisor / scheduler.

    Returns the parsed JSON dict, or ``None`` if the
    ledger is missing or unreadable.
    """
    retry_path = (
        Path(str(evidence_root)) / "round_budget_retry.json"
    )
    if not retry_path.is_file():
        return None
    try:
        return json.loads(retry_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def bump_slice_epoch(evidence_root: Any) -> int:
    """Round-31: bump the slice_epoch on the retry ledger.

    Called by the persistent supervisor / scheduler at
    the start of a NEW execution slice. This prevents
    the permanent-failure mode where
    ``completed_round_count >= budget`` on every
    invocation: the slice_epoch increment lets a fresh
    slice get a fresh round budget while preserving the
    unresolved findings (the retry ledger carries the
    ``head_sha`` and outstanding work).

    Returns the new slice_epoch (>= 1).
    """
    existing = read_round_budget_retry(evidence_root) or {}
    new_epoch = int(existing.get("slice_epoch", 0)) + 1
    payload = dict(existing)
    payload["slice_epoch"] = new_epoch
    payload["recorded_at"] = _now_iso()
    retry_path = (
        Path(str(evidence_root)) / "round_budget_retry.json"
    )
    try:
        retry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = retry_path.with_suffix(
            retry_path.suffix + ".tmp",
        )
        tmp_path.write_text(
            json.dumps(payload, sort_keys=True),
        )
        tmp_path.replace(retry_path)
    except OSError:
        # Persistence failure: return the epoch anyway;
        # the supervisor will retry on the next slice.
        pass
    return new_epoch


__all__ = [
    "ALL_FINDING_STATES",
    "ALL_SEVERITIES",
    "DEFAULT_MAX_ROUNDS",
    "DirectiveContractError",
    "DirectiveStore",
    "EscalateToHuman",
    "FINDING_LEDGER_SCHEMA_VERSION",
    "FINDING_STATE_ACTIVE",
    "FINDING_STATE_DISPATCHED",
    "FINDING_STATE_OBSERVED",
    "FINDING_STATE_REPAIRED",
    "FINDING_STATE_SUPERSEDED",
    "Finding",
    "FindingLedger",
    "InvalidSnapshot",
    "RELAY_SCHEMA_VERSION",
    "RelayError",
    "RelayLoop",
    "RecoverableRetry",
    "ReviewDirective",
    "RoundDecision",
    "RoundTranscript",
    "SEVERITY_CI_FAILURE",
    "SEVERITY_P0_ESCALATE",
    "SEVERITY_P1",
    "SEVERITY_P2",
    "WORKER_PROMPT_TEMPLATE",
    # Round-C22/C22: export so tests + callers can introspect the
    # narrow resurrection helper without reaching into ``_``-prefixed
    # names.
    "_c22_is_followup_eligible",
    "_maybe_resurrect_outdated_thread",
    "_normalize_operator_logins",
    "_parse_iso8601_utc",
    "bump_slice_epoch",
    "build_directive",
    "build_worker_prompt",
    "collect_findings",
    "evaluate_round",
    "filter_findings_to_current_head",
    "heads_equal",
    "read_round_budget_retry",
    "relay_state_for_outcome",
]
