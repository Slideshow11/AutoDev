# autocoder_supervisor (AutoDev v1)

This package is the standalone Python module
`autocoder_supervisor` extracted from
`Slideshow11/Automated-Edge-Discovery` PR #417 (`b57fcaa`).
The module is the inner supervisor that drives the
`AutoDev` autonomous-development system.

The package name is retained as `autocoder_supervisor` for
compatibility with the reviewed v1 implementation:

- distribution name: `autocoder-supervisor`
- import package: `autocoder_supervisor`
- module path: `autocoder_supervisor`
- existing AED_-prefixed environment variables and
  configuration compatibility fields are preserved.
- existing serialized state contracts, schema names, event
  identifiers and readiness-state names are preserved.

## Naming

- AutoDev is the autonomous-development system and the
  public repository identity.
- Humphry is the current coding-agent persona operating
  through AutoDev.
- The internal Python package is named `autocoder_supervisor`
  for compatibility with the reviewed v1 implementation
  extracted from AED PR #417.

## What this package gives you

- **Importable and unit-testable** without depending on
  anything under `~/.hermes` or any user-specific absolute
  path.
- **Configurable** via a TOML file that the validator
  rejects if it contains tokens or absolute user-specific
  paths.
- **Operational documentation** for installation, upgrade,
  rollback, isolated testing, and runtime-state migration.
- **Strongly-typed contracts** (`contracts.py`) for every
  supervisor concept (configuration, provider policy,
  readiness state, exact-head evidence snapshot, actionable
  reviewer event, durable event-consumption record, worker
  lease, worker-launch receipt, cooldown state, terminal
  merge evidence).
- **Versioned invariant ledger** (`INVARIANTS.md`) listing
  15 invariants with enforcing implementation and asserting
  tests.
- **Dry-run validation** (`validate.py`) that checks
  configuration validity, required directories, file
  permissions, repository accessibility, provider policy,
  service-instance scope, and conflicting worker leases.

## What this package does NOT do

- It does **not** merge the PR. Merge is the only human
  boundary for both AutoDev and the supervisor.
- It does **not** redesign the reviewer-provider interface.
  The `ReviewProvider` abstraction is deferred to a later
  AutoDev generalization phase.
- It does **not** contain tokens, credentials, or
  user-specific absolute paths in any committed file.
- It does **not** modify Automated-Edge-Discovery. The
  AED-embedded copy is preserved until the standalone
  package is proven at parity.

## Layout

```text
autocoder_supervisor/
├── __init__.py
├── contracts.py
├── config.py
├── supervisor.py
├── validate.py
├── INVARIANTS.md
├── README.md
├── examples/
│   └── aed-supervisor.example.toml
├── service/
│   └── aed-supervisor@.service.template
└── docs/
    ├── INSTALL.md
    ├── UPGRADE.md
    ├── ROLLBACK.md
    ├── ISOLATED_TEST.md
    └── STATE_MIGRATION.md
```

## Quick start (isolated canary)

```bash
# 1. Pick a non-user-specific install root.
sudo install -d /opt/aed-supervisor-canary
sudo cp -r autocoder_supervisor /opt/aed-supervisor-canary/
sudo cp pyproject.toml /opt/aed-supervisor-canary/pyproject.toml

# 2. Install into a venv (no PYTHONPATH needed once installed).
sudo python3 -m venv /opt/aed-supervisor-canary/venv
sudo /opt/aed-supervisor-canary/venv/bin/pip install \
    /opt/aed-supervisor-canary

# 3. Copy the example config and edit it.
sudo cp /opt/aed-supervisor-canary/autocoder_supervisor/examples/aed-supervisor.example.toml \
    /etc/aed-supervisor-canary.toml
sudo $EDITOR /etc/aed-supervisor-canary.toml

# 4. Run the dry-run validation (installed package, no repo PYTHONPATH).
PYTHONPATH= \
    /opt/aed-supervisor-canary/venv/bin/python \
    -m autocoder_supervisor.validate --config /etc/aed-supervisor-canary.toml

# 5. Run a single iteration (--once exits after one heartbeat).
PYTHONPATH= \
    AED_PR_NUMBER=<N> AED_REPO_OWNER=<owner> AED_REPO_NAME=<repo> \
    AED_AUTHORITATIVE_HEAD=<head_sha> \
    /opt/aed-supervisor-canary/venv/bin/python \
    -m autocoder_supervisor.supervisor \
    --config /etc/aed-supervisor-canary.toml --once
```

See `docs/INSTALL.md` for production installation.
