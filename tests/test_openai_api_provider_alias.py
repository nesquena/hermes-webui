"""openai-api as a first-class picker provider.

hermes-agent registers its built-in OpenAI provider as ``openai-api`` in
``hermes_cli.auth.PROVIDER_REGISTRY``.  The WebUI must recognise this
slug in ``_PROVIDER_DISPLAY`` and ``_PROVIDER_MODELS`` so that GPT
models appear in the picker AND the session ``model_provider`` stays
``openai-api`` on the send path (where the agent resolves it against
its own registry).

An alias-only approach (``openai-api`` → ``openai``) fixes display but
breaks the send path because ``openai`` is not a registered provider in
the agent.  See PR review discussion on #3444.
"""

import api.config as cfg


class TestOpenaiApiPickerProvider:
    """openai-api must be a first-class picker provider, not an alias."""

    def test_provider_display_registered(self):
        assert "openai-api" in cfg._PROVIDER_DISPLAY

    def test_provider_models_registered(self):
        assert "openai-api" in cfg._PROVIDER_MODELS
        assert len(cfg._PROVIDER_MODELS["openai-api"]) > 0

    def test_not_aliased(self):
        assert "openai-api" not in cfg._PROVIDER_ALIASES

    def test_canonicalise_preserves_slug(self):
        assert cfg._canonicalise_provider_id("openai-api") == "openai-api"

    def test_openai_canonical_unchanged(self):
        assert cfg._canonicalise_provider_id("openai") == "openai"

    def test_openai_codex_unchanged(self):
        assert cfg._canonicalise_provider_id("openai-codex") == "openai-codex"


class TestOpenaiApiSendPath:
    """The send path must preserve openai-api, not collapse it to openai."""

    def test_resolve_model_provider_preserves_openai_api(self, monkeypatch):
        monkeypatch.setattr(cfg, "cfg", {
            "model": {"provider": "openai-api", "default": "gpt-5.5"},
        })
        _model, provider, _base_url = cfg.resolve_model_provider("gpt-5.5")
        assert provider == "openai-api"

    def test_model_with_provider_context_preserves_openai_api(self, monkeypatch):
        monkeypatch.setattr(cfg, "cfg", {
            "model": {"provider": "openai-api", "default": "gpt-5.5"},
        })
        result = cfg.model_with_provider_context("gpt-5.5", "openai-api")
        assert "openai" not in result or "openai-api" in result


class TestOpenaiApiEnvDetection:
    """OPENAI_API_KEY env detection must surface `openai-api`, not a bare `openai`
    the agent registry can't resolve (#3443 detection-side, Codex follow-up)."""

    def test_openai_api_key_detects_openai_api_not_bare_openai(self):
        import inspect
        src = inspect.getsource(cfg)
        # The env-detection branch for OPENAI_API_KEY must add "openai-api".
        assert 'detected_providers.add("openai-api")' in src
        # And must NOT add a bare "openai" (no such slug in the agent registry).
        assert 'detected_providers.add("openai")\n' not in src
