# Install — production

The source-controlled supervisor is a Python package. It can
be installed system-wide or into a virtualenv. The
recommended production deployment uses a dedicated system
user and a per-instance layout that matches the
`aed-supervisor@<instance>.service` systemd template unit.

## Source-tree layout

The package is laid out so that the install root contains
both `pyproject.toml` and the `autocoder_supervisor/`
package directory side-by-side:

```
<repo-root>/
├── pyproject.toml                        # packaging manifest
├── autocoder_supervisor/                 # supervisor-v1 Python package
│   ├── __init__.py
│   ├── contracts.py
│   ├── config.py
│   ├── supervisor.py
│   ├── validate.py
│   ├── INVARIANTS.md
│   ├── README.md
│   ├── docs/
│   ├── examples/
│   └── service/
└── autocoder_lifecycle/                  # generic lifecycle primitives
    ├── __init__.py
    ├── registry.py
    ├── checkpoint.py
    ├── no_stall.py
    └── watchdog.py
```

The committed `pyproject.toml` is at `pyproject.toml`
(NOT inside `autocoder_supervisor/`). It is moved to the
install root alongside the package directory.

## 0. Create the service account

```bash
# Create the unprivileged account the supervisor and any
# launched worker run as.
if ! getent group aed-supervisor > /dev/null; then
    sudo groupadd --system aed-supervisor
fi
if ! getent passwd aed-supervisor > /dev/null; then
    sudo useradd --system \
        --gid aed-supervisor \
        --home-dir /var/lib/aed-supervisor \
        --shell /usr/sbin/nologin \
        --comment "Autocoder supervisor service account" \
        aed-supervisor
fi
```

## 1. Install the Python package

Either system-wide:

```bash
# 1. Create the install root.
sudo install -d /opt/aed-supervisor

# 2. Copy the package directories into the install root.
sudo cp -r autocoder_supervisor /opt/aed-supervisor/
sudo cp -r autocoder_lifecycle /opt/aed-supervisor/

# 3. Copy the packaging manifest to the install root.
sudo cp pyproject.toml /opt/aed-supervisor/pyproject.toml

# 4. Install. The supervisor package has a single conditional
#    runtime dependency (``tomli`` on Python <3.11; the
#    ``tomllib`` stdlib module is used on Python 3.11+).
#    On Python <3.11, do NOT pass ``--no-deps``; the conditional
#    ``tomli`` dependency is required for the supervisor's
#    config loader to import.
sudo python3 -m pip install /opt/aed-supervisor
```

…or into a virtualenv:

```bash
# 1. Stage the install root in a temp location.
sudo install -d /opt/aed-supervisor-install
sudo cp -r autocoder_supervisor /opt/aed-supervisor-install/
sudo cp -r autocoder_lifecycle /opt/aed-supervisor-install/
sudo cp pyproject.toml /opt/aed-supervisor-install/pyproject.toml

# 2. Create the venv.
sudo install -d /opt/aed-supervisor/venv
sudo python3 -m venv /opt/aed-supervisor/venv

# 3. Install from the staged root. The supervisor has a
#    conditional ``tomli`` dependency on Python <3.11 (the
#    ``tomllib`` stdlib is used on 3.11+), so do NOT pass
#    ``--no-deps`` on Python <3.11.
sudo /opt/aed-supervisor/venv/bin/pip install \
    /opt/aed-supervisor-install

# 4. Clean up the staging root.
sudo rm -rf /opt/aed-supervisor-install
```

After install, verify the package is importable from outside
the repository checkout. Use the same Python that the
supervisor will use at runtime:

```bash
# For a system-wide install:
python3 -c "import autocoder_supervisor; print(autocoder_supervisor.__file__)"
python3 -m autocoder_supervisor.validate --help
python3 -m autocoder_supervisor.supervisor --help

# For a virtualenv install — use the venv's python:
/opt/aed-supervisor/venv/bin/python -c "import autocoder_supervisor; print(autocoder_supervisor.__file__)"
/opt/aed-supervisor/venv/bin/python -m autocoder_supervisor.validate --help
/opt/aed-supervisor/venv/bin/python -m autocoder_supervisor.supervisor --help
```

**Important:** if you used the virtualenv install, do NOT
use the host `python3` for verification — the host interpreter
does not have the package installed. Always use the venv
interpreter.

## 2. Create the per-instance state and log directories

The systemd template unit hard-codes
`/var/lib/aed-supervisor/%i` and `/etc/aed-supervisor/%i/`
as the per-instance layout. Create them before enabling the
unit, with `aed-supervisor` as the owner. The unit template
also writes to the working checkout (the worker may commit
there); the operator must own that path too.

The example configuration in
`autocoder_supervisor/examples/aed-supervisor.example.toml`
sets ``state_dir`` to ``/var/lib/aed-supervisor/<instance>/state``.
Note that this is a *nested* directory inside the systemd
state-directory parent — the systemd ``StateDirectory=`` directive
creates only ``/var/lib/aed-supervisor/<instance>/``, not the
nested ``state`` directory. The operator MUST create the
nested ``state`` directory manually with ``aed-supervisor`` as
the owner and ``0700`` as the mode *before* enabling the
service, so the supervisor can read and write its lease,
snapshots, and readiness state file without falling back
to runtime directory creation under the wrong owner.

