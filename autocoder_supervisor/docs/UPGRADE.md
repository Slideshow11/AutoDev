# Upgrade

The supervisor follows the package's `schema_version` field.
This document covers upgrading within the same `v1` schema
(e.g. bug fixes, performance improvements) and across schema
versions.

## In-place upgrade (same `v1` schema)

The supervisor imports the package from `site-packages`
(since `pip install /opt/aed-supervisor` registers it with
the system Python or the venv Python). The source-tree swap
alone does NOT update the installed distribution; the
operator must reinstall.

```bash
sudo systemctl stop aed-supervisor@<instance>.service

# Preserve the current install tree as the rollback
# target. The rollback procedure (ROLLBACK.md) requires
# /opt/aed-supervisor.old to exist; this step ensures
# the artifact is created and verified.
if [ -d /opt/aed-supervisor.old ]; then
    sudo rm -rf /opt/aed-supervisor.old
fi
sudo mv /opt/aed-supervisor /opt/aed-supervisor.old
sudo install -d -m 0755 /opt/aed-supervisor
# Stage the new source tree in a temporary install root.
sudo rm -rf /opt/aed-supervisor.new
sudo install -d /opt/aed-supervisor.new
sudo cp -r autocoder_supervisor /opt/aed-supervisor.new/
sudo cp pyproject.toml /opt/aed-supervisor.new/pyproject.toml

# Reinstall the staged distribution with the runtime
# interpreter. The supervisor's state files (lease,
# snapshots, readiness) are unchanged.
if [ -x /opt/aed-supervisor/venv/bin/python ]; then
    # VIRTUALENV install
    sudo /opt/aed-supervisor/venv/bin/pip install \
        --upgrade /opt/aed-supervisor.new
else
    # SYSTEM-WIDE install
    sudo python3 -m pip install --upgrade /opt/aed-supervisor.new
fi

sudo rm -rf /opt/aed-supervisor.new
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
preserved; only the source tree and the installed
distribution change.
