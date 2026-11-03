"""Unload ``api.updates`` for one test without splitting its identity (#8784).

Tests of the early-boot paths drop ``api.updates`` from ``sys.modules``.  If anything imports the module
while it is gone (a lazy ``from api.updates import ...`` on another code path, a background thread), a
second module object appears: ``monkeypatch.delitem`` puts the original back into ``sys.modules`` at
teardown, but the ``api`` package attribute keeps the new one.  Later tests then patch one object
(``mock.patch("api.updates.x")`` and ``from api import updates`` resolve the package attribute) while the
routes call the other (``from api.updates import x`` resolves ``sys.modules``), and the stub never reaches
the code under test.

``updates_module_unloaded`` removes both references for the duration of the block and restores both
afterwards, discarding anything imported meanwhile, so the module the routes resolve at call time is the
one every test patches.
"""
from __future__ import annotations

import contextlib
import sys

_MISSING = object()


@contextlib.contextmanager
def updates_module_unloaded():
    import api

    saved_module = sys.modules.pop("api.updates", None)
    saved_attr = api.__dict__.get("updates", _MISSING)
    if saved_attr is not _MISSING:
        delattr(api, "updates")
    try:
        yield
    finally:
        if saved_module is None:
            sys.modules.pop("api.updates", None)
        else:
            sys.modules["api.updates"] = saved_module
        if saved_attr is _MISSING:
            api.__dict__.pop("updates", None)
        else:
            api.updates = saved_attr


def updates_module_identity_is_single() -> bool:
    """True when ``sys.modules['api.updates']`` and the ``api`` package attribute agree (or both are absent)."""
    import api

    return sys.modules.get("api.updates") is api.__dict__.get("updates")
