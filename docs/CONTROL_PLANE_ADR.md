# AutoDev Control Plane — Architecture Decision Record (ADR-001)

## Status

Proposed for `feat/autonomous-execution-control-plane-v1` (PR #3).

## Context

AutoDev Wave 1 needs an autonomous execution control plane that drives
approved migration waves from an immutable task specification through the
state machine described in the directive. The control plane must:

- orchestrate a single PR at a time per (repository, run-id) scope;
- cooperate with the existing `autocoder_supervisor` (worker-side
  supervisor) and `autocoder_lifecycle` (state primitives) packages
  without duplicating them;
- make invalid state transitions and readiness bypasses mechanically
  impossible rather than merely forbidden by prompt text;
- survive process restarts and context-window turnover.

The previous supervisor (PR #1) and lifecycle primitives (PR #2) supply
the lower layers. The control plane is the upper layer.

## Decision

A new package `autocoder_orchestration` is added. It owns:

- The **controller** — the deterministic state machine that drives one
  PR from `PLANNED` through `COMPLETE`.
- The **durable state store** — atomic, versioned, mode-`0600` files
  keyed by run context.
- The **immutable run context** — typed manifest binding repository,
  PR number, authorized head, evidence directory, etc.
- The **canonical readiness engine** — one gate evaluator used by the
  controller, candidate builder, status CLI, strict observer, verifier
  preflight, and merge-authorization preflight.
- The **strict readiness observer** — checked-in library code (not a
  per-PR script).
- The **review-thread reconciliation contract** — provider-agnostic
  representation of findings.
- The **candidate builder** — refuses to build without a readiness
  certificate.
- The **verifier handoff** — typed record so a fresh worker can pick
  up the verification task.
- The **merge authorization record** — typed record binding human
  approval to the exact head.
- The **CLI** — `initialize`, `run`, `status`, `gates`, `observe`,
  `build-candidate`, `handoff-verifier`, `apply-verifier-result`,
  `merge-authorize`, `merge`, `verify-post-merge`, `next-wave`.

## Roles and trust boundaries

| Role | Can read | Can write | Authority |
| --- | --- | --- | --- |
| Implementation worker | run context, state | worker-owned reports only | none over state |
| Controller | run context, state, evidence | run state, events | full state authority |
| Candidate builder | run context, state, evidence | `/var/tmp/.../candidate.json` | none over state |
| Strict observer | run context, state, evidence | observation log | none over state |
| Verifier (independent) | run context, candidate, raw evidence | verifier record | none over state |
| Human operator | run context, state, candidate, verifier record | merge authorization record | merge authorization |

The implementation worker is **not** the controller. The verifier is
**not** the implementation worker. A terminal token printed by a worker
is informational only; it cannot change authoritative state.

## State machine

`PLANNED`

`IMPLEMENTING`

`AWAITING_CI`

`REPAIRING_REVIEW_FINDINGS`

`QUALIFYING_READINESS`

`READY_FOR_CANDIDATE`

`CANDIDATE_FROZEN`

`AWAITING_INDEPENDENT_VERIFICATION`

`VERIFYING`

`VERIFICATION_FAILED`

`VERIFICATION_REPAIR`

`AWAITING_MERGE_AUTHORIZATION`

`MERGE_AUTHORIZED`

`POST_MERGE_VERIFYING`

`COMPLETE`

`BLOCKED`

Each transition lists:

- allowed predecessors;
- required evidence;
- authorized actor;
- idempotency behavior;
- head-stability requirements;
- invalidation behavior;
- durable event emitted.

## Mapping to existing AutoDev code

- `autocoder_lifecycle.checkpoint.CheckpointState` supplies the
  in-memory checkpoint shape. The orchestration layer owns DURABLE
  checkpoints (atomic files + revision counter).
- `autocoder_lifecycle.registry.LifecycleStateRegistry` is used as the
  procedural state vocabulary. The controller's state machine is a
  separate union of explicit states (above). Conversion is performed
  at the integration boundary.
- `autocoder_lifecycle.watchdog.WatchdogState` is reused for the
  advisory per-phase watchdog. The strict readiness observer is
  checked-in orchestration code that lives **on top of** the watchdog.
- `autocoder_supervisor.supervisor` is the worker-side supervisor. The
  orchestration controller supervises the supervisor at a higher
  level (one controller process per run, one supervisor process per
  worker). The supervisor's `readiness_state.json` and
  `snapshot_a.json` are inputs to the controller's `READY_FOR_CANDIDATE`
  gate — the controller does not replicate the supervisor's logic.

## Durable state model

Path layout:

```
<evidence-root>/<owner>/<repository>/pr-<pr-number>/<run-id>/
    context.json          # immutable run context (mode 0600)
    state.json            # current run state, rev counter (mode 0600)
    journal.jsonl         # append-only event journal (mode 0600)
    heartbeat.json        # controller heartbeat (mode 0600)
    lease.lock            # advisory flock (mode 0700 dir)
    readiness.json        # latest readiness certificate (mode 0600)
    observations.jsonl    # canonical readiness observer log (mode 0600)
    freeze.sha256         # input-freeze digest (mode 0600)
    candidate.json        # built candidate (mode 0600)
    candidate.sha256      # atomic sidecar (mode 0600)
    verifier-handoff.json # handoff to independent verifier (mode 0600)
    verifier-record.json  # verifier output (mode 0600)
    merge-authorization.json  # human authorization (mode 0600)
    merge-record.json      # post-merge evidence (mode 0600)
    plan.json              # next-wave plan (mode 0600)
```

All state files are written `tmp + os.replace`. Schema validation on
every read. Malformed state fails closed.

## Trust root

The integrity of the immutable run context is the trust root. The
controller enforces that every state transition reads and re-validates
the run context. The candidate is built from Git-object bytes at the
exact authorized head; the candidate includes a hash of the run
context, so any change to the context after build invalidates the
candidate.

## Restart behavior

The controller reads the journal on startup. If the last journal entry
is in a transitional state, the controller re-evaluates that state
from the canonical inputs (Git history, GitHub API, etc.) and either
re-enters the state cleanly or rolls back to the previous committed
state. The atomic write semantic ensures that a crash between writing
the journal entry and the state file is detected by the journal-only
flag on next startup.

## Next-wave scheduling

The controller loads a plan (`plan.json`) containing ordered wave
specifications. A wave is one PR's worth of work. The controller does
not start a wave unless:

- the previous wave's `COMPLETE` is recorded in the journal;
- the plan explicitly permits continuation; and
- a human merge authorization for the previous wave includes a
  `next_wave_authorization` flag.

## Deferred to later waves

- Full review-provider extraction (the reconciliation contract is
  generic; provider-specific parsing stays behind an adapter).
- AED-side lifecycle adapter (PR 1b in the validated roadmap).
- Policy / authorization / execution identity extraction.
- GitHub and review infrastructure extraction.
- Worker test and repair infrastructure extraction.
- Controller decomposition into micro-services.
- AED conversion and cleanup.

## References

- `/var/tmp/autodev-evidence/AED_AUTODEV_ARCHITECTURAL_AUDIT_VALIDATED.json`
- `/var/tmp/autodev-evidence/AED_AUTODEV_EXTRACTION_ROADMAP_VALIDATED.md`
- `/var/tmp/autodev-evidence/AED_AUTODEV_FIRST_PR_SPEC.md`
- `INVARIANTS.md` (existing supervisor invariants; the new package
  adds control-plane invariants)

## ADR-001 — Post-merge hardening (post-PR #3)

After PR #3 was merged, six defects were observed during the merge
itself. They are repaired by the
`fix/control-plane-merge-integrity-v1` branch. None of them changed
the user-facing contract; they repaired the implementation.

### Defects and repairs

**DEFECT A: MIXED DIGEST DEFINITIONS.** The pipeline used at least
three different digest conventions interchangeably: SHA-256 of
canonical JSON body bytes, SHA-256 of the full file including a
footer, and a `_sha256` field stored inside a parsed artifact. The
candidate's authorized SHA was the JSON-body SHA, while the full-file
hash produced another value. This caused a false pre-merge mismatch.

Repair: a single canonical artifact format lives in
`autocoder_orchestration/artifacts.py`. The artifact is valid UTF-8
JSON only, no comment or footer text is appended, the digest is
SHA-256 of the exact complete file bytes, and the digest lives in
a separate atomic sidecar.

**DEFECT B: OPTIONAL CANDIDATE INTEGRITY CHECK.** The previous
executor treated a missing `candidate._sha256` field as optional
and skipped the comparison. A missing digest must never weaken merge
authorization.

Repair: `read_artifact` is mandatory; a missing sidecar, malformed
sidecar, malformed JSON, symlink, insecure mode or digest mismatch
raises and blocks the caller. The production merge path never
treats a missing digest as optional.

**DEFECT C: TEMPORARY MANUAL STAGING.** The previous executor
expected `candidate.json` and `verifier-record.json` under a path
used as `auto_repo_root`. The operator workflow had to create a
temporary run directory and copy artifacts.

Repair: `MergeTransactionInputs` carries explicit
`authorization_artifact_path`, `candidate_artifact_path`,
`verifier_artifact_path`, `merge_record_artifact_path`,
`repository_checkout`, `run_state_root`, `evidence_root`. The
production merge path refuses if any two named roots resolve to
the same directory.

**DEFECT D: SPLIT MERGE EXECUTION.** The previous workflow called
the guarded merge command first and then a separate code path
constructed the post-merge record. The intended atomic workflow is
one operation that does both.

Repair: `execute_guarded_merge_transaction` is the single
production merge path. It loads and verifies every artifact,
fetches all live GitHub evidence, repeats every exact-head and
integrity guard, invokes the guarded command once with a finite
timeout, reconciles timeout or ambiguity against the live PR
state, reconciles post-merge Git (branch-independent), writes the
merge record through the canonical artifact writer, and transitions
the state machine to `COMPLETE`.

**DEFECT E: CURRENT-BRANCH ASSUMPTION.** The first local fast-forward
attempt occurred while the checkout was on the feature branch.

Repair: `reconcile_after_merge` reads the current branch from
`HEAD`, refuses on a dirty working tree, switches to the authorized
base branch when needed, fast-forwards with `--ff-only`, verifies
local base equals origin/base, verifies the squash commit and its
tree, and only deletes the local feature branch when it matches the
authorized head.

**DEFECT F: RECORD HASH CONFUSION.** Authorization and merge records
also used body hashes, footer hashes and sidecars in ways that
required manual interpretation.

Repair: every accepted control-plane artifact uses the same digest
contract (C-21, C-22). All five artifacts — readiness certificate,
candidate, verifier record, merge authorization, merge record —
are written and read through one module.

