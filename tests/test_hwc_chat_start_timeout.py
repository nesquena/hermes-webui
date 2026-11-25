"""hwc: slash ids on an openrouter profile skip the catalog.

send() also waits 120s. A cold catalog can outlast the default client timeout.
Every other profile keeps master's catalog path: a custom profile needs the
catalog to publish its endpoint's advertised ids (also when a custom_providers
entry named "openrouter" takes over the bare provider), and an omitted or blank
profile needs it for the active-provider repair.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


class _CatalogConsulted(Exception):
    pass


def _resolve_with_catalog_sentinel(
    model: str, profile_provider: str, profile_config=None
):
    from api import routes

    def _sentinel(*_args, **_kwargs):
        raise _CatalogConsulted()

    with patch.object(routes, "get_available_models", _sentinel):
        return routes._resolve_compatible_session_model_state(
            model,
            None,
            profile_provider=profile_provider,
            profile_config={} if profile_config is None else profile_config,
        )


def _resolve_without_catalog(model: str, profile_provider: str):
    from api import routes

    def _boom(*_args, **_kwargs):
        raise AssertionError("live catalog must not run")

    with patch.object(routes, "get_available_models", _boom):
        return routes._resolve_compatible_session_model_state(
            model,
            None,
            profile_provider=profile_provider,
            profile_config={"model": {"provider": profile_provider}},
        )


def test_openrouter_slash_id_skips_catalog():
    effective, provider, changed = _resolve_without_catalog(
        "openai/gpt-6-luna",
        "openrouter",
    )
    assert effective == "openai/gpt-6-luna"
    assert provider == "openrouter"
    assert changed is False


def test_custom_slash_id_still_consults_catalog():
    # The catalog publishes the custom endpoint's advertised ids. Skipping it
    # made the worker strip an advertised "openai/..." id to its bare name.
    with pytest.raises(_CatalogConsulted):
        _resolve_with_catalog_sentinel("openai/gpt-6-luna", "custom")


def test_named_custom_slash_id_still_consults_catalog():
    with pytest.raises(_CatalogConsulted):
        _resolve_with_catalog_sentinel("openai/gpt-6-luna", "custom:my-proxy")


def test_blank_profile_slash_id_still_consults_catalog():
    # The profile reader turns a blank provider into None; a literal "" keeps
    # master's catalog path and its active-provider repair.
    with pytest.raises(_CatalogConsulted):
        _resolve_with_catalog_sentinel("x-ai/grok-4.7", "")


def test_openrouter_taken_over_by_custom_entry_still_consults_catalog():
    # A custom_providers entry named "openrouter" makes the worker route the
    # bare provider as that custom endpoint, which needs the advertised ids.
    config = {
        "model": {"provider": "openrouter"},
        "custom_providers": [
            {"name": "openrouter", "base_url": "https://proxy.example.invalid/v1"}
        ],
    }
    with pytest.raises(_CatalogConsulted):
        _resolve_with_catalog_sentinel("openai/gpt-6-luna", "openrouter", config)


def test_qualified_slash_id_on_openrouter_still_consults_catalog():
    with pytest.raises(_CatalogConsulted):
        _resolve_with_catalog_sentinel("@anthropic:anthropic/claude-x", "openrouter")


def test_chat_start_post_waits_120s():
    text = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    send_at = text.find("async function send(")
    assert send_at >= 0
    regen_at = text.find("async function startRegeneration(", send_at)
    assert regen_at > send_at
    send_body = text[send_at:regen_at]
    assert "timeoutMs:120000" in send_body
