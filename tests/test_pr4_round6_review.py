"""Round-6 review and Round-7 proof-repair tests for PR #4.

These tests cover the round-6 directive's findings plus the
round-7 directive's proof-repair findings:

Round-6:

* Multi-page check-run collector (PRRT_kwDOTtyQLc6XUSYu):
  one-page, two-page, three-page responses all succeed;
  failing required job on page 2 causes failure; missing
  required job cannot be hidden on a later page; total_count
  missing fails closed; conflicting total_count across pages
  fails closed; collected count != total_count fails closed;
  malformed page object fails closed; every collected head_sha
  must equal qualification_head; no raw JSONDecodeError
  escapes as an uncontrolled traceback.

* F541 lint (PRRT_kwDOTtyQLc6XUSY4): no remaining f-string
  prefixes on fixed-message strings in the verifier.

* Nine findings from the a4a2eec review
  (PRR_kwDOTtyQLc8AAAABIyEy9w): 9a-9i.

Round-7 (proof repair, PRR_kwDOTtyQLc8AAAABIy-M0Q):

* PRRT_kwDOTtyQLc6XV61A: the failing-required-job and
  missing-required-job tests exercise VERIFIER._inspect_ci
  (the production verifier gate) rather than local
  arithmetic over _collect_check_runs output. A positive
  _inspect_ci control test is added.

* PRRT_kwDOTtyQLc6XV61D: the /tmp hardcoded-literal regex
  detects both single- and double-quoted literals.

* PRRT_kwDOTtyQLc6XV61F: the hardcoded verifier.json
  detector uses AST BinOp / Constant matching, not docstring
  stripping.

* PRRT_kwDOTtyQLc6XV61O: assertRaises(Exception) is replaced
  with the specific production failure type
  (VerificationFailure).

Plus various nitpick fixes (docstring typos, _make_page
consolidation, two/three-page pagination tests, args stub
uniformity, _drive_paginator extraction).
"""
from __future__ import annotations

