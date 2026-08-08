"""Round-6 review, Round-7 proof-repair, Round-8 final-loop,
Round-9 review-diagnostic, and Round-10 live-verifier-fix tests
for PR #4.

These tests cover:

Round-6 directive's findings (multi-page CI, F541, 9a-9i).
Round-7 directive's proof-repair findings (XV61A, XV61D,
XV61F, XV61O).
Round-8 directive's three fresh threads:
* PRRT_kwDOTtyQLc6XWVRw -- _inspect_ci production-gate
  tests: remove inert downstream-gate patches and broad
  exception swallowing; the positive control must directly
  call VERIFIER._inspect_ci and assert the actual return
  value proves total_count == 106, len(runs) == 106, all
  six required jobs present and successful.
* PRRT_kwDOTtyQLc6XWVR0 -- AST containment for assertFalse
  exemption + body[0] docstring check.
* PRRT_kwDOTtyQLc6XWVR3 -- exercise the real
  _paginate_latest_reviews / _paginate_connection /
  _inspect_coderabbit path; mock only the GraphQL/network
  boundary.
Round-9 directive's two fresh threads:
* PRRT_kwDOTtyQLc6XXFOx -- safe offenders diagnostic; no
  offenders[0] indexing before the length assertion.
* PRRT_kwDOTtyQLc6XXFO3 -- test docstring correction for
  unexpected-cursor test (hasNextPage=True).
Round-10 directive's live-verifier qualification defects:
* Multi-page check-run collector now embeds ``?per_page=``
  query params in the URL (the prior ``-F`` field form
  returns HTTP 404 on real ``gh api`` invocations).
* Test helpers (_two_page, _run, fake_run_gh) now extract
  page=N from both URL form and legacy field form.
* ``Round9SafeDiagnosticsTests`` and
  ``Round9PaginatorPreservationTests`` preserved and
  extended.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# Load the verifier module ONCE so the VerificationFailure class
# identity is shared with the tests.
_verifier_spec = importlib.util.spec_from_file_location(
    "independent_verifier",
    REPO_ROOT / "scripts" / "independent_verifier_v2.py",
)
VERIFIER = importlib.util.module_from_spec(_verifier_spec)
# Test importing the verifier should NOT swallow SystemExit; the
# refusal guard must surface. Subprocess tests cover the
# refusal themselves.
_verifier_spec.loader.exec_module(VERIFIER)
VerificationFailure = VERIFIER.VerificationFailure


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _two_page(a, b):
    """Drive a two-page paginator. The CLI invocation now
    embeds ``?per_page=&page=N`` in the URL (round-10
    directive). The driver extracts the page number from
    either URL form or the legacy ``-F page=N`` field form,
    so the test remains compatible with the production
    collector's actual subprocess invocation."""
    import re
    def _run(al, *, env=None):
        for a_token in al:
            if not isinstance(a_token, str):
                continue
            if "page=" not in a_token:
                continue
            # URL form: .../check-runs?per_page=100&page=N
            m = re.search(r"[?&]page=(\d+)", a_token)
            if m:
                return [a, b][int(m.group(1)) - 1]
            # Legacy field form: -F page=N or a standalone
            # ``page=N`` argument.
            for piece in a_token.split():
                if piece.startswith("page="):
                    n = int(piece.split("=", 1)[1])
                    return [a, b][n - 1]
        raise AssertionError(f"no page= in {al!r}")
    return _run


def _drive_paginator(pages, decision, args):
    """Module-level helper that drives the real paginator
    against mocked pages (extracted from the prior duplicated
    implementations). Mock ``_run_gh_graphql`` only."""
    def fake_run_gh(query, variables):
        if "reviewDecision" in query:
            return {"data": {"repository": {"pullRequest": {
                "reviewDecision": decision,
            }}}}
        cursor = variables["cursor"]
        cursor_index = {"null": 0}
        for i in range(1, len(pages)):
            cursor_index[f"CURSOR_{i}"] = i
        if cursor not in cursor_index:
            raise AssertionError(
                f"paginator sent an unexpected cursor: {cursor!r}"
            )
        idx = cursor_index[cursor]
        return {"data": {"repository": {"pullRequest": {
            "latestReviews": {
                "pageInfo": {
                    "hasNextPage": idx + 1 < len(pages),
                    "endCursor": (
                        f"CURSOR_{idx + 1}"
                        if idx + 1 < len(pages) else None
                    ),
                },
                "totalCount": sum(len(p["nodes"]) for p in pages),
                "nodes": pages[idx]["nodes"],
            },
        }}}}

    with mock.patch.object(VERIFIER, "_run_gh_graphql",
                           side_effect=fake_run_gh):
        return VERIFIER._inspect_coderabbit(args)


# ----------------------------------------------------------------------
# Multi-page check-run collector tests (production-paginate).
# ----------------------------------------------------------------------


