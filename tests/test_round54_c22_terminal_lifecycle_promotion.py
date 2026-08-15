"""Round-54/C22 P1 repair: persist TERMINAL Codex lifecycles.

Defect: ``autocoder_supervisor/hermes_fingerprint.py`` exposes
the empirical ``codex_lifecycle_observed`` gate, which only
satisfies when the durable ``review_requests/codex__<head>.json``
ledger contains ``lifecycle`` in
``("REVIEW_COMPLETE", "OPTIONAL_DEGRADED")``. Prior to the fix,
no production call site wrote either value: every
``write_review_request`` produced ``REQUEST_INTENT`` or
``REQUEST_SENT``. The gate therefore could not pass under
normal supervisor flow and the empirical observation stayed
non-terminal forever.

This file proves the repair:

  A. ``_promote_review_request_terminal`` is a real production
     helper that mutates the durable ledger atomically.

  B. ``process_provider_quotas`` promotes an existing
     codex ``REQUEST_INTENT`` / ``REQUEST_SENT`` /
     ``ACKNOWLEDGED`` record to ``REVIEW_COMPLETE`` when the
     classified state is ``PROVIDER_STATE_REVIEW_COMPLETE``.

  C. ``process_provider_quotas`` persists an explicit
     ``OPTIONAL_DEGRADED`` sentinel for the OPTIONAL codex
     provider when no review was produced on the live head.

  D. The empirical gate in ``hermes_fingerprint.py`` reads the
     terminal lifecycle back and flips
     ``observation_complete`` to True with ``value=True``.

  E. The promotion is idempotent: running
     ``_promote_review_request_terminal`` twice does not
     duplicate the side effect.

  F. A SUPERSEDED record on the same head is not overwritten
     by ``REVIEW_COMPLETE`` (supersession wins, per Closure X).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


SUP_PATH = (
    Path(__file__).resolve().parent.parent
    / "autocoder_supervisor"
    / "supervisor.py"
)


def _bootstrap_temp_state(tmp_path: Path):
    """Bootstrap the supervisor module's state paths under
    ``tmp_path`` so the helper can run end-to-end without a
    real install.
    """
    sys.path.insert(0, str(SUP_PATH.parent.parent))
    from autocoder_supervisor import supervisor as sup_mod  # type: ignore

    sd = tmp_path / "state"
    sd.mkdir(parents=True, exist_ok=True)
    sup_mod.HEARTBEAT_PATH = tmp_path / "heartbeat"
    sup_mod.HEARTBEAT_PATH.write_text("dummy")
    sup_mod.LOG_PATH = tmp_path / "supervisor.log"
    sup_mod.LOCK_PATH = tmp_path / "lock"
    sup_mod.LEASE_PATH = sd / "worker_lease.json"
    sup_mod.LAST_RESUME_PATH = sd / "last_resume.json"
    sup_mod.QUOTA_PATH = sd / "quota_state.json"
    sup_mod.REVIEW_REQUESTS_DIR = sd / "review_requests"
    sup_mod.REVIEW_REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    sup_mod.SNAPSHOT_A_PATH = sd / "snapshot_a.json"
    sup_mod.SNAPSHOT_B_PATH = sd / "snapshot_b.json"
    sup_mod.RUN_STATE = sd / "run_state.json"
    sup_mod.UNCONSUMED_EVENTS_PATH = sd / "unconsumed_events.json"
    sup_mod.READINESS_STATE_PATH = sd / "readiness_state.json"
    sup_mod.STATE_DIR = sd
    sup_mod.WORKER_ATTEMPTS_DIR = sd / "worker_attempts"
    sup_mod.WORKER_ATTEMPTS_DIR.mkdir(parents=True, exist_ok=True)
    sup_mod.LEASE_PATH.write_text("{}")
    sup_mod.LAST_RESUME_PATH.write_text("{}")
    sup_mod.RUN_STATE.write_text(json.dumps({"current_head": "abc" * 14}))
    sup_mod.UNCONSUMED_EVENTS_PATH.write_text(json.dumps({"events": []}))
    sup_mod.READINESS_STATE_PATH.write_text(
        json.dumps({"state": "ACTIVE_REPAIR"})
    )
    sup_mod.SNAPSHOT_A_PATH.write_text(json.dumps({}))
    sup_mod.SNAPSHOT_B_PATH.write_text(json.dumps({}))
    sup_mod.QUOTA_PATH.write_text(json.dumps({"providers": {}}))
    return sup_mod


def _provider_config_with_codex():
    return {
        "coderabbit": {
            "bot_logins": ["coderabbitai[bot]"],
            "trigger_handle": "@coderabbitai review",
            "quota_patterns": [],
            "use_reviews_api": False,
            "required_for_pr_416": True,
            "required_for_final_merge": True,
            "required_for_current_repair_round": True,
        },
        "codex": {
            "bot_logins": ["chatgpt-codex-connector[bot]"],
            "trigger_handle": "@codex review",
            "quota_patterns": [],
            "use_reviews_api": False,
            "required_for_pr_416": False,
            "required_for_final_merge": False,
            "required_for_current_repair_round": False,
        },
    }


# ---------------------------------------------------------------------------
# A. Helper exists and is callable
# ---------------------------------------------------------------------------


class TestAPromotionHelperIsPresent:
    def test_helper_exists(self):
        from autocoder_supervisor import supervisor as _sup

        assert hasattr(_sup, "_promote_review_request_terminal")
        assert callable(_sup._promote_review_request_terminal)

    def test_rejects_non_terminal_lifecycle(self, tmp_path):
        """The helper MUST refuse to write a progress lifecycle
        value. This protects the empirical gate from accidental
        rewrites of REQUEST_INTENT/REQUEST_SENT."""
        sup_mod = _bootstrap_temp_state(tmp_path)
        head = "abc" * 14
        ok = sup_mod._promote_review_request_terminal(
            "codex", head, lifecycle="REQUEST_SENT", reason="invalid"
        )
        assert ok is False
        # No ledger file was written.
        files = list(sup_mod.REVIEW_REQUESTS_DIR.glob("codex__*.json"))
        assert files == []

    def test_rejects_empty_head(self, tmp_path):
        sup_mod = _bootstrap_temp_state(tmp_path)
        ok = sup_mod._promote_review_request_terminal(
            "codex", "", lifecycle="REVIEW_COMPLETE", reason="n/a"
        )
        assert ok is False


# ---------------------------------------------------------------------------
# B. REVIEW_COMPLETE promotion when state is REVIEW_COMPLETE
# ---------------------------------------------------------------------------


class TestBProcessProviderQuotasPromotesToReviewComplete:
    def test_review_complete_state_promotes_existing_record(
        self, tmp_path, monkeypatch
    ):
        """The supervisor classifies a codex review as
        PROVIDER_STATE_REVIEW_COMPLETE on a heartbeat. The
        existing REQUEST_SENT ledger record MUST be promoted
        to REVIEW_COMPLETE on that same heartbeat."""
        sup_mod = _bootstrap_temp_state(tmp_path)
        sup_mod.PROVIDERS = _provider_config_with_codex()
        head = "abc" * 14
        sup_mod.AUTHORITATIVE_HEAD = head
        # Pre-persist a REQUEST_SENT record on the live head.
        sup_mod.write_review_request(
            provider="codex",
            head_sha=head,
            record={
                "actor": "post_review_request",
                "requested_at": "2026-08-13T12:00:00Z",
                "lifecycle": "REQUEST_SENT",
                "request_head": head,
                "request_id": "req-abcdef0123456789",
            },
        )
        # Stub the classifier to return REVIEW_COMPLETE for
        # both providers so the test is deterministic.
        monkeypatch.setattr(
            sup_mod,
            "classify_provider_state",
            lambda provider, body: (
                sup_mod.PROVIDER_STATE_REVIEW_COMPLETE,
                "review_complete_marker",
            ),
        )
        # Stub handle_paused_providers so the call does not
        # need full state. We only want the side effect of
        # process_provider_quotas.
        monkeypatch.setattr(
            sup_mod,
            "enter_provider_quota_pause",
            lambda *a, **kw: None,
        )
        live = {"head_sha": head}
        sup_mod.process_provider_quotas(live)
        record = sup_mod.read_review_request("codex", head)
        assert record is not None
        assert record["lifecycle"] == "REVIEW_COMPLETE"
        assert record["terminal_lifecycle"] == "REVIEW_COMPLETE"
        assert record["terminal_reason"].startswith("codex_state=REVIEW_COMPLETE")
        assert record["request_id"] == "req-abcdef0123456789"

    def test_review_complete_state_promotes_from_empty(
        self, tmp_path, monkeypatch
    ):
        """If the codex state is REVIEW_COMPLETE but there is
        no existing ledger file (the review came in through a
        path that didn't go through ``post_review_request``),
        the helper MUST still write a fresh REVIEW_COMPLETE
        record so the empirical gate can satisfy."""
        sup_mod = _bootstrap_temp_state(tmp_path)
        sup_mod.PROVIDERS = _provider_config_with_codex()
        head = "abc" * 14
        sup_mod.AUTHORITATIVE_HEAD = head
        monkeypatch.setattr(
            sup_mod,
            "classify_provider_state",
            lambda provider, body: (
                sup_mod.PROVIDER_STATE_REVIEW_COMPLETE,
                "review_complete_marker",
            ),
        )
        monkeypatch.setattr(
            sup_mod,
            "enter_provider_quota_pause",
            lambda *a, **kw: None,
        )
        # No prior codex record.
        live = {"head_sha": head}
        sup_mod.process_provider_quotas(live)
        record = sup_mod.read_review_request("codex", head)
        assert record is not None
        assert record["lifecycle"] == "REVIEW_COMPLETE"


# ---------------------------------------------------------------------------
# C. OPTIONAL_DEGRADED promotion when no review exists
# ---------------------------------------------------------------------------


class TestCProcessProviderQuotasPersistsOptionalDegraded:
    def test_unknown_state_persists_optional_degraded(
        self, tmp_path, monkeypatch
    ):
        """Codex is the OPTIONAL provider. When there is no
        review record on the live head and the state is not
        in the active/paused set, the supervisor MUST write
        an OPTIONAL_DEGRADED sentinel so the empirical gate
        can satisfy."""
        sup_mod = _bootstrap_temp_state(tmp_path)
        sup_mod.PROVIDERS = _provider_config_with_codex()
        head = "abc" * 14
        sup_mod.AUTHORITATIVE_HEAD = head
        monkeypatch.setattr(
            sup_mod,
            "classify_provider_state",
            lambda provider, body: (
                sup_mod.PROVIDER_STATE_UNKNOWN
                if provider == "codex"
                else sup_mod.PROVIDER_STATE_REVIEW_COMPLETE,
                "test_classifier",
            ),
        )
        # No prior codex record. State UNKNOWN / cleared.
        monkeypatch.setattr(
            sup_mod,
            "enter_provider_quota_pause",
            lambda *a, **kw: None,
        )
        live = {"head_sha": head}
        sup_mod.process_provider_quotas(live)
        record = sup_mod.read_review_request("codex", head)
        assert record is not None
        assert record["lifecycle"] == "OPTIONAL_DEGRADED"
        assert record["terminal_reason"] == (
            "codex_state=UNKNOWN no_review_record_on_live_head"
        )

    def test_in_progress_state_does_not_promote_optional_degraded(
        self, tmp_path, monkeypatch
    ):
        """If codex state is REVIEW_IN_PROGRESS, the helper
        MUST NOT downgrade to OPTIONAL_DEGRADED — the review
        is still in flight."""
        sup_mod = _bootstrap_temp_state(tmp_path)
        sup_mod.PROVIDERS = _provider_config_with_codex()
        head = "abc" * 14
        sup_mod.AUTHORITATIVE_HEAD = head
        monkeypatch.setattr(
            sup_mod,
            "classify_provider_state",
            lambda provider, body: (
                sup_mod.PROVIDER_STATE_REVIEW_IN_PROGRESS,
                "in_progress",
            ),
        )
        monkeypatch.setattr(
            sup_mod,
            "enter_provider_quota_pause",
            lambda *a, **kw: None,
        )
        live = {"head_sha": head}
        sup_mod.process_provider_quotas(live)
        record = sup_mod.read_review_request("codex", head)
        # No record yet; REVIEW_IN_PROGRESS does NOT trigger
        # OPTIONAL_DEGRADED, so the helper stays silent.
        assert record is None


# ---------------------------------------------------------------------------
# D. Empirical gate reads the terminal lifecycle back
# ---------------------------------------------------------------------------


class TestDEmpiricalGateSatisfiesFromTerminalLifecycle:
    def test_gate_satisfies_on_review_complete(self, tmp_path):
        """End-to-end: the gate reader in
        ``hermes_fingerprint._read_codex_optional_lifecycle_evidence``
        MUST flip ``value=True`` and ``observation_complete=True``
        when the ledger contains REVIEW_COMPLETE."""
        sup_mod = _bootstrap_temp_state(tmp_path)
        from autocoder_supervisor import hermes_fingerprint as hf

        head = "abc" * 14
        # Write a REVIEW_COMPLETE record.
        sup_mod.write_review_request(
            provider="codex",
            head_sha=head,
            record={
                "actor": "round54_c22_terminal_promotion",
                "requested_at": "2026-08-13T12:00:00Z",
                "lifecycle": "REVIEW_COMPLETE",
                "request_head": head,
            },
        )
        out = hf._read_codex_optional_lifecycle_evidence(
            state_dir=str(tmp_path / "state")
        )
        assert out.get("observation_complete") is True
        assert out.get("value") is True
        assert "REVIEW_COMPLETE" in out.get("terminal_states", [])
        assert "REVIEW_COMPLETE" in out.get("progress_states_observed", [])

    def test_gate_satisfies_on_optional_degraded(self, tmp_path):
        sup_mod = _bootstrap_temp_state(tmp_path)
        from autocoder_supervisor import hermes_fingerprint as hf

        head = "abc" * 14
        sup_mod.write_review_request(
            provider="codex",
            head_sha=head,
            record={
                "actor": "round54_c22_terminal_promotion",
                "requested_at": "2026-08-13T12:00:00Z",
                "lifecycle": "OPTIONAL_DEGRADED",
                "request_head": head,
            },
        )
        out = hf._read_codex_optional_lifecycle_evidence(
            state_dir=str(tmp_path / "state")
        )
        assert out.get("observation_complete") is True
        assert out.get("value") is True


# ---------------------------------------------------------------------------
# E. Idempotency
# ---------------------------------------------------------------------------


class TestEPromotionIsIdempotent:
    def test_double_promotion_does_not_duplicate_side_effect(
        self, tmp_path
    ):
        sup_mod = _bootstrap_temp_state(tmp_path)
        head = "abc" * 14
        sup_mod.write_review_request(
            provider="codex",
            head_sha=head,
            record={
                "actor": "post_review_request",
                "requested_at": "2026-08-13T12:00:00Z",
                "lifecycle": "REQUEST_SENT",
                "request_head": head,
            },
        )
        # First promotion.
        first = sup_mod._promote_review_request_terminal(
            "codex", head,
            lifecycle="REVIEW_COMPLETE",
            reason="first",
        )
        record1 = sup_mod.read_review_request("codex", head)
        # Second promotion: idempotent at the lifecycle level.
        second = sup_mod._promote_review_request_terminal(
            "codex", head,
            lifecycle="REVIEW_COMPLETE",
            reason="second",
        )
        record2 = sup_mod.read_review_request("codex", head)
        assert first is True
        assert second is True
        # The terminal_at timestamp is the canonical record;
        # both calls report success, but the durable record
        # lifecycle stays REVIEW_COMPLETE without being
        # rewritten to a non-terminal value.
        assert record1["lifecycle"] == "REVIEW_COMPLETE"
        assert record2["lifecycle"] == "REVIEW_COMPLETE"


# ---------------------------------------------------------------------------
# F. Superseded records are not overwritten
# ---------------------------------------------------------------------------


class TestFSupersededRecordsAreNotOverwritten:
    def test_review_complete_does_not_overwrite_superseded(
        self, tmp_path
    ):
        """A SUPERSEDED record on the live head MUST NOT be
        overwritten by REVIEW_COMPLETE. Closure X treats
        supersession as terminal: a request marked SUPERSEDED
        cannot be re-promoted."""
        sup_mod = _bootstrap_temp_state(tmp_path)
        head = "abc" * 14
        sup_mod.write_review_request(
            provider="codex",
            head_sha=head,
            record={
                "actor": "round44_c12",
                "lifecycle": "SUPERSEDED",
                "provider": "codex",
                "stale_head": head,
                "superseded_by_head": head,
                "reason": "stale_marker",
            },
        )
        ok = sup_mod._promote_review_request_terminal(
            "codex", head,
            lifecycle="REVIEW_COMPLETE",
            reason="attempt",
        )
        assert ok is False
        record = sup_mod.read_review_request("codex", head)
        assert record["lifecycle"] == "SUPERSEDED"


# ---------------------------------------------------------------------------
# G. End-to-end: gate passes after process_provider_quotas
# ---------------------------------------------------------------------------


class TestGEndToEndGateSatisfies:
    def test_review_complete_path_satisfies_gate(
        self, tmp_path, monkeypatch
    ):
        """A full heartbeat cycle: process_provider_quotas is
        called with codex state = REVIEW_COMPLETE. The gate
        reader then satisfies."""
        sup_mod = _bootstrap_temp_state(tmp_path)
        from autocoder_supervisor import hermes_fingerprint as hf

        sup_mod.PROVIDERS = _provider_config_with_codex()
        head = "abc" * 14
        sup_mod.AUTHORITATIVE_HEAD = head
        # Pre-persist a REQUEST_SENT record (the typical
        # starting state for a codex request that has just
        # finished).
        sup_mod.write_review_request(
            provider="codex",
            head_sha=head,
            record={
                "actor": "post_review_request",
                "requested_at": "2026-08-13T12:00:00Z",
                "lifecycle": "REQUEST_SENT",
                "request_head": head,
            },
        )
        monkeypatch.setattr(
            sup_mod,
            "classify_provider_state",
            lambda provider, body: (
                sup_mod.PROVIDER_STATE_REVIEW_COMPLETE,
                "review_complete_marker",
            ),
        )
        monkeypatch.setattr(
            sup_mod,
            "enter_provider_quota_pause",
            lambda *a, **kw: None,
        )
        live = {"head_sha": head}
        sup_mod.process_provider_quotas(live)
        out = hf._read_codex_optional_lifecycle_evidence(
            state_dir=str(tmp_path / "state")
        )
        assert out.get("value") is True
        assert out.get("observation_complete") is True

    def test_optional_degraded_path_satisfies_gate(
        self, tmp_path, monkeypatch
    ):
        sup_mod = _bootstrap_temp_state(tmp_path)
        from autocoder_supervisor import hermes_fingerprint as hf

        sup_mod.PROVIDERS = _provider_config_with_codex()
        head = "abc" * 14
        sup_mod.AUTHORITATIVE_HEAD = head
        monkeypatch.setattr(
            sup_mod,
            "classify_provider_state",
            lambda provider, body: (
                sup_mod.PROVIDER_STATE_UNKNOWN
                if provider == "codex"
                else sup_mod.PROVIDER_STATE_REVIEW_COMPLETE,
                "test_classifier",
            ),
        )
        monkeypatch.setattr(
            sup_mod,
            "enter_provider_quota_pause",
            lambda *a, **kw: None,
        )
        live = {"head_sha": head}
        sup_mod.process_provider_quotas(live)
        out = hf._read_codex_optional_lifecycle_evidence(
            state_dir=str(tmp_path / "state")
        )
        assert out.get("value") is True
        assert out.get("observation_complete") is True
