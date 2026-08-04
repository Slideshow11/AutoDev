# AutoDev

A review-governed autonomous software-development system.

## Naming

- **AutoDev** is the autonomous-development system and the
  public repository identity.
- **Humphry** is the current coding-agent persona operating
  through AutoDev.
- The internal Python package is named `autocoder_supervisor`
  for compatibility with the reviewed v1 implementation
  extracted from `Slideshow11/Automated-Edge-Discovery`
  PR #417.

This is **not** a finished autonomous software company. This
extraction is a bounded mechanical step that moves the
supervisor v1 implementation from the AED repository into
its own standalone project. Generalisation of the
`ReviewProvider` abstraction, additional model providers,
an app-building UI, and hosted multi-tenant deployment are
**deferred** to a later AutoDev phase.

## What AutoDev v1 currently provides

This repository contains the standalone supervisor v1:

- A **durable external supervisor** with a Python package
  interface and configuration loader.
- **One-writer enforcement** so two concurrent supervisor
  instances cannot race on the same PR scope.
- **Event deduplication** so repeated observation of the
  same CodeRabbit review evidence does not launch another
  worker.
- **Restart recovery** that preserves state across a
  supervisor process restart without duplicating workers.
- **Exact-head review evidence** that requires the provider's
  review to cover the exact current PR head.
- **Configurable repository and PR scope** through a TOML
  configuration that the validator rejects if it contains
  tokens or absolute user-specific paths.
- **Required and optional reviewer policies** with
  independent state machines (rate-limited Codex never
  pauses CodeRabbit).
- **Provisional readiness** while the PR is open and active
  monitoring once `AWAITING_MERGE_AUTHORIZATION` is
  reached.
- **Merge-only human authorization** — no merge occurs
  without explicit operator authorisation against the
  exact head.
- **Installed-package deployment** with both system-wide
  and virtualenv install paths, restrictive runtime
  permissions, and a systemd template unit.

See `autocoder_supervisor/README.md` for the package-level
overview and `autocoder_supervisor/INVARIANTS.md` for the
15 versioned invariants the supervisor enforces.

## What AutoDev v1 does NOT provide

AutoDev v1 is intentionally narrow. The current extraction
does **not** provide:

- a general `ReviewProvider` interface;
- arbitrary model-provider adapters beyond the existing
  CodeRabbit (required) and Codex (optional) handlers;
- an app-building UI;
- mobile application generation;
- local-model routing;
- a hosted service;
- multi-tenant isolation;
- billing;
- commercial deployment;
- a finished autonomous software company.

Do not market unimplemented functionality as complete.

## Source

The supervisor v1 implementation was extracted from
`Slideshow11/Automated-Edge-Discovery` PR #417. The exact
source commit and reviewed head are recorded in
`provenance/aed-pr417-source-manifest.json`. The extraction
narrative is in `provenance/EXTRACTION.md`.

## Status

This repository is **public**. License terms have not yet
been selected. The first functional pull request is the
supervisor-v1 extraction from AED PR #417; that PR is
**not** authorised to merge in this phase.

## License

License terms have not yet been selected.
