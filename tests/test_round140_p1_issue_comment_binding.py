"""Round-140 P1 regression tests.

The single P1 finding for this directive was:
    "Bind issue comments using evidence the endpoint provides"

Production responses from ``/issues/{PR}/comments`` never expose
``commit_id``/``commit_oid``, so the round-32 gate
``isinstance(c.get("commit_id"), str) and c.get("commit_id") == head_sha``
was unreachable on the production path. The fix binds issue
comments to the current head using evidence THIS endpoint DOES
expose:
  (a) ``surfaces["reviews"]`` is the head-bound set of formal
      reviews from this provider (already filtered to
      ``commit_id == head_sha``), so its presence is positive
      evidence that this provider has a live review cycle on the
      current head, AND
  (b) the issue comment is the MOST RECENT bot-authored issue
      comment we observed for this provider on this PR.

P1 finding coverage:
  #1  issue-comment binding no longer relies on the unreachable
      ``commit_id`` field; the freshest bot-authored comment is
      bound to the current head when a head-bound review cycle
      exists.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR_PATH = REPO_ROOT / "autocoder_supervisor" / "supervisor.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_github_get(comments_by_page, reviews=None):
    """Build a fake ``github_get`` that returns issue comments
    by page and an optional reviews list for the head-bound
    formal reviews fetch."""

    def _fake(path: str, token: str = "") -> Any:
        if "/pulls/" in path and "/reviews" in path:
            return reviews if reviews is not None else []
        if "/issues/" in path and "/comments" in path:
            # Anchor on `&page=N` so ``per_page=100`` (which
            # contains the substring ``page=1``) does NOT
            # match a page-1 query by accident.
            for page_idx, payload in comments_by_page.items():
                marker = f"&page={page_idx}"
                if marker in path:
                    return payload
            return None
        return None

    return _fake


# ---------------------------------------------------------------------------
# P1#1 — issue-comment binding uses endpoint-visible evidence
# ---------------------------------------------------------------------------


def _load_collect_provider_surfaces():
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from autocoder_supervisor import supervisor as sup
    finally:
        sys.path.pop(0)
    return sup


def test_p1_01_freshest_bot_comment_is_bound_when_head_review_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-140 P1#1: with a head-bound formal review from
    this provider, the freshest bot-authored issue comment MUST
    be bound to ``head_sha`` (the previous gate relied on a
    ``commit_id`` field the endpoint never exposes).

    Setup: codex provider (``use_reviews_api=True``) has one
    head-bound review. Two bot-authored issue comments on page 1
    with ids ``[200, 201]`` — the loop sees them in reverse
    order (``reversed(comments)``), so the freshest is ``cid=201``.
    That comment MUST receive ``commit_id == head_sha`` AND a
    populated ``review_cycle``.
    """
    sup = _load_collect_provider_surfaces()
    head_sha = "f" * 40
    bot_login = "chatgpt-codex-connector[bot]"

    fake_reviews = [
        {
            "id": 900,
            "commit_id": head_sha,
            "user": {"login": bot_login},
            "submitted_at": "2026-08-12T22:00:00Z",
            "state": "COMMENTED",
            "body": "review",
        }
    ]
    fake_comments_page1 = [
        {
            "id": 200,
            "user": {"login": bot_login},
            "created_at": "2026-08-12T20:00:00Z",
            "body": "older bot comment",
        },
        {
            "id": 201,
            "user": {"login": bot_login},
            "created_at": "2026-08-12T22:50:00Z",
            "body": "freshest bot comment",
        },
    ]

    monkeypatch.setattr(
        sup,
        "github_get",
        _stub_github_get(
            comments_by_page={1: fake_comments_page1},
            reviews=fake_reviews,
        ),
    )

    surfaces = sup.collect_provider_surfaces(
        "codex", head_sha, "fake-token"
    )

    by_id = {c["id"]: c for c in surfaces["issue_comments"]}
    assert 200 in by_id, "older bot comment MUST be collected"
    assert 201 in by_id, "freshest bot comment MUST be collected"

    # The freshest comment MUST be bound to the current head.
    freshest = by_id[201]
    assert freshest["commit_id"] == head_sha, (
        "round-140 P1#1: the freshest bot-authored issue "
        "comment MUST be bound to head_sha when a head-bound "
        "formal review exists. The previous gate required "
        "`c.get(\"commit_id\") == head_sha` but the "
        "/issues/{PR}/comments endpoint never exposes "
        "`commit_id`. The fix binds via endpoint-visible "
        "evidence: surfaces['reviews'] present + cid == "
        "latest_provider_cid."
    )
    assert freshest["review_cycle"] == (
        f"codex:{head_sha}:201"
    ), (
        "round-140 P1#1: the freshest bot-authored issue "
        "comment MUST receive a populated review_cycle "
        "ledger entry so the relay's collect_findings "
        "filter accepts it."
    )

    # The older comment MUST NOT be bound (only the freshest
    # for this provider is bound; the round-31 invariant
    # against historical-chatter replay is preserved).
    older = by_id[200]
    assert older["commit_id"] is None, (
        "round-140 P1#1: only the freshest provider issue "
        "comment is bound; historical comments from this "
        "provider must remain unbound so head-A chatter "
        "cannot reappear on a head-B capture."
    )
    assert older["review_cycle"] is None


