# AUTOCODER SOURCE COMPLETENESS AUDIT

**Audit reference:** `provenance/AUTOCODER_SOURCE_COMPLETENESS.json`
**Schema:** `autocoder.source_completeness.v1`
**AED reference commit:** `b57fcaad806c68b93668bcd318fa26ab15a8ab40`
**AutoDev reference head:** `e99c33aa8b857600e70941637a7007af83af0a64`

## 1. Purpose

The directive for AutoDev PR #1 requires a full source-completeness audit at
AED commit `b57fcaad806c68b93668bcd318fa26ab15a8ab40`. The audit enumerates
every tracked file in AED, classifies each file into exactly one of the
prescribed dispositions, documents the supervisor-v1 runtime dependency
closure, proves clean-install independence from the AED repository layout,
and decides whether PR #1 is self-contained or requires additional
supervisor-v1 migrations.

## 2. Inventory at AED `b57fcaad`

The audit enumerated **713 tracked files** in AED at the reference commit.

| Disposition                                  | Count | Description |
| -------------------------------------------- | ----- | ----------- |
| `COPIED_BYTE_IDENTICAL`                      | 1     | Files copied byte-for-byte into AutoDev (verified by SHA-256 equality) |
| `TRANSFORMED_IN_AUTODEV`                     | 16    | Files copied with path / branding / packaging transformation |
| `RETAINED_AS_AED_SPECIFIC_INTEGRATION`       | 457   | AED-side code retained in AED (lifecycle, policy, scripts, docs, engine, tests) |
| `HISTORICAL_OR_RUNTIME_EVIDENCE_EXCLUDED`    | 239   | AED fixture data, corpus evidence, egg-info build artifacts |
| **Unclassified**                             | **0** | **Zero unclassified candidates** |

The total disposition count is 713 — every AED tracked file is classified.

## 3. Supervisor-v1 runtime inventory

The supervisor-v1 runtime consists of five Python modules shipped by the
`autocoder-supervisor` wheel:

```
autocoder_supervisor/__init__.py
autocoder_supervisor/config.py
autocoder_supervisor/contracts.py
autocoder_supervisor/supervisor.py
autocoder_supervisor/validate.py
```

Plus the nine documented doc/config files outside the package proper:

```
autocoder_supervisor/INVARIANTS.md
autocoder_supervisor/README.md
autocoder_supervisor/docs/INSTALL.md
autocoder_supervisor/docs/ISOLATED_TEST.md
autocoder_supervisor/docs/ROLLBACK.md
autocoder_supervisor/docs/STATE_MIGRATION.md
autocoder_supervisor/docs/UPGRADE.md
autocoder_supervisor/examples/aed-supervisor.example.toml
autocoder_supervisor/service/aed-supervisor@.service.template
```

Plus the pyproject.toml at the repo root and the migrated test files:

```
pyproject.toml (root)
tests/test_autocoder_supervisor.py
tests/test_autocoder_supervisor_packaging.py
```

Total: 17 source files migrated from AED. Plus 10 standalone AutoDev
additions (README, INVARIANTS, CI workflows, scanner, scanner-allowlist,
etc.) that have no AED source counterpart.

## 4. Dependency closure

The audit inspected every import and subprocess invocation inside the
supervisor-v1 runtime:

### 4.1 Python imports

Within the supervisor-v1 package, the relative-import graph is:

```
autocoder_supervisor.__init__ → (none)
autocoder_supervisor.config   → .contracts
autocoder_supervisor.contracts→ (none)
autocoder_supervisor.supervisor → .config, .contracts
autocoder_supervisor.validate → .config, .contracts
```

**No import references `aed_lifecycle`, `aed_policy`, `scripts.local`,
`aed_continue_pr`, `aed_executor_packet`, `aed_launch_receipt`,
`_shared_*`, `_ledger_*`, or any other AED-layout module.**

The stdlib imports are: argparse, errno, fcntl, json, os, re, subprocess,
sys, time, urllib.error, urllib.request, uuid, datetime, pathlib, typing,
dataclasses, shutil, `__future__`.

