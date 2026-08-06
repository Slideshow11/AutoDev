"""Strict readiness observer.

Library code that observes GitHub-side state via a caller-supplied
data source and emits per-observation records. The observer itself
contains no PR number, repository name, or SHA — those are passed in
through the run context.

A failed observation resets the qualifying interval. The interval
must be at least ``quiet_window_seconds`` of uninterrupted
qualifying observations before the window is recorded as complete.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class Observation:
    """A single observation of the canonical readiness state."""

    ts_utc: str
    ts_monotonic: float
    qualifying: bool
    qualification_reason: str
    pr_head_sha: Optional[str]
    head_ok: bool
    all_ci_pass: bool
    coderabbit_pass: bool
    threads_unresolved_current: int
    threads_unresolved_outdated: int
    worker_lease_active: bool
    unconsumed_event_count: int
    provider_in_progress: bool
    supervisor_pid: Optional[int]
    process_start_identity: Optional[str]
    heartbeat: bool
    api_failure: Optional[str]
    parse_failure: Optional[str]
    fallback_success: bool

    def to_dict(self) -> dict:
        return {
            "ts_utc": self.ts_utc,
            "ts_monotonic": self.ts_monotonic,
            "qualifying": self.qualifying,
            "qualification_reason": self.qualification_reason,
            "pr_head_sha": self.pr_head_sha,
            "head_ok": self.head_ok,
            "all_ci_pass": self.all_ci_pass,
            "coderabbit_pass": self.coderabbit_pass,
            "threads_unresolved_current": self.threads_unresolved_current,
            "threads_unresolved_outdated": self.threads_unresolved_outdated,
            "worker_lease_active": self.worker_lease_active,
            "unconsumed_event_count": self.unconsumed_event_count,
            "provider_in_progress": self.provider_in_progress,
            "supervisor_pid": self.supervisor_pid,
            "process_start_identity": self.process_start_identity,
            "heartbeat": self.heartbeat,
            "api_failure": self.api_failure,
            "parse_failure": self.parse_failure,
            "fallback_success": self.fallback_success,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Observation":
        if not isinstance(payload, dict):
            raise ValueError("observation payload must be a dict")
        return cls(
            ts_utc=str(payload["ts_utc"]),
            ts_monotonic=float(payload["ts_monotonic"]),
            qualifying=bool(payload["qualifying"]),
            qualification_reason=str(payload["qualification_reason"]),
            pr_head_sha=payload.get("pr_head_sha"),
            head_ok=bool(payload.get("head_ok", False)),
            all_ci_pass=bool(payload.get("all_ci_pass", False)),
            coderabbit_pass=bool(payload.get("coderabbit_pass", False)),
            threads_unresolved_current=int(payload.get("threads_unresolved_current", 0)),
            threads_unresolved_outdated=int(payload.get("threads_unresolved_outdated", 0)),
            worker_lease_active=bool(payload.get("worker_lease_active", False)),
            unconsumed_event_count=int(payload.get("unconsumed_event_count", 0)),
            provider_in_progress=bool(payload.get("provider_in_progress", False)),
            supervisor_pid=payload.get("supervisor_pid"),
            process_start_identity=payload.get("process_start_identity"),
            heartbeat=bool(payload.get("heartbeat", False)),
            api_failure=payload.get("api_failure"),
            parse_failure=payload.get("parse_failure"),
            fallback_success=bool(payload.get("fallback_success", False)),
        )


@dataclass
class ObservationLog:
    """Append-only observation log."""

    observations: List[Observation] = field(default_factory=list)

    def append(self, o: Observation) -> None:
        if not isinstance(o, Observation):
            raise ValueError("observation must be Observation")
        self.observations.append(o)

    def all_qualifying(self) -> List[Observation]:
        return [o for o in self.observations if o.qualifying]

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(o.to_dict()) for o in self.observations) + "\n"

    @classmethod
    def from_jsonl(cls, text: str) -> "ObservationLog":
        obs = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            obs.append(Observation.from_dict(json.loads(line)))
        return cls(observations=obs)


# === Data source protocol ===
class ObservationDataSource:
    """Caller-supplied data source for the observer.

    The observer itself contains no PR number, repository name, or
    expected SHA. The data source supplies them through the
    ``expected_head`` argument and re-evaluates the live state on
    each call.
    """

    def fetch(self, expected_head: str) -> Dict[str, Any]:
        """Return a dict matching the keys of one observation block.

        The data source must NOT omit fields. Missing fields cause
        the observer to mark the observation as non-qualifying.
        """
        raise NotImplementedError("subclass must implement fetch")


# === Strict Observer ===
class StrictObserver:
    """Library observer that captures a continuous qualifying interval.

    The observer runs in a loop. On each iteration it asks the data
    source for a fresh snapshot, classifies it, and appends an
    :class:`Observation` to the log. Any non-qualifying observation
    resets the interval. The interval is recorded as complete only
    when ``quiet_window_seconds`` of uninterrupted qualifying
    observations have been captured.
    """

    def __init__(
        self,
        data_source: ObservationDataSource,
        *,
        quiet_window_seconds: int,
        poll_interval_seconds: float = 5.0,
        max_iterations: Optional[int] = None,
    ) -> None:
        if quiet_window_seconds < 0:
            raise ValueError("quiet_window_seconds must be non-negative")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.data_source = data_source
        self.quiet_window_seconds = quiet_window_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.max_iterations = max_iterations

    def observe(
        self,
        expected_head: str,
        *,
        anonymous_log: Optional[ObservationLog] = None,
    ) -> Tuple[ObservationLog, bool, float]:
        """Run the observer until the quiet window is complete or
        ``max_iterations`` is reached.

        Returns ``(log, complete, duration_monotonic)``.
        """
        log = anonymous_log or ObservationLog()
        first_qualifying: Optional[Observation] = None
        last_qualifying: Optional[Observation] = None
        iter_count = 0
        while True:
            if self.max_iterations is not None and iter_count >= self.max_iterations:
                break
            iter_count += 1
            snap = self._safe_fetch(expected_head)
            obs = self._classify(snap, expected_head)
            log.append(obs)
            if obs.qualifying:
                if first_qualifying is None:
                    first_qualifying = obs
                last_qualifying = obs
                if (
                    first_qualifying is not None
                    and last_qualifying is not None
                    and (last_qualifying.ts_monotonic - first_qualifying.ts_monotonic)
                    >= self.quiet_window_seconds
                ):
                    # Also require PID and start-identity stability
                    if first_qualifying.supervisor_pid == last_qualifying.supervisor_pid and \
                       first_qualifying.process_start_identity == last_qualifying.process_start_identity:
                        duration = last_qualifying.ts_monotonic - first_qualifying.ts_monotonic
                        return log, True, duration
            else:
                # Reset
                first_qualifying = None
                last_qualifying = None
            time.sleep(self.poll_interval_seconds)
        duration = (
            (last_qualifying.ts_monotonic - first_qualifying.ts_monotonic)
            if first_qualifying is not None and last_qualifying is not None
            else 0.0
        )
        return log, False, duration

    def _safe_fetch(self, expected_head: str) -> Dict[str, Any]:
        """Fetch from the data source, marking any error as a non-qualifying observation."""
        try:
            return self.data_source.fetch(expected_head)
        except Exception as e:
            return {
                "pr_head_sha": None,
                "head_ok": False,
                "all_ci_pass": False,
                "coderabbit_pass": False,
                "threads_unresolved_current": 0,
                "threads_unresolved_outdated": 0,
                "worker_lease_active": False,
                "unconsumed_event_count": 0,
                "provider_in_progress": False,
                "supervisor_pid": None,
                "process_start_identity": None,
                "heartbeat": False,
                "api_failure": f"{type(e).__name__}: {e}",
                "parse_failure": None,
                "fallback_success": False,
                "qualifying": False,
                "qualification_reason": f"data source error: {e}",
            }

    def _classify(self, snap: Dict[str, Any], expected_head: str) -> Observation:
        # The data source MAY have returned a 'qualifying' opinion; we
        # always recompute deterministically from the gate fields.
        ts_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ts_monotonic = time.monotonic()

        head_ok = snap.get("head_ok") is True and snap.get("pr_head_sha") == expected_head
        ci_ok = snap.get("all_ci_pass") is True
        cr_ok = snap.get("coderabbit_pass") is True
        no_threads = (
            int(snap.get("threads_unresolved_current", 0)) == 0
            and int(snap.get("threads_unresolved_outdated", 0)) == 0
        )
        no_worker = (
            snap.get("worker_lease_active") is False
            and int(snap.get("unconsumed_event_count", 0)) == 0
            and snap.get("provider_in_progress") is False
        )
        no_api_fail = snap.get("api_failure") is None
        no_parse_fail = snap.get("parse_failure") is None
        no_fallback = snap.get("fallback_success") is False
        has_pid = snap.get("supervisor_pid") is not None
        has_start_id = snap.get("process_start_identity") is not None
        has_heartbeat = snap.get("heartbeat") is True

        reasons = []
        if not head_ok:
            reasons.append("head not stable")
        if not ci_ok:
            reasons.append("CI not all passing")
        if not cr_ok:
            reasons.append("CodeRabbit not approved")
        if not no_threads:
            reasons.append("unresolved threads")
        if not no_worker:
            reasons.append("active worker/event/provider")
        if not no_api_fail:
            reasons.append("API failure")
        if not no_parse_fail:
            reasons.append("parse failure")
        if not no_fallback:
            reasons.append("fallback-derived success")
        if not has_pid:
            reasons.append("no supervisor PID")
        if not has_start_id:
            reasons.append("no start identity")
        if not has_heartbeat:
            reasons.append("no heartbeat")

        qualifying = (
            head_ok
            and ci_ok
            and cr_ok
            and no_threads
            and no_worker
            and no_api_fail
            and no_parse_fail
            and no_fallback
            and has_pid
            and has_start_id
            and has_heartbeat
        )
        return Observation(
            ts_utc=ts_utc,
            ts_monotonic=ts_monotonic,
            qualifying=qualifying,
            qualification_reason="qualifying" if qualifying else "; ".join(reasons),
            pr_head_sha=snap.get("pr_head_sha"),
            head_ok=head_ok,
            all_ci_pass=ci_ok,
            coderabbit_pass=cr_ok,
            threads_unresolved_current=int(snap.get("threads_unresolved_current", 0)),
            threads_unresolved_outdated=int(snap.get("threads_unresolved_outdated", 0)),
            worker_lease_active=snap.get("worker_lease_active", False),
            unconsumed_event_count=int(snap.get("unconsumed_event_count", 0)),
            provider_in_progress=snap.get("provider_in_progress", False),
            supervisor_pid=snap.get("supervisor_pid"),
            process_start_identity=snap.get("process_start_identity"),
            heartbeat=has_heartbeat,
            api_failure=snap.get("api_failure"),
            parse_failure=snap.get("parse_failure"),
            fallback_success=no_fallback is False,
        )
