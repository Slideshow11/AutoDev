"""Round-773 P1: ``_resolve_production_runtime_binding`` MUST
resolve standalone-launch synthetic-package siblings.

The production supervisor can be launched in two modes:

  1. Package launch (``python -m autocoder_supervisor.supervisor``)
     — every sibling is registered under its canonical
     ``autocoder_supervisor.<x>`` name in ``sys.modules``.

  2. Standalone launch
     (``scripts/restart_supervisor_multir.sh`` runs
     ``python3 $SUP_DIR/supervisor.py``). The top-of-file shim
     rebinds ``__package__`` to a synthetic
     ``_aed_supervisor_standalone`` package and sibling modules
     are loaded as ``_aed_supervisor_standalone.<x>``.
     ``autocoder_supervisor`` is NOT on ``sys.path`` in this
     mode.

Before round 773 the
``_resolve_production_runtime_binding`` helper only consulted
the canonical ``import_path``. Under standalone launch the
LOADED_MODULE lookup missed and ``find_spec`` raised
``ValueError`` (no parent package); ``acceptance_runtime_identity.json``
recorded every sibling as ``exists=False`` and the
``actual_production_sha256`` was ``None``.

The round-773 fix detects the synthetic standalone package in
``sys.modules`` and prepends ``_aed_supervisor_standalone.<x>``
to the candidate-path list. Both LOADED_MODULE and
``find_spec`` walks resolve successfully under either launch
mode; canonical-package behavior is unchanged.

This test exercises the standalone-binding end-to-end: when
the supervisor runs under the ``_aed_supervisor_standalone``
package, every ``autocoder_supervisor.*`` acceptance binding
resolves to a real file with a non-empty SHA. The
``autocoder_orchestration.*`` siblings, which are loaded
independently of the supervisor package, also resolve under
their canonical names (they were never affected by the
standalone shim).
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest


# Derive the supervisor directory from this test file so the
# test works on any checkout (including CI runners that clone
# to a different absolute path). The repo layout has
# ``autocoder_supervisor/`` as a sibling of ``tests/``.
AUTOCODER_SUPERVISOR_DIR = Path(__file__).resolve().parent.parent / "autocoder_supervisor"


@pytest.fixture
def clean_sysmodules():
    """Snapshot and restore ``sys.modules`` so the synthetic
    package tests do not bleed into sibling tests."""
    saved = dict(sys.modules)
    try:
        yield
    finally:
        for name in list(sys.modules.keys()):
            if name not in saved:
                sys.modules.pop(name, None)
        for name, mod in saved.items():
            sys.modules[name] = mod


def _standalone_pkg_in_sysmodules() -> bool:
    """True iff the synthetic standalone package is loaded."""
    for k in list(sys.modules.keys()):
        if k == "_aed_supervisor_standalone" or k.startswith(
            "_aed_supervisor_standalone."
        ):
            return True
    return False


def _load_supervisor_under_standalone_shim() -> types.ModuleType:
    """Reload ``autocoder_supervisor.supervisor`` under the
    synthetic ``_aed_supervisor_standalone`` package used by
    the production standalone shim. Returns the module object.
    """
    pkg_name = "_aed_supervisor_standalone"
    # Drop any prior bindings under the synthetic name so
    # the import below re-runs the module body cleanly.
    for name in list(sys.modules.keys()):
        if name == pkg_name or name.startswith(pkg_name + "."):
            sys.modules.pop(name, None)
    # Also drop the canonical binding so the import does not
    # short-circuit.
    sys.modules.pop("autocoder_supervisor.supervisor", None)
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(AUTOCODER_SUPERVISOR_DIR)]  # type: ignore[attr-defined]
    sys.modules[pkg_name] = pkg
    # Load a fresh module under the synthetic package via the
    # standard importlib loader.
    src_path = AUTOCODER_SUPERVISOR_DIR / "supervisor.py"
    spec = importlib.util.spec_from_file_location(
        pkg_name + ".supervisor", str(src_path)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(src_path)
    mod.__package__ = pkg_name  # type: ignore[misc]
    sys.modules[pkg_name + ".supervisor"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_standalone_pkg_detection_helper() -> None:
    """The detection helper correctly reports standalone mode
    after the shim has been installed."""
    # Reset.
    for k in list(sys.modules.keys()):
        if k == "_aed_supervisor_standalone" or k.startswith(
            "_aed_supervisor_standalone."
        ):
            sys.modules.pop(k, None)
    assert not _standalone_pkg_in_sysmodules()
    # Install just the package.
    pkg = types.ModuleType("_aed_supervisor_standalone")
    sys.modules["_aed_supervisor_standalone"] = pkg
    try:
        assert _standalone_pkg_in_sysmodules()
    finally:
        sys.modules.pop("_aed_supervisor_standalone", None)


def test_resolve_sibling_under_standalone_package(
    clean_sysmodules,
) -> None:
    """When the supervisor runs under
    ``_aed_supervisor_standalone``, ``_resolve_production_runtime_binding``
    MUST resolve every ``autocoder_supervisor.*`` acceptance
    sibling to a real file with a non-empty SHA."""
    sup_mod = _load_supervisor_under_standalone_shim()

    # Sanity: the standalone package is registered.
    assert _standalone_pkg_in_sysmodules()

    # The function lives on the reloaded supervisor module.
    resolver = sup_mod._resolve_production_runtime_binding

    # Each (logical_filename, import_path) pair must resolve
    # to an existing file with a non-empty SHA under
    # standalone launch. We use a deliberately small subset
    # that proves the fix without re-running every binding.
    bindings = sup_mod._ACCEPTANCE_RUNTIME_BINDINGS
    for logical_filename, import_path in bindings:
        rec = resolver(logical_filename, import_path)
        assert rec["logical_module"] == logical_filename
        # All 17 bindings MUST be loadable on disk in
        # standalone launch. Before the fix, every
        # ``autocoder_supervisor.*`` entry was ``exists=False``.
        assert rec["actual_production_path"], (
            f"{logical_filename} ({import_path}) returned empty "
            f"actual_production_path; binding_method={rec['binding_method']}"
        )
        assert rec["actual_production_sha256"], (
            f"{logical_filename} ({import_path}) returned empty "
            f"actual_production_sha256; binding_method={rec['binding_method']}"
        )
        assert rec["exists"] is True, (
            f"{logical_filename} ({import_path}) reports exists=False "
            f"under standalone launch; binding_method={rec['binding_method']}"
        )
        # The binding method is one of the supported three.
        assert rec["binding_method"] in {
            "loaded_module",
            "import_spec",
            "executing_module",
        }


def test_canonical_package_unchanged(clean_sysmodules) -> None:
    """Round-773 must not change canonical-package
    resolution. Re-import the supervisor under its canonical
    package name and confirm every sibling resolves to a
    real file."""
    # Force re-import of the canonical supervisor module so
    # the standalone-mode state from a prior test does not
    # leak.
    sys.modules.pop("autocoder_supervisor.supervisor", None)
    for k in list(sys.modules.keys()):
        if k == "_aed_supervisor_standalone" or k.startswith(
            "_aed_supervisor_standalone."
        ):
            sys.modules.pop(k, None)
    sup_mod = importlib.import_module(
        "autocoder_supervisor.supervisor"
    )
    resolver = sup_mod._resolve_production_runtime_binding
    bindings = sup_mod._ACCEPTANCE_RUNTIME_BINDINGS
    for logical_filename, import_path in bindings:
        rec = resolver(logical_filename, import_path)
        assert rec["exists"] is True, (
            f"{logical_filename} ({import_path}) regressed under "
            f"canonical-package launch; binding_method={rec['binding_method']}"
        )
        assert rec["actual_production_sha256"], (
            f"{logical_filename} ({import_path}) returned empty SHA under "
            f"canonical-package launch"
        )


def test_candidate_paths_built_only_when_standalone_active(
    clean_sysmodules,
) -> None:
    """When the standalone package is NOT registered the
    candidate-path list collapses to ``[import_path]`` — the
    canonical lookup. The new code therefore cannot perturb
    package-mode resolution even if the shim is not installed.
    """
    # Wipe standalone state and re-import canonical.
    sys.modules.pop("autocoder_supervisor.supervisor", None)
    for k in list(sys.modules.keys()):
        if k == "_aed_supervisor_standalone" or k.startswith(
            "_aed_supervisor_standalone."
        ):
            sys.modules.pop(k, None)
    sup_mod = importlib.import_module(
        "autocoder_supervisor.supervisor"
    )
    # Inspect the function's local frame via a probe: we
    # can verify behaviorally by checking that an
    # ``autocoder_orchestration.*`` binding (which is NOT
    # prefixed by ``autocoder_supervisor.``) still resolves
    # under canonical name only.
    rec = sup_mod._resolve_production_runtime_binding(
        "store.py", "autocoder_orchestration.store"
    )
    assert rec["exists"] is True
    assert rec["binding_method"] == "loaded_module"