### 4.2 Subprocess invocations

| Executable | Call site | Purpose |
| ---------- | --------- | ------- |
| `git` | `validate.py:103` | `git status --porcelain` on the operator-supplied working_checkout |
| `git` | `validate.py:315` | `git rev-parse HEAD` on the operator-supplied working_checkout |
| `gh`  | `supervisor.py:1063` | `gh pr comment --body` to post CodeRabbit review requests |
| `which` | `supervisor.py:931` | locate the `hermes` binary on PATH (fallback) |
| `<configured-hermes-binary>` | `supervisor.py:1001` | launch the worker process in its own session |

None of these subprocess calls reference the AED repository layout.

### 4.3 Files read at runtime

The supervisor reads and writes only:

- `/etc/aed-supervisor/<instance>/aed-supervisor.toml` (operator config)
- `/etc/aed-supervisor/<instance>/supervisor.env` (operator environment)
- `/var/lib/aed-supervisor/<instance>/state/*` (private state, supervisor-owned)
- `/var/log/aed-supervisor/*` (private log dir, supervisor-owned)
- The operator-supplied `working_checkout` (a git checkout, validated with `git`)

None of these are AED source-tree paths.

### 4.4 Verdict

**The supervisor-v1 runtime has zero AED-internal dependencies at the
Python import, subprocess call, or filesystem-read level.** Every dependency
either resolves to Python stdlib, the package's own internal relative
imports, or absolute operator-supplied paths.

## 5. Clean-install proof

The audit built the standalone wheel from the AutoDev source tree and
installed it into a fresh, AED-free venv.

| Step | Command | Exit |
| ---- | ------- | ---- |
| Wheel build | `python3 -m build --wheel --outdir /tmp/wheels` | 0 |
| Venv creation | `python3 -m venv /tmp/clean_venv` | 0 |
| Install | `/tmp/clean_venv/bin/pip install /tmp/wheels/autocoder_supervisor-1.0.0-py3-none-any.whl` | 0 |
| Import smoke | `/tmp/clean_venv/bin/python3 -c 'import autocoder_supervisor'` | 0 |
| Submodule imports | `/tmp/clean_venv/bin/python3 -c 'from autocoder_supervisor import supervisor, validate, config, contracts'` | 0 |
| CLI help (supervisor) | `/tmp/clean_venv/bin/python3 -m autocoder_supervisor.supervisor --help` | 0 |
| CLI help (validate) | `/tmp/clean_venv/bin/python3 -m autocoder_supervisor.validate --help` | 0 |
| AED-path grep | `grep -rnE 'aed_lifecycle\|aed_policy\|_shared_\|_ledger_\|aed_continue\|aed_executor\|aed_launch\|scripts/local\|scripts/ci' /tmp/clean_venv/lib/python3.11/site-packages/autocoder_supervisor/` | 1 (no matches) |

The wheel has SHA-256 `f14683f3b12ab4b250dc66d5d76f8bca15ad0b560bfdaf2598e0f9e0849d8084`
and ships 9 files (5 source modules + 4 dist-info metadata files).

The clean venv was created with no AED checkout on `PYTHONPATH`, no
source-tree import fallback, and the working directory outside AED.

## 6. Extracted-manifest cross-check

The audit cross-checked every AED source file in
`provenance/aed-pr417-source-manifest.json` against the new audit's
classification:

- 17 manifest source files: all classified as either
  `COPIED_BYTE_IDENTICAL` (11) or `TRANSFORMED_IN_AUTODEV` (6).
- 10 standalone AutoDev additions: all classified as standalone additions
  (not part of the AED inventory because they have no AED source).

**Every manifest source path maps to an `EXTRACTED` classification.**

The byte-identical subset is verified by `source_sha256 == destination_sha256`
in the provenance manifest; the only file with matching source and
destination hashes is `autocoder_supervisor/__init__.py`. The other
sixteen extracted files carry `transformation_classification` values
in the provenance manifest indicating path, branding, or packaging
transformations applied during extraction.

## 7. PR scope decision

**Verdict:** `PR_1_CONTAINS_SELF_CONTAINED_SUPERVISOR_V1`

