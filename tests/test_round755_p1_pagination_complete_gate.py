"""Round-755/P1 regression: ``_collect_coderabbit_exact_head_surfaces`` must
require BOTH ``review_threads_pagination_complete`` AND ``not
review_threads_pagination_failed`` before reporting
``review_threads_collected=True``.

Fresh evidence after the round-686 pagination fix: when ``capture_live_snapshot``
paginates ``reviewThreads`` through ``for _ in range(6):`` and the sixth page
still reports ``hasNextPage=True``, the loop exits with both flags at their
initialized ``False`` values:

  - ``snap["review_threads_pagination_failed"] = False``
  - ``snap["review_threads_pagination_complete"] = False``

The pre-fix ``hermes_fingerprint._collect_coderabbit_exact_head_surfaces``
only consulted ``pagination_failed`` when computing
``out["review_threads_collected"]``. The 600 threads on the first six pages
were therefore reported as a fully collected surface, allowing
``surfaces_complete`` and ``clean`` to flip True on an incomplete inventory.

This regression pins the new behavior: when ``review_threads_pagination_complete``
is False (regardless of the ``failed`` flag), ``review_threads_collected``
MUST be False so ``surfaces_complete`` stays False and ``persist_coderabbit_head_assessment``
refuses to persist a clean assessment on an incomplete inventory.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from autocoder_supervisor.hermes_fingerprint import (
    _collect_coderabbit_exact_head_surfaces,
)

EXPECTED_HEAD = "f4d444f2a41711ec2a0f07a99ef75058778f9127"
HEAD_PREFIX = EXPECTED_HEAD[:7]


def _eval(snap):
    return _collect_coderabbit_exact_head_surfaces(
        snap, expected_head=EXPECTED_HEAD
    )


def _cr_surfaces(issue_comments=None):
    """Minimal complete coderabbit provider surface (no inlines)."""
    return {
        "provider": "coderabbit",
        "head_sha": EXPECTED_HEAD,
        "reviews": [
            {
                "id": "rev-1",
                "provider": "coderabbit",
                "commit_id": EXPECTED_HEAD,
                "submitted_at": "2026-08-15T20:00:00Z",
                "state": "APPROVED",
                "login": "coderabbitai[bot]",
                "body": "LGTM.",
            },
        ],
        "issue_comments": list(issue_comments or []),
        "review_comments": [],
        "check_runs": [],
    }


def _complete_status_comment():
    """Status comment carrying the exact head prefix."""
    return [
        {
            "id": 9001,
            "body": (
                f"All findings addressed. Review completed at "
                f"head `{HEAD_PREFIX}`."
            ),
            "created_at": "2026-08-15T20:01:00Z",
            "login": "coderabbitai[bot]",
        },
    ]


def _snap_with_threads(*, pagination_failed, pagination_complete,
                       thread_count):
    """Build a snap mirroring capture_live_snapshot's review_threads shape.

    ``thread_count`` threads (all resolved) populate ``review_threads`` so
    ``isinstance(threads, dict) and threads != {}`` would have let the old
    code report the surface as fully collected.
    """
    threads = {
        f"PRRT_kwTHREAD_{i:03d}": {
            "resolved": True,
            "outdated": False,
            "owner": "coderabbit",
            "path": "a.py",
            "line": 1,
            "body": "resolved comment",
            "commit_oid": EXPECTED_HEAD,
        }
        for i in range(thread_count)
    }
    return {
        "formal_reviews": [{
            "id": "rev-1", "provider": "coderabbit",
            "commit_id": EXPECTED_HEAD,
            "submitted_at": "2026-08-15T20:00:00Z",
            "state": "APPROVED",
            "login": "coderabbitai[bot]",
            "body": "LGTM.",
        }],
        "review_threads": threads,
        "review_comments": [],
        "issue_comments": _complete_status_comment(),
        "provider_surface_complete": True,
        "provider_surfaces": {
            "coderabbit": _cr_surfaces(
                issue_comments=_complete_status_comment()
            ),
        },
        "review_threads_pagination_failed": pagination_failed,
        "review_threads_pagination_complete": pagination_complete,
    }


# ===== Round-755/P1: 6-page pagination exhaustion =====

def test_pagination_exhaustion_marks_review_threads_incomplete():
    """Round-755/P1: ``capture_live_snapshot`` returns ``pagination_failed=False``
    AND ``pagination_complete=False`` when the sixth page still reports
    ``hasNextPage=True`` (loop hits the ``for _ in range(6)`` exit with
    both flags at their initialized False values).

    Pre-fix behavior: ``review_threads_collected=True`` because only
    ``pagination_failed`` was consulted. ``clean`` could then flip True
    on an inventory that the snap explicitly flagged as incomplete
    (pagination_complete=False).

    Post-fix behavior: ``review_threads_collected=False`` and
    ``clean=False``.
    """
    snap = _snap_with_threads(
        pagination_failed=False,
        pagination_complete=False,  # 6-page hasNextPage=True exhaustion
        thread_count=600,            # 6 pages × 100 threads
    )
    out = _eval(snap)
    assert out["review_threads_pagination_complete"] is False, (
        f"pagination_complete MUST propagate to the surface dict; "
        f"got {out!r}"
    )
    assert out["review_threads_collected"] is False, (
        f"pagination_complete=False MUST force review_threads_collected=False "
        f"regardless of thread dict shape; got {out!r}"
    )
    assert out["clean"] is False, (
        f"clean MUST be False on an incomplete inventory; got {out!r}"
    )


def test_pagination_failed_with_threads_marks_incomplete():
    """Round-686/P1 regression: a paginated fetch failure leaves
    ``pagination_failed=True`` and ``pagination_complete=False``. The
    pre-existing ``not pagination_failed`` check already handles this;
    pin it so future refactors don't regress.
    """
    snap = _snap_with_threads(
        pagination_failed=True,
        pagination_complete=False,
        thread_count=300,  # some threads made it into the dict
    )
    out = _eval(snap)
    assert out["review_threads_pagination_complete"] is False
    assert out["review_threads_collected"] is False
    assert out["clean"] is False


def test_pagination_complete_with_threads_marks_collected():
    """Happy path: when ``capture_live_snapshot`` reports
    ``pagination_complete=True`` (last page had ``hasNextPage=False``),
    ``review_threads_collected`` MUST be True. Pin so we don't over-correct
    in the opposite direction.
    """
    snap = _snap_with_threads(
        pagination_failed=False,
        pagination_complete=True,
        thread_count=42,
    )
    out = _eval(snap)
    assert out["review_threads_pagination_complete"] is True
    assert out["review_threads_collected"] is True
    # The other surfaces (statuses, formal reviews, top-level comment)
    # are populated by the snap; verify the inventory is otherwise clean.
    assert out["statuses_collected"] is True
    assert out["formal_reviews_collected"] is True
    assert out["top_level_comment_collected"] is True
    assert out["actionable_finding_ids"] == []
    assert out["clean"] is True


def test_pagination_complete_defaults_to_true_when_absent():
    """Backwards compatibility: pre-116 snapshots did not set
    ``review_threads_pagination_complete``. The collector MUST default
    it to True so legacy snapshots keep their existing ``review_threads_collected``
    semantics until proven otherwise. This guards against accidentally
    flipping all legacy inventories to incomplete.
    """
    snap = _snap_with_threads(
        pagination_failed=False,
        pagination_complete=True,  # the snap field IS set
        thread_count=5,
    )
    # Drop the explicit flag entirely to mimic the legacy shape.
    snap.pop("review_threads_pagination_complete", None)
    out = _eval(snap)
    assert out["review_threads_pagination_complete"] is True, (
        f"missing review_threads_pagination_complete MUST default to True "
        f"(legacy snapshots carry no flag); got {out!r}"
    )
    assert out["review_threads_collected"] is True
    assert out["clean"] is True


def test_pagination_exhaustion_does_not_persist_clean_assessment(
    monkeypatch,
):
    """Round-755/P1 end-to-end: a snapshot where the pagination loop
    exhausted (no error, but ``pagination_complete=False``) MUST be
    refused by ``persist_coderabbit_head_assessment``. The writer
    enforces this by checking ``surfaces_complete``, which is False
    because ``review_threads_collected=False``.

    Use ``monkeypatch`` to redirect ``state_dir`` to a writable temp
    directory (the harness pins ``/tmp/autodev-pytest`` as a directory,
    not a per-test root).
    """
    import tempfile
    from autocoder_supervisor.hermes_fingerprint import (
        persist_coderabbit_head_assessment,
    )
    with tempfile.TemporaryDirectory() as td:
        snap = _snap_with_threads(
            pagination_failed=False,
            pagination_complete=False,
            thread_count=600,
        )
        out = persist_coderabbit_head_assessment(
            snap=snap,
            state_dir=td,
            expected_head=EXPECTED_HEAD,
            now_iso_fn=lambda: "2026-08-15T20:05:00+00:00",
        )
        # ``written_path`` MUST be None when observation_complete is False
        # (the surfaces_complete gate already exists in the writer).
        assert out.get("written_path") is None, (
            f"writer MUST refuse to persist on pagination exhaustion; "
            f"got out={out!r}"
        )
        from pathlib import Path
        artifact_path = (
            Path(td) / "provider_head_assessment" / "coderabbit"
            / f"{EXPECTED_HEAD}.json"
        )
        assert not artifact_path.exists(), (
            f"writer MUST NOT create the canonical artifact on incomplete "
            f"inventory; found {artifact_path}"
        )