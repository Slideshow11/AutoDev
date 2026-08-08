"""Tests for the supervisor <-> relay directive bridge.

The bridge is the THIN layer that lets the supervisor use a
relay-built prompt when a directive is present. Tests cover:

1. resolve_worker_prompt returns None when no directive is
   configured.
2. resolve_worker_prompt renders the canonical worker prompt
   when a directive is on disk.
3. The bridge prompt is byte-identical to the relay's
   build_worker_prompt output for the same directive.
4. The bridge never raises on malformed or missing files.
5. The supervisor's launch_worker consults the bridge and
   uses the directive prompt.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Tests must run on both the canonical evidence root and a
# caller-supplied directive path.
from autocoder_supervisor.directive_bridge import (
    resolve_worker_prompt,
    _render_directive_prompt,
)


def _write_directive(evidence_root: Path, directive: dict) -> Path:
    evidence_root.mkdir(parents=True, exist_ok=True)
    target = evidence_root / "directive.json"
    target.write_text(json.dumps(directive, indent=2, sort_keys=True))
    return target


def _make_directive() -> dict:
    return {
        "schema_version": "autocoder.review_repair_relay.v1",
        "directive_id": "abc-123",
        "round_index": 2,
        "head_sha": "a" * 40,
        "repo": "owner/repo",
        "pr_number": 4,
        "created_at": "2026-08-08T00:00:00Z",
        "summary": "3 findings: P1=2, P2=0, CI_FAIL=1",
        "coordinator_actor": "controller",
        "findings": [
            {
                "finding_id": "coderabbit:1",
                "source": "coderabbit",
                "severity": "P1",
                "title": "broken",
                "body": "P1: foo.py:1 broken",
                "file_path": "foo.py",
                "line": 1,
                "url": None,
                "suggested_test": None,
                "review_id": None,
                "comment_id": 1,
                "check_name": None,
            },
        ],
    }


class TestResolveWorkerPrompt:
    def test_returns_none_when_no_directive(self, tmp_path: Path) -> None:
        # No AED_DIRECTIVE_PATH, no AED_EVIDENCE_ROOT, no file present.
        os.environ.pop("AED_DIRECTIVE_PATH", None)
        os.environ.pop("AED_EVIDENCE_ROOT", None)
        assert resolve_worker_prompt() is None

    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        os.environ["AED_DIRECTIVE_PATH"] = str(tmp_path / "missing.json")
        try:
            assert resolve_worker_prompt() is None
        finally:
            os.environ.pop("AED_DIRECTIVE_PATH", None)

    def test_returns_prompt_when_directive_present(self, tmp_path: Path) -> None:
        directive = _make_directive()
        path = _write_directive(tmp_path, directive)
        os.environ["AED_DIRECTIVE_PATH"] = str(path)
        try:
            prompt = resolve_worker_prompt()
            assert prompt is not None
            assert "abc-123" in prompt
            assert "a" * 40 in prompt
            assert "PR 4" in prompt
            assert "owner/repo" in prompt
        finally:
            os.environ.pop("AED_DIRECTIVE_PATH", None)

    def test_returns_none_on_malformed_json(self, tmp_path: Path) -> None:
        path = tmp_path / "directive.json"
        path.write_text("not json")
        os.environ["AED_DIRECTIVE_PATH"] = str(path)
        try:
            assert resolve_worker_prompt() is None
        finally:
            os.environ.pop("AED_DIRECTIVE_PATH", None)

    def test_returns_none_on_non_dict_payload(self, tmp_path: Path) -> None:
        path = tmp_path / "directive.json"
        path.write_text(json.dumps([1, 2, 3]))
        os.environ["AED_DIRECTIVE_PATH"] = str(path)
        try:
            assert resolve_worker_prompt() is None
        finally:
            os.environ.pop("AED_DIRECTIVE_PATH", None)

    def test_returns_none_on_wrong_schema_version(self, tmp_path: Path) -> None:
        d = _make_directive()
        d["schema_version"] = "wrong.version"
        path = _write_directive(tmp_path, d)
        os.environ["AED_DIRECTIVE_PATH"] = str(path)
        try:
            assert resolve_worker_prompt() is None
        finally:
            os.environ.pop("AED_DIRECTIVE_PATH", None)

    def test_returns_none_on_missing_required_field(self, tmp_path: Path) -> None:
        d = _make_directive()
        del d["head_sha"]
        path = _write_directive(tmp_path, d)
        os.environ["AED_DIRECTIVE_PATH"] = str(path)
        try:
            assert resolve_worker_prompt() is None
        finally:
            os.environ.pop("AED_DIRECTIVE_PATH", None)

    def test_returns_none_on_non_dict_directive(self, tmp_path: Path) -> None:
        path = tmp_path / "directive.json"
        path.write_text("\"a string\"")
        os.environ["AED_DIRECTIVE_PATH"] = str(path)
        try:
            assert resolve_worker_prompt() is None
        finally:
            os.environ.pop("AED_DIRECTIVE_PATH", None)


class TestBridgePromptByteIdenticalToRelay:
    """The bridge prompt must match the relay prompt for the
    same directive. The two implementations are paired via this
    test so any drift is caught immediately.
    """

    def test_byte_identical_with_relay_prompt(self, tmp_path: Path) -> None:
        from autocoder_orchestration.review_repair_relay import (
            ReviewDirective,
            Finding,
            build_directive,
            build_worker_prompt,
            RoundDecision,
        )
        import dataclasses
        # Build a fresh directive via the relay so the canonical
        # serialization matches the relay's own serializer.
        finding = Finding(
            finding_id="coderabbit:1",
            source="coderabbit",
            severity="P1",
            title="broken",
            body="P1: foo.py:1 broken",
            file_path="foo.py",
            line=1,
            url=None,
            suggested_test=None,
            review_id=None,
            comment_id=1,
            check_name=None,
        )
        directive = build_directive(
            round_index=2,
            head_sha="a" * 40,
            repo="owner/repo",
            pr_number=4,
            findings=[finding],
            coordinator_actor="controller",
        )
        # Build the relay prompt.
        decision = RoundDecision(
            action="launch_worker",
            round_index=2,
            head_sha=directive.head_sha,
            outcome="completed",
            p1_count=1,
            p2_count=0,
            ci_failure_count=0,
            escalate_reasons=(),
            directive=directive,
            directive_digest=None,
        )
        relay_prompt = build_worker_prompt(decision)
        # Persist the directive and build the bridge prompt.
        path = tmp_path / "directive.json"
        path.write_text(json.dumps(directive.to_dict(), indent=2, sort_keys=True))
        os.environ["AED_DIRECTIVE_PATH"] = str(path)
        try:
            bridge_prompt = resolve_worker_prompt()
        finally:
            os.environ.pop("AED_DIRECTIVE_PATH", None)
        assert bridge_prompt == relay_prompt


class TestBridgePromptFormat:
    def test_prompt_includes_round_and_pr(self, tmp_path: Path) -> None:
        path = _write_directive(tmp_path, _make_directive())
        os.environ["AED_DIRECTIVE_PATH"] = str(path)
        try:
            prompt = resolve_worker_prompt()
        finally:
            os.environ.pop("AED_DIRECTIVE_PATH", None)
        assert "round 2" in prompt
        assert "PR 4" in prompt
        assert "owner/repo" in prompt

    def test_prompt_includes_standing_authorization(self, tmp_path: Path) -> None:
        path = _write_directive(tmp_path, _make_directive())
        os.environ["AED_DIRECTIVE_PATH"] = str(path)
        try:
            prompt = resolve_worker_prompt()
        finally:
            os.environ.pop("AED_DIRECTIVE_PATH", None)
        assert "Standing authorization" in prompt
        assert "Do NOT amend history" in prompt
        assert "Do NOT force-push" in prompt
        assert "Do NOT merge" in prompt

    def test_prompt_includes_directive_json(self, tmp_path: Path) -> None:
        path = _write_directive(tmp_path, _make_directive())
        os.environ["AED_DIRECTIVE_PATH"] = str(path)
        try:
            prompt = resolve_worker_prompt()
        finally:
            os.environ.pop("AED_DIRECTIVE_PATH", None)
        # The directive JSON is embedded in a code block.
        assert "```json" in prompt
        assert "abc-123" in prompt
        assert "owner/repo" in prompt


class TestSupervisorConsultsBridge:
    """Verify that the supervisor's launch_worker consults the
    bridge and uses the directive prompt when one is present.
    """

    def test_launch_worker_uses_directive_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from autocoder_supervisor import supervisor as sup
        # Reset all module globals to a known-isolated state.
        import tempfile
        isolated = tempfile.mkdtemp(prefix="autocoder-test-")
        monkeypatch.setattr(
            sup, "WORKER_COMMAND_TEMPLATE",
            ["echo", "{prompt}", "{session_id}"],
        )
        monkeypatch.setattr(sup, "SESSION_ID", "test-session")
        monkeypatch.setattr(sup, "INSTANCE_ID", "test-instance")
        monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", "a" * 40)
        monkeypatch.setattr(sup, "PR_NUMBER", 4)
        monkeypatch.setattr(sup, "REPO_OWNER", "owner")
        monkeypatch.setattr(sup, "REPO_NAME", "repo")
        # Provide a directive at the canonical path.
        directive = _make_directive()
        evidence_root = tmp_path / "evidence"
        _write_directive(evidence_root, directive)
        monkeypatch.setenv("AED_EVIDENCE_ROOT", str(evidence_root))
        # Intercept Popen so we can capture the cmd without
        # actually launching a worker.
        captured_cmd: list = []
        class FakePopen:
            def __init__(self, cmd, **kwargs):
                captured_cmd.extend(cmd)
                self.pid = 99999
        monkeypatch.setattr(sup.subprocess, "Popen", FakePopen)
        # Stub out file system writes the supervisor does.
        monkeypatch.setattr(sup, "write_lease", lambda lease: None)
        monkeypatch.setattr(sup, "write_cooldown", lambda: None)
        monkeypatch.setattr(sup, "start_time_evidence", lambda pid: {"pid": pid})
        # Run.
        lease = sup.launch_worker(
            {"current_head": "a" * 40}, {"snapshot": {}},
        )
        assert lease is not None
        # The directive prompt contains the directive_id and
        # the directive JSON; the resume template does not.
        joined = " ".join(captured_cmd)
        assert "abc-123" in joined
        assert "owner/repo" in joined
        assert "REPAIR DIRECTIVE" in joined
