# Upgrade

The supervisor follows the package's `schema_version` field.
This document covers upgrading within the same `v1` schema
(e.g. bug fixes, performance improvements) and across schema
versions.

## In-place upgrade (same `v1` schema)

```bash
sudo systemctl stop aed-supervisor@<instance>.service
# Remove any pre-existing staging dir from a previous
# interrupted upgrade. Without this, `cp -r` would preserve
# obsolete files inside the new staging tree.
sudo rm -rf /opt/aed-supervisor.new
sudo install -d /opt/aed-supervisor.new
sudo cp -r autocoder_supervisor /opt/aed-supervisor.new/
sudo cp pyproject.toml /opt/aed-supervisor.new/pyproject.toml
# Replace the old install atomically:
sudo mv /opt/aed-supervisor /opt/aed-supervisor.old
sudo mv /opt/aed-supervisor.new /opt/aed-supervisor
sudo systemctl start aed-supervisor@<instance>.service
```

The supervisor's persistent state (lease, snapshots, readiness
state) is unchanged by an in-place upgrade — the on-disk
schema is the same.

## Cross-schema upgrade

When the `schema_version` field changes (e.g. v1 → v2):

1. The supervisor refuses to start with the old state files
   if it cannot interpret the new schema.
2. The operator must run a one-time migration command (see
   `STATE_MIGRATION.md`).
3. After the migration, restart the service.

## Verifying an upgrade

After upgrading, check that the service started cleanly:

```bash
sudo systemctl status aed-supervisor@<instance>.service
sudo journalctl -u aed-supervisor@<instance>.service -n 50
```

Look for:
- `supervisor started (source-controlled v1)` (or the
  matching v2 log line)
- the absence of `ValueError` lines about state files
- the absence of `lease_alive` failures

If the upgrade went well, the readiness state and lease are
preserved; only the source tree changes.
