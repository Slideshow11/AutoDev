"""Round-C24 / Defect 3: true repair timestamp regression.

The audit's exact reproduction: a reviewer follow-up arrives
between the verified repair push (T1) and the supervisor's
later heartbeat observation (T3). Strict ``follow-up.createdAt
> superseded_at`` comparison must use T1, NOT T3, otherwise
the genuinely post-repair follow-up is silently rejected.

Chronology:

  T1 = verified repair push / commit (the worker's
      authoritative ``finished_at`` after ``gh push`` +
      GitHub round-trip verification).
  T2 = reviewer posts a follow-up on the prior head.
  T3 = supervisor's heartbeat observes the new head and
      calls ``mark_superseded_by_head``.

The audit requires ``T1 < T2 < T3`` and the resurrection rule
must use T1, not T3.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest


_REPO_ROOT = Path(os.environ.get("REPO_ROOT", str(Path(__file__).resolve().parent.parent)))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from autocoder_orchestration.review_repair_relay import (  # noqa: E402
    FINDING_STATE_OBSERVED,
    FINDING_STATE_SUPERSEDED,
    Finding,
    FindingLedger,
)
from autocoder_orchestration.store import StateStore  # noqa: E402


PRIOR_HEAD = "a" * 40
REPAIR_HEAD = "b" * 40

# Authoritative repair timestamps.
T1_REPAIR_PUSH = "2026-08-19T14:18:00Z"  # worker's verified push
T2_REVIEWER_FOLLOWUP = "2026-08-19T14:27:14Z"  # reviewer follow-up
T3_OBSERVATION = "2026-08-19T19:07:00Z"  # supervisor observation


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    s = StateStore(str(tmp_path))
    s.write_atomic(
        "state.json",
        {
            "current_state": "REPAIRING_REVIEW_FINDINGS",
            "revision": 0,
            "expected_revision": 0,
            "journal": [],
            "evidence": {},
        },
    )
    return s


def _make_finding() -> Finding:
    return Finding(
        finding_id="finding-T1-T2-T3",
        source="codex",
        severity="P1",
        title="Test finding for true-repair-timestamp",
        body="Body content.",
        file_path="autocoder_supervisor/reviewer_policy.py",
        line=1016,
        url=None,
        suggested_test=None,
        review_id=12345,
        comment_id=67890,
        check_name=None,
    )


def _superseded_records(store: StateStore):
    out = []
    for entry in store.read_journal("finding_ledger.jsonl"):
        if isinstance(entry, dict) and entry.get("state") == FINDING_STATE_SUPERSEDED:
            out.append(entry)
    return out


class TestTrueRepairTimestamp:
    def test_T1_less_than_T2_less_than_T3(
        self, store: StateStore,
    ) -> None:
        """Chronology precondition: T1 < T2 < T3.

        The audit requires this ordering. Without it, the
        regression below cannot meaningfully exercise the
        strict comparison."""
        assert T1_REPAIR_PUSH < T2_REVIEWER_FOLLOWUP < T3_OBSERVATION, (
            f"test fixture chronology broken: "
            f"T1={T1_REPAIR_PUSH} T2={T2_REVIEWER_FOLLOWUP} T3={T3_OBSERVATION}"
        )

    def test_superseded_at_with_T1_qualifies_followup(
        self, store: StateStore,
    ) -> None:
        """The audit's required behaviour: ``mark_superseded_by_head``
        called with the authoritative T1 (verified push time)
        records ``superseded_at=T1``. A follow-up at T2 is then
        strictly AFTER the superseded record (T2 > T1) and
        qualifies for resurrection."""
        ledger = FindingLedger(store, head_sha=PRIOR_HEAD)
        ledger.record_observed(_make_finding())
        ledger.mark_superseded_by_head(
            PRIOR_HEAD,
            new_head_sha=REPAIR_HEAD,
            directive_id="dir-T1",
            superseded_at=T1_REPAIR_PUSH,
        )
        records = _superseded_records(store)
        assert len(records) == 1
        assert records[0]["superseded_at"] == T1_REPAIR_PUSH
        # The follow-up at T2 is strictly later than T1 → qualifies.
        assert T2_REVIEWER_FOLLOWUP > records[0]["superseded_at"]

    def test_superseded_at_with_T3_rejects_followup(
        self, store: StateStore,
    ) -> None:
        """The audit's documented defect: when the prior
        implementation wrote ``superseded_at=T3`` (the later
        observation wall clock), the follow-up at T2 was
        silently rejected because T2 < T3.

        This test documents the BUG. The fix uses T1 (verified
        push time) instead. We keep this negative test as a
        regression witness: when ``superseded_at=T3`` is
        passed, the resurrection rule must continue to
        compare ``T2 > T3`` and reject.
        """
        ledger = FindingLedger(store, head_sha=PRIOR_HEAD)
        ledger.record_observed(_make_finding())
        ledger.mark_superseded_by_head(
            PRIOR_HEAD,
            new_head_sha=REPAIR_HEAD,
            directive_id="dir-T3",
            superseded_at=T3_OBSERVATION,
        )
        records = _superseded_records(store)
        assert len(records) == 1
        assert records[0]["superseded_at"] == T3_OBSERVATION
        # The follow-up at T2 is BEFORE T3 → rejected.
        assert T2_REVIEWER_FOLLOWUP < records[0]["superseded_at"]
        # Therefore the audit's exact reproduction: ``T2 > T3``
        # is False, and the follow-up is silently rejected.

    def test_legacy_call_without_superseded_at_fails_closed(
        self, store: StateStore,
    ) -> None:
        """Backward compatibility + fail-closed: callers that
        omit ``superseded_at`` MUST NOT silently produce a
        ``_now_iso()`` record (the audit's documented defect).
        The relay removes the ``superseded_at`` field entirely,
        so the C22 resurrection helper (which compares
        ``follow_up.createdAt > superseded_at``) refuses to
        resurrect. Operators MUST pass the verified
        ``attempt.finished_at`` explicitly."""
        ledger = FindingLedger(store, head_sha=PRIOR_HEAD)
        ledger.record_observed(_make_finding())
        ledger.mark_superseded_by_head(
            PRIOR_HEAD,
            new_head_sha=REPAIR_HEAD,
            directive_id="dir-legacy",
            # ``superseded_at`` deliberately omitted.
        )
        records = _superseded_records(store)
        assert len(records) == 1
        # The C24 fail-closed: ``superseded_at`` is absent
        # entirely. This is the canonical signal that the
        # authoritative repair transition time is missing.
        assert "superseded_at" not in records[0], (
            "fail-closed invariant broken: ``superseded_at`` "
            "must be absent when caller does not supply the "
            "authoritative repair transition time"
        )