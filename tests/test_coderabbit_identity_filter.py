"""Regression tests for the CodeRabbit identity filter in cli.cmd_merge.

GitHub App bot logins may be returned by GraphQL either as
``coderabbitai`` or as ``coderabbitai[bot]`` depending on the
installation. The CLI must:

* accept either form as a real CodeRabbit review;
* reject any human reviewer (no false positives);
* reject any unrelated bot (no false positives);
* leave the gate unavailable when no matching review exists
  (fail-closed C-25).
"""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from autocoder_orchestration import cli
from autocoder_orchestration.cli import (
    CODERABBIT_AUTHOR_LOGIN,
    _normalize_coderabbit_login,
)


def _reviews_payload(login_to_state: list[tuple[str, str]]) -> dict:
    """Build a GraphQL ``latestReviews`` payload."""
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "latestReviews": {
                        "nodes": [
                            {"author": {"login": l}, "state": s}
                            for l, s in login_to_state
                        ],
                    },
                },
            },
        },
    }


def _build_inputs_payload(tmpdir: Path):
    """Minimal MergeTransactionInputs-shaped dict for cmd_merge."""
    return {
        "authorization_artifact_path": tmpdir / "authorization.json",
        "candidate_artifact_path": tmpdir / "candidate.json",
        "verifier_artifact_path": tmpdir / "verifier.json",
        "merge_record_artifact_path": tmpdir / "merge-record.json",
        "repository_checkout": tmpdir / "repo",
        "run_state_root": tmpdir / "state",
        "evidence_root": tmpdir / "evidence",
        "live_pr_payload": {
            "state": "open",
            "merged": False,
            "head": {"sha": "2a8e4e9c1f3a4b5d6e7f8091a2b3c4d5e40ffe0d"},
            "baseRefName": "main",
            "mergeable": "MERGEABLE",
            "autoMergeRequest": None,
            "isDraft": False,
            "mergeStateStatus": "CLEAN",
            "repo": "Slideshow11/AutoDev",
        },
        "live_ci_state": {
            "all_required_passing": True,
            "coderabbit_passing": True,
        },
        "live_review_state": {
            "latest_coderabbit_state": "APPROVED",
        },
        "live_thread_inventory": {
            "unresolved_current": 0,
            "unresolved_outdated": 0,
        },
        "working_tree_clean": True,
    }


class NormalizeCoderabbitLoginTests(unittest.TestCase):
    """The normalized-identity contract is the only comparison key."""

    def test_strips_bot_suffix(self) -> None:
        self.assertEqual(
            _normalize_coderabbit_login("coderabbitai[bot]"),
            "coderabbitai",
        )

    def test_handles_bare_login(self) -> None:
        self.assertEqual(
            _normalize_coderabbit_login("coderabbitai"),
            "coderabbitai",
        )

    def test_lowercases(self) -> None:
        self.assertEqual(
            _normalize_coderabbit_login("CoderabbitAI"),
            "coderabbitai",
        )

    def test_handles_human_account(self) -> None:
        self.assertNotEqual(
            _normalize_coderabbit_login("slideshow11"),
            _normalize_coderabbit_login(CODERABBIT_AUTHOR_LOGIN),
        )

    def test_handles_unrelated_bot(self) -> None:
        # A bot with a similar name but different prefix must not match.
        self.assertNotEqual(
            _normalize_coderabbit_login("not-coderabbitai[bot]"),
            _normalize_coderabbit_login(CODERABBIT_AUTHOR_LOGIN),
        )

    def test_handles_missing_login(self) -> None:
        self.assertEqual(_normalize_coderabbit_login(""), "")  # type: ignore[arg-type]

    def test_no_substring_matching(self) -> None:
        # Substring would over-match. The normalization must not
        # collapse to a partial match.
        self.assertNotEqual(
            _normalize_coderabbit_login("coderabbitai-fork[bot]"),
            _normalize_coderabbit_login(CODERABBIT_AUTHOR_LOGIN),
        )


