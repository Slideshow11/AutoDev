from __future__ import annotations

import json
from pathlib import Path

import pytest

from autocoder_orchestration.review_repair_relay import (
    Finding,
    FindingLedger,
    _maybe_resurrect_outdated_thread,
)
from autocoder_orchestration.store import StateStore
from autocoder_supervisor import reviewer_policy as policy
from autocoder_supervisor import supervisor as sup


HEAD = "f" * 40
NEXT_HEAD = "e" * 40


def test_request_intent_consumes_neither_successful_cap(tmp_path: Path) -> None:
    ledger = tmp_path / "requests"
    ledger.mkdir()
    (ledger / f"codex__{HEAD}.json").write_text(json.dumps({
        "lifecycle": "REQUEST_INTENT",
        "requested_at": "2026-08-19T20:00:00Z",
        "request_id": "req-failed",
    }))
    assert policy._count_active_request_records(
        ledger_path=ledger,
        provider="codex",
        head_sha=HEAD,
        superseded_records=[],
    ) == (0, 0)


def test_optional_sourcery_pause_does_not_block() -> None:
    sourcery = policy.ReviewerPolicy(
        name="sourcery",
        required=False,
        auto_trigger=False,
        max_requests_per_head=0,
        unavailable_behavior="IGNORE",
    )
    plans = policy.plan_reviewer_actions(
        head_sha=HEAD,
        snap={
            "head_sha": HEAD,
            "providers": {"sourcery": {"paused": True, "in_progress": False}},
            "formal_reviews": [],
            "provider_surfaces": {},
        },
        policies={"sourcery": sourcery},
    )
    assert plans["sourcery"].action == "NOT_NEEDED"
    assert plans["sourcery"].reason == "optional_provider_paused_acceptable"


def test_required_provider_pause_still_blocks() -> None:
    codex = policy.ReviewerPolicy(
        name="codex",
        required=True,
        auto_trigger=True,
        unavailable_behavior="BLOCK",
    )
    plans = policy.plan_reviewer_actions(
        head_sha=HEAD,
        snap={
            "head_sha": HEAD,
            "providers": {"codex": {"paused": True, "in_progress": False}},
            "formal_reviews": [],
            "provider_surfaces": {},
        },
        policies={"codex": codex},
    )
    assert plans["codex"].action == "BLOCK"


