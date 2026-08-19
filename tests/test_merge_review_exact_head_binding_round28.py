"""Round-28 P3: locked exact-head binding of CodeRabbit reviews.

User-supplied invariant:
  The locked CodeRabbit review fetch MUST query the review
  commit identity. For every review candidate obtain:
    canonical reviewer login;
    review state;
    commit { oid } or equivalent reviewed-commit identity.
  Require:
    reviewer identity == configured canonical CodeRabbit
      identity using exact normalized comparison;
    review state acceptable;
    review commit OID == authorized exact head.
  Do NOT infer review binding from the PR's current headRefOid.
  A current PR head plus an old APPROVED review is NOT
  exact-head approval.

Tests:
  A approved by CodeRabbit on head A
  → PR advances to head B
  → GitHub retains old approval
  → locked re-fetch sees current head B + review commit A
  → merge refused.

  Also prove lookalike accounts such as coderabbit-helper
  cannot satisfy the gate.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from autocoder_orchestration.merge_authorization import (
    MergeGateChanged,
    MergeTransactionInputs,
    fetch_live_review_state,
    _normalize_canonical_reviewer_login,
)


# ===== Test: lookalike accounts (coderabbit-helper) cannot satisfy the gate =====

def test_normalize_canonical_reviewer_login_strips_bot_suffix() -> None:
    """``_normalize_canonical_reviewer_login`` lowercases and
    strips ``[bot]`` so ``coderabbitai[bot]`` and
    ``coderabbitai`` both match the canonical
    ``coderabbitai`` reference.
    """
    assert _normalize_canonical_reviewer_login("coderabbitai") == "coderabbitai"
    assert _normalize_canonical_reviewer_login("coderabbitai[bot]") == "coderabbitai"
    assert _normalize_canonical_reviewer_login("CodeRabbitAI[bot]") == "coderabbitai"
    assert _normalize_canonical_reviewer_login("  coderabbitai  ") == "coderabbitai"


def test_normalize_canonical_reviewer_login_rejects_lookalikes() -> None:
    """Lookalike accounts such as ``coderabbit-helper`` MUST NOT
    normalize to the canonical ``coderabbitai``.
    """
    assert _normalize_canonical_reviewer_login("coderabbit-helper") != "coderabbitai"
    assert _normalize_canonical_reviewer_login("coderabbit-helper[bot]") != "coderabbitai"
    assert _normalize_canonical_reviewer_login("coderabbitia") != "coderabbitai"
    assert _normalize_canonical_reviewer_login("") != "coderabbitai"


# ===== Test: fetch_live_review_state returns commit.oid per review =====

def test_fetch_live_review_state_includes_commit_oid() -> None:
    """``fetch_live_review_state`` returns each review with a
    ``commit_oid`` field AND a top-level
    ``latest_coderabbit_commit_oid``. The production code uses
    these to enforce the round-28 P3 exact-head binding.
    """
    runner_calls: List[Tuple] = []

    def fake_runner(*args, **kwargs):
        runner_calls.append((args, kwargs))
        return {
            "returncode": 0,
            "stdout": json.dumps({
                "data": {"repository": {"pullRequest": {
                    "headRefOid": "b" * 40,
                    "reviews": {"nodes": [{
                        "state": "APPROVED",
                        "author": {"login": "coderabbitai[bot]"},
                        "submittedAt": "2026-01-01T00:00:00Z",
                        "commit": {"oid": "a" * 40},
                    }]},
                }}},
            }),
            "stderr": "", "timed_out": False,
        }

    out = fetch_live_review_state(
        "gh", "owner/repo", 4, runner=fake_runner,
    )
    assert out["latest_coderabbit_state"] == "APPROVED"
    assert out["latest_coderabbit_commit_oid"] == "a" * 40, (
        f"latest_coderabbit_commit_oid MUST be exposed at the top "
        f"level; got {out!r}"
    )
    assert out["reviews"][0]["commit_oid"] == "a" * 40, (
        f"each review MUST carry commit_oid; got {out['reviews']!r}"
    )


def test_fetch_live_review_state_lookalike_account_does_not_match() -> None:
    """A review authored by ``coderabbit-helper`` MUST NOT be
    treated as the canonical CodeRabbit review. The
    ``latest_coderabbit_state`` MUST be ``None`` when only a
    lookalike account has reviewed.
    """
    def fake_runner(*args, **kwargs):
        return {
            "returncode": 0,
            "stdout": json.dumps({
                "data": {"repository": {"pullRequest": {
                    "headRefOid": "b" * 40,
                    "reviews": {"nodes": [{
                        "state": "APPROVED",
                        "author": {"login": "coderabbit-helper[bot]"},
                        "submittedAt": "2026-01-01T00:00:00Z",
                        "commit": {"oid": "a" * 40},
                    }]},
                }}},
            }),
            "stderr": "", "timed_out": False,
        }
    out = fetch_live_review_state(
        "gh", "owner/repo", 4, runner=fake_runner,
    )
    # Lookalike-only: latest_coderabbit_state is None.
    assert out["latest_coderabbit_state"] is None, (
        f"lookalike account MUST NOT satisfy the canonical "
        f"reviewer match; got {out!r}"
    )
    # The raw review IS still in the list (for diagnostics) but
    # ``latest_coderabbit_commit_oid`` is None.
    assert out["latest_coderabbit_commit_oid"] is None


# ===== Test: production-path gate rejects stale APPROVED against advanced head =====

class TestRound28P3GateProductionPath:
    """Round-28 P3: the locked merge gate rejects a stale APPROVED
    CodeRabbit review against a head the PR has already advanced
    past.

    Sequence:
      Head A: CodeRabbit approves commit A
      PR advances to commit B
      GitHub retains the old APPROVED review (does NOT
      automatically dismiss approvals when the head advances)
      The locked re-fetch sees:
        - current headRefOid == B (live PR head)
        - review commit.oid == A (old approval)
      The merge gate MUST refuse the merge because the
      review commit OID does NOT match the authorized exact
      head.
    """

    def _setup_inputs(self, tmp_path: Path, *, authorized_head: str) -> MergeTransactionInputs:
        """Build inputs wired to a fake ``_safe_run`` that
        emits the live review with a STALE commit OID. The
        gate MUST refuse the merge because the review OID
        differs from the authorized head.
        """
        # The fake returns the SAME PR payload + review for
        # every call. The review was authored against commit A
        # but the current PR head is B. The authorized head is B.
        live_pr_payload = {
            "state": "open", "merged": False,
            "head": {"sha": "b" * 40},  # current PR head
            "baseRefName": "main", "mergeable": "MERGEABLE",
            "autoMergeRequest": None, "isDraft": False,
            "reviewDecision": "APPROVED",
            "repo": "owner/repo",
        }
        # The CodeRabbit review was submitted against commit A
        # (the old head). GitHub retains the approval because
        # the user did not dismiss it.
        def fake(*args, **kwargs):
            return {
                "returncode": 0,
                "stdout": json.dumps({
                    "data": {"repository": {"pullRequest": {
                        "headRefOid": "b" * 40,
                        "reviews": {"nodes": [{
                            "state": "APPROVED",
                            "author": {"login": "coderabbitai[bot]"},
                            "submittedAt": "2026-01-01T00:00:00Z",
                            "commit": {"oid": "a" * 40},  # STALE!
                        }]},
                    }}},
                }),
                "stderr": "", "timed_out": False,
            }
        from autocoder_orchestration.merge_authorization import (
            _build_default_live_fetchers,
        )
        inputs = MergeTransactionInputs(
            authorization_artifact_path=tmp_path / "authorization.json",
            candidate_artifact_path=tmp_path / "candidate.json",
            verifier_artifact_path=tmp_path / "verifier.json",
            merge_record_artifact_path=tmp_path / "merge-record.json",
            repository_checkout=tmp_path,
            run_state_root=tmp_path / "state",
            evidence_root=tmp_path / "evidence",
            live_pr_payload=live_pr_payload,
            live_ci_state={"all_required_passing": True, "coderabbit_passing": True},
            live_review_state={"latest_coderabbit_state": "APPROVED"},
            live_thread_inventory={"unresolved_current": 0, "unresolved_outdated": 0},
            working_tree_clean=True,
            required_ci_names=(),
            _bypass_oid_reachability=True,
        )
        # Round-28 P3: provide a fetcher override that
        # simulates the real ``fetch_live_review_state``
        # returning a stale review commit OID. The fake
        # closure MUST be used directly (the gate fetches
        # via the injected closures, not via ``_safe_run``).
        def review_fetcher() -> Dict[str, Any]:
            return {
                "head_sha": "b" * 40,
                "reviews": [{
                    "state": "APPROVED",
                    "author": "coderabbitai[bot]",
                    "submitted_at": "2026-01-01T00:00:00Z",
                    "commit_oid": "a" * 40,  # STALE!
                }],
                "latest_coderabbit_state": "APPROVED",
                "latest_coderabbit_commit_oid": "a" * 40,  # STALE!
                "latest_coderabbit_login": "coderabbitai[bot]",
                "canonical_reviewer_login": "coderabbitai",
                "repo": "owner/repo",
            }
        fetchers = _build_default_live_fetchers(inputs)
        fetchers["review_state"] = review_fetcher
        inputs._set_live_fetchers(fetchers)
        return inputs

    def test_stale_approval_against_advanced_head_refused(
        self, tmp_path: Path,
    ) -> None:
        """The user's specific scenario. RunContext authorized
        against head B (current PR head), but the live
        CodeRabbit review was submitted against the OLD head A.
        The merge gate MUST refuse.
        """
        from autocoder_orchestration.merge_authorization import (
            execute_guarded_merge_transaction,
        )
        authorized_head = "b" * 40  # authorized against head B
        inputs = self._setup_inputs(tmp_path, authorized_head=authorized_head)
        # The merge gate reads auth.authorized_head. Build a
        # stub authorization artifact so the gate sees the
        # authorized head.
        candidate = {"exact_head": authorized_head, "files": []}
        from autocoder_orchestration.artifacts import write_artifact
        write_artifact(tmp_path / "candidate.json", candidate)
        verifier = {"verdict": "VERIFIED", "defects": [], "candidate_sha256": ""}
        write_artifact(tmp_path / "verifier.json", verifier)
        auth_payload = {
            "schema_version": "autocoder.merge_authorization.v1",
            "run_id": "r28-p3",
            "repo": "owner/repo", "pr_number": 4,
            "authorized_head": authorized_head,
            "candidate_sha256": "",
            "verifier_record_sha256": "",
            "base_branch": "main",
            "feature_branch": "feat/r28",
            "merge_method": "squash",
            "delete_branch": False,
            "require_match_head_commit": True,
            "authorization_timestamp": "2026-08-09T00:00:00Z",
            "author": "test",
            "next_wave_authorization": None,
            "notes": "",
            "required_ci_jobs": [],
        }
        write_artifact(tmp_path / "authorization.json", auth_payload)
        # Fill in digests.
        from autocoder_orchestration.artifacts import read_artifact
        cand_digest = read_artifact(tmp_path / "candidate.json").digest
        verifier_payload = json.loads((tmp_path / "verifier.json").read_text())
        verifier_payload["candidate_sha256"] = cand_digest
        write_artifact(tmp_path / "verifier.json", verifier_payload)
        ver_digest = read_artifact(tmp_path / "verifier.json").digest
        auth_payload["candidate_sha256"] = cand_digest
        auth_payload["verifier_record_sha256"] = ver_digest
        write_artifact(tmp_path / "authorization.json", auth_payload)

        with pytest.raises(MergeGateChanged) as exc:
            execute_guarded_merge_transaction(inputs)
        msg = str(exc.value)
        assert "a" * 40 in msg and "b" * 40 in msg, (
            f"MergeGateChanged MUST reference both the live "
            f"review commit OID and the authorized head; got {msg!r}"
        )
        assert "stale" in msg.lower() or "older" in msg.lower() or "does NOT match" in msg, (
            f"MergeGateChanged MUST describe the staleness; got {msg!r}"
        )


# ===== Test: exact-head bound review (head matches) PASSES =====

def test_review_commit_oid_matching_authorized_head_passes(
    tmp_path: Path,
) -> None:
    """The happy path: the live review's commit.oid matches
    the authorized exact head. The gate MUST NOT raise
    ``MergeGateChanged`` for review binding.
    """
    from autocoder_orchestration.merge_authorization import (
        MergeTransactionInputs,
        execute_guarded_merge_transaction,
    )
    authorized_head = "a" * 40
    inputs = MergeTransactionInputs(
        authorization_artifact_path=tmp_path / "authorization.json",
        candidate_artifact_path=tmp_path / "candidate.json",
        verifier_artifact_path=tmp_path / "verifier.json",
        merge_record_artifact_path=tmp_path / "merge-record.json",
        repository_checkout=tmp_path,
        run_state_root=tmp_path / "state",
        evidence_root=tmp_path / "evidence",
        live_pr_payload={
            "state": "open", "merged": False,
            "head": {"sha": authorized_head},
            "baseRefName": "main", "mergeable": "MERGEABLE",
            "autoMergeRequest": None, "isDraft": False,
            "reviewDecision": "APPROVED",
            "repo": "owner/repo",
        },
        live_ci_state={"all_required_passing": True, "coderabbit_passing": True},
        live_review_state={"latest_coderabbit_state": "APPROVED"},
        live_thread_inventory={"unresolved_current": 0, "unresolved_outdated": 0},
        working_tree_clean=True,
        required_ci_names=(),
        _bypass_oid_reachability=True,
    )
    from autocoder_orchestration.merge_authorization import (
        _build_default_live_fetchers,
    )
    fetchers = _build_default_live_fetchers(inputs, review_commit_oid=authorized_head)
    inputs._set_live_fetchers(fetchers)
    # Build a canonical auth/candidate/verifier artifact set.
    from autocoder_orchestration.artifacts import write_artifact, read_artifact
    candidate = {"exact_head": authorized_head, "files": []}
    write_artifact(tmp_path / "candidate.json", candidate)
    cand_digest = read_artifact(tmp_path / "candidate.json").digest
    verifier = {"verdict": "VERIFIED", "defects": [], "candidate_sha256": cand_digest}
    write_artifact(tmp_path / "verifier.json", verifier)
    ver_digest = read_artifact(tmp_path / "verifier.json").digest
    auth_payload = {
        "schema_version": "autocoder.merge_authorization.v1",
        "run_id": "r28-p3", "repo": "owner/repo", "pr_number": 4,
        "authorized_head": authorized_head,
        "candidate_sha256": cand_digest,
        "verifier_record_sha256": ver_digest,
        "base_branch": "main", "feature_branch": "feat/r28",
        "merge_method": "squash", "delete_branch": False,
        "require_match_head_commit": True,
        "authorization_timestamp": "2026-08-09T00:00:00Z",
        "author": "test", "next_wave_authorization": None,
        "notes": "", "required_ci_jobs": [],
    }
    write_artifact(tmp_path / "authorization.json", auth_payload)

    # The transaction proceeds past the review gate. It
    # will fail at the merge subprocess (no real ``gh``
    # available) but the exception MUST NOT be
    # ``MergeGateChanged``.
    from autocoder_orchestration.merge_authorization import (
        MergeGateFetchError, MergeSubprocessFailed, MergeAmbiguousOutcome,
    )
    try:
        execute_guarded_merge_transaction(inputs)
    except (MergeSubprocessFailed, MergeAmbiguousOutcome,
            MergeGateFetchError, Exception) as exc:
        # The exception MUST NOT be MergeGateChanged — the
        # review gate passed; only downstream gates can fail.
        assert not isinstance(exc, MergeGateChanged), (
            f"review gate MUST pass when commit OID matches; "
            f"got {exc!r}"
        )


# ===== Test: review by lookalike account refused even with APPROVED state =====

def test_lookalike_account_refused_even_when_state_is_approved(
    tmp_path: Path,
) -> None:
    """The user's specific scenario: an APPROVED review was
    submitted by a lookalike account such as
    ``coderabbit-helper``. The canonical CodeRabbit identity
    is ``coderabbitai``. The gate MUST refuse.
    """
    from autocoder_orchestration.merge_authorization import (
        MergeTransactionInputs,
        execute_guarded_merge_transaction,
    )
    authorized_head = "a" * 40
    inputs = MergeTransactionInputs(
        authorization_artifact_path=tmp_path / "authorization.json",
        candidate_artifact_path=tmp_path / "candidate.json",
        verifier_artifact_path=tmp_path / "verifier.json",
        merge_record_artifact_path=tmp_path / "merge-record.json",
        repository_checkout=tmp_path,
        run_state_root=tmp_path / "state",
        evidence_root=tmp_path / "evidence",
        live_pr_payload={
            "state": "open", "merged": False,
            "head": {"sha": authorized_head},
            "baseRefName": "main", "mergeable": "MERGEABLE",
            "autoMergeRequest": None, "isDraft": False,
            "reviewDecision": "APPROVED",
            "repo": "owner/repo",
        },
        live_ci_state={"all_required_passing": True, "coderabbit_passing": True},
        live_review_state={"latest_coderabbit_state": "APPROVED"},
        live_thread_inventory={"unresolved_current": 0, "unresolved_outdated": 0},
        working_tree_clean=True,
        required_ci_names=(),
        _bypass_oid_reachability=True,
    )

    def lookalike_fetcher() -> Dict[str, Any]:
        # The review IS APPROVED but is attributed to a
        # lookalike account. ``fetch_live_review_state`` would
        # return ``latest_coderabbit_state=None`` because the
        # canonical-reviewer match fails. We simulate that
        # here directly.
        return {
            "head_sha": "",
            "reviews": [{
                "state": "APPROVED",
                "author": "coderabbit-helper[bot]",
                "submitted_at": "2026-01-01T00:00:00Z",
                "commit_oid": authorized_head,
            }],
            "latest_coderabbit_state": None,
            "latest_coderabbit_commit_oid": None,
            "latest_coderabbit_login": "coderabbit-helper[bot]",
            "canonical_reviewer_login": "coderabbitai",
            "repo": "owner/repo",
        }
    inputs._set_live_fetchers({
        "pr_payload": lambda: {
            "state": "open", "merged": False, "isDraft": False,
            "head": {"sha": authorized_head},
            "baseRefName": "main", "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "autoMergeRequest": None,
            "reviewDecision": "APPROVED",
            "repo": "owner/repo",
        },
        "required_ci": lambda: {
            "head_sha": "", "checks": {},
        },
        "review_state": lookalike_fetcher,
        "thread_inventory": lambda: {
            "head_sha": "", "unresolved_current": 0,
            "unresolved_outdated": 0, "paginated_completely": True,
            "error": None,
        },
    })

    # Build the auth artifact.
    from autocoder_orchestration.artifacts import write_artifact, read_artifact
    candidate = {"exact_head": authorized_head, "files": []}
    write_artifact(tmp_path / "candidate.json", candidate)
    cand_digest = read_artifact(tmp_path / "candidate.json").digest
    verifier = {"verdict": "VERIFIED", "defects": [], "candidate_sha256": cand_digest}
    write_artifact(tmp_path / "verifier.json", verifier)
    ver_digest = read_artifact(tmp_path / "verifier.json").digest
    auth_payload = {
        "schema_version": "autocoder.merge_authorization.v1",
        "run_id": "r28-p3", "repo": "owner/repo", "pr_number": 4,
        "authorized_head": authorized_head,
        "candidate_sha256": cand_digest,
        "verifier_record_sha256": ver_digest,
        "base_branch": "main", "feature_branch": "feat/r28",
        "merge_method": "squash", "delete_branch": False,
        "require_match_head_commit": True,
        "authorization_timestamp": "2026-08-09T00:00:00Z",
        "author": "test", "next_wave_authorization": None,
        "notes": "", "required_ci_jobs": [],
    }
    write_artifact(tmp_path / "authorization.json", auth_payload)

    with pytest.raises(MergeGateChanged) as exc:
        execute_guarded_merge_transaction(inputs)
    msg = str(exc.value)
    assert "coderabbit-helper" in msg or "lookalike" in msg.lower() or "canonical" in msg.lower(), (
        f"MergeGateChanged MUST reference the lookalike account "
        f"or canonical-reviewer mismatch; got {msg!r}"
    )
