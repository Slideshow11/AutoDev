"""Tests for autocoder_orchestration.reconciliation."""
from __future__ import annotations

import pytest

from autocoder_orchestration.reconciliation import (
    Reconciler,
    Finding,
    FindingDisposition,
    ThreadResolution,
)


HEAD = "a" * 64


def _finding(
    thread_id: str = "t1",
    head: str = HEAD,
    description: str = "test finding",
    is_outdated: bool = False,
    disposition: FindingDisposition = FindingDisposition.OPEN_VALID,
    repair_commit_sha: str | None = None,
) -> Finding:
    import hashlib
    return Finding(
        provider="coderabbitai",
        review_id="r1",
        thread_id=thread_id,
        path="autocoder_orchestration/x.py",
        line=42,
        head_sha=head,
        is_outdated=is_outdated,
        severity="major",
        description=description,
        description_hash=hashlib.sha256(description.encode()).hexdigest(),
        disposition=disposition,
        repair_commit_sha=repair_commit_sha,
    )


class TestFinding:
    def test_finding_constructs(self) -> None:
        f = _finding()
        assert f.thread_id == "t1"
        assert f.disposition == FindingDisposition.OPEN_VALID

    def test_invalid_provider_rejected(self) -> None:
        with pytest.raises(ValueError):
            f = _finding()
            f = Finding(
                provider="bad/provider",
                review_id="r1",
                thread_id="t1",
                path="x.py",
                line=1,
                head_sha=HEAD,
                is_outdated=False,
                severity="x",
                description="x",
                description_hash="x",
                disposition=FindingDisposition.OPEN_VALID,
            )

    def test_invalid_review_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            Finding(
                provider="coderabbitai",
                review_id="r/1",
                thread_id="t1",
                path="x.py",
                line=1,
                head_sha=HEAD,
                is_outdated=False,
                severity="x",
                description="x",
                description_hash="x",
                disposition=FindingDisposition.OPEN_VALID,
            )

    def test_invalid_thread_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            Finding(
                provider="coderabbitai",
                review_id="r1",
                thread_id="t/1",
                path="x.py",
                line=1,
                head_sha=HEAD,
                is_outdated=False,
                severity="x",
                description="x",
                description_hash="x",
                disposition=FindingDisposition.OPEN_VALID,
            )

    def test_invalid_head_sha_rejected(self) -> None:
        with pytest.raises(ValueError):
            Finding(
                provider="coderabbitai",
                review_id="r1",
                thread_id="t1",
                path="x.py",
                line=1,
                head_sha="not_sha",
                is_outdated=False,
                severity="x",
                description="x",
                description_hash="x",
                disposition=FindingDisposition.OPEN_VALID,
            )

    def test_description_hash_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError, match="description_hash"):
            Finding(
                provider="coderabbitai",
                review_id="r1",
                thread_id="t1",
                path="x.py",
                line=1,
                head_sha=HEAD,
                is_outdated=False,
                severity="x",
                description="actual description",
                description_hash="z" * 64,
                disposition=FindingDisposition.OPEN_VALID,
            )

    def test_roundtrip(self) -> None:
        f = _finding()
        d = f.to_dict()
        restored = Finding.from_dict(d)
        assert restored.thread_id == f.thread_id
        assert restored.description_hash == f.description_hash


class TestIsResolvable:
    def test_repaired_is_resolvable(self) -> None:
        assert _finding(disposition=FindingDisposition.REPAIRED).is_resolvable()

    def test_superseded_is_resolvable(self) -> None:
        assert _finding(disposition=FindingDisposition.SUPERSEDED).is_resolvable()

    def test_invalid_is_resolvable(self) -> None:
        assert _finding(disposition=FindingDisposition.INVALID).is_resolvable()

    def test_open_valid_not_resolvable(self) -> None:
        assert not _finding(disposition=FindingDisposition.OPEN_VALID).is_resolvable()

    def test_inconclusive_not_resolvable(self) -> None:
        assert not _finding(disposition=FindingDisposition.INCONCLUSIVE).is_resolvable()


class TestReconciler:
    def test_resolved_thread_classified_repaired(self) -> None:
        r = Reconciler(HEAD)
        raw = {"id": "t1", "isResolved": True}
        f = r.classify(
            raw_thread=raw,
            provider="coderabbitai",
            review_id="r1",
            description="x",
            path="x.py",
            line=1,
        )
        assert f.disposition == FindingDisposition.REPAIRED

    def test_outdated_thread_classified_superseded(self) -> None:
        r = Reconciler(HEAD)
        raw = {"id": "t1", "isResolved": False}
        f = r.classify(
            raw_thread=raw,
            provider="coderabbitai",
            review_id="r1",
            description="x",
            is_outdated=True,
        )
        assert f.disposition == FindingDisposition.SUPERSEDED

    def test_unresolved_thread_classified_open_valid(self) -> None:
        r = Reconciler(HEAD)
        raw = {"id": "t1", "isResolved": False}
        f = r.classify(
            raw_thread=raw,
            provider="coderabbitai",
            review_id="r1",
            description="x",
        )
        assert f.disposition == FindingDisposition.OPEN_VALID


class TestResolutionEligibility:
    def test_repaired_resolution_eligible(self) -> None:
        r = Reconciler(HEAD)
        f = _finding(disposition=FindingDisposition.REPAIRED)
        ok, reason = r.is_resolution_eligible(f, HEAD, later_comment_reopened=False)
        assert ok, reason

    def test_open_valid_not_eligible(self) -> None:
        r = Reconciler(HEAD)
        f = _finding(disposition=FindingDisposition.OPEN_VALID)
        ok, reason = r.is_resolution_eligible(f, HEAD, later_comment_reopened=False)
        assert not ok

    def test_reopened_finding_not_eligible(self) -> None:
        r = Reconciler(HEAD)
        f = _finding(disposition=FindingDisposition.REPAIRED)
        ok, reason = r.is_resolution_eligible(f, HEAD, later_comment_reopened=True)
        assert not ok
        assert "reopened" in reason

    def test_head_drift_blocks_non_superseded(self) -> None:
        r = Reconciler(HEAD)
        f = _finding(disposition=FindingDisposition.REPAIRED, head="b" * 64)
        ok, reason = r.is_resolution_eligible(f, HEAD, later_comment_reopened=False)
        assert not ok

    def test_superseded_allows_head_drift(self) -> None:
        r = Reconciler(HEAD)
        f = _finding(disposition=FindingDisposition.SUPERSEDED, head="b" * 64)
        ok, reason = r.is_resolution_eligible(f, HEAD, later_comment_reopened=False)
        assert ok


class TestThreadResolution:
    def test_resolution_constructs(self) -> None:
        f = _finding(disposition=FindingDisposition.REPAIRED)
        r = ThreadResolution(
            finding=f,
            resolved_by="controller",
            resolved_at="2026-08-05T22:00:00Z",
            rationale="finding repaired in commit X",
        )
        assert r.finding.thread_id == "t1"

    def test_open_valid_resolution_rejected(self) -> None:
        f = _finding(disposition=FindingDisposition.OPEN_VALID)
        with pytest.raises(ValueError):
            ThreadResolution(
                finding=f,
                resolved_by="controller",
                resolved_at="2026-08-05T22:00:00Z",
                rationale="x",
            )
