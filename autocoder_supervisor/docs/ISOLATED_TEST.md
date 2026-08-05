# Isolated testing

The supervisor supports isolated testing out of the box.
This document covers three common patterns.

## 1. Pure unit tests (recommended)

The supervisor package is importable and testable without
the daemon loop. The existing test suite
(`tests/test_autocoder_supervisor.py`) runs the focused
supervisor tests under a second using isolated state
directories and mocked GitHub/provider responses:

```bash
python3 -m pytest -q tests/test_autocoder_supervisor.py --no-header
```

The tests do not require any host-specific state and never
mutate the real PR.

## 2. CLI-level isolated run

The supervisor binary supports an `--isolated-state` flag
that creates a fresh state directory under
`tempfile.mkdtemp(prefix="aed-supervisor-")`:

```bash
PYTHONPATH=. \
    python3 -m autocoder_supervisor.supervisor --isolated-state --once
```

This is useful for smoke-testing the daemon loop against a
mocked environment.

## 3. Per-test state directory via fixture

When you need to run a real daemon iteration but want
deterministic state, use the `isolated_state` fixture
defined in `tests/test_autocoder_supervisor.py`:

```python
def test_my_supervisor_scenario(isolated_state):
    supervisor.write_snapshot("A", _clean_snap())
    supervisor.write_readiness_state({...})
    # ...
```

The fixture patches the supervisor's module-level path
globals to point at a temporary directory; the test can read
and write supervisor state without affecting anything else.

## 4. Deterministic GitHub provider responses

The supervisor module exposes
`supervisor.capture_live_snapshot` and
`supervisor.run_iteration_v5` as public functions. Patch
them with `unittest.mock.patch.object` to inject canned
snapshots and assert on the supervisor's decisions.

Example:

```python
with patch.object(supervisor, "capture_live_snapshot",
                  return_value=clean_snap):
    it = supervisor.run_iteration_v5({"current_head": AUTH}, token="")
assert it["decision"] == "skip"
```

## 5. CI integration

The recommended CI gate for any change to this package is:

```bash
python3 -m pytest -q tests/test_autocoder_supervisor.py --no-header
```

If the focused suite is green, the full repository suite can
be run:

```bash
python3 -m pytest -q --no-header
```

This must also pass with its real exit code (0).
