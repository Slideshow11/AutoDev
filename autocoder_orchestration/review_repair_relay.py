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
from dataclasses import dataclass, field
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
    StateStoreError,
    ProcessIdentity,
    Lease,
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
_PATH_ANCHOR_RE = re.compile(
    r"(?P<path>[A-Za-z0-9_./\-]+\.[A-Za-z0-9_]+)(?::(?P<line>\d+))?",
)

#: Regex that captures a suggested test from the test-gap markers.
_TEST_SUGGEST_RE = re.compile(
    r"(?P<test>test_[A-Za-z0-9_]+|Test[A-Za-z0-9_]+|[A-Za-z0-9_]+Test)\b",
)


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
        if not self.head_sha or (len(self.head_sha) != 40 and len(self.head_sha) != 64):
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
        """SHA-256 of the canonical serialization of the directive."""
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
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


def _collect_review_findings(snapshot: dict) -> List[Finding]:
    """Extract CodeRabbit-style inline-comment findings from a snapshot.

    The snapshot shape is the supervisor's
    ``capture_live_snapshot`` output. The collector accepts
    ``_provider_issue_comments`` (per-provider subset) and/or the
    unfiltered ``issue_comments`` list. Provider matching is done
    by bot-login substring because the supervisor records bot
    logins under ``[bot]``-suffixed form for GitHub Apps.
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
    """
    if not isinstance(snapshot, dict):
        raise InvalidSnapshot("snapshot must be a dict")
    findings: List[Finding] = []
    checks = snapshot.get("required_checks") or {}
    if not isinstance(checks, dict):
        return findings
    for name, info in checks.items():
        if not isinstance(info, dict):
            continue
        # Only emit a finding if the operator marked this check as
        # required in the supervisor config. The supervisor's
        # snapshot includes every check it fetched, not just the
        # required ones.
        if required_check_names and name not in required_check_names:
            continue
        conclusion = str(info.get("conclusion") or "").lower()
        status = str(info.get("status") or "").lower()
        # A successful conclusion is never a finding.
        if conclusion == "success":
            continue
        # A still-running check is not a finding yet; the next
        # supervisor iteration will see its conclusion.
        if conclusion == "" and status not in ("completed", "failure", "failed"):
            continue
        # ``conclusion`` is anything other than "success" and the
        # run has reached a terminal state. Emit a finding.
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
    return findings


def collect_findings(
    snapshot: dict,
    *,
    required_check_names: Tuple[str, ...] = (),
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
    """
    review_findings = _collect_review_findings(snapshot)
    ci_findings = _collect_ci_findings(snapshot, required_check_names)
    findings: List[Finding] = list(review_findings) + list(ci_findings)
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


__all__ = [
    "RelayError",
    "EscalateToHuman",
    "InvalidSnapshot",
    "DirectiveContractError",
    "DEFAULT_MAX_ROUNDS",
    "RELAY_SCHEMA_VERSION",
    "SEVERITY_P0_ESCALATE",
    "SEVERITY_P1",
    "SEVERITY_P2",
    "SEVERITY_CI_FAILURE",
    "ALL_SEVERITIES",
    "Finding",
    "ReviewDirective",
    "RoundTranscript",
    "DirectiveStore",
    "collect_findings",
    "build_directive",
    "heads_equal",
    "relay_state_for_outcome",
]
