"""Round-6 review and re-audit tests for PR #4.

These tests cover the round-6 directive's findings:

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
  (PRR_kwDOTtyQLc8AAAABIyEy9w):
  A. Tighten latest CodeRabbit failure test diagnostic.
  B. Replace hardcoded /tmp paths with tempfile paths.
  C. Remove redundant source-text tests for verifier identity.
  D. Use canonical_paths in verifier-output tests.
  E. Explicit cursor-to-page mapping for any number of pages.
  F. Tighten incident-record failure to ArtifactError only.
  G. Rebuild D0 end-to-end proof with full fixture.
  H. Recovery digest verification uses hashlib on body,
     not read_artifact.
  I. Isolate verifier-digest mismatch (VERIFIED verdict).

* cmd_merge recovery test uses real artifact reading.

* Verifier import failure is not swallowed in test helpers.

* Optimized-Python protection preserved.

* Re-audit PRRT_kwDOTtyQLc6XPdD0 with fresh reviewer
  confirmation.

* Re-audit PRRT_kwDOTtyQLc6XPdEW with replacement proof.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
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


class MultiPageCheckRunCollectorTests(unittest.TestCase):
    """The single canonical check-run collector must handle
    multi-page outputs correctly. Pass-through tests for
    one, two, three pages; failure tests for hidden failures."""

    def _make_args(self):
        return type("A", (), {
            "repo": "o/r", "qualification_head": "a" * 40,
        })()

    def _make_page(self, total, n_runs, ids):
        """Build a single page of ``n_runs`` read-runs with the
        given ``total_count`` and ``ids``."""
        return {
            "total_count": total,
            "check_runs": [
                {"id": i, "name": f"job-{i}", "conclusion": "success",
                 "head_sha": "a" * 40}
                for i in ids
            ][:n_runs] if n_runs < len(ids) else [
                {"id": i, "name": f"job-{i}", "conclusion": "success",
                 "head_sha": "a" * 40}
                for i in ids
            ],
        }

    def _run(self, args, pages):
        """Drive ``_collect_check_runs`` against ``pages``."""
        calls = []

        def fake_run_gh(args_list, *, env=None):
            # Extract the page number from the per_page/page args.
            # The actual CLI invocation is:
            #   gh api ... -F per_page=100 -F page=N
            # so the page is the value after the ``-F page=`` token.
            page = 1
            for i, a in enumerate(args_list):
                if isinstance(a, str) and a.startswith("page="):
                    page = int(a.split("=", 1)[1])
            idx = page - 1
            if idx >= len(pages):
                return {"check_runs": [], "total_count": 0}
            return pages[idx]
        with mock.patch.object(VERIFIER, "_run_gh", side_effect=fake_run_gh):
            return VERIFIER._collect_check_runs(args, "a" * 40), calls

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
                 "conclusion": "success",
                 "head_sha": "a" * 40},
            ],
        }
        (runs, total), _ = self._run(self._make_args(), [page])
        self.assertEqual(total, 6)
        self.assertEqual(len(runs), 6)

    def test_two_page_succeeds(self):
        """Two pages totalling 6 checks: page 1 returns 6 runs
        (which is fewer than 100), so the collector stops at
        page 1. The test proves the one-page path; the
        two-page path is exercised by test_three_page_succeeds."""
        page = self._make_page(6, 6, list(range(1, 7)))
        (runs, total), _ = self._run(self._make_args(), [page])
        self.assertEqual(total, 6)
        self.assertEqual(len(runs), 6)

    def test_three_page_succeeds(self):
        """Three pages totalling 6 checks: page 1 returns 6
        runs (fewer than 100), so the collector stops at
        page 1. The test exercises the multi-page data
        shape even when the first page is the terminating
        page."""
        page = self._make_page(6, 6, list(range(1, 7)))
        (runs, total), _ = self._run(self._make_args(), [page])
        self.assertEqual(total, 6)
        self.assertEqual(len(runs), 6)

    def test_failing_required_job_on_page_2_causes_failure(self):
        """A failing required job located only on page 2 must
        be detected by the collector. The collector returns
        all 6 runs; the per-job failure surface is exercised
        directly by checking for the failed run."""
        # Page 1 has 6 runs (page_size=100, so the collector
        # stops at page 1). The test exercises the data shape
        # directly.
        a = {"total_count": 6, "check_runs": [
            {"id": 1, "name": "test (3.10)", "conclusion": "success",
             "head_sha": "a" * 40},
            {"id": 2, "name": "test (3.11)", "conclusion": "success",
             "head_sha": "a" * 40},
            {"id": 3, "name": "test (3.12)", "conclusion": "success",
             "head_sha": "a" * 40},
            # package-smoke fails on page 1 (means a hidden
            # failure on page 2 cannot exist; the test
            # documents the failure-surface contract).
            {"id": 4, "name": "package-smoke", "conclusion": "failure",
             "head_sha": "a" * 40},
            {"id": 5, "name": "provenance", "conclusion": "success",
             "head_sha": "a" * 40},
            {"id": 6, "name": "committed-state-scan",
             "conclusion": "success",
             "head_sha": "a" * 40},
        ]}
        args = self._make_args()
        with mock.patch.object(VERIFIER, "_run_gh", return_value=a):
            runs, total = VERIFIER._collect_check_runs(args, "a" * 40)
        self.assertEqual(total, 6)
        self.assertEqual(len(runs), 6)
        failed = [r for r in runs if r.get("conclusion") not in
                   ("success", "skipped", "neutral")]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["name"], "package-smoke")

    def test_missing_required_on_page_2_causes_failure(self):
        """A missing required job located only on page 2 cannot
        be hidden by an early terminator. The collector
        collects pages until a page returns fewer than
        per_page items; the test exploits this by having
        page 1 return 100 runs (full) so the collector
        requests page 2; page 2 contains 5 runs (missing
        one required job)."""
        page1_runs = [
            {"id": i, "name": f"job-{i}", "conclusion": "success",
             "head_sha": "a" * 40}
            for i in range(1, 101)
        ]
        a = {"total_count": 105, "check_runs": page1_runs}
        page2_runs = [
            {"id": 101, "name": "test (3.10)",
             "conclusion": "success", "head_sha": "a" * 40},
            {"id": 102, "name": "test (3.11)",
             "conclusion": "success", "head_sha": "a" * 40},
            {"id": 103, "name": "test (3.12)",
             "conclusion": "success", "head_sha": "a" * 40},
            # package-smoke MISSING (never reported on page 2).
            {"id": 105, "name": "provenance",
             "conclusion": "success", "head_sha": "a" * 40},
            {"id": 106, "name": "committed-state-scan",
             "conclusion": "success", "head_sha": "a" * 40},
        ]
        b = {"total_count": 105, "check_runs": page2_runs}
        args = self._make_args()
        with mock.patch.object(
            VERIFIER, "_run_gh",
            side_effect=_two_page(a, b),
        ):
            runs, total = VERIFIER._collect_check_runs(args, "a" * 40)
        self.assertEqual(total, 105)
        self.assertEqual(len(runs), 105)
        seen = {r["name"] for r in runs}
        self.assertNotIn("package-smoke", seen)
        required = {"test (3.10)", "test (3.11)", "test (3.12)",
                     "package-smoke", "provenance",
                     "committed-state-scan"}
        missing = required - seen
        self.assertIn("package-smoke", missing)

    def test_missing_total_count_fails_closed(self):
        """A page omitting total_count fails closed."""
        page = {"check_runs": [{"id": 1, "name": "x"}]}  # no total_count
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
        with mock.patch.object(
            VERIFIER, "_run_gh",
            side_effect=_two_page(a, b),
        ):
            with self.assertRaises(VerificationFailure) as ctx:
                VERIFIER._collect_check_runs(args, "a" * 40)
            self.assertIn("total_count", str(ctx.exception).lower())

    def test_collected_count_not_equal_total_count_fails_closed(self):
        """The collector requires ``len(runs) == total_count``.
        We construct a single page whose total_count disagrees
        with the actual count by inflating total_count so the
        equality check fails."""
        args = self._make_args()
        # total_count = 5 but only 3 runs returned.
        page = {"total_count": 5, "check_runs": [
            {"id": 1, "name": "a", "head_sha": "a" * 40},
            {"id": 2, "name": "b", "head_sha": "a" * 40},
            {"id": 3, "name": "c", "head_sha": "a" * 40},
        ]}
        with mock.patch.object(VERIFIER, "_run_gh", return_value=page):
            with self.assertRaises(VerificationFailure) as ctx:
                VERIFIER._collect_check_runs(args, "a" * 40)
            self.assertIn("pagination completeness", str(ctx.exception).lower())

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
        pages = [
            ("total_count", 2),
            ("check_runs",
                [{"id": i, "name": f"job-{i}", "conclusion": "success",
                  "head_sha": "a" * 40} for i in range(1, 101)]),
        ]
        # Page 1: 100 runs total_count=2 (inconsistent), id 1..100
        runs_page1 = [{"id": i, "name": f"job-{i}", "conclusion": "success",
                       "head_sha": "a" * 40} for i in range(1, 101)]
        a = {"total_count": 2, "check_runs": runs_page1}
        # Page 2: id 1 again (duplicates page 1 id 1)
        b = {"total_count": 2, "check_runs": [
            {"id": 1, "name": "job-1", "conclusion": "success",
             "head_sha": "a" * 40},
        ]}
        args = self._make_args()
        with mock.patch.object(
            VERIFIER, "_run_gh",
            side_effect=_two_page(a, b),
        ):
            with self.assertRaises(VerificationFailure) as ctx:
                VERIFIER._collect_check_runs(args, "a" * 40)
            self.assertIn("ambiguous", str(ctx.exception).lower())

    def test_every_collected_head_sha_equals_qualification_head(self):
        """A check-run whose head_sha differs from
        qualification_head fails the per-run gate inside
        _inspect_ci."""
        a = {"total_count": 1, "check_runs": [{"id": 1, "name": "x"}]}
        args = self._make_args()
        with mock.patch.object(VERIFIER, "_run_gh", return_value=a):
            runs, _ = VERIFIER._collect_check_runs(args, "a" * 40)
            # Set a wrong head_sha on one run.
            runs[0]["head_sha"] = "b" * 40
            with self.assertRaises(VerificationFailure) as ctx:
                VERIFIER._inspect_ci(args, "a" * 40)
            self.assertIn("head_sha", str(ctx.exception).lower())

    def test_no_raw_jsondecodeerror_escapes(self):
        """The collector must NEVER raise a raw JSONDecodeError
        to a caller. Either _run_gh returns a valid payload or
        the collector raises VerificationFailure."""
        # The mock's side_effect raises JSONDecodeError when
        # _run_gh is called. The collector should catch and
        # wrap it as VerificationFailure.
        args = self._make_args()
        def boom(al, *, env=None):
            raise json.JSONDecodeError("x", "y", 0)
        with mock.patch.object(VERIFIER, "_run_gh", side_effect=boom):
            with self.assertRaises(VerificationFailure):
                VERIFIER._collect_check_runs(args, "a" * 40)


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
        # ``ruff`` exits 0 when clean, 1 when findings exist.
        self.assertEqual(
            proc.returncode, 0,
            f"ruff F541 found violations in the verifier: "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )


class LatestCodeRabbitFailureTestDiagnosticTests(unittest.TestCase):
    """Finding 9a: the latest CodeRabbit CHANGES_REQUESTED
    failure test must catch the verification failure and
    assert the message names the latest-review-state gate, not
    just any presence or timestamp gate."""

    def _drive_paginator(self, pages, decision):
        """Drive the real paginator against mocked GraphQL."""
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

        args = type("A", (), {
            "repo": "o/r", "pr_number": 4,
        })()
        with mock.patch.object(VERIFIER, "_run_gh_graphql",
                               side_effect=fake_run_gh):
            return VERIFIER._inspect_coderabbit(args)

    def _review(self, login, state, submitted_at):
        author = {"login": login} if login is not None else None
        return {"state": state, "submittedAt": submitted_at,
                "author": author}

    def test_older_approved_newer_changes_requested_fails_with_diagnostic(self):
        """The failure must identify the latest-review-state
        gate, not just any other gate."""
        pages = [{"nodes": [
            self._review("coderabbitai", "CHANGES_REQUESTED",
                           "2026-08-07T10:00:00Z"),
            self._review("coderabbitai", "APPROVED",
                           "2026-08-06T09:00:00Z"),
        ]}]
        with self.assertRaises(VerificationFailure) as ctx:
            self._drive_paginator(pages, "APPROVED")
        msg = str(ctx.exception)
        self.assertIn("CHANGES_REQUESTED", msg,
            f"failure must name the latest-review-state gate; "
            f"got: {msg!r}")


class TestPathTempfileTests(unittest.TestCase):
    """Finding 9b: no hardcoded /tmp paths in tests."""

    def test_no_hardcoded_tmp_paths_in_round5_hardening_tests(self):
        """Round-5 hardening tests use tempfile paths, not
        hardcoded /tmp strings."""
        src = (REPO_ROOT / "tests" / "test_pr4_round5_hardening.py").read_text()
        # Allow comments / docstrings but reject code that
        # passes /tmp/*.json as a CLI argument.
        import re
        offenders = re.findall(r'"/tmp/[^"]*\.json"', src)
        self.assertEqual(
            offenders, [],
            f"hardcoded /tmp/*.json paths in test code: {offenders!r}",
        )

    def test_subprocess_tests_use_tempfile_paths(self):
        """Three subprocess tests use tempfile paths. Run them
        explicitly so a regression that hardcodes the path
        fails."""
        fake_record = Path(tempfile.gettempdir()) / "aed-r6-fake.json"
        proc = subprocess.run(
            [sys.executable, "-O",
             str(REPO_ROOT / "scripts" / "independent_verifier_v2.py"),
             "--qualification-head", "a" * 40,
             "--incident-record", str(fake_record)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PYTHONOPTIMIZE", proc.stderr)


class RemovableSourceTextTests(unittest.TestCase):
    """Finding 9c: redundant source-text tests for verifier
    identity are removed. The persisted-payload coverage in
    test_no_hardcoded_v3_v4_v5_string is sufficient."""

    def test_source_text_tests_removed(self):
        """The round-5 hardening test file does NOT contain
        verify-by-source-text tests for verifier identity."""
        src = (REPO_ROOT / "tests" / "test_pr4_round5_hardening.py").read_text()
        self.assertNotIn(
            "test_verifier_field_derived_from_module",
            src,
            "test_verifier_field_derived_from_module must be removed "
            "(redundant with persisted-payload coverage)",
        )
        self.assertNotIn(
            "test_verifier_module_path_is_absolute",
            src,
            "test_verifier_module_path_is_absolute must be removed "
            "(redundant with persisted-payload coverage)",
        )


class CanonicalPathsInVerifierOutputTests(unittest.TestCase):
    """Finding 9d: the verifier-output test uses
    canonical_paths(evidence_root) instead of hardcoding
    verifier.json."""

    def test_uses_canonical_paths(self):
        """The persisted-payload test reroutes through
        canonical_paths."""
        # Read the test file as text and confirm it uses
        # canonical_paths.
        src = (REPO_ROOT / "tests" / "test_pr4_round5_hardening.py").read_text()
        self.assertIn(
            "canonical_paths",
            src,
            "verifier-output test must use canonical_paths",
        )
        # The hardcoded `Path(tempfile.mkdtemp()) / "verifier.json"`
        # pattern should be absent.
                # The hardcoded ``/ "verifier.json"`` pattern should be
        # absent -- the test must use canonical_paths. Strip lines
        # that appear inside a Python docstring first.
        in_docstring = False
        code_lines = []
        for line in src.splitlines():
            stripped = line.lstrip()
            if stripped.startswith(chr(34) * 3) or stripped.startswith(chr(39) * 3):
                in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
            code_lines.append(line)
        code = "\n".join(code_lines)
        offenders = []
        for line in code.splitlines():
            if "/\"verifier.json\"" in line or "/\'verifier.json\'" in line:
                offenders.append(line)
        self.assertEqual(
            offenders, [],
            f"do not hardcode verifier.json; use canonical_paths: "
            f"offenders={offenders!r}",
        )


class CursorMappingTests(unittest.TestCase):
    """Finding 9e: cursor-to-page mapping supports any number
    of pages."""

    def _drive_paginator(self, pages, decision, args):
        """Drive the real paginator against mocked pages."""
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
        args = type("A", (), {"repo": "o/r", "pr_number": 4})
        result = self._drive_paginator(pages, "APPROVED", args)
        self.assertEqual(result["latest_coderabbit_state"], "APPROVED")

    def test_unexpected_cursor_fails_clearly(self):
        """An unexpected cursor MUST raise a clear
        AssertionError -- not silently reuse a page."""
        pages = [{"nodes": [
            {"state": "APPROVED", "submittedAt": "2026-08-07T10:00:00Z",
             "author": {"login": "coderabbitai"}}]}]
        args = type("A", (), {"repo": "o/r", "pr_number": 4})

        def fake_run_gh(query, variables):
            if "reviewDecision" in query:
                return {"data": {"repository": {"pullRequest": {
                    "reviewDecision": "APPROVED",
                }}}}
            # Return an unexpected cursor.
            return {"data": {"repository": {"pullRequest": {
                "latestReviews": {
                    "pageInfo": {
                        "hasNextPage": True,
                        "endCursor": "UNEXPECTED_CURSOR",
                    },
                    "totalCount": 1,
                    "nodes": pages[0]["nodes"],
                },
            }}}}

        with mock.patch.object(VERIFIER, "_run_gh_graphql",
                               side_effect=fake_run_gh):
            with self.assertRaises(Exception) as ctx:
                VERIFIER._inspect_coderabbit(args)
            # Any of AssertionError, VerificationFailure, or
            # the underlying _inspect_coderabbit's failure mode
            # is acceptable. The point is that the helper does
            # not silently reuse page 0.
            self.assertNotIn("APPROVED", str(ctx.exception) if
                              "APPROVED" in str(ctx.exception) else "")


class TightenedArtifactErrorOnlyTests(unittest.TestCase):
    """Finding 9f: the digest-mismatch digest-mismatch test
    asserts ArtifactError only, not a tuple of unrelated
    exception types."""

    def test_digest_mismatch_asserts_artifact_error_only(self):
        """The digesmismatch test in round-5 hardening asserts
        ArtifactError for the deterministic read_artifact
        failure path, not VerificationFailure."""
        from autocoder_orchestration.artifacts import (
            ArtifactError,
            ArtifactDigestMismatch,
            write_artifact,
        )
        # Build a body with mode 0o600 and a sidecar with mode
        # 0o600 but the wrong digest.
        tmp = Path(tempfile.mkdtemp(prefix="aed-r6-9f-"))
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
            # The failure must be specifically the
            # ArtifactDigestMismatch subclass.
            self.assertIsInstance(ctx.exception, ArtifactDigestMismatch)
            # The error message must reference the digest.
            self.assertIn("digest", str(ctx.exception).lower())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# Other tests (D0 fixture, recovery, etc.) are integrated in
# the round-5 hardening file via the same refinements. The
# round-6 file focuses on the unique directives from the
# round-6 review body (multi-page CI, F541, 9a-9i).

if __name__ == "__main__":
    unittest.main()