def test_nested_freshness_snapshot_is_json_native(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = policy.ReviewerTriggerPlan(
        provider="codex",
        action="NOT_NEEDED",
        reason="fresh_exact_head_review",
        freshness=policy.FreshnessResult(
            provider="codex",
            state=policy.FRESHNESS_FRESH,
            reviewed_head=HEAD,
            reason="exact_head",
        ),
    )
    monkeypatch.setattr(policy, "load_policies_from_providers", lambda *a, **k: {"codex": object()})
    monkeypatch.setattr(policy, "plan_reviewer_actions", lambda **kwargs: {"codex": entry})
    monkeypatch.setattr(sup, "PROVIDERS", {"codex": {}})
    monkeypatch.setattr(sup, "REVIEW_REQUESTS_DIR", tmp_path / "requests")
    monkeypatch.setattr(sup, "RUN_STATE", tmp_path / "missing-run-state.json")
    monkeypatch.setattr(sup, "SNAPSHOT_A_PATH", tmp_path / "snapshot-a.json")
    snapshot = {
        "head_sha": HEAD,
        "readiness_state": "AWAITING_MERGE_AUTHORIZATION",
    }
    sup.apply_reviewer_plan(snapshot, head_sha=HEAD)
    json.dumps(snapshot)
    sup.write_snapshot("A", snapshot)
    persisted = json.loads((tmp_path / "snapshot-a.json").read_text())
    assert persisted["reviewer_plan"]["codex"]["freshness"]["state"] == policy.FRESHNESS_FRESH


def _phase_spy(monkeypatch: pytest.MonkeyPatch):
    calls = []

    def resolve_phase(**kwargs):
        calls.append(kwargs)
        return policy.PHASE_REPAIR_HEAD if kwargs.get("has_any_superseded_request") else policy.PHASE_INITIAL_HEAD

    monkeypatch.setattr(policy, "resolve_phase", resolve_phase)
    monkeypatch.setattr(policy, "load_policies_from_providers", lambda *a, **k: {"codex": object()})
    monkeypatch.setattr(policy, "plan_reviewer_actions", lambda **kwargs: {})
    monkeypatch.setattr(sup, "PROVIDERS", {"codex": {}})
    return calls


def test_valid_superseded_record_drives_phase_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _phase_spy(monkeypatch)
    requests = tmp_path / "requests"
    requests.mkdir()
    (requests / f"codex__{HEAD}.superseded.json").write_text(json.dumps({
        "lifecycle": "SUPERSEDED",
        "provider": "codex",
        "stale_head": HEAD,
        "superseded_by_head": NEXT_HEAD,
    }))
    monkeypatch.setattr(sup, "REVIEW_REQUESTS_DIR", requests)
    monkeypatch.setattr(sup, "RUN_STATE", tmp_path / "missing.json")
    sup.apply_reviewer_plan({"head_sha": NEXT_HEAD}, head_sha=NEXT_HEAD)
    assert calls[-1]["has_any_superseded_request"] is True


def test_malformed_superseded_record_cannot_force_repair_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _phase_spy(monkeypatch)
    requests = tmp_path / "requests"
    requests.mkdir()
    (requests / "bad.superseded.json").write_text(json.dumps({
        "lifecycle": "SUPERSEDED", "provider": "codex", "stale_head": "short",
    }))
    monkeypatch.setattr(sup, "REVIEW_REQUESTS_DIR", requests)
    monkeypatch.setattr(sup, "RUN_STATE", tmp_path / "missing.json")
    sup.apply_reviewer_plan({"head_sha": NEXT_HEAD}, head_sha=NEXT_HEAD)
    assert calls[-1]["has_any_superseded_request"] is False


def test_proper_repair_journal_wins_without_fallback(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text(json.dumps({
        "journal": [{"event": "control_plane.repair_pushed", "head_observed": HEAD}],
    }))
    assert policy.resolve_phase(
        state_root=tmp_path, has_any_superseded_request=False,
    ) == policy.PHASE_REPAIR_HEAD


def test_true_repair_time_t1_followup_t2_observed_t3_qualifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(str(tmp_path / "orchestration"))
    ledger = FindingLedger(store, head_sha=HEAD)
    finding = Finding.from_dict({
        "finding_id": "thread:one", "source": "codex", "severity": "P1",
        "title": "finding", "body": "actionable",
    })
    ledger.mark_active(finding)
    monkeypatch.setattr(
        "autocoder_orchestration.review_repair_relay._now_iso",
        lambda: "2026-08-19T20:03:00Z",  # T3 observation
    )
    ledger.mark_superseded_by_head(
        HEAD,
        new_head_sha=NEXT_HEAD,
        superseded_at="2026-08-19T20:01:00Z",  # T1 worker push
    )
    transition = ledger.superseded_repair_transition("thread:one")
    assert transition["superseded_at"] == "2026-08-19T20:01:00Z"
    thread = {
        "id": "one",
        "resolved": False,
        "outdated": True,
        "superseded_at": transition["superseded_at"],
        "superseded_by_head": NEXT_HEAD,
        "replies": [{
            "author": "chatgpt-codex-connector",
            "createdAt": "2026-08-19T20:02:00Z",  # T2 follow-up
            "body": "Actionable finding remains after the repair push.",
        }],
    }
    assert _maybe_resurrect_outdated_thread(
        thread, current_head=NEXT_HEAD,
    ) is not None


def test_missing_trustworthy_repair_time_fails_closed(tmp_path: Path) -> None:
    store = StateStore(str(tmp_path / "orchestration"))
    ledger = FindingLedger(store, head_sha=HEAD)
    finding = Finding.from_dict({
        "finding_id": "thread:one", "source": "codex", "severity": "P1",
        "title": "finding", "body": "actionable",
    })
    ledger.mark_active(finding)
    ledger.mark_superseded_by_head(HEAD, new_head_sha=NEXT_HEAD)
    assert ledger.superseded_repair_transition("thread:one") is None
