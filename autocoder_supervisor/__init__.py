"""AED Autocoder Supervisor (source-controlled).

This package contains the source-controlled version of the
external Autocoder supervisor that has been operating as a
host-level daemon under ``~/.hermes/aed-supervisor/``. The
working v5 supervisor was ported into the AED repository
without changing its semantics, so the new branch can be
operated by a source-controlled instance while the historical
external supervisor keeps its archived state under the original
home directory.

Package contents
----------------

``contracts``
    Strongly-typed definitions for every supervisor concept
    (configuration, provider policy, readiness state, exact-head
    evidence snapshot, actionable reviewer event, durable
    event-consumption record, worker lease, worker-launch
    receipt, cooldown state, terminal merge evidence).

``config``
    Configuration loader (``SupervisorConfig``) plus validation
    that rejects unsafe values (tokens, hard-coded absolute
    user-specific paths).

``supervisor``
    The working v5 supervisor implementation, refactored so
    that module-level paths/policy are populated from a
    ``SupervisorConfig`` instance. The same function surface is
    preserved so that the existing ported test suite continues
    to drive the supervisor via monkeypatched globals.

``validate``
    Dry-run validation command that checks configuration,
    directories, file permissions, repository accessibility,
    provider policy, service-instance scope and conflicting
    worker leases.

The supervisor source is intentionally *not* a re-design. The
behavioural contracts and invariant ledger are the canonical
description of the system. The ``ReviewProvider`` abstraction
described in the long-term README is intentionally deferred to
the standalone Autocoder extraction and is **not** implemented
inside this stabilization PR. The supervisor source is
importable and runnable.
"""
from __future__ import annotations

# Submodules are not eagerly imported here because importing
# ``supervisor`` triggers the configuration bootstrap (which
# loads environment variables and constructs the PROVIDERS
# dict from ``SupervisorConfig.default_config_from_env()``).
# Import the submodules you need explicitly:
#
#     from autocoder_supervisor import supervisor
#     from autocoder_supervisor import validate
#     from autocoder_supervisor import config
#     from autocoder_supervisor import contracts
