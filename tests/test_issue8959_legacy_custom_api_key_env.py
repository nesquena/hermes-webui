"""#8959: a legacy ``custom_providers`` entry that declares its key with the
``api_key_env`` spelling must resolve that key — for the chat, for the
context-length lookup that follows the chat (#8951), and for the presentation
layer — exactly as the Agent's own resolver does.

``hermes_cli/runtime_provider_custom.py`` ``_key_env_secret`` reads
``entry.get("key_env") or entry.get("api_key_env")`` for ANY block, legacy
entries included. WebUI's ladder for a legacy entry read ``key_env`` only, so
the second spelling was dropped, the record looked keyless, and the chat sent
the ``dummy-key`` placeholder to an endpoint that answers 401/403.

Precedence pinned here (same as ``_normalized_raw_provider_record`` applies to
``providers.<id>`` records, and same as the Agent's resolver): literal
``api_key`` (a ``${VAR}`` reference first) > ``key_env`` > ``api_key_env`` >
the ``CUSTOM_<SLUG>_API_KEY`` convention. When one entry declares BOTH env
spellings, ``key_env`` wins: that is the Agent's order, the normalisation's
order, and it keeps WebUI and the Agent resolving the SAME key for the same
config.
"""
import pytest

import api.config as config
import api.providers as providers
from api import routes

URL = "https://p.legacy-url.test/v1"
KEY = "legacy-spelled-key"
ENV_VAR = "P_LEGACY_API_KEY_VAR"


def _bind_legacy_entry(monkeypatch, **fields):
    """Bind a config whose single legacy ``custom_providers`` entry is ``p``."""
    entry = {"name": "p", "base_url": URL}
    entry.update(fields)
    cfg = {"custom_providers": [entry]}
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    # api/providers.py holds its own module-level reference to get_config.
    monkeypatch.setattr(providers, "get_config", lambda: cfg)
    # The CUSTOM_<SLUG>_API_KEY convention fallback must stay out of the way so
    # each test pins the spelling under test, not an ambient process key.
    monkeypatch.delenv("CUSTOM_P_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM:P_API_KEY", raising=False)
    return entry


# ── the bug: ``api_key_env`` on a legacy entry ───────────────────────────────


def test_legacy_entry_api_key_env_resolves_the_configured_key(monkeypatch):
    _bind_legacy_entry(monkeypatch, api_key_env=ENV_VAR)
    monkeypatch.setenv(ENV_VAR, KEY)

    api_key, base_url = config.resolve_custom_provider_connection("custom:p")

    assert api_key == KEY, "api_key_env spelling was dropped; the chat would send the keyless placeholder"
    assert base_url == URL


def test_legacy_entry_api_key_env_reaches_the_agent_bundle(monkeypatch):
    _bind_legacy_entry(monkeypatch, api_key_env=ENV_VAR)
    monkeypatch.setenv(ENV_VAR, KEY)

    bundle = config.resolve_custom_provider_bundle("custom:p", lookup_only=True)

    assert bundle["api_key"] == KEY
    assert bundle["keyless"] is False


def test_legacy_entry_api_key_env_reaches_the_context_length_lookup(monkeypatch):
    # The lookup follows the chat (#8951): for a named custom provider it takes
    # the URL and key from the chat's own routing chain
    # (_session_context_length_lookup_state -> the merge's lookup_only bundle),
    # so the api_key_env spelling the chat now resolves reaches it too.
    _bind_legacy_entry(monkeypatch, api_key_env=ENV_VAR)
    monkeypatch.setenv(ENV_VAR, KEY)

    _model, provider, base_url, api_key = routes._session_context_length_lookup_state(
        "m-x", "custom:p",
    )

    assert provider == "custom:p"
    assert base_url == URL
    assert api_key == KEY


def test_legacy_entry_api_key_env_declared_but_unset_is_not_keyless(monkeypatch):
    # A declared-but-unresolved credential is a misconfiguration, not a claim
    # that the endpoint wants no auth: the keyless placeholder must NOT be
    # substituted for it (that turns the real cause into an opaque 401).
    _bind_legacy_entry(monkeypatch, api_key_env=ENV_VAR)
    monkeypatch.delenv(ENV_VAR, raising=False)

    bundle = config.resolve_custom_provider_bundle("custom:p")

    assert bundle["api_key"] is None
    assert bundle["keyless"] is False


def test_chat_merge_sends_the_api_key_env_key_not_the_placeholder(monkeypatch):
    _bind_legacy_entry(monkeypatch, api_key_env=ENV_VAR)
    monkeypatch.setenv(ENV_VAR, KEY)

    bundle = routes._resolve_agent_connection_bundle(
        "custom:p", None, None, {"provider": "custom"}, lookup_provider="custom:p",
    )

    assert bundle["api_key"] == KEY
    assert bundle["base_url"] == URL


