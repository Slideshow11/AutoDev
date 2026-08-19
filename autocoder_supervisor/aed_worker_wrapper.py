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


def _resolve_pid_in_path(path_str: str, pid: int) -> str:
    """Replace the ``<PID>`` token in a path template with the
    actual wrapper PID. The worker-attempt artifact path uses
    this token so concurrent attempts do not collide.
    """
    if "<PID>" in path_str:
        return path_str.replace("<PID>", str(pid))
    return path_str


def _write_launch_failure_artifact(
    args: argparse.Namespace,
    launch_failure: str,
) -> int:
    """Write a canonical ``WORKER_EXECUTION_FAILED`` artifact
    at ``args.result_artifact_path`` and return 127 so the
    wrapper exits with the conventional launch-failure code.

    Round-1064 P2: extracted from the ``Popen`` failure
    branch into a module-level helper so other pre-launch
    validation paths (worker-hooks-path, malformed
    GIT_CONFIG_COUNT, etc.) can report failures the same
    way. The supervisor ingests the artifact; a bare
    ``return 1`` would leave the launch failure unobservable.
    """
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
            "launch_failure": launch_failure,
            "worker_result_envelope_seen": False,
            "worker_envelope_source": "round167_p2_launch_failure",
            "envelope_status": "missing",
            "envelope_match_count": 0,
            # Round-167/CodeRabbit-P2: emit the full
            # result-contract field set so the supervisor's
            # ``WorkerResultArtifact.validate_against_attempt``
            # does not misclassify a launch failure as a
            # contract violation. ``expected_result_contract_id``
            # echoes the supervisor-owned prelaunch id;
            # ``observed_result_contract_id`` is empty because
            # no envelope was produced; ``result_contract_match``
            # is False with an explicit reason.
            "result_contract_id": args.result_contract_id or "",
            "expected_result_contract_id": args.result_contract_id or "",
            "observed_result_contract_id": "",
            "result_contract_match": False,
            "result_contract_mismatch_reason": (
                "worker did not launch; no envelope was produced"
            ),
        },
    }
    try:
        target = Path(_resolve_pid_in_path(
            args.result_artifact_path, _wrapper_pid,
        ))
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
        "--worker-hooks-path",
        default="",
        help=(
            "Round-697: absolute path to the source-controlled "
            "AED worker-only Git hook directory "
            "(autocoder_worker_hooks/). When provided, the "
            "wrapper injects (1) AED_AUTODEV_WORKER=1, "
            "AED_WORKER_PRELAUNCH_HEAD=<--prelaunch-head>, "
            "AED_WORKER_ATTEMPT_ID=<--attempt-id>, "
            "AED_WORKER_HOOKS_PATH=<this-path> into the child "
            "environment; (2) a worker-only core.hooksPath "
            "via GIT_CONFIG_COUNT/_KEY_n/_VALUE_n that "
            "preserves any inherited Git config entries. "
            "If empty, the wrapper does not install the "
            "push gate and falls back to the historical "
            "behaviour."
        ),
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

    # Round-697: worker-only push boundary.
    #
    # When the supervisor passes --worker-hooks-path, this
    # wrapper does TWO things for the worker child environment
    # and ONLY for the worker child environment:
    #
    #   1. Inject the round-697 worker-env contract so the
    #      pre-commit / pre-push hooks can identify themselves
    #      as AED worker sessions and read the prelaunch head.
    #   2. Inject a worker-only core.hooksPath via the safe
    #      per-process Git mechanism (GIT_CONFIG_COUNT /
    #      GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n). This DOES
    #      NOT mutate the operator's ~/.gitconfig or any
    #      persistent repository-local hooksPath setting;
    #      those only affect interactive git invocations.
    #      The operator's interactive git installation is
    #      untouched.
    if args.worker_hooks_path:
        # Defence in depth: refuse to launch a worker whose
        # prelaunch-head arg is missing or malformed.
        import re as _re
        if not _re.fullmatch(r"[0-9a-f]{40}", args.prelaunch_head or ""):
            print(
                "aed_worker_wrapper: refusing to install "
                "worker hooks without a valid --prelaunch-head "
                f"(got {args.prelaunch_head!r})",
                file=sys.stderr,
                flush=True,
            )
            return 1
        # Round-1064 P2: require an absolute existing hook
        # directory so Git cannot silently skip the
        # pre-commit/pre-push gates. A relative path or a
        # missing directory would let Git run ``git push``
        # with no hooks at all, which bypasses the
        # round-697 contract. The wrapper writes a canonical
        # WORKER_EXECUTION_FAILED artifact on rejection so
        # the supervisor observes the launch failure instead
        # of seeing an empty launch.
        _hooks_path = Path(args.worker_hooks_path)
        if not _hooks_path.is_absolute() or not _hooks_path.is_dir():
            print(
                "aed_worker_wrapper: refusing to install "
                "worker hooks at a non-absolute or non-existent "
                f"--worker-hooks-path ({args.worker_hooks_path!r}); "
                "would silently drop the push gate",
                file=sys.stderr,
                flush=True,
            )
            return _write_launch_failure_artifact(
                args,
                launch_failure=(
                    f"invalid --worker-hooks-path: "
                    f"{args.worker_hooks_path!r} is not an "
                    "absolute existing directory"
                ),
            )
        # Note: --attempt-id is already required elsewhere in
        # the wrapper; we trust it for log correlation only.
        child_env["AED_AUTODEV_WORKER"] = "1"
        child_env["AED_WORKER_PRELAUNCH_HEAD"] = args.prelaunch_head
        child_env["AED_WORKER_ATTEMPT_ID"] = args.attempt_id or ""
        child_env["AED_WORKER_HOOKS_PATH"] = args.worker_hooks_path

        # Build the GIT_CONFIG_COUNT/_KEY_n/_VALUE_n triplet.
        # Preserve any inherited Git config env entries so the
        # operator's standard config (signing, identity, etc.)
        # still applies during the worker session, just with
        # the worker-only hooksPath shadowing core.hooksPath
        # for child git processes.
        #
        # NOTE: Git's per-process config env is ZERO-INDEXED;
        # see the git-config(1) man page:
        #   "If GIT_CONFIG_COUNT is set to a positive number, all
        #    environment pairs GIT_CONFIG_KEY_<n> and
        #    GIT_CONFIG_VALUE_<n> up to that number will be added
        #    to the process's runtime configuration. The config
        #    pairs are zero-indexed."
        # We key N=0..N-1, not 1..N. Writing KEY_<N> instead
        # of KEY_<N-1> would cause "missing config key" fatal
        # errors at every child git invocation.
        existing = []
        try:
            existing_count_raw = child_env.get("GIT_CONFIG_COUNT", "0") or "0"
            existing_count = int(existing_count_raw)
        except (TypeError, ValueError):
            # Malformed inherited Git config: refuse to launch.
            print(
                "aed_worker_wrapper: inherited GIT_CONFIG_COUNT "
                f"is malformed ({child_env.get('GIT_CONFIG_COUNT')!r}); "
                "refusing to launch worker to avoid dropping the "
                "push gate silently",
                file=sys.stderr,
                flush=True,
            )
            return 1
        # Round-1064 P2: a negative GIT_CONFIG_COUNT would be
        # rewritten by the wrapper as ``GIT_CONFIG_COUNT=0`` plus a
        # ``GIT_CONFIG_KEY_-1`` entry that Git silently ignores —
        # so the worker hook would never be installed and the
        # push gate would be bypassed. Reject negative counts
        # up front and emit a canonical WORKER_EXECUTION_FAILED
        # artifact so the supervisor observes the launch failure.
        if existing_count < 0:
            print(
                "aed_worker_wrapper: inherited GIT_CONFIG_COUNT "
                f"is negative ({existing_count!r}); refusing to "
                "launch worker to avoid dropping the push gate silently",
                file=sys.stderr,
                flush=True,
            )
            return _write_launch_failure_artifact(
                args,
                launch_failure=(
                    f"invalid inherited GIT_CONFIG_COUNT: "
                    f"{existing_count!r} (must be non-negative)"
                ),
            )
        # Round-1064 P2: each inherited index MUST carry both a
        # key AND a value. A partial entry (e.g. ``GIT_CONFIG_KEY_2``
        # with no ``GIT_CONFIG_VALUE_2``) would otherwise leave a
        # gap in the rewritten config; Git treats a declared-but-
        # missing ``GIT_CONFIG_KEY_<n>`` as a fatal error and the
        # worker would fail every child git invocation.
        for i in range(0, existing_count):
            k = child_env.get(f"GIT_CONFIG_KEY_{i}")
            if k is None:
                continue
            v = child_env.get(f"GIT_CONFIG_VALUE_{i}")
            if v is None:
                # Incomplete inherited pair: drop it AND every
                # later index (Git would also reject those as a
                # result of the count-vs-entries mismatch).
                # Treat this as a malformed launch and emit a
                # canonical WORKER_EXECUTION_FAILED artifact so
                # the supervisor observes the failure rather
                # than a worker that exits immediately.
                print(
                    "aed_worker_wrapper: inherited "
                    f"GIT_CONFIG_VALUE_{i} is missing while "
                    f"GIT_CONFIG_KEY_{i}={k!r}; refusing to "
                    "launch worker because the partial config "
                    "would crash every child git invocation",
                    file=sys.stderr,
                    flush=True,
                )
                return _write_launch_failure_artifact(
                    args,
                    launch_failure=(
                        f"incomplete inherited GIT_CONFIG "
                        f"pair at index {i}: "
                        f"GIT_CONFIG_KEY_{i}={k!r} present but "
                        f"GIT_CONFIG_VALUE_{i} missing"
                    ),
                )
            existing.append((k, v))
        # Append the worker hooksPath. The new index is
        # ``len(existing)`` (zero-indexed), so the rewritten
        # config is always contiguous 0..new_count-1.
        new_entry = (
            "core.hooksPath", args.worker_hooks_path,
        )
        new_count = len(existing) + 1
        # Wipe inherited keys then rewrite in deterministic
        # order — keys are 0..N-1.
        for k in ("GIT_CONFIG_COUNT",):
            child_env.pop(k, None)
        # The upper bound is the maximum of the inherited
        # count (for cleanup) and the new count (so we wipe
        # any stale KEY/VALUE pairs the caller may have set).
        for i in range(0, max(existing_count, new_count)):
            child_env.pop(f"GIT_CONFIG_KEY_{i}", None)
            child_env.pop(f"GIT_CONFIG_VALUE_{i}", None)
        # Re-emit in order, then append new entry at index
        # ``len(existing)``.
        child_env["GIT_CONFIG_COUNT"] = str(new_count)
        for i, (k, v) in enumerate(existing):
            child_env[f"GIT_CONFIG_KEY_{i}"] = k
            child_env[f"GIT_CONFIG_VALUE_{i}"] = v
        child_env[f"GIT_CONFIG_KEY_{len(existing)}"] = new_entry[0]
        child_env[f"GIT_CONFIG_VALUE_{len(existing)}"] = new_entry[1]

    # Open stdout log for tee
    stdout_log_path = Path(args.stdout_log_path)
    stdout_log_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_log_fh = stdout_log_path.open("wb", buffering=0)
    # Round-168 P2: bound the in-memory stdout buffer. The complete
    # stdout is preserved on disk via stdout_log_fh; `captured` is
    # only needed by the envelope parser below (which scans for a
    # trailing envelope). A bounded tail keeps wrapper memory stable
    # against noisy workers without changing envelope detection,
    # since the envelope is always emitted at the end of the
    # worker's final response. 1 MiB is far larger than any
    # realistic envelope payload.
    _CAPTURED_TAIL_MAX = 1 * 1024 * 1024
    captured = bytearray()

    def _append_bounded(buf: bytearray, chunk: bytes, cap: int) -> None:
        if cap <= 0:
            return
        buf.extend(chunk)
        if len(buf) > cap:
            # Drop the oldest bytes, keeping the most recent `cap`.
            del buf[: len(buf) - cap]

    def _tee_pipe(fd):
        """Read from fd, write to log and capture buffer."""
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                _append_bounded(captured, chunk, _CAPTURED_TAIL_MAX)
                try:
                    stdout_log_fh.write(chunk)
                except OSError:
                    # Disk capacity exhaustion or log rotation
                    # removed the file; the in-memory tail still
                    # gives envelope detection.
                    pass
        except OSError:
            # Pipe closed / errno on read; the process likely exited.
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
        return _write_launch_failure_artifact(
            args, launch_failure=str(e),
        )

    # Read in a thread (blocking)
    def _reader():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                # Round-168 P2: bound the in-memory buffer; the
                # on-disk log keeps the full stdout.
                _append_bounded(captured, chunk, _CAPTURED_TAIL_MAX)
                try:
                    stdout_log_fh.write(chunk)
                except OSError:
                    # Disk write failure must not abort the reader.
                    pass
        except Exception:
            # Pipe closed / read error; the worker subprocess
            # likely exited. The on-disk log is authoritative.
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
            # Round-169 P2: terminate the entire child
            # process group, not just the direct child.
            # ``start_new_session=True`` above puts the
            # worker in its own process group whose pgid
            # equals ``proc.pid``; ``proc.kill()`` only
            # signals the direct child, leaving worker
            # subprocesses free to mutate the checkout or
            # push commits after the wrapper reports a
            # timeout. Signal SIGKILL on the group and
            # wait for the child to exit before the
            # timeout artifact is written.
            try:
                import signal as _signal
                os.killpg(proc.pid, _signal.SIGKILL)
                proc.wait(timeout=5)
            except Exception:
                # Fall back to direct child kill if the
                # group is already gone or wait times out.
                try:
                    proc.kill()
                    proc.wait(timeout=5)
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

    # Round-54/C22 continuation §5/§6/§7/§8: result-contract
    # identity validation. The wrapper knows what it EXPECTS
    # via CLI args. It must independently verify that the
    # worker returned the SAME expected identity — not that
    # the supervisor passed it. When the worker omits
    # ``result_contract_id`` entirely, or echoes a different
    # value, the wrapper MUST fail closed with
    # ``WORKER_RESULT_INVALID`` and surface both the expected
    # and observed values for forensic audit. The supervisor
    # later reads ``extra.observed_result_contract_id`` and
    # ``extra.expected_result_contract_id`` to decide whether
    # to attempt push verification, consume the event, or
    # resolve the GitHub thread.
    _expected_result_contract_id = args.result_contract_id or ""
    _observed_result_contract_id = ""
    _result_contract_mismatch_reason = ""
    if (
        _envelope_match_count == 1
        and isinstance(envelope, dict)
        and "_envelope_parse_error" not in envelope
        and "_envelope_parse_warning" not in envelope
    ):
        _observed_result_contract_id = (
            envelope.get("result_contract_id") or ""
        )
        if not isinstance(_observed_result_contract_id, str):
            _observed_result_contract_id = str(_observed_result_contract_id)
        if not _expected_result_contract_id:
            # Supervisor did not pass an expected contract id.
            # Without an expected identity there is nothing to
            # compare against; the wrapper cannot prove worker
            # identity and MUST fail closed (C22 §6 / §8.L).
            _result_contract_mismatch_reason = (
                "no expected result_contract_id passed via "
                "--result-contract-id; cannot verify worker "
                "identity"
            )
        elif not _observed_result_contract_id:
            _result_contract_mismatch_reason = (
                "worker envelope omitted result_contract_id"
            )
        elif _observed_result_contract_id != _expected_result_contract_id:
            _result_contract_mismatch_reason = (
                "worker envelope result_contract_id does not "
                "match expected prelaunch contract id"
            )
    elif _envelope_match_count == 1 and (
        not isinstance(envelope, dict)
        or "_envelope_parse_error" in envelope
        or "_envelope_parse_warning" in envelope
    ):
        # Exactly one envelope match but it failed to parse
        # cleanly OR was flagged as multiple by the parser.
        # Either way we cannot prove worker identity.
        _result_contract_mismatch_reason = (
            "envelope did not parse to a clean dict; cannot "
            "extract observed result_contract_id"
        )
    elif _envelope_match_count > 1:
        _result_contract_mismatch_reason = (
            "multiple envelopes detected; cannot validate a "
            "single observed result_contract_id"
        )
    else:
        _result_contract_mismatch_reason = (
            "no envelope detected; no observed "
            "result_contract_id"
        )
    _result_contract_match = (
        not _result_contract_mismatch_reason
        and bool(_observed_result_contract_id)
        and _observed_result_contract_id == _expected_result_contract_id
    )

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
    elif not _result_contract_match:
        # C22 §5/§7/§8: identity mismatch between expected
        # prelaunch contract and observed worker envelope.
        # Fail closed: do NOT trust the worker's
        # ``result_type`` claim when we cannot prove the
        # worker is the one the supervisor launched.
        _final_result_type = "WORKER_RESULT_INVALID"
    else:
        # Exactly one envelope AND result-contract match.
        # The result_type comes from the envelope's body if
        # present and valid, otherwise the default.
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
            # Round-54/C22 continuation §2/§5/§6/§7: persist
            # the prelaunch result contract AND the observed
            # worker-reported contract id so the supervisor can
            # validate worker output identity against the
            # prelaunch contract. The two fields are distinct:
            # ``expected_result_contract_id`` is what the
            # supervisor passed via --result-contract-id;
            # ``observed_result_contract_id`` is what the
            # worker actually emitted in its envelope. They
            # MUST match for a canonical valid attempt.
            "result_contract_id": args.result_contract_id or "",
            "expected_result_contract_id": _expected_result_contract_id,
            "observed_result_contract_id": _observed_result_contract_id,
            "result_contract_match": _result_contract_match,
            "result_contract_mismatch_reason": _result_contract_mismatch_reason,
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
