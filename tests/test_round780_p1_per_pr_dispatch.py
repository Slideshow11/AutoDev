"""Round-780 P1 regression tests.

The single P1 finding for this directive was:
    "Dispatch each secondary PR under its own globals"

The post-loop dispatch site used a single
``handle_new_events(rs, new_events, token, iteration)`` call
after the per-PR tick loop had restored ``PR_NUMBER``,
``AUTHORITATIVE_HEAD``, ``rs``, and ``iteration`` to the
canonical PR. Secondary PR events were tagged with their
PR's ``head_sha`` and ``pr_number`` (round-779), but
``handle_new_events`` -> ``_invoke_relay_for_events`` reads
the singleton ``AUTHORITATIVE_HEAD`` / ``PR_NUMBER``
globals and ``revoke_readiness`` /
``_reopen_qualifying_head_if_needed`` /
``_persist_round_budget_retry`` all fall back to
``iteration.get("head_sha")``. Every one of those keys was
mis-attributed to the canonical PR for secondary events,
so the relay captured a snapshot against the canonical
PR's state root and the readiness gate persisted the
canonical PR's head_sha.

Round-780 introduces ``_dispatch_events_per_pr`` which
partitions ``new_events`` by ``pr_number`` and dispatches
each partition under that PR's own ``PR_NUMBER`` /
``AUTHORITATIVE_HEAD`` / ``iteration.head_sha``, then
restores the canonical globals in a ``finally`` block.

P1 finding coverage:

  #1  secondary PR events are dispatched under their own
      ``PR_NUMBER`` + ``AUTHORITATIVE_HEAD`` + per-PR
      ``iteration.head_sha`` (canonical events still
      dispatch under canonical globals).
  #2  ``_dispatch_events_per_pr`` restores canonical
      globals in a ``finally`` so a dispatch exception in
      one partition cannot leak the secondary PR's globals
      into post-loop logic.
  #3  events without a ``pr_number`` attribute default to
      the canonical PR's partition (durable-thread drain,
      provenance drift, pre-round-779 unconsumed events).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch as _patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR_PATH = REPO_ROOT / "autocoder_supervisor" / "supervisor.py"


def _load_supervisor():
    """Load the supervisor module via the canonical package
    path so the standalone shim is bypassed. The
    ``AED_SKIP_IDENTITY_GUARD=1`` env is required by the
    supervisor's launch identity guard, but
    ``_dispatch_events_per_pr`` does not call
    ``launch_worker`` directly, so we just monkeypatch the
    ``handle_new_events`` symbol."""
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from autocoder_supervisor import supervisor as sup
    finally:
        sys.path.pop(0)
    return sup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingHandle:
    """Mock replacement for ``handle_new_events`` that
    records the arguments of every invocation so tests can
    verify the per-PR dispatch contract."""

    def __init__(self, sup_module):
        self.sup_module = sup_module
        self.calls: list[dict[str, Any]] = []
        # Raise on the secondary partition to verify
        # ``_dispatch_events_per_pr`` restores canonical
        # globals in the ``finally`` block.
        self.raise_on_pr: int | None = None

    def __call__(self, rs, new_events, token, iteration):
        # ``handle_new_events`` is defined in the
        # supervisor module, so its ``globals()`` returns
        # ``sup_module.__dict__``. We read PR_NUMBER /
        # AUTHORITATIVE_HEAD from THAT namespace -- the
        # values the dispatcher actually mutated.
        sup_globals = self.sup_module.__dict__
        snap = {
            "pr_number": sup_globals.get("PR_NUMBER"),
            "authoritative_head": sup_globals.get(
                "AUTHORITATIVE_HEAD", "",
            ),
            "rs_id": id(rs),
            "event_ids": [
                e.get("id") for e in (new_events or [])
                if isinstance(e, dict)
            ],
            "iteration_head_sha": (
                (iteration or {}).get("head_sha")
            ),
            "iteration_head_match": (
                (iteration or {}).get("head_match")
            ),
            "iteration_decision": (
                (iteration or {}).get("decision")
            ),
        }
        self.calls.append(snap)
        if self.raise_on_pr is not None and (
            sup_globals.get("PR_NUMBER") == self.raise_on_pr
        ):
            raise RuntimeError(
                "synthetic dispatch failure for "
                f"PR {self.raise_on_pr}"
            )


def _event(eid: str, pr_number: int | None) -> dict:
    ev: dict[str, Any] = {"id": eid, "kind": "review_thread"}
    if pr_number is not None:
        ev["pr_number"] = int(pr_number)
    return ev


# ---------------------------------------------------------------------------
# P1 #1: secondary PR events dispatch under their own globals
# ---------------------------------------------------------------------------


def test_p1_01_secondary_pr_dispatches_under_own_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-780 P1#1: secondary PR events must dispatch
    under their own ``PR_NUMBER`` / ``AUTHORITATIVE_HEAD``
    / per-PR ``iteration.head_sha``. Canonical events
    dispatch under canonical globals. Each partition is a
    separate ``handle_new_events`` invocation."""
    sup = _load_supervisor()
    recorder = _RecordingHandle(sup)
    monkeypatch.setattr(sup, "handle_new_events", recorder)
    monkeypatch.setattr(sup, "log", lambda *a, **kw: None)

    # Globals BEFORE dispatch -- the canonical PR is bound.
    monkeypatch.setattr(sup, "PR_NUMBER", 5)
    monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", "c" * 40)

    canonical_iter = {
        "head_sha": "c" * 40,
        "head_match": True,
        "decision": "head_match",
    }
    secondary_head = "d" * 40
    per_pr_iterations = [
        {
            "pr_number": 7,
            "head_sha": secondary_head,
            "head_match": False,
            "decision": "head_mismatch",
            "events": [],
        },
    ]
    events = [
        _event("ev-canonical-1", pr_number=5),
        _event("ev-secondary-1", pr_number=7),
        _event("ev-canonical-2", pr_number=5),
        _event("ev-secondary-2", pr_number=7),
    ]
    sup._dispatch_events_per_pr(
        events,
        token="t",
        canonical_pr=5,
        canonical_iteration=canonical_iter,
        canonical_rs={"current_head": "c" * 40},
        per_pr_iterations=per_pr_iterations,
    )
    assert len(recorder.calls) == 2, (
        "round-780 P1#1: dispatcher MUST produce one "
        "handle_new_events call per PR partition; got "
        f"{len(recorder.calls)} (calls: {recorder.calls})"
    )
    by_pr = {c["pr_number"]: c for c in recorder.calls}
    # Canonical first.
    assert 5 in by_pr, "canonical PR partition MUST dispatch"
    assert by_pr[5]["authoritative_head"] == "c" * 40, (
        "round-780 P1#1: canonical partition MUST dispatch "
        f"under canonical AUTHORITATIVE_HEAD, got "
        f"{by_pr[5]['authoritative_head']!r}"
    )
    assert by_pr[5]["iteration_head_sha"] == "c" * 40
    assert sorted(by_pr[5]["event_ids"]) == [
        "ev-canonical-1", "ev-canonical-2",
    ]
    # Secondary under its OWN globals.
    assert 7 in by_pr, (
        "secondary PR partition MUST dispatch; got "
        f"calls={list(by_pr)}"
    )
    assert by_pr[7]["authoritative_head"] == secondary_head, (
        "round-780 P1#1: secondary partition MUST dispatch "
        f"under the secondary PR's AUTHORITATIVE_HEAD, got "
        f"{by_pr[7]['authoritative_head']!r}"
    )
    assert by_pr[7]["pr_number"] == 7
    assert by_pr[7]["iteration_head_sha"] == secondary_head
    assert by_pr[7]["iteration_head_match"] is False
    assert by_pr[7]["iteration_decision"] == "head_mismatch"
    assert sorted(by_pr[7]["event_ids"]) == [
        "ev-secondary-1", "ev-secondary-2",
    ]
    # Globals restored to canonical after dispatch. The
    # helper mutates the supervisor module's globals, not
    # the test script's globals.
    assert sup.__dict__.get("PR_NUMBER") == 5, (
        "round-780 P1#1: canonical PR_NUMBER MUST be "
        "restored after dispatch"
    )
    assert sup.__dict__.get("AUTHORITATIVE_HEAD") == "c" * 40, (
        "round-780 P1#1: canonical AUTHORITATIVE_HEAD MUST "
        "be restored after dispatch"
    )


