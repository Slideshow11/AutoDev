"""Round-590/P1: the canonical CodeRabbit
``provider_head_assessment/coderabbit/<head>.json``
artifact must have a PRODUCTION writer, not just the
hand-fabricated test fixture that the
``_read_coderabbit_clean_head_evidence`` reader requires.

Without this writer the clean-head gate always fails with
``no_canonical_provider_head_assessment_artifact`` and the
PR is permanently blocked, regardless of whether the live
CodeRabbit review is actually clean.

Tests cover:
  - ``persist_coderabbit_head_assessment`` writes the
    canonical artifact when the snapshot is complete and
    clean
  - a stale artifact for a prior head is rotated to
    ``<old>.superseded.json``
  - surfaces_complete=False (no formal review at head)
    refuses to write the canonical artifact
  - surfaces_complete=True but actionable findings present
    writes the artifact with ``clean=False``
  - the reader at
    ``_read_coderabbit_clean_head_evidence`` is satisfied
    by an artifact written through the producer
  - empty snap / empty expected_head fail closed
  - test_writer_round_trip_idempotent: writing the same
    head twice leaves only one active file
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


HEAD_FULL = "cd15d30c1111111111111111111111111111aaaa"
HEAD_PRIOR = "ab12cd340000000000000000000000000000ffff"
HEAD_SHORT = HEAD_FULL[:12]


def _make_snapshot(*, head: str = HEAD_FULL) -> dict:
    """Build a complete, clean CodeRabbit snapshot for tests."""
    return {
        "head_sha": head,
        "formal_reviews": [
            {
                "id": 100,
                "submitted_at": "2026-08-15T10:00:00Z",
                "commit_id": head,
                "provider": "coderabbit",
                "login": "coderabbitai[bot]",
            },
        ],
        "review_comments": [
            {
                "id": 200,
                "body": "nit: rename variable for clarity",
                "path": "src/example.py",
                "line": 12,
                "commit_id": head,
                "user": {"login": "coderabbitai[bot]"},
            },
        ],
        "issue_comments": [
            {
                "id": 300,
                "body": (
                    f"All findings addressed. Review completed at "
                    f"head `{head[:7]}`."
                ),
                "created_at": "2026-08-15T10:01:00Z",
                "user": {"login": "coderabbitai[bot]"},
            },
        ],
        "review_threads": {
            "thread-1": {
                "resolved": True,
                "outdated": False,
                "owner": "coderabbit",
            },
        },
    }


class TestPersistCoderabbitHeadAssessment:
    def test_writer_creates_canonical_artifact(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            persist_coderabbit_head_assessment,
        )
        snap = _make_snapshot()
        out = persist_coderabbit_head_assessment(
            snap=snap,
            state_dir=str(tmp_path),
            expected_head=HEAD_FULL,
            now_iso_fn=lambda: "2026-08-15T10:02:00+00:00",
        )
        assert out["written_path"] is not None
        artifact_path = (
            tmp_path / "provider_head_assessment" / "coderabbit"
            / f"{HEAD_FULL}.json"
        )
        assert artifact_path.exists()
        data = json.loads(artifact_path.read_text())
        assert data["provider"] == "coderabbit"
        assert data["head_sha"] == HEAD_FULL
        assert data["observation_complete"] is True
        assert data["clean"] is True
        assert data["surfaces"]["formal_reviews_collected"] is True
        assert data["surfaces"]["inline_comments_collected"] is True
        assert data["surfaces"]["top_level_comment_collected"] is True
        assert data["surfaces"]["review_threads_collected"] is True
        assert data["surfaces"]["statuses_collected"] is True
        assert data["actionable_finding_ids"] == []
        assert data["completion_proof"]["exact_head_status"] == (
            "success"
        )

    def test_writer_satisfies_clean_head_reader(self, tmp_path):
        """Round-590/P1: the artifact written by the producer
        MUST satisfy ``_read_coderabbit_clean_head_evidence``.
        Without a producer, this round-trip is impossible."""
        from autocoder_supervisor.hermes_fingerprint import (
            _read_coderabbit_clean_head_evidence,
            persist_coderabbit_head_assessment,
        )
        snap = _make_snapshot()
        out = persist_coderabbit_head_assessment(
            snap=snap,
            state_dir=str(tmp_path),
            expected_head=HEAD_FULL,
            now_iso_fn=lambda: "2026-08-15T10:02:00+00:00",
        )
        assert out["written_path"] is not None
        reader = _read_coderabbit_clean_head_evidence(
            state_dir=str(tmp_path),
            expected_head=HEAD_FULL,
        )
        assert reader["value"] is True
        assert reader["reason"] == "ok_canonical_head_assessment"
        assert reader["evidence_head"] == HEAD_FULL

    def test_writer_rotates_prior_head_artifact(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            persist_coderabbit_head_assessment,
        )
        # First write at the prior head.
        snap_prior = _make_snapshot(head=HEAD_PRIOR)
        out1 = persist_coderabbit_head_assessment(
            snap=snap_prior,
            state_dir=str(tmp_path),
            expected_head=HEAD_PRIOR,
            now_iso_fn=lambda: "2026-08-14T00:00:00+00:00",
        )
        assert out1["written_path"] is not None
        prior_path = (
            tmp_path / "provider_head_assessment" / "coderabbit"
            / f"{HEAD_PRIOR}.json"
        )
        assert prior_path.exists()
        # Now persist at the new head; the prior must rotate.
        snap_new = _make_snapshot(head=HEAD_FULL)
        out2 = persist_coderabbit_head_assessment(
            snap=snap_new,
            state_dir=str(tmp_path),
            expected_head=HEAD_FULL,
            now_iso_fn=lambda: "2026-08-15T10:00:00+00:00",
        )
        assert out2["written_path"] is not None
        assert (
            f"{HEAD_PRIOR}.superseded.json" in out2["rotated_paths"][0]
        )
        rotated_path = (
            tmp_path / "provider_head_assessment" / "coderabbit"
            / f"{HEAD_PRIOR}.superseded.json"
        )
        assert rotated_path.exists()
        # Active artifact now points at the new head.
        active_path = (
            tmp_path / "provider_head_assessment" / "coderabbit"
            / f"{HEAD_FULL}.json"
        )
        assert active_path.exists()
        # Prior active path must be gone (replaced).
        assert not prior_path.exists()

    def test_writer_refuses_when_formal_review_missing(self, tmp_path):
        """If no CodeRabbit formal review is bound to the head,
        surfaces_complete is False and the canonical artifact
        MUST NOT be written. The reader would otherwise
        classify a partial observation as clean evidence."""
        from autocoder_supervisor.hermes_fingerprint import (
            persist_coderabbit_head_assessment,
        )
        snap = _make_snapshot()
        # Strip the formal review to break surfaces_complete.
        snap["formal_reviews"] = []
        out = persist_coderabbit_head_assessment(
            snap=snap,
            state_dir=str(tmp_path),
            expected_head=HEAD_FULL,
        )
        assert out["written_path"] is None
        assert out["observation_complete"] is False
        assert out["clean"] is False
        # The artifact MUST NOT have been written.
        artifact_path = (
            tmp_path / "provider_head_assessment" / "coderabbit"
            / f"{HEAD_FULL}.json"
        )
        assert not artifact_path.exists()

    def test_writer_records_unclean_state(self, tmp_path):
        """An unclean observation (actionable findings present)
        MUST still produce an artifact, but with clean=False.
        The reader relies on clean=False to refuse the gate."""
        from autocoder_supervisor.hermes_fingerprint import (
            persist_coderabbit_head_assessment,
        )
        snap = _make_snapshot()
        # Add an unresolved thread to introduce actionable findings.
        snap["review_threads"]["thread-2"] = {
            "resolved": False,
            "outdated": False,
            "owner": "coderabbit",
        }
        out = persist_coderabbit_head_assessment(
            snap=snap,
            state_dir=str(tmp_path),
            expected_head=HEAD_FULL,
            now_iso_fn=lambda: "2026-08-15T10:02:00+00:00",
        )
        assert out["written_path"] is not None
        artifact_path = (
            tmp_path / "provider_head_assessment" / "coderabbit"
            / f"{HEAD_FULL}.json"
        )
        data = json.loads(artifact_path.read_text())
        assert data["observation_complete"] is True
        assert data["clean"] is False
        assert "thread-2" in data["actionable_finding_ids"]

    def test_writer_fails_closed_on_empty_snap(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            persist_coderabbit_head_assessment,
        )
        out = persist_coderabbit_head_assessment(
            snap=None,
            state_dir=str(tmp_path),
            expected_head=HEAD_FULL,
        )
        assert out["written_path"] is None
        assert out["error"] == "snap_not_dict"

    def test_writer_fails_closed_on_missing_head(self, tmp_path):
        from autocoder_supervisor.hermes_fingerprint import (
            persist_coderabbit_head_assessment,
        )
        snap = _make_snapshot()
        out = persist_coderabbit_head_assessment(
            snap=snap,
            state_dir=str(tmp_path),
            expected_head="",
        )
        assert out["written_path"] is None
        assert out["error"] == "no_expected_head"

    def test_writer_idempotent_same_head(self, tmp_path):
        """Writing twice for the same head leaves exactly one
        active artifact; no rotation is triggered."""
        from autocoder_supervisor.hermes_fingerprint import (
            persist_coderabbit_head_assessment,
        )
        snap = _make_snapshot()
        out1 = persist_coderabbit_head_assessment(
            snap=snap,
            state_dir=str(tmp_path),
            expected_head=HEAD_FULL,
        )
        out2 = persist_coderabbit_head_assessment(
            snap=snap,
            state_dir=str(tmp_path),
            expected_head=HEAD_FULL,
        )
        assert out1["written_path"] == out2["written_path"]
        assert out2["rotated_paths"] == []
        asm_dir = (
            tmp_path / "provider_head_assessment" / "coderabbit"
        )
        active = list(asm_dir.glob("*.json"))
        assert len(active) == 1
        assert active[0].name == f"{HEAD_FULL}.json"