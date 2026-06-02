"""openai-api → openai provider alias.

hermes-agent reports its built-in OpenAI provider as ``openai-api``
(the slug in ``hermes_cli.auth.PROVIDER_REGISTRY``).  Without an alias,
GPT models from that provider slug are invisible in the WebUI model
picker because the slug doesn't match the ``openai`` group key in
``_PROVIDER_MODELS``.

Same shape as #815 (``z.ai`` → ``zai``) and #1384 (``local`` →
``custom``).
"""

import api.config as cfg


class TestOpenaiApiProviderAlias:
    """``_resolve_provider_alias`` must rewrite ``openai-api`` → ``openai``."""

    def test_openai_api_resolves_to_openai(self):
        assert cfg._resolve_provider_alias("openai-api") == "openai"

    def test_openai_api_case_insensitive(self):
        assert cfg._resolve_provider_alias("OPENAI-API") == "openai"
        assert cfg._resolve_provider_alias("OpenAI-API") == "openai"

    def test_alias_table_contains_entry(self):
        assert cfg._PROVIDER_ALIASES.get("openai-api") == "openai", (
            "_PROVIDER_ALIASES must map 'openai-api' → 'openai'"
        )

    def test_openai_canonical_unchanged(self):
        """The canonical slug 'openai' must pass through unchanged."""
        assert cfg._resolve_provider_alias("openai") == "openai"
