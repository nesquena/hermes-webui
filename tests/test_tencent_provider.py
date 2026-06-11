"""Tests for Tencent Cloud (TokenHub) provider registration.

Validates that the 'tencent' provider is correctly registered across all
configuration surfaces: display name, aliases, models, env var, onboarding,
and Nous vendor priority.

The Hermes agent registers this provider as ``tencent-tokenhub`` and reads
``TOKENHUB_API_KEY``; the WebUI exposes it under the catalog key ``tencent``
with the friendlier ``TENCENT_API_KEY`` primary name, mapping the two via
``_PROVIDER_ALIASES`` and ``_PROVIDER_ENV_VAR_ALIASES``.
"""

import api.config as config
from api.config import (
    _PROVIDER_ALIASES,
    _PROVIDER_DISPLAY,
    _PROVIDER_MODELS,
    _NOUS_VENDOR_PRIORITY,
)
from api.providers import _PROVIDER_ENV_VAR, _PROVIDER_ENV_VAR_ALIASES
from api.onboarding import _SUPPORTED_PROVIDER_SETUPS


class TestTencentProviderDisplay:
    def test_tencent_in_provider_display(self):
        assert _PROVIDER_DISPLAY["tencent"] == "Tencent Cloud"


class TestTencentProviderEnvVar:
    def test_tencent_env_var_mapping(self):
        assert _PROVIDER_ENV_VAR["tencent"] == "TENCENT_API_KEY"

    def test_tencent_tokenhub_alias_env_var(self):
        # The agent runtime reads TOKENHUB_API_KEY; WebUI must read it too.
        assert "TOKENHUB_API_KEY" in _PROVIDER_ENV_VAR_ALIASES["tencent"]


class TestTencentProviderAliases:
    def test_tencent_tokenhub_alias(self):
        # The agent's canonical slug must resolve to the WebUI catalog key.
        assert _PROVIDER_ALIASES["tencent-tokenhub"] == "tencent"

    def test_tencent_cloud_alias(self):
        assert _PROVIDER_ALIASES["tencent-cloud"] == "tencent"

    def test_tencentcloud_alias(self):
        assert _PROVIDER_ALIASES["tencentcloud"] == "tencent"

    def test_hunyuan_alias(self):
        assert _PROVIDER_ALIASES["hunyuan"] == "tencent"

    def test_tencent_hunyuan_alias(self):
        assert _PROVIDER_ALIASES["tencent-hunyuan"] == "tencent"

    def test_alias_resolution_via_function(self):
        # _resolve_provider_alias merges the agent's alias table on top of the
        # WebUI's local one when hermes_cli is importable. Either way, every
        # tencent variant must resolve to a tencent-prefixed slug.
        for alias in (
            "tencent-tokenhub",
            "tencent-cloud",
            "tencentcloud",
            "hunyuan",
            "tencent-hunyuan",
        ):
            resolved = config._resolve_provider_alias(alias)
            assert "tencent" in resolved, (
                f"Alias '{alias}' resolved to '{resolved}', expected a "
                f"tencent-prefixed slug"
            )


class TestTencentProviderModels:
    def test_tencent_models_exist(self):
        assert "tencent" in _PROVIDER_MODELS

    def test_tencent_models_count(self):
        assert len(_PROVIDER_MODELS["tencent"]) == 10

    def test_tencent_models_order(self):
        ids = [m["id"] for m in _PROVIDER_MODELS["tencent"]]
        expected = [
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "deepseek-v3.2",
            "glm-5",
            "glm-5-turbo",
            "glm-5.1",
            "kimi-k2.6",
            "kimi-k2.5",
            "minimax-m2.5",
            "minimax-m2.7",
        ]
        assert ids == expected

    def test_tencent_models_have_labels(self):
        for model in _PROVIDER_MODELS["tencent"]:
            assert "id" in model
            assert "label" in model
            assert model["label"].strip()


class TestTencentOnboarding:
    def test_tencent_in_onboarding(self):
        assert "tencent" in _SUPPORTED_PROVIDER_SETUPS

    def test_tencent_base_url(self):
        setup = _SUPPORTED_PROVIDER_SETUPS["tencent"]
        assert setup["default_base_url"] == "https://tokenhub.tencentmaas.com/v1"

    def test_tencent_default_model(self):
        setup = _SUPPORTED_PROVIDER_SETUPS["tencent"]
        assert setup["default_model"] == "deepseek-v4-flash"

    def test_tencent_env_var(self):
        setup = _SUPPORTED_PROVIDER_SETUPS["tencent"]
        assert setup["env_var"] == "TENCENT_API_KEY"

    def test_tencent_category(self):
        setup = _SUPPORTED_PROVIDER_SETUPS["tencent"]
        assert setup["category"] == "specialized"

    def test_tencent_models_populated(self):
        setup = _SUPPORTED_PROVIDER_SETUPS["tencent"]
        assert len(setup["models"]) == 10


class TestTencentNousVendorPriority:
    def test_tencent_in_nous_vendor_priority(self):
        assert "tencent" in _NOUS_VENDOR_PRIORITY
