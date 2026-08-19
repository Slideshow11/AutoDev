"""Round-687/P1: head-bound provider issue-comment surface is consumed.

Contract
--------
Production ``capture_live_snapshot()`` records in
``snap["_provider_issue_comments"][provider]`` are RAW GitHub
issue-comment dicts. They carry only a top-level ``login`` field
(no ``commit_id`` / ``review_cycle`` provenance) because GitHub's
``/issues/{N}/comments`` endpoint does not stamp those.

The head-bound copies live in
``snap["provider_surfaces"][provider]["issue_comments"]``. Those
records DO carry ``commit_id`` (== ``head_sha``) and ``review_cycle``
(``provider:head_sha:freshest_cid``) for the freshest bot-authored
comment in the current review cycle (Round-31/140 binding).

Round-687/P1: ``_collect_review_findings`` MUST prefer the head-bound
surface when present. Reading the raw subset makes the primary loop
reject every such record (the head-binding check at lines 1252-1271
requires EITHER ``commit_id == head_sha`` OR a non-empty
``review_cycle`` — neither exists on raw records).

Tests pin four behavioral contracts:

A. Head-bound surface records WITH ``commit_id`` /
   ``review_cycle`` → consumed → actionable Finding emitted.
B. Raw subset records WITHOUT ``commit_id`` /
   ``review_cycle`` → ignored when head-bound surface also
   exists for that provider (primary loop must not double-emit
   stale/head-less records).
C. Raw subset records WITH ``commit_id`` /
   ``review_cycle`` → still consumed when no head-bound surface
   is present (legacy test path / pre-round-666 snapshots).
D. Raw subset records alone → still consumed for backward
   compatibility (legacy fixtures populate only
   ``_provider_issue_comments``).
"""
from __future__ import annotations

import pytest

from autocoder_orchestration.review_repair_relay import (
    Finding,
    _collect_review_findings,
)


HEAD = "a" * 40


def _directive_body(
    *,
    provider: str = "coderabbit",
    cid: int = 1,
) -> str:
    """Body that exercises P1 severity + an actionable anchor at
    ``autocoder_orchestration/review_repair_relay.py:1221``.

    The body intentionally avoids URLs in the prefix so the
    collector's ``_extract_anchor`` regex does not pick up a
    stray ``img.sh`` token. Body length > 200 chars keeps
    ``_is_actionable_provider_comment`` happy (round-29 rule:
    short bodies with status markers are dropped).
    """
    return (
        f"Round-687 P1: consume the head-bound provider "
        f"issue-comment surface for {provider} cid {cid}\n"
        "When an actionable finding exists only in a provider "
        "issue comment, production capture_live_snapshot() puts "
        "raw records with top-level login and no commit_id / "
        "review_cycle in _provider_issue_comments, while the "
        "head-bound versions are stored under provider_surfaces. "
        "Selecting the raw index here makes the primary loop "
        "reject every such record because the head-binding check "
        "requires either commit_id matching head_sha or a "
        "non-empty review_cycle. The fix is to read from "
        "provider_surfaces first and fall back to the raw subset "
        "only when the surfaces entry is absent for that provider. "
        "See file: autocoder_orchestration/review_repair_relay.py:1221"
    )


def _raw_comment(*, cid: int, body: str, login: str = "coderabbitai[bot]") -> dict:
    """Raw GitHub issue-comment shape — login only, no head binding."""
    return {
        "id": cid,
        "user": {"login": login},
        "created_at": "2026-08-16T00:00:00Z",
        "body": body,
    }


def _head_bound_comment(*, cid: int, body: str, head: str = HEAD) -> dict:
    """Head-bound surface record — stamped with commit_id + review_cycle."""
    return {
        "id": cid,
        "user": {"login": "coderabbitai[bot]"},
        "created_at": "2026-08-16T00:00:00Z",
        "body": body,
        "commit_id": head,
        "review_cycle": f"coderabbit:{head}:{cid}",
    }


def _snap_with_surfaces(
    *,
    raw_subset: dict,
    surfaces: dict,
    head: str = HEAD,
) -> dict:
    return {
        "captured_at": "2026-08-16T00:00:00Z",
        "head_sha": head,
        "head_match": True,
        "mergeable": True,
        "formal_reviews": [],
        "review_threads": {},
        "issue_comments": [],
        "required_checks": {},
        "providers": {},
        "_provider_issue_comments": raw_subset,
        "unconsumed_event_ids": [],
        "provider_surfaces": surfaces,
        "review_comments": [],
        "provider_surface_complete": True,
    }


def test_head_bound_surface_with_provenance_is_consumed() -> None:
    """A. Head-bound records are consumed and become actionable Findings."""
    snap = _snap_with_surfaces(
        raw_subset={"coderabbit": [_raw_comment(
            cid=1,
            body=_directive_body(),
        )]},
        surfaces={
            "coderabbit": {
                "provider": "coderabbit",
                "head_sha": HEAD,
                "reviews": [{"id": 99}],
                "issue_comments": [_head_bound_comment(
                    cid=1,
                    body=_directive_body(),
                )],
                "review_comments": [],
                "check_runs": [],
            },
        },
    )
    findings = _collect_review_findings(snap)
    cr = [f for f in findings if f.source == "coderabbit"]
    assert cr, (
        f"expected a coderabbit Finding from head-bound surface; "
        f"got sources: {[f.source for f in findings]!r}"
    )
    # The finding MUST point at the line the directive targets.
    assert cr[0].file_path == "autocoder_orchestration/review_repair_relay.py"
    assert cr[0].line == 1221


