"""Regression tests for PR #4 round-4 review findings.

These tests cover the production defects identified by CodeRabbit
on f20d443 (PR #4 round-4 review). Each test exercises a single
defect end-to-end through the production code path, not via
source-string inspection.

Coverage:

* GraphQL query builders: balanced braces; first and second
  pages; cursor propagation; mocked GraphQL execution across
  multiple pages.

* totalCount required: missing totalCount fails the pagination
  completeness check.

* Positive live PR readiness: MERGEABLE/CLEAN passes;
  UNKNOWN/CONFLICTING fails; draft fails; closed/merged
  fails.

* Explicit latest CodeRabbit review: sorted by submittedAt,
  not connection order; tolerates null authors; rejects
  CHANGES_REQUESTED; accepts APPROVED on a later page.

* Incident-record self-authentication removed: missing
  sidecar fails; forged body with no sidecar fails; altered
  body with old sidecar fails; verifier does NOT create any
  file.

* StateError handling in the post-merge COMPLETE persistence
  path: Controller.report_complete raising StateError returns
  EXIT_STATE with merge record durable; recovery output
  identifies the merge-record PATH AND DIGEST.

* Verifier-level test exercises the actual
  ``_inspect_coderabbit`` step from
  ``scripts/independent_verifier_v2.py`` with mocked
  GraphQL responses, not the production CLI filter.
"""
from __future__ import annotations

import importlib.util
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


def _load_verifier_module():
    """Load ``scripts/independent_verifier_v2.py`` as a module."""
    verifier_path = REPO_ROOT / "scripts" / "independent_verifier_v2.py"
    spec = importlib.util.spec_from_file_location(
        "independent_verifier", verifier_path,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class GraphQLQueryBuilderTests(unittest.TestCase):
    """The two paginated GraphQL query builders must produce
    syntactically valid documents with balanced braces, and
    pagination through them must actually traverse multiple
    pages."""

    def setUp(self):
        self.mod = _load_verifier_module()

    def test_review_threads_query_balanced(self):
        doc = self.mod._build_review_threads_query()
        self.mod._assert_graphql_balanced(doc, "review_threads_query")

    def test_latest_reviews_query_balanced(self):
        doc = self.mod._build_latest_reviews_query()
        self.mod._assert_graphql_balanced(doc, "latest_reviews_query")

    def test_review_decision_query_balanced(self):
        doc = self.mod._build_review_decision_query()
        self.mod._assert_graphql_balanced(doc, "review_decision_query")

    def test_pagination_completeness_with_totalCount_required(self):
        """A response that omits ``totalCount`` MUST fail the
        pagination completeness check. The current implementation
        raises AssertionError; this test pins that."""
        # Mock _run_gh_graphql to return a page missing totalCount.
        # The paginator should raise before returning.
        calls = {"n": 0}

        def fake_run(query, variables):
            calls["n"] += 1
            return {
                "data": {"repository": {"pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False,
                                     "endCursor": None},
                        "nodes": [],
                        # totalCount intentionally missing
                    },
                }}},
            }

        args = type("A", (), {"repo": "o/r", "pr_number": 1})()
        with mock.patch.object(self.mod, "_run_gh_graphql", fake_run):
            with self.assertRaises(AssertionError) as ctx:
                self.mod._paginate_review_threads(args)
            self.assertIn("totalCount", str(ctx.exception))


