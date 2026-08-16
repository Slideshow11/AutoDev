"""Round-697 (push-gate fix): tests package marker.

Several test files in this directory import sibling test modules
as ``from tests.test_X import …`` (e.g. ``tests/test_round40_durable_dispatch.py``
importing ``tests.test_directive_bridge``). This import style requires
``tests`` to be importable as a regular Python package, which in turn
requires an ``__init__.py`` file at the package root. Without this
marker the imports raise ``ModuleNotFoundError: No module named
'tests.test_X'`` whenever pytest's collection order places the
``from tests.X import …`` test late enough that ``tests`` is not
yet cached as a package in ``sys.modules``.

This marker is intentionally empty — it adds only the package
identity required by Python's import system and does NOT introduce
any new code path, fixture, or test. It is the minimum deterministic
test-isolation correction the round-697 directive authorizes.

The marker is source-controlled so a fresh ``git clone`` of this
repository reproduces the test collection behaviour without
requiring operator intervention.
"""
