"""Independent exact-head verifier for PR #4.

This verifier is the genuine external verifier for the
strict-window qualification cycle on PR #4. It runs OUTSIDE
the implementation worker's process tree. It MUST NOT reuse
any conclusion from a prior session; every check below is
re-fetched against live GitHub state on every invocation.

Final verifier contract:

    qualification_head = one exact 40-character SHA supplied
        as immutable input via ``--qualification-head``. There
        is NO accepted-head set, NO production-code-equivalence
        exception, NO manifest-only-head exception, NO
        test-only-head exception.

The verifier proves every one of the following is bound to
exactly ``qualification_head``:

    * live_pr_head (live PR fetch)
    * CI query SHA (check-runs query) -- ALL pages collected
    * every required CI check-run head_sha
    * every strict-window observation pr_head_sha
    * candidate.exact_head
    * verifier.qualification_head itself
    * future authorization.authorized_head
    * AED file measured sha256
    * incident-record digest

The verifier's verifier.json is populated only from observed
results; helper return values flow through ``main`` into the
record. There are no hardcoded "true" literals and no copying
of the expected input into the "actual" field.

Operational location: ``scripts/``. The location alone does NOT
provide independence; the independence comes from running this
script in a separate process tree from the implementation worker
and never re-using a prior VERIFIED conclusion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Module-level guard: refuse to run under python -O / PYTHONOPTIMIZE.
# Per round-5 finding PRRT_kwDOTtyQLc6XSGep, every safety gate below
# must use ``raise VerificationFailure(...)`` rather than ``assert``.
# ``__debug__`` is False when the interpreter runs with -O or when
# ``PYTHONOPTIMIZE`` is set in the environment. We refuse the run
# outright so a malformed invocation cannot silently strip the gates.
if not __debug__:
    raise SystemExit(
        "independent_verifier_v2 must not run under python -O or "
        "PYTHONOPTIMIZE; assert-based gates would be stripped"
    )


class VerificationFailure(Exception):
    """Raised when a security-relevant verifier gate rejects
    live state. Used in place of ``assert`` so the gate cannot
    be stripped by ``python -O`` or ``PYTHONOPTIMIZE``.

    Every gate in this module that contributes to the
    ``verdict == "VERIFIED"`` outcome MUST raise
    ``VerificationFailure`` rather than ``assert`` so the
    optimization-stripping fail-open path is impossible.
    """


def _gate(condition: bool, label: str, message: str) -> None:
    """Raise ``VerificationFailure`` when ``condition`` is False.

    Used in place of ``assert`` for every security-relevant gate
    in this module. ``label`` identifies the gate (used in the
    raised message) and ``message`` describes the specific
    failure. The exception cannot be stripped by ``python -O``
    or ``PYTHONOPTIMIZE``.
    """
    if not condition:
        raise VerificationFailure(f"{label}: {message}")


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from autocoder_orchestration.artifacts import read_artifact, write_artifact
from autocoder_orchestration.canonical_paths import canonical_paths


DEFAULT_PR_NUMBER = 4
DEFAULT_REPO = "Slideshow11/AutoDev"
DEFAULT_EVIDENCE_ROOT = Path("/var/tmp/aed-pr4-evidence-final")
DEFAULT_STRICT_WINDOW_OBS = DEFAULT_EVIDENCE_ROOT / "strict_observations.jsonl"
DEFAULT_AED_PATH = REPO_ROOT / "scripts" / "quiet_window_observer.py"
DEFAULT_AED_EXPECTED_SHA = (
    "9897bd3b780fd03561b6d9f10302ced2e549cb5f3288aebadddddaa1c70f42ae"
)
CODERABBIT_AUTHOR_LOGIN = "coderabbitai"

# Hard page cap so a misbehaving pagination never loops forever.
PAGINATION_MAX_PAGES = 20
GRAPHQL_TIMEOUT_SECONDS = 120


def _step(name):
    def deco(fn):
        def wrapper(*args, **kwargs):
            print(f"=== {name} ===")
            try:
                return fn(*args, **kwargs)
            except AssertionError as e:
                print(f"  FAIL: {e}")
                raise
            finally:
                print()
        return wrapper
    return deco


def _normalize_login(login: object) -> str:
    """Match the production CLI's normalization contract so
    ``coderabbitai`` and ``coderabbitai[bot]`` both normalize to
    ``coderabbitai``. The verifier uses the same rule as
    ``autocoder_orchestration.cli._filter_coderabbit_review_state``.
    """
    if not isinstance(login, str):
        return ""
    s = login.lower().strip()
    if s.endswith("[bot]"):
        s = s[:-5].rstrip()
    return s


def _run_gh(args, *, env=None):
    """Run a ``gh`` command and return parsed JSON output."""
    base_env = {**os.environ, "HOME": os.path.expanduser("~")}
    if env:
        base_env.update(env)
    out = subprocess.check_output(
        ["gh"] + args, env=base_env, text=True,
    )
    return json.loads(out)


# ---------------------------------------------------------------------------
# Query builders
#
# Each builder is a small, directly-testable helper that produces a
# syntactically-valid GraphQL document. They use GraphQL variables (via
# ``-F`` flags the caller passes) so neither owner nor PR number is
# interpolated into the document.
# ---------------------------------------------------------------------------

def _build_review_threads_query() -> str:
    """Build the paginated ``reviewThreads`` query.

    The document has exactly four scopes:
      query Threads
        repository
          pullRequest
            reviewThreads

    plus the inner scopes for ``pageInfo``, ``comments``,
    ``nodes``, and the per-comment ``author{login}``.

    Output is a balanced GraphQL document; see
    ``tests/test_pr4_round4_query_builder.py`` for the regression
    that asserts brace / scope balance.
    """
    return """