def test_p1_01_no_binding_without_head_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-140 P1#1 (negative case): without a head-bound
    review for this provider, NO issue comment is bound —
    preserving the round-31 invariant that historical
    issue-comment chatter cannot reappear as a head-B finding.
    """
    sup = _load_collect_provider_surfaces()
    head_sha = "f" * 40
    bot_login = "chatgpt-codex-connector[bot]"

    fake_comments_page1 = [
        {
            "id": 301,
            "user": {"login": bot_login},
            "created_at": "2026-08-12T22:00:00Z",
            "body": "fresh bot comment",
        }
    ]

    monkeypatch.setattr(
        sup,
        "github_get",
        _stub_github_get(
            comments_by_page={1: fake_comments_page1},
            reviews=[],  # no head-bound review
        ),
    )

    surfaces = sup.collect_provider_surfaces(
        "codex", head_sha, "fake-token"
    )
    by_id = {c["id"]: c for c in surfaces["issue_comments"]}
    assert 301 in by_id
    assert by_id[301]["commit_id"] is None, (
        "round-140 P1#1: without a head-bound review cycle, "
        "the comment MUST remain unbound so the relay's "
        "filter rejects it."
    )
    assert by_id[301]["review_cycle"] is None


def test_p1_01_freshest_picked_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-140 P1#1 (multi-page case): when bot comments
    appear across multiple pages, the loop's monotonic-id
    tracking picks the highest ``cid`` as the freshest,
    binding ONLY that one to the current head.
    """
    sup = _load_collect_provider_surfaces()
    head_sha = "f" * 40
    bot_login = "chatgpt-codex-connector[bot]"

    fake_reviews = [
        {
            "id": 901,
            "commit_id": head_sha,
            "user": {"login": bot_login},
            "submitted_at": "2026-08-12T22:00:00Z",
            "state": "COMMENTED",
            "body": "review",
        }
    ]
    page1 = [
        {
            "id": 100,
            "user": {"login": bot_login},
            "created_at": "2026-08-12T18:00:00Z",
            "body": "older bot comment p1",
        },
    ]
    page2 = [
        {
            "id": 150,
            "user": {"login": bot_login},
            "created_at": "2026-08-12T21:00:00Z",
            "body": "mid bot comment p2",
        },
        {
            "id": 175,
            "user": {"login": bot_login},
            "created_at": "2026-08-12T22:30:00Z",
            "body": "freshest bot comment p2",
        },
    ]

    monkeypatch.setattr(
        sup,
        "github_get",
        _stub_github_get(
            comments_by_page={1: page1, 2: page2},
            reviews=fake_reviews,
        ),
    )

    surfaces = sup.collect_provider_surfaces(
        "codex", head_sha, "fake-token"
    )
    by_id = {c["id"]: c for c in surfaces["issue_comments"]}
    # All three comments collected.
    assert {100, 150, 175}.issubset(set(by_id.keys()))

    # Only cid=175 (highest) is bound.
    assert by_id[175]["commit_id"] == head_sha
    assert by_id[175]["review_cycle"] == (
        f"codex:{head_sha}:175"
    )
    assert by_id[100]["commit_id"] is None
    assert by_id[100]["review_cycle"] is None
    assert by_id[150]["commit_id"] is None
    assert by_id[150]["review_cycle"] is None


def test_p1_01_does_not_rely_on_commit_id_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-140 P1#1 (guard against regression): the source
    MUST NOT require ``c.get("commit_id")`` in the binding
    gate. The previous gate required this field, which the
    ``/issues/{PR}/comments`` endpoint never returns. This is
    a source-text guard so a future revert is caught at test
    time.
    """
    text = SUPERVISOR_PATH.read_text(encoding="utf-8")
    # The previous unreachable gate was::
    #     if isinstance(c.get("commit_id"), str)
    #        and c.get("commit_id") == head_sha
    # That pattern MUST NOT appear in the binding gate. We
    # accept occurrences in unrelated code paths (e.g. the
    # review fetcher at line ~7194).
    assert (
        'isinstance(c.get("commit_id"), str)\n'
        '                        and c.get("commit_id") == head_sha'
    ) not in text, (
        "round-140 P1#1: the issue-comment binding gate MUST "
        "NOT depend on `c.get(\"commit_id\")` because the "
        "/issues/{PR}/comments endpoint never exposes that "
        "field. Use endpoint-visible evidence (head-bound "
        "reviews + freshest bot-authored cid)."
    )