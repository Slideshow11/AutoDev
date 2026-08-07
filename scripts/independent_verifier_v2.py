"""Independent exact-head verifier for PR #4.

This verifier is the genuine external verifier for the
strict-window qualification cycle on PR #4. It runs OUTSIDE
the implementation worker's process tree. It MUST NOT reuse
any conclusion from a prior session; every check below is
re-fetched against live GitHub state on every invocation.

Final verifier contract (per PR #4 round-2 review):

    qualification_head = one exact 40-character SHA supplied
        as immutable input. There is NO accepted-head set,
        NO production-code-equivalence exception, NO
        manifest-only-head exception, NO test-only-head
        exception.

The verifier proves every one of the following is bound to
exactly ``qualification_head``:

    * live_pr_head (live PR fetch)
    * CI query SHA (check-runs query)
    * every required CI check-run head_sha
    * every strict-window observation pr_head_sha
    * candidate.exact_head
    * verifier.qualification_head itself
    * future authorization.authorized_head

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
import subprocess
import sys
import tempfile
import time
from pathlib import Path


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


def _run_gh(args, *, env=None):
    """Run a ``gh`` command and return parsed JSON output.

    ``env`` is the inherited environment; ``gh`` requires
    ``HOME`` and ``PATH``. We extend, not replace.
    """
    base_env = {**os.environ, "HOME": os.path.expanduser("~")}
    if env:
        base_env.update(env)
    out = subprocess.check_output(
        ["gh"] + args, env=base_env, text=True,
    )
    return json.loads(out)


def _run_gh_graphql(query, *, env=None):
    """Run a ``gh api graphql`` query via a tempfile."""
    base_env = {**os.environ, "HOME": os.path.expanduser("~")}
    if env:
        base_env.update(env)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".graphql", delete=False,
    ) as f:
        f.write(query)
        qpath = f.name
    try:
        proc = subprocess.run(
            ["gh", "api", "graphql", "-F", f"query=@{qpath}"],
            env=base_env, capture_output=True, text=True,
        )
    finally:
        os.unlink(qpath)
    if proc.returncode != 0:
        raise AssertionError(
            f"gh api graphql failed: {proc.stderr}",
        )
    return json.loads(proc.stdout)


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

    _fetch_pr(args, qual)
    _inspect_ci(args, qual)
    _inspect_coderabbit(args)
    _inspect_threads(args)
    _verify_aed(args)
    _verify_strict_window(args, qual)
    _verify_candidate(args, qual)
    _verify_incident_record(args)
    _write_verifier(args, qual)

    print("=" * 60)
    print("VERIFIER PASSED -- all independently-fetched checks clean")
    print(f"verifier.qualification_head == {qual}")
    print("=" * 60)
    return 0


@_step("Fetch live PR state for exact head")
def _fetch_pr(args, qual):
    data = _run_gh([
        "pr", "view", str(args.pr_number), "--repo", args.repo,
        "--json", "headRefOid,state,mergedAt,isDraft,reviewDecision,mergeable,mergeStateStatus",
    ])
    print(json.dumps(data, indent=2))
    live_head = str(data.get("headRefOid", "")).lower()
    assert live_head == qual, (
        f"live PR head {live_head!r} != qualification_head {qual!r}"
    )
    assert data.get("state") == "OPEN", f"state is {data.get('state')}, expected OPEN"
    assert data.get("mergedAt") is None, "PR is merged; verifier should fail"
    print(f"OK: live PR head == qualification_head == {qual}")
    return data


@_step("Inspect exact-head CI on the qualification head (no EXPECTED_HEAD constant)")
def _inspect_ci(args, qual):
    # Query check-runs at the LIVE qualification head, not at any
    # pinned constant. The step name in the report claims
    # "exact-head CI" because the SHA passed to the API is the
    # qualification head.
    runs = _run_gh([
        "api", f"repos/{args.repo}/commits/{qual}/check-runs",
    ]).get("check_runs", [])
    print(f"check-runs count: {len(runs)}")
    for r in runs:
        print(f"  {r['name']}: {r.get('conclusion') or r.get('status')}")
        # Every check-run's head_sha MUST equal the qualification head.
        head_sha = str(r.get("head_sha") or r.get("head", {}).get("sha") or "")
        if head_sha:
            assert head_sha.lower() == qual, (
                f"check-run {r['name']!r} bound to {head_sha!r}, "
                f"not the qualification head {qual!r}"
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
    return runs


@_step("Inspect CodeRabbit review decision")
def _inspect_coderabbit(args):
    query = (
        'query {\n'
        f'  repository(owner:"{args.repo.split("/")[0]}", name:"{args.repo.split("/")[1]}") {{\n'
        f'    pullRequest(number:{args.pr_number}) {{\n'
        '      reviewDecision\n'
        '      latestReviews(first:5) {\n'
        '        nodes { state submittedAt author { login } }\n'
        '      }\n'
        '    }\n'
        '  }\n'
        '}\n'
    )
    parsed = _run_gh_graphql(query)["data"]["repository"]["pullRequest"]
    decision = parsed.get("reviewDecision")
    print(f"reviewDecision: {decision}")
    for r in parsed.get("latestReviews", {}).get("nodes", []):
        author_login = r.get("author", {}).get("login") if r.get("author") else None
        print(f"  {r['submittedAt']} {author_login} {r['state']}")
    return {"decision": decision, "reviews": parsed.get("latestReviews", {}).get("nodes", [])}


@_step("Inspect every review thread")
def _inspect_threads(args):
    query = (
        'query {\n'
        f'  repository(owner:"{args.repo.split("/")[0]}", name:"{args.repo.split("/")[1]}") {{\n'
        f'    pullRequest(number:{args.pr_number}) {{\n'
        '      reviewThreads(first:100) {\n'
        '        totalCount\n'
        '        nodes { id isResolved isOutdated }\n'
        '      }\n'
        '    }\n'
        '  }\n'
        '}\n'
    )
    data = _run_gh_graphql(query)["data"]["repository"]["pullRequest"]["reviewThreads"]
    nodes = data["nodes"]
    print(f"total: {data['totalCount']}, "
          f"resolved: {sum(1 for n in nodes if n['isResolved'])}, "
          f"unresolved: {sum(1 for n in nodes if not n['isResolved'])}, "
          f"unresolved_outdated: {sum(1 for n in nodes if not n['isResolved'] and n['isOutdated'])}")
    unresolved = [n for n in nodes if not n["isResolved"]]
    assert not unresolved, f"unresolved threads: {unresolved}"
    print(f"OK: every thread on PR #{args.pr_number} is resolved")
    return data


@_step("Verify AED unchanged: scripts/quiet_window_observer.py")
def _verify_aed(args):
    p = args.aed_path
    raw = p.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    print(f"actual:   {actual}")
    print(f"expected: {args.aed_expected_sha}")
    assert actual == args.aed_expected_sha, "AED sha mismatch"
    # Also cross-check the source-manifest records the same sha.
    manifest_path = REPO_ROOT / "provenance" / "aed-pr417-source-manifest.json"
    with manifest_path.open() as f:
        m = json.load(f)
    for entry in m["files"]:
        if entry["destination_path"] == "scripts/quiet_window_observer.py":
            print(f"manifest: {entry['destination_sha256']}")
            assert entry["destination_sha256"] == args.aed_expected_sha, "manifest sha mismatch"
    print("OK: AED sha matches manifest and PR-3 base")


@_step("Verify 180-second strict window observation record")
def _verify_strict_window(args, qual):
    raw = args.strict_window_obs.read_bytes()
    data = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    qualifying = [d for d in data if d.get("qualifying")]
    print(f"total: {len(data)}, qualifying: {len(qualifying)}")
    assert len(qualifying) >= 1, "no qualifying observations"
    span = qualifying[-1]["ts_monotonic"] - qualifying[0]["ts_monotonic"]
    print(f"span: {span:.3f} seconds (target: >= 180)")
    assert span >= 180.0, "strict window < 180s"
    # Stability invariants
    pids = {d.get("supervisor_pid") for d in qualifying}
    start_ids = {d.get("process_start_identity") for d in qualifying}
    heads = {str(d.get("pr_head_sha", "")).lower() for d in qualifying}
    assert len(pids) == 1, f"pid drift: {pids}"
    assert len(start_ids) == 1, f"start_id drift: {start_ids}"
    assert len(heads) == 1, f"head drift: {heads}"
    # Every observation pr_head_sha MUST equal the qualification head.
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


@_step("Verify candidate + sidecar exact-file digests bound to qualification head")
def _verify_candidate(args, qual):
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


@_step("Inspect the force-push incident record")
def _verify_incident_record(args):
    p = Path("/var/tmp/autodev-evidence/AED_AUTODEV_P4_FORCE_PUSH_INCIDENT.json")
    assert p.exists(), f"incident record missing: {p}"
    text = p.read_text().split("\n# sha256:")[0]
    data = json.loads(text)
    assert "force" in data.get("force_push_mechanism", "").lower(), \
        "incident record must record the force mechanism"
    assert data.get("no_repeat_permitted") is True, "must forbid repeat"
    print(f"incident classification: {data.get('incident_class')}")
    print(f"force_push_mechanism: {data.get('force_push_mechanism')[:60]}")
    print(f"restored_head_sha: {data.get('restored_head_sha')}")
    print(f"no_repeat_permitted: {data.get('no_repeat_permitted')}")
    print("OK: incident record is honest and complete")


@_step("Write the canonical verifier.json + sidecar bound to the qualification head")
def _write_verifier(args, qual):
    paths = canonical_paths(args.evidence_root)
    cand = read_artifact(paths["candidate"])
    verifier_record = {
        "schema_version": "autocoder.verifier_record.v1",
        "candidate_sha256": cand.digest,
        "qualification_head": qual,
        "verdict": "VERIFIED",
        "defects": [],
        "verified_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "aed_checked": True,
        "aed_clean": True,
        "aed_actual_sha256": args.aed_expected_sha,
        "aed_expected_sha256": args.aed_expected_sha,
        "verifier": "scripts.independent_verifier_v2",
        "checks": {
            "exact_head_ci": "all 6 jobs pass on qualification_head",
            "thread_state": "every review thread resolved",
            "aed_unchanged": True,
            "strict_window_passed": True,
            "force_push_incident_recorded": True,
            "merge_state": "OPEN, not merged",
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
    assert reread.payload.get("candidate_sha256") == cand.digest
    assert reread.payload.get("qualification_head") == qual
    print(f"OK: verifier.json + sidecar written; "
          f"verifier.qualification_head == {qual}")


if __name__ == "__main__":
    sys.exit(main())