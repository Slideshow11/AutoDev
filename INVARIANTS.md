# AED Autocoder Supervisor — Invariant Ledger (v1)

This document is the canonical, versioned description of the
behavioural invariants that the source-controlled Autocoder
supervisor must enforce. Every invariant names:

- the **invariant** — the behavioural rule;
- the **enforcing implementation** — the function in
  `autocoder_supervisor/supervisor.py` that
  enforces it;
- the **asserting tests** — the test names in
  `tests/test_autocoder_supervisor.py` that prove it.

Invariants are **versioned** with this ledger. Adding a new
invariant requires a new ledger version. Changing the meaning
of an existing invariant requires a deprecation cycle and a
new ledger version.

This ledger is the source of truth for **invariant I-08**
("review evidence is bound to the exact current head") and
**invariant I-15** ("runtime state and secrets are never
committed"). Every invariant listed here is implemented in the
source-controlled supervisor and asserted by an automated
test.

---

## I-01 — Exactly one writer for a repository/PR scope

There is at most one live worker process per `(repo_owner,
repo_name, pr_number)` tuple at any time. The worker is
authorised by a durable `worker_lease.json` that contains the
PID, the process-group ID, and the process start-time
evidence; the lease is validated on every heartbeat, and the
worker subprocess is launched with `start_new_session=True`
so the supervisor can terminate the entire process group via
`os.killpg`.

- Enforcing implementation: `acquire_lock`,
  `launch_worker`, `lease_alive`, `read_lease`, `write_lease`,
  `remove_lease`, `list_unconsumed_events`.
- Asserting tests:
  `test_n_two_simultaneous_launches_produce_one_writer`,
  `test_o_crash_after_marking_event_actionable_recovered`,
  `test_p_crash_after_launch_does_not_double_launch`,
  `test_a_revoke_on_new_coderabbit_thread_launches_one_worker`.

## I-02 — Merge is the only human boundary

The only state transition that requires an explicit human
authorisation is the transition from
`AWAITING_MERGE_AUTHORIZATION` to `MERGED`. All other state
transitions are driven by observable review/CI evidence and
do not require human input.

- Enforcing implementation: `POLICY["human_boundary"]`,
  the dedicated `MERGED` literal in the state machine (the
  terminal `MERGED` state is written by the
  merge-authorization command which lives outside this
  stabilization PR — it is not a `supervisor.STATE_*`
  constant).
- Asserting tests:
  `test_s_merge_authorization_for_one_head_cannot_be_reused`,
  `test_f_state_persists_across_simulated_restart`.

## I-03 — Readiness is provisional while the PR remains open

Readiness states (`PROVISIONAL_READY`,
`AWAITING_MERGE_AUTHORIZATION`) are valid only while the PR
remains open. Any `head_sha_drift` against the authoritative
head transitions the supervisor back to `ACTIVE_REPAIR`.

- Enforcing implementation: `evaluate_readiness`,
  `run_iteration_v5`, `snapshot_differs`, `revoke_readiness`.
- Asserting tests:
  `test_d_snapshot_differs_reports_head_drift`,
  `test_d_evaluate_readiness_returns_head_mismatch`,
  `test_j_stale_head_clean_review_cannot_authorize_current_head`.

## I-04 — AWAITING_MERGE_AUTHORIZATION remains actively monitored

While the supervisor is in `AWAITING_MERGE_AUTHORIZATION`,
each heartbeat re-captures a snapshot and re-runs
`evaluate_readiness`. Any new evidence (a new review, a new
issue comment, a new unresolved thread, a check conclusion
change, a provider returning to in-progress, or any other
observable delta) revokes readiness and transitions to
`ACTIVE_REPAIR`.

- Enforcing implementation: `run_iteration_v5` (in the
  `READINESS_STATES` branch of the main loop),
  `detect_new_actionable_events`, `revoke_readiness`.
- Asserting tests:
  `test_e_check_failure_blocks_readiness`,
  `test_g_new_formal_review_after_provisional_readiness`,
  `test_h_new_reviewer_issue_comment_after_provisional_readiness`,
  `test_i_provider_returns_to_in_progress_after_readiness`,
  `test_k_clean_status_with_unresolved_thread_blocks_readiness`.

## I-05 — New actionable evidence revokes readiness

When new actionable evidence is detected, the supervisor
transitions to `ACTIVE_REPAIR`, marks the event as actionable
in `unconsumed_events.json`, and launches exactly one worker
for the event.

- Enforcing implementation: `detect_new_actionable_events`,
  `write_unconsumed_event`, the main loop's
  `if new_events and not cooldown_active()` branch.
- Asserting tests:
  `test_a_revoke_on_new_coderabbit_thread_launches_one_worker`,
  `test_revoke_readiness_sets_state_active_repair`.

## I-06 — Each event is durably identified and consumed exactly once

Every actionable event has a stable string `id` (see
`contracts.ActionableEventDict`). The
`launched_events.json` file is the durable dedup record. Even
if the `unconsumed_events.json` list is cleared (because the
worker successfully repaired the underlying issue), the
launched_events record persists, so the supervisor never
launches a duplicate worker for the same event id.

- Enforcing implementation: `mark_event_launched`,
  `unmark_event_launched`, `launched_event_ids`,
  `write_unconsumed_event`, `consume_event`.
- Asserting tests:
  `test_b_dedup_on_subsequent_heartbeats`,
  `test_b_unmark_allows_relaunch`.

## I-07 — Repeated observation of the same event does not launch another writer

When the same event is observed on consecutive heartbeats,
the `fresh_ids = events - launched_event_ids()` filter
ensures that the worker is launched at most once.

- Enforcing implementation: the main loop's
  `fresh_ids = [e["id"] for e in new_events if e.get("id") and e["id"] not in already]`.
- Asserting tests:
  `test_b_dedup_on_subsequent_heartbeats`.

## I-08 — Review evidence is bound to the exact current head

A CodeRabbit review, a Codex review, or any per-head
evidence is correlated against the authoritative head. If the
PR head has moved past the head recorded in the request
record, the response is classified as `stale` and does not
count as actionable.

- Enforcing implementation: `correlate_provider_review`'s
  `stale` flag, `collect_provider_surfaces` per-head filter,
  `handle_paused_providers`' head-change branch.
- Asserting tests:
  `test_j_stale_head_clean_review_cannot_authorize_current_head`,
  `test_c_required_provider_in_progress_blocks_readiness`.

## I-09 — A successful provider check is not sufficient without inspecting findings

A CodeRabbit `Review completed` exact-head commit status does
not, by itself, prove that CodeRabbit found no issues. The
supervisor requires the actual review record / inline
comments to be observed before transitioning out of
`ACTIVE_REPAIR`. The `evaluate_readiness` and
`detect_new_actionable_events` functions inspect the formal
review records and inline comments; they do not rely solely
on the commit-status `success` field.

- Enforcing implementation: `capture_live_snapshot`,
  `detect_new_actionable_events`,
  `threads_block_readiness` (unresolved threads block
  readiness regardless of provider status).
- Asserting tests:
  `test_k_clean_status_with_unresolved_thread_blocks_readiness`.

## I-10 — Required and optional provider states remain independent

The supervisor never globally pauses the run because a single
optional provider is quota-limited. A provider that is in a
quota-pause state cannot drive the run, but a different
provider that is required and available continues to make
progress.

- Enforcing implementation: `POLICY["provider_states_are_independent"]`,
  `process_provider_quotas`, `handle_paused_providers`,
  `resume_if_eligible`' `globally_paused` rule (it is only
  true when every provider eligible for the current phase is
  unavailable).
