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

## Migration command (future schema versions only)

```bash
INSTANCE=canary  # whatever name the operator chooses
# (When a future schema version introduces this command.)
sudo python3 -m autocoder_supervisor.migrate \
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

## Migration from the historical external supervisor

The historical external supervisor at
`~/.hermes/aed-supervisor/` uses the same on-disk schema as
this package's v1 (the port was shape-preserving). The
target directory `/var/lib/aed-supervisor/%i/state` is
already created by the documented install procedure.
Moving the legacy `state` directory onto that destination
must therefore copy the **contents** of the legacy
directory into the existing destination (not move the
directory itself, which would produce
`/var/lib/aed-supervisor/%i/state/state`).

```bash
# 1. Stop the legacy external supervisor.
systemctl --user stop aed-supervisor-legacy.service

# 2. Copy the LEGACY STATE CONTENTS into the existing
#    destination, while the supervisor is stopped.
sudo install -d -o aed-supervisor -g aed-supervisor -m 0700 \
    /var/lib/aed-supervisor/$INSTANCE/state
sudo rsync -a \
    --chown=aed-supervisor:aed-supervisor \
    --chmod=D0700,F0600 \
    ~/.hermes/aed-supervisor/state/ \
    /var/lib/aed-supervisor/$INSTANCE/state/

# 3. Verify the resulting layout has NO
#    /var/lib/aed-supervisor/$INSTANCE/state/state/ directory.
test ! -e /var/lib/aed-supervisor/%i/state/state

# 4. Update the configuration to point at the new paths.
sudo $EDITOR /etc/aed-supervisor/%i/aed-supervisor.toml

# 5. Start the source-controlled supervisor.
sudo systemctl start aed-supervisor@%i.service
```

The on-disk state is fully portable; the only thing that
must change is the configuration's path entries.

All operations are performed with `sudo`, against the
already-created destination directory, with the service
account as the owner and restrictive modes (`0700` for
directories, `0600` for files). The legacy supervisor must
be stopped before the copy; the new supervisor must be
started only after the layout verification.
