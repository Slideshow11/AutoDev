# State migration

State migration is only relevant when the supervisor's
`schema_version` field changes. Within the same schema
version, the on-disk state is interpreted identically by any
build of the supervisor.

## v1 (current) state directory layout

```
$state_dir/
├── worker_lease.json            # durable worker lease (one writer)
├── last_resume.json             # persistent cooldown timestamp
├── quota_state.json             # per-provider quota pause state
├── review_requests/             # per-provider review request records
├── unconsumed_events.json       # durable unconsumed event list
├── launched_events.json         # durable launched-event dedup record
├── snapshot_a.json              # snapshot A (last heartbeat)
├── snapshot_b.json              # snapshot B (quiet-window comparison)
├── readiness_state.json         # persisted readiness state
└── run_state.json               # run_state.json from the worker
```

The supervisor home (parent of `$state_dir`) also contains:

```
$supervisor_home/
├── heartbeat                    # ISO timestamp of last inspection
├── lock                         # singleton flock + holder pid
└── supervisor.log               # JSONL log
```

## When to migrate

When the package's `schema_version` changes, the on-disk
files may need to be reshaped (renamed, repacked, or have
new required fields added). The supervisor refuses to start
on the old layout and logs a `migration_required` line.

## Migration command

```bash
# (When a future schema version introduces this command.)
python3 -m autocoder_supervisor.migrate \
    --config /etc/aed-supervisor/aed-supervisor.toml \
    --from-schema aed.autocoder_supervisor.v1 \
    --to-schema aed.autocoder_supervisor.v2
```

The migration command:
1. Stops the supervisor (via systemd).
2. Backs up the state directory to `${state_dir}.bak`.
3. Applies the migration in-place.
4. Restarts the supervisor.
5. Verifies readiness state is recoverable.

## Migration safety

- The migration is **idempotent**: re-running it on an
  already-migrated state is a no-op.
- The backup directory is preserved until the operator
  removes it (e.g. after a successful post-migration review).
- If the migration fails partway through, the supervisor
  falls back to the `.bak` directory and refuses to start.

## Migration within v1

There are no migrations within v1 — the on-disk schema is
stable. Adding a new field to a state file (e.g. an optional
`last_verified_at` timestamp on the lease) is a backward-
compatible change that does not require a migration.

## Migration from the external supervisor

The historical external supervisor at
`~/.hermes/aed-supervisor/` uses the same on-disk schema as
this package's v1 (the port was shape-preserving). To
migrate an existing external supervisor's state to this
package:

1. Stop the external supervisor: `systemctl --user stop aed-supervisor-legacy.service`.
2. Move the state directory:
   `mv ~/.hermes/aed-supervisor/state /var/lib/aed-supervisor/state`.
3. Update the configuration to point at the new paths.
4. Start the source-controlled supervisor.

The on-disk state is fully portable; the only thing that
must change is the configuration's path entries.
