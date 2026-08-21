"""Round-C24-R2 / Exact-head resurrection independent of timestamps.

Audit defect being pinned (§1-§4):

  Production intentionally writes SUPERSEDED rows WITHOUT
  ``superseded_at`` (``relay_wiring._fetch_pr_head_pushed_at``
  fails closed to ``""`` after the repo-level push-time proxy
  was invalidated). The previous contract made
  ``FindingLedger.superseded_repair_transition`` return ``None``
  for those rows and made ``_c22_is_followup_eligible`` evaluate
  the timestamp gate BEFORE the exact-head branch, so the
  C24-R2 exact-head identity path was unreachable — a dead
  path.

Contract under test:

  A. SUPERSEDED(superseded_by_head=B, no superseded_at) +
     followup.commit_id=B                      -> resurrect
  B. same row + followup.commit_id=C           -> reject
  C. same row + no commit binding              -> fail closed
  D. same row + no binding + superseded_at=T1,
     followup.createdAt=T2 > T1                -> legacy path qualifies
  E. commit matches B but status-marker body   -> reject (CR-002)
  F. commit matches B but operator-authored    -> reject
  G. resolved thread                           -> reject
  H. production snapshot first page preserves
     reply commit binding (+ ledger identity)  -> preserved
  I. production snapshot paginated reply keeps
     commit binding                            -> preserved
  J. ledger lookup returns identity evidence
     without superseded_at                     -> not None
  K. malformed superseded_by_head              -> fail closed

The §2 reproduction (test_matrix_a_resurrects_via_exact_head and
its collector companion) seeds the ledger EXACTLY like
production does — ``record_observed`` followed by
``mark_superseded_by_head(new_head_sha=B)`` with NO
``superseded_at`` — and drives the same
lookup/snapshot/collector path production uses. It FAILED on
head 07487ea3 (transition returned None; eligibility never
reached the identity branch).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest


_REPO_ROOT = Path(os.environ.get("REPO_ROOT", str(Path(__file__).resolve().parent.parent)))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from autocoder_orchestration.review_repair_relay import (  # noqa: E402
    Finding,
    FindingLedger,
    _maybe_resurrect_outdated_thread,
    _collect_review_findings,
)
from autocoder_orchestration.store import StateStore  # noqa: E402


HEAD_A = "a" * 40  # old head (finding observed here)
HEAD_B = "b" * 40  # superseding repair head
HEAD_C = "c" * 40  # unrelated later head

THREAD_ID = "PRRT_TEST"
FINDING_ID = f"thread:{THREAD_ID}"

T1 = "2026-08-20T12:00:00Z"  # valid superseded_at (legacy path)
T2 = "2026-08-20T13:00:00Z"  # follow-up createdAt strictly after T1

ACTIONABLE_BODY = (
    "<sub><sub>![P1 Badge](...)</sub></sub> P1: the retry loop "
    "still swallows KeyboardInterrupt; please re-derive the guard."
)
STATUS_MARKER_BODY = "<sub>📝 Walkthrough</sub>\nWalkthrough"


def _make_finding(head_sha: str = HEAD_A) -> Finding:
    return Finding.from_dict({
        "finding_id": FINDING_ID,
        "source": "coderabbit",
        "severity": "P1",
        "title": "retry loop swallows interrupts",
        "body": ACTIONABLE_BODY,
        "file_path": "autocoder_supervisor/supervisor.py",
        "line": 10,
    })


def _seed_production_shape_ledger(
    store: StateStore,
    *,
    superseded_at: str | None = None,
) -> FindingLedger:
    """Seed the ledger EXACTLY like the production relay does:
    an OBSERVED finding on HEAD_A promoted to SUPERSEDED by
    ``mark_superseded_by_head`` when the worker push advanced the
    head to HEAD_B. ``superseded_at`` is omitted unless the caller
    supplies one — matching ``relay_wiring.mark_head_advanced_public``
    which forwards ``None`` whenever the push-time fetch fails
    closed."""
    ledger = FindingLedger(store, head_sha=HEAD_A)
    ledger.record_observed(_make_finding())
    ledger.mark_superseded_by_head(
        HEAD_A,
        new_head_sha=HEAD_B,
        superseded_at=superseded_at,
    )
    return ledger


def _durable_row(store: StateStore) -> dict:
    rows = [
        r for r in store.read_journal("finding_ledger.jsonl")
        if isinstance(r, dict) and r.get("finding_id") == FINDING_ID
    ]
    assert rows, "expected at least one durable ledger row"
    return rows[-1]


def _production_thread(
    *,
    replies: list[dict],
    superseded_by_head: str | None = HEAD_B,
    superseded_at: str | None = None,
    resolved: bool = False,
    outdated: bool = True,
) -> dict:
    """Thread dict in the exact shape ``capture_live_snapshot``
    stamps from the durable ledger lookup (both fields forwarded
    verbatim; absent evidence stays ``None``)."""
    return {
        "id": THREAD_ID,
        "thread_id": THREAD_ID,
        "path": "autocoder_supervisor/supervisor.py",
        "line": 10,
        "body": ACTIONABLE_BODY[:200],
        "resolved": resolved,
        "outdated": outdated,
        "superseded_by_head": superseded_by_head,
        "superseded_at": superseded_at,
        "replies": replies,
    }


def _reply(
    *,
    commit_id: str | None = None,
    created_at: str = T2,
    author: str = "external-reviewer",
    body: str = ACTIONABLE_BODY,
) -> dict:
    reply: dict = {
        "id": "981",
        "createdAt": created_at,
        "updatedAt": created_at,
        "body": body,
        "author": author,
    }
    # Mirror production shaping: ``commit_id`` is present only when
    # GitHub delivered one (the key is always written by the
    # snapshot; value may be None).
    reply["commit_id"] = commit_id
    return reply


class TestMatrixALedgerToResurrection:
    """§2 reproduction: production-shaped ledger row + the SAME
    lookup/snapshot/collector path production uses."""

    @pytest.fixture()
    def store(self, tmp_path: Path) -> StateStore:
        return StateStore(str(tmp_path / "orch"))

    def test_durable_row_has_identity_without_timestamp(
        self, store: StateStore,
    ) -> None:
        """Required durable row shape: state=SUPERSEDED,
        superseded_by_head=B, NO superseded_at."""
        _seed_production_shape_ledger(store)
        row = _durable_row(store)
        assert row["state"] == "SUPERSEDED"
        assert row["superseded_by_head"] == HEAD_B
        assert "superseded_at" not in row, (
            "no timestamp may be invented merely to satisfy the "
            "old schema"
        )

    def test_ledger_lookup_returns_identity_not_none(
        self, store: StateStore,
    ) -> None:
        """Matrix J: the lookup returns identity evidence instead
        of ``None`` when ``superseded_at`` is absent."""
        _seed_production_shape_ledger(store)
        transition = FindingLedger(
            store, head_sha=HEAD_B,
        ).superseded_repair_transition(FINDING_ID)
        assert transition is not None
        assert transition["superseded_by_head"] == HEAD_B
        assert "superseded_at" not in transition

    def test_matrix_a_resurrects_via_exact_head(
        self, store: StateStore,
    ) -> None:
        """THE dead-path regression. On 07487ea3 this FAILED:
        ``superseded_repair_transition`` returned ``None`` (it
        demanded ``superseded_at``) so the thread carried no
        ``superseded_by_head``, and the eligibility helper hit its
        timestamp gate (``repair_transition_ts is None``) BEFORE
        the exact-head branch. The finding could never resurrect
        despite perfect identity evidence."""
        _seed_production_shape_ledger(store)
        transition = FindingLedger(
            store, head_sha=HEAD_B,
        ).superseded_repair_transition(FINDING_ID)
        assert transition is not None, (
            "dead path: identity-only SUPERSEDED row must yield "
            "the superseding head"
        )
        thread = _production_thread(
            replies=[_reply(commit_id=HEAD_B)],
            superseded_by_head=transition["superseded_by_head"],
            superseded_at=transition.get("superseded_at"),
        )
        result = _maybe_resurrect_outdated_thread(
            thread, current_head=HEAD_B,
        )
        assert result is not None, (
            "exact-head identity (commit_id == superseded_by_head) "
            "must resurrect WITHOUT any superseded_at timestamp"
        )
        assert result["followup"]["commit_id"] == HEAD_B

    def test_matrix_a_collector_path_resurrects(
        self, store: StateStore,
    ) -> None:
        """Same evidence driven through ``_collect_review_findings``
        — the collector entry point production snapshots flow
        through."""
        _seed_production_shape_ledger(store)
        transition = FindingLedger(
            store, head_sha=HEAD_B,
        ).superseded_repair_transition(FINDING_ID)
        assert transition is not None
        snap = {
            "head_sha": HEAD_B,
            "review_threads": {
                THREAD_ID: _production_thread(
                    replies=[_reply(commit_id=HEAD_B)],
                    superseded_by_head=transition["superseded_by_head"],
                    superseded_at=transition.get("superseded_at"),
                ),
            },
        }
        findings = _collect_review_findings(snap)
        ids = [f.finding_id for f in findings]
        assert FINDING_ID in ids, (
            "collector must resurrect the finding via the "
            "exact-head identity path"
        )


class TestMatrixBCDEFGEligibility:
    """Eligibility semantics on the production-shaped row
    (identity present, timestamp absent)."""

    @pytest.fixture()
    def thread_factory(self, tmp_path: Path):
        store = StateStore(str(tmp_path / "orch"))
        _seed_production_shape_ledger(store)
        transition = FindingLedger(
            store, head_sha=HEAD_B,
        ).superseded_repair_transition(FINDING_ID)
        assert transition is not None

        def _make(replies: list[dict], **kw) -> dict:
            return _production_thread(
                replies=replies,
                superseded_by_head=transition["superseded_by_head"],
                superseded_at=transition.get("superseded_at"),
                **kw,
            )

        return _make

    def test_matrix_b_mismatched_commit_rejected(
        self, thread_factory,
    ) -> None:
        """commit_id bound to a DIFFERENT head (C) is positive
        evidence the follow-up is NOT on the repair head."""
        thread = thread_factory([_reply(commit_id=HEAD_C)])
        assert _maybe_resurrect_outdated_thread(
            thread, current_head=HEAD_B,
        ) is None

    def test_matrix_c_no_binding_no_timestamp_fails_closed(
        self, thread_factory,
    ) -> None:
        """No commit binding + no superseded_at anywhere: the
        legacy time path fails closed (missing boundary)."""
        thread = thread_factory([_reply(commit_id=None)])
        assert _maybe_resurrect_outdated_thread(
            thread, current_head=HEAD_B,
        ) is None

    def test_matrix_d_legacy_time_path_qualifies(
        self, tmp_path: Path,
    ) -> None:
        """With a VALID superseded_at=T1 and no commit binding,
        the legacy strict-timestamp comparison still qualifies a
        T2 > T1 follow-up (fall-through when identity evidence is
        unavailable on the follow-up)."""
        store = StateStore(str(tmp_path / "orch"))
        _seed_production_shape_ledger(store, superseded_at=T1)
        transition = FindingLedger(
            store, head_sha=HEAD_B,
        ).superseded_repair_transition(FINDING_ID)
        assert transition is not None
        assert transition.get("superseded_at") == T1
        thread = _production_thread(
            replies=[_reply(commit_id=None, created_at=T2)],
            superseded_by_head=transition["superseded_by_head"],
            superseded_at=transition.get("superseded_at"),
        )
        result = _maybe_resurrect_outdated_thread(
            thread, current_head=HEAD_B,
        )
        assert result is not None

    def test_matrix_e_status_marker_with_matching_head_rejected(
        self, thread_factory,
    ) -> None:
        """CR-002 regression guard: R3 (actionable body) runs
        BEFORE the identity acceptance branch."""
        thread = thread_factory([
            _reply(commit_id=HEAD_B, body=STATUS_MARKER_BODY),
        ])
        assert _maybe_resurrect_outdated_thread(
            thread, current_head=HEAD_B,
        ) is None

    def test_matrix_f_operator_author_rejected(
        self, thread_factory,
    ) -> None:
        thread = thread_factory([
            _reply(commit_id=HEAD_B, author="github-actions"),
        ])
        assert _maybe_resurrect_outdated_thread(
            thread, current_head=HEAD_B,
            operator_logins=("github-actions",),
        ) is None

    def test_matrix_g_resolved_thread_rejected(
        self, thread_factory,
    ) -> None:
        thread = thread_factory(
            [_reply(commit_id=HEAD_B)], resolved=True,
        )
        assert _maybe_resurrect_outdated_thread(
            thread, current_head=HEAD_B,
        ) is None


class TestMatrixKMalformedIdentity:
    """Malformed / missing ``superseded_by_head`` fails closed."""

    def _row(self, **overrides) -> dict:
        row = {
            "schema_version": 1,
            "finding_id": FINDING_ID,
            "source": "coderabbit",
            "severity": "P1",
            "signature": "sig",
            "head_sha": HEAD_A,
            "state": "SUPERSEDED",
        }
        row.update(overrides)
        return row

    def test_non_hex_superseded_by_head_returns_none(
        self, tmp_path: Path,
    ) -> None:
        store = StateStore(str(tmp_path / "orch"))
        store.append_journal("finding_ledger.jsonl", self._row(
            superseded_by_head="not-a-sha",
        ))
        assert FindingLedger(
            store, head_sha=HEAD_B,
        ).superseded_repair_transition(FINDING_ID) is None

    def test_missing_superseded_by_head_returns_none(
        self, tmp_path: Path,
    ) -> None:
        store = StateStore(str(tmp_path / "orch"))
        store.append_journal("finding_ledger.jsonl", self._row())
        assert FindingLedger(
            store, head_sha=HEAD_B,
        ).superseded_repair_transition(FINDING_ID) is None

    def test_empty_string_superseded_by_head_returns_none(
        self, tmp_path: Path,
    ) -> None:
        store = StateStore(str(tmp_path / "orch"))
        store.append_journal("finding_ledger.jsonl", self._row(
            superseded_by_head="",
        ))
        assert FindingLedger(
            store, head_sha=HEAD_B,
        ).superseded_repair_transition(FINDING_ID) is None

    def test_malformed_directive_id_returns_none(
        self, tmp_path: Path,
    ) -> None:
        store = StateStore(str(tmp_path / "orch"))
        store.append_journal("finding_ledger.jsonl", self._row(
            superseded_by_head=HEAD_B,
            directive_id=12345,
        ))
        assert FindingLedger(
            store, head_sha=HEAD_B,
        ).superseded_repair_transition(FINDING_ID) is None


# ---------------------------------------------------------------------------
# Matrix H / I: production snapshot capture.
#
# These drive the REAL ``capture_live_snapshot`` (GraphQL thread
# inventory + per-thread pagination + durable ledger stamping)
# against faked transports, proving the reply ``commit_id``
# binding survives first-page shaping AND pagination, and that
# the production-shaped identity-only ledger row is stamped onto
# the thread evidence.
# ---------------------------------------------------------------------------

def _graphql_threads_payload(reply_commit_oid, *, has_next_page: bool) -> dict:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                        "nodes": [{
                            "id": THREAD_ID,
                            "isResolved": False,
                            "isOutdated": True,
                            "path": "autocoder_supervisor/supervisor.py",
                            "comments": {
                                "pageInfo": {
                                    "hasNextPage": has_next_page,
                                    "endCursor": (
                                        "CURSOR1" if has_next_page else None
                                    ),
                                },
                                "nodes": [
                                    {
                                        "databaseId": 9001,
                                        "author": {"login": "coderabbitai[bot]"},
                                        "createdAt": T1,
                                        "updatedAt": T1,
                                        "body": ACTIONABLE_BODY,
                                        "path": "autocoder_supervisor/supervisor.py",
                                        "line": 10,
                                        "commit": {"oid": HEAD_A},
                                    },
                                    {
                                        "databaseId": 9002,
                                        "author": {"login": "external-reviewer"},
                                        "createdAt": T2,
                                        "updatedAt": T2,
                                        "body": ACTIONABLE_BODY,
                                        "path": "autocoder_supervisor/supervisor.py",
                                        "line": 10,
                                        "commit": {"oid": reply_commit_oid},
                                    },
                                ],
                            },
                        }],
                    },
                },
            },
        },
    }


@pytest.fixture()
def snapshot_env(monkeypatch, tmp_path: Path):
    """Isolate the supervisor globals ``capture_live_snapshot``
    touches and pre-seed the production-shaped identity-only
    ledger row under the canonical orchestration state root."""
    import autocoder_supervisor.supervisor as sup

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    orch = tmp_path / "orch_state"
    orch.mkdir(parents=True)
    run_ctx = {
        "schema_version": "autocoder.run_context.v1",
        "run_id": "c24r2-matrix",
        "repo_owner": "owner",
        "repo_name": "repo",
        "pr_number": 4,
        "current_authorized_head": HEAD_B,
    }
    (orch / "run_context.json").write_text(json.dumps(run_ctx))
    (orch / "state.json").write_text(json.dumps({
        "schema_version": "autocoder.state_machine.v1",
        "current_state": "AWAITING_CI",
    }))
    run_state_path = tmp_path / "run_state.json"
    run_state_path.write_text(json.dumps({
        "current_head": HEAD_B,
        "orchestration_state_root": str(orch),
    }))
    for p in (run_state_path, orch / "run_context.json", orch / "state.json"):
        os.chmod(p, 0o600)

    monkeypatch.setattr(sup, "STATE_DIR", state_dir)
    monkeypatch.setattr(sup, "RUN_STATE", run_state_path)
    monkeypatch.setattr(sup, "UNCONSUMED_EVENTS_PATH",
                        state_dir / "unconsumed_events.json")
    monkeypatch.setattr(sup, "LOG_PATH", tmp_path / "supervisor.log")
    monkeypatch.setattr(sup, "PR_NUMBER", 4)
    monkeypatch.setattr(sup, "REPO_OWNER", "owner")
    monkeypatch.setattr(sup, "REPO_NAME", "repo")

    # Seed the durable ledger AT the canonical state root so the
    # snapshot's ``_superseded_repair_transition_for_thread``
    # lookup exercises the real resolution + read path.
    store = StateStore(str(orch))
    _seed_production_shape_ledger(store)

    return sup


def _fake_urlopen_factory(threads_payload: dict):
    class _FakeResp:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=20):
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        if "/graphql" in url:
            return _FakeResp(json.dumps(threads_payload).encode())
        if "/check-runs" in url:
            return _FakeResp(json.dumps({"check_runs": []}).encode())
        if "/pulls/" in url and "/reviews" in url:
            return _FakeResp(json.dumps([]).encode())
        if "/issues/" in url and "/comments" in url:
            return _FakeResp(json.dumps([]).encode())
        if "/pulls/" in url:
            return _FakeResp(json.dumps({"head": {"sha": HEAD_B}}).encode())
        return _FakeResp(json.dumps({}).encode())

    return fake_urlopen


class TestMatrixHISnapshotCapture:
    def test_matrix_h_first_page_reply_keeps_commit_binding(
        self, snapshot_env, monkeypatch,
    ) -> None:
        """First-page reply shaping must retain the reply's own
        ``commit { oid }``, and the durable identity-only ledger
        row must be stamped as ``superseded_by_head``."""
        sup = snapshot_env
        payload = _graphql_threads_payload(HEAD_B, has_next_page=False)
        monkeypatch.setattr(
            urllib.request, "urlopen", _fake_urlopen_factory(payload),
        )
        monkeypatch.setattr(
            sup, "_git_superseding_repair_committed_at",
            lambda *a, **k: None,
        )
        snap = sup.capture_live_snapshot({}, "fake-token")
        assert snap.get("review_threads_pagination_complete") is True
        thread = snap["review_threads"][THREAD_ID]
        # Identity evidence stamped from the durable ledger row
        # (which carries NO superseded_at).
        assert thread["superseded_by_head"] == HEAD_B
        assert thread["superseded_at"] is None
        # First-page reply retains its exact-head binding.
        replies = thread["replies"]
        assert len(replies) == 1
        assert replies[0]["commit_id"] == HEAD_B, (
            "first-page reply shaping must preserve commit { oid }"
        )

    def test_matrix_i_paginated_reply_keeps_commit_binding(
        self, snapshot_env, monkeypatch,
    ) -> None:
        """A qualifying follow-up beyond the inline first page
        (>25-comment thread) must keep its commit binding through
        the pagination pass."""
        sup = snapshot_env
        payload = _graphql_threads_payload(HEAD_A, has_next_page=True)
        monkeypatch.setattr(
            urllib.request, "urlopen", _fake_urlopen_factory(payload),
        )
        monkeypatch.setattr(
            sup, "_git_superseding_repair_committed_at",
            lambda *a, **k: None,
        )

        def fake_graphql(query: str, variables: dict, **kwargs):
            assert "$nodeId" in query or "node(id:" in query
            return {
                "node": {
                    "comments": {
                        "pageInfo": {
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                        "nodes": [
                            # Cursor anchor node (page 2 re-lists
                            # the last node of page 1 first).
                            {
                                "databaseId": 9002,
                                "author": {"login": "external-reviewer"},
                                "createdAt": T2,
                                "updatedAt": T2,
                                "body": ACTIONABLE_BODY,
                                "path": "autocoder_supervisor/supervisor.py",
                                "line": 10,
                                "commit": {"oid": HEAD_A},
                            },
                            # NEW page-2 reply bound to the repair head.
                            {
                                "databaseId": 9003,
                                "author": {"login": "external-reviewer"},
                                "createdAt": T2,
                                "updatedAt": T2,
                                "body": ACTIONABLE_BODY,
                                "path": "autocoder_supervisor/supervisor.py",
                                "line": 10,
                                "commit": {"oid": HEAD_B},
                            },
                        ],
                    },
                },
            }

        monkeypatch.setattr(sup, "_github_graphql", fake_graphql)
        snap = sup.capture_live_snapshot({}, "fake-token")
        thread = snap["review_threads"][THREAD_ID]
        # First-page comments (anchor + reply) + both page-2 nodes
        # (cursor anchor + new reply) per the existing
        # ``comment_count`` accounting.
        assert thread["comment_count"] == 4
        replies = thread["replies"]
        # NOTE: the pre-existing pagination pass re-lists the
        # cursor anchor node as a reply entry (documented
        # behaviour, unchanged this round); the binding evidence
        # is what matters here.
        assert [r["id"] for r in replies] == ["9002", "9002", "9003"]
        # ALL paginated entries retain their bindings.
        assert replies[0]["commit_id"] == HEAD_A
        assert replies[1]["commit_id"] == HEAD_A
        assert replies[2]["commit_id"] == HEAD_B, (
            "paginated reply shaping must preserve commit { oid }"
        )

    def test_matrix_i_paginated_reply_resurrects_end_to_end(
        self, snapshot_env, monkeypatch,
    ) -> None:
        """Full production chain: paginated reply bound to the
        repair head resurrects through the collector on the
        captured snapshot."""
        sup = snapshot_env
        payload = _graphql_threads_payload(HEAD_A, has_next_page=True)
        monkeypatch.setattr(
            urllib.request, "urlopen", _fake_urlopen_factory(payload),
        )
        monkeypatch.setattr(
            sup, "_git_superseding_repair_committed_at",
            lambda *a, **k: None,
        )

        def fake_graphql(query: str, variables: dict, **kwargs):
            return {
                "node": {
                    "comments": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "databaseId": 9002,
                                "author": {"login": "external-reviewer"},
                                "createdAt": T2,
                                "updatedAt": T2,
                                "body": ACTIONABLE_BODY,
                                "path": "autocoder_supervisor/supervisor.py",
                                "line": 10,
                                "commit": {"oid": HEAD_A},
                            },
                            {
                                "databaseId": 9003,
                                "author": {"login": "external-reviewer"},
                                "createdAt": T2,
                                "updatedAt": T2,
                                "body": ACTIONABLE_BODY,
                                "path": "autocoder_supervisor/supervisor.py",
                                "line": 10,
                                "commit": {"oid": HEAD_B},
                            },
                        ],
                    },
                },
            }

        monkeypatch.setattr(sup, "_github_graphql", fake_graphql)
        snap = sup.capture_live_snapshot({}, "fake-token")
        findings = _collect_review_findings(snap)
        assert FINDING_ID in [f.finding_id for f in findings], (
            "paginated exact-head-bound reply must resurrect the "
            "finding through the production collector"
        )