class CoderabbitReviewFilterTests(unittest.TestCase):
    """The CLI's filter must accept the right identity and reject others."""

    def _run_review_filter(self, payload: dict) -> str | None:
            """Invoke the review-filter block via subprocess and return the
            resolved ``latest_coderabbit_state`` value (or ``None``).

            Uses ``python -c`` with an absolute path-to-module via
            ``importlib.util`` so the subprocess does not need a
            pre-existing cwd.
            """
            import importlib.util
            import json as _json
            import subprocess as _sp
            cli_module = importlib.import_module("autocoder_orchestration.cli")
            result = _json.dumps(cli_module._filter_coderabbit_review_state(payload))
            # Avoid running a subprocess at all: directly invoke the
            # function and return its result.
            return _json.loads(result) if result != "null" else None

    def test_coderabbitai_bare_login_recognized(self) -> None:
        """The GraphQL login ``coderabbitai`` satisfies the gate."""
        payload = _reviews_payload(
            [("coderabbitai", "APPROVED")],
        )
        result = self._run_review_filter(payload)
        self.assertEqual(result, "APPROVED")

    def test_coderabbitai_bot_login_recognized(self) -> None:
        """The GitHub App form ``coderabbitai[bot]`` also satisfies."""
        payload = _reviews_payload(
            [("coderabbitai[bot]", "APPROVED")],
        )
        result = self._run_review_filter(payload)
        self.assertEqual(result, "APPROVED")

    def test_human_approved_does_not_satisfy(self) -> None:
        """A human APPROVED review must NOT satisfy the CodeRabbit gate."""
        payload = _reviews_payload(
            [("Slideshow11", "APPROVED"), ("coderabbitai", "COMMENTED")],
        )
        result = self._run_review_filter(payload)
        # The CodeRabbit review is COMMENTED, not APPROVED, so the
        # gate state is COMMENTED (not human's APPROVED).
        self.assertEqual(result, "COMMENTED")

    def test_unrelated_bot_approved_does_not_satisfy(self) -> None:
        """An unrelated bot's APPROVED review must NOT satisfy the gate."""
        payload = _reviews_payload(
            [("dependabot[bot]", "APPROVED"), ("coderabbitai", "APPROVED")],
        )
        result = self._run_review_filter(payload)
        # The CodeRabbit review IS present and APPROVED, so the gate
        # sees the correct state. The dependabot entry is ignored
        # (not over-matching it to CodeRabbit).
        self.assertEqual(result, "APPROVED")

    def test_only_unrelated_bot_fails_closed(self) -> None:
        """If only an unrelated bot reviewed, the gate is unavailable."""
        payload = _reviews_payload(
            [("dependabot[bot]", "APPROVED")],
        )
        result = self._run_review_filter(payload)
        self.assertIsNone(result)

    def test_no_reviews_fails_closed(self) -> None:
        """An empty review list leaves the state unavailable."""
        payload = _reviews_payload([])
        result = self._run_review_filter(payload)
        self.assertIsNone(result)


class PagedReviewWalkTests(unittest.TestCase):
    """The paginated ``_fetch_coderabbit_review_state`` helper iterates
    every page and merges matching CodeRabbit reviews across pages."""

    def test_pagination_finds_match_on_second_page(self) -> None:
        """A CodeRabbit review on page 2 is still recognized."""
        # Use a fake runner that returns page 1 empty and page 2 with
        # a CodeRabbit review. The CLI's _fetch_coderabbit_review_state
        # walks every page.
        from autocoder_orchestration import cli as cli_module

        page1 = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "latestReviews": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "C1"},
                            "nodes": [
                                {"author": {"login": "alice"}, "state": "COMMENTED"},
                            ],
                        }
                    }
                }
            }
        }
        page2 = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "latestReviews": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {"author": {"login": "coderabbitai[bot]"},
                                 "state": "APPROVED"},
                            ],
                        }
                    }
                }
            }
        }

        calls = []

        def fake_runner(args, **kwargs):
            calls.append(args)
            # First call: page 1 (cursor null)
            # Subsequent: cursor set
            # Return page 1 the first time, page 2 the second time
            if len(calls) == 1:
                r = subprocess.CompletedProcess(
                    args=[], returncode=0,
                    stdout=json.dumps(page1),
                    stderr="",
                )
            else:
                r = subprocess.CompletedProcess(
                    args=[], returncode=0,
                    stdout=json.dumps(page2),
                    stderr="",
                )
            return r

        import unittest.mock as mockmod
        with mockmod.patch.object(cli_module.subprocess, "run",
                                  side_effect=fake_runner):
            state = cli_module._fetch_coderabbit_review_state(
                "gh", owner="o", name="r", pr_number=4,
            )
        self.assertEqual(state, "APPROVED")
        # Two gh invocations: one per page.
        self.assertEqual(len(calls), 2)

    def test_pagination_no_match_returns_none(self) -> None:
        """Walking every page without finding a CodeRabbit review → None."""
        from autocoder_orchestration import cli as cli_module

        page = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "latestReviews": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {"author": {"login": "alice"},
                                 "state": "APPROVED"},
                            ],
                        }
                    }
                }
            }
        }
        calls = []

        def fake_runner(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(page), stderr="",
            )

        import unittest.mock as mockmod
        with mockmod.patch.object(cli_module.subprocess, "run",
                                  side_effect=fake_runner):
            state = cli_module._fetch_coderabbit_review_state(
                "gh", owner="o", name="r", pr_number=4,
            )
        self.assertIsNone(state)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()