- Asserting tests:
  `test_c_policy_classifies_codex_as_optional`,
  `test_c_required_provider_in_progress_blocks_readiness`,
  `test_c_optional_provider_in_progress_does_not_block_readiness`,
  `test_c_codex_pause_does_not_pause_run`,
  `test_c_no_codex_review_request_record_exists`.

## I-11 — Pollers are sensors; they do not classify code findings

Code-review bots (CodeRabbit, Codex) classify code findings.
The supervisor only observes their classifications and
classifies the resulting *review surface* (review present?
walkthrough present? rate-limited? inline comments?). The
supervisor never itself claims a finding is "repaired" or
"out-of-scope" without durable evidence.

- Enforcing implementation: `inspect_live_state`,
  `collect_provider_surfaces`, `correlate_provider_review`,
  the absence of any "interpret the diff" logic in the
  supervisor.
- Asserting tests:
  `test_l_embedded_reviewer_commands_are_inert`,
  `test_m_only_top_level_commands_from_authorized_operator_account`.

## I-12 — Missed handoffs are recovered on later heartbeats

If the supervisor crashes between marking an event as
actionable and launching the worker, the worker-launch
receipt is missing. The next heartbeat sees the event in
`unconsumed_events.json`, the lease is invalid, and the
worker is launched again with the same event id (the
`launched_events.json` filter still passes because the
previous launch was not durable). This is the canonical
recovery path for crashes between mark and launch.

