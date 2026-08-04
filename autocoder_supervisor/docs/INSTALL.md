# Install — production

The source-controlled supervisor is a Python package. It can
be installed system-wide or into a virtualenv. The
recommended production deployment uses a dedicated system
user and a systemd user service.

## Source-tree layout

The package is laid out so that the install root contains
both `pyproject.toml` and the `autocoder_supervisor/`
package directory side-by-side:

```
<repo-root>/
├── pyproject.toml                        # packaging manifest
└── autocoder_supervisor/                 # Python package
    ├── __init__.py
    ├── contracts.py
    ├── config.py
    ├── supervisor.py
    ├── validate.py
    ├── INVARIANTS.md
    ├── README.md
    ├── docs/
    ├── examples/
    └── service/
```

The committed `pyproject.toml` is at `pyproject.toml`
(NOT inside `autocoder_supervisor/`). It is moved to the
install root alongside the package directory.

## 1. Install the Python package

Either system-wide:

```bash
# 1. Create the install root.
sudo install -d /opt/aed-supervisor

# 2. Copy the package directory into the install root.
sudo cp -r autocoder_supervisor /opt/aed-supervisor/

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

## 2. Prepare state + log directories

```bash
sudo install -d -o aed-supervisor -g aed-supervisor -m 0700 \
    /var/lib/aed-supervisor/state
sudo install -d -o aed-supervisor -g aed-supervisor -m 0750 \
    /var/log/aed-supervisor
```

## 3. Configure

```bash
sudo install -d -o aed-supervisor -g aed-supervisor -m 0750 /etc/aed-supervisor
sudo cp autocoder_supervisor/examples/aed-supervisor.example.toml \
    /etc/aed-supervisor/aed-supervisor.toml
sudo chown aed-supervisor:aed-supervisor /etc/aed-supervisor/aed-supervisor.toml
sudo chmod 0640 /etc/aed-supervisor/aed-supervisor.toml
sudo -u aed-supervisor $EDITOR /etc/aed-supervisor/aed-supervisor.toml
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
sudo cp autocoder_supervisor/service/aed-supervisor@.service.template \
    /etc/systemd/system/aed-supervisor@.service
sudo systemctl daemon-reload
sudo systemctl enable --now aed-supervisor@<instance>.service
```

The `<instance>` placeholder names the supervisor instance —
e.g. `aed-supervisor@canary.service`. `%i` inside the unit
expands to `<instance>`, so different instances can coexist
with different state directories and configurations.

## 5. Verify

```bash
sudo systemctl status aed-supervisor@<instance>.service
sudo journalctl -u aed-supervisor@<instance>.service -f
```

You should see the supervisor log "supervisor started
(source-controlled v1)" within one heartbeat (default 120s).