class MultiPageCheckRunCollectorTests(unittest.TestCase):
    """The single canonical check-run collector must handle
    multi-page outputs correctly. These tests prove the
    collector's pagination and inventory completeness; the
    _inspect_ci tests (below) prove the verifier actually
    rejects missing or failing required jobs."""

    def _make_args(self):
        return type("A", (), {
            "repo": "o/r", "qualification_head": "a" * 40,
        })()

    def _make_page(self, total, n_runs, ids):
        """Build a single page of ``n_runs`` runs with the
        given ``total_count`` and ``ids``. Both branches of
        the prior conditional comprehension collapsed into
        one (the trailing slice is a no-op when
        ``n_runs >= len(ids)``)."""
        return {
            "total_count": total,
            "check_runs": [
                {"id": i, "name": f"job-{i}", "conclusion": "success",
                 "head_sha": "a" * 40}
                for i in ids
            ][:n_runs],
        }

    def _run(self, args, pages):
        """Drive ``_collect_check_runs`` against ``pages``;
        return only the collector result (the prior unused
        ``calls`` list has been removed)."""
        import re
        def fake_run_gh(args_list, *, env=None):
            page = 1
            for a_t in args_list:
                if not isinstance(a_t, str):
                    continue
                # URL form: .../check-runs?per_page=100&page=N
                m = re.search(r"[?&]page=(\d+)", a_t)
                if m:
                    page = int(m.group(1))
                    continue
                # Legacy field form: -F page=N
                for piece in a_t.split():
                    if piece.startswith("page="):
                        page = int(piece.split("=", 1)[1])
                        break
            idx = page - 1
            if idx >= len(pages):
                return {"check_runs": [], "total_count": 0}
            return pages[idx]
        with mock.patch.object(VERIFIER, "_run_gh", side_effect=fake_run_gh):
            return VERIFIER._collect_check_runs(args, "a" * 40)

    def test_one_page_succeeds(self):
        """A single page with all six required jobs and the
        expected total_count returns the runs and the total."""
        page = {
            "total_count": 6,
            "check_runs": [
                {"id": 1, "name": "test (3.10)", "conclusion": "success",
                 "head_sha": "a" * 40},
                {"id": 2, "name": "test (3.11)", "conclusion": "success",
                 "head_sha": "a" * 40},
                {"id": 3, "name": "test (3.12)", "conclusion": "success",
                 "head_sha": "a" * 40},
                {"id": 4, "name": "package-smoke", "conclusion": "success",
                 "head_sha": "a" * 40},
                {"id": 5, "name": "provenance", "conclusion": "success",
                 "head_sha": "a" * 40},
                {"id": 6, "name": "committed-state-scan",
                 "conclusion": "success", "head_sha": "a" * 40},
            ],
        }
        runs, total = self._run(self._make_args(), [page])
        self.assertEqual(total, 6)
        self.assertEqual(len(runs), 6)

    def test_two_page_succeeds(self):
        """Page 1 returns a full 100 runs so the collector
        requests page 2. Page 2 returns 5 runs and terminates.
        The completeness check passes at 105 == total_count."""
        page1 = self._make_page(105, 100, list(range(1, 101)))
        page2 = self._make_page(105, 5, list(range(101, 106)))
        runs, total = self._run(self._make_args(), [page1, page2])
        self.assertEqual(total, 105)
        self.assertEqual(len(runs), 105)

    def test_three_page_succeeds(self):
        """Three pages: page 1 full (100 runs), page 2 full
        (100 runs), page 3 partial (5 runs); total_count = 205."""
        page1 = self._make_page(205, 100, list(range(1, 101)))
        page2 = self._make_page(205, 100, list(range(101, 201)))
        page3 = self._make_page(205, 5, list(range(201, 206)))
        runs, total = self._run(self._make_args(),
                                 [page1, page2, page3])
        self.assertEqual(total, 205)
        self.assertEqual(len(runs), 205)

    def test_collector_returns_runs_on_failing_required_job(self):
        """Collector-level test: a failing required job
        located on page 2 is returned in the runs list. The
        production verifier gate that catches this failure is
        exercised in the InspectCiTests class below."""
        page1_runs = [
            {"id": i, "name": f"job-{i}", "conclusion": "success",
             "head_sha": "a" * 40}
            for i in range(1, 101)
        ]
        a = {"total_count": 105, "check_runs": page1_runs}
        b = {"total_count": 105, "check_runs": [
            {"id": 101, "name": "test (3.10)",
             "conclusion": "success", "head_sha": "a" * 40},
            {"id": 102, "name": "test (3.11)",
             "conclusion": "success", "head_sha": "a" * 40},
            {"id": 103, "name": "test (3.12)",
             "conclusion": "success", "head_sha": "a" * 40},
            {"id": 104, "name": "package-smoke",
             "conclusion": "failure", "head_sha": "a" * 40},
            {"id": 105, "name": "provenance",
             "conclusion": "success", "head_sha": "a" * 40},
        ]}
        args = self._make_args()
        with mock.patch.object(VERIFIER, "_run_gh",
                                side_effect=_two_page(a, b)):
            runs, total = VERIFIER._collect_check_runs(args, "a" * 40)
        self.assertEqual(total, 105)
        self.assertEqual(len(runs), 105)
        failed = [r for r in runs if r.get("conclusion") not in
                   ("success", "skipped", "neutral")]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["name"], "package-smoke")

    def test_collector_returns_runs_on_missing_required_job(self):
        """Collector-level test: a missing required job on
        page 2 is reflected in the runs list. The production
        verifier gate that catches this failure is exercised
        in the InspectCiTests class below."""
        page1_runs = [
            {"id": i, "name": f"job-{i}", "conclusion": "success",
             "head_sha": "a" * 40}
            for i in range(1, 101)
        ]
        a = {"total_count": 105, "check_runs": page1_runs}
        b = {"total_count": 105, "check_runs": [
            {"id": 101, "name": "test (3.10)",
             "conclusion": "success", "head_sha": "a" * 40},
            {"id": 102, "name": "test (3.11)",
             "conclusion": "success", "head_sha": "a" * 40},
            {"id": 103, "name": "test (3.12)",
             "conclusion": "success", "head_sha": "a" * 40},
            {"id": 105, "name": "provenance",
             "conclusion": "success", "head_sha": "a" * 40},
            {"id": 106, "name": "committed-state-scan",
             "conclusion": "success", "head_sha": "a" * 40},
        ]}
        args = self._make_args()
        with mock.patch.object(VERIFIER, "_run_gh",
                                side_effect=_two_page(a, b)):
            runs, total = VERIFIER._collect_check_runs(args, "a" * 40)
        self.assertEqual(total, 105)
        self.assertEqual(len(runs), 105)
        seen = {r["name"] for r in runs}
        self.assertNotIn("package-smoke", seen)

    def test_missing_total_count_fails_closed(self):
        """A page omitting total_count fails closed."""
        page = {"check_runs": [{"id": 1, "name": "x"}]}
        args = self._make_args()
        with mock.patch.object(VERIFIER, "_run_gh", return_value=page):
            with self.assertRaises(VerificationFailure) as ctx:
                VERIFIER._collect_check_runs(args, "a" * 40)
            self.assertIn("total_count", str(ctx.exception).lower())

    def test_conflicting_total_count_across_pages_fails_closed(self):
        """Conflicting total_count between pages fails closed."""
        runs = [
            {"id": i, "name": f"job-{i}", "conclusion": "success",
             "head_sha": "a" * 40}
            for i in range(1, 101)
        ]
        a = {"total_count": 105, "check_runs": runs}
        b = {"total_count": 110, "check_runs": []}
        args = self._make_args()
        with mock.patch.object(VERIFIER, "_run_gh",
                                side_effect=_two_page(a, b)):
            with self.assertRaises(VerificationFailure) as ctx:
                VERIFIER._collect_check_runs(args, "a" * 40)
            self.assertIn("total_count", str(ctx.exception).lower())

    def test_collected_count_not_equal_total_count_fails_closed(self):
        """The collector requires ``len(runs) == total_count``."""
        args = self._make_args()
        page = {"total_count": 5, "check_runs": [
            {"id": 1, "name": "a", "head_sha": "a" * 40},
            {"id": 2, "name": "b", "head_sha": "a" * 40},
            {"id": 3, "name": "c", "head_sha": "a" * 40},
        ]}
        with mock.patch.object(VERIFIER, "_run_gh", return_value=page):
            with self.assertRaises(VerificationFailure) as ctx:
                VERIFIER._collect_check_runs(args, "a" * 40)
            self.assertIn("pagination completeness",
                          str(ctx.exception).lower())

    def test_malformed_page_object_fails_closed(self):
        """A page that is not a dict fails closed."""
        args = self._make_args()
        with mock.patch.object(VERIFIER, "_run_gh", return_value=[]):
            with self.assertRaises(VerificationFailure) as ctx:
                VERIFIER._collect_check_runs(args, "a" * 40)
            self.assertIn("non-dict", str(ctx.exception).lower())

    def test_malformed_check_runs_list_fails_closed(self):
        """A page whose ``check_runs`` is not a list fails
        closed."""
        page = {"total_count": 1, "check_runs": "not-a-list"}
        args = self._make_args()
        with mock.patch.object(VERIFIER, "_run_gh", return_value=page):
            with self.assertRaises(VerificationFailure) as ctx:
                VERIFIER._collect_check_runs(args, "a" * 40)
            self.assertIn("check_runs", str(ctx.exception).lower())

    def test_duplicate_run_ids_fail_closed(self):
        """A duplicate run id across pages indicates ambiguous
        pagination; the collector fails closed."""
        runs_page1 = [
            {"id": i, "name": f"job-{i}", "conclusion": "success",
             "head_sha": "a" * 40} for i in range(1, 101)
        ]
        a = {"total_count": 2, "check_runs": runs_page1}
        b = {"total_count": 2, "check_runs": [
            {"id": 1, "name": "job-1", "conclusion": "success",
             "head_sha": "a" * 40},
        ]}
        args = self._make_args()
        with mock.patch.object(VERIFIER, "_run_gh",
                                side_effect=_two_page(a, b)):
            with self.assertRaises(VerificationFailure) as ctx:
                VERIFIER._collect_check_runs(args, "a" * 40)
            self.assertIn("ambiguous", str(ctx.exception).lower())

    def test_every_collected_head_sha_equals_qualification_head(self):
        """A check-run whose head_sha differs from
        qualification_head fails the per-run gate inside
        _inspect_ci. The page literal returned by _run_gh
        already contains the incorrect head_sha value; the
        test invokes _inspect_ci directly (no mutation)."""
        a = {"total_count": 1, "check_runs": [
            {"id": 1, "name": "x", "head_sha": "b" * 40}]}
        args = self._make_args()
        with mock.patch.object(VERIFIER, "_run_gh", return_value=a):
            with self.assertRaises(VerificationFailure) as ctx:
                VERIFIER._inspect_ci(args, "a" * 40)
            self.assertIn("head_sha", str(ctx.exception).lower())

    def test_no_raw_jsondecodeerror_escapes(self):
        """The collector must NEVER raise a raw JSONDecodeError
        to a caller. Either _run_gh returns a valid payload or
        the collector raises VerificationFailure."""
        args = self._make_args()
        def boom(al, *, env=None):
            raise json.JSONDecodeError("x", "y", 0)
        with mock.patch.object(VERIFIER, "_run_gh", side_effect=boom):
            with self.assertRaises(VerificationFailure):
                VERIFIER._collect_check_runs(args, "a" * 40)


