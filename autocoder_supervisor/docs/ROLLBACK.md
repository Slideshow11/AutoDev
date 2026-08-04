# Rollback

If a deployed supervisor has a bug that prevents the repair
cycle from making progress, the recommended rollback procedure
is:

## 1. Stop the service

```bash
sudo systemctl stop aed-supervisor@<instance>.service
```

## 2. Restore the previous source tree

```bash
# Confirm a rollback target exists before touching the live tree.
test -d /opt/aed-supervisor.old || {
    echo "no /opt/aed-supervisor.old to roll back to" >&2
    exit 1
}
sudo mv /opt/aed-supervisor /opt/aed-supervisor.broken
sudo mv /opt/aed-supervisor.old /opt/aed-supervisor
```

(The `.old` directory was created by the `UPGRADE.md` recipe.)

## 3. Restart the service

```bash
sudo systemctl start aed-supervisor@<instance>.service
```

## 4. Verify

```bash
sudo systemctl status aed-supervisor@<instance>.service
sudo journalctl -u aed-supervisor@<instance>.service -n 50
```

The persistent state files (lease, snapshots, readiness
state) are **not** modified by a rollback — they live under
`/var/lib/aed-supervisor/state/`, separate from the source
tree.

## When rollback is not enough

If the source tree change introduced a behaviour change that
wrote a corrupted state file, do **not** restart the service
with the corrupted state. Instead:

1. Stop the service.
2. Inspect the state directory: `ls -la /var/lib/aed-supervisor/state/`.
3. Move any state file with `sudo journalctl -u aed-supervisor` error
   references to `/var/lib/aed-supervisor/state/quarantine/`.
4. Restart the service — it will start in `ACTIVE_REPAIR` and
   rebuild the missing state from the live PR.
5. File an issue with the corrupted state file contents so
   the bug can be fixed upstream.

## External (legacy) supervisor rollback

The historical external supervisor at
`~/.hermes/aed-supervisor/` is **never** touched by this
package. If you need to "rollback" to the legacy supervisor,
simply stop the source-controlled service and start the
legacy service:

```bash
sudo systemctl stop aed-supervisor@<instance>.service
systemctl --user start aed-supervisor-legacy.service
```

(Where `aed-supervisor-legacy.service` is the systemd unit
that launches `~/.hermes/aed-supervisor/supervisor.py`. Note
that the legacy unit is intentionally user-scoped because it
runs under a per-user systemd manager; the source-controlled
unit is system-scoped because it has its own system user.)
