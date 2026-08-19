"""Round-28 P4: fail closed on partial locked GraphQL responses.

User-supplied invariant:
  The in-transaction reviewThreads collector MUST reject
  incomplete GraphQL evidence. For EVERY page require:
    no top-level GraphQL errors;
    connection object exists;
    nodes exists and is a list;
    pageInfo exists and has the required fields;
    hasNextPage=true requires a usable endCursor;
    page-count ceiling cannot silently truncate;
    all pages complete before paginated_completely=True.
  Missing fields MUST NEVER default to ``nodes=[]`` or
  ``hasNextPage=False`` because that converts incomplete
  evidence into "zero unresolved threads".

Tests:
  data + errors;
  missing nodes;
  nodes=null;
  missing pageInfo;
  hasNextPage true + missing endCursor;
  later-page failure.
  Every case: merge subprocess not invoked.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from autocoder_orchestration.merge_authorization import (
    MergeGateFetchError,
    fetch_live_thread_inventory,
)


def _valid_page(end_cursor: Optional[str] = None, has_next: bool = False) -> Dict[str, Any]:
    """Return a valid GraphQL response page."""
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "headRefOid": "a" * 40,
                    "reviewThreads": {
                        "pageInfo": {
                            "hasNextPage": has_next,
                            "endCursor": end_cursor,
                        },
                        "nodes": [],
                    },
                }
            }
        }
    }


def _runner_for_pages(pages: List[Dict[str, Any]]):
    """Build a fake ``_safe_run`` that returns each page in
    sequence on subsequent calls.
    """
    pages_iter = iter(pages)

    def runner(*args, **kwargs):
        try:
            page = next(pages_iter)
        except StopIteration:
            page = _valid_page(has_next=False)
        return {
            "returncode": 0,
            "stdout": json.dumps(page),
            "stderr": "",
            "timed_out": False,
        }
    return runner


# ===== Test: data + errors fails closed =====

def test_data_with_errors_fails_closed() -> None:
    """A page that returns both ``data`` and a non-empty
    ``errors`` array MUST fail closed. The gate refuses to
    merge because the partial response is incomplete.
    """
    page = _valid_page()
    page["errors"] = [{"message": "partial: reviewThreads timed out"}]
    with pytest.raises(MergeGateFetchError) as exc:
        fetch_live_thread_inventory(
            "gh", "owner/repo", 4,
            runner=_runner_for_pages([page]),
        )
    assert "errors" in str(exc.value).lower(), (
        f"MergeGateFetchError MUST mention errors; got {exc.value!r}"
    )


# ===== Test: missing nodes field fails closed =====

def test_missing_nodes_field_fails_closed() -> None:
    """A page that omits ``nodes`` MUST fail closed. The
    ``nodes`` field MUST be a list (not missing, not null).
    """
    page = _valid_page()
    del page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    with pytest.raises(MergeGateFetchError) as exc:
        fetch_live_thread_inventory(
            "gh", "owner/repo", 4,
            runner=_runner_for_pages([page]),
        )
    assert "nodes" in str(exc.value).lower(), (
        f"MergeGateFetchError MUST mention nodes; got {exc.value!r}"
    )


# ===== Test: nodes=null fails closed =====

def test_nodes_null_fails_closed() -> None:
    """A page with ``nodes=null`` MUST fail closed. We do NOT
    default to ``nodes=[]`` because that converts incomplete
    evidence into "zero unresolved threads".
    """
    page = _valid_page()
    page["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"] = None
    with pytest.raises(MergeGateFetchError) as exc:
        fetch_live_thread_inventory(
            "gh", "owner/repo", 4,
            runner=_runner_for_pages([page]),
        )
    assert "nodes" in str(exc.value).lower(), (
        f"MergeGateFetchError MUST mention nodes; got {exc.value!r}"
    )


# ===== Test: missing pageInfo fails closed =====

def test_missing_page_info_fails_closed() -> None:
    """A page that omits ``pageInfo`` MUST fail closed.
    """
    page = _valid_page()
    del page["data"]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"]
    with pytest.raises(MergeGateFetchError) as exc:
        fetch_live_thread_inventory(
            "gh", "owner/repo", 4,
            runner=_runner_for_pages([page]),
        )
    assert "pageinfo" in str(exc.value).lower(), (
        f"MergeGateFetchError MUST mention pageInfo; got {exc.value!r}"
    )


# ===== Test: hasNextPage=True + missing endCursor fails closed =====

def test_has_next_page_with_missing_end_cursor_fails_closed() -> None:
    """hasNextPage=True with endCursor missing MUST fail
    closed. We do NOT default to a usable cursor.
    """
    page = _valid_page(has_next=True)  # endCursor is None
    with pytest.raises(MergeGateFetchError) as exc:
        fetch_live_thread_inventory(
            "gh", "owner/repo", 4,
            runner=_runner_for_pages([page]),
        )
    assert "endcursor" in str(exc.value).lower() or "cursor" in str(exc.value).lower(), (
        f"MergeGateFetchError MUST mention endCursor; got {exc.value!r}"
    )


# ===== Test: later-page failure fails closed =====

def test_later_page_failure_fails_closed() -> None:
    """The first page returns a valid response with
    hasNextPage=True. The second page returns an empty
    response (no ``data`` field). The transaction MUST fail
    closed rather than silently truncate.
    """
    page1 = _valid_page(has_next=True, end_cursor="cursor-page-2")
    page2 = {}  # empty: no data, no errors, no reviewThreads
    runner = _runner_for_pages([page1, page2])
    with pytest.raises(MergeGateFetchError) as exc:
        fetch_live_thread_inventory(
            "gh", "owner/repo", 4,
            runner=runner,
        )
    # The error MUST reference the page-1 vs page-2 distinction.
    # Round-28 P4: the gate refuses to assume the count is
    # complete on a partial later-page response.
    msg = str(exc.value)
    assert "data" in msg.lower() or "page 1" in msg.lower(), (
        f"MergeGateFetchError MUST reference the later-page "
        f"incompleteness; got {msg!r}"
    )


# ===== Test: max_pages exceeded fails closed =====

def test_max_pages_exceeded_fails_closed() -> None:
    """When the inventory exceeds ``max_pages``, the gate MUST
    fail closed rather than silently truncate.
    """
    # Build 25 pages that always say hasNextPage=True so the
    # inventory never terminates.
    pages = []
    for i in range(25):
        pages.append(_valid_page(
            has_next=True, end_cursor=f"cursor-{i + 1}",
        ))
    runner = _runner_for_pages(pages)
    with pytest.raises(MergeGateFetchError) as exc:
        fetch_live_thread_inventory(
            "gh", "owner/repo", 4, max_pages=3,
            runner=runner,
        )
    assert "max_pages" in str(exc.value) or "exceeded" in str(exc.value), (
        f"MergeGateFetchError MUST reference the page-ceiling "
        f"violation; got {exc.value!r}"
    )


# ===== Test: complete valid pagination succeeds =====

def test_complete_valid_pagination_succeeds() -> None:
    """A complete, well-formed pagination MUST succeed. The
    final page has hasNextPage=False with a usable
    endCursor (which is allowed to be null when hasNextPage
    is False).
    """
    page1 = _valid_page(has_next=True, end_cursor="cursor-2")
    page2 = _valid_page(has_next=False)  # endCursor null is fine
    runner = _runner_for_pages([page1, page2])
    out = fetch_live_thread_inventory(
        "gh", "owner/repo", 4,
        runner=runner,
    )
    assert out["paginated_completely"] is True
    assert out["error"] is None
    assert out["unresolved_current"] == 0


# ===== Test: happy-path inventory with unresolved threads =====

def test_complete_inventory_with_unresolved_threads_counts_correctly() -> None:
    """A complete inventory with a mix of unresolved and
    resolved threads MUST count them correctly.
    """
    page1 = {
        "data": {"repository": {"pullRequest": {
            "headRefOid": "a" * 40,
            "reviewThreads": {
                "pageInfo": {"hasNextPage": True, "endCursor": "c2"},
                "nodes": [
                    {"isResolved": False, "isOutdated": False},
                    {"isResolved": True, "isOutdated": False},
                ],
            },
        }}},
    }
    page2 = {
        "data": {"repository": {"pullRequest": {
            "headRefOid": "a" * 40,
            "reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {"isResolved": False, "isOutdated": True},
                    {"isResolved": False, "isOutdated": False},
                ],
            },
        }}},
    }
    runner = _runner_for_pages([page1, page2])
    out = fetch_live_thread_inventory(
        "gh", "owner/repo", 4,
        runner=runner,
    )
    # 2 unresolved-current (page 1 + page 2) + 1 unresolved-outdated
    # (page 2). The resolved one is excluded.
    assert out["unresolved_current"] == 2
    assert out["unresolved_outdated"] == 1


# ===== Test: data=null fails closed =====

def test_data_null_fails_closed() -> None:
    """A page with ``data=null`` MUST fail closed. We do NOT
    default ``data`` to ``{}`` and silently treat the page as
    empty (which would convert incomplete evidence into "zero
    unresolved threads").
    """
    page = {"data": None}
    with pytest.raises(MergeGateFetchError) as exc:
        fetch_live_thread_inventory(
            "gh", "owner/repo", 4,
            runner=_runner_for_pages([page]),
        )
    assert "data" in str(exc.value).lower(), (
        f"MergeGateFetchError MUST mention data; got {exc.value!r}"
    )
