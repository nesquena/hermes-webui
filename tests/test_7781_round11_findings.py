"""Regression coverage for the 2026-11-18 #7781 review finding.

``_plugin_manager_home_context()`` used to ``yield None`` when the agent
modules or the active-home binding were unavailable. Callers entered their
plugin-manager block anyway, which resolved the AMBIENT Hermes home and could
list another profile's plugin skills. The context now fails closed (raises),
and the callers' existing handlers turn that into an empty listing / 404.
"""

from __future__ import annotations

import sys
import types

import pytest

import api.routes as routes


def test_home_context_raises_when_agent_modules_are_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_constants", None)
    with pytest.raises(RuntimeError) as excinfo:
        with routes._plugin_manager_home_context():
            pass
    assert "plugin-manager home scope unavailable" in str(excinfo.value)


def test_home_context_raises_when_the_active_home_cannot_be_bound(monkeypatch):
    fake = types.ModuleType("hermes_constants")
    fake.set_hermes_home_override = lambda home: object()
    fake.reset_hermes_home_override = lambda token: None
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)

    import api.profiles as profiles

    def boom():
        raise RuntimeError("no active home")

    monkeypatch.setattr(profiles, "get_active_hermes_home", boom)
    with pytest.raises(RuntimeError) as excinfo:
        with routes._plugin_manager_home_context():
            pass
    assert "could not be bound" in str(excinfo.value)


def test_listing_call_site_catches_a_failed_scope():
    """The listing path must catch the context failure and return no plugin
    skills: returning the ambient registry is exactly the leak the finding
    names."""
    import inspect

    source = inspect.getsource(routes._list_plugin_skills_for_response)
    context_at = source.index("with _plugin_manager_home_context():")
    guarded = source.rfind("try:", 0, context_at)
    assert guarded != -1, source
    # The failure handler that returns the empty listing must follow the call.
    tail = source[context_at:]
    assert "except Exception:" in tail
    assert "return []" in tail