- Enforcing implementation: `run_iteration_v5`'s
  `write_unconsumed_event` step (runs before the launch
  branch) and the launch branch's `lease_alive` check.
- Asserting tests:
  `test_o_crash_after_marking_event_actionable_recovered`,
  `test_p_crash_after_launch_does_not_double_launch`.

## I-13 — A supervisor restart preserves state without duplicating workers

On restart, the supervisor:
1. reads the persistent readiness state (`read_readiness_state`);
2. reads the persistent `unconsumed_events.json`;
3. reads the persistent `worker_lease.json`;
4. re-validates the lease (`lease_alive`);
5. continues the loop without launching a new worker if the
   lease is still alive and no new actionable evidence has
   appeared.

- Enforcing implementation: `main()`'s pre-loop setup,
  `lease_alive`, `read_readiness_state`, `list_unconsumed_events`.
- Asserting tests:
  `test_f_state_persists_across_simulated_restart`,
  `test_f_no_duplicate_worker_launch_on_resume`,
  `test_f_revalidates_readiness_after_restart`,
  `test_f_no_active_repair_revival_without_head_change`.

## I-14 — No merge occurs without exact-head authorisation

The supervisor never invokes the GitHub merge API. Merge is
the only human boundary (I-02). The terminal `MERGED` state
is written only by the operator-driven merge-authorization
command (which lives outside this stabilisation PR). The
`evaluate_readiness` function refuses readiness on any head
that does not match the authoritative head.

- Enforcing implementation: `POLICY["human_boundary"]`,
  `evaluate_readiness`' `head_mismatch` branch, the
  intentional absence of any `gh pr merge` call in the
  supervisor module.
- Asserting tests:
  `test_s_merge_authorization_for_one_head_cannot_be_reused`,
  `test_d_evaluate_readiness_returns_head_mismatch`.

## I-15 — Runtime state and secrets are never committed

The committed source tree contains no runtime state files
(no `state/`, no `logs/`, no `lock`, no `heartbeat`, no
`worker_lease.json`, no `quota_state.json`,
no `unconsumed_events.json`, no `launched_events.json`,
no `snapshot_a.json`, no `snapshot_b.json`,
no `readiness_state.json`, no `run_state.json`, no review
request records, no `MERGE_TERMINAL_EVIDENCE.json`) and no
secrets (no GitHub PATs, no Slack tokens, no AWS keys, no
PEM private keys, no `Authorization: Bearer` headers, no
`oauth_token:` cookies, no `password=`/`secret=` values).

The `.gitignore` of this repository already excludes the
runtime state files generated by the package
(`autocoder_supervisor/state/`,
`autocoder_supervisor/logs/`,
`autocoder_supervisor/lock`,
`autocoder_supervisor/heartbeat`).

The validator `config.validate_config_dict` rejects
credential-shaped values and absolute user-specific paths
so an accidentally committed configuration cannot leak the
operator's home directory or any token.

- Enforcing implementation: `config.validate_config_dict`,
  the `.gitignore` rules under
  `autocoder_supervisor/`.
- Asserting tests:
  `test_q_runtime_files_use_restrictive_permissions`,
  `test_r_configuration_with_secrets_or_user_paths_is_rejected`.

---

## Mapping: human steering -> invariant