def test_p1_01_dispatch_order_canonical_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-780 P1#1: canonical partition MUST dispatch
    first; secondary partitions follow in the
    ``per_pr_iterations`` order. The post-loop logic (head
    rebind, qualifying-readiness promotion) depends on the
    canonical globals being restored after each dispatch."""
    sup = _load_supervisor()
    recorder = _RecordingHandle(sup)
    monkeypatch.setattr(sup, "handle_new_events", recorder)
    monkeypatch.setattr(sup, "log", lambda *a, **kw: None)
    monkeypatch.setattr(sup, "PR_NUMBER", 5)
    monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", "c" * 40)

    sup._dispatch_events_per_pr(
        [
            _event("ev-5", pr_number=5),
            _event("ev-9", pr_number=9),
            _event("ev-7", pr_number=7),
        ],
        token="t",
        canonical_pr=5,
        canonical_iteration={"head_sha": "c" * 40},
        canonical_rs={},
        per_pr_iterations=[
            {"pr_number": 9, "head_sha": "e" * 40, "events": []},
            {"pr_number": 7, "head_sha": "d" * 40, "events": []},
        ],
    )
    assert [c["pr_number"] for c in recorder.calls] == [5, 9, 7], (
        "round-780 P1#1: canonical PR MUST dispatch first, "
        "then secondaries in per_pr_iterations order; got "
        f"{[c['pr_number'] for c in recorder.calls]}"
    )


# ---------------------------------------------------------------------------
# P1 #2: canonical globals restored in ``finally`` after dispatch exception
# ---------------------------------------------------------------------------


def test_p1_02_canonical_globals_restored_after_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-780 P1#2: a dispatch exception in one
    partition MUST NOT leak that PR's globals into post-loop
    logic. The ``finally`` block restores canonical
    ``PR_NUMBER`` and ``AUTHORITATIVE_HEAD``."""
    sup = _load_supervisor()
    recorder = _RecordingHandle(sup)
    recorder.raise_on_pr = 7  # raise on the secondary dispatch
    monkeypatch.setattr(sup, "handle_new_events", recorder)
    monkeypatch.setattr(sup, "log", lambda *a, **kw: None)
    monkeypatch.setattr(sup, "PR_NUMBER", 5)
    monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", "c" * 40)

    sup._dispatch_events_per_pr(
        [
            _event("ev-c", pr_number=5),
            _event("ev-s", pr_number=7),
        ],
        token="t",
        canonical_pr=5,
        canonical_iteration={"head_sha": "c" * 40},
        canonical_rs={},
        per_pr_iterations=[
            {"pr_number": 7, "head_sha": "d" * 40, "events": []},
        ],
    )
    # Canonical dispatch ran; secondary raised; globals are
    # restored to canonical.
    assert sup.__dict__.get("PR_NUMBER") == 5, (
        "round-780 P1#2: PR_NUMBER MUST be restored to "
        "canonical after a dispatch exception in a secondary "
        "partition"
    )
    assert sup.__dict__.get("AUTHORITATIVE_HEAD") == "c" * 40, (
        "round-780 P1#2: AUTHORITATIVE_HEAD MUST be restored "
        "to canonical after a dispatch exception in a "
        "secondary partition"
    )


