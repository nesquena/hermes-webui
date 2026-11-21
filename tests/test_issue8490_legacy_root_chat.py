"""Regression coverage for the 2026-11-18 #8490 review finding.

A saved chat without ``profile`` belongs to the root profile: its Hermes home
already resolves through ``get_hermes_home_for_profile(None)``. The turn's
identity used to fall back to the process-wide active profile — whichever tab
last set it — so a legacy root chat could run in another tab's named Docker
container while reading root config.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import api.streaming as streaming


def test_legacy_root_session_resolves_no_profile_even_when_another_tab_is_active(
    monkeypatch,
):
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "work")
    monkeypatch.setattr(profiles, "_active_profile", "work", raising=False)

    assert streaming._resolve_turn_profile_name(SimpleNamespace(profile=None)) is None
    assert streaming._resolve_turn_profile_name(SimpleNamespace(profile="")) is None


def test_named_session_keeps_its_own_profile():
    assert (
        streaming._resolve_turn_profile_name(SimpleNamespace(profile="work"))
        == "work"
    )


def test_streaming_turn_uses_the_session_owner_for_its_resolved_name():
    source = inspect.getsource(streaming._run_agent_streaming)
    assert "_resolved_profile_name = _resolve_turn_profile_name(s)" in source
    # The old process-wide fallback must stay gone: its value belongs to a
    # different tab and its Docker container.
    assert "_resolved_profile_name = get_active_profile_name()" not in source
