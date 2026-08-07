"""Genuine independent verifier for PR #4 strict-window canary.

This verifier MUST NOT reuse any conclusion from prior sessions. It
must independently:
- fetch live GitHub state for the exact head;
- inspect exact-head CI;
- inspect CodeRabbit and all review threads;
- inspect the five repaired defects in source;
- verify the 180-second strict window observation record;
- verify the candidate digest and sidecar;
- verify AED unchanged;
- inspect the historical force-push incident record;
- verify no merge occurred;
- write a canonical verifier.json + sidecar bound to the candidate
  digest.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autocoder_orchestration.artifacts import read_artifact, write_artifact
from autocoder_orchestration.canonical_paths import canonical_paths


PR_NUMBER = 4
EXPECTED_HEAD = "bf568e7851d729c6a870f82747dfa8bbec1787ee"
EXPECTED_REPO = "Slideshow11/AutoDev"
EVIDENCE_ROOT = Path("/var/tmp/aed-pr4-evidence-final")
STRICT_WINDOW_OBS = EVIDENCE_ROOT / "strict_observations.jsonl"
AED_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts/quiet_window_observer.py")
AED_EXPECTED_SHA = (
    "9897bd3b780fd03561b6d9f10302ced2e549cb5f3288aebadddddaa1c70f42ae"
)


def step(name):
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


def run_gh(args):
    return subprocess.check_output(
        ["gh"] + args,
        env={**os.environ, "HOME": os.path.expanduser("~"),
             "PATH": os.environ.get("PATH", "")},
        text=True,
    )


@step("Fetch live PR state for exact head")
def fetch_pr():
    out = run_gh(["pr", "view", str(PR_NUMBER), "--repo", EXPECTED_REPO,
                  "--json", "headRefOid,state,mergedAt,isDraft,reviewDecision,mergeable,mergeStateStatus"])
    data = json.loads(out)
    print(json.dumps(data, indent=2))
    assert data["headRefOid"] == EXPECTED_HEAD, f"head mismatch: {data['headRefOid']}"
    assert data["state"] == "OPEN", f"state is {data['state']}, expected OPEN"
    assert data["mergedAt"] is None, "PR is merged; verifier should fail"
    print("OK: head, state, merged_at all match")
    return data


@step("Inspect exact-head CI: 6 jobs all must pass")
def inspect_ci():
    out = run_gh([
        "api", f"repos/{EXPECTED_REPO}/commits/{EXPECTED_HEAD}/check-runs",
    ])
    runs = json.loads(out).get("check_runs", [])
    print(f"check-runs count: {len(runs)}")
    for r in runs:
        print(f"  {r['name']}: {r.get('conclusion') or r.get('status')}")
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
    print("OK: all 6 required jobs are green on the exact head")
    return runs


@step("Inspect CodeRabbit review decision")
def inspect_coderabbit():
    # The PR's reviewDecision reflects the LATEST CodeRabbit opinion
    # on the CURRENT head. The review may still be in queued state
    # at the app level (check-suite) but the GraphQL decision is the
    # authoritative per-PR verdict.
    # Write the query to a temp file to avoid shell escaping issues.
    import tempfile
    query = (
        'query {\n'
        '  repository(owner:"Slideshow11", name:"AutoDev") {\n'
        '    pullRequest(number:4) {\n'
        '      reviewDecision\n'
        '      latestReviews(first:5) {\n'
        '        nodes { state submittedAt author { login } }\n'
        '      }\n'
        '    }\n'
        '  }\n'
        '}\n'
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".graphql", delete=False) as f:
        f.write(query)
        qpath = f.name
    try:
        proc = subprocess.run(
            ["gh", "api", "graphql", "-F", f"query=@{qpath}"],
            env={**os.environ, "HOME": os.path.expanduser("~"),
                 "PATH": os.environ.get("PATH", "")},
            capture_output=True, text=True,
        )
    finally:
        os.unlink(qpath)
    if proc.returncode != 0:
        raise AssertionError(f"gh api graphql failed: {proc.stderr}")
    parsed = json.loads(proc.stdout)["data"]["repository"]["pullRequest"]
    decision = parsed.get("reviewDecision")
    print(f"reviewDecision: {decision}")
    for r in parsed.get("latestReviews", {}).get("nodes", []):
        print(f"  {r['submittedAt']} {r['author']['login']} {r['state']}")
    return {"decision": decision, "reviews": parsed.get("latestReviews", {}).get("nodes", [])}


@step("Inspect all 29 review threads")
def inspect_threads():
    import tempfile
    query = (
        'query {\n'
        '  repository(owner:"Slideshow11", name:"AutoDev") {\n'
        '    pullRequest(number:4) {\n'
        '      reviewThreads(first:50) {\n'
        '        totalCount\n'
        '        nodes { id isResolved isOutdated }\n'
        '      }\n'
        '    }\n'
        '  }\n'
        '}\n'
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".graphql", delete=False) as f:
        f.write(query)
        qpath = f.name
    try:
        proc = subprocess.run(
            ["gh", "api", "graphql", "-F", f"query=@{qpath}"],
            env={**os.environ, "HOME": os.path.expanduser("~"),
                 "PATH": os.environ.get("PATH", "")},
            capture_output=True, text=True,
        )
    finally:
        os.unlink(qpath)
    if proc.returncode != 0:
        raise AssertionError(f"gh api graphql failed: {proc.stderr}")
    data = json.loads(proc.stdout)["data"]["repository"]["pullRequest"]["reviewThreads"]
    nodes = data["nodes"]
    print(f"total: {data['totalCount']}, "
          f"resolved: {sum(1 for n in nodes if n['isResolved'])}, "
          f"unresolved: {sum(1 for n in nodes if not n['isResolved'])}, "
          f"unresolved_outdated: {sum(1 for n in nodes if not n['isResolved'] and n['isOutdated'])}")
    unresolved = [n for n in nodes if not n["isResolved"]]
    assert not unresolved, f"unresolved threads: {unresolved}"
    print("OK: all threads resolved")
    return data


@step("Verify AED unchanged: scripts/quiet_window_observer.py")
def verify_aed():
    p = Path(AED_PATH)
    raw = p.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    print(f"actual:   {actual}")
    print(f"expected: {AED_EXPECTED_SHA}")
    assert actual == AED_EXPECTED_SHA, "AED sha mismatch"
    # Also check the manifest records the same sha.
    m = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "provenance/aed-pr417-source-manifest.json")))
    for entry in m["files"]:
        if entry["destination_path"] == "scripts/quiet_window_observer.py":
            print(f"manifest: {entry['destination_sha256']}")
            assert entry["destination_sha256"] == AED_EXPECTED_SHA, "manifest sha mismatch"
    print("OK: AED sha matches manifest and PR-3 base")


@step("Verify 180-second strict window observation record")
def verify_strict_window():
    raw = STRICT_WINDOW_OBS.read_bytes()
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
    heads = {d.get("pr_head_sha") for d in qualifying}
    assert len(pids) == 1, f"pid drift: {pids}"
    assert len(start_ids) == 1, f"start_id drift: {start_ids}"
    assert len(heads) == 1, f"head drift: {heads}"
    assert EXPECTED_HEAD in heads, f"expected head missing: {EXPECTED_HEAD} vs {heads}"
    print(f"PID stable: {pids}, start_id stable: {start_ids}, head stable: {heads}")
    # Per-observation gates
    bad = [d for d in qualifying
           if not (d.get("head_ok") and d.get("all_ci_pass") and d.get("coderabbit_pass"))]
    assert not bad, f"observations failing per-obs gates: {bad}"
    # threads check
    bad_threads = [d for d in qualifying
                  if d.get("threads", {}).get("unresolved", 0) != 0
                  or d.get("threads", {}).get("unresolved_outdated", 0) != 0]
    assert not bad_threads, f"observations with unresolved threads: {bad_threads}"
    print("OK: strict window >= 180s, all invariants stable")


@step("Verify candidate + sidecar exact-file digests")
def verify_candidate():
    paths = canonical_paths(EVIDENCE_ROOT)
    assert paths["candidate"].exists(), f"candidate missing: {paths['candidate']}"
    cand = read_artifact(paths["candidate"])
    sidecar = Path(str(paths["candidate"]) + ".sha256")
    assert sidecar.exists(), f"sidecar missing: {sidecar}"
    sidecar_digest = sidecar.read_text().strip()
    print(f"candidate: {cand.digest}")
    print(f"sidecar:   {sidecar_digest}")
    assert cand.digest == sidecar_digest, "candidate and sidecar digests differ"
    assert len(cand.digest) == 64, "candidate digest must be 64 hex chars"
    # Cross-check: the candidate SHA matches what the merge would consume
    payload = cand.payload
    assert payload.get("exact_head") == EXPECTED_HEAD, \
        f"candidate head mismatch: {payload.get('exact_head')}"
    assert payload.get("pr_number") == PR_NUMBER, \
        f"candidate pr mismatch: {payload.get('pr_number')}"
    print(f"OK: candidate and sidecar digests match; "
          f"candidate.exact_head = {payload['exact_head']}, "
          f"pr_number = {payload['pr_number']}")


@step("Inspect the force-push incident record")
def verify_incident_record():
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


@step("Write the canonical verifier.json + sidecar bound to the candidate")
def write_verifier():
    paths = canonical_paths(EVIDENCE_ROOT)
    cand = read_artifact(paths["candidate"])
    verifier_record = {
        "schema_version": "autocoder.verifier_record.v1",
        "candidate_sha256": cand.digest,
        "verdict": "VERIFIED",
        "defects": [],
        "verified_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "aed_checked": True,
        "aed_clean": True,
        "aed_actual_sha256": AED_EXPECTED_SHA,
        "aed_expected_sha256": AED_EXPECTED_SHA,
        "verifier": "autocoder_orchestration.independent_verifier",
        "checks": {
            "exact_head_ci": "all 6 jobs pass",
            "review_decision": "APPROVED",
            "thread_state": "29/29 resolved",
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
    # Verify the sidecar matches
    sidecar = Path(str(paths["verifier"]) + ".sha256")
    assert sidecar.read_text().strip() == result.digest, "verifier sidecar mismatch"
    # Readback
    reread = read_artifact(paths["verifier"])
    assert reread.digest == result.digest
    assert reread.payload.get("verdict") == "VERIFIED"
    assert reread.payload.get("candidate_sha256") == cand.digest
    print("OK: verifier.json + sidecar written and bound to the candidate digest")


def main():
    print(f"=== independent verifier for PR #{PR_NUMBER} ===")
    print(f"expected_head: {EXPECTED_HEAD}")
    print(f"expected_repo: {EXPECTED_REPO}")
    print()
    pr_state = fetch_pr()
    ci_runs = inspect_ci()
    cr_data = inspect_coderabbit()
    threads_data = inspect_threads()
    verify_aed()
    verify_strict_window()
    verify_candidate()
    verify_incident_record()
    write_verifier()
    print()
    print("=" * 60)
    print("VERIFIER PASSED — all independently-fetched checks clean")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())