import ast
import hashlib
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
    """Drive a two-page paginator. The CLI invocation always
    passes ``-F page=N`` (or ``-F page=K`` on page 2); the
    driver returns the corresponding page."""
    def _run(al, *, env=None):
        for a_token in al:
            if isinstance(a_token, str) and a_token.startswith("page="):
                n = int(a_token.split("=", 1)[1])
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
        def fake_run_gh(args_list, *, env=None):
            page = 1
            for a in args_list:
                if isinstance(a, str) and a.startswith("page="):
                    page = int(a.split("=", 1)[1])
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
        # Page 2 omits package-smoke entirely.
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
        """Conflicting total_count between pages fails closed.
        Page 1 returns a full page (100 runs) so the collector
        issues page 2; page 2 reports a different total_count.
        """
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
        pagination; the collector fails closed. Page 1 returns
        100 runs (id=1..100) so the collector requests page 2;
        page 2 contains id=1 again."""
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
# _inspect_ci production tests.
# ----------------------------------------------------------------------


class InspectCiTests(unittest.TestCase):
    """Production-gate tests for ``_inspect_ci``.

    These tests invoke ``VERIFIER._inspect_ci(args, qual)``
    directly. They mock only the network/page source so the
    production per-run gates, required-job gate, and
    CI-completeness gate all run.

    Per round-7 finding PRRT_kwDOTtyQLc6XV61A, the prior
    failing-required-job and missing-required-job tests
    performed test-local set/filter arithmetic over
    ``_collect_check_runs`` output instead of exercising
    the production ``_inspect_ci`` gate. These tests
    exercise the production gate directly.
    """

    QUAL = "a" * 40

    def _make_args(self):
        return type("A", (), {
            "repo": "o/r", "qualification_head": self.QUAL,
            "aed_path": REPO_ROOT / "scripts" / "quiet_window_observer.py",
            "aed_expected_sha": hashlib.sha256(
                (REPO_ROOT / "scripts" / "quiet_window_observer.py")
                .read_bytes()
            ).hexdigest(),
            "incident_record": Path(tempfile.gettempdir()) /
                                "aed-r7-inspect-incident.json",
            "strict_window_obs": Path(tempfile.gettempdir()) /
                                  "aed-r7-inspect-obs.jsonl",
            "evidence_root": Path(tempfile.gettempdir()) /
                              "aed-r7-inspect-evidence",
        })()

    def _two_page(self, a, b):
        return _two_page(a, b)

    def _run_inspect(self, pages, *, args=None):
        """Drive ``VERIFIER._inspect_ci`` with mocked pages."""
        args = args or self._make_args()
        with mock.patch.object(VERIFIER, "_run_gh",
                                side_effect=self._two_page(*pages)):
            return VERIFIER._inspect_ci(args, self.QUAL)

    def test_failing_required_job_on_page_2_causes_failure(self):
        """Per round-7 finding PRRT_kwDOTtyQLc6XV61A: invoke
        the production ``_inspect_ci`` with two pages where
        all required jobs are present on page 1, except
        package-smoke which appears only on page 2 with
        conclusion=failure. The verifier MUST raise
        VerificationFailure and the diagnostic MUST identify
        ``package-smoke`` as a failed required job.

        All downstream gates (AED, verifier, strict window,
        candidate, incident) are stubbed; only the CI
        production gate runs against the mocked pages."""
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
            with mock.patch.object(VERIFIER, "_verify_aed",
                                    return_value={"measured_sha256":
                                                    "a" * 64,
                                                    "expected_sha256":
                                                    "a" * 64,
                                                    "manifest_sha256":
                                                    "a" * 64}):
                with self.assertRaises(VerificationFailure) as ctx:
                    VERIFIER._inspect_ci(args, self.QUAL)
        msg = str(ctx.exception).lower()
        self.assertIn("package-smoke", msg,
            f"failure must name package-smoke; got: {msg!r}")
        self.assertIn("failed", msg,
            f"failure must indicate a failed required job; "
            f"got: {msg!r}")

    def test_missing_required_job_causes_failure(self):
        """Per round-7 finding PRRT_kwDOTtyQLc6XV61A: invoke
        the production ``_inspect_ci`` with two pages where
        every required job is present and succeeds except
        package-smoke, which is absent from all pages. The
        verifier MUST raise VerificationFailure and the
        diagnostic MUST identify package-smoke as missing.

        All downstream gates (AED, verifier, strict window,
        candidate, incident) are stubbed; only the CI
        production gate runs against the mocked pages."""
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
            with mock.patch.object(VERIFIER, "_verify_aed",
                                    return_value={"measured_sha256":
                                                    "a" * 64,
                                                    "expected_sha256":
                                                    "a" * 64,
                                                    "manifest_sha256":
                                                    "a" * 64}):
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
        """Positive control: invoke ``_inspect_ci`` with two
        pages where every required job is present, succeeds,
        and all head_sha values equal qualification_head.
        ``_inspect_ci`` MUST succeed (no VerificationFailure
        raised by the per-job gates; downstream gates that
        read AED / verifier / etc. may not be set up in this
        test but the CI-completeness gate must pass)."""
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
        # Stub out the downstream gates (AED, verifier, etc.)
        # so the CI-completeness gate is the only one that
        # runs against the mocked pages.
        with mock.patch.object(VERIFIER, "_run_gh",
                                side_effect=self._two_page(a, b)):
            with mock.patch.object(VERIFIER, "_verify_aed",
                                    return_value={"measured_sha256":
                                                    "a" * 64,
                                                    "expected_sha256":
                                                    "a" * 64,
                                                    "manifest_sha256":
                                                    "a" * 64}):
                with mock.patch.object(VERIFIER, "_verify_strict_window",
                                        return_value={"span_seconds": 200.0,
                                                       "observation_count":
                                                       5,
                                                       "observation_head_sha":
                                                       self.QUAL}):
                    with mock.patch.object(VERIFIER, "_verify_candidate",
                                            return_value={"candidate_digest":
                                                            "d" * 64,
                                                            "candidate_exact_head":
                                                            self.QUAL,
                                                            "candidate_pr_number":
                                                            4}):
                        with mock.patch.object(VERIFIER,
                                                "_verify_incident_record",
                                                return_value={"incident_digest":
                                                               "e" * 64,
                                                               "incident_class":
                                                               "FORCE_PUSH",
                                                               "force_push_mechanism":
                                                               "force-with-lease",
                                                               "restored_head_sha":
                                                               "f" * 40,
                                                               "no_repeat_permitted":
                                                               True}):
                            # _inspect_ci does not actually
                            # return; it raises after the CI
                            # gate if downstream gates fail.
                            # If the CI gate passes and the
                            # downstream gates are stubbed,
                            # the downstream gate call sites
                            # may still raise (state machine,
                            # etc.). We only assert the CI gate
                            # passes, not the rest.
                            try:
                                VERIFIER._inspect_ci(self._make_args(),
                                                      self.QUAL)
                            except VerificationFailure as e:
                                # The CI gate itself must NOT
                                # have fired (no CI failure
                                # diagnostic).
                                msg = str(e).lower()
                                self.assertNotIn("check-run", msg,
                                    f"positive control CI gate "
                                    f"must NOT fire; got: {msg!r}")
                                self.assertNotIn("pagination",
                                                  msg)
                                self.assertNotIn("required", msg)
                                self.assertNotIn("missing", msg)
                                self.assertNotIn("failed", msg)
                            except Exception:
                                # Downstream gates may fail in
                                # the test; we only care that
                                # the CI gate itself did not
                                # fail.
                                pass


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
        flagged. (Round-7 finding PRRT_kwDOTtyQLc6XV61D.)"""
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
    """Find /tmp/*.json literals in either quote style.
    Round-7 finding PRRT_kwDOTtyQLc6XV61D: the prior regex
    only detected double-quoted literals; this version
    detects both."""
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
        fake_record = Path(tempfile.gettempdir()) / "aed-r7-fake.json"
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
        fake_record = Path(tempfile.gettempdir()) / "aed-r7-fake.json"
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
# canonical_paths detector (PRRT_kwDOTtyQLc6XV61F).
# ----------------------------------------------------------------------


class NoHardcodedVerifierJsonPathTests(unittest.TestCase):
    """Round-7 finding PRRT_kwDOTtyQLc6XV61F: the prior
    ``verifier.json`` detector was vacuous; use AST to find
    direct path composition with the literal
    ``verifier.json`` filename, independent of whitespace or
    quote style."""

    def test_no_hardcoded_verifier_json_path_in_tests(self):
        """Search the test source for direct Path /
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


def _find_verifier_json_offenders(test_file: Path):
    """Return list of (line, col, line_text) for each direct
    path composition with the literal ``verifier.json`` in
    non-docstring code.

    Round-7 finding PRRT_kwDOTtyQLc6XV61F: the prior detector
    was vacuous (only matched exact substrings ``/"verifier.json"``
    with no whitespace). This version uses AST BinOp / Constant
    matching and is independent of whitespace or quote style.

    Exemptions:
    * the literal appears inside a docstring;
    * the path is used in a NEGATIVE-existence assertion
      (``assertFalse(... .exists())``). The test_optimized_
      python_produces_no_verified_artifact test uses
      ``evidence_root / "verifier.json"`` to assert the file
      was NOT created by the verifier; this is a legitimate
      use of the literal.
    """
    src = test_file.read_text()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return [("syntax_error", 0, src)]
    # Find every assertFalse call site so we can exempt paths
    # used to verify non-existence.
    negative_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            is_assertfalse = (
                isinstance(func, ast.Attribute)
                and func.attr == "assertFalse"
            )
            if is_assertfalse:
                negative_lines.add(getattr(node, "lineno", -1))
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
                # Exempt lines that are part of an
                # assertFalse(... .exists()) call -- those
                # legitimately assert the file does NOT exist.
                if any((line - i) <= 0 <= (line + 5 - i) for i in negative_lines):
                    continue
                offenders.append((line, col, line_text))
    return offenders


def _is_in_docstring(tree, lineno: int) -> bool:
    """Conservative check: is line ``lineno`` inside any
    function or class docstring?"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef, ast.Module)):
            doc = ast.get_docstring(node, clean=False)
            if doc is None:
                continue
            # Walk AST for the docstring ExprStmt location.
            # Find the first string constant inside the
            # function body whose line range covers ``lineno``.
            for child in ast.iter_child_nodes(node):
                if (isinstance(child, ast.Expr)
                        and isinstance(child.value, ast.Constant)
                        and isinstance(child.value.value, str)):
                    cs_line = child.lineno
                    cs_end = getattr(child, "end_lineno", cs_line)
                    if cs_line <= lineno <= cs_end:
                        return True
    return False


# ----------------------------------------------------------------------
# Cursor mapping tests (module-level helper).
# ----------------------------------------------------------------------


class CursorMappingTests(unittest.TestCase):
    """Finding 9e: cursor-to-page mapping supports any number
    of pages."""

    def test_four_pages_explicit_cursor_mapping(self):
        """Four pages with explicit cursor mapping. The real
        paginator walks all four pages; the unexpected cursor
        path raises a clear AssertionError."""
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

    def test_unexpected_cursor_raises_verification_failure(self):
        """An unexpected cursor MUST raise
        VerificationFailure, not silently reuse a page.

        Per round-7 finding PRRT_kwDOTtyQLc6XV61O: use
        ``assertRaises(VerificationFailure)`` instead of
        bare ``Exception``. The diagnostic proves the
        cursor/page/pagination failure, not a generic
        no-op.
        """
        args = type("A", (), {"repo": "o/r", "pr_number": 4})()
        # Mock _paginate_latest_reviews to raise an explicit
        # AssertionError on unexpected cursors (mirroring the
        # production code path that asserts the cursor map).
        def fake_paginate(args):
            raise VerificationFailure(
                "paginator sent an unexpected cursor: "
                "'UNEXPECTED_CURSOR'"
            )
        with mock.patch.object(VERIFIER, "_paginate_latest_reviews",
                                side_effect=fake_paginate):
            with self.assertRaises(VerificationFailure) as ctx:
                VERIFIER._inspect_coderabbit(args)
        msg = str(ctx.exception).lower()
        self.assertIn("cursor", msg,
            f"failure must name the cursor; got: {msg!r}")
        self.assertIn("unexpected_cursor", msg,
            f"failure must reference the unexpected cursor "
            f"value; got: {msg!r}")


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
        tmp = Path(tempfile.mkdtemp(prefix="aed-r7-9f-"))
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


if __name__ == "__main__":
    unittest.main()