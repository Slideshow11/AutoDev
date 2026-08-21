from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from autocoder_supervisor import supervisor as sup


HEAD = "fc3661fbc79618c8d734561cbbf2dae87938bf7b"
OTHER_HEAD = "a" * 40


def _comment(provider: str, head: str, request_id: str, cid: int = 1) -> dict:
    return {
        "id": cid,
        "created_at": f"2026-08-19T20:00:{cid:02d}Z",
        "html_url": f"https://github.test/issues/comments/{cid}",
        "body": (
            f"@{provider} review\n\n"
            f"<!-- autodev-review-request:v1:{provider}:{head}:{request_id} -->"
        ),
    }


@pytest.fixture
def request_env(tmp_path: Path, monkeypatch):
    remote_comments: list[dict] = []
    mutation_bodies: list[str] = []
    monkeypatch.setattr(sup, "REVIEW_REQUESTS_DIR", tmp_path / "requests")
    monkeypatch.setattr(sup, "REPO_OWNER", "Slideshow11")
    monkeypatch.setattr(sup, "REPO_NAME", "AutoDev")
    monkeypatch.setattr(sup, "PR_NUMBER", 9)
    monkeypatch.setattr(sup, "PROVIDERS", {
        "codex": {"trigger_handle": "@codex review"},
        "coderabbit": {"trigger_handle": "@coderabbitai review"},
    })
    monkeypatch.setattr(sup, "fetch_live_pr_head_now", lambda: HEAD)
    monkeypatch.setattr(sup, "get_github_token", lambda: "configured")

    def github_get(path: str, token: str, **kwargs):
        if "/comments" in path:
            page = int(path.rsplit("page=", 1)[-1])
            return list(remote_comments) if page == 1 else []
        raise AssertionError(path)

    def run(cmd, **kwargs):
        body = cmd[cmd.index("--body") + 1]
        mutation_bodies.append(body)
        remote_comments.append({
            "id": len(remote_comments) + 100,
            "created_at": "2026-08-19T20:01:00Z",
            "html_url": "https://github.test/issues/comments/100",
            "body": body,
        })
        return SimpleNamespace(
            returncode=0,
            stdout="https://github.test/issues/comments/100\n",
            stderr="",
        )

    monkeypatch.setattr(sup, "github_get", github_get)
    monkeypatch.setattr(sup.subprocess, "run", run)
    return remote_comments, mutation_bodies


def test_full_head_marker_parser_and_malformed_rejection() -> None:
    marker = f"<!-- autodev-review-request:v1:codex:{HEAD}:req-abc123 -->"
    assert sup._parse_review_request_marker(marker) == (
        "codex", HEAD, "req-abc123"
    )
    assert sup._parse_review_request_marker(
        marker.replace(HEAD, HEAD[:12])
    ) is None
    assert sup._parse_review_request_marker("@codex review") is None


def test_1_no_local_no_marker_posts_once(request_env) -> None:
    _, mutations = request_env
    assert sup.post_review_request("codex", HEAD) is True
    assert len(mutations) == 1
    assert HEAD in mutations[0]


def test_2_local_request_sent_prevents_duplicate(request_env) -> None:
    _, mutations = request_env
    sup.write_review_request("codex", HEAD, {"lifecycle": "REQUEST_SENT"})
    assert sup.post_review_request("codex", HEAD) is False
    assert not mutations


def test_3_remote_marker_reconstructs_local_ledger(request_env) -> None:
    remote, mutations = request_env
    remote.append(_comment("codex", HEAD, "req-existing", 1))
    assert sup.post_review_request("codex", HEAD) is False
    assert not mutations
    adopted = sup.read_review_request("codex", HEAD)
    assert adopted["lifecycle"] == "REQUEST_SENT"
    assert adopted["request_id"] == "req-existing"


@pytest.mark.parametrize(
    "remote_comment",
    [
        _comment("codex", OTHER_HEAD, "req-other-head", 1),
        _comment("coderabbit", HEAD, "req-other-provider", 1),
        {"id": 1, "created_at": "x", "body": "<!-- malformed -->"},
    ],
)
def test_4_5_6_nonmatching_markers_allow_current_request(
    request_env, remote_comment: dict,
) -> None:
    remote, mutations = request_env
    remote.append(remote_comment)
    assert sup.post_review_request("codex", HEAD) is True
    assert len(mutations) == 1


def test_duplicate_historical_markers_are_adopted_deterministically(
    request_env,
) -> None:
    remote, mutations = request_env
    remote.extend([
        _comment("codex", HEAD, "req-first", 1),
        _comment("codex", HEAD, "req-second", 2),
    ])
    assert sup.post_review_request("codex", HEAD) is False
    assert not mutations
    adopted = sup.read_review_request("codex", HEAD)
    assert adopted["request_id"] == "req-first"
    assert adopted["historical_duplicate_count"] == 1
    assert adopted["historical_duplicate_request_ids"] == ["req-second"]


def test_7_concurrent_supervisors_emit_at_most_one_mutation(request_env) -> None:
    _, mutations = request_env
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: sup.post_review_request("codex", HEAD), range(2)))
    assert len(mutations) == 1


def test_8_restart_after_lost_local_state_does_not_repost(
    request_env, monkeypatch,
) -> None:
    remote, mutations = request_env
    assert sup.post_review_request("codex", HEAD) is True
    first_root = sup.REVIEW_REQUESTS_DIR
    monkeypatch.setattr(sup, "REVIEW_REQUESTS_DIR", first_root.parent / "lost")
    assert remote
    assert sup.post_review_request("codex", HEAD) is False
    assert len(mutations) == 1


def test_9_failed_intent_is_retryable_when_marker_absent(
    request_env, monkeypatch,
) -> None:
    _, mutations = request_env
    calls = 0
    real_run = sup.subprocess.run

    def flaky(cmd, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="network")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(sup.subprocess, "run", flaky)
    assert sup.post_review_request("codex", HEAD) is False
    assert sup.read_review_request("codex", HEAD)["lifecycle"] == "REQUEST_INTENT"
    assert sup.post_review_request("codex", HEAD) is True
    assert len(mutations) == 1
    histories = list(sup.REVIEW_REQUESTS_DIR.glob("*.intent.json"))
    assert len(histories) == 1


def test_10_remote_success_local_write_crash_is_reconciled(
    request_env, monkeypatch,
) -> None:
    remote, mutations = request_env
    real_write = sup.write_review_request

    def crash_after_send(provider: str, head_sha: str, record: dict) -> None:
        if record.get("lifecycle") == "REQUEST_SENT":
            raise OSError("simulated local crash")
        real_write(provider, head_sha, record)

    monkeypatch.setattr(sup, "write_review_request", crash_after_send)
    assert sup.post_review_request("codex", HEAD) is True
    assert remote and len(mutations) == 1
    monkeypatch.setattr(sup, "write_review_request", real_write)
    assert sup.post_review_request("codex", HEAD) is False
    assert len(mutations) == 1
    assert sup.read_review_request("codex", HEAD)["lifecycle"] == "REQUEST_SENT"


def test_marker_inventory_failure_fails_closed(request_env, monkeypatch) -> None:
    _, mutations = request_env
    monkeypatch.setattr(sup, "github_get", lambda *args, **kwargs: None)
    assert sup.post_review_request("codex", HEAD) is False
    assert not mutations