The stabilisation PR persists the operator's standing
policy:

| Human steering statement                | Enforced by |
| --------------------------------------- | ----------- |
| "human_boundary = merge_only"           | I-02, I-14  |
| "Codex is optional"                     | I-10        |
| "Codex is rate-limited until reset"     | I-10        |
| "do NOT post Codex review requests"     | I-10        |
| "ready is provisional while open"       | I-03, I-04  |
| "any new evidence revokes readiness"    | I-04, I-05  |
| "exactly one writer"                    | I-01        |
| "no merge without authorisation"        | I-02, I-14  |
| "no secrets in committed tree"          | I-15        |

---

# AutoDev Control Plane — Invariant Ledger (v1)

This ledger (continued below v1) describes the invariants enforced
by the `autocoder_orchestration` package. The invariants are
versioned with this ledger.

Every invariant names:

- the **invariant** — the behavioural rule;
- the **enforcing implementation** — the function/module in
  `autocoder_orchestration/*.py` that enforces it;
- the **asserting tests** — the test names in
  `tests/test_autocoder_orchestration_*.py` that prove it.

---

## C-01 — State transitions are mechanically enforced

The controller is the only writer of `state.json`. The state
machine refuses any transition that is not in the
`_FORWARD_TRANSITIONS` table. Workers cannot mutate state
directly; they invoke controller methods that re-validate the
transition.

- Enforcing implementation: `state_machine.transition()`
- Asserting tests: `test_prohibited_transitions.*`,
  `test_worker_cannot_set_controller_only_state`,
  `test_verifier_cannot_authorize_merge`.

## C-02 — Worker author cannot place the run in controller-only states

Implementation workers cannot place the run in:

- CANDIDATE_FROZEN
- VERIFYING
- AWAITING_MERGE_AUTHORIZATION
- MERGE_AUTHORIZED
- COMPLETE

These states are reserved for the controller, candidate builder,
verifier, and human operator respectively.

- Enforcing implementation: `state_machine.CONTROLLER_ONLY_STATES`
- Asserting tests: `test_implementation_worker_cannot_set_*` (5 tests).

## C-03 — Only the verifier can write verifier evidence

The verifier writes a `verifier-record.json` file. The
controller's `verifier_passed` and `verifier_failed` methods
are the only entry points that accept a verifier record.
Implementation workers cannot write a verifier record.

- Enforcing implementation: `controller.verifier_passed`,
  `controller.verifier_failed`
- Asserting tests: `test_verifier_api_only_takes_records`.

## C-04 — Readiness is a structured gate evaluation

The readiness engine evaluates 25 named gates and returns one
structured decision. There is no skip/ignore/trust/assume/force
flag. A failed gate prevents a passing decision.

- Enforcing implementation: `readiness.ReadinessEngine.evaluate`
- Asserting tests: `tests/test_autocoder_orchestration_readiness.py`
  (31 tests covering every gate).

## C-05 — Strict observer requires continuous qualifying interval

The strict observer records observations and rejects the quiet
window if any observation inside the qualifying interval is
non-qualifying. The interval resets on a failed observation.
PID and process-start-identity must remain stable for the
interval.

- Enforcing implementation: `observer.StrictObserver.observe`
- Asserting tests: `test_watcher_oscillation_resets`,
  `test_unresolved_thread_resets_interval`,
  `test_stale_head_disqualifies`.

## C-06 — Candidate must be built from exact-head Git-object bytes

The candidate builder reads files via `git show <exact_head>:<path>`,
never from the mutable working tree. Path traversal is rejected,
absolute paths are rejected.

- Enforcing implementation: `candidate.CandidateBuilder._git_show_sha`
- Asserting tests: `test_build_from_exact_head`,
  `test_build_refuses_unsafe_path`,
  `test_build_writes_files`.

## C-07 — Candidate refuses without a passing readiness certificate

The candidate builder rejects any readiness decision that has
failed gates. A `CandidateNotReady` error is raised. No partial
candidate is written.

- Enforcing implementation: `candidate.CandidateBuilder.build`
- Asserting tests: `test_refuses_without_readiness`,
  `test_refuses_head_mismatch`,
  `test_refuses_run_id_mismatch`.

