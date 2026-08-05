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
