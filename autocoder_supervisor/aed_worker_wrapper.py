#!/usr/bin/env python3
"""Round-51/C19: worker wrapper that captures the worker's
strict machine-readable result envelope and writes the
canonical WorkerResultArtifact. This removes the worker's
final filesystem tool-call dependency: the worker only
needs to emit the envelope text, and the wrapper persists
the artifact to the per-attempt path.

Envelope format (emitted as the LAST block of the worker's
final response):

    ===WORKER_RESULT_ENVELOPE===
    {
      "schema_version": "autocoder.worker_envelope.v1",
      "attempt_id": "att-<TIMESTAMP>-<PID>",
      "claim_id": "att-<TIMESTAMP>-<PID>",
      "directive_digest": "<sha256 hex>",
      "directive_id": "<uuid>",
      "result_type": "NO_CHANGES_REQUIRED | REPAIR_PUSHED | REPAIR_COMMIT_PRODUCED | COMMIT_PRODUCED_NOT_PUSHED | WORKER_EXECUTION_FAILED",
      "produced_commit_shas": ["<sha>", ...] | [],
      "pushed_commit_shas": ["<sha>", ...] | [],
      "completed_at": "<ISO-8601 UTC>",
      "prelaunch_head": "<40-char hex>",
      "no_changes_required_proof": {
        "findings": [{"finding_id": "thread:...", "disposition": "ALREADY_SATISFIED|REPAIRED|SUPERSEDED|STILL_ACTIONABLE|INCOMPLETE_EVIDENCE", ...}],
        "source": "round50_envelope_parser"
      }
    }
    ===END_ENVELOPE===

Usage:
    aed_worker_wrapper.py \
        --attempt-id att-20260812T120000Z-12345 \
        --directive-digest <sha256> \
        --directive-id <uuid> \
        --directive-path <path> \
        --prelaunch-head <sha> \
        --result-artifact-path <path> \
        --stdout-log-path <path> \
        -- <original hermes command and args>
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


ENVELOPE_OPEN = "===WORKER_RESULT_ENVELOPE==="
ENVELOPE_CLOSE = "===END_ENVELOPE==="

ENVELOPE_RE = re.compile(
    re.escape(ENVELOPE_OPEN) + r"\s*(\{.*?\})\s*" + re.escape(ENVELOPE_CLOSE),
    re.DOTALL,
)


def _resolve_wrapper_argv(wrapper_kwargs: dict, original_cmd: list) -> list:
    """Build the cmd list that invokes aed_worker_wrapper with the
    original hermes cmd passed as positional args after ``--``.

    Round-51/C19: the supervisor uses this helper to wrap the
    configured worker_command template. The wrapper captures
    stdout, parses the WORKER_RESULT_ENVELOPE, and writes the
    canonical WorkerResultArtifact to disk.
    """
    wrapper_path = Path(__file__).resolve()
    wrapper_argv = [sys.executable, str(wrapper_path)]
    for k, v in wrapper_kwargs.items():
        if v is None or v == "":
            continue
        wrapper_argv.append(f"--{k.replace('_', '-')}")
        wrapper_argv.append(str(v))
    wrapper_argv.append("--")
    wrapper_argv.extend(original_cmd)
    return wrapper_argv


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_file_sha256(path: Path) -> str:
    import hashlib
    if not path.is_file():
        return ""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Worker wrapper that captures the result envelope and writes the canonical artifact."
    )
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--directive-digest", default="")
    parser.add_argument("--directive-id", default="")
    parser.add_argument("--directive-path", default="")
    parser.add_argument("--prelaunch-head", default="")
    parser.add_argument(
        "--claim-id",
        default="",
        help="The WorkerAttemptRecord.claim_id (lease or directive UUID) the wrapper MUST write into the canonical artifact.",
    )
    parser.add_argument(
        "--result-artifact-path",
        required=True,
        help="Path where the canonical WorkerResultArtifact must be written.",
    )
    parser.add_argument(
        "--stdout-log-path",
        required=True,
        help="Path where the worker's stdout is teed (preserved for forensic chain-of-custody).",
    )
    parser.add_argument(
        "--orch-result-artifact-path",
        default="",
        help="Optional second canonical-artifact path under the orch worker_attempts dir.",
    )
    parser.add_argument(
        "--expected-branch",
        default="feat/review-repair-relay-v1",
    )
    parser.add_argument(
        "--pr-number",
        type=int,
        default=5,
    )
    parser.add_argument(
        "--repo",
        default="Slideshow11/AutoDev",
    )
    parser.add_argument(
        "--result-contract-id",
        default="",
        help="Round-54/C22 continuation §2: the prelaunch "
        "result contract id the worker must echo in its "
        "envelope. The wrapper records this contract id "
        "in artifact['extra']['result_contract_id'] for "
        "the supervisor to validate the worker's envelope "
        "against.",
    )
    parser.add_argument(
        "--result-type-default",
        default="WORKER_EXECUTION_FAILED",
        help="Fallback result_type when the worker emits no envelope.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="Worker timeout in seconds (default 15 minutes).",
    )
    parser.add_argument(
        "--cwd",
        default="",
        help="Working directory for the worker subprocess.",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        help="Environment overrides, e.g. KEY=VALUE. May be passed multiple times.",
    )
    parser.add_argument(
        "child_argv",
        nargs=argparse.REMAINDER,
        help="The original worker command (after --).",
    )
    args = parser.parse_args()

    # Strip leading -- if present
    child_argv = list(args.child_argv)
    if child_argv and child_argv[0] == "--":
        child_argv = child_argv[1:]
    if not child_argv:
        print(
            "aed_worker_wrapper: no child command after --",
            file=sys.stderr,
            flush=True,
        )
        return 2

    # Build child env
    child_env = os.environ.copy()
    for kv in args.env:
        if "=" in kv:
            k, v = kv.split("=", 1)
            child_env[k] = v

    # Open stdout log for tee
    stdout_log_path = Path(args.stdout_log_path)
    stdout_log_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_log_fh = stdout_log_path.open("wb", buffering=0)
    captured = bytearray()

    def _tee_pipe(fd):
        """Read from fd, write to log and capture buffer."""
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                captured.extend(chunk)
                try:
                    stdout_log_fh.write(chunk)
                except OSError:
                    pass
        except OSError:
            pass

    import threading
    import select

    cwd = args.cwd or os.getcwd()
    start = time.time()
    try:
        proc = subprocess.Popen(
            child_argv,
            cwd=cwd,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        # Round-167 P2: write a WORKER_EXECUTION_FAILED artifact and
        # close stdout_log_fh before returning. The supervisor ingests
        # the canonical artifact at args.result_artifact_path; a bare
        # return 127 leaves no worker result and the launch failure is
        # unobservable. Also close stdout_log_fh to avoid a file-handle
        # leak on this failure path.
        print(f"aed_worker_wrapper: failed to launch: {e}", file=sys.stderr)
        try:
            stdout_log_fh.close()
        except Exception:
            pass
        _wrapper_pid = os.getpid()
        _resolved_attempt_id = f"{args.attempt_id}-{_wrapper_pid}"
        _resolved_claim_id = args.claim_id or _resolved_attempt_id
        _failure_artifact = {
            "schema_version": "autocoder.worker_result.v1",
            "attempt_id": _resolved_attempt_id,
            "claim_id": _resolved_claim_id,
            "directive_digest": args.directive_digest,
            "result_type": "WORKER_EXECUTION_FAILED",
            "produced_commit_shas": [],
            "pushed_commit_shas": [],
            "completed_at": now_iso(),
            "no_changes_required_proof": None,
            "tests_run": 0,
            "tests_passed": 0,
            "attempt_nonce": args.attempt_id.rsplit("-", 1)[0],
            "repo": args.repo,
            "pr_number": args.pr_number,
            "expected_branch": args.expected_branch,
            "prelaunch_head": args.prelaunch_head,
            "worker_pid": _wrapper_pid,
            "extra": {
                "launch_failure": str(e),
                "worker_result_envelope_seen": False,
                "worker_envelope_source": "round167_p2_launch_failure",
                "envelope_status": "missing",
                "envelope_match_count": 0,
                "result_contract_id": args.result_contract_id or "",
            },
        }
        def _resolve_pid_lf(path_str: str) -> str:
            if "<PID>" in path_str:
                return path_str.replace("<PID>", str(_wrapper_pid))
            return path_str
        try:
            target = Path(_resolve_pid_lf(args.result_artifact_path))
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                    tmp.write(json.dumps(_failure_artifact, indent=2))
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_path, target)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as write_err:
            print(
                f"aed_worker_wrapper: failed to write launch-failure artifact: {write_err}",
                file=sys.stderr,
            )
        return 127

    # Read in a thread (blocking)
    def _reader():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                captured.extend(chunk)
                try:
                    stdout_log_fh.write(chunk)
                except OSError:
                    pass
        except Exception:
            pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    exit_code = None
    while True:
        rc = proc.poll()
        if rc is not None:
            exit_code = rc
            break
        if time.time() - start > args.timeout:
            try:
                proc.kill()
            except Exception:
                pass
            exit_code = -9
            break
        time.sleep(0.5)

    # Drain remaining output
    t.join(timeout=5)

    try:
        stdout_log_fh.close()
    except Exception:
        pass

    # Parse envelope
    envelope = None
    try:
        text = captured.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    # Round-54/C22 continuation §6 (defect 1): count
    # envelope occurrences. Exactly one valid envelope is
    # required. Zero envelopes is WORKER_RESULT_MISSING.
    # Multiple envelopes is WORKER_RESULT_INVALID
    # (fail closed). Capture the envelope AND the
    # envelope_status / envelope_match_count as locals.
    # The artifact dict does not exist yet — we must NOT
    # write to artifact["extra"] here. Apply these locals
    # AFTER the artifact is built below.
    _all_matches = list(ENVELOPE_RE.finditer(text))
    _envelope_match_count = len(_all_matches)
    _envelope_status = "present" if _envelope_match_count == 1 else (
        "missing" if _envelope_match_count == 0 else "multiple"
    )
    if _envelope_match_count == 1:
        m = _all_matches[0]
        try:
            envelope = json.loads(m.group(1))
            if not isinstance(envelope, dict):
                envelope = {"_envelope_parse_error": "envelope is not a dict",
                            "_raw": m.group(1)[:2000]}
        except Exception as e:
            envelope = {"_envelope_parse_error": str(e), "_raw": m.group(1)[:2000]}
    elif _envelope_match_count > 1:
        # Multiple envelopes: WORKER_RESULT_INVALID.
        # Use the first for forensic reference only.
        try:
            _first = json.loads(_all_matches[0].group(1))
            if isinstance(_first, dict):
                envelope = {
                    "_envelope_match_count": _envelope_match_count,
                    "_envelope_parse_warning": (
                        f"Multiple envelopes found ({_envelope_match_count}); "
                        f"treating as WORKER_RESULT_INVALID"
                    ),
                }
            else:
                envelope = {"_envelope_parse_error": "first envelope not dict",
                            "_raw": _all_matches[0].group(1)[:2000]}
        except Exception:
            envelope = {"_envelope_parse_error": "first envelope JSON parse failed",
                        "_raw": _all_matches[0].group(1)[:2000]}
    # Note: result_type is finalized AFTER the artifact is
    # built, so we cannot overwrite WORKER_RESULT_INVALID
    # from the envelope's result_type field. See
    # _finalize_result_type() below.

    # The wrapper resolves the canonical attempt_id and

    # The wrapper resolves the canonical attempt_id and
    # claim_id by appending the wrapper's OWN PID (not
    # hermes's PID) to the attempt_id_prefix the supervisor
    # passed in. The wrapper IS the process the supervisor
    # launched and tracks in its WorkerAttemptRecord. The
    # worker (hermes) is a child of the wrapper and uses
    # its own PID for the filename suffix; the wrapper
    # substitutes the wrapper's PID into both the filename
    # AND the canonical artifact's attempt_id/claim_id so
    # the supervisor's identity validation passes.
    _wrapper_pid = os.getpid()
    _resolved_attempt_id = f"{args.attempt_id}-{_wrapper_pid}"
    # claim_id must equal the WorkerAttemptRecord.claim_id
    # for the C18 identity validation to pass. The supervisor
    # passes the lease-id or directive-UUID via --claim-id.
    # If the supervisor did not pass one (e.g. legacy path),
    # fall back to the resolved attempt_id (which still
    # passes the attempt_id check but will fail the claim_id
    # check — a clear signal of a misconfigured launch).
    _resolved_claim_id = args.claim_id or _resolved_attempt_id

    # Round-54/C22 continuation §6: build the canonical
    # WorkerResultArtifact. The envelope_status /
    # envelope_match_count / result_type are derived from
    # the envelope capture above, NOT from the envelope's
    # result_type field. This prevents the worker from
    # overwriting WORKER_RESULT_INVALID by including a
    # valid result_type in the envelope body.
    if _envelope_match_count > 1:
        _final_result_type = "WORKER_RESULT_INVALID"
    elif _envelope_match_count == 0:
        _final_result_type = args.result_type_default
    else:
        # Exactly one envelope. The result_type comes
        # from the envelope's body if present and valid,
        # otherwise the default.
        _final_result_type = args.result_type_default
        if isinstance(envelope, dict) and "_envelope_parse_error" not in envelope:
            _env_rt = envelope.get("result_type")
            if _env_rt in (
                "NO_CHANGES_REQUIRED", "REPAIR_PUSHED",
                "REPAIR_COMMIT_PRODUCED", "COMMIT_PRODUCED_NOT_PUSHED",
                "WORKER_EXECUTION_FAILED",
            ):
                _final_result_type = _env_rt
    # Build the canonical WorkerResultArtifact
    artifact = {
        "schema_version": "autocoder.worker_result.v1",
        "attempt_id": _resolved_attempt_id,
        "claim_id": _resolved_claim_id,
        "directive_digest": args.directive_digest,
        "result_type": _final_result_type,
        "produced_commit_shas": (
            list(envelope.get("produced_commit_shas") or [])
            if isinstance(envelope, dict) and _envelope_match_count == 1
            else []
        ),
        "pushed_commit_shas": (
            list(envelope.get("pushed_commit_shas") or [])
            if isinstance(envelope, dict) and _envelope_match_count == 1
            else []
        ),
        "completed_at": (
            envelope.get("completed_at", now_iso())
            if isinstance(envelope, dict) and _envelope_match_count == 1
            else now_iso()
        ),
        "no_changes_required_proof": (
            envelope.get("no_changes_required_proof")
            if (
                isinstance(envelope, dict)
                and _envelope_match_count == 1
                and envelope.get("no_changes_required_proof")
                and "_envelope_parse_error" not in envelope
            )
            else None
        ),
        "tests_run": 0,
        "tests_passed": 0,
        "attempt_nonce": (
            envelope.get("attempt_nonce", args.attempt_id.rsplit("-", 1)[0])
            if isinstance(envelope, dict) and _envelope_match_count == 1
            else args.attempt_id.rsplit("-", 1)[0]
        ),
        "repo": args.repo,
        "pr_number": args.pr_number,
        "expected_branch": args.expected_branch,
        "prelaunch_head": args.prelaunch_head,
        "worker_pid": proc.pid,
        "extra": {
            "worker_result_envelope_seen": envelope is not None,
            "worker_envelope_source": "round51_c19_wrapper",
            "envelope_status": _envelope_status,
            "envelope_match_count": _envelope_match_count,
            # Round-54/C22 continuation §2: persist the
            # prelaunch result contract for the supervisor
            # to validate worker output against.
            "result_contract_id": args.result_contract_id or "",
        },
    }
    # Round-54/C22 continuation: envelope_status and
    # envelope_match_count are set ABOVE during the
    # artifact build (defect 1 fix: the wrapper no longer
    # touches artifact before it exists). No additional
    # post-processing is needed here.

    # Substitute <PID> in the target paths with the
    # wrapper's OWN PID. The wrapper is the process the
    # supervisor launched and tracks in its
    # WorkerAttemptRecord; the supervisor's identity
    # validation expects the artifact's attempt_id to
    # match the file's basename which carries the
    # wrapper's PID (the supervisor's launch PID).
    def _resolve_pid(path_str: str) -> str:
        if "<PID>" in path_str:
            return path_str.replace("<PID>", str(_wrapper_pid))
        return path_str

    # Write the canonical artifact atomically: write to a sibling temp
    # file in the same directory, flush+fsync, then os.replace the
    # final target. This prevents the supervisor from observing a
    # truncated/partial JSON file when it polls the expected path
    # mid-write (round-164 P2).
    def _atomic_write_json(path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write(payload)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    target = Path(_resolve_pid(args.result_artifact_path))
    try:
        _atomic_write_json(target, json.dumps(artifact, indent=2))
    except OSError as e:
        print(f"aed_worker_wrapper: failed to write {target}: {e}", file=sys.stderr)
        return 1

    # Optional second copy under the orch dir
    if args.orch_result_artifact_path:
        orch_target = Path(_resolve_pid(args.orch_result_artifact_path))
        try:
            _atomic_write_json(orch_target, json.dumps(artifact, indent=2))
        except OSError:
            pass

    # Print a one-line summary to stderr (visible to supervisor log)
    print(
        f"aed_worker_wrapper: attempt={args.attempt_id} "
        f"result_type={_final_result_type} "
        f"envelope_seen={envelope is not None} "
        f"envelope_status={_envelope_status} "
        f"envelope_match_count={_envelope_match_count} "
        f"artifact={target} exit_code={exit_code}",
        file=sys.stderr,
        flush=True,
    )

    return exit_code or 0


if __name__ == "__main__":
    sys.exit(main())