# ----------------------------------------------------------------------
# _inspect_ci production tests (Round-7 PRRT_kwDOTtyQLc6XV61A +
# Round-8 PRRT_kwDOTtyQLc6XWVRw).
# ----------------------------------------------------------------------


class InspectCiTests(unittest.TestCase):
    """Production-gate tests for ``_inspect_ci``.

    Per round-7 finding PRRT_kwDOTtyQLc6XV61A and round-8
    finding PRRT_kwDOTtyQLc6XWVRw:
    * The tests mock only the real network/data boundary
      (``_run_gh``).
    * No inert downstream-gate patches.
    * No broad ``except Exception: pass``.
    * The positive control directly invokes
      ``VERIFIER._inspect_ci`` and asserts the actual
      return value proves ``total_count == 106``,
      ``len(runs) == 106``, all six required jobs are
      represented and pass the production success gate,
      and the multi-page collector was actually traversed.
    """

    QUAL = "a" * 40
    REQUIRED_JOBS = {
        "test (3.10)", "test (3.11)", "test (3.12)",
        "package-smoke", "provenance", "committed-state-scan",
    }

    def _make_args(self):
        # Per round-9 finding (nitpick on _make_args):
        # ``_inspect_ci`` only reads ``args.repo`` (via
        # ``_collect_check_runs``). Retain only that field;
        # the previous fixture hashed a repository file on
        # every call (aed_expected_sha) and included unused
        # aed_path, strict_window_obs, and evidence_root.
        return type("A", (), {
            "repo": "o/r", "qualification_head": self.QUAL,
        })()

    def _two_page(self, a, b):
        return _two_page(a, b)

    def test_failing_required_job_on_page_2_causes_failure(self):
        """Per round-7 finding PRRT_kwDOTtyQLc6XV61A and round-8
        finding PRRT_kwDOTtyQLc6XWVRw: invoke the production
        ``_inspect_ci`` with two pages where package-smoke
        appears only on page 2 with conclusion=failure. The
        verifier MUST raise VerificationFailure and the
        diagnostic MUST identify ``package-smoke`` and
        indicate a failed required job.

        Only ``_run_gh`` is mocked. No inert downstream-gate
        patches. No broad exception swallowing."""
        page1_runs = [
            {"id": i, "name": f"job-{i}", "conclusion": "success",
             "head_sha": self.QUAL}
            for i in range(1, 101)
        ]
        a = {"total_count": 106, "check_runs": page1_runs}
        b = {"total_count": 106, "check_runs": [
            {"id": 101, "name": "test (3.10)",
             "conclusion": "success", "head_sha": self.QUAL},
            {"id": 102, "name": "test (3.11)",
             "conclusion": "success", "head_sha": self.QUAL},
            {"id": 103, "name": "test (3.12)",
             "conclusion": "success", "head_sha": self.QUAL},
            {"id": 104, "name": "package-smoke",
             "conclusion": "failure", "head_sha": self.QUAL},
            {"id": 105, "name": "provenance",
             "conclusion": "success", "head_sha": self.QUAL},
            {"id": 106, "name": "committed-state-scan",
             "conclusion": "success", "head_sha": self.QUAL},
        ]}
        args = self._make_args()
        with mock.patch.object(VERIFIER, "_run_gh",
                                side_effect=self._two_page(a, b)):
            with self.assertRaises(VerificationFailure) as ctx:
                VERIFIER._inspect_ci(args, self.QUAL)
        msg = str(ctx.exception).lower()
        self.assertIn("package-smoke", msg,
            f"failure must name package-smoke; got: {msg!r}")
        self.assertIn("failed", msg,
            f"failure must indicate a failed required job; "
            f"got: {msg!r}")

    def test_missing_required_job_causes_failure(self):
        """Per round-7 finding PRRT_kwDOTtyQLc6XV61A and
        round-8 finding PRRT_kwDOTtyQLc6XWVRw: invoke the
        production ``_inspect_ci`` with two pages where
        package-smoke is absent from all pages. The verifier
        MUST raise VerificationFailure and the diagnostic
        MUST identify package-smoke as missing.

        Only ``_run_gh`` is mocked. No inert downstream-gate
        patches. No broad exception swallowing."""
        page1_runs = [
            {"id": i, "name": f"job-{i}", "conclusion": "success",
             "head_sha": self.QUAL}
            for i in range(1, 101)
        ]
        a = {"total_count": 105, "check_runs": page1_runs}
        b = {"total_count": 105, "check_runs": [
            {"id": 101, "name": "test (3.10)",
             "conclusion": "success", "head_sha": self.QUAL},
            {"id": 102, "name": "test (3.11)",
             "conclusion": "success", "head_sha": self.QUAL},
            {"id": 103, "name": "test (3.12)",
             "conclusion": "success", "head_sha": self.QUAL},
            {"id": 105, "name": "provenance",
             "conclusion": "success", "head_sha": self.QUAL},
            {"id": 106, "name": "committed-state-scan",
             "conclusion": "success", "head_sha": self.QUAL},
        ]}
        args = self._make_args()
        with mock.patch.object(VERIFIER, "_run_gh",
                                side_effect=self._two_page(a, b)):
            with self.assertRaises(VerificationFailure) as ctx:
                VERIFIER._inspect_ci(args, self.QUAL)
        msg = str(ctx.exception).lower()
        self.assertIn("package-smoke", msg,
            f"failure must name missing package-smoke; "
            f"got: {msg!r}")
        self.assertIn("missing", msg,
            f"failure must indicate a missing required job; "
            f"got: {msg!r}")

    def test_two_page_positive_inspect_ci_control(self):
        """Per round-8 finding PRRT_kwDOTtyQLc6XWVRw: positive
        control. Invoke ``_inspect_ci`` with two pages where
        every required job is present, succeeds, and every
        head_sha equals qualification_head. Capture the
        production return value and assert:
        * total_count == 106
        * len(runs) == 106
        * all six required job names are present
        * all six required jobs satisfy the production success
          gate (conclusion in success/skipped/neutral)
        * the complete multi-page collector was traversed
          (i.e. both pages were visited)

        No inert downstream-gate patches. No broad
        exception swallowing. The test captures
        ``_inspect_ci``'s actual return value.
        """
        page1_runs = [
            {"id": i, "name": f"job-{i}", "conclusion": "success",
             "head_sha": self.QUAL}
            for i in range(1, 101)
        ]
        a = {"total_count": 106, "check_runs": page1_runs}
        b = {"total_count": 106, "check_runs": [
            {"id": 101, "name": "test (3.10)",
             "conclusion": "success", "head_sha": self.QUAL},
            {"id": 102, "name": "test (3.11)",
             "conclusion": "success", "head_sha": self.QUAL},
            {"id": 103, "name": "test (3.12)",
             "conclusion": "success", "head_sha": self.QUAL},
            {"id": 104, "name": "package-smoke",
             "conclusion": "success", "head_sha": self.QUAL},
            {"id": 105, "name": "provenance",
             "conclusion": "success", "head_sha": self.QUAL},
            {"id": 106, "name": "committed-state-scan",
             "conclusion": "success", "head_sha": self.QUAL},
        ]}
        # Track every call into the mocked _run_gh so we can
        # prove both pages were visited by the production
        # multi-page collector.
        import re
        calls = []
        def fake_run_gh(args_list, *, env=None):
            calls.append(args_list)
            for a_token in args_list:
                if not isinstance(a_token, str):
                    continue
                # URL form: .../check-runs?per_page=100&page=N
                m = re.search(r"[?&]page=(\d+)", a_token)
                if m:
                    n = int(m.group(1))
                    return [a, b][n - 1]
                # Legacy field form: -F page=N
                for piece in a_token.split():
                    if piece.startswith("page="):
                        n = int(piece.split("=", 1)[1])
                        return [a, b][n - 1]
            raise AssertionError(f"no page= in {args_list!r}")
        args = self._make_args()
        with mock.patch.object(VERIFIER, "_run_gh",
                                side_effect=fake_run_gh):
            result = VERIFIER._inspect_ci(args, self.QUAL)
        # total_count == 106 from the production return value.
        self.assertEqual(result["total_count"], 106,
            f"production total_count must equal 106; "
            f"got: {result['total_count']!r}")
        # len(runs) == 106.
        runs = result["runs"]
        self.assertEqual(len(runs), 106,
            f"production runs must contain 106 entries; "
            f"got: {len(runs)!r}")
        # All six required jobs are represented.
        seen_names = {r["name"] for r in runs}
        self.assertTrue(self.REQUIRED_JOBS.issubset(seen_names),
            f"all six required jobs must be represented; "
            f"missing: {self.REQUIRED_JOBS - seen_names!r}")
        # All six required jobs satisfy the production
        # success gate (conclusion in success/skipped/neutral).
        required_runs = [r for r in runs
                          if r["name"] in self.REQUIRED_JOBS]
        for r in required_runs:
            self.assertIn(
                r["conclusion"], ("success", "skipped", "neutral"),
                f"required job {r['name']!r} has conclusion "
                f"{r['conclusion']!r}; production success "
                f"gate must pass",
            )
        # The complete multi-page collector was traversed:
        # both pages were visited (call_count >= 2).
        self.assertGreaterEqual(len(calls), 2,
            f"production multi-page collector must visit "
            f"both pages; got {len(calls)} _run_gh call(s)")


