"""#8784: updater tests own their module identity and their git checkout.

Dropping ``api.updates`` from ``sys.modules`` for a test (the early-boot cases in test_issue1633) must not
leave two module objects behind when something imports the module meanwhile: ``mock.patch("api.updates.x")``
and ``from api import updates`` resolve the ``api`` package attribute, while the routes' ``from api.updates
import x`` resolves ``sys.modules``, so a split sends every later stub to the wrong object.
"""
from __future__ import annotations

import importlib
import sys

from tests._updates_module_isolation import (
    updates_module_identity_is_single,
    updates_module_unloaded,
)


def test_unloading_then_importing_the_updates_module_keeps_one_identity():
    import api

    before = sys.modules["api.updates"]
    assert api.updates is before
    with updates_module_unloaded():
        assert "api.updates" not in sys.modules
        assert "updates" not in api.__dict__
        # What the gate hit: a lazy import of api.updates while a test has it unloaded.
        reimported = importlib.import_module("api.updates")
        assert reimported is not before
    assert sys.modules["api.updates"] is before
    assert api.updates is before
    assert updates_module_identity_is_single()


def test_unloading_restores_an_absent_module_as_absent(monkeypatch):
    import api

    monkeypatch.delitem(sys.modules, "api.updates", raising=False)
    monkeypatch.delattr(api, "updates", raising=False)
    with updates_module_unloaded():
        importlib.import_module("api.updates")
    assert "api.updates" not in sys.modules
    assert "updates" not in api.__dict__
    assert updates_module_identity_is_single()


def test_patch_target_and_route_import_resolve_the_same_object():
    """The object the routes import at call time is the one a string patch target lands on."""
    from unittest.mock import patch

    with updates_module_unloaded():
        importlib.import_module("api.updates")
    from api.updates import check_for_updates as route_view  # noqa: F401  (what routes do)
    with patch("api.updates.check_for_updates", lambda **kw: "stubbed"):
        from api.updates import check_for_updates as patched_view
        assert patched_view(force=True) == "stubbed"
