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
import re
import time
import uuid
from dataclasses import dataclass
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
    ProcessIdentity,
    current_process_identity,
)
from .artifacts import write_artifact, read_artifact, ArtifactError

# === Error hierarchy ===

class RelayError(Exception):
    """Base error for the relay."""


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
        except (OSError, KeyError, AttributeError):
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

    def mark_superseded_by_head(self, old_head_sha: str) -> int:
        """Mark every ACTIVE finding on ``old_head_sha`` as
        SUPERSEDED.

        Called when the supervisor observes the worker push and
        the head advances from ``old_head_sha`` to a new SHA.
        The ledger rewrites every ACTIVE / DISPATCHED / OBSERVED
        entry for ``old_head_sha`` to SUPERSEDED. SUPERSEDED is
        a terminal state on that head.

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
            superseded["superseded_at"] = _now_iso()
            try:
                self.store.append_journal(self._rel_path(), superseded)
                promoted += 1
            except (OSError, AttributeError):
                # Ledger writes are advisory; do not block the
                # controller on a journal write failure.
                pass
        return promoted

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
                f"round_index must be non-negative: {self.round_index}"
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
_NON_FINDING_COMMENT_RE = re.compile(
    r"(?i)\b("
    r"walkthrough|"
    r"in progress|"
    r"review in progress|"
    r"in-review|"
    r"review complete|"
    r"review completed|"
    r"review approved|"
    r"review request|"
    r"finished review|"
    r"commented on your changes|"
    r"finished"
    r")\b"
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
    """
    if not body:
        return False
    stripped = body.strip()
    if not stripped:
        return False
    # The first non-empty line sets the comment's intent.
    first_line = stripped.split("\n", 1)[0].strip().rstrip(".,;:!?")
    # Strip leading emoji / decorative characters. A
    # status marker may be prefixed with a traffic-light
    # emoji (🚦), a bell (🔔), etc. The alpha-stripped
    # first line is the canonical form.
    alphanumeric_first_line = "".join(
        c for c in first_line if c.isalnum() or c.isspace()
    ).strip()
    # If the first line is a status marker (with optional
    # emoji / whitespace prefix), the entire comment is a
    # status marker.
    if (
        _NON_FINDING_COMMENT_RE.match(first_line)
        or _NON_FINDING_COMMENT_RE.match(alphanumeric_first_line)
    ):
        # And the body is short (single-line status marker).
        # Long bodies with a status-marker first line are
        # treated as actionable (the body has real content).
        if len(stripped) < 200 and stripped.count("\n") <= 2:
            return False
    return True


def _collect_review_findings(snapshot: dict) -> List[Finding]:
    """Extract CodeRabbit-style inline-comment findings from a snapshot.

    The snapshot shape is the supervisor's
    ``capture_live_snapshot`` output. The collector accepts
    ``_provider_issue_comments`` (per-provider subset) and/or the
    unfiltered ``issue_comments`` list. Provider matching is done
    by bot-login substring because the supervisor records bot
    logins under ``[bot]``-suffixed form for GitHub Apps.

    Status markers (walkthrough, in-progress, completion)
    are filtered out so the relay does not turn a clean
    head into a persistent repair loop.
    """
    if not isinstance(snapshot, dict):
        raise InvalidSnapshot("snapshot must be a dict")
    findings: List[Finding] = []
    seen_ids: set = set()
    # Prefer the per-provider subset when present, fall back to
    # the unfiltered list. The supervisor's snapshot guarantees
    # that the subset is a filtered copy of the unfiltered list.
    primary = snapshot.get("_provider_issue_comments") or {}
    for provider, comments in primary.items():
        if not isinstance(comments, list):
            continue
        for c in comments:
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            if cid is None:
                continue
            finding_id = f"{provider}:{cid}"
            if finding_id in seen_ids:
                continue
            seen_ids.add(finding_id)
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
            seen_ids.add(finding_id)
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
            seen_ids.add(finding_id)
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
    """
    review_findings = _collect_review_findings(snapshot)
    ci_findings = _collect_ci_findings(snapshot, required_check_names)
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
    """
    if not findings:
        raise DirectiveContractError("build_directive requires at least one finding")
    p0 = [f for f in findings if f.severity == SEVERITY_P0_ESCALATE]
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
    # When the snapshot's head_match flag is explicitly False,
    # the capture was for a different head. Refuse.
    if snapshot.get("head_match") is False:
        raise InvalidSnapshot(
            f"snapshot head_match is False for requested head {head_sha!r}; "
            "stale snapshot rejected"
        )
    findings = collect_findings(
        snapshot,
        required_check_names=required_check_names,
        ledger=finding_ledger,
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
    try:
        directive = build_directive(
            round_index=round_index,
            head_sha=head_sha,
            repo=repo,
            pr_number=pr_number,
            findings=findings,
            coordinator_actor=coordinator_actor,
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
    "Directive SHA-256: {directive_sha256}\n\n"
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
    "the next round decision."
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
        # max_rounds is a SAFETY NET for a runaway loop where the
        # head never advances. Default is disabled (None); the
        # persistent supervisor does not need this bound.
        self.max_rounds = (
            int(max_rounds) if max_rounds is not None else None
        )
        self.identity = identity or current_process_identity()

    def run_once(
        self,
        snapshot: dict,
        head_sha: str,
        *,
        repo: str,
        pr_number: int,
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
        began, not when it ended. The ``max_rounds`` safety
        net only triggers if the same head produces more
        repair rounds than the configured maximum AND no new
        findings appear across that span — the relay never
        halts on a single reachable round.
        """
        round_index = self.directive_store.last_round_index() + 1
        if self.max_rounds is not None:
            prior = self.directive_store.read_transcript()
            same_head_consecutive = sum(
                1 for t in prior
                if t.head_sha_before == head_sha and t.outcome == "completed"
            )
            if same_head_consecutive >= self.max_rounds:
                # SAFETY NET: the head has not advanced and the
                # worker has produced more than ``max_rounds``
                # directive launches on the same head. The
                # relay is stuck in a repair loop. Drive the
                # controller into BLOCKED so the operator can
                # diagnose.
                self.controller.block(
                    reason=(
                        f"relay safety net: {same_head_consecutive} "
                        f"consecutive rounds without head advancement "
                        f"on {head_sha[:12]}..; operator must inspect"
                    )
                )
                raise EscalateToHuman(
                    f"relay safety net: head {head_sha[:12]}.. has not "
                    f"advanced after {same_head_consecutive} repair rounds"
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
        else:
            outcome = "escalated"
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

    def mark_head_advanced(self, old_head_sha: str, new_head_sha: str) -> None:
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
        """
        if new_head_sha == old_head_sha:
            return
        # Round-27: advance the finding ledger so the prior
        # head's findings are not re-emitted on the new head
        # unless fresh evidence explicitly reopens them.
        old_ledger = FindingLedger(self.store, head_sha=old_head_sha)
        promoted = old_ledger.mark_superseded_by_head(old_head_sha)
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
    "ReviewDirective",
    "RoundDecision",
    "RoundTranscript",
    "SEVERITY_CI_FAILURE",
    "SEVERITY_P0_ESCALATE",
    "SEVERITY_P1",
    "SEVERITY_P2",
    "WORKER_PROMPT_TEMPLATE",
    "build_directive",
    "build_worker_prompt",
    "collect_findings",
    "evaluate_round",
    "filter_findings_to_current_head",
    "heads_equal",
    "relay_state_for_outcome",
]