```bash
INSTANCE=canary  # whatever name the operator chooses

# Per-instance state directory parent (systemd
# StateDirectory= directive will also create this on
# service start, but creating it explicitly here lets us
# set the owner once).
sudo install -d -o aed-supervisor -g aed-supervisor -m 0755 \
    /var/lib/aed-supervisor/$INSTANCE

# Nested state_dir matching the configured
# state_dir in aed-supervisor.example.toml. The
# systemd StateDirectory= directive does NOT create
# this nested directory; the supervisor will create it
# at runtime under the running service UID if it is
# missing, which produces the wrong owner for operator
# inspection. Create it here with the systemd user and
# mode 0700 BEFORE enabling the service.
sudo install -d -o aed-supervisor -g aed-supervisor -m 0700 \
    /var/lib/aed-supervisor/$INSTANCE/state

# Verify the nested state directory has the required
# ownership and mode. Fail the install if any check
# fails so the supervisor does not start with the wrong
# state directory permissions.
test -d /var/lib/aed-supervisor/$INSTANCE/state
sudo chown aed-supervisor:aed-supervisor \
    /var/lib/aed-supervisor/$INSTANCE/state
sudo chmod 0700 /var/lib/aed-supervisor/$INSTANCE/state

# Per-instance log directory (systemd LogsDirectory=)
sudo install -d -o aed-supervisor -g aed-supervisor -m 0750 \
    /var/log/aed-supervisor/$INSTANCE

# /var/log/aed-supervisor is the systemd LogsDirectory.
sudo install -d -o aed-supervisor -g aed-supervisor -m 0750 \
    /var/log/aed-supervisor
```

## 3. Configure

The per-instance configuration path is
`/etc/aed-supervisor/$INSTANCE/aed-supervisor.toml`. The
template file is `examples/aed-supervisor.example.toml`.

```bash
INSTANCE=canary

sudo install -d -o aed-supervisor -g aed-supervisor -m 0750 \
    /etc/aed-supervisor/$INSTANCE
sudo install -o aed-supervisor -g aed-supervisor -m 0644 \
    autocoder_supervisor/examples/aed-supervisor.example.toml \
    /etc/aed-supervisor/$INSTANCE/aed-supervisor.toml
sudo -u aed-supervisor $EDITOR \
    /etc/aed-supervisor/$INSTANCE/aed-supervisor.toml
```

The four `__SET_ME__` placeholders in the systemd template
(`AED_PR_NUMBER`, `AED_REPO_OWNER`, `AED_REPO_NAME`,
`AED_AUTHORITATIVE_HEAD`) are normally loaded from
`/etc/aed-supervisor/$INSTANCE/supervisor.env` (an
`EnvironmentFile=`-style file). The operator is expected to
materialize that file before enabling the service.

```bash
sudo install -o aed-supervisor -g aed-supervisor -m 0640 /dev/null \
    /etc/aed-supervisor/$INSTANCE/supervisor.env
sudo -u aed-supervisor $EDITOR \
    /etc/aed-supervisor/$INSTANCE/supervisor.env
```

A minimal `supervisor.env` content:

```text
AED_PR_NUMBER=1
AED_REPO_OWNER=Slideshow11
AED_REPO_NAME=AutoDev
AED_AUTHORITATIVE_HEAD=18ba0df49d2a19779e350d6df5a102b254cbeed7
```

The validator (run with `python3 -m autocoder_supervisor.validate --config …`)
will reject credentials or absolute user-specific paths in
the configuration.

## 4. Install the systemd service

The committed file is a systemd *template unit*. The systemd
convention requires template units to be named with the
literal `@` in the filename (so `foo@.service` becomes
`foo@<instance>.service` when instantiated). Copy the file to
`/etc/systemd/system/aed-supervisor@.service`:

```bash
sudo install -o root -g root -m 0644 \
    autocoder_supervisor/service/aed-supervisor@.service.template \
    /etc/systemd/system/aed-supervisor@.service
sudo systemctl daemon-reload
sudo systemctl enable --now aed-supervisor@canary.service
```

The `<instance>` placeholder names the supervisor instance —
e.g. `aed-supervisor@canary.service`. `%i` inside the unit
expands to `<instance>`, so different instances can coexist
with different state directories and configurations.

The unit's `User=` and `Group=` directives are set to
`aed-supervisor`. The unit's `EnvironmentFile=`-style path
is loaded from
`/etc/aed-supervisor/%i/supervisor.env` so the per-instance
secrets do not appear in the unit file.

## 5. Verify

```bash
sudo systemctl status aed-supervisor@canary.service
sudo journalctl -u aed-supervisor@canary.service -f
```

You should see the supervisor log "supervisor started
(source-controlled v1)" within one heartbeat (default 120s).
