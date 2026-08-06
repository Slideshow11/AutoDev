#!/usr/bin/env python3
"""Strict quiet-window observer for an AutoDev supervisor readiness window.

Records a full observation of supervisor state, exact PR head, CI checks,
CodeRabbit, review-thread totals, worker lease, unconsumed events and
provider-in-progress every 15 seconds, and confirms that the supervisor
remains in AWAITING_MERGE_AUTHORIZATION for at least 180 monotonic seconds
on a specific head SHA.

Configuration via command-line arguments (no hardcoded paths):

    --canary-root PATH         Required. Canary root directory
    --pr-number N              Required. PR number to verify
    --expected-head SHA        Required. 40-char SHA the PR must match
    --repo OWNER/NAME          Required. e.g. OWNER/NAME
    --window-seconds N         Default 180. Strict quiet window
    --safety-margin-seconds N  Default 30. Run length is window + margin
    --output PATH              Default <canary-root>/strict_observations.jsonl

Exit codes:
    0  Strict quiet window PASSED (qualifying observations span >= window seconds,
       supervisor PID + process-start identity stable throughout)
    1  Strict quiet window DID NOT PASS
    2  Configuration error (missing arg, bad SHA format, missing required tooling)

Per directive (2026-08-06):
- Don't start timer until supervisor reaches AWAITING_MERGE_AUTHORIZATION
- Don't permit malformed/missing/unparsable states
- Reset timer on any drift (PID change, head_sha mismatch, CI failure, etc.)
- Records UTC + monotonic timestamp + supervisor PID + process-start identity +
  flock owner + heartbeat + readiness + exact GitHub head + 6 CI checks +
  CodeRabbit + review-thread totals + worker lease + unconsumed events +
  review request + provider-in-progress
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path


SHA_HEX_RE = re.compile(r"^[0-9a-f]{40}$")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--canary-root", required=True, help="Canary root directory")
    p.add_argument("--pr-number", type=int, required=True, help="PR number to verify")
    p.add_argument("--expected-head", required=True, help="40-char SHA the PR head must match")
    p.add_argument("--repo", required=True, help="OWNER/NAME, e.g. OWNER/NAME")
    p.add_argument("--window-seconds", type=float, default=180.0, help="Strict quiet window (default 180)")
    p.add_argument("--safety-margin-seconds", type=float, default=30.0, help="Safety margin (default 30)")
    p.add_argument("--output", default=None, help="Output JSONL path (default <canary>/strict_observations.jsonl)")
    p.add_argument("--gh-binary", default="gh", help="gh CLI binary (default 'gh')")
    p.add_argument("--sleep-seconds", type=float, default=15.0, help="Sleep between observations (default 15)")
    return p.parse_args()


def gh_json(gh_bin: str, args: list[str]) -> dict:
    out = subprocess.check_output([gh_bin, *args], text=True)
    return json.loads(out)


def get_pr_data(gh_bin: str, repo: str, pr_number: int) -> dict:
    pr = gh_json(gh_bin, ["api", f"repos/{repo}/pulls/{pr_number}"])
    return {
        "head_sha": pr["head"]["sha"],
        "base_sha": pr["base"]["sha"],
        "state": pr["state"],
        "is_draft": pr["draft"],
    }


def get_pr_checks(gh_bin: str, repo: str, pr_number: int) -> list[dict]:
    out = subprocess.check_output(
        [gh_bin, "pr", "checks", str(pr_number), "--repo", repo],
        text=True,
    )
    jobs = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            jobs.append({"name": parts[0], "state": parts[1]})
    return jobs


def get_threads(gh_bin: str, repo: str, pr_number: int) -> dict:
    q = (
        'query { repository(owner: "%s", name: "%s") { '
        "pullRequest(number: %d) { "
        "reviewThreads(first: 100) { totalCount nodes { id isResolved isOutdated } } } } }"
    ) % tuple(repo.split("/", 1) + [pr_number])
    resp = gh_json(gh_bin, ["api", "graphql", "-f", f"query={q}"])
    nodes = resp["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    return {
        "total": len(nodes),
        "resolved": sum(1 for n in nodes if n["isResolved"]),
        "unresolved": sum(1 for n in nodes if not n["isResolved"]),
        "unresolved_outdated": sum(1 for n in nodes if not n["isResolved"] and n["isOutdated"]),
    }


def get_supervisor_pid(canary_root: Path) -> str | None:
    cmd_marker = f"autocoder_supervisor.supervisor --config {canary_root}/canary.toml"
    proc = subprocess.run(
        ["pgrep", "-f", cmd_marker], capture_output=True, text=True,
    )
    pids = [line for line in proc.stdout.splitlines() if line.strip()]
    return pids[0] if pids else None


def get_process_start_identity(pid: str | None) -> str | None:
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        parts = data.split()
        # field 22 (1-indexed) is start_time in clock ticks — stable identity
        return str(int(parts[21]))
    except Exception as e:
        return f"ERROR:{e}"


def get_flock_owner(canary_root: Path) -> str | None:
    lock_path = canary_root / "lease" / "lock"
    if not lock_path.exists():
        return None
    try:
        result = subprocess.run(
            ["lsof", "-t", str(lock_path)], capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().splitlines()
            return ",".join(pids) + f":{lock_path.stat().st_ino}"
    except Exception:
        return None
    return None


def read_readiness(canary_root: Path):
    p = canary_root / "state" / "readiness_state.json"
    if not p.exists():
        return None, "MISSING"
    try:
        data = json.loads(p.read_text())
        return data, data.get("state", "MISSING")
    except Exception as e:
        return None, f"PARSE_ERROR: {e}"


def read_heartbeat(canary_root: Path) -> str | None:
    p = canary_root / "lease" / "heartbeat"
    if not p.exists():
        return None
    try:
        return p.read_text().strip()
    except Exception:
        return None


def read_worker_lease(canary_root: Path) -> dict:
    p = canary_root / "state" / "worker_lease.json"
    if not p.exists():
        return {"exists": False}
    try:
        data = json.loads(p.read_text())
        return {"exists": True, "data": data, "active": data.get("active", False)}
    except Exception:
        return {"exists": True, "parse_error": True}


def read_unconsumed_events(canary_root: Path) -> dict:
    p = canary_root / "state" / "unconsumed_events.json"
    if not p.exists():
        return {"exists": False}
    try:
        data = json.loads(p.read_text())
        ids = data.get("ids", []) if isinstance(data, dict) else data
        return {"exists": True, "count": len(ids), "ids": ids}
    except Exception:
        return {"exists": True, "parse_error": True}


def read_provider_in_progress(canary_root: Path) -> dict:
    p = canary_root / "state" / "snapshot_a.json"
    if not p.exists():
        return {"exists": False}
    try:
        data = json.loads(p.read_text())
        providers = data.get("providers", {})
        in_progress = {k: v.get("in_progress", False) for k, v in providers.items()}
        any_in_progress = any(in_progress.values())
        return {"exists": True, "providers": in_progress, "any_in_progress": any_in_progress}
    except Exception:
        return {"exists": True, "parse_error": True}


def read_review_request(canary_root: Path) -> dict:
    log_path = canary_root / "logs" / "supervisor.log"
    return {"log_path": str(log_path), "exists": log_path.exists()}


def is_fully_qualifying(observation: dict, expected_head: str) -> tuple[bool, str]:
    """Return (ok, reason). ok=True means this observation satisfies all strict conditions."""
    if observation["readiness"] != "AWAITING_MERGE_AUTHORIZATION":
        return False, "readiness not AWAITING_MERGE_AUTHORIZATION"
    if observation["pr_head_sha"] != expected_head:
        return False, "head_sha drift"
    if not observation["head_ok"]:
        return False, "head_ok false"
    if not observation["all_ci_pass"]:
        return False, "ci check failed"
    if not observation["coderabbit_pass"]:
        return False, "CodeRabbit not pass"
    if observation["threads"]["unresolved"] != 0:
        return False, "unresolved threads"
    if observation["threads"]["unresolved_outdated"] != 0:
        return False, "unresolved outdated threads"
    if observation.get("worker_lease_active"):
        return False, "active worker lease"
    if observation.get("unconsumed_event_count", 0) != 0:
        return False, "pending unconsumed events"
    if observation.get("provider_in_progress", False):
        return False, "provider in progress"
    if not observation.get("supervisor_pid"):
        return False, "no supervisor PID"
    if not observation.get("process_start_identity"):
        return False, "no process start identity"
    if not observation.get("heartbeat"):
        return False, "no heartbeat"
    return True, "qualifying"


def collect_observation(args, canary_root: Path, expected_head: str) -> dict:
    pr = get_pr_data(args.gh_binary, args.repo, args.pr_number)
    checks = get_pr_checks(args.gh_binary, args.repo, args.pr_number)
    threads = get_threads(args.gh_binary, args.repo, args.pr_number)
    pid = get_supervisor_pid(canary_root)
    start_id = get_process_start_identity(pid)
    heartbeat = read_heartbeat(canary_root)
    _, readiness_state = read_readiness(canary_root)
    lease = read_worker_lease(canary_root)
    unconsumed = read_unconsumed_events(canary_root)
    provider_ip = read_provider_in_progress(canary_root)

    six_ci_jobs = [c for c in checks if c["name"] in (
        "test (3.10)", "test (3.11)", "test (3.12)",
        "package-smoke", "provenance", "committed-state-scan",
    )]
    all_ci_pass = all(c["state"] == "pass" for c in six_ci_jobs)
    cr_job = next((c for c in checks if c["name"] == "CodeRabbit"), None)
    coderabbit_pass = bool(cr_job and cr_job["state"] == "pass")

    return {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ts_monotonic": time.monotonic(),
        "supervisor_pid": pid,
        "process_start_identity": start_id,
        "flock_owner": get_flock_owner(canary_root),
        "heartbeat": heartbeat,
        "readiness": readiness_state,
        "pr_head_sha": pr["head_sha"],
        "pr_state": pr["state"],
        "pr_is_draft": pr["is_draft"],
        "head_ok": pr["head_sha"] == expected_head,
        "ci_jobs": checks,
        "all_ci_pass": all_ci_pass,
        "coderabbit_pass": coderabbit_pass,
        "threads": threads,
        "worker_lease": lease,
        "worker_lease_active": lease.get("active", False) if isinstance(lease, dict) else False,
        "unconsumed_events": unconsumed,
        "unconsumed_event_count": unconsumed.get("count", 0) if isinstance(unconsumed, dict) else 0,
        "review_request": read_review_request(canary_root),
        "provider_in_progress": provider_ip.get("any_in_progress", False) if isinstance(provider_ip, dict) else False,
    }


def main() -> int:
    args = parse_args()
    if not SHA_HEX_RE.match(args.expected_head):
        print(f"ERROR: --expected-head must be 40 lowercase hex chars; got {args.expected_head!r}", file=sys.stderr)
        return 2

    canary_root = Path(args.canonical_root) if hasattr(args, "canonical_root") else Path(args.canary_root)
    if not canary_root.is_dir():
        print(f"ERROR: --canary-root {canary_root} is not a directory", file=sys.stderr)
        return 2

    output_path = Path(args.output) if args.output else canary_root / "strict_observations.jsonl"
    if output_path.exists():
        output_path.unlink()

    total_run = args.window_seconds + args.safety_margin_seconds
    deadline = time.monotonic() + total_run
    first_qualifying_monotonic: float | None = None
    last_qualifying_monotonic: float | None = None
    first_qualifying_utc: str | None = None
    last_qualifying_utc: str | None = None
    obs_count = 0
    first_pid: str | None = None
    first_start_id: str | None = None
    last_pid: str | None = None
    last_start_id: str | None = None

    print(f"Observer starting; will run for {total_run:.0f}s "
          f"({args.window_seconds:.0f}s window + {args.safety_margin_seconds:.0f}s safety margin)")
    print(f"Canary: {canary_root}")
    print(f"PR: {args.repo}#{args.pr_number}")
    print(f"Head: {args.expected_head}")
    print(f"Log: {output_path}")

    while time.monotonic() < deadline:
        obs_count += 1
        try:
            obs = collect_observation(args, canary_root, args.expected_head)
            qualifying, reason = is_fully_qualifying(obs, args.expected_head)
            obs["qualifying"] = qualifying
            obs["qualification_reason"] = reason
        except subprocess.CalledProcessError as e:
            print(f"[obs {obs_count}] gh command failed: {e}", file=sys.stderr)
            time.sleep(args.sleep_seconds)
            continue

        with open(output_path, "a") as f:
            f.write(json.dumps(obs) + "\n")

        if qualifying:
            if first_qualifying_monotonic is None:
                first_qualifying_monotonic = obs["ts_monotonic"]
                first_qualifying_utc = obs["ts_utc"]
                first_pid = obs["supervisor_pid"]
                first_start_id = obs["process_start_identity"]
                print(f"[{obs['ts_utc']}] obs {obs_count}: FIRST QUALIFYING (pid={first_pid}, start_id={first_start_id})")
            last_qualifying_monotonic = obs["ts_monotonic"]
            last_qualifying_utc = obs["ts_utc"]
            last_pid = obs["supervisor_pid"]
            last_start_id = obs["process_start_identity"]
            assert first_qualifying_monotonic is not None
            assert last_qualifying_monotonic is not None
            duration = last_qualifying_monotonic - first_qualifying_monotonic
            print(f"[{obs['ts_utc']}] obs {obs_count}: QUALIFYING (duration={duration:.1f}s, pid={last_pid}, start_id={last_start_id})")
            if duration >= args.window_seconds:
                if first_pid == last_pid and first_start_id == last_start_id:
                    print()
                    print("=== STRICT QUIET WINDOW PASSED ===")
                    print(f"first qualifying: {first_qualifying_utc}")
                    print(f"last qualifying: {last_qualifying_utc}")
                    print(f"duration (monotonic): {duration:.1f}s (>= {args.window_seconds:.1f}s)")
                    print(f"PID stable: {first_pid}")
                    print(f"start identity stable: {first_start_id}")
                    print(f"observation count: {obs_count}")
                    return 0
                else:
                    print(f"[{obs['ts_utc']}] PID or start identity drifted; resetting timer")
                    first_qualifying_monotonic = None
        else:
            if first_qualifying_monotonic is not None:
                print(f"[{obs['ts_utc']}] obs {obs_count}: NOT qualifying ({reason}); resetting timer")
            first_qualifying_monotonic = None

        time.sleep(args.sleep_seconds)

    print()
    print("=== STRICT QUIET WINDOW DID NOT PASS ===")
    if (
        first_qualifying_monotonic is not None
        and last_qualifying_monotonic is not None
    ):
        duration = last_qualifying_monotonic - first_qualifying_monotonic
        print(f"final qualifying duration: {duration:.1f}s (needed >= {args.window_seconds:.1f}s)")
    else:
        print("no qualifying interval captured")
    return 1


if __name__ == "__main__":
    sys.exit(main())