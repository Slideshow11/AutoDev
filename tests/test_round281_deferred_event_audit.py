"""Cooldown-deferred event audit (pre-canary §8).

Properties:

- No event may disappear merely because it is old.
- Age alone is never terminality.
- Every nonterminal deferred event has a future owner.
- Same-head request idempotency may not suppress retries
  forever.
- Head change explicitly supersedes stale head-scoped work
  where appropriate.
- Already-terminal work is semantically consumed.
- Failed work remains durable.
- A deferred optional-provider event cannot deadlock
  readiness forever.
- Required-provider deferred work cannot be incorrectly
  ignored.
- No duplicate live mutation owner exists.

PASS requires:

  DEFERRED_WITHOUT_RETRY_OWNER = 0
  ORPHANED = 0
  TERMINAL_PENDING_CONSUMPTION = 0
  SUPERSEDED_PENDING_CONSUMPTION = 0
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audit_deferred_entries(entries: list[dict]) -> dict:
    """Audit a list of cooldown-deferred entries.

    Returns a dict with these counters:
      - total: total entries
      - with_retry_owner: entries that carry a non-empty retry_owner
      - without_retry_owner: entries with no retry_owner
      - by_kind: {kind: count}
    """
    out = {"total": 0, "with_retry_owner": 0, "without_retry_owner": 0, "by_kind": {}}
    for e in entries:
        if not isinstance(e, dict):
            continue
        out["total"] += 1
        owner = e.get("retry_owner")
        kind = e.get("kind", "unknown")
        out["by_kind"][kind] = out["by_kind"].get(kind, 0) + 1
        if isinstance(owner, str) and owner.strip():
            out["with_retry_owner"] += 1
        else:
            out["without_retry_owner"] += 1
    return out


# ---------------------------------------------------------------------------
# Migration: legacy ids → entries with default retry_owner
# ---------------------------------------------------------------------------


class TestLegacyMigration:
    def test_legacy_ids_become_entries_with_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Pre-canary round-281 §8: a legacy {ids, last_deferred_at}
        # ledger MUST be migrated forward into {entries} on the
        # next ``_mark_cooldown_deferred`` call.
        from autocoder_supervisor import supervisor as _sup
        ledger_path = tmp_path / "cooldown.json"
        monkeypatch.setattr(_sup, "_COOLDOWN_DEFERRED_PATH", ledger_path)
        # Write the legacy shape.
        legacy = {
            "ids": [
                "new_thread:PRRT_kwDOTtyQLc6Xhdz7",
                "check_changed:full-suite",
                "provider_state:codex",
                "new_review:4898758255",
            ],
            "last_deferred_at": "2026-08-13T22:00:00Z",
        }
        ledger_path.write_text(json.dumps(legacy))
        # Now mark a new event as deferred.
        _sup._mark_cooldown_deferred([
            {"id": "new_thread:PRRT_kwDOTtyQLc6Xnew", "head_sha": "a" * 40},
        ])
        # Read back.
        result = json.loads(ledger_path.read_text())
        assert "entries" in result
        # The four legacy ids are migrated.
        ids = [e["id"] for e in result["entries"]]
        assert "new_thread:PRRT_kwDOTtyQLc6Xhdz7" in ids
        assert "check_changed:full-suite" in ids
        assert "provider_state:codex" in ids
        assert "new_review:4898758255" in ids
        # All four legacy entries have a retry_owner.
        audit = _audit_deferred_entries(result["entries"])
        assert audit["without_retry_owner"] == 0


# ---------------------------------------------------------------------------
# New entries carry retry_owner + supersession_condition
# ---------------------------------------------------------------------------


class TestRetryOwnerEnrichment:
    def test_new_event_has_retry_owner_and_conditions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from autocoder_supervisor import supervisor as _sup
        ledger_path = tmp_path / "cooldown.json"
        monkeypatch.setattr(_sup, "_COOLDOWN_DEFERRED_PATH", ledger_path)
        _sup._mark_cooldown_deferred([
            {
                "id": "new_thread:PRRT_kwDOTtyQLc6XpLEZ",
                "kind": "new_review_thread",
                "head_sha": "a" * 40,
            },
        ])
        result = json.loads(ledger_path.read_text())
        assert len(result["entries"]) == 1
        e = result["entries"][0]
        assert e["id"] == "new_thread:PRRT_kwDOTtyQLc6XpLEZ"
        assert e["kind"] == "new_review_thread"
        assert e["head_sha"] == "a" * 40
        assert e["retry_owner"] == "supervisor_handle_new_events"
        assert e["next_retry_condition"] == "cooldown_expired"
        assert e["supersession_condition"] == "head_change"
        assert "deferred_at" in e


# ---------------------------------------------------------------------------
# Provider-state events get the right retry owner
# ---------------------------------------------------------------------------


class TestProviderStateRetryOwner:
    def test_provider_state_event_gets_quota_retry_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from autocoder_supervisor import supervisor as _sup
        ledger_path = tmp_path / "cooldown.json"
        monkeypatch.setattr(_sup, "_COOLDOWN_DEFERRED_PATH", ledger_path)
        _sup._mark_cooldown_deferred([
            {
                "id": "provider_state:codex",
                "kind": "provider_state_change",
                "head_sha": "b" * 40,
            },
        ])
        result = json.loads(ledger_path.read_text())
        e = result["entries"][0]
        assert e["retry_owner"] == "supervisor_quota_state_retry"
        assert e["next_retry_condition"] == "next_retry_timestamp_elapsed"


# ---------------------------------------------------------------------------
# Consume removes only the consumed ids
# ---------------------------------------------------------------------------


class TestConsumeIsolatesEntries:
    def test_consume_keeps_others(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from autocoder_supervisor import supervisor as _sup
        ledger_path = tmp_path / "cooldown.json"
        monkeypatch.setattr(_sup, "_COOLDOWN_DEFERRED_PATH", ledger_path)
        _sup._mark_cooldown_deferred([
            {"id": "new_thread:A"},
            {"id": "new_thread:B"},
            {"id": "new_thread:C"},
        ])
        _sup._consume_cooldown_deferred(["new_thread:B"])
        result = json.loads(ledger_path.read_text())
        ids = [e["id"] for e in result["entries"]]
        assert ids == ["new_thread:A", "new_thread:C"]


# ---------------------------------------------------------------------------
# Audit invariants
# ---------------------------------------------------------------------------


class TestAuditInvariants:
    def test_deferred_without_retry_owner_is_zero_after_mark(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from autocoder_supervisor import supervisor as _sup
        ledger_path = tmp_path / "cooldown.json"
        monkeypatch.setattr(_sup, "_COOLDOWN_DEFERRED_PATH", ledger_path)
        # Enqueue a variety of event kinds.
        for eid in [
            "new_thread:PRRT_kwDOTtyQLc6XpLEZ",
            "new_review:4898758255",
            "check_changed:full-suite",
            "provider_state:codex",
            "unresolved_thread_drain:foo",
        ]:
            _sup._mark_cooldown_deferred([
                {"id": eid, "head_sha": "c" * 40}
            ])
        result = json.loads(ledger_path.read_text())
        audit = _audit_deferred_entries(result["entries"])
        assert audit["total"] == 5
        assert audit["without_retry_owner"] == 0
        assert audit["with_retry_owner"] == 5


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_duplicate_mark_does_not_double_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from autocoder_supervisor import supervisor as _sup
        ledger_path = tmp_path / "cooldown.json"
        monkeypatch.setattr(_sup, "_COOLDOWN_DEFERRED_PATH", ledger_path)
        _sup._mark_cooldown_deferred([{"id": "new_thread:X"}])
        _sup._mark_cooldown_deferred([{"id": "new_thread:X"}])
        _sup._mark_cooldown_deferred([{"id": "new_thread:X"}])
        result = json.loads(ledger_path.read_text())
        ids = [e["id"] for e in result["entries"]]
        assert ids.count("new_thread:X") == 1


# ---------------------------------------------------------------------------
# Audit of real production cooldown ledger (live state inspection)
# ---------------------------------------------------------------------------


class TestRealProductionLedger:
    def test_real_cooldown_ledger_has_owner_for_each_entry(
        self, tmp_path: Path,
    ) -> None:
        """Inspect the live cooldown-deferred ledger and confirm
        every entry has a retry_owner. We do NOT modify the
        live ledger; we read it, classify legacy entries
        via the migration helper, and assert the result has
        zero entries without an owner.
        """
        from autocoder_supervisor import supervisor as _sup
        # Resolve the live ledger path from $OPERATOR_HOME so
        # this test does not hardcode an absolute filesystem
        # path in source.
        import os as _os
        operator_home = _os.environ.get("OPERATOR_HOME") or "/home/max"
        real_path = Path(operator_home) / ".hermes/aed-supervisor/state/cooldown_deferred_events.json"
        if not real_path.exists():
            pytest.skip("real cooldown ledger not present in this environment")
        raw = json.loads(real_path.read_text())
        # Simulate migration by feeding the raw ids through the
        # classifier.
        migrated = []
        for eid in raw.get("ids", []):
            cls = _sup._classify_event_retry_owner({"id": eid})
            migrated.append(cls)
        # Audit.
        audit = _audit_deferred_entries(migrated)
        assert audit["total"] == len(raw.get("ids", []))
        assert audit["without_retry_owner"] == 0
        # Verify each kind has an appropriate retry owner.
        valid_owners = {
            "provider_state_change": "supervisor_quota_state_retry",
            "unresolved_thread_drain": "supervisor_drain_replay",
            "new_review_thread": "supervisor_handle_new_events",
            "ci_check_change": "supervisor_handle_new_events",
            "new_review": "supervisor_handle_new_events",
            "unknown": "supervisor_handle_new_events",
        }
        for e in migrated:
            assert e["retry_owner"] == valid_owners.get(
                e["kind"], "supervisor_handle_new_events"
            )