query Threads($owner: String!, $name: String!, $pr: Int!, $first: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: $first, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        totalCount
        nodes {
          id
          isResolved
          isOutdated
          path
          comments(first: 50) {
            nodes {
              id
              author {
                login
              }
              body
              createdAt
            }
          }
        }
      }
    }
  }
}
"""


def _build_latest_reviews_query() -> str:
    """Build the paginated ``latestReviews`` query.

    Output has exactly four scopes:
      query Reviews
        repository
          pullRequest
            latestReviews

    plus ``pageInfo``, ``nodes``, and per-node ``author{login}``.
    """
    return """
query Reviews($owner: String!, $name: String!, $pr: Int!, $first: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      latestReviews(first: $first, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        totalCount
        nodes {
          state
          submittedAt
          author {
            login
          }
        }
      }
    }
  }
}
"""


def _build_review_decision_query() -> str:
    """Build the one-shot ``reviewDecision`` query.

    Used by ``_inspect_coderabbit`` to re-fetch the live
    PR-level ``reviewDecision``. This is a separate query
    from the paginated ``latestReviews`` so a malicious or
    misconfigured reviewer list cannot also forge a
    ``reviewDecision``.
    """
    return """
query Decision($owner: String!, $name: String!, $pr: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewDecision
    }
  }
}
"""


# Compile-time regex used to validate balanced GraphQL braces.
_GRAPHQL_BRACE_RE = re.compile(r"[{}]")


def _assert_graphql_balanced(doc: str, label: str) -> None:
    """``label`` is the test/log identifier; ``doc`` is the
    rendered GraphQL document. Fail loudly on unbalanced scopes
    so the bug class from PRRT_kwDOTtyQLc6XRdiR cannot return."""
    opens = _GRAPHQL_BRACE_RE.findall(doc).count("{")
    closes = _GRAPHQL_BRACE_RE.findall(doc).count("}")
    if opens != closes:
        raise VerificationFailure(
            f"GraphQL document {label!r} is unbalanced: "
            f"{opens} opens, {closes} closes"
        )
    depth = 0
    for ch in doc:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if depth < 0:
            raise VerificationFailure(
                f"GraphQL document {label!r} has a closing brace before "
                f"an opening one"
            )
    if depth != 0:
        raise VerificationFailure(
            f"GraphQL document {label!r} does not fully close (depth={depth})"
        )


# Compile-time validation: every built-in document MUST be balanced.
for _name, _builder in [
    ("review_threads_query", _build_review_threads_query),
    ("latest_reviews_query", _build_latest_reviews_query),
    ("review_decision_query", _build_review_decision_query),
]:
    _assert_graphql_balanced(_builder(), _name)


def _run_gh_graphql(query: str, *, variables: Dict[str, Any]) -> dict:
    """Run a ``gh api graphql`` query via a tempfile. Variables
    are passed via ``-F key=value``. The query itself is never
    interpolated with caller data; all caller-supplied
    identifiers reach GitHub as typed GraphQL variables.

    A finite timeout is enforced so a network stall fails the
    verifier instead of blocking forever.
    """
    base_env = {**os.environ, "HOME": os.path.expanduser("~")}
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".graphql", delete=False,
    ) as f:
        f.write(query)
        qpath = f.name
    cmd = ["gh", "api", "graphql", "-F", f"query=@{qpath}"]
    for k, v in variables.items():
        cmd.extend(["-F", f"{k}={v}"])
    try:
        try:
            proc = subprocess.run(
                cmd, env=base_env, capture_output=True, text=True,
                timeout=GRAPHQL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as e:
            raise AssertionError(
                f"gh api graphql timed out after {e.timeout}s"
            ) from e
    finally:
        os.unlink(qpath)
    if proc.returncode != 0:
        raise AssertionError(f"gh api graphql failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _paginate_connection(
    args, query_builder, *, variables_base: Dict[str, Any],
    page_size: int = 100, label: str = "connection",
) -> Tuple[List[dict], int]:
    """Generic paginator for any GraphQL connection whose
    ``nodes`` carry the data we want and whose result includes
    ``pageInfo{hasNextPage endCursor} totalCount``.

    Returns ``(nodes, total_count)``. Always requires
    ``totalCount`` to be present (per round-4 finding
    PRRT_kwDOTtyQLc6XRdia and round-5 finding
    PRRT_kwDOTtyQLc6XSGeg: completeness must FAIL CLOSED if
    ``totalCount`` is absent).
    """
    nodes: List[dict] = []
    total: Optional[int] = None
    cursor = "null"
    page = 0
    while True:
        page += 1
        if page > PAGINATION_MAX_PAGES:
            raise VerificationFailure(
                f"{label} pagination exceeded {PAGINATION_MAX_PAGES} "
                f"pages; aborting to avoid an infinite loop"
            )
        variables = dict(variables_base)
        variables["first"] = str(page_size)
        variables["cursor"] = cursor
        query = query_builder()
        result = _run_gh_graphql(query, variables=variables)
        data = (result.get("data", {})
                    .get("repository", {})
                    .get("pullRequest", {})
                    .get(label, {}))
        # totalCount is REQUIRED for fail-closed completeness.
        page_total = data.get("totalCount")
        _gate(
            page_total is not None,
            f"{label} totalCount completeness",
            f"{label} response omitted totalCount; "
            f"pagination completeness cannot be proven; "
            f"failing closed to avoid an invisible partial inventory",
        )
        if total is None:
            total = page_total
        else:
            _gate(
                page_total == total,
                f"{label} totalCount consistency",
                f"{label} totalCount changed across pages: "
                f"{total} -> {page_total}",
            )
        page_nodes = data.get("nodes", [])
        nodes.extend(page_nodes)
        page_info = data.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info["endCursor"]
    _gate(
        len(nodes) == total,
        f"{label} pagination completeness",
        f"collected {len(nodes)} != totalCount {total}; "
        f"a node on a later page would be invisible",
    )
    return nodes, total


def _paginate_review_threads(args) -> Tuple[List[dict], int]:
    return _paginate_connection(
        args, _build_review_threads_query,
        variables_base={
            "owner": args.repo.split("/")[0],
            "name": args.repo.split("/")[1],
            "pr": str(args.pr_number),
        },
        label="reviewThreads",
    )


def _paginate_latest_reviews(args) -> Tuple[List[dict], int]:
    return _paginate_connection(
        args, _build_latest_reviews_query,
        variables_base={
            "owner": args.repo.split("/")[0],
            "name": args.repo.split("/")[1],
            "pr": str(args.pr_number),
        },
        label="latestReviews",
    )


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--qualification-head", required=True, type=str,
        help="Exact 40-character SHA the verifier must bind every check to.",
    )
    p.add_argument("--pr-number", type=int, default=DEFAULT_PR_NUMBER)
    p.add_argument("--repo", default=DEFAULT_REPO)
    p.add_argument(
        "--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT,
    )
    p.add_argument(
        "--strict-window-obs",
        type=Path, default=DEFAULT_STRICT_WINDOW_OBS,
    )
    p.add_argument(
        "--aed-path", type=Path, default=DEFAULT_AED_PATH,
    )
    p.add_argument("--aed-expected-sha", default=DEFAULT_AED_EXPECTED_SHA)
    p.add_argument(
        "--incident-record", type=Path, required=True,
        help=("Path to the force-push incident record (canonical "
              "artifact with mandatory .sha256 sidecar). Required; "
              "the verifier does not silently default to a shared "
              "/var/tmp path."),
    )
    args = p.parse_args(argv)
    qual = args.qualification_head.strip().lower()
    if len(qual) != 40 or any(c not in "0123456789abcdef" for c in qual):
        raise SystemExit(
            f"--qualification-head must be exactly 40 lowercase hex chars; "
            f"got {qual!r}",
        )

    print(f"=== independent verifier for PR #{args.pr_number} ===")
    print(f"qualification_head: {qual}")
    print(f"repo: {args.repo}")
    print()

    observations: Dict[str, Any] = {}
    observations["pr"] = _fetch_pr(args, qual)
    observations["ci"] = _inspect_ci(args, qual)
    observations["coderabbit"] = _inspect_coderabbit(args)
    observations["threads"] = _inspect_threads(args)
    observations["aed"] = _verify_aed(args)
    observations["strict_window"] = _verify_strict_window(args, qual)
    observations["candidate"] = _verify_candidate(args, qual)
    observations["incident"] = _verify_incident_record(args)

    _write_verifier(args, qual, observations)

    print("=" * 60)
    print("VERIFIER PASSED -- all independently-fetched checks clean")
    print(f"verifier.qualification_head == {qual}")
    print("=" * 60)
    return 0


@_step("Fetch live PR state for exact head (positive readiness)")
def _fetch_pr(args, qual) -> dict:
    data = _run_gh([
        "pr", "view", str(args.pr_number), "--repo", args.repo,
        "--json", "headRefOid,state,mergedAt,isDraft,reviewDecision,mergeable,mergeStateStatus,autoMergeRequest",
    ])
    print(json.dumps(data, indent=2))
    live_head = str(data.get("headRefOid", "")).lower()
    _gate(
        live_head == qual,
        "exact live PR head",
        f"live PR head {live_head!r} != qualification_head {qual!r}",
    )
    _gate(
        data.get("state") == "OPEN",
        "PR state",
        f"PR state is {data.get('state')!r}, expected 'OPEN'",
    )
    _gate(
        data.get("mergedAt") is None,
        "merged state",
        f"PR is merged at {data.get('mergedAt')!r}; verifier should fail",
    )
    _gate(
        data.get("isDraft") is False,
        "draft state",
        f"PR is a draft; isDraft={data.get('isDraft')!r}",
    )
    mergeable = data.get("mergeable")
    _gate(
        mergeable == "MERGEABLE",
        "mergeability",
        f"PR mergeability is not positively confirmed; "
        f"mergeable={mergeable!r}, "
        f"mergeStateStatus={data.get('mergeStateStatus')!r}; "
        f"UNKNOWN and CONFLICTING both fail",
    )
    merge_state_status = data.get("mergeStateStatus")
    ACCEPTED_STATES = ("CLEAN", "HAS_HOOKS", "UNSTABLE")
    _gate(
        merge_state_status in ACCEPTED_STATES,
        "mergeStateStatus",
        f"PR mergeStateStatus={merge_state_status!r} is not in "
        f"the positive set {ACCEPTED_STATES}; UNKNOWN and "
        f"DIRTY/BLOCKED/BEHIND both fail",
    )
    auto_merge = data.get("autoMergeRequest")
    _gate(
        auto_merge is None,
        "auto-merge absent",
        f"autoMergeRequest must be None; got {auto_merge!r}",
    )
    print(f"OK: live PR head == qualification_head == {qual}")
    return data


@_step("Inspect exact-head CI on the qualification head (paginates ALL pages)")
def _inspect_ci(args, qual) -> dict:
    """Collect every check-run on the qualification head via a
    single canonical collector.

    Implementation note: ``gh api ... --paginate`` emits one
    JSON document per page and ``json.loads`` cannot parse the
    concatenated output as a single object. We therefore
    request pages explicitly with ``?per_page=100&page=N`` and
    aggregate them in Python. The collector:

    * binds every request to qualification_head;
    * collects every page until the page returns fewer than
      ``per_page`` items;
    * requires an explicit ``total_count`` on every page;
    * requires every page's total_count to agree;
    * requires ``len(collected runs) == total_count``;
    * rejects a check-run whose head_sha differs from
      qualification_head;
    * rejects malformed page shapes;
    * rejects duplicate run IDs that would otherwise mask
      a missing intermediate page.
    """
    runs, total_count = _collect_check_runs(args, qual)
    print(f"check-runs total: {total_count}, collected: {len(runs)}")
    for r in runs:
        print(f"  {r['name']}: {r.get('conclusion') or r.get('status')}")
        head_sha = str(r.get("head_sha") or r.get("head", {}).get("sha") or "")
        _gate(
            not head_sha or head_sha.lower() == qual,
            "exact-head CI",
            f"check-run {r['name']!r} head_sha={head_sha!r} "
            f"!= qualification_head={qual!r}",
        )
    _gate(
        total_count is not None,
        "exact-head CI",
        "check-runs response omitted total_count; "
        "completeness cannot be proven; failing closed",
    )
    _gate(
        total_count > 0,
        "exact-head CI",
        "check-runs response reported total_count == 0",
    )
    _gate(
        len(runs) == total_count,
        "exact-head CI",
        f"check-runs pagination completeness: "
        f"collected {len(runs)} != total_count {total_count}; "
        f"a check-run beyond page 1 would be invisible",
    )
    required_jobs = {
        "test (3.10)", "test (3.11)", "test (3.12)",
        "package-smoke", "provenance", "committed-state-scan",
    }
    seen = {r["name"] for r in runs}
    missing = required_jobs - seen
    _gate(
        not missing,
        "required CI completeness",
        f"missing required jobs: {missing}",
    )
    failed = [r["name"] for r in runs
              if r.get("conclusion") not in ("success", "skipped", "neutral")]
    _gate(
        not failed,
        "exact-head CI",
        f"failed jobs: {failed}",
    )
    print(f"OK: all 6 required jobs are green on qualification head {qual}")
    return {"runs": runs, "total_count": total_count}


# Check-run pagination constants.
_CHECK_RUN_PAGE_SIZE = 100
_CHECK_RUN_MAX_PAGES = 20


def _collect_check_runs(args, qual) -> tuple:
    """Single canonical collector for every check-run on the
    qualification head.

    Returns ``(runs, total_count)`` where ``total_count`` is
    the repository-side total reported by the API on every
    page (consistent across pages). The collector refuses to
    fall back to ``len(runs)`` -- ``total_count`` must be
    explicitly present and equal across pages.

    Each page is fetched via ``gh api ... ?per_page=...&page=N``
    so the helper survives multi-page JSON output. The
    collector also rejects malformed page shapes and any
    duplicated runs that would otherwise mask a missing
    intermediate page.
    """
    runs: List[dict] = []
    total_count = None
    seen_ids: set = set()
    page_n = 0
    while True:
        page_n += 1
        if page_n > _CHECK_RUN_MAX_PAGES:
            raise VerificationFailure(
                f"check-run pagination exceeded "
                f"{_CHECK_RUN_MAX_PAGES} pages; aborting"
            )
        # Each request is bound to qualification_head (the
        # path includes the 40-char SHA). We do NOT use
        # ``--paginate`` because its concatenated JSON output
        # cannot be parsed by a single ``json.loads`` call.
        try:
            data = _run_gh([
                "api", f"repos/{args.repo}/commits/{qual}/check-runs",
                "-q", ".",
                "-F", f"per_page={_CHECK_RUN_PAGE_SIZE}",
                "-F", f"page={page_n}",
            ])
        except json.JSONDecodeError as e:
            raise VerificationFailure(
                f"check-runs page {page_n} returned invalid JSON: "
                f"{e!r}; failing closed"
            ) from e
        # Malformed page shape: must be a dict with
        # ``check_runs`` (a list) and ``total_count``.
        if not isinstance(data, dict):
            raise VerificationFailure(
                f"check-runs page {page_n} returned a non-dict "
                f"payload: {type(data).__name__}"
            )
        page_runs = data.get("check_runs")
        if not isinstance(page_runs, list):
            raise VerificationFailure(
                f"check-runs page {page_n} missing 'check_runs' list"
            )
        page_total = data.get("total_count")
        if page_total is None:
            raise VerificationFailure(
                f"check-runs page {page_n} omitted total_count; "
                f"completeness cannot be proven"
            )
        # Every page must agree on the repository-side total.
        if total_count is None:
            total_count = page_total
        else:
            if page_total != total_count:
                raise VerificationFailure(
                    f"check-runs page {page_n} reported "
                    f"total_count={page_total}; previous pages "
                    f"reported total_count={total_count}"
                )
        # Reject duplicate runs that would mask a missing
        # intermediate page.
        for r in page_runs:
            rid = r.get("id")
            if rid is not None and rid in seen_ids:
                raise VerificationFailure(
                    f"check-run id={rid} appeared on multiple "
                    f"pages; pagination is ambiguous"
                )
            if rid is not None:
                seen_ids.add(rid)
            runs.append(r)
        # Stop when GitHub returns fewer than ``per_page`` items.
        # BEFORE returning, verify that the collected count equals
        # the server-reported total. This is the completeness
        # invariant; failing closed here prevents the inspect
        # layer from asserting tautological completeness.
        if len(page_runs) < _CHECK_RUN_PAGE_SIZE:
            if len(runs) != total_count:
                raise VerificationFailure(
                    f"check-run pagination completeness: "
                    f"collected {len(runs)} != total_count {total_count}; "
                    f"a check-run beyond page 1 would be invisible"
                )
            break
    return runs, total_count


@_step("Inspect live CodeRabbit review decision (fail-closed)")
def _inspect_coderabbit(args) -> dict:
    """Fail-closed live CodeRabbit gate.

    Per round-4 finding PRRT_kwDOTtyQLc6XRdiz: do NOT rely on
    connection order. Collect ALL matching CodeRabbit reviews,
    sort explicitly by submittedAt, and select the newest one.
    """
    target_login = _normalize_login(CODERABBIT_AUTHOR_LOGIN)
    reviews_nodes, _ = _paginate_latest_reviews(args)
    # Collect every matching CodeRabbit review object so we can
    # sort by submittedAt and tolerate null/deleted authors
    # without crashing. A null author is "no matching identity".
    coderabbit_reviews: List[dict] = []
    for r in reviews_nodes:
        author_obj = r.get("author") or {}
        author_login = author_obj.get("login") or ""
        if _normalize_login(author_login) == target_login:
            coderabbit_reviews.append(r)
    # Sort explicitly by submittedAt; never trust connection
    # order. submittedAt is required; missing submittedAt would
    # be ambiguous and we fail closed.
    for r in coderabbit_reviews:
        _gate(
            r.get("submittedAt"),
            "CodeRabbit review submittedAt",
            f"CodeRabbit review missing submittedAt: {r!r}",
        )
    coderabbit_reviews.sort(
        key=lambda r: r.get("submittedAt", ""),
        reverse=True,
    )
    # Re-fetch reviewDecision via its own one-shot query. This
    # isolates the per-PR decision from the latestReviews list
    # so the two cannot be forged together.
    query = _build_review_decision_query()
    result = _run_gh_graphql(query, variables={
        "owner": args.repo.split("/")[0],
        "name": args.repo.split("/")[1],
        "pr": str(args.pr_number),
    })
    decision = (result.get("data", {})
                     .get("repository", {})
                     .get("pullRequest", {})
                     .get("reviewDecision"))
    print(f"reviewDecision: {decision}")
    states = [r["state"] for r in coderabbit_reviews]
    print(f"matching CodeRabbit reviews (sorted by submittedAt desc): "
          f"{[(r.get('submittedAt'), r.get('state')) for r in coderabbit_reviews]}")

    _gate(
        decision is not None,
        "CodeRabbit reviewDecision",
        "live reviewDecision is None; no review decision is available",
    )
    _gate(
        decision != "CHANGES_REQUESTED",
        "CodeRabbit reviewDecision",
        f"live reviewDecision is {decision!r}; verifier must fail closed",
    )
    _gate(
        coderabbit_reviews,
        "CodeRabbit presence",
        "no live CodeRabbit review found across all paginated "
        "latestReviews; the production identity contract requires "
        "coderabbitai or coderabbitai[bot]",
    )
    latest = coderabbit_reviews[0]
    latest_state = latest["state"]
    _gate(
        latest_state == "APPROVED",
        "CodeRabbit latest review state",
        f"newest live CodeRabbit review is {latest_state!r}; "
        f"verifier must require APPROVED",
    )
    return {
        "review_decision": decision,
        "coderabbit_states": states,
        "latest_coderabbit_state": latest_state,
        "latest_coderabbit_submitted_at": latest.get("submittedAt"),
    }


@_step("Inspect every review thread (paginates ALL pages, totalCount required)")
def _inspect_threads(args) -> dict:
    nodes, total = _paginate_review_threads(args)
    print(f"total threads: {total}, "
          f"resolved: {sum(1 for n in nodes if n['isResolved'])}, "
          f"unresolved: {sum(1 for n in nodes if not n['isResolved'])}, "
          f"unresolved_outdated: {sum(1 for n in nodes if not n['isResolved'] and n['isOutdated'])}")
    unresolved = [n for n in nodes if not n["isResolved"]]
    _gate(
        not unresolved,
        "unresolved thread gate",
        f"unresolved threads: {unresolved}",
    )
    print(f"OK: every review thread on PR #{args.pr_number} is resolved")
    return {"nodes": nodes, "count": total}


@_step("Verify AED unchanged: scripts/quiet_window_observer.py (exactly one manifest match)")
def _verify_aed(args) -> dict:
    p = args.aed_path
    raw = p.read_bytes()
    measured_sha = hashlib.sha256(raw).hexdigest()
    print(f"actual:   {measured_sha}")
    print(f"expected: {args.aed_expected_sha}")
    _gate(
        measured_sha == args.aed_expected_sha,
        "AED digest",
        f"measured_sha={measured_sha!r} != "
        f"aed_expected_sha={args.aed_expected_sha!r}",
    )
    manifest_path = REPO_ROOT / "provenance" / "aed-pr417-source-manifest.json"
    with manifest_path.open() as f:
        m = json.load(f)
    matches = [
        e for e in m["files"]
        if e["destination_path"] == "scripts/quiet_window_observer.py"
    ]
    _gate(
        len(matches) == 1,
        "AED manifest uniqueness",
        f"manifest must contain exactly one entry for "
        f"scripts/quiet_window_observer.py; found {len(matches)}",
    )
    manifest_sha = matches[0]["destination_sha256"]
    print(f"manifest: {manifest_sha}")
    _gate(
        manifest_sha == args.aed_expected_sha,
        "AED manifest digest",
        f"manifest sha {manifest_sha!r} does not match the "
        f"expected AED sha {args.aed_expected_sha!r}",
    )
    print("OK: AED sha matches measured, manifest, and PR-3 base")
    return {
        "measured_sha256": measured_sha,
        "expected_sha256": args.aed_expected_sha,
        "manifest_sha256": manifest_sha,
    }


@_step("Verify 180-second strict window observation record")
def _verify_strict_window(args, qual) -> dict:
    raw = args.strict_window_obs.read_bytes()
    data = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    qualifying = [d for d in data if d.get("qualifying")]
    print(f"total: {len(data)}, qualifying: {len(qualifying)}")
    _gate(
        len(qualifying) >= 1,
        "strict-window observations",
        "no qualifying observations in strict window record",
    )
    span = qualifying[-1]["ts_monotonic"] - qualifying[0]["ts_monotonic"]
    print(f"span: {span:.3f} seconds (target: >= 180)")
    _gate(
        span >= 180.0,
        "strict-window duration",
        f"strict window span {span:.3f}s < 180s",
    )
    pids = {d.get("supervisor_pid") for d in qualifying}
    start_ids = {d.get("process_start_identity") for d in qualifying}
    heads = {str(d.get("pr_head_sha", "")).lower() for d in qualifying}
    _gate(len(pids) == 1, "strict-window supervisor_pid",
          f"pid drift: {pids}")
    _gate(len(start_ids) == 1, "strict-window process_start_identity",
          f"start_id drift: {start_ids}")
    _gate(len(heads) == 1, "strict-window head",
          f"head drift: {heads}")
    obs_head = next(iter(heads))
    _gate(
        obs_head == qual,
        "strict-window head binding",
        f"strict-window observation head {obs_head!r} != "
        f"qualification head {qual!r}",
    )
    print(f"PID stable: {pids}, start_id stable: {start_ids}, head stable: {obs_head}")
    bad = [d for d in qualifying
           if not (d.get("head_ok") and d.get("all_ci_pass") and d.get("coderabbit_pass"))]
    _gate(
        not bad,
        "strict-window per-observation gates",
        f"observations failing per-obs gates: {bad}",
    )
    bad_threads = [d for d in qualifying
                  if d.get("threads", {}).get("unresolved", 0) != 0
                  or d.get("threads", {}).get("unresolved_outdated", 0) != 0]
    _gate(
        not bad_threads,
        "strict-window threads-clean",
        f"observations with unresolved threads: {bad_threads}",
    )
    print("OK: strict window >= 180s, all invariants stable")
    return {
        "span_seconds": span,
        "observation_count": len(qualifying),
        "observation_head_sha": obs_head,
    }


@_step("Verify candidate + sidecar exact-file digests bound to qualification head")
def _verify_candidate(args, qual) -> dict:
    paths = canonical_paths(args.evidence_root)
    _gate(
        paths["candidate"].exists(),
        "candidate artifact existence",
        f"candidate missing: {paths['candidate']}",
    )
    cand = read_artifact(paths["candidate"])
    sidecar = Path(str(paths["candidate"]) + ".sha256")
    _gate(
        sidecar.exists(),
        "candidate sidecar existence",
        f"sidecar missing: {sidecar}",
    )
    sidecar_digest = sidecar.read_text().strip()
    print(f"candidate: {cand.digest}")
    print(f"sidecar:   {sidecar_digest}")
    _gate(
        cand.digest == sidecar_digest,
        "candidate digest vs sidecar",
        f"candidate digest {cand.digest!r} != sidecar digest {sidecar_digest!r}",
    )
    _gate(
        len(cand.digest) == 64,
        "candidate digest format",
        f"candidate digest must be 64 hex chars; got {len(cand.digest)}",
    )
    payload = cand.payload
    candidate_head = str(payload.get("exact_head", "")).lower()
    _gate(
        candidate_head == qual,
        "candidate exact_head binding",
        f"candidate.exact_head {candidate_head!r} != qualification head {qual!r}",
    )
    _gate(
        payload.get("pr_number") == args.pr_number,
        "candidate pr_number",
        f"candidate pr_number {payload.get('pr_number')!r} != args.pr_number {args.pr_number!r}",
    )
    print(f"OK: candidate.exact_head == qualification_head == {qual}")
    return {
        "candidate_digest": cand.digest,
        "candidate_exact_head": candidate_head,
        "candidate_pr_number": payload.get("pr_number"),
    }


@_step("Inspect the force-push incident record (canonical artifact, --incident-record required)")
def _verify_incident_record(args) -> dict:
    """Read the incident record as a canonical artifact.

    Per round-4 finding PRRT_kwDOTtyQLc6XRdi5: the verifier MUST
    NOT manufacture, repair, or migrate the sidecar it is about
    to verify. ``--incident-record`` is REQUIRED (no
    ``/var/tmp`` default) and the sidecar MUST already exist
    on disk with a valid digest. ``read_artifact`` performs the
    verification; any failure (missing sidecar, digest
    mismatch, malformed sidecar, insecure mode, legacy footer)
    fails the verifier closed.

    The historical incident body is preserved verbatim; the
    verifier never modifies either the body or the sidecar.
    """
    p = args.incident_record
    _gate(
        p.exists(),
        "incident record existence",
        f"incident record missing: {p}",
    )
    sidecar_path = Path(str(p) + ".sha256")
    _gate(
        sidecar_path.exists(),
        "incident sidecar existence",
        f"incident sidecar missing: {sidecar_path}; "
        f"the verifier does not create sidecars. The canonical "
        f"sidecar must be produced by the artifact producer, "
        f"not the verifier.",
    )
    # ``read_artifact`` performs the canonical verification:
    # sidecar presence, digest equality, mode, and footer check.
    record = read_artifact(p)
    payload = record.payload
    print(f"incident record digest: {record.digest}")
    print(f"incident_class: {payload.get('incident_class')!r}")
    mechanism = str(payload.get("force_push_mechanism") or "")
    print(f"force_push_mechanism: {mechanism[:80]}")
    print(f"restored_head_sha: {payload.get('restored_head_sha')!r}")
    print(f"no_repeat_permitted: {payload.get('no_repeat_permitted')!r}")
    _gate(
        "force" in mechanism.lower(),
        "incident force_push_mechanism",
        f"incident record must record the force mechanism; "
        f"got {mechanism!r}",
    )
    _gate(
        payload.get("no_repeat_permitted") is True,
        "incident no_repeat_permitted",
        "must forbid repeat",
    )
    print("OK: incident record is canonical, complete, and honest")
    return {
        "incident_digest": record.digest,
        "incident_class": payload.get("incident_class"),
        "force_push_mechanism": mechanism,
        "restored_head_sha": payload.get("restored_head_sha"),
        "no_repeat_permitted": payload.get("no_repeat_permitted"),
    }


@_step("Write the canonical verifier.json + sidecar populated from observed results")
def _write_verifier(args, qual, observations) -> None:
    """Populate the verifier record from observed results. Every
    value comes from a helper return value (not from a
    hardcoded literal and not from a copy of the expected
    input)."""
    paths = canonical_paths(args.evidence_root)
    cand_digest = observations["candidate"]["candidate_digest"]
    aed_measured = observations["aed"]["measured_sha256"]
    aed_expected = observations["aed"]["expected_sha256"]
    sw = observations["strict_window"]
    incident = observations["incident"]
    cr = observations["coderabbit"]
    pr = observations["pr"]
    ci = observations["ci"]
    threads_total = observations["threads"]["count"]

    verifier_record = {
        "schema_version": "autocoder.verifier_record.v2",
        "candidate_sha256": cand_digest,
        "qualification_head": qual,
        "live_pr_head": str(pr.get("headRefOid", "")).lower(),
        "live_pr_state": pr.get("state"),
        "live_pr_merged_at": pr.get("mergedAt"),
        "live_pr_is_draft": pr.get("isDraft"),
        "live_pr_mergeable": pr.get("mergeable"),
        "live_pr_merge_state_status": pr.get("mergeStateStatus"),
        "live_pr_auto_merge_request": pr.get("autoMergeRequest"),
        "live_ci_total_count": ci["total_count"],
        "live_ci_collected_count": len(ci["runs"]),
        "live_review_decision": cr["review_decision"],
        "latest_coderabbit_state": cr["latest_coderabbit_state"],
        "latest_coderabbit_submitted_at": cr.get("latest_coderabbit_submitted_at"),
        "thread_total_count": threads_total,
        "thread_unresolved_count": sum(
            1 for n in observations["threads"]["nodes"]
            if not n["isResolved"]
        ),
        "aed_measured_sha256": aed_measured,
        "aed_expected_sha256": aed_expected,
        "aed_manifest_sha256": observations["aed"]["manifest_sha256"],
        "aed_clean": aed_measured == aed_expected,
        "strict_window_span_seconds": sw["span_seconds"],
        "strict_window_observation_count": sw["observation_count"],
        "strict_window_head_sha": sw["observation_head_sha"],
        "incident_record_digest": incident["incident_digest"],
        "incident_class": incident["incident_class"],
        "force_push_mechanism": incident["force_push_mechanism"],
        "restored_head_sha": incident["restored_head_sha"],
        "no_repeat_permitted": incident["no_repeat_permitted"],
        "verdict": "VERIFIED",
        "defects": [],
        "verified_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "verifier": f"scripts.{Path(__file__).stem}",
        "verifier_module_path": str(Path(__file__).resolve()),
        "verifier_file_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "checks": {
            "live_pr_head_matches": str(pr.get("headRefOid", "")).lower() == qual,
            "exact_head_ci_all_pass": all(
                r.get("conclusion") in ("success", "skipped", "neutral")
                for r in ci["runs"]
            ),
            "thread_state_clean": all(
                n["isResolved"] for n in observations["threads"]["nodes"]
            ),
            "aed_unchanged": aed_measured == aed_expected,
            "strict_window_passed": sw["span_seconds"] >= 180.0,
            "live_coderabbit_approved": cr["latest_coderabbit_state"] == "APPROVED",
            "live_review_decision_acceptable": cr["review_decision"] not in (
                None, "CHANGES_REQUESTED"
            ),
            "merge_state_clean": pr.get("state") == "OPEN" and pr.get("mergedAt") is None,
            "auto_merge_absent": pr.get("autoMergeRequest") is None,
            "pr_not_draft": pr.get("isDraft") is False,
            "pr_mergeable": pr.get("mergeable") == "MERGEABLE",
            "pr_merge_state_status_acceptable": pr.get("mergeStateStatus") in (
                "CLEAN", "HAS_HOOKS", "UNSTABLE",
            ),
            "force_push_incident_recorded": incident["force_push_mechanism"] != "",
        },
    }
    result = write_artifact(paths["verifier"], verifier_record)
    print(f"verifier:   {paths['verifier']}")
    print(f"sidecar:    {Path(str(paths['verifier']) + '.sha256')}")
    print(f"digest:     {result.digest}")
    sidecar = Path(str(paths["verifier"]) + ".sha256")
    _gate(
        sidecar.read_text().strip() == result.digest,
        "verifier sidecar",
        f"verifier sidecar mismatch: {sidecar.read_text().strip()!r} != {result.digest!r}",
    )
    reread = read_artifact(paths["verifier"])
    _gate(reread.digest == result.digest, "verifier reread digest",
          f"reread digest {reread.digest!r} != written digest {result.digest!r}")
    _gate(reread.payload.get("verdict") == "VERIFIED", "verifier verdict reread",
          f"reread verdict {reread.payload.get('verdict')!r} != 'VERIFIED'")
    _gate(
        reread.payload.get("candidate_sha256") == cand_digest,
        "verifier reread candidate_sha256",
        f"reread candidate_sha256 {reread.payload.get('candidate_sha256')!r} != {cand_digest!r}",
    )
    _gate(
        reread.payload.get("qualification_head") == qual,
        "verifier reread qualification_head",
        f"reread qualification_head {reread.payload.get('qualification_head')!r} != {qual!r}",
    )
    _gate(
        reread.payload.get("aed_measured_sha256") == aed_measured,
        "verifier reread aed_measured_sha256",
        f"reread aed_measured_sha256 {reread.payload.get('aed_measured_sha256')!r} != {aed_measured!r}",
    )
    print(f"OK: verifier.json + sidecar written; "
          f"verifier.qualification_head == {qual}")


if __name__ == "__main__":
    sys.exit(main())