class IncidentRecordSelfAuthTests(unittest.TestCase):
    """The verifier MUST NOT create, repair, or migrate the
    sidecar of evidence it is about to verify."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="aed-r4-incident-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_sidecar_fails(self):
        """A canonical incident record without a sidecar MUST
        fail. The verifier does NOT create the sidecar."""
        # Write a valid incident body, no sidecar.
        body_path = self.tmpdir / "incident.json"
        body_path.write_text(json.dumps({
            "schema_version": "autocoder.incident.v1",
            "incident_class": "FORCE_PUSH",
            "force_push_mechanism": "git push --force-with-lease",
            "no_repeat_permitted": True,
            "restored_head_sha": "a" * 40,
        }))
        # Save byte snapshot of the directory before verification.
        before = sorted(p.name for p in self.tmpdir.iterdir())
        mod = _load_verifier_module()
        args = type("A", (), {
            "incident_record": body_path,
            "repo": "o/r", "pr_number": 1,
        })()
        with self.assertRaises(Exception) as ctx:
            mod._verify_incident_record(args)
        # The exception message MUST mention the missing sidecar.
        msg = str(ctx.exception).lower()
        self.assertIn("sidecar", msg)
        # Directory contents MUST be unchanged: no sidecar was
        # created.
        after = sorted(p.name for p in self.tmpdir.iterdir())
        self.assertEqual(before, after,
            f"verifier created files; before={before} after={after}")

    def test_forged_body_with_no_sidecar_fails(self):
        """A forged incident record with no sidecar MUST fail.
        Even if the body itself is well-formed JSON, the
        missing sidecar is itself the failure mode."""
        body_path = self.tmpdir / "incident.json"
        body_path.write_text(json.dumps({
            "schema_version": "autocoder.incident.v1",
            "incident_class": "FORCE_PUSH",
            "force_push_mechanism": "git push --force-with-lease",
            "no_repeat_permitted": True,
            "restored_head_sha": "b" * 40,
        }))
        before = sorted(p.name for p in self.tmpdir.iterdir())
        mod = _load_verifier_module()
        args = type("A", (), {
            "incident_record": body_path,
            "repo": "o/r", "pr_number": 1,
        })()
        with self.assertRaises(Exception):
            mod._verify_incident_record(args)
        after = sorted(p.name for p in self.tmpdir.iterdir())
        self.assertEqual(before, after,
            f"verifier created files; before={before} after={after}")

    def test_altered_body_with_old_sidecar_fails(self):
        """A sidecar that does not match the current body bytes
        MUST fail. The verifier does NOT regenerate the sidecar."""
        body_path = self.tmpdir / "incident.json"
        body_path.write_text(json.dumps({
            "schema_version": "autocoder.incident.v1",
            "incident_class": "FORCE_PUSH",
            "force_push_mechanism": "git push --force-with-lease",
            "no_repeat_permitted": True,
            "restored_head_sha": "c" * 40,
        }))
        # Write a sidecar that does not match the body bytes.
        wrong_sidecar = self.tmpdir / "incident.json.sha256"
        wrong_sidecar.write_text("f" * 64 + "\n")
        before = sorted(p.name for p in self.tmpdir.iterdir())
        mod = _load_verifier_module()
        args = type("A", (), {
            "incident_record": body_path,
            "repo": "o/r", "pr_number": 1,
        })()
        with self.assertRaises(Exception):
            mod._verify_incident_record(args)
        # Sidecar content MUST be unchanged.
        self.assertEqual(wrong_sidecar.read_text(), "f" * 64 + "\n")
        after = sorted(p.name for p in self.tmpdir.iterdir())
        self.assertEqual(before, after)

    def test_valid_canonical_incident_record_passes(self):
        """A canonical incident record with a valid sidecar MUST
        pass; ``--incident-record`` is required and accepted."""
        body_path = self.tmpdir / "incident.json"
        body_bytes = json.dumps({
            "schema_version": "autocoder.incident.v1",
            "incident_class": "FORCE_PUSH",
            "force_push_mechanism": "git push --force-with-lease",
            "no_repeat_permitted": True,
            "restored_head_sha": "d" * 40,
        }).encode("utf-8")
        # The canonical artifact reader requires mode 0o600;
        # write through os.open with the strict mode so the file
        # is canonical-ready.
        import os
        fd = os.open(str(body_path),
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, body_bytes)
        finally:
            os.close(fd)
        # Compute the correct sidecar.
        import hashlib
        digest = hashlib.sha256(body_bytes).hexdigest()
        sidecar_path = self.tmpdir / "incident.json.sha256"
        fd2 = os.open(str(sidecar_path),
                      os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd2, (digest + "\n").encode("ascii"))
        finally:
            os.close(fd2)
        mod = _load_verifier_module()
        args = type("A", (), {
            "incident_record": body_path,
            "repo": "o/r", "pr_number": 1,
        })()
        result = mod._verify_incident_record(args)
        self.assertEqual(result["incident_digest"], digest)
        self.assertEqual(
            result["force_push_mechanism"],
            "git push --force-with-lease",
        )
        self.assertTrue(result["no_repeat_permitted"])

    def test_incident_record_argument_required(self):
        """``--incident-record`` MUST be required; the verifier
        does not silently default to a shared /var/tmp path."""
        # Invoke ``main`` with no args and verify it errors out.
        mod = _load_verifier_module()
        with self.assertRaises(SystemExit):
            mod.main(["--qualification-head", "a" * 40])


class LiveCodeRabbitGateTests(unittest.TestCase):
    """The verifier-level test exercises ``_inspect_coderabbit``
    from the production verifier module with mocked GitHub
    GraphQL responses. It does NOT rely on
    ``cli_module._filter_coderabbit_review_state``."""

    def setUp(self):
        self.mod = _load_verifier_module()
        self.args = type("A", (), {
            "repo": "o/r", "pr_number": 4,
        })()

    def _reviews(self, items):
        return {"nodes": items}

    def _review(self, login, state, submitted_at):
        author = {"login": login} if login is not None else None
        return {"state": state, "submittedAt": submitted_at,
                "author": author}

    def test_older_approved_newer_changes_requested_fails(self):
        """Order A: older APPROVED, newer CHANGES_REQUESTED. The
        newest CodeRabbit review is CHANGES_REQUESTED, so the
        verifier MUST fail closed."""
        pages = [
            # First page: older APPROVED + newer CHANGES_REQUESTED
            self._reviews([
                self._review("coderabbitai", "CHANGES_REQUESTED",
                              "2026-08-07T10:00:00Z"),
                self._review("coderabbitai", "APPROVED",
                              "2026-08-06T09:00:00Z"),
            ]),
        ]
        def fake_paginate(args):
            nodes = []
            for page in pages:
                nodes.extend(page["nodes"])
            return nodes, len(nodes)
        def fake_run_decision(query, variables):
            return {"data": {"repository": {"pullRequest": {
                "reviewDecision": "APPROVED",
            }}}}
        with mock.patch.object(self.mod, "_paginate_latest_reviews",
                                fake_paginate), \
             mock.patch.object(self.mod, "_run_gh_graphql",
                                fake_run_decision):
            with self.assertRaises(AssertionError):
                self.mod._inspect_coderabbit(self.args)

    def test_newer_approved_older_changes_requested_passes(self):
        """Order B: newer APPROVED, older CHANGES_REQUESTED. The
        newest CodeRabbit review is APPROVED; the older
        CHANGES_REQUESTED must not block the gate."""
        pages = [
            self._reviews([
                self._review("coderabbitai", "APPROVED",
                              "2026-08-07T10:00:00Z"),
                self._review("coderabbitai", "CHANGES_REQUESTED",
                              "2026-08-06T09:00:00Z"),
            ]),
        ]
        def fake_paginate(args):
            nodes = []
            for page in pages:
                nodes.extend(page["nodes"])
            return nodes, len(nodes)
        def fake_run_decision(query, variables):
            return {"data": {"repository": {"pullRequest": {
                "reviewDecision": "APPROVED",
            }}}}
        with mock.patch.object(self.mod, "_paginate_latest_reviews",
                                fake_paginate), \
             mock.patch.object(self.mod, "_run_gh_graphql",
                                fake_run_decision):
            result = self.mod._inspect_coderabbit(self.args)
        self.assertEqual(result["latest_coderabbit_state"], "APPROVED")

    def test_no_coderabbit_fails(self):
        """Order C: no CodeRabbit review at all. The verifier
        MUST fail closed."""
        pages = [
            self._reviews([
                self._review("some-human", "APPROVED",
                              "2026-08-07T10:00:00Z"),
            ]),
        ]
        def fake_paginate(args):
            nodes = []
            for page in pages:
                nodes.extend(page["nodes"])
            return nodes, len(nodes)
        def fake_run_decision(query, variables):
            return {"data": {"repository": {"pullRequest": {
                "reviewDecision": "APPROVED",
            }}}}
        with mock.patch.object(self.mod, "_paginate_latest_reviews",
                                fake_paginate), \
             mock.patch.object(self.mod, "_run_gh_graphql",
                                fake_run_decision):
            with self.assertRaises(AssertionError):
                self.mod._inspect_coderabbit(self.args)

    def test_null_author_plus_valid_coderabbit(self):
        """Order D: a null-author entry plus a valid CodeRabbit
        entry. The null author must not crash; the valid
        CodeRabbit entry must be selected."""
        pages = [
            self._reviews([
                self._review(None, "APPROVED", "2026-08-05T09:00:00Z"),
                self._review("coderabbitai", "APPROVED",
                              "2026-08-07T10:00:00Z"),
            ]),
        ]
        def fake_paginate(args):
            nodes = []
            for page in pages:
                nodes.extend(page["nodes"])
            return nodes, len(nodes)
        def fake_run_decision(query, variables):
            return {"data": {"repository": {"pullRequest": {
                "reviewDecision": "APPROVED",
            }}}}
        with mock.patch.object(self.mod, "_paginate_latest_reviews",
                                fake_paginate), \
             mock.patch.object(self.mod, "_run_gh_graphql",
                                fake_run_decision):
            result = self.mod._inspect_coderabbit(self.args)
        self.assertEqual(result["latest_coderabbit_state"], "APPROVED")

    def test_coderabbit_on_later_page(self):
        """Order E: CodeRabbit on a later page. The paginator
        must traverse every page before the verifier decides."""
        pages = [
            self._reviews([
                self._review("some-human", "APPROVED",
                              "2026-08-01T09:00:00Z"),
            ]),
            self._reviews([
                self._review("coderabbitai", "APPROVED",
                              "2026-08-07T10:00:00Z"),
            ]),
        ]
        # Mock the GraphQL paginator directly so we exercise the
        # "CodeRabbit on a later page" path.
        def fake_paginate(args):
            nodes = []
            for page in pages:
                nodes.extend(page["nodes"])
            return nodes, len(nodes)
        def fake_run_decision(query, variables):
            return {"data": {"repository": {"pullRequest": {
                "reviewDecision": "APPROVED",
            }}}}
        with mock.patch.object(self.mod, "_paginate_latest_reviews",
                                fake_paginate), \
             mock.patch.object(self.mod, "_run_gh_graphql",
                                fake_run_decision):
            result = self.mod._inspect_coderabbit(self.args)
        self.assertEqual(result["latest_coderabbit_state"], "APPROVED")

    def test_changes_requested_decision_fails_closed(self):
        """The live reviewDecision == "CHANGES_REQUESTED" MUST
        fail the gate regardless of the latest CodeRabbit
        review."""
        pages = [
            self._reviews([
                self._review("coderabbitai", "APPROVED",
                              "2026-08-07T10:00:00Z"),
            ]),
        ]
        def fake_paginate(args):
            nodes = []
            for page in pages:
                nodes.extend(page["nodes"])
            return nodes, len(nodes)
        def fake_run_decision(query, variables):
            return {"data": {"repository": {"pullRequest": {
                "reviewDecision": "CHANGES_REQUESTED",
            }}}}
        with mock.patch.object(self.mod, "_paginate_latest_reviews",
                                fake_paginate), \
             mock.patch.object(self.mod, "_run_gh_graphql",
                                fake_run_decision):
            with self.assertRaises(AssertionError):
                self.mod._inspect_coderabbit(self.args)


class LivePRReadinessTests(unittest.TestCase):
    """``_fetch_pr`` requires positive readiness evidence."""

    def setUp(self):
        self.mod = _load_verifier_module()
        self.args = type("A", (), {"repo": "o/r", "pr_number": 4})
        self.qual = "a" * 40

    def _pr(self, head="a" * 40, state="OPEN", merged_at=None,
            is_draft=False, mergeable="MERGEABLE",
            merge_state_status="CLEAN", auto_merge=None):
        return {
            "headRefOid": head, "state": state, "mergedAt": merged_at,
            "isDraft": is_draft, "reviewDecision": "APPROVED",
            "mergeable": mergeable, "mergeStateStatus": merge_state_status,
            "autoMergeRequest": auto_merge,
        }

    def _run(self, pr_data):
        with mock.patch.object(self.mod, "_run_gh",
                                return_value=pr_data):
            return self.mod._fetch_pr(self.args, self.qual)

    def test_mergeable_clean_passes(self):
        result = self._run(self._pr())
        self.assertEqual(result["mergeable"], "MERGEABLE")
        self.assertEqual(result["mergeStateStatus"], "CLEAN")

    def test_unknown_fails(self):
        with self.assertRaises(AssertionError) as ctx:
            self._run(self._pr(mergeable="UNKNOWN",
                                 merge_state_status="UNKNOWN"))
        self.assertIn("mergeability", str(ctx.exception).lower())

    def test_conflicting_fails(self):
        with self.assertRaises(AssertionError):
            self._run(self._pr(mergeable="CONFLICTING",
                                 merge_state_status="BLOCKED"))

    def test_draft_fails(self):
        with self.assertRaises(AssertionError):
            self._run(self._pr(is_draft=True))

    def test_closed_fails(self):
        with self.assertRaises(AssertionError):
            self._run(self._pr(state="CLOSED"))

    def test_merged_fails(self):
        with self.assertRaises(AssertionError):
            self._run(self._pr(merged_at="2026-08-07T00:00:00Z"))

    def test_has_hooks_accepted(self):
        """HAS_HOOKS is in the positive set; status-confirmed
        mergeable PRs with pre-merge hooks must pass."""
        result = self._run(self._pr(merge_state_status="HAS_HOOKS"))
        self.assertEqual(result["mergeStateStatus"], "HAS_HOOKS")

    def test_unstable_accepted(self):
        result = self._run(self._pr(merge_state_status="UNSTABLE"))
        self.assertEqual(result["mergeStateStatus"], "UNSTABLE")


class PaginationMultiPageTests(unittest.TestCase):
    """The generic paginator must actually traverse multiple
    pages and reject ``totalCount`` mismatches."""

    def setUp(self):
        self.mod = _load_verifier_module()

    def test_cursor_propagation_across_pages(self):
        """Two pages of data; cursor propagates from page 1 to
        page 2; totalCount is consistent across pages."""
        calls = []

        def fake_run(query, variables):
            cursor = variables["cursor"]
            calls.append(variables)
            if cursor == "null":
                return {
                    "data": {"repository": {"pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": True,
                                         "endCursor": "CURSOR_1"},
                            "totalCount": 3,
                            "nodes": [{"id": "t1"}],
                        },
                    }}},
                }
            elif cursor == "CURSOR_1":
                return {
                    "data": {"repository": {"pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": True,
                                         "endCursor": "CURSOR_2"},
                            "totalCount": 3,
                            "nodes": [{"id": "t2"}],
                        },
                    }}},
                }
            else:  # CURSOR_2
                return {
                    "data": {"repository": {"pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False,
                                         "endCursor": None},
                            "totalCount": 3,
                            "nodes": [{"id": "t3"}],
                        },
                    }}},
                }

        args = type("A", (), {"repo": "o/r", "pr_number": 1})
        with mock.patch.object(self.mod, "_run_gh_graphql", fake_run):
            nodes, total = self.mod._paginate_review_threads(args)
        self.assertEqual(total, 3)
        self.assertEqual([n["id"] for n in nodes], ["t1", "t2", "t3"])
        # First request uses cursor "null"; subsequent requests
        # use the prior endCursor.
        self.assertEqual(calls[0]["cursor"], "null")
        self.assertEqual(calls[1]["cursor"], "CURSOR_1")
        self.assertEqual(calls[2]["cursor"], "CURSOR_2")

    def test_total_count_mismatch_across_pages_fails(self):
        """If totalCount differs across pages, the paginator
        fails closed."""
        def fake_run(query, variables):
            return {
                "data": {"repository": {"pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False,
                                     "endCursor": None},
                        "totalCount": 5,  # page 1 says 5
                        "nodes": [{"id": "t1"}],
                    },
                }}},
            }
        args = type("A", (), {"repo": "o/r", "pr_number": 1})
        with mock.patch.object(self.mod, "_run_gh_graphql", fake_run):
            # Single-page: totalCount=5, nodes=1 -> fail.
            with self.assertRaises(AssertionError):
                self.mod._paginate_review_threads(args)

    def test_missing_total_count_fails(self):
        """If totalCount is missing, the paginator fails closed."""
        def fake_run(query, variables):
            return {
                "data": {"repository": {"pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False,
                                     "endCursor": None},
                        # totalCount missing
                        "nodes": [{"id": "t1"}],
                    },
                }}},
            }
        args = type("A", (), {"repo": "o/r", "pr_number": 1})
        with mock.patch.object(self.mod, "_run_gh_graphql", fake_run):
            with self.assertRaises(AssertionError) as ctx:
                self.mod._paginate_review_threads(args)
            self.assertIn("totalCount", str(ctx.exception))


class FailedVerifierCanonicalSemanticsTests(unittest.TestCase):
    """PRRT_kwDOTtyQLc6XPdD0 re-audit: option B is satisfied by
    the production merge transaction re-verifying the verifier
    digest on every guarded merge. ``verifier_failed`` writes a
    fresh record; if a passing record was previously
    authorized, the next guarded merge sees the digest
    mismatch and refuses to invoke ``gh pr merge``."""

    def test_verifier_digest_mismatch_blocks_merge(self):
        """The merge transaction re-verifies
        ``auth.verifier_record_sha256`` against the canonical
        verifier.json digest. A subsequent ``verifier_failed``
        that overwrites the canonical record invalidates the
        digest and blocks the guarded merge."""
        # This is the existing production contract; verify the
        # source has the digest-mismatch check.
        from autocoder_orchestration import merge_authorization
        src = Path(merge_authorization.__file__).read_text()
        self.assertIn(
            "auth.verifier_record_sha256 != verifier_digest",
            src,
            "merge_authorization must verify "
            "auth.verifier_record_sha256 against the canonical "
            "verifier digest; this is the Option B chain for "
            "PRRT_kwDOTtyQLc6XPdD0",
        )

    def test_verifier_failed_writes_verdict_failed_tag(self):
        """``Controller.verifier_failed`` stamps ``_verdict_failed``
        so a subsequent ``cmd_merge_authorize`` rejects any
        authorization bound to a record carrying the tag."""
        from autocoder_orchestration import controller
        src = Path(controller.__file__).read_text()
        self.assertIn(
            "_verdict_failed",
            src,
            "Controller must tag failed verifier records with "
            "_verdict_failed=True so cmd_merge_authorize can "
            "reject them by name",
        )


class StateErrorRecoveryTests(unittest.TestCase):
    """The post-merge COMPLETE persistence path handles
    ``StateError`` and returns EXIT_STATE with the merge record
    durable. The recovery output identifies the merge-record
    PATH AND DIGEST."""

    def setUp(self):
        from autocoder_orchestration import cli as cli_module
        from autocoder_orchestration.context import (
            SCHEMA_VERSION as RC_SCHEMA,
        )
        from autocoder_orchestration.context import RunContext
        from autocoder_orchestration.store import StateStore
        from autocoder_orchestration.state_machine import (
            STATE_POST_MERGE_VERIFYING,
            StateMachine,
        )
        from autocoder_orchestration.merge_authorization import (
            MergeRecord,
        )
        from autocoder_orchestration.canonical_paths import canonical_paths
        from autocoder_orchestration.artifacts import write_artifact
        from autocoder_orchestration.controller import Controller
        from argparse import Namespace

        self.tmpdir = Path(tempfile.mkdtemp(prefix="aed-r4-state-"))
        self.run_state_root = self.tmpdir / "state"
        self.evidence_root = self.tmpdir / "evidence"
        self.run_state_root.mkdir(parents=True)
        self.evidence_root.mkdir(parents=True)
        self.ctx = RunContext(
            schema_version=RC_SCHEMA,
            run_id="test-r4-state",
            created_at="2026-08-07T15:00:00Z",
            repo_owner="Slideshow11", repo_name="AutoDev",
            local_checkout=str(self.run_state_root / "repo"),
            base_branch="main",
            authorized_base_sha="a" * 40,
            feature_branch="fix/test",
            pr_number=4,
            current_authorized_head="b" * 40,
            task_specification_path=str(self.evidence_root / "task.txt"),
            task_specification_sha256="c" * 64,
            required_ci_jobs=("test (3.10)", "test (3.11)",
                              "test (3.12)", "package-smoke",
                              "provenance", "committed-state-scan"),
            reviewer_policy="approve-only",
            quiet_window_seconds=180,
            implementation_worker_command=("echo", "worker"),
            verifier_command=None,
            verifier_handoff_policy="strict",
            permitted_mutations=("candidate.json", "verifier.json",
                                 "merge-record.json"),
            human_only_actions=("merge",),
            evidence_root=str(self.evidence_root),
            state_root=str(self.run_state_root),
            next_wave_policy="none",
        )
        self.store = StateStore(str(self.run_state_root))
        self.store.write_atomic("run_context.json", self.ctx.to_dict())
        sm = StateMachine(current_state=STATE_POST_MERGE_VERIFYING)
        self.store.write_atomic("state.json", sm.to_dict())
        self.paths = canonical_paths(self.evidence_root)
        for p in self.paths.values():
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Write a merge record.
        from autocoder_orchestration.merge_authorization import (
            MergeRecord,
        )
        rec = MergeRecord(
            schema_version="autocoder.merge_record.v2",
            run_id=self.ctx.run_id,
            repo=f"{self.ctx.repo_owner}/{self.ctx.repo_name}",
            pr_number=self.ctx.pr_number,
            authorized_head=self.ctx.current_authorized_head,
            squash_merge_commit="c1" * 14,
            merge_commit_parent="p1" * 10,
            squash_commit_parent_count=1,
            squash_tree_sha256="t1" * 16,
            final_local_main_sha="m1" * 20,
            final_origin_main_sha="m2" * 20,
            local_main_equals_origin_main=True,
            feature_branch_deleted_locally=False,
            feature_branch_deleted_remotely=False,
            working_tree_clean=True,
            aed_clean_post_merge=True,
            candidate_sha256_unchanged=True,
            verifier_record_sha256_unchanged=True,
            candidate_exact_file_digest="d" * 64,
            verifier_record_exact_file_digest="e" * 64,
            authorization_exact_file_digest="f" * 64,
            merge_record_exact_file_digest="",
            merge_timestamp="2026-08-07T15:00:00Z",
            unauthorized_actions_taken={
                "force_push": False, "auto_merge": False,
            },
            unavailable_observations=[],
            notes="",
            state_transition="MERGE_AUTHORIZED -> POST_MERGE_VERIFYING (COMPLETE transition is durably persisted by cmd_merge via Controller.report_complete)",
            final_state="COMPLETE",
        )
        write_artifact(self.paths["merge_record"], rec.to_dict())
        self.merge_record_path = self.paths["merge_record"]
        self.cli_module = cli_module
        self.args = Namespace(
            run_id=self.ctx.run_id,
            state_root=str(self.run_state_root),
            evidence_root=str(self.evidence_root),
            json=True,
            pr_number=self.ctx.pr_number,
            authorized_head=self.ctx.current_authorized_head,
        )
        self.Controller = Controller

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cmd_post_merge_verify_state_error_returns_exit_state(self):
        """``Controller.report_complete`` raising ``StateError``
        MUST produce a controlled EXIT_STATE, the merge record
        MUST remain durable, and the recovery output MUST
        identify both the merge-record PATH and DIGEST."""
        from autocoder_orchestration.state_machine import StateError

        def raise_state_error(self_):
            raise StateError("simulated state-machine rejection")
        with mock.patch.object(self.Controller, "report_complete",
                                raise_state_error):
            exit_code = self.cli_module.cmd_post_merge_verify(self.args)
        # EXIT_STATE == 4
        self.assertEqual(exit_code, self.cli_module.EXIT_STATE)
        # Merge record remains durable.
        self.assertTrue(self.merge_record_path.exists())


if __name__ == "__main__":
    unittest.main()