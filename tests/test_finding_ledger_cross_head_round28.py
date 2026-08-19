"""Round-28 P5: FindingLedger cross-head semantics — user-spec test.

User-supplied scenario:
  A/F ACTIVE
  → push B
  → A/F may be historically SUPERSEDED_BY_HEAD_B
  but:
  B reports F again
  → B/F becomes ACTIVE
  → repair directive contains F
  → qualification blocked.

Only positive cross-head resolution evidence may suppress F
on the new head.
"""
from __future__ import annotations

from pathlib import Path


from autocoder_orchestration.review_repair_relay import (
    FINDING_STATE_ACTIVE,
    FINDING_STATE_SUPERSEDED,
    Finding,
    FindingLedger,
)


def _make_finding(*, body: str = "P1 finding", title: str = "P1 finding",
                  comment_id: int = 99) -> Finding:
    return Finding(
        finding_id=f"coderabbit:{comment_id}",
        source="coderabbit",
        severity="P1",
        title=title,
        body=body,
        file_path=None,
        line=None,
        url=None,
        suggested_test=None,
        review_id=None,
        comment_id=comment_id,
        check_name=None,
    )


def test_round_28_p5_cross_head_active_finding_remains_active(
    tmp_path: Path,
) -> None:
    """Round-28 P5 user-specified scenario.

    Sequence:
      Head A: F is ACTIVE (worker has not yet resolved it).
      Worker pushes head B.
      ``mark_head_advanced`` is called; A/F is marked
      SUPERSEDED on the OLD head.
      Reviewer reports identical F on head B (same signature).
      Round-28 P5: B/F MUST be ACTIVE on head B. The
      SUPERSEDED row on A is NOT positive cross-head
      resolution evidence. The directive on B MUST contain F.
      Qualification MUST be blocked.
    """
    from autocoder_orchestration.store import StateStore
    store = StateStore(str(tmp_path / "state"))
    old_head = "a" * 40
    new_head = "b" * 40
    # The finding on the OLD head is ACTIVE.
    ledger_a = FindingLedger(store, head_sha=old_head)
    f = _make_finding()
    ledger_a.record_observed(f)
    ledger_a.record_dispatched(f)
    ledger_a.mark_active(f)
    # State on A right before head advance: ACTIVE.
    assert ledger_a.state_of(f.finding_id)["state"] == FINDING_STATE_ACTIVE
    # Worker pushes head B. The supervisor calls
    # ``mark_head_advanced`` which promotes A/F to SUPERSEDED
    # on the OLD head.
    promoted = ledger_a.mark_superseded_by_head(old_head)
    assert promoted == 1, (
        f"mark_superseded_by_head MUST promote ACTIVE / "
        f"DISPATCHED entries on the OLD head; got {promoted}"
    )
    # The ledger's historical record on the OLD head shows
    # SUPERSEDED (for audit; the relay can use this to know
    # F was previously active on A).
    assert ledger_a.state_of(f.finding_id)["state"] == FINDING_STATE_SUPERSEDED
    # Round-28 P5: on the NEW head, the same finding with the
    # same signature MUST be FRESH. The cross-head SUPERSEDED
    # on A is NOT positive cross-head resolution evidence.
    ledger_b = FindingLedger(store, head_sha=new_head)
    f_b = _make_finding()  # same signature
    assert ledger_b.is_fresh(f_b) is True, (
        "Round-28 P5: identical F observed on B after head "
        "advance MUST be FRESH; the SUPERSEDED row on A is "
        "NOT positive cross-head resolution evidence."
    )
    # The relay would emit F into the next directive on B.
    # If F is the only finding, the directive MUST contain it,
    # qualification MUST be blocked.


def test_round_28_p5_different_signature_is_fresh(
    tmp_path: Path,
) -> None:
    """A different signature (e.g. body edit) at the new head
    is fresh evidence and MUST be emitted.
    """
    from autocoder_orchestration.store import StateStore
    store = StateStore(str(tmp_path / "state"))
    old_head = "a" * 40
    new_head = "b" * 40
    ledger_a = FindingLedger(store, head_sha=old_head)
    f = _make_finding()
    ledger_a.record_observed(f)
    ledger_a.record_dispatched(f)
    ledger_a.mark_active(f)
    ledger_a.mark_superseded_by_head(old_head)
    ledger_b = FindingLedger(store, head_sha=new_head)
    f_b_edited = _make_finding(body="new body", title="new body")
    assert ledger_b.is_fresh(f_b_edited) is True