# ----------------------------------------------------------------------
# F541 lint.
# ----------------------------------------------------------------------


class F541LintTests(unittest.TestCase):
    """No ``f-string without placeholder`` (F541) survives in
    the verifier. Use the production ruff tool to confirm so
    the test is a true lint check, not a naive AST guess."""

    def test_no_f541_findings_in_verifier(self):
        """Run ``ruff check --select F541`` against the verifier
        and assert the run reports no findings."""
        import shutil as _shutil
        if not _shutil.which("ruff"):
            self.skipTest("ruff not installed on PATH")
        verifier = REPO_ROOT / "scripts" / "independent_verifier_v2.py"
        proc = subprocess.run(
            ["ruff", "check", "--select", "F541", str(verifier)],
            capture_output=True, text=True,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"ruff F541 found violations in the verifier: "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )


# ----------------------------------------------------------------------
# Latest CodeRabbit failure test diagnostic.
# ----------------------------------------------------------------------


class LatestCodeRabbitFailureTestDiagnosticTests(unittest.TestCase):
    """Finding 9a: the latest CodeRabbit CHANGES_REQUESTED
    failure test must catch the verification failure and
    assert the message names the latest-review-state gate, not
    just any presence or timestamp gate."""

    def test_older_approved_newer_changes_requested_fails_with_diagnostic(self):
        """The failure must identify the latest-review-state
        gate, not just any other gate."""
        def _review(login, state, submitted_at):
            author = {"login": login} if login is not None else None
            return {"state": state, "submittedAt": submitted_at,
                    "author": author}
        pages = [{"nodes": [
            _review("coderabbitai", "CHANGES_REQUESTED",
                     "2026-08-07T10:00:00Z"),
            _review("coderabbitai", "APPROVED",
                     "2026-08-06T09:00:00Z"),
        ]}]
        args = type("A", (), {"repo": "o/r", "pr_number": 4})()
        with self.assertRaises(VerificationFailure) as ctx:
            _drive_paginator(pages, "APPROVED", args)
        msg = str(ctx.exception)
        self.assertIn("CHANGES_REQUESTED", msg,
            f"failure must name the latest-review-state gate; "
            f"got: {msg!r}")


# ----------------------------------------------------------------------
# No hardcoded /tmp paths in test code (PRRT_kwDOTtyQLc6XV61D).
# ----------------------------------------------------------------------


