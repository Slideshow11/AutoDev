"""Round-589 P2 regression: list_open_drifts must fail closed.

The drift ledger reader at
``autocoder_supervisor.provenance_maintenance.list_open_drifts``
previously wrapped ``json.loads`` in a bare ``except`` clause
that returned ``[]`` on any read or parse error. That silently
masked a missing/malformed ledger as "no open drift", which
the supervisor's ``handle_new_events`` then took at face value —
leaving a persistently truncated ledger unable to emit its
provenance-repair events and stalling the autonomous
maintenance lifecycle indefinitely.

The fix delegates to the canonical
``_read_drift_ledger_or_failclosed`` helper (which raises
``AtomicWriteError``) so an unreadable ledger surfaces as a
fail-closed condition in the supervisor's existing
``except Exception`` envelope.

This test pins:
  1. A missing ledger still returns ``[]`` (no change).
  2. A malformed JSON ledger raises ``AtomicWriteError``
     (NEW behaviour — previously returned ``[]``).
  3. An unreadable ledger (e.g. directory at the ledger
     path) raises ``AtomicWriteError``.
  4. A non-list root raises ``AtomicWriteError``.
  5. A well-formed list still returns only non-terminal,
     non-superseded entries.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


def _import():
    # ``autocoder_supervisor`` is the package this repo ships.
    from autocoder_supervisor.provenance_maintenance import (
        AtomicWriteError,
        list_open_drifts,
    )
    return list_open_drifts, AtomicWriteError


def test_missing_ledger_returns_empty(tmp_path: Path) -> None:
    list_open_drifts, _ = _import()
    p = tmp_path / "provenance_drift_pending.json"
    assert list_open_drifts(ledger_path=p) == []


def test_malformed_ledger_raises_atomicwriteerror(tmp_path: Path) -> None:
    list_open_drifts, AtomicWriteError = _import()
    p = tmp_path / "provenance_drift_pending.json"
    p.write_text("{not valid json")
    with pytest.raises(AtomicWriteError):
        list_open_drifts(ledger_path=p)


def test_unreadable_ledger_raises_atomicwriteerror(tmp_path: Path) -> None:
    list_open_drifts, AtomicWriteError = _import()
    p = tmp_path / "provenance_drift_pending.json"
    # A directory at the ledger path makes read_text() raise
    # OSError on Linux. The fail-closed contract MUST surface
    # this as AtomicWriteError, NOT as an empty list.
    p.mkdir()
    with pytest.raises(AtomicWriteError):
        list_open_drifts(ledger_path=p)


def test_non_list_ledger_raises_atomicwriteerror(tmp_path: Path) -> None:
    list_open_drifts, AtomicWriteError = _import()
    p = tmp_path / "provenance_drift_pending.json"
    p.write_text(json.dumps({"not": "a list"}))
    with pytest.raises(AtomicWriteError):
        list_open_drifts(ledger_path=p)


def test_valid_ledger_filters_terminal_and_superseded(
    tmp_path: Path,
) -> None:
    list_open_drifts, _ = _import()
    p = tmp_path / "provenance_drift_pending.json"
    p.write_text(json.dumps([
        {
            "state": "DRIFT_DETECTED",
            "head_sha": "abc",
            "attempt_id": "a1",
        },
        {
            "state": "TERMINAL",
            "head_sha": "xyz",
            "attempt_id": "a2",
        },
        {
            "state": "SUPERSEDED",
            "head_sha": "def",
            "attempt_id": "a3",
        },
    ]))
    open_drifts = list_open_drifts(ledger_path=p)
    assert len(open_drifts) == 1
    assert open_drifts[0]["head_sha"] == "abc"


def test_empty_ledger_returns_empty(tmp_path: Path) -> None:
    list_open_drifts, _ = _import()
    p = tmp_path / "provenance_drift_pending.json"
    p.write_text("[]")
    assert list_open_drifts(ledger_path=p) == []


def test_ledger_with_null_entry_raises_atomicwriteerror(
    tmp_path: Path,
) -> None:
    """Round-679 P2: a ``[null]`` ledger must fail closed.

    Previously the ``isinstance(rec, dict)`` filter inside
    ``list_open_drifts`` silently discarded non-object entries,
    so the supervisor's ``handle_new_events`` observed an empty
    drift list and emitted no repair event. The maintenance
    lifecycle then stalled indefinitely on a corrupted ledger.
    """
    list_open_drifts, AtomicWriteError = _import()
    p = tmp_path / "provenance_drift_pending.json"
    p.write_text(json.dumps([None]))
    with pytest.raises(AtomicWriteError):
        list_open_drifts(ledger_path=p)


def test_ledger_with_string_entry_raises_atomicwriteerror(
    tmp_path: Path,
) -> None:
    """Round-679 P2: a ``["corrupt"]`` ledger must fail closed.

    Same fail-closed contract as ``test_ledger_with_null_entry_raises_atomicwriteerror``
    — string entries must NOT silently disappear.
    """
    list_open_drifts, AtomicWriteError = _import()
    p = tmp_path / "provenance_drift_pending.json"
    p.write_text(json.dumps(["corrupt"]))
    with pytest.raises(AtomicWriteError):
        list_open_drifts(ledger_path=p)


def test_ledger_with_mixed_dict_and_non_dict_raises(
    tmp_path: Path,
) -> None:
    """Round-679 P2: any single non-object entry poisons the ledger.

    A list with a valid dict followed by a corrupt scalar MUST
    fail closed; we do NOT silently keep the valid dict and drop
    the bad one — the helper is fail-closed on the whole ledger.
    """
    list_open_drifts, AtomicWriteError = _import()
    p = tmp_path / "provenance_drift_pending.json"
    p.write_text(json.dumps([
        {"state": "DRIFT_DETECTED", "head_sha": "abc"},
        "corrupt",
    ]))
    with pytest.raises(AtomicWriteError):
        list_open_drifts(ledger_path=p)
