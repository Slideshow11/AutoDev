# AutoDev v1 extraction — narrative

## Why this extraction occurred

The supervisor implementation that drives the autonomous
review-and-repair loop was developed as part of the AED
repository (an internal discovery-and-edge tool) and merged
through AED PR #417 (`b57fcaa`). AutoDev v1 lifts that
supervisor into its own standalone public repository so it
can be installed, tested, and reviewed independently of the
AED codebase.

This extraction is a bounded mechanical step. It preserves
the reviewed v1 behaviour, invariants, contracts, and tests;
it does not introduce the `ReviewProvider` abstraction,
additional model providers, an app-building UI, or any
other generalisation.

## Source

| Field | Value |
| --- | --- |
| Source repository | `Slideshow11/Automated-Edge-Discovery` |
| Source PR | #417 |
| Source reviewed head | `18ba0df49d2a19779e350d6df5a102b254cbeed7` |
| Source commit (squash) | `b57fcaad806c68b93668bcd318fa26ab15a8ab40` |
| Source pre-merge main | `9697b136f311b340e4794c8a20e2568fc2e2d08a` |
| Source PR branch | `chore/version-autocoder-supervisor-v1` |
| Source commits in PR | 10 (from `f3e72d3` to `18ba0df`) |

The exact per-file source SHA-256 hashes are recorded in
`aed-pr417-source-manifest.json`. The hash record is the
source-to-destination audit trail; this document is the
narrative.

## Destination

| Field | Value |
| --- | --- |
| Destination repository | `Slideshow11/AutoDev` |
| Visibility | public |
| Default branch | `main` |
| Bootstrap commit | `be7602ff0cfd307f101b507341ed75a3e022332b` |
| Extraction branch | `feat/extract-supervisor-v1` |
| Product identity | AutoDev |
| Agent persona | Humphry |

## File categories

The extraction manifest records 17 source files and 26
destination files (17 source + 10 standalone additions = 27'd
plus 4 other destinations covered earlier). The destination tree adds a root
`INVARIANTS.md` (a copy of the package-internal ledger for
top-level discoverability), the standalone root `README.md`,
`docs/AED_INTEGRATION.md`, `.gitignore`, and the standalone
GitHub Actions workflow `.github/workflows/ci.yml`.

| Classification | Count |
| --- | --- |
| byte_identical | 9 |
| path_only | 6 |
| branding_only | 1 (the package README rewrite) |
| packaging_metadata_only | 1 |
| standalone_ci_addition | 4 |
| standalone_documentation_addition | 3 |

The 9 byte-identical files are the functional core of the
supervisor:

- `autocoder_supervisor/__init__.py`
- `autocoder_supervisor/config.py`
- `autocoder_supervisor/contracts.py`
- `autocoder_supervisor/supervisor.py`
- `autocoder_supervisor/validate.py`
- `autocoder_supervisor/docs/ROLLBACK.md`
- `autocoder_supervisor/docs/STATE_MIGRATION.md`
- `autocoder_supervisor/examples/autocoder-supervisor.example.toml`
- `autocoder_supervisor/service/autocoder-supervisor@.service.template`

The other 8 source files were re-pointed to the standalone
layout (`autocoder_supervisor/...` paths instead of
`scripts/local/autocoder_supervisor/...`) without changing
semantics. The package `README.md` is the one file where the
dominant change is branding (the public product name is
AutoDev; the internal Python package is named
`autocoder_supervisor`).

## Why each transformed file changed

Each transformed file's transformation explanation is
recorded in `aed-pr417-source-manifest.json`. In summary:

- `pyproject.toml`: moved out of `scripts/local/` to the
  repo root; updated its docstring and the
  `[tool.setuptools.packages.find]` exclude list. The
  distribution name `autocoder-supervisor` and the conditional
  `tomli` runtime dependency are preserved.
- `autocoder_supervisor/README.md`: rewritten to identify
  AutoDev as the public product while keeping the
  `autocoder_supervisor` Python package identity.
- `autocoder_supervisor/docs/INSTALL.md`,
  `autocoder_supervisor/docs/UPGRADE.md`,
  `autocoder_supervisor/docs/ISOLATED_TEST.md`,
  `INVARIANTS.md`: paths re-pointed to the standalone
  layout.
- `tests/test_autocoder_supervisor.py`: `SUPERVISOR_PKG_ROOT`
  updated to point at the parent of `tests/` directly.
- `tests/test_autocoder_supervisor_packaging.py`:
  `SOURCE_PKG` and `SOURCE_MANIFEST` updated to point at
  the standalone package and `pyproject.toml` at the repo
  root.

## What was NOT changed

- The internal Python package name `autocoder_supervisor`.
- The Python distribution name `autocoder-supervisor`.
- The existing AED_-prefixed environment variables
  (`AED_PR_NUMBER`, `AED_REPO_OWNER`, `AED_REPO_NAME`,
  `AED_AUTHORITATIVE_HEAD`, etc.).
- The configuration schema names and v1 compatibility
  fields.
- The serialized state contracts, schema names
  (`aed.autocoder_supervisor.v1`), event identifiers, and
  readiness-state names.
- The worker or supervisor behaviour.
- The provider policy.

These names remain temporarily for compatibility with the
AED-embedded copy and may be addressed by a separate
reviewed migration in a later AutoDev phase.

## History preservation

This extraction does not preserve full Git history. The
PR #417 squash commit (`b57fcaa`) is the source of truth
for the v1 behaviour. The destination repository's
`feat/extract-supervisor-v1` branch starts from the
AutoDev `main` bootstrap commit and adds the extraction as
a flat sequence of commits. The provenance manifest is the
authoritative per-file audit trail.

## What remains deferred

- The general `ReviewProvider` abstraction.
- Additional model-provider adapters.
- An app-building UI.
- Hosted multi-tenant deployment.
- Mobile-application generation.
- Local-model routing.
- Billing and accounts.
- A finished autonomous software company.

## AED was not modified

AED main, AED PR #417 evidence, and AED PR #416 evidence
remain untouched. The AED-embedded supervisor copy remains
in place until a separate reviewed consumer-migration PR
performs the AED-side integration (described in
`docs/AED_INTEGRATION.md`).

## No merge in this phase

The extraction pull request is opened against the AutoDev
`main` branch but is not authorised to merge. The merge is
the human boundary. This phase stops when the extraction
PR reaches `AWAITING_MERGE_AUTHORIZATION` on the installed-
artifact cross-repository canary.


## Open scanner finding (round 11 deferral)

Round-11 CodeRabbit thread PRRT_kwDOTtyQLc6WiHve (Sensitive Data Exposure, CWE-200) noted that the current per-file allowlist suppresses every occurrence of a token in an allowlisted file. A real `ghp_` credential appended to an allowlisted file would pass. Switching from path-based exemptions to occurrence-specific (line-anchored) exemptions is a heavy-lift refactor that is deferred to a dedicated follow-up commit. Until then, occurrences inside allowlisted files MUST be documented and reviewed; the pre-commit pipeline lists every forbidden occurrence so a reviewer can diff against the documented set.