## C-08 — Verifier role guard rejects same-identity verifier

The verifier role guard rejects a verification attempt when:

- the verifier process identity matches the implementation worker;
- the verifier executable path is inside the target branch checkout;
- the verifier has write credentials configured.

- Enforcing implementation:
  `verifier_handoff.VerifierRoleGuard.validate`
- Asserting tests: `test_rejects_same_process_identity`,
  `test_rejects_executable_in_target_checkout`,
  `test_rejects_write_credentials`.

## C-09 — Merge authorization binds all critical fields

The merge authorization includes:

- authorized head;
- candidate SHA-256;
- verifier record SHA-256;
- merge method (squash only by default).

It is the human-only signature that authorizes the merge.

- Enforcing implementation: `merge_authorization.MergeAuthorization`
- Asserting tests: `test_refuses_no_match_head_commit`,
  `test_refuses_admin`, `test_refuses_auto`,
  `test_refuses_merge_commit`, `test_refuses_rebase`.

## C-10 — Merge executor refuses unsafe variants

The merge executor refuses:

- a different head than the authorized one;
- admin bypass;
- auto-merge;
- merge commit;
- rebase merge;
- missing match-head-commit flag.

- Enforcing implementation: `merge_authorization.MergeExecutor`
- Asserting tests: `test_compute_command`, `test_refuses_admin`,
  `test_refuses_auto`, `test_refuses_merge_commit`,
  `test_refuses_rebase`, `test_refuses_no_match_head_commit`.

## C-11 — State files are mode 0600, private directories are mode 0700

The state store writes state files with mode 0600 and creates
private directories with mode 0700. Symlinks are rejected.

- Enforcing implementation: `store._atomic_write`,
  `store._ensure_private_dir`
- Asserting tests: `test_write_creates_file_with_0600`,
  `test_creates_directory_with_0700`.

## C-12 — Malformed state fails closed

Reading a state file with invalid JSON, a non-dict top-level,
or a symlink raises `StateCorruption`. The store does not
silently default.

- Enforcing implementation: `store._read_json`
- Asserting tests: `test_read_invalid_json_raises`,
  `test_read_non_dict_raises`.

## C-13 — Path traversal rejected

Both the store and the candidate builder reject paths containing
`..` segments or absolute paths.

- Enforcing implementation: `store._safe_path`,
  `candidate.CandidateBuilder._git_show_sha`
- Asserting tests: `test_write_rejects_unsafe_path`,
  `test_build_refuses_unsafe_path`.

## C-14 — No literal PR number or repository name in observer

The strict observer contains no embedded PR number, repository
name, or expected SHA. All these identifiers come from the run
context or the data source.

- Enforcing implementation: `observer.StrictObserver`
- Asserting tests: `test_observer_module_has_no_pr_number`,
  `test_observer_module_has_no_repo_name`.

## C-15 — PR-scoped state paths

The controller's state path is keyed by repo owner, repo name,
PR number, and run ID. PR #1 state cannot enter PR #2 state.

- Enforcing implementation: `context.RunContext.state_path`
- Asserting tests: `test_run_id_uniqueness`,
  `test_pr_one_state_cannot_enter_pr_two`.

## C-16 — Atomic rev-bumping writes

The state store writes revision-bumped files atomically. After
write, the in-place revision is incremented; the new revision
is what readers see.

- Enforcing implementation: `store.write_atomic`
- Asserting tests: `test_write_revisions_increment`,
  `test_cas_succeeds_on_match`.

## C-17 — Inventory hashes invalidate the candidate

The candidate records the SHA-256 of every input file. If a
later re-build computes a different SHA for the same path,
the candidate is invalidated.

- Enforcing implementation: `candidate.Candidate.source_files`
- Asserting tests: `test_input_hash_match_accepted`,
  `test_input_hash_mismatch_rejected`.

## C-18 — Atomic journal writes

The store appends to the journal via Python's append mode. Each
entry is a single JSON line. The journal is auditable.

- Enforcing implementation: `store.append_journal`,
  `store.read_journal`