PR #1 contains the entire supervisor-v1 runtime. No direct supervisor-v1
dependency is missing. No expansion into a broader AED automation rewrite is
required for supervisor-v1 independence.

### Retained AED-specific components

The following AED subsystems are retained in AED because they are not
supervisor-v1 runtime dependencies:

- `aed_lifecycle/` (4 files) — AED lifecycle subsystem
- `aed_policy/` (6 files) — AED policy subsystem
- `scripts/local/_shared_*.py`, `scripts/local/_ledger_review_shared.py`,
  `scripts/local/_smoke_shared.py`, `scripts/local/_production_facade.py`
  (8 files) — AED shared helpers
- `scripts/local/aed_*.py` (15 files) — AED operation scripts (PR continuation,
  executor packet, launch receipt, mutation authorization, run identity,
  supervisor lock, tasker, etc.)
- `scripts/local/` remainder (77 files) — AED controller-side scripts
  (autocoder_run_controller, phase_exec, phase_ledger, plan_preview_*,
  mutation_policy, etc.)
- `scripts/ci/` (3 files) — AED CI shell scripts
- `.github/` (6 files) — AED GitHub Actions workflows + PR template
- `schemas/` (12 files) — AED JSON schemas (trial ledger, edge hypothesis, etc.)
- `docs/governance/` (5 files) — AED governance design docs
- `docs/` remainder (111 files) — AED architecture, roadmaps, controller design,
  executor packet usage, lifecycle state registry, codex remediation, etc.
- `engine/` (35 files) — AED core edge-discovery engine
- `examples/` (9 files) — AED edge-discovery examples
- `tests/` (155 files) — AED tests for the AED-side subsystems
- `bin/` (2 files) — AED bin/ helper scripts
- `corpus/` (7 files) — historical corpus evidence
- `fixtures/` (226 files) — AED fixture data (schemas, hypothesis cards)
- `automated_edge_discovery.egg-info/` (5 files) — Python egg metadata
- Root metafiles (`Makefile`, `README.md`, `setup.cfg`, `setup.py`,
  `pyproject.toml`, `requirements.txt`, `.gitignore`, `.review_check.py`,
  `.review_check.sh`, `0`)

### Follow-up AutoDev migrations (deferred, not required for supervisor-v1)

Future AutoDev PRs may extract individual AED scripts. None of these
extractions are required for supervisor-v1 runtime independence. The
migration candidates are documented as
`RETAINED_AS_AED_SPECIFIC_INTEGRATION` rather than
`FOLLOW_UP_AUTODEV_MIGRATION_REQUIRED` because the audit cannot bind the
extraction timing — the directive requires that **no supervisor-v1 runtime
dependency be deferred**, and none of these retained files are supervisor-v1
runtime dependencies.

## 8. Audit tests

Two test files enforce the audit; both were added by PR #1:

1. `tests/test_autocoder_supervisor_source_completeness.py` — schema
   validation, disposition completeness, hash integrity, supervisor-v1
   dependency-closure proof, clean-install proof regression,
   no-AED-path-leak regression, manifest-match cross-check, supervisor-v1
   scope decision invariant.

2. `tests/test_extraction_provenance.py` — references the provenance
   manifest for byte-identical and hash verification. The audit
   cross-checks this manifest against its own classification.

(PR #1 ships these tests because they validate the extraction's own
manifests and the supervisor's behavior; they are not AED-side test
code. AED-side tests live in `Automated-Edge-Discovery/tests/` and
are not part of the AutoDev repo.)

## 9. Summary

PR #1's supervisor-v1 is genuinely independent of the AED repository.
No `aed_lifecycle`, `aed_policy`, `scripts/local/*`, `scripts/ci/*`,
`_shared_*`, `_ledger_*`, or other AED-layout module is referenced from
the supervisor's Python imports, subprocess commands, or filesystem reads.
The wheel installs cleanly into a fresh venv with no AED checkout. The
audit enumerates 713 AED tracked files, classifies every one of them into
exactly one prescribed disposition, and provides zero unclassified
candidates.