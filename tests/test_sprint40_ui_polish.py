"""
Tests for issue #433 — stale provider-prefixed model IDs sent to custom endpoints.

When a user configures a custom endpoint (config_base_url set) and previously
had sessions with provider-prefixed model IDs (e.g. openai/gpt-5.4), the
resolve_model_provider() function must strip the provider prefix so the bare
model ID (gpt-5.4) is sent to the custom endpoint.
"""
import unittest
from unittest.mock import patch
import api.config as config


class TestCustomEndpointModelStripping:
    """Tests for fix #433: strip provider prefix when custom base_url is set."""

    def _resolve(self, model_id, provider=None, base_url=None):
        """Helper: set cfg directly (same pattern as test_model_resolver.py)."""
        old_cfg = dict(config.cfg)
        model_cfg = {}
        if provider:
            model_cfg['provider'] = provider
        if base_url:
            model_cfg['base_url'] = base_url
        config.cfg['model'] = model_cfg
        try:
            return config.resolve_model_provider(model_id)
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)

    def test_prefixed_model_stripped_for_custom_endpoint(self):
        """Issue #433: 'openai/gpt-5.4' with custom base_url returns bare 'gpt-5.4'."""
        model, provider, base_url = self._resolve(
            'openai/gpt-5.4',
            provider='custom',
            base_url='http://my-proxy.local:8080/v1',
        )
        assert model == 'gpt-5.4', (
            "Expected bare 'gpt-5.4' for custom endpoint, got '{}'."
            " Stale provider-prefix must be stripped.".format(model)
        )
        assert base_url == 'http://my-proxy.local:8080/v1'
        assert provider == 'custom'

    def test_bare_model_unchanged_for_custom_endpoint(self):
        """Bare model ID (no slash) must pass through untouched with custom base_url."""
        model, provider, base_url = self._resolve(
            'gpt-4o',
            provider='custom',
            base_url='http://my-proxy.local:8080/v1',
        )
        assert model == 'gpt-4o', (
            "Bare model 'gpt-4o' should not be modified, got '{}'.".format(model)
        )
        assert base_url == 'http://my-proxy.local:8080/v1'
        assert provider == 'custom'

    def test_prefixed_model_kept_for_openrouter(self):
        """When NO custom base_url (openrouter route), prefixed model must stay prefixed."""
        model, provider, base_url = self._resolve(
            'openai/gpt-5.4',
            provider='anthropic',  # cross-provider pick triggers openrouter routing
        )
        # Cross-provider model with openrouter routing must keep full provider/model path
        assert 'openai/gpt-5.4' in model or provider == 'openrouter', (
            "Expected prefixed model or openrouter routing for non-custom endpoint, "
            "got model='{}', provider='{}'.".format(model, provider)
        )
        assert base_url is None, (
            "OpenRouter routing must not set a base_url, got '{}'.".format(base_url)
        )
