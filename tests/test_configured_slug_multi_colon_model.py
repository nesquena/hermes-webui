"""Regression tests: a *configured* custom-provider slug with a multi-colon model ID.

The #7182 shared-grammar tests cover the un-configured fallback (host:port,
peel-one-segment). But when the ``custom:<slug>`` prefix matches an actual
``custom_providers[]`` entry, the model ID after it may itself contain colons
(``syn:small:text``, ``qwen3.8:27b-mtp-q8_0``, ``model-a:free``). A positional
split cannot recover that boundary — the first two segments are the provider and
everything after them is the model. Splitting on the KNOWN slug (and only a
configured one) is the only unambiguous grammar.

Reported in #7182 (Ollama name:tag) and reproduced end-to-end with a
``synthetic`` custom provider whose model IDs are ``syn:<tier>:<modality>``:
``@custom:synthetic:syn:small:text`` resolved to model ``small:text`` with
provider ``custom:synthetic:syn`` (unknown provider → HTTP 400 upstream).
"""

import pytest

from api import config
from api.config import _parse_provider_qualified_model_id


SYNTHETIC_PROVIDERS = [
    {
        "name": "synthetic",
        "base_url": "https://api.synthetic.new/anthropic",
        "models": [
            "syn:large:text",
            "syn:small:text",
            "syn:large:vision",
            "syn:small:vision",
        ],
        "discover_models": False,
    },
]


@pytest.fixture()
def synthetic_provider_cfg():
    """Register a named custom provider whose model IDs contain colons."""
    old_cfg = dict(config.cfg)
    config.cfg["custom_providers"] = SYNTHETIC_PROVIDERS
    try:
        yield
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)


class TestConfiguredSlugMultiColonModel:
    """``@custom:<configured-slug>:<model-with-colons>`` splits on the slug."""

    @pytest.mark.parametrize(
        "value,expected_model,expected_provider",
        [
            # The reported failure: tiered synthetic model IDs.
            ("@custom:synthetic:syn:small:text", "syn:small:text", "custom:synthetic"),
            ("@custom:synthetic:syn:large:vision", "syn:large:vision", "custom:synthetic"),
            # Ollama-style tags behind a configured named provider.
            ("@custom:ollama-local:qwen3.8:27b-mtp-q8_0", "qwen3.8:27b-mtp-q8_0", "custom:ollama-local"),
            # :free suffix + configured slug still keeps the full model ID.
            ("@custom:proxy:model-a:free", "model-a:free", "custom:proxy"),
            # Single-colon model through a configured slug (no change in behavior).
            ("@custom:proxy:plain-model", "plain-model", "custom:proxy"),
        ],
    )
    def test_configured_slug_wins_over_positional_split(
        self, synthetic_provider_cfg, value, expected_model, expected_provider
    ):
        assert _parse_provider_qualified_model_id(value) == (
            expected_model,
            expected_provider,
        )

    def test_full_resolution_roundtrip(self, synthetic_provider_cfg, monkeypatch):
        """Encoded → parsed → resolved keeps the model intact; the credential
        layer (resolve_custom_provider_connection) supplies the endpoint, as in
        production (streaming backfills base_url from the matched entry)."""
        monkeypatch.setattr(
            config, "get_config", lambda: {"custom_providers": SYNTHETIC_PROVIDERS}
        )
        encoded = config.model_with_provider_context(
            "syn:small:text", "custom:synthetic"
        )
        model, provider, _base_url = config.resolve_model_provider(encoded)
        assert model == "syn:small:text"
        assert provider == "custom:synthetic"
        api_key, base_url = config.resolve_custom_provider_connection(provider)
        assert base_url == "https://api.synthetic.new/anthropic"

    def test_unconfigured_slug_keeps_hostport_peel(self):
        """Without a configured entry, host:port-style IDs keep the #1776 peel."""
        # No custom_providers registered in this test.
        assert _parse_provider_qualified_model_id(
            "@custom:192.168.1.5:11434:llama4"
        ) == ("llama4", "custom:192.168.1.5:11434")

    def test_unknown_slug_peel_unchanged(self):
        """An unknown multi-segment slug still peels one segment (upstream rule)."""
        assert _parse_provider_qualified_model_id(
            "@custom:not-a-provider:model-a:free"
        ) == ("model-a:free", "custom:not-a-provider")


class TestDiscoverOptOutKeepsCuratedList:
    """``discover_models: false`` must pin the picker to the configured list.

    A pre-warmed endpoint-advertised catalog (#7409) previously bypassed the
    curated ``models:`` list for custom providers, letting endpoint-advertised
    IDs shadow the user's hand-curated catalog.
    """

    def test_provider_discover_allowed_false(self, synthetic_provider_cfg):
        entry = config._custom_provider_entries()[0]
        assert config._provider_discover_allowed(entry) is False

    def test_provider_discover_defaults_true(self):
        assert config._provider_discover_allowed({"name": "x"}) is True
        assert config._provider_discover_allowed(None) is True

    @pytest.mark.parametrize("flag", ["false", "no", "0", "FALSE"])
    def test_string_false_forms(self, flag):
        assert config._provider_discover_allowed({"discover_models": flag}) is False