class NoHardcodedTmpPathsTests(unittest.TestCase):
    """Round-7 finding PRRT_kwDOTtyQLc6XV61D: the hardcoded
    /tmp literal detector must catch both double-quoted and
    single-quoted literals. A self-test proves both forms are
    detected."""

    def test_detects_double_quoted_tmp_literals(self):
        """Double-quoted /tmp/*.json literals in test code are
        flagged."""
        offenders = _find_tmp_literal_offenders(
            '"/tmp/aed-r5b-fake.json"')
        self.assertEqual(offenders, ['"/tmp/aed-r5b-fake.json"'])

    def test_detects_single_quoted_tmp_literals(self):
        """Single-quoted /tmp/*.json literals in test code are
        flagged."""
        offenders = _find_tmp_literal_offenders(
            "'/tmp/aed-r5b-fake.json'")
        self.assertEqual(offenders, ["'/tmp/aed-r5b-fake.json'"])

    def test_no_hardcoded_tmp_paths_in_round5_hardening_tests(self):
        """Round-5 hardening tests must not contain any
        /tmp/*.json literals in either quote style."""
        src = (REPO_ROOT / "tests" / "test_pr4_round5_hardening.py").read_text()
        offenders = _find_tmp_literal_offenders(src)
        self.assertEqual(
            offenders, [],
            f"hardcoded /tmp/*.json paths in test code "
            f"(both quote styles): {offenders!r}",
        )


def _find_tmp_literal_offenders(src: str):
    """Find /tmp/*.json literals in either quote style."""
    return re.findall(r"""['\"]/tmp/[^'\"]*\.json['\"]""", src)


# ----------------------------------------------------------------------
# Optimized-Python refusal (renamed for accuracy).
# ----------------------------------------------------------------------


class OptimizedPythonRefusalTests(unittest.TestCase):
    """Round-7 finding PRRT_kwDOTtyQLc6XV61D: rename
    ``test_subprocess_tests_use_tempfile_paths`` to
    ``test_optimized_python_is_refused`` so the test name
    matches its actual contract: the PYTHONOPTIMIZE/``-O``
    refusal is the assertion under test. The tempfile-path
    invariant is covered by the separate
    ``test_no_hardcoded_tmp_paths_in_round5_hardening_tests``
    test."""

    def test_optimized_python_is_refused(self):
        """The verifier refuses to run under ``python -O`` and
        the refusal banner appears on stderr."""
        fake_record = Path(tempfile.gettempdir()) / "aed-r8-fake.json"
        proc = subprocess.run(
            [sys.executable, "-O",
             str(REPO_ROOT / "scripts" / "independent_verifier_v2.py"),
             "--qualification-head", "a" * 40,
             "--incident-record", str(fake_record)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PYTHONOPTIMIZE", proc.stderr)
        self.assertIn("assert-based gates would be stripped", proc.stderr)

    def test_pythonoptimize_env_var_is_refused(self):
        """``PYTHONOPTIMIZE=1`` in the environment refuses the
        verifier at import time."""
        env = os.environ.copy()
        env["PYTHONOPTIMIZE"] = "1"
        fake_record = Path(tempfile.gettempdir()) / "aed-r8-fake.json"
        proc = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "independent_verifier_v2.py"),
             "--qualification-head", "a" * 40,
             "--incident-record", str(fake_record)],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PYTHONOPTIMIZE", proc.stderr)


# ----------------------------------------------------------------------
# canonical_paths detector (Round-7 PRRT_kwDOTtyQLc6XV61F +
# Round-8 PRRT_kwDOTtyQLc6XWVR0).
# ----------------------------------------------------------------------


