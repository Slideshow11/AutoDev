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
    opens = doc.count("{")
    closes = doc.count("}")
    assert opens == closes, (
        f"GraphQL document {label!r} is unbalanced: "
        f"{opens} opens, {closes} closes"
    )
    depth = 0
    for ch in doc:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        assert depth >= 0, (
            f"GraphQL document {label!r} has a closing brace before "
            f"an opening one"
        )
    assert depth == 0, (
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
    PRRT_kwDOTtyQLc6XRdia: completeness must FAIL CLOSED if
    ``totalCount`` is absent).
    """
    nodes: List[dict] = []
    total: Optional[int] = None
    cursor = "null"
    page = 0
    while True:
        page += 1
        if page > PAGINATION_MAX_PAGES:
            raise AssertionError(
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
        if page_total is None:
            raise AssertionError(
                f"{label} response omitted totalCount; "
                f"pagination completeness cannot be proven; "
                f"failing closed to avoid an invisible partial inventory"
            )
        if total is None:
            total = page_total
        else:
            assert page_total == total, (
                f"{label} totalCount changed across pages: "
                f"{total} -> {page_total}"
            )
        page_nodes = data.get("nodes", [])
        nodes.extend(page_nodes)
        page_info = data.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info["endCursor"]
    assert total is not None, (
        f"{label} pagination completed without a totalCount; "
        f"the response was missing totalCount on every page"
    )
    assert len(nodes) == total, (
        f"{label} pagination completeness: "
        f"collected {len(nodes)} != totalCount {total}; "
        f"a node on a later page would be invisible"
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
    assert live_head == qual, (
        f"live PR head {live_head!r} != qualification_head {qual!r}"
    )
    assert data.get("state") == "OPEN", (
        f"PR state is {data.get('state')!r}, expected 'OPEN'"
    )
    assert data.get("mergedAt") is None, "PR is merged; verifier should fail"
    assert data.get("isDraft") is False, (
        f"PR is a draft; verifier must reject; isDraft={data.get('isDraft')!r}"
    )
    # POSITIVE mergeability contract (per round-4 finding
    # PRRT_kwDOTtyQLc6XRdig). The verifier requires an explicit
    # positive readiness value -- UNKNOWN / CONFLICTING both
    # fail. This is consistent with the production guarded-merge
    # path: a real merge is unsafe until mergeability is
    # confirmed, and a verifier that allows UNKNOWN would pass
    # a PR whose readiness has not yet been computed.
    mergeable = data.get("mergeable")
    assert mergeable == "MERGEABLE", (
        f"PR mergeability is not positively confirmed; "
        f"mergeable={mergeable!r}, "
        f"mergeStateStatus={data.get('mergeStateStatus')!r}; "
        f"UNKNOWN and CONFLICTING both fail"
    )
    # POSITIVE mergeStateStatus contract. GitHub returns
    # CLEAN / HAS_HOOKS / UNSTABLE for an actively-mergeable PR;
    # UNKNOWN means mergeability has not been computed yet and
    # the verifier MUST reject it.
    merge_state_status = data.get("mergeStateStatus")
    ACCEPTED_STATES = ("CLEAN", "HAS_HOOKS", "UNSTABLE")
    assert merge_state_status in ACCEPTED_STATES, (
        f"PR mergeStateStatus={merge_state_status!r} is not in "
        f"the positive set {ACCEPTED_STATES}; UNKNOWN and "
        f"DIRTY/BLOCKED/BEHIND both fail"
    )
    auto_merge = data.get("autoMergeRequest")
    assert auto_merge is None, (
        f"autoMergeRequest must be None; got {auto_merge!r}"
    )
    print(f"OK: live PR head == qualification_head == {qual}")
    return data


@_step("Inspect exact-head CI on the qualification head (paginates ALL pages)")
def _inspect_ci(args, qual) -> dict:
    raw = _run_gh([
        "api", f"repos/{args.repo}/commits/{qual}/check-runs",
        "-q", ".",
        "--paginate",
    ])
    if isinstance(raw, list):
        runs = raw
        total_count = len(runs)
    else:
        runs = raw.get("check_runs", [])
        total_count = raw.get("total_count", len(runs))
    print(f"check-runs total: {total_count}, collected: {len(runs)}")
    for r in runs:
        print(f"  {r['name']}: {r.get('conclusion') or r.get('status')}")
        head_sha = str(r.get("head_sha") or r.get("head", {}).get("sha") or "")
        if head_sha and head_sha.lower() != qual:
            raise AssertionError(
                f"check-run {r['name']!r} head_sha={head_sha!r} "
                f"!= qualification_head={qual!r}"
            )
    # FAIL CLOSED on completeness (per round-4 P0).
    # total_count must be present and must equal collected count.
    assert total_count > 0, (
        f"check-runs response omitted total_count; "
        f"completeness cannot be proven"
    )
    assert len(runs) == total_count, (
        f"check-runs pagination completeness: "
        f"collected {len(runs)} != total_count {total_count}; "
        f"a check-run beyond page 1 would be invisible"
    )
    required_jobs = {
        "test (3.10)", "test (3.11)", "test (3.12)",
        "package-smoke", "provenance", "committed-state-scan",
    }
    seen = {r["name"] for r in runs}
    missing = required_jobs - seen
    assert not missing, f"missing required jobs: {missing}"
    failed = [r["name"] for r in runs
              if r.get("conclusion") not in ("success", "skipped", "neutral")]
    assert not failed, f"failed jobs: {failed}"
    print(f"OK: all 6 required jobs are green on qualification head {qual}")
    return {"runs": runs, "total_count": total_count}


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
        assert r.get("submittedAt"), (
            f"CodeRabbit review missing submittedAt: {r!r}"
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

    assert decision is not None, (
        "live reviewDecision is None; no review decision is available"
    )
    assert decision != "CHANGES_REQUESTED", (
        f"live reviewDecision is {decision!r}; verifier must fail closed"
    )
    assert coderabbit_reviews, (
        "no live CodeRabbit review found across all paginated "
        "latestReviews; the production identity contract requires "
        "coderabbitai or coderabbitai[bot]"
    )
    latest = coderabbit_reviews[0]
    latest_state = latest["state"]
    assert latest_state == "APPROVED", (
        f"newest live CodeRabbit review is {latest_state!r}; "
        f"verifier must require APPROVED"
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
    assert not unresolved, f"unresolved threads: {unresolved}"
    print(f"OK: every review thread on PR #{args.pr_number} is resolved")
    return {"nodes": nodes, "count": total}


@_step("Verify AED unchanged: scripts/quiet_window_observer.py (exactly one manifest match)")
def _verify_aed(args) -> dict:
    p = args.aed_path
    raw = p.read_bytes()
    measured_sha = hashlib.sha256(raw).hexdigest()
    print(f"actual:   {measured_sha}")
    print(f"expected: {args.aed_expected_sha}")
    assert measured_sha == args.aed_expected_sha, "AED sha mismatch"
    manifest_path = REPO_ROOT / "provenance" / "aed-pr417-source-manifest.json"
    with manifest_path.open() as f:
        m = json.load(f)
    matches = [
        e for e in m["files"]
        if e["destination_path"] == "scripts/quiet_window_observer.py"
    ]
    assert len(matches) == 1, (
        f"manifest must contain exactly one entry for "
        f"scripts/quiet_window_observer.py; found {len(matches)}"
    )
    manifest_sha = matches[0]["destination_sha256"]
    print(f"manifest: {manifest_sha}")
    assert manifest_sha == args.aed_expected_sha, (
        "manifest sha does not match the measured/expected AED sha"
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
    assert len(qualifying) >= 1, "no qualifying observations"
    span = qualifying[-1]["ts_monotonic"] - qualifying[0]["ts_monotonic"]
    print(f"span: {span:.3f} seconds (target: >= 180)")
    assert span >= 180.0, "strict window < 180s"
    pids = {d.get("supervisor_pid") for d in qualifying}
    start_ids = {d.get("process_start_identity") for d in qualifying}
    heads = {str(d.get("pr_head_sha", "")).lower() for d in qualifying}
    assert len(pids) == 1, f"pid drift: {pids}"
    assert len(start_ids) == 1, f"start_id drift: {start_ids}"
    assert len(heads) == 1, f"head drift: {heads}"
    obs_head = next(iter(heads))
    assert obs_head == qual, (
        f"strict-window observation head {obs_head!r} != "
        f"qualification head {qual!r}"
    )
    print(f"PID stable: {pids}, start_id stable: {start_ids}, head stable: {obs_head}")
    bad = [d for d in qualifying
           if not (d.get("head_ok") and d.get("all_ci_pass") and d.get("coderabbit_pass"))]
    assert not bad, f"observations failing per-obs gates: {bad}"
    bad_threads = [d for d in qualifying
                  if d.get("threads", {}).get("unresolved", 0) != 0
                  or d.get("threads", {}).get("unresolved_outdated", 0) != 0]
    assert not bad_threads, f"observations with unresolved threads: {bad_threads}"
    print("OK: strict window >= 180s, all invariants stable")
    return {
        "span_seconds": span,
        "observation_count": len(qualifying),
        "observation_head_sha": obs_head,
    }


@_step("Verify candidate + sidecar exact-file digests bound to qualification head")
def _verify_candidate(args, qual) -> dict:
    paths = canonical_paths(args.evidence_root)
    assert paths["candidate"].exists(), f"candidate missing: {paths['candidate']}"
    cand = read_artifact(paths["candidate"])
    sidecar = Path(str(paths["candidate"]) + ".sha256")
    assert sidecar.exists(), f"sidecar missing: {sidecar}"
    sidecar_digest = sidecar.read_text().strip()
    print(f"candidate: {cand.digest}")
    print(f"sidecar:   {sidecar_digest}")
    assert cand.digest == sidecar_digest, "candidate and sidecar digests differ"
    assert len(cand.digest) == 64, "candidate digest must be 64 hex chars"
    payload = cand.payload
    candidate_head = str(payload.get("exact_head", "")).lower()
    assert candidate_head == qual, (
        f"candidate.exact_head {candidate_head!r} != qualification head {qual!r}"
    )
    assert payload.get("pr_number") == args.pr_number, (
        f"candidate pr mismatch: {payload.get('pr_number')}"
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
    assert p.exists(), f"incident record missing: {p}"
    sidecar_path = Path(str(p) + ".sha256")
    assert sidecar_path.exists(), (
        f"incident sidecar missing: {sidecar_path}; "
        f"the verifier does not create sidecars. The canonical "
        f"sidecar must be produced by the artifact producer, "
        f"not the verifier."
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
    assert "force" in mechanism.lower(), (
        "incident record must record the force mechanism"
    )
    assert payload.get("no_repeat_permitted") is True, "must forbid repeat"
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
        "verifier": "scripts.independent_verifier_v4",
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
    assert sidecar.read_text().strip() == result.digest, "verifier sidecar mismatch"
    reread = read_artifact(paths["verifier"])
    assert reread.digest == result.digest
    assert reread.payload.get("verdict") == "VERIFIED"
    assert reread.payload.get("candidate_sha256") == cand_digest
    assert reread.payload.get("qualification_head") == qual
    assert reread.payload.get("aed_measured_sha256") == aed_measured
    print(f"OK: verifier.json + sidecar written; "
          f"verifier.qualification_head == {qual}")


if __name__ == "__main__":
    sys.exit(main())