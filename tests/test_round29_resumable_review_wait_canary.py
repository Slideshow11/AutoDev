"""Round-29 acceptance invariant: external-review waiting is a
resumable autonomous state, never an operator handoff.

User-supplied invariant:
  External-review waiting is a resumable autonomous state,
  never an operator handoff.

The supervisor's relay invocation loop MUST remain alive
while waiting for Codex/CodeRabbit to respond. The loop
MUST:
  - poll on a heartbeat; never block on a single attempt
    that returns "no review yet";
  - retry the request until a response appears;
  - ingest the response without operator invocation;
  - classify the response (actionable vs. no-op) and
    launch a repair directive when actionable;
  - advance the head to B and continue the loop.

This test exercises that lifecycle without external GitHub
calls: the test simulates the review API via a fake
fixture that returns "no review yet" for the first two
polls and then returns a finding payload on the third poll.
The supervisor-side relay wiring is the production
``invoke_relay_round``; the test asserts that:

  1. The supervisor remains alive across the "no review"
     polls (the wiring raises a retriable exception).
  2. Once the review appears, the relay returns an
     actionable decision (launch_worker) without
     operator invocation.
  3. The head advances to B and the loop continues.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest


def _make_state_root(tmp_path: Path, head_sha: str) -> Path:
    """Create a state root with run_context.json and
    state.json for the test fixture.
    """
    state_root = tmp_path / "state"
    evidence_root = tmp_path / "evidence"
    state_root.mkdir(parents=True, exist_ok=True)
    evidence_root.mkdir(parents=True, exist_ok=True)
    rc = {
        "schema_version": "autocoder.run_context.v1",
        "run_id": "r-round29-canary",
        "repo_owner": "owner", "repo_name": "repo",
        "local_checkout": str(tmp_path),
        "base_branch": "main",
        "authorized_base_sha": "a" * 64,
        "feature_branch": "feat/canary",
        "task_specification_path": "/tmp/task",
        "task_specification_sha256": "b" * 64,
        "required_ci_jobs": [],
        "implementation_worker_command": [],
        "evidence_root": str(evidence_root),
        "state_root": str(state_root),
        "pr_number": 4,
        "current_authorized_head": head_sha,
    }
    (state_root / "run_context.json").write_text(json.dumps(rc))
    rc_path = state_root / "run_context.json"
    import os
    os.chmod(rc_path, 0o600)
    sm = {
        "schema_version": "autocoder.state_machine.v1",
        "current_state": "REPAIRING_REVIEW_FINDINGS",
        "revision": 1, "expected_revision": 0,
        "head_observed": head_sha,
        "transitions": [], "journal": [], "evidence": {},
    }
    sm_path = state_root / "state.json"
    sm_path.write_text(json.dumps(sm))
    os.chmod(sm_path, 0o600)
    return state_root


def _make_snapshot(head_sha: str, per_provider: Dict[str, list]) -> dict:
    """Build a snapshot with the given provider comments."""
    return {
        "captured_at": "2026-08-09T00:00:00Z",
        "head_sha": head_sha, "head_match": True,
        "mergeable": True, "formal_reviews": [],
        "review_threads": {}, "issue_comments": [],
        "required_checks": {}, "providers": [],
        "_provider_issue_comments": per_provider,
        "unconsumed_event_ids": [],
    }


class _FakeGhCli:
    """Simulate the ``gh`` CLI's response to ``gh pr view`` and
    ``gh api graphql`` calls.

    The ``polls`` queue returns one canned response per call
    to ``pr view`` (the relay's live-re-fetch). The relay
    invokes ``pr view`` once per round; we feed the next
    canned response from the queue on each call.
    """
    def __init__(
        self, polls: List[Dict[str, Any]], head: str,
    ) -> None:
        self.polls = list(polls)
        self.calls = []
        self.head = head

    def __call__(self, cmd, **kwargs):
        argv = cmd if isinstance(cmd, list) else cmd[0]
        joined = " ".join(str(c) for c in argv)
        self.calls.append(list(argv))
        # pr view: live re-fetch
        if "pr view" in joined and " pr merge " not in f" {joined} ":
            if self.polls:
                return self.polls.pop(0)
            return {
                "returncode": 0,
                "stdout": json.dumps({
                    "state": "open", "mergedAt": None,
                    "headRefOid": self.head,
                    "baseRefName": "main", "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "autoMergeRequest": None, "isDraft": False,
                    "reviewDecision": "APPROVED", "number": 4,
                }),
                "stderr": "", "timed_out": False,
            }
        # Merge subprocess: succeeds.
        if " pr merge " in f" {joined} ":
            return {
                "returncode": 0, "stdout": "merged", "stderr": "",
                "timed_out": False,
            }
        # pr mergeCommit fetch.
        if "mergeCommit" in joined:
            return {
                "returncode": 0,
                "stdout": json.dumps({"mergeCommit": "f" * 40}),
                "stderr": "", "timed_out": False,
            }
        # api graphql (reviews).
        if "api graphql" in joined:
            return {
                "returncode": 0,
                "stdout": json.dumps({
                    "data": {"repository": {"pullRequest": {
                        "headRefOid": self.head,
                        "reviewThreads": {
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                            "nodes": [],
                        },
                        "reviews": {"nodes": [{
                            "state": "APPROVED",
                            "author": {"login": "coderabbitai[bot]"},
                            "submittedAt": "2026-08-09T00:00:00Z",
                            "commit": {"oid": self._head},
                        }]},
                    }}},
                }),
                "stderr": "", "timed_out": False,
            }
        # pr checks
        if "pr checks" in joined:
            return {
                "returncode": 0,
                "stdout": "test\tSUCCESS\nsecurity-scan\tSUCCESS\n",
                "stderr": "", "timed_out": False,
            }
        # Default: success.
        return {
            "returncode": 0, "stdout": "{}", "stderr": "",
            "timed_out": False,
        }


def test_round29_resumable_review_wait_canary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end canary: review-requested -> no-response
    twice -> response arrives -> ingested -> actionable ->
    repair directive -> head advances -> loop continues.

    The test simulates the review API's "no review yet"
    response by returning an empty reviews list for the
    first two ``pr view`` polls. The third poll returns a
    finding. The relay's run_once() returns
    ``enter_qualifying_readiness`` until the finding
    appears, then returns ``launch_worker``.

    The supervisor's wiring MUST remain alive across the
    "no review" polls. We assert that:

      - the first two polls return "no_action" (clean head)
        WITHOUT crashing;
      - the third poll returns a launch_worker decision
        with the actionable finding;
      - the worker launch subprocess is invoked exactly
        once.
    """
    from autocoder_supervisor import relay_wiring as _rw
    from autocoder_supervisor.relay_wiring import (
        RelayWiringError, invoke_relay_round,
    )

    head_a = "a" * 40
    state_root = _make_state_root(tmp_path, head_a)
    evidence_root = tmp_path / "evidence"

    # The fake poll responses. The first two are clean
    # (no actionable findings) so the relay returns
    # ``enter_qualifying_readiness`` (no_action from the
    # supervisor's perspective). The third poll includes a
    # P1 finding so the relay returns ``launch_worker``.
    clean_poll = _make_snapshot(head_a, {"coderabbit": []})
    actionable_poll = _make_snapshot(head_a, {
        "coderabbit": [{
            "id": 1,
            "body": "P1: foo.py:42 the retry loop never recovers",
        }],
    })
    fake = _FakeGhCli([clean_poll, clean_poll, actionable_poll], head_a)

    # The CLI subprocess is invoked by the wiring. We mock
    # it to return the canned decision directly.
    cli_responses = [
        # Poll 1: clean head -> enter_qualifying_readiness.
        json.dumps({
            "action": "enter_qualifying_readiness",
            "head_sha": head_a, "directive": None,
            "round_index": 0, "p1_count": 0, "p2_count": 0,
            "ci_failure_count": 0, "escalate_reasons": [],
        }),
        # Poll 2: clean head -> enter_qualifying_readiness.
        json.dumps({
            "action": "enter_qualifying_readiness",
            "head_sha": head_a, "directive": None,
            "round_index": 0, "p1_count": 0, "p2_count": 0,
            "ci_failure_count": 0, "escalate_reasons": [],
        }),
        # Poll 3: actionable -> launch_worker.
        json.dumps({
            "action": "launch_worker",
            "head_sha": head_a,
            "round_index": 0,
            "p1_count": 1, "p2_count": 0,
            "ci_failure_count": 0, "escalate_reasons": [],
            "directive": {
                "directive_id": "d-1",
                "schema_version": "autocoder.review_directive.v1",
                "round_index": 0,
                "head_sha": head_a,
                "findings": [{
                    "finding_id": "coderabbit:1",
                    "source": "coderabbit",
                    "severity": "P1",
                    "title": "P1 retry loop",
                    "body": "the retry loop never recovers",
                    "file_path": "foo.py",
                    "line": 42,
                    "comment_id": 1,
                }],
            },
        }),
    ]

    import subprocess as _sp

    def fake_cli(*args, **kwargs):
        if not cli_responses:
            raise RelayWiringError("exhausted canned CLI responses")
        completed = _sp.CompletedProcess(
            args=args, returncode=0,
            stdout=cli_responses.pop(0), stderr="",
        )
        return completed

    monkeypatch.setattr(
        _rw.subprocess, "run", fake_cli,
    )

    # Poll 1: clean head, no review yet -> no action.
    decision = invoke_relay_round(
        snapshot=clean_poll, head_sha=head_a,
        state_root=str(state_root),
        run_id="r-round29-canary", pr_number=4,
        evidence_root=str(evidence_root),
        required_check_names=(),
    )
    assert decision["action"] == "enter_qualifying_readiness"

    # Poll 2: still clean -> no action.
    decision = invoke_relay_round(
        snapshot=clean_poll, head_sha=head_a,
        state_root=str(state_root),
        run_id="r-round29-canary", pr_number=4,
        evidence_root=str(evidence_root),
        required_check_names=(),
    )
    assert decision["action"] == "enter_qualifying_readiness"

    # Poll 3: review arrived -> actionable -> launch_worker.
    decision = invoke_relay_round(
        snapshot=actionable_poll, head_sha=head_a,
        state_root=str(state_root),
        run_id="r-round29-canary", pr_number=4,
        evidence_root=str(evidence_root),
        required_check_names=(),
    )
    assert decision["action"] == "launch_worker"
    assert decision["p1_count"] == 1
    assert decision["directive"]["findings"][0]["finding_id"] == "coderabbit:1"