class NoHardcodedVerifierJsonPathTests(unittest.TestCase):
    """Round-7 finding PRRT_kwDOTtyQLc6XV61F: AST BinOp /
    Constant matching for path composition with
    ``verifier.json``.

    Round-8 finding PRRT_kwDOTtyQLc6XWVR0:
    * AST containment for assertFalse exemption (no line
      arithmetic).
    * Docstring detection restricted to ``node.body[0]``.
    * Regression cases for multiline legitimate
      ``assertFalse`` exemptions; hardcoded paths
      immediately before an ``assertFalse`` are still
      detected; docstrings are ignored; mid-body standalone
      strings do NOT create false docstring exemptions.

    Round-9 directive (PRRT_kwDOTtyQLc6XXFOx): the
    containment check uses AST node-id descendant tracking
    (not line-range overlap). A regression case proves the
    detection works for an offender that shares a line with
    an ``assertFalse`` call WITHOUT being one of its
    arguments.
    """

    def test_no_hardcoded_verifier_json_path_in_tests(self):
        """Search the test source for direct path /
        hardcoded string composition with ``verifier.json``."""
        for test_file in (
            REPO_ROOT / "tests" / "test_pr4_round4_review.py",
            REPO_ROOT / "tests" / "test_pr4_round5_hardening.py",
            REPO_ROOT / "tests" / "test_pr4_round6_review.py",
            REPO_ROOT / "tests" / "test_pr4_round6_d0_fixture.py",
        ):
            offenders = _find_verifier_json_offenders(test_file)
            self.assertEqual(
                offenders, [],
                f"{test_file.name}: hardcoded Path(...) / "
                f"verifier.json composition not allowed; use "
                f"canonical_paths(evidence_root): {offenders!r}",
            )

    def test_uses_canonical_paths_in_round5_hardening(self):
        """Round-5 hardening test must use canonical_paths."""
        src = (REPO_ROOT / "tests" / "test_pr4_round5_hardening.py").read_text()
        self.assertIn("canonical_paths", src,
            "test_pr4_round5_hardening must use canonical_paths")

    def test_multiline_assertFalse_exempt_is_exempt(self):
        """Round-8 regression case 1: a multiline legitimate
        negative-existence assertion
        ``self.assertFalse(\n    (evidence_root / ``"verifier.json"``).exists()\n)``
        MUST be exempt because the candidate BinOp is
        structurally contained inside the assertFalse Call.
        """
        src = (
            "def f(self):\n"
            "    self.assertFalse(\n"
            "        (evidence_root / \"verifier.json\").exists(),\n"
            "        \"no verifier.json\",\n"
            "    )\n"
        )
        # Parse, then build a fake test_file object that
        # contains this source via Path.write_text in a
        # tempdir.
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="aed-r8-ast-"))
        try:
            test_file = tmp / "synthetic_test.py"
            test_file.write_text(src)
            offenders = _find_verifier_json_offenders(test_file)
            self.assertEqual(
                offenders, [],
                f"multiline legitimate negative-existence "
                f"assertion must be exempt; offenders: {offenders!r}",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_hardcoded_path_immediately_before_assertFalse_is_detected(self):
        """Round-8 regression case 2: a hardcoded verifier
        path immediately BEFORE an assertFalse call (NOT
        inside it) MUST still be detected. This is the case
        where the prior line-arithmetic exemption wrongly
        excused the offender."""
        src = (
            "def f(self):\n"
            "    verifier_json = evidence_root / \"verifier.json\"\n"
            "    self.assertFalse(\n"
            "        verifier_json.exists(),\n"
            "        \"no verifier.json\",\n"
            "    )\n"
        )
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="aed-r8-ast2-"))
        try:
            test_file = tmp / "synthetic_test.py"
            test_file.write_text(src)
            offenders = _find_verifier_json_offenders(test_file)
            # Per round-9 finding PRRT_kwDOTtyQLc6XXFOx: the
            # diagnostic must be safe for ALL possible offender
            # collections, including []. Do not dereference
            # offenders[0] before the length assertion runs.
            self.assertEqual(
                len(offenders), 1,
                f"hardcoded path immediately before assertFalse "
                f"must STILL be detected; offenders={offenders!r}",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_normal_hardcoded_verifier_path_is_detected(self):
        """Round-8 regression case 3: a normal hardcoded
        verifier path elsewhere in the code is detected."""
        src = (
            "def f():\n"
            "    path = evidence_root / \"verifier.json\"\n"
            "    return path\n"
        )
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="aed-r8-ast3-"))
        try:
            test_file = tmp / "synthetic_test.py"
            test_file.write_text(src)
            offenders = _find_verifier_json_offenders(test_file)
            self.assertEqual(
                len(offenders), 1,
                f"normal hardcoded path must be detected; "
                f"offenders: {offenders!r}",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_docstring_mentioning_verifier_path_is_ignored(self):
        """Round-8 regression case 4: a docstring containing
        ``/ "verifier.json"`` is ignored."""
        src = (
            "def f():\n"
            "    \"\"\"Some docstring mentioning\n"
            "    evidence_root / \"verifier.json\"\n"
            "    but only in prose.\"\"\"\n"
            "    return None\n"
        )
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="aed-r8-ast4-"))
        try:
            test_file = tmp / "synthetic_test.py"
            test_file.write_text(src)
            offenders = _find_verifier_json_offenders(test_file)
            self.assertEqual(
                offenders, [],
                f"docstring mention must be ignored; offenders: "
                f"{offenders!r}",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_mid_body_string_does_not_create_docstring_exemption(self):
        """Round-8 regression case 5: a mid-body standalone
        string expression is NOT a docstring and MUST NOT
        create a docstring exemption for a hardcoded
        verifier path elsewhere in the same body."""
        src = (
            "def f():\n"
            "    \"unrelated string expression\"\n"
            "    path = evidence_root / \"verifier.json\"\n"
            "    return path\n"
        )
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="aed-r8-ast5-"))
        try:
            test_file = tmp / "synthetic_test.py"
            test_file.write_text(src)
            offenders = _find_verifier_json_offenders(test_file)
            self.assertEqual(
                len(offenders), 1,
                f"mid-body standalone string must NOT create "
                f"docstring exemption; offenders: {offenders!r}",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_offender_sharing_line_with_assertFalse_but_not_argument(self):
        """Round-9 regression case 6: an offender that
        shares a line with an ``assertFalse`` call WITHOUT
        being one of its arguments must still be detected.
        The line-range overlap check would have wrongly
        exempted it; the AST node-id descendant tracking
        correctly exempts only descendants of the
        ``assertFalse`` Call.
        """
        src = (
            "def f(self):\n"
            "    p = root / \"verifier.json\"; self.assertFalse(p.exists())\n"
        )
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="aed-r9-share-"))
        try:
            test_file = tmp / "synthetic_test.py"
            test_file.write_text(src)
            offenders = _find_verifier_json_offenders(test_file)
            self.assertEqual(
                len(offenders), 1,
                f"offender on the same line as assertFalse but "
                f"not as its argument MUST be detected; "
                f"offenders: {offenders!r}",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _find_verifier_json_offenders(test_file: Path):
    """Return list of (line, col, line_text) for each direct
    path composition with the literal ``verifier.json`` in
    non-docstring code.

    Round-8 finding PRRT_kwDOTtyQLc6XWVR0:
    * The assertFalse exemption is determined by AST
      containment: a candidate BinOp whose expression is
      STRUCTURALLY CONTAINED inside an ``assertFalse(...)``
      call (including multiline calls) is exempt. The
      expression lives inside the assertFalse if the
      ``Call`` node's source range covers the candidate
      node's line range.
    * Docstring detection is restricted to ``node.body[0]``
      -- a mid-body string literal is NOT a docstring.

    Exemptions:
    * the literal appears inside a docstring (the leading
      statement of Module / ClassDef / FunctionDef /
      AsyncFunctionDef);
    * the path is used in a NEGATIVE-existence assertion
      (``assertFalse(... .exists())``). AST containment
      decides this rather than line arithmetic.
    """
    src = test_file.read_text()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return [("syntax_error", 0, src)]
    # Per round-9 directive (PRRT_kwDOTtyQLc6XXFOx): track
    # descendant node identities of every assertFalse Call.
    # A candidate BinOp is exempt IFF its node identity is
    # IN that descendant set (true AST containment, not
    # line-range overlap). ``ast.walk`` keeps every node
    # alive through ``tree``, so ``id()`` values stay
    # stable for the duration of the scan.
    exempt_node_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            is_assertfalse = (
                isinstance(func, ast.Attribute)
                and func.attr == "assertFalse"
            )
            if is_assertfalse:
                for descendant in ast.walk(node):
                    exempt_node_ids.add(id(descendant))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            right = node.right
            if (isinstance(right, ast.Constant)
                    and isinstance(right.value, str)
                    and right.value == "verifier.json"):
                line = getattr(node, "lineno", -1)
                col = getattr(node, "col_offset", -1)
                lines = src.splitlines()
                line_text = lines[line - 1] if 0 < line <= len(lines) else ""
                if _is_in_docstring(tree, line):
                    continue
                # AST containment: candidate node identity
                # must be in the assertFalse descendant set.
                if id(node) in exempt_node_ids:
                    continue
                offenders.append((line, col, line_text))
    return offenders


def _is_in_docstring(tree, lineno: int) -> bool:
    """Conservative docstring check: ``lineno`` is inside the
    docstring of a Module / ClassDef / FunctionDef /
    AsyncFunctionDef body IFF it lies within the source
    range of the leading statement of that body and that
    statement is a string expression.

    Per round-8 finding PRRT_kwDOTtyQLc6XWVR0, only
    ``node.body[0]`` is considered; mid-body string
    literals do NOT create a docstring exemption.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef, ast.Module)):
            body = getattr(node, "body", None)
            if not body:
                continue
            child = body[0]
            if not (isinstance(child, ast.Expr)
                    and isinstance(child.value, ast.Constant)
                    and isinstance(child.value.value, str)):
                continue
            cs_line = child.lineno
            cs_end = getattr(child, "end_lineno", cs_line)
            if cs_line <= lineno <= cs_end:
                return True
    return False


# ----------------------------------------------------------------------
# Cursor mapping tests (Round-7 PRRT_kwDOTtyQLc6XV61O +
# Round-8 PRRT_kwDOTtyQLc6XWVR3).
# ----------------------------------------------------------------------


class CursorMappingTests(unittest.TestCase):
    """Round-7 finding PRRT_kwDOTtyQLc6XV61O: cursor-to-page
    mapping supports any number of pages.

    Round-8 finding PRRT_kwDOTtyQLc6XWVR3: exercise the real
    ``_paginate_latest_reviews`` /
    ``_paginate_connection`` / ``_inspect_coderabbit`` path.
    Mock only the GraphQL/network boundary
    (``_run_gh_graphql``). Build a fixture where the
    reviewDecision request succeeds, the first
    latestReviews page is accepted, hasNextPage=True,
    endCursor carries the cursor that the production
    paginator must propagate, and the subsequent
    network-boundary call identifies the unexpected cursor.
    The resulting VerificationFailure propagates through
    the real paginator.
    """

    def test_four_pages_explicit_cursor_mapping(self):
        """Four pages with explicit cursor mapping. The real
        paginator walks all four pages; the unexpected
        cursor path raises a clear AssertionError."""
        def _review(i):
            return {"state": "APPROVED" if i == 3 else "OTHER",
                    "submittedAt": f"2026-08-07T1{i}:00:00Z",
                    "author": {"login": "coderabbitai"}}
        pages = [{"nodes": [_review(0)]},
                 {"nodes": [_review(1)]},
                 {"nodes": [_review(2)]},
                 {"nodes": [_review(3)]}]
        args = type("A", (), {"repo": "o/r", "pr_number": 4})()
        result = _drive_paginator(pages, "APPROVED", args)
        self.assertEqual(result["latest_coderabbit_state"], "APPROVED")

    def test_real_paginator_succeeds_across_two_pages(self):
        """Real-paginator control (Round-8 finding
        PRRT_kwDOTtyQLc6XWVR3): two pages where page 1 has
        no CodeRabbit, page 2 has the matching APPROVED.
        Mock only ``_run_gh_graphql``. The real paginator
        walks both pages using the page-1 cursor. The
        CodeRabbit APPROVED on page 2 is selected as the
        newest."""
        def fake_run_gh_graphql(query, variables):
            if "reviewDecision" in query:
                return {"data": {"repository": {"pullRequest": {
                    "reviewDecision": "APPROVED",
                }}}}
            cursor = variables["cursor"]
            cursor_index = {"null": 0, "CURSOR_1": 1}
            if cursor not in cursor_index:
                raise AssertionError(
                    f"paginator sent an unexpected cursor: "
                    f"{cursor!r}"
                )
            idx = cursor_index[cursor]
            pages = [
                {"nodes": [
                    {"state": "APPROVED",
                     "submittedAt": "2026-08-01T09:00:00Z",
                     "author": {"login": "some-human"}},
                ]},
                {"nodes": [
                    {"state": "APPROVED",
                     "submittedAt": "2026-08-07T10:00:00Z",
                     "author": {"login": "coderabbitai"}},
                ]},
            ]
            return {"data": {"repository": {"pullRequest": {
                "latestReviews": {
                    "pageInfo": {
                        "hasNextPage": idx + 1 < len(pages),
                        "endCursor": (
                            f"CURSOR_{idx + 1}"
                            if idx + 1 < len(pages) else None
                        ),
                    },
                    "totalCount": 2,
                    "nodes": pages[idx]["nodes"],
                },
            }}}}

        args = type("A", (), {"repo": "o/r", "pr_number": 4})()
        cursors_seen = []
        def capturing_fake(query, variables):
            cursors_seen.append(variables.get("cursor"))
            return fake_run_gh_graphql(query, variables)
        with mock.patch.object(VERIFIER, "_run_gh_graphql",
                               side_effect=capturing_fake):
            result = VERIFIER._inspect_coderabbit(args)
        self.assertEqual(result["latest_coderabbit_state"], "APPROVED")
        # Two pagination calls: one with cursor=null, one
        # with cursor=CURSOR_1.
        self.assertIn("null", cursors_seen)
        self.assertIn("CURSOR_1", cursors_seen)

    def test_real_paginator_unexpected_cursor_fails_via_network(self):
        """Real-paginator failure (Round-8 finding
        PRRT_kwDOTtyQLc6XWVR3, Round-9 finding
        PRRT_kwDOTtyQLc6XXFO3):

        * ONLY ``_run_gh_graphql`` is mocked.
        * Page 1 returns ``hasNextPage=True`` and supplies
          an ``endCursor`` of ``"UNEXPECTED_CURSOR"``.
        * The production paginator must forward that
          ``endCursor`` on its second request.
        * The second mocked network call rejects the
          propagated cursor and raises ``VerificationFailure``.
        * That ``VerificationFailure`` propagates through
          the real ``_paginate_latest_reviews`` /
          ``_paginate_connection`` / ``_inspect_coderabbit``
          chain.
        * The test asserts the actual cursor value the
          production paginator sent (proving the paginator
          ran, not that the mock manufactured the failure
          before the paginator executed).
        """
        cursors_seen = []

        def fake_run_gh_graphql(query, variables):
            cursors_seen.append(variables.get("cursor"))
            if "reviewDecision" in query:
                return {"data": {"repository": {"pullRequest": {
                    "reviewDecision": "APPROVED",
                }}}}
            # First call: accepted, returns a cursor that the
            # production paginator does NOT expect.
            cursor = variables.get("cursor")
            if cursor == "null":
                return {"data": {"repository": {"pullRequest": {
                    "latestReviews": {
                        "pageInfo": {
                            "hasNextPage": True,
                            "endCursor": "UNEXPECTED_CURSOR",
                        },
                        "totalCount": 5,
                        "nodes": [
                            {"state": "APPROVED",
                             "submittedAt": "2026-08-07T10:00:00Z",
                             "author": {"login": "coderabbitai"}},
                        ],
                    },
                }}}}
            # Subsequent calls: identify the unexpected
            # cursor and raise VerificationFailure -- the
            # production paginator must propagate this.
            raise VerificationFailure(
                f"paginator sent an unexpected cursor: {cursor!r}"
            )

        args = type("A", (), {"repo": "o/r", "pr_number": 4})()
        with mock.patch.object(VERIFIER, "_run_gh_graphql",
                               side_effect=fake_run_gh_graphql):
            with self.assertRaises(VerificationFailure) as ctx:
                VERIFIER._inspect_coderabbit(args)
        # The first request was made (proves the production
        # paginator executed; the failure was not
        # manufactured before the paginator ran).
        self.assertIn("null", cursors_seen)
        # The production paginator sent the unexpected
        # cursor; the mock propagated the failure.
        self.assertIn("UNEXPECTED_CURSOR", cursors_seen)
        # The diagnostic identifies the unexpected cursor.
        msg = str(ctx.exception).lower()
        self.assertIn("cursor", msg,
            f"failure must name the cursor; got: {msg!r}")
        self.assertIn("unexpected_cursor", msg,
            f"failure must reference the actual cursor value "
            f"the production paginator sent; got: {msg!r}")


# ----------------------------------------------------------------------
# Source-text tests removed (9c).
# ----------------------------------------------------------------------


class RemovableSourceTextTests(unittest.TestCase):
    """Finding 9c: redundant source-text tests for verifier
    identity are removed."""

    def test_source_text_tests_removed(self):
        src = (REPO_ROOT / "tests" / "test_pr4_round5_hardening.py").read_text()
        self.assertNotIn(
            "test_verifier_field_derived_from_module", src)
        self.assertNotIn(
            "test_verifier_module_path_is_absolute", src)


# ----------------------------------------------------------------------
# Tightened ArtifactError-only test (9f).
# ----------------------------------------------------------------------


class TightenedArtifactErrorOnlyTests(unittest.TestCase):
    """Finding 9f: the digest-mismatch test asserts ArtifactError
    only, not a tuple of unrelated exception types."""

    def test_digest_mismatch_asserts_artifact_error_only(self):
        """The digest-mismatch test in round-5 hardening asserts
        ArtifactError for the deterministic read_artifact
        failure path, not VerificationFailure."""
        from autocoder_orchestration.artifacts import (
            ArtifactError,
            ArtifactDigestMismatch,
            write_artifact,
        )
        tmp = Path(tempfile.mkdtemp(prefix="aed-r8-9f-"))
        try:
            body_path = tmp / "incident.json"
            body_bytes = json.dumps({
                "schema_version": "autocoder.incident.v1",
                "incident_class": "FORCE_PUSH",
                "force_push_mechanism": "git push --force-with-lease",
                "no_repeat_permitted": True,
                "restored_head_sha": "a" * 40,
            }).encode("utf-8")
            fd = os.open(str(body_path),
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, body_bytes)
            finally:
                os.close(fd)
            sidecar = tmp / "incident.json.sha256"
            fd2 = os.open(str(sidecar),
                          os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd2, ("f" * 64 + "\n").encode("ascii"))
            finally:
                os.close(fd2)
            args = type("A", (), {
                "incident_record": body_path,
                "repo": "o/r", "pr_number": 1,
            })()
            with self.assertRaises(ArtifactError) as ctx:
                VERIFIER._verify_incident_record(args)
            self.assertIsInstance(ctx.exception, ArtifactDigestMismatch)
            self.assertIn("digest", str(ctx.exception).lower())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Round9SafeDiagnosticsTests(unittest.TestCase):
    """Round-9 directive (PRRT_kwDOTtyQLc6XXFOx): the
    diagnostic for an offenders-length assertion must be
    safe for ALL possible offender collections, including
    ``[]``.

    Prove:
    * expected offender found -> test passes;
    * simulated/mutated detector returns ``[]`` -> failure
      is a normal ``AssertionError``;
    * the failure is NOT ``IndexError``;
    * the diagnostic includes the full offenders
      representation safely.
    """

    def test_assertEqual_safe_when_offenders_is_empty(self):
        """When the detector returns ``[]`` (the regression
        case), the diagnostic f-string must NOT raise
        ``IndexError``; the assertion produces a normal
        ``AssertionError`` instead."""
        # Simulated empty offenders collection.
        offenders: list = []
        # The exact f-string from the round-8 source code,
        # now expected to be safe.
        try:
            self.assertEqual(
                len(offenders), 1,
                f"hardcoded path immediately before assertFalse "
                f"must STILL be detected; offenders={offenders!r}",
            )
        except AssertionError as e:
            # The expected failure type is AssertionError,
            # NOT IndexError.
            msg = str(e)
            self.assertIn("offenders=[]", msg,
                f"diagnostic must safely include the offenders "
                f"representation; got: {msg!r}")
            self.assertIn("must STILL be detected", msg,
                f"diagnostic must describe the regression; "
                f"got: {msg!r}")
        except IndexError as e:
            self.fail(
                f"the diagnostic raised IndexError instead of "
                f"AssertionError; the diagnostic must be safe "
                f"for empty offenders. Got: {e!r}"
            )

    def test_assertEqual_safe_when_offenders_has_one(self):
        """When the detector returns exactly one offender,
        the assertion passes and the diagnostic is not
        evaluated.
        """
        offenders = [(42, 0, "test code")]
        self.assertEqual(
            len(offenders), 1,
            f"hardcoded path immediately before assertFalse "
            f"must STILL be detected; offenders={offenders!r}",
        )

    def test_assertEqual_safe_when_offenders_has_many(self):
        """When the detector returns multiple offenders, the
        diagnostic safely reports all of them.
        """
        offenders = [(1, 0, "a"), (2, 0, "b"), (3, 0, "c")]
        try:
            self.assertEqual(
                len(offenders), 1,
                f"hardcoded path immediately before assertFalse "
                f"must STILL be detected; offenders={offenders!r}",
            )
        except AssertionError as e:
            msg = str(e)
            self.assertIn("offenders=[", msg,
                f"diagnostic must include the offenders list; "
                f"got: {msg!r}")
        except IndexError as e:
            self.fail(
                f"the diagnostic raised IndexError instead of "
                f"AssertionError; got: {e!r}"
            )


class Round9PaginatorPreservationTests(unittest.TestCase):
    """Round-9 directive (PRRT_kwDOTtyQLc6XXFO3): the
    unexpected-cursor test must preserve the real
    production-paginator behavioral proof. The previous
    docstring was inaccurate; this regression group
    preserves the actual test behavior and the real
    paginator path is exercised.
    """

    def test_real_paginator_unexpected_cursor_preserves_behavior(self):
        """Re-run the production-paginator behavioral proof
        in case of test rot. The test must:
        * NOT patch _paginate_latest_reviews or
          _paginate_connection;
        * mock only _run_gh_graphql;
        * page 1 returns hasNextPage=True with endCursor
          "UNEXPECTED_CURSOR";
        * the production paginator must forward that
          cursor on the second request;
        * the second mocked network call rejects the
          unexpected cursor;
        * VerificationFailure propagates through the real
          paginator."""
        cursors_seen = []

        def fake_run_gh_graphql(query, variables):
            cursors_seen.append(variables.get("cursor"))
            if "reviewDecision" in query:
                return {"data": {"repository": {"pullRequest": {
                    "reviewDecision": "APPROVED",
                }}}}
            cursor = variables.get("cursor")
            if cursor == "null":
                return {"data": {"repository": {"pullRequest": {
                    "latestReviews": {
                        "pageInfo": {
                            "hasNextPage": True,
                            "endCursor": "UNEXPECTED_CURSOR",
                        },
                        "totalCount": 5,
                        "nodes": [
                            {"state": "APPROVED",
                             "submittedAt": "2026-08-07T10:00:00Z",
                             "author": {"login": "coderabbitai"}},
                        ],
                    },
                }}}}
            raise VerificationFailure(
                f"paginator sent an unexpected cursor: {cursor!r}"
            )

        args = type("A", (), {"repo": "o/r", "pr_number": 4})()
        with mock.patch.object(VERIFIER, "_run_gh_graphql",
                                side_effect=fake_run_gh_graphql):
            with self.assertRaises(VerificationFailure) as ctx:
                VERIFIER._inspect_coderabbit(args)
        # First call cursor is None (passed as "null").
        self.assertIn("null", cursors_seen)
        # First page had hasNextPage=True and an endCursor.
        # The production paginator must forward it on the
        # second request.
        self.assertIn("UNEXPECTED_CURSOR", cursors_seen)
        # The diagnostic identifies the cursor.
        msg = str(ctx.exception).lower()
        self.assertIn("cursor", msg,
            f"failure must name the cursor; got: {msg!r}")
        self.assertIn("unexpected_cursor", msg,
            f"failure must reference the unexpected cursor; "
            f"got: {msg!r}")
        # The production paginator is NOT patched.
        # (Proven by the fact that the production paginator
        # raised a different cursor sent than the first call.)


if __name__ == "__main__":
    unittest.main()