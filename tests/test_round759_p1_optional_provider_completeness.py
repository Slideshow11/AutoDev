"""Round-759 P1: optional-provider outages must not poison the global
completeness gate.

When ``collect_provider_surfaces`` raises for an OPTIONAL provider
(e.g. Codex, whose ``required_for_current_repair_round`` is False),
``provider_surface_complete`` must stay True so ``evaluate_readiness()``
can still qualify the head based on every required CodeRabbit surface
being complete. A REQUIRED provider (e.g. coderabbit) failure must
still flip ``provider_surface_complete`` to False so the relay
returns a recoverable retry rather than entering readiness on
incomplete evidence.
"""

from __future__ import annotations

import json

import pytest

from autocoder_supervisor import supervisor as _sup


def _stub_live_snapshot_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the network-touching helpers ``capture_live_snapshot`` uses so
    it can run hermetically without touching GitHub."""
    monkeypatch.setattr(
        "autocoder_supervisor.supervisor.AUTHORITATIVE_HEAD",
        "a" * 40,
    )
    monkeypatch.setattr(
        "autocoder_supervisor.supervisor.PR_NUMBER", 5,
    )
    monkeypatch.setattr(
        "autocoder_supervisor.supervisor.REPO_OWNER", "owner",
    )
    monkeypatch.setattr(
        "autocoder_supervisor.supervisor.REPO_NAME", "repo",
    )

    def _fake_safe_github_get(url: str, token: str):
        if "/pulls/" in url and "/reviews" not in url:
            return {"head": {"sha": "a" * 40}, "mergeable": True}
        if "/reviews" in url:
            return []
        if "/check-runs" in url:
            return {"check_runs": []}
        if "/issues/" in url and "/comments" in url:
            return []
        return None

    monkeypatch.setattr(_sup, "safe_github_get", _fake_safe_github_get)

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self) -> bytes:
            return json.dumps({
                "data": {"repository": {"pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {
                            "hasNextPage": False, "endCursor": None,
                        },
                        "nodes": [],
                    },
                }}},
            }).encode()

    import urllib.request as _ur
    monkeypatch.setattr(
        _ur, "urlopen", lambda req, timeout=20: _FakeResp(),
    )


class TestOptionalProviderFailureKeepsComplete:
    """An optional provider API failure must not poison the global
    ``provider_surface_complete`` gate."""

    def test_codex_optional_failure_keeps_complete_true(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Sanity: codex is configured as optional.
        assert (
            _sup.PROVIDERS["codex"]["required_for_current_repair_round"]
            is False
        )
        assert (
            _sup.PROVIDERS["coderabbit"]["required_for_current_repair_round"]
            is True
        )

        # Stub collect_provider_surfaces: codex raises (simulating
        # an outage of its per-review comments API); coderabbit
        # returns a normal surface.
        def _fake_collect(
            provider_name: str, head: str, token: str,
        ) -> dict:
            if provider_name == "codex":
                raise RuntimeError(
                    "codex per-review comments API unavailable",
                )
            return {
                "provider": provider_name,
                "review_comments": [],
                "issue_comments": [],
            }

        monkeypatch.setattr(_sup, "collect_provider_surfaces", _fake_collect)
        _stub_live_snapshot_calls(monkeypatch)

        snap = _sup.capture_live_snapshot(
            {"current_head": "a" * 40}, "",
        )

        # The failure MUST be recorded for observability.
        assert "codex" in snap["provider_surface_failures"]
        assert (
            "unavailable"
            in snap["provider_surface_failures"]["codex"]
        )
        # BUT the global completeness gate MUST stay True because
        # the failing provider is OPTIONAL.
        assert snap["provider_surface_complete"] is True, (
            "optional codex outage must not poison the global "
            "completeness gate; only REQUIRED provider failures "
            "should set provider_surface_complete=False"
        )

    def test_coderabbit_required_failure_marks_complete_false(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Sanity: coderabbit is configured as required.
        assert (
            _sup.PROVIDERS["coderabbit"]["required_for_current_repair_round"]
            is True
        )

        def _fake_collect(
            provider_name: str, head: str, token: str,
        ) -> dict:
            if provider_name == "coderabbit":
                raise RuntimeError("coderabbit API unavailable")
            return {
                "provider": provider_name,
                "review_comments": [],
                "issue_comments": [],
            }

        monkeypatch.setattr(_sup, "collect_provider_surfaces", _fake_collect)
        _stub_live_snapshot_calls(monkeypatch)

        snap = _sup.capture_live_snapshot(
            {"current_head": "a" * 40}, "",
        )

        assert snap["provider_surface_complete"] is False
        assert "coderabbit" in snap["provider_surface_failures"]


class TestOptionalProviderFailureDoesNotBlockReadiness:
    """Integration: a Codex-only outage must not block
    ``evaluate_readiness()`` when every required surface is
    otherwise complete."""

    def test_codex_failure_does_not_block_readiness_via_evaluate(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Hermetic snapshot: provider_surface_complete stays True
        # even though codex failure is recorded.
        snap = {
            "head_sha": "a" * 40,
            "providers": {
                "codex": {
                    "clean": False, "paused": True,
                    "in_progress": False,
                },
                "coderabbit": {
                    "clean": True, "paused": False,
                    "in_progress": False,
                },
            },
            "checks": {},
            "unconsumed_event_ids": [],
            # Round-759: optional outage does NOT poison this.
            "provider_surface_complete": True,
            "provider_surface_failures": {
                "codex": "codex API unavailable",
            },
        }

        def _empty_unconsumed() -> list:
            return []

        monkeypatch.setattr(
            _sup, "list_unconsumed_events", _empty_unconsumed,
        )
        res = _sup.evaluate_readiness(snap, "a" * 40)
        assert res["ready"] is True


def test_synthetic_optional_provider_failure_stays_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synthetic provider explicitly configured as optional
    (``required_for_current_repair_round=False``) does not
    poison the gate when it fails."""

    synthetic_name = "synthetic_optional"
    _sup.PROVIDERS[synthetic_name] = {
        "bot_logins": [f"{synthetic_name}[bot]"],
        "trigger_handle": f"@{synthetic_name} review",
        "quota_patterns": [],
        "use_reviews_api": True,
        "required_for_current_repair_round": False,
        "required_for_final_merge": False,
        "required_for_pr_416": False,
        "quota_reset_at": None,
    }
    try:

        def _fake_collect(
            provider_name: str, head: str, token: str,
        ) -> dict:
            if provider_name == synthetic_name:
                raise RuntimeError("synthetic provider down")
            return {
                "provider": provider_name,
                "review_comments": [],
                "issue_comments": [],
            }

        monkeypatch.setattr(_sup, "collect_provider_surfaces", _fake_collect)
        _stub_live_snapshot_calls(monkeypatch)

        snap = _sup.capture_live_snapshot(
            {"current_head": "a" * 40}, "",
        )

        assert snap["provider_surface_complete"] is True
        assert synthetic_name in snap["provider_surface_failures"]
    finally:
        _sup.PROVIDERS.pop(synthetic_name, None)
