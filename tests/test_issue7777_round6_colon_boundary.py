"""#7777 round 6 — the ``@provider:model`` boundary must be the CONFIGURED one.

Review 5489844033 (2026-10-12), still open at the 2026-11-23 re-gate:

    Colon-bearing custom ids (``api/config.py``, ``static/ui.js``):
    ``@custom:alpha:model-a:free`` is matched as ``free``. Use the
    configured custom-provider boundaries; the Agent parses it as
    ``custom:alpha`` / ``model-a:free``.

Pre-fix behaviour: ``_strip_provider_prefix_from_model_id`` walked the colon
boundaries longest-first and accepted the first candidate that
``_is_known_model_provider`` approved. That helper accepts EVERY
``custom:*`` spelling, so the longest candidate ``custom:alpha:model-a``
passed and the id was cut to ``free`` — the model half was lost, and an
exclusion for ``model-a:free`` missed the row while an exclusion for ``free``
hid it.

The fix matches the CONFIGURED custom-provider slug exactly, first, before
the generic known-provider scan.
"""

from __future__ import annotations

import pytest

from api import config


@pytest.fixture
def _configured_custom(monkeypatch):
    """Install a configured custom provider and return its slug."""

    def _install(name: str) -> str:
        entry = {"name": name, "base_url": "https://example.invalid/v1"}
        slug = config._custom_provider_slug_from_name(name)
        monkeypatch.setattr(
            config, "_custom_provider_entries", lambda config_obj=None: [entry]
        )
        return slug

    return _install


def test_a_configured_custom_slug_bounds_the_provider_prefix(_configured_custom):
    """``@custom:alpha:model-a:free`` → ``model-a:free`` (the model half)."""
    _configured_custom("alpha")
    got = config._strip_provider_prefix_from_model_id("@custom:alpha:model-a:free")
    assert got == "model-a:free", f"got {got!r}"


def test_the_agent_parse_shape_is_preserved_for_a_colon_model_id(
    _configured_custom,
):
    """The Agent parses ``@custom:alpha:model-a:free`` as alpha / model-a:free."""
    _configured_custom("alpha")
    got = config._strip_provider_prefix_from_model_id("@custom:alpha:model-a:free")
    assert got != "free", "the model half was lost to the colon boundary"
    assert got == "model-a:free", f"got {got!r}"


def test_an_exclusion_for_the_model_half_now_matches(_configured_custom):
    """Excluding ``model-a:free`` must hide the row (it did not before)."""
    _configured_custom("alpha")
    excludes = {"model-a:free"}
    assert config._is_model_id_excluded(
        "@custom:alpha:model-a:free", excludes
    ), "the exclusion for the model half did not match"


def test_a_builtin_provider_boundary_is_unaffected():
    """A plain builtin prefix still strips at its single colon."""
    got = config._strip_provider_prefix_from_model_id("@openrouter:gpt-5.6-luna")
    assert got == "gpt-5.6-luna", f"got {got!r}"


def test_a_model_id_with_a_colon_under_a_builtin_prefix_is_preserved():
    """``@opencode-zen:vendor/model:1`` keeps the colon inside the model id."""
    got = config._strip_provider_prefix_from_model_id("@opencode-zen:vendor/model:1")
    assert got == "vendor/model:1", f"got {got!r}"


def test_an_unconfigured_custom_prefix_still_keeps_the_model_half(monkeypatch):
    """With no configured custom providers the shared parser still applies.

    ``@custom:alpha:model-a:free`` must yield ``model-a:free`` in both the
    configured and the unconfigured case: the model half is the part that
    matters for an exclusion match, and the historical "strip the first
    segment" fallback lost it (``alpha:model-a:free`` never matched an
    exclusion for ``model-a:free``).
    """
    monkeypatch.setattr(config, "_custom_provider_entries", lambda config_obj=None: [])
    got = config._strip_provider_prefix_from_model_id("@custom:alpha:model-a:free")
    assert got == "model-a:free", f"got {got!r}"


def test_an_endpoint_style_custom_slug_keeps_its_port(monkeypatch):
    """``@custom:localhost:11434:Qwen3`` → ``Qwen3`` (the port is not the model)."""
    monkeypatch.setattr(config, "_custom_provider_entries", lambda config_obj=None: [])
    got = config._strip_provider_prefix_from_model_id("@custom:localhost:11434:Qwen3")
    assert got == "Qwen3", f"got {got!r}"


def test_an_ip_endpoint_style_custom_slug_keeps_its_port(monkeypatch):
    """``@custom:10.8.71.41:8080:Qwen3`` → ``Qwen3``."""
    monkeypatch.setattr(config, "_custom_provider_entries", lambda config_obj=None: [])
    got = config._strip_provider_prefix_from_model_id("@custom:10.8.71.41:8080:Qwen3")
    assert got == "Qwen3", f"got {got!r}"