def test_round_28_p5_same_head_superseded_silences(
    tmp_path: Path,
) -> None:
    """When the SUPERSEDED entry is on the SAME head as the
    new observation, the terminal state still suppresses the
    finding (the prior is terminal on this exact head).
    """
    from autocoder_orchestration.store import StateStore
    store = StateStore(str(tmp_path / "state"))
    head = "a" * 40
    ledger = FindingLedger(store, head_sha=head)
    f = _make_finding()
    ledger.record_observed(f)
    ledger.record_dispatched(f)
    ledger.mark_active(f)
    ledger.mark_superseded_by_head(head)
    assert ledger.is_fresh(_make_finding()) is False, (
        "Same-head SUPERSEDED is still terminal: a fresh "
        "observation on the same head is suppressed because "
        "the prior terminal state is authoritative for that "
        "head."
    )


def test_round_28_p5_repaired_silences_same_head(
    tmp_path: Path,
) -> None:
    """A REPAIRED entry on the SAME head silences.
    """
    from autocoder_orchestration.store import StateStore
    store = StateStore(str(tmp_path / "state"))
    head = "a" * 40
    ledger = FindingLedger(store, head_sha=head)
    f = _make_finding()
    ledger.record_observed(f)
    ledger.record_dispatched(f)
    ledger.mark_active(f)
    ledger.mark_repaired(f, resolution_evidence="upstream resolved")
    assert ledger.is_fresh(_make_finding()) is False


def test_round_28_p5_repaired_does_NOT_silence_new_head(
    tmp_path: Path,
) -> None:
    """A REPAIRED entry on the OLD head does NOT silence a
    fresh observation on the NEW head — same rule as
    SUPERSEDED.
    """
    from autocoder_orchestration.store import StateStore
    store = StateStore(str(tmp_path / "state"))
    old_head = "a" * 40
    new_head = "b" * 40
    ledger_a = FindingLedger(store, head_sha=old_head)
    f = _make_finding()
    ledger_a.record_observed(f)
    ledger_a.record_dispatched(f)
    ledger_a.mark_active(f)
    ledger_a.mark_repaired(f, resolution_evidence="upstream resolved")
    ledger_b = FindingLedger(store, head_sha=new_head)
    assert ledger_b.is_fresh(_make_finding()) is True, (
        "Round-28 P5: REPAIRED on the OLD head does NOT "
        "silence a fresh observation on the NEW head."
    )


def test_round_28_p5_latest_terminal_state_remains_available_for_opt_in(
    tmp_path: Path,
) -> None:
    """``latest_terminal_state`` remains as an opt-in API for
    callers that want to implement their own
    positive-evidence policy. ``is_fresh`` itself does NOT
    consult it.
    """
    from autocoder_orchestration.store import StateStore
    store = StateStore(str(tmp_path / "state"))
    old_head = "a" * 40
    new_head = "b" * 40
    ledger_a = FindingLedger(store, head_sha=old_head)
    f = _make_finding()
    ledger_a.record_observed(f)
    ledger_a.record_dispatched(f)
    ledger_a.mark_active(f)
    ledger_a.mark_superseded_by_head(old_head)
    # The API still exists and returns the SUPERSEDED row.
    ledger_b = FindingLedger(store, head_sha=new_head)
    sig = ledger_b._signature(f)
    terminal = ledger_b.latest_terminal_state(f.finding_id, sig)
    assert terminal is not None, (
        "latest_terminal_state is an opt-in API; it still "
        "returns SUPERSEDED entries across heads."
    )
    assert terminal["state"] == FINDING_STATE_SUPERSEDED
    # But ``is_fresh`` does NOT consult it — the finding is
    # FRESH on the new head.
    assert ledger_b.is_fresh(_make_finding()) is True
