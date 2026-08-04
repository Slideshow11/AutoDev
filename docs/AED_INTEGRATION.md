# AED integration (consumer migration plan)

This document describes the **future** integration of
AutoDev as a consumer dependency inside the
`Slideshow11/Automated-Edge-Discovery` (AED) repository.

This phase does **not** modify AED. The standalone package
must be proven at parity before the AED-embedded copy is
removed.

## Goals

1. AED no longer carries its own supervisor copy.
2. AED consumes the standalone `autocoder_supervisor`
   package from AutoDev.
3. The existing supervisor PR-417 evidence and the
   external-supervisor audit evidence (under
   `~/.hermes/aed-supervisor/`) remain preserved.

## Step-by-step plan (for execution in a future reviewed PR)

1. **Publish or reference a reviewed AutoDev release.**
   Tag a release commit on `Slideshow11/AutoDev` after the
   extraction PR has been reviewed and merged in the
   standalone repository.

2. **Update AED to consume the standalone package.** In the
   AED repository, remove the
   `scripts/local/autocoder_supervisor/` directory and add
   a dependency declaration pointing at the standalone
   AutoDev release.

3. **Run parity tests.** The parity tests are the focused
   supervisor suite and the packaging regression suite,
   ported into AutoDev. They must pass on the AED consumer
   side before the embedded copy is removed.

4. **Remove the embedded copy only after the standalone
   dependency is proven.** Do not delete the AED-embedded
   supervisor copy until the AED side has been observed
   running the standalone package against a real PR scope
   in a non-trivial time window.

5. **Preserve old runtime evidence.** The historical
   supervisor PR-416 audit evidence and the
   `~/.hermes/aed-supervisor/` audit evidence are preserved
   indefinitely as run history. The AutoDev extraction
   does not erase them.

## Required changes in AED (deferred to the future PR)

- Remove `scripts/local/autocoder_supervisor/`.
- Remove `scripts/local/pyproject.toml` if AED does not need
  the standalone packaging manifest at the same path.
- Replace the AED `pyproject.toml` (if any) with a
  dependency on `autocoder-supervisor` from AutoDev.
- Update AED's tests to import the standalone package.
- Update AED's CI to install the standalone package before
  running supervisor tests.
- Update the AED PR-body evidence to point at the
  AutoDev-side evidence files when those become available.

## What this phase did NOT do

- Did not modify AED.
- Did not create a dependency on AutoDev from AED.
- Did not delete the embedded supervisor copy.
- Did not change the AED-merged PR #417 evidence files.
- Did not enable the standalone supervisor to consume AED
  configurations directly; the supervisor remains a
  general PR-watchdog that AED can configure at install
  time.