def test_chat_merge_refuses_the_placeholder_when_api_key_env_is_unset(monkeypatch):
    _bind_legacy_entry(monkeypatch, api_key_env=ENV_VAR)
    monkeypatch.delenv(ENV_VAR, raising=False)

    with pytest.raises(config.CustomProviderRouteError):
        routes._resolve_agent_connection_bundle(
            "custom:p", None, None, {"provider": "custom"}, lookup_provider="custom:p",
        )


def test_presentation_layer_sees_the_api_key_env_key(monkeypatch):
    _bind_legacy_entry(monkeypatch, api_key_env=ENV_VAR)
    monkeypatch.setenv(ENV_VAR, KEY)

    assert providers._get_provider_api_key("custom:p") == KEY
    assert providers._provider_has_key("custom:p") is True


# ── no regressions: the spellings that already worked ────────────────────────


def test_legacy_entry_key_env_still_resolves_the_configured_key(monkeypatch):
    _bind_legacy_entry(monkeypatch, key_env=ENV_VAR)
    monkeypatch.setenv(ENV_VAR, KEY)

    api_key, base_url = config.resolve_custom_provider_connection("custom:p")

    assert api_key == KEY
    assert base_url == URL


def test_legacy_entry_literal_api_key_still_resolves(monkeypatch):
    _bind_legacy_entry(monkeypatch, api_key="literal-key")

    api_key, base_url = config.resolve_custom_provider_connection("custom:p")

    assert api_key == "literal-key"
    assert base_url == URL


def test_legacy_entry_dollar_env_reference_still_resolves(monkeypatch):
    _bind_legacy_entry(monkeypatch, api_key=f"${{{ENV_VAR}}}")
    monkeypatch.setenv(ENV_VAR, KEY)

    api_key, _base_url = config.resolve_custom_provider_connection("custom:p")

    assert api_key == KEY


def test_legacy_entry_key_env_wins_over_api_key_env_when_both_declared(monkeypatch):
    # key_env first: the Agent's order (entry.get("key_env") or
    # entry.get("api_key_env")), the normalisation's order, and it keeps WebUI
    # and the Agent resolving the same key for the same config.
    other_var = "P_LEGACY_API_KEY_OTHER_VAR"
    _bind_legacy_entry(monkeypatch, key_env=ENV_VAR, api_key_env=other_var)
    monkeypatch.setenv(ENV_VAR, KEY)
    monkeypatch.setenv(other_var, "other-key")

    api_key, _base_url = config.resolve_custom_provider_connection("custom:p")

    assert api_key == KEY


def test_legacy_entry_literal_api_key_wins_over_both_env_spellings(monkeypatch):
    _bind_legacy_entry(monkeypatch, api_key="literal-key", key_env=ENV_VAR, api_key_env="P_OTHER")
    monkeypatch.setenv(ENV_VAR, "env-key")
    monkeypatch.setenv("P_OTHER", "other-env-key")

    api_key, _base_url = config.resolve_custom_provider_connection("custom:p")

    assert api_key == "literal-key"


def test_legacy_entry_without_any_key_declaration_stays_keyless(monkeypatch):
    _bind_legacy_entry(monkeypatch)

    bundle = config.resolve_custom_provider_bundle("custom:p")

    assert bundle["api_key"] is None
    assert bundle["keyless"] is True


def test_chat_merge_keeps_the_placeholder_for_a_genuinely_keyless_entry(monkeypatch):
    _bind_legacy_entry(monkeypatch)

    bundle = routes._resolve_agent_connection_bundle(
        "custom:p", None, None, {"provider": "custom"}, lookup_provider="custom:p",
    )

    assert bundle["api_key"] == config.KEYLESS_CUSTOM_API_KEY
    assert bundle["base_url"] == URL


def test_providers_record_api_key_env_normalization_still_resolves(monkeypatch):
    # ``providers.<id>`` records keep their api_key_env -> key_env respelling in
    # _normalized_raw_provider_record; this fix must not disturb that path.
    monkeypatch.setattr(
        config,
        "get_config",
        lambda: {
            "providers": {
                "custom:p": {
                    "base_url": URL,
                    "api_key_env": ENV_VAR,
                },
            },
        },
    )
    monkeypatch.setenv(ENV_VAR, KEY)

    api_key, base_url = config.resolve_custom_provider_connection("custom:p")

    assert api_key == KEY
    assert base_url == URL
