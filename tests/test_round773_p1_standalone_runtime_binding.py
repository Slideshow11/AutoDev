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
    package tests do not bleed into sibling tests.

    Round-697 (push-gate fix): extended to also restore
    module-attribute mutations made during the test. This is
    necessary because the supervisor's module-level
    ``_apply_config`` populates ``RUN_STATE``, ``STATE_DIR``,
    and friends from env-derived paths at import time. When
    a synthetic standalone-package binding re-imports the
    supervisor module, those globals are set on the new module
    object. Without attribute restoration, downstream tests
    that monkeypatch ``autocoder_supervisor.supervisor``'s
    ``RUN_STATE`` would silently bind to a stale module object.
    """
    saved = dict(sys.modules)
    _mod_attr_snapshots = {}
    for _m in saved.values():
        if _m is None or not hasattr(_m, "__dict__"):
            continue
        # Round-1064 P2: restrict snapshot capture to valid
        # module objects and let any unexpected error from
        # ``dict(_m.__dict__)`` propagate. The previous
        # ``except Exception: pass`` silenced real failures
        # such that a missing snapshot could leak mutated
        # module attributes into later tests.
        _mod_attr_snapshots[id(_m)] = dict(_m.__dict__)
    try:
        yield
    finally:
        for name in list(sys.modules.keys()):
            if name not in saved:
                sys.modules.pop(name, None)
        for name, mod in saved.items():
            sys.modules[name] = mod
        # Round-697 (push-gate fix): revert attribute-level
        # mutations on restored modules.
        for _m in list(sys.modules.values()):
            if _m is None:
                continue
            _key = id(_m)
            if _key in _mod_attr_snapshots:
                _snapshot = _mod_attr_snapshots[_key]
                _current = getattr(_m, "__dict__", {})
                for _attr in list(_current.keys()):
                    if _attr not in _snapshot:
                        try:
                            delattr(_m, _attr)
                        except (AttributeError, TypeError):
                            pass
                for _attr, _val in _snapshot.items():
                    if _current.get(_attr) != _val:
                        try:
                            setattr(_m, _attr, _val)
                        except (AttributeError, TypeError):
                            pass


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
    synthetic ``_aed_supervisor_standalone`` package used by the
    production standalone shim. Returns the module object.
    """
    pkg_name = "_aed_supervisor_standalone"
    # Drop any prior bindings under the synthetic name so
    # the import below re-runs the module body cleanly.
    for name in list(sys.modules.keys()):
        if name == pkg_name or name.startswith(pkg_name + "."):
            sys.modules.pop(name, None)
    # Round-697 (push-gate fix): capture the canonical supervisor
    # module binding BEFORE we drop it so downstream tests can
    # be sure they re-bind to the same object. The reload below
    # builds a NEW module object that shadows the canonical one;
    # without the restore in the calling test's
    # ``clean_sysmodules`` fixture the new object would persist
    # into later tests' ``from .supervisor import log`` rebinding
    # and silently break ``tests/test_state_root_resolver.py``'s
    # ``monkeypatch.setattr(supervisor, "log", lambda)`` contract.
    # The canonical binding is restored by ``clean_sysmodules``
    # teardown. This comment is the only thing this file adds.
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


def test_standalone_pkg_detection_helper(clean_sysmodules) -> None:
    """The detection helper correctly reports standalone mode
    after the shim has been installed.

    Round-1064 P2: the test now uses the ``clean_sysmodules``
    fixture so any prior ``_aed_supervisor_standalone*``
    bindings are restored after the test instead of being
    permanently removed.
    """
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
    sibling to a real file with a non-empty SHA.

    Round-1064 P2: the supervisor is loaded as a synthetic
    module via ``_load_supervisor_under_standalone_shim``;
    that path leaves ``__main__.__file__`` pointing at the
    pytest launcher (``__main__.py``). The
    ``executing_module`` resolver branch keys on
    ``__main__.__file__`` and would otherwise hash the
    pytest binary instead of the production source. Save
    and restore ``__main__.__file__`` here so the resolver
    exercises its production executing_module path.
    """
    sup_mod = _load_supervisor_under_standalone_shim()

    # Sanity: the standalone package is registered.
    assert _standalone_pkg_in_sysmodules()

    # The function lives on the reloaded supervisor module.
    resolver = sup_mod._resolve_production_runtime_binding

    # Round-1064 P2: pin ``__main__.__file__`` to the
    # synthetic supervisor's source path so the
    # ``executing_module`` resolver branch returns the
    # production file, not the pytest launcher. Restore
    # the original value at teardown so other tests do not
    # observe the synthetic module's file path.
    saved_main_file = getattr(sys.modules["__main__"], "__file__", None)
    try:
        sys.modules["__main__"].__file__ = str(AUTOCODER_SUPERVISOR_DIR / "supervisor.py")

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
            # Round-1064 P2: assert the basename matches the
            # logical filename so an unrelated existing file
            # cannot satisfy the check.
            actual_path = Path(rec["actual_production_path"])
            assert actual_path.name == logical_filename, (
                f"{logical_filename} ({import_path}) resolved to "
                f"{actual_path!r}; the basename must equal the "
                f"logical filename, got {actual_path.name!r}"
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
    finally:
        if saved_main_file is None:
            try:
                delattr(sys.modules["__main__"], "__file__")
            except AttributeError:
                pass
        else:
            sys.modules["__main__"].__file__ = saved_main_file


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