def test_raw_subset_alone_is_ignored_when_head_bound_surface_exists() -> None:
    """B. Raw subset without provenance is ignored when head-bound
    surface exists for the same provider — primary loop must not
    emit stale/head-less records."""
    snap = _snap_with_surfaces(
        raw_subset={"coderabbit": [_raw_comment(
            cid=1,
            body=_directive_body(),
        )]},
        surfaces={
            "coderabbit": {
                "provider": "coderabbit",
                "head_sha": HEAD,
                "reviews": [],
                "issue_comments": [],
                "review_comments": [],
                "check_runs": [],
            },
        },
    )
    findings = _collect_review_findings(snap)
    # Primary loop MUST NOT emit the raw record because the
    # head-bound surface exists (even though empty) — the primary
    # loop only reads head-bound records for that provider.
    assert findings == [], (
        f"raw subset must NOT be consumed when head-bound surface "
        f"exists; got findings: {[f.finding_id for f in findings]!r}"
    )


def test_legacy_subset_with_provenance_is_consumed_when_no_surface() -> None:
    """C. Legacy fixtures that stamp ``commit_id`` /
    ``review_cycle`` directly into ``_provider_issue_comments`` and
    leave ``provider_surfaces`` empty are still consumed
    (backward compatibility with pre-round-666 fixtures)."""
    snap = _snap_with_surfaces(
        raw_subset={"coderabbit": [{
            "id": 1,
            "user": {"login": "coderabbitai[bot]"},
            "created_at": "2026-08-16T00:00:00Z",
            "body": _directive_body(),
            "commit_id": HEAD,
            "review_cycle": f"coderabbit:{HEAD}:1",
        }]},
        surfaces={},
    )
    findings = _collect_review_findings(snap)
    assert findings, "legacy subset with provenance must still be consumed"
    assert findings[0].source == "coderabbit"
    assert findings[0].file_path == "autocoder_orchestration/review_repair_relay.py"


def test_raw_subset_alone_is_consumed_for_backward_compatibility() -> None:
    """D. Raw subset alone (no provider_surfaces entry, no
    provenance fields) is consumed via the fallback path so
    fixtures that only populate ``_provider_issue_comments``
    keep working."""
    snap = _snap_with_surfaces(
        raw_subset={"coderabbit": [{
            "id": 1,
            "user": {"login": "coderabbitai[bot]"},
            "created_at": "2026-08-16T00:00:00Z",
            "body": _directive_body(),
            "commit_id": HEAD,
            "review_cycle": f"coderabbit:{HEAD}:1",
        }]},
        surfaces={},
    )
    findings = _collect_review_findings(snap)
    assert findings, "subset-only fixture with provenance must still be consumed"


def test_unbound_raw_records_are_dropped_even_when_surface_absent() -> None:
    """B'. Raw records WITHOUT provenance MUST be dropped even
    when no provider_surfaces entry exists — preserves the
    round-31 invariant that head-A chatter cannot reappear on
    head-B."""
    snap = _snap_with_surfaces(
        raw_subset={"coderabbit": [_raw_comment(
            cid=1,
            body=_directive_body(),
        )]},
        surfaces={},
    )
    findings = _collect_review_findings(snap)
    assert findings == [], (
        f"unbound raw records must not survive; got: "
        f"{[f.finding_id for f in findings]!r}"
    )


def test_raw_subset_surfaces_isolated_per_provider() -> None:
    """E. Head-bound surface for provider A does NOT shadow raw
    subset entries for provider B (and vice versa)."""
    snap = _snap_with_surfaces(
        raw_subset={
            "codex": [{
                "id": 7,
                "user": {"login": "codex[bot]"},
                "created_at": "2026-08-16T00:00:00Z",
                "body": _directive_body(provider="codex", cid=7),
                "commit_id": HEAD,
                "review_cycle": f"codex:{HEAD}:7",
            }],
        },
        surfaces={
            "coderabbit": {
                "provider": "coderabbit",
                "head_sha": HEAD,
                "reviews": [{"id": 99}],
                "issue_comments": [_head_bound_comment(
                    cid=1,
                    body=_directive_body(provider="coderabbit", cid=1),
                )],
                "review_comments": [],
                "check_runs": [],
            },
        },
    )
    findings = _collect_review_findings(snap)
    sources = sorted({f.source for f in findings})
    assert sources == ["coderabbit", "codex"], (
        f"both providers should produce findings; got sources={sources!r}"
    )


def test_non_object_surface_entries_are_skipped() -> None:
    """F. provider_surfaces entries that are not dicts (defensive
    against malformed snapshots) do not crash the collector; the
    raw subset path still works."""
    snap = _snap_with_surfaces(
        raw_subset={"coderabbit": [{
            "id": 1,
            "user": {"login": "coderabbitai[bot]"},
            "created_at": "2026-08-16T00:00:00Z",
            "body": _directive_body(),
            "commit_id": HEAD,
            "review_cycle": f"coderabbit:{HEAD}:1",
        }]},
        surfaces={"coderabbit": "not-a-dict"},  # malformed
    )
    findings = _collect_review_findings(snap)
    assert findings, "subset fallback must survive malformed surface entries"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))