"""Regression tests for Nous portal model routing bugs (issue #854).

Two bugs fixed:
1. Nous static model IDs were bare names (claude-opus-4.6) instead of
   slash-prefixed (anthropic/claude-opus-4.6), causing Nous to reject them.
2. resolve_model_provider() routed slash-prefixed cross-namespace models
   through OpenRouter instead of the configured portal provider.
"""
import sys
import types


def _models_with_provider(provider, monkeypatch):
    """Patch config.cfg to simulate an active provider, return resolve_model_provider."""
    import api.config as config

    old = dict(config.cfg)
    config.cfg.clear()
    config.cfg["model"] = {"provider": provider}
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    try:
        from api.config import resolve_model_provider
        return resolve_model_provider
    finally:
        config.cfg.clear()
        config.cfg.update(old)


class TestNousModelIds:
    """Nous static model IDs must be slash-prefixed for Nous API compatibility."""

    def test_nous_models_use_slash_prefixed_ids(self):
        """All Nous static models must carry a provider/model slash prefix."""
        from api.config import _PROVIDER_MODELS
        nous_models = _PROVIDER_MODELS.get("nous", [])
        assert nous_models, "Nous must have at least one static model"
        for m in nous_models:
            mid = m["id"]
            assert "/" in mid, (
                f"Nous model '{mid}' must be in provider/model format "
                f"(e.g. anthropic/claude-opus-4.6) so Nous routes it correctly. "
                f"Bare names cause Nous to reject the request."
            )

    def test_nous_known_models_present(self):
        """Key Nous models must be present with correct slash-prefixed IDs."""
        from api.config import _PROVIDER_MODELS
        nous_ids = {m["id"] for m in _PROVIDER_MODELS.get("nous", [])}
        assert "anthropic/claude-opus-4.6" in nous_ids, (
            "anthropic/claude-opus-4.6 must be in Nous model list"
        )
        assert "anthropic/claude-sonnet-4.6" in nous_ids, (
            "anthropic/claude-sonnet-4.6 must be in Nous model list"
        )
        assert "openai/gpt-5.4-mini" in nous_ids, (
            "openai/gpt-5.4-mini must be in Nous model list"
        )

    def test_nous_models_no_bare_names(self):
        """No Nous model should use a bare name without a slash prefix."""
        from api.config import _PROVIDER_MODELS
        bare_names = {"claude-opus-4.6", "claude-sonnet-4.6", "gpt-5.4-mini",
                      "gemini-3.1-pro-preview"}
        nous_ids = {m["id"] for m in _PROVIDER_MODELS.get("nous", [])}
        for bare in bare_names:
            assert bare not in nous_ids, (
                f"Bare model ID '{bare}' found in Nous model list. "
                f"Must be slash-prefixed (e.g. anthropic/{bare})."
            )


class TestPortalProviderRouting:
    """Portal providers (Nous, OpenCode) must route cross-namespace models
    through themselves, not through OpenRouter."""

    def _resolve(self, model_id, provider):
        import api.config as config
        old = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {"provider": provider}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0
        try:
            from api.config import resolve_model_provider
            return resolve_model_provider(model_id)
        finally:
            config.cfg.clear()
            config.cfg.update(old)
            config._cfg_mtime = old_mtime

    def test_nous_routes_anthropic_model(self):
        """anthropic/claude-opus-4.6 with nous provider must route to nous, not openrouter."""
        model, provider, _ = self._resolve("anthropic/claude-opus-4.6", "nous")
        assert provider == "nous", (
            f"Expected provider='nous', got '{provider}'. "
            f"Nous portal must handle cross-namespace models directly."
        )
        assert model == "claude-opus-4.6", (
            f"Expected bare model 'claude-opus-4.6', got '{model}'."
        )

    def test_nous_routes_openai_model(self):
        """openai/gpt-5.4-mini with nous provider must route to nous, not openrouter."""
        model, provider, _ = self._resolve("openai/gpt-5.4-mini", "nous")
        assert provider == "nous", (
            f"Expected provider='nous', got '{provider}'."
        )

    def test_nous_routes_google_model(self):
        """google/gemini-3.1-pro-preview with nous provider must route to nous."""
        model, provider, _ = self._resolve("google/gemini-3.1-pro-preview", "nous")
        assert provider == "nous", (
            f"Expected provider='nous', got '{provider}'."
        )

    def test_opencode_zen_routes_cross_namespace(self):
        """opencode-zen is also a portal — cross-namespace models must route through it."""
        model, provider, _ = self._resolve("anthropic/claude-sonnet-4.6", "opencode-zen")
        assert provider == "opencode-zen", (
            f"Expected provider='opencode-zen', got '{provider}'."
        )

    def test_non_portal_still_routes_to_openrouter(self):
        """Non-portal providers (anthropic) must still route cross-namespace to OpenRouter."""
        model, provider, _ = self._resolve("openai/gpt-5.4-mini", "anthropic")
        assert provider == "openrouter", (
            f"Expected provider='openrouter' for cross-namespace with anthropic config, "
            f"got '{provider}'."
        )

    def test_openrouter_config_keeps_full_path(self):
        """OpenRouter config must always keep the full provider/model path."""
        model, provider, _ = self._resolve("anthropic/claude-sonnet-4.6", "openrouter")
        assert provider == "openrouter"
        assert model == "anthropic/claude-sonnet-4.6"