- Asserting tests: `test_append_and_read`.

## C-19 — Lock ownership uses PID + process-start identity

A lease acquired by PID X can only be re-acquired or released
by the same PID with the same /proc start_id. PID reuse is
detected as a different start identity.

- Enforcing implementation: `store.Lease.acquire`
- Asserting tests: `test_different_process_cannot_acquire`,
  `test_same_process_re_acquire`.

## C-20 — Controller-owned state machine

The controller is the only place where the state machine
transitions are applied. Workers report observations; the
controller applies transitions. A terminal token printed by
a worker is informational only.

- Enforcing implementation: `controller.Controller`
- Asserting tests: `test_full_happy_path`,
  `test_block`, `test_state_persisted_across_reload`.


# AED Autocoder Orchestration — Invariant Ledger (v1, continued)

This document continues the supervisor invariant ledger with the
orchestration control plane invariants. The control plane drives
a single PR through qualification and merge.

## C-21 — One canonical artifact digest

Every persistent orchestration artifact (readiness certificate,
candidate, verifier handoff, verifier record, merge authorization,
merge record, evidence freeze, final report, invalidation registry
entries) is written and read through `autocoder_orchestration.artifacts`.

The artifact file is valid UTF-8 JSON only. No comment or digest
footer line is appended. The artifact digest is SHA-256 of the
EXACT complete artifact-file bytes. The digest lives in a separate
atomic sidecar `<artifact-path>.sha256`. The sidecar contains
exactly one lowercase 64-character hex digest (optional trailing
newline).

- Enforcing implementation: `autocoder_orchestration/artifacts.py`
  (`write_artifact`, `read_artifact`).
- Asserting tests: `test_writer_creates_valid_json_without_footer`,
  `test_sidecar_equals_sha256_of_exact_file_bytes`, `test_reader_*`.

## C-22 — Mandatory sidecars on every artifact

Every accepted persistent artifact must have a sidecar. A missing
sidecar, malformed sidecar text, malformed JSON, symlink, insecure
mode or digest mismatch raises and blocks any caller — including
the production merge path.

There is no production merge path that treats a missing digest as
optional. The reader returns a payload together with the verified
exact-file digest or raises.

- Enforcing implementation: `artifacts.read_artifact`.
- Asserting tests: `test_reader_rejects_missing_sidecar`,
  `test_reader_rejects_malformed_sidecar`,
  `test_reader_rejects_symlink_*`, `test_reader_rejects_insecure_mode`,
  `test_reader_rejects_appended_footer_text`,
  `test_missing_candidate_digest_blocks_merge_runner`,
  `test_missing_verifier_digest_blocks_merge_runner`.

## C-23 — No legacy footer artifacts in the production merge path

Production flows that read a candidate, verifier record, merge
authorization, or merge record via `read_artifact` refuse artifacts
whose body contains a `# sha256: ...` footer text line.

A legacy artifact may only be loaded by the explicit audit-only
helper `read_legacy_with_footer`, which exists for one-time
conversion or forensic review and is not used by the production
merge path.

- Enforcing implementation: `artifacts.read_artifact`,
  `artifacts.read_legacy_with_footer`.
- Asserting tests: `test_legacy_footer_artifacts_are_refused_by_production_merge`,
  `test_explicit_legacy_conversion_is_audited_and_deterministic`.

## C-24 — Repository / state / evidence roots are independent

The repository checkout, run-state root and evidence root are three
distinct named roots. The production merge path refuses to proceed
if any two of them resolve to the same directory.

A production merge invocation does not require copying artifacts
into the repository checkout, renaming artifacts to magic
filenames, or constructing a temporary fake run root.

- Enforcing implementation: `merge_authorization._ensure_distinct_paths`,
  `merge_authorization.MergeTransactionInputs`.
- Asserting tests: `test_repository_root_and_state_root_are_independent`,
  `test_evidence_paths_do_not_require_temporary_staging`,
  `test_end_to_end_simulated_authorization_to_complete_flow`.

## C-25 — One guarded merge transaction

The production merge path is one checked-in operation,
`execute_guarded_merge_transaction`, which:

1. loads and verifies the human authorization artifact,
2. loads and verifies the candidate and verifier artifacts,
3. fetches all live GitHub evidence,
4. repeats every exact-head and integrity guard,
5. constructs only the permitted command
   (`gh pr merge <pr> --repo <owner/repo> --squash --delete-branch --match-head-commit <exact-head>`),
6. invokes it once with a finite timeout,
7. preserves stdout, stderr, return code and timing,
8. resolves timeout or ambiguity against the live PR state,
9. writes the merge result through the canonical artifact writer,
10. transitions the state machine to COMPLETE.

The CLI must call this operation. A production flow that calls
`MergeExecutor().compute_command` and a separate runner, then
expects another command to build the merge record, is prohibited.

- Enforcing implementation: `merge_authorization.execute_guarded_merge_transaction`,
  `cli.cmd_merge`.
- Asserting tests: `test_merge_operation_invokes_runner_exactly_once`,
  `test_failed_pre_merge_guard_invokes_runner_zero_times`,
  `test_admin_auto_merge_rebase_flags_are_impossible`,
  `test_cli_exercise_production_path`.

## C-26 — Timeout reconciliation fails closed or completes reconciliation

If the guarded merge subprocess times out:

- The runner does NOT retry blindly.
- The runner re-queries the live PR.
- If the server-side merge completed, reconciliation continues.
- If the server-side merge did NOT complete, the transaction fails
  closed with `MergeSubprocessFailed` or `MergeAmbiguousOutcome`.

A merge subprocess that times out AND the live re-query fails is
always reported as `MergeAmbiguousOutcome`; the merge is rejected.

- Enforcing implementation: `merge_authorization.execute_guarded_merge_transaction`
  timeout reconciliation block.
- Asserting tests: `test_timeout_plus_server_side_merged_is_reconciled_as_success`,
  `test_timeout_plus_server_side_open_is_not_reported_as_merged`,
  `test_ambiguous_state_fails_closed`.

## C-27 — Branch-independent post-merge verification

The post-merge reconciliation does not assume the current
checked-out branch. It:

- reads the current branch from `HEAD`;
- refuses if the working tree is dirty;
- switches to the authorized base branch when needed;
- fast-forwards the local base branch with `--ff-only`;
- verifies local base equals origin/base;
- verifies the squash commit, its tree and its parent;
- verifies the remote feature branch deletion;
- deletes the local feature branch only when it matches the
  authorized head SHA-256;
- records any unavailable observation explicitly rather than
  fabricating success.

- Enforcing implementation: `merge_authorization.reconcile_after_merge`.
- Asserting tests: `test_current_feature_branch_safely_switched_to_base_before_ff`,
  `test_dirty_working_tree_blocks_branch_switching`,
  `test_unrelated_branches_are_never_deleted`,
  `test_server_side_merge_followed_by_local_git_failure_still_writes_record`.

## C-28 — Restart recovery after irreversible merge

A successful remote-side merge is irreversible. The transaction
writes the merge record with whatever local Git observations
were available; any missing observation is recorded as
`unavailable_observations` and the merge is still reported as
COMPLETE if the server-side merge completed.

A subsequent process restart can load the merge record from the
canonical artifact (which survives process restart) and resume
the audit / reconciliation flow.

- Enforcing implementation: `merge_authorization.MergeRecord.unavailable_observations`,
  `artifacts.write_artifact` durable writer.
- Asserting tests: `test_server_side_merge_followed_by_local_git_failure_still_writes_record`,
  `test_authorization_and_merge_records_survive_process_restart`.

## C-29 — No temporary artifact staging required

A production merge invocation does not require:

- copying artifacts into the repository checkout;
- renaming artifacts to magic filenames;
- constructing a temporary fake run root;
- modifying candidate or verifier records;
- manually computing a different digest convention.

The artifacts live at their canonical paths and are loaded
directly.

- Enforcing implementation: `cli.cmd_merge`,
  `merge_authorization.execute_guarded_merge_transaction`.
- Asserting tests: `test_evidence_paths_do_not_require_temporary_staging`,
  `test_end_to_end_simulated_authorization_to_complete_flow`.