# ---------------------------------------------------------------------------
# P1 #3: events without ``pr_number`` default to canonical partition
# ---------------------------------------------------------------------------


def test_p1_03_events_without_pr_number_route_to_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-780 P1#3: events without a ``pr_number``
    attribute (pre-round-779 unconsumed events, synthetic
    durable-thread drain, provenance-drift) MUST default to
    the canonical PR's partition."""
    sup = _load_supervisor()
    recorder = _RecordingHandle(sup)
    monkeypatch.setattr(sup, "handle_new_events", recorder)
    monkeypatch.setattr(sup, "log", lambda *a, **kw: None)
    monkeypatch.setattr(sup, "PR_NUMBER", 5)
    monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", "c" * 40)

    sup._dispatch_events_per_pr(
        [
            _event("ev-untagged", pr_number=None),
            _event("ev-canonical", pr_number=5),
        ],
        token="t",
        canonical_pr=5,
        canonical_iteration={"head_sha": "c" * 40},
        canonical_rs={},
        per_pr_iterations=[],
    )
    # Untagged + canonical both land in the canonical call.
    assert len(recorder.calls) == 1, (
        "round-780 P1#3: untagged + canonical events MUST "
        "share the canonical partition; got "
        f"{len(recorder.calls)} dispatch calls"
    )
    assert recorder.calls[0]["pr_number"] == 5
    assert sorted(recorder.calls[0]["event_ids"]) == [
        "ev-canonical", "ev-untagged",
    ]


def test_p1_03_empty_events_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-780 P1#3: an empty event list is a no-op; the
    dispatcher MUST NOT call ``handle_new_events``."""
    sup = _load_supervisor()
    recorder = _RecordingHandle(sup)
    monkeypatch.setattr(sup, "handle_new_events", recorder)
    monkeypatch.setattr(sup, "log", lambda *a, **kw: None)
    sup._dispatch_events_per_pr(
        [],
        token="t",
        canonical_pr=5,
        canonical_iteration={},
        canonical_rs={},
        per_pr_iterations=[],
    )
    assert recorder.calls == [], (
        "round-780 P1#3: empty events list MUST NOT trigger "
        "any dispatch calls"
    )


# ---------------------------------------------------------------------------
# Source-tree guard: the call site uses the helper, not the single-shot form
# ---------------------------------------------------------------------------


def test_source_call_site_uses_per_pr_helper() -> None:
    """Round-780 source guard: the post-loop dispatch site
    MUST call ``_dispatch_events_per_pr`` instead of the
    single-shot ``handle_new_events(rs, new_events, token,
    iteration)`` form. Regressing to the single-shot form
    is the round-780 defect."""
    text = SUPERVISOR_PATH.read_text(encoding="utf-8")
    # Both call sites are inside the post-loop block.
    assert text.count(
        "_dispatch_events_per_pr("
    ) >= 2, (
        "round-780 source guard: expected at least two "
        "_dispatch_events_per_pr call sites (new_events "
        "and replayed payloads)"
    )
    # The legacy single-shot form must NOT survive.
    legacy = (
        "handle_new_events(\n"
        "                                rs, new_events, "
        "token, iteration\n"
        "                            )"
    )
    assert legacy not in text, (
        "round-780 source guard: legacy single-shot "
        "handle_new_events(rs, new_events, token, "
        "iteration) MUST NOT survive"
    )
