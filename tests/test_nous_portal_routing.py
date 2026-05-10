"""Regression tests for Nous portal model routing bugs (issue #854).

Two bugs fixed:
1. Nous static model IDs were bare names (claude-opus-4.6) instead of
   slash-prefixed (anthropic/claude-opus-4.6), causing Nous to reject them.
2. resolve_model_provider() routed slash-prefixed cross-namespace models
   through OpenRouter instead of the configured portal provider.

Invariant: when a portal provider (Nous, OpenCode) is active, the full
slash-prefixed model ID MUST be preserved end-to-end — portals use the
provider/model path as the canonical name at their inference endpoint.
Stripping the prefix to a bare name is exactly Bug 1, so the fix for Bug 2
must not reintroduce it.
"""


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
    """Nous static model IDs must use @nous: prefix for explicit portal routing."""

    def test_nous_models_use_at_prefix(self):
        """All Nous static models must carry the @nous: explicit provider prefix.

        This ensures they route through the @provider:model branch of
        resolve_model_provider() — identical to the live-fetched path — rather
        than relying on the slash-only portal provider guard.
        """
        from api.config import _PROVIDER_MODELS

        nous_models = _PROVIDER_MODELS.get("nous", [])
        assert nous_models, "Nous must have at least one static model"
        for m in nous_models:
            mid = m["id"]
            assert mid.startswith("@nous:"), (
                f"Nous model '{mid}' must start with '@nous:' "
                f"(e.g. @nous:anthropic/claude-opus-4.6) so it routes through "
                f"the explicit provider hint branch, not the weaker portal guard."
            )

    def test_nous_known_models_present(self):
        """Key Nous models must be present with correct @nous:-prefixed IDs."""
        from api.config import _PROVIDER_MODELS

        nous_ids = {m["id"] for m in _PROVIDER_MODELS.get("nous", [])}
        assert "@nous:anthropic/claude-opus-4.6" in nous_ids, (
            "@nous:anthropic/claude-opus-4.6 must be in Nous model list"
        )
        assert "@nous:anthropic/claude-sonnet-4.6" in nous_ids, (
            "@nous:anthropic/claude-sonnet-4.6 must be in Nous model list"
        )
        assert "@nous:openai/gpt-5.4-mini" in nous_ids, (
            "@nous:openai/gpt-5.4-mini must be in Nous model list"
        )

    def test_nous_models_no_bare_or_slash_only(self):
        """No Nous static model should be bare or slash-only without @nous: prefix."""
        from api.config import _PROVIDER_MODELS

        bad_ids = {
            "claude-opus-4.6",
            "claude-sonnet-4.6",
            "gpt-5.4-mini",
            "gemini-3.1-pro-preview",
            "anthropic/claude-opus-4.6",
            "anthropic/claude-sonnet-4.6",
            "openai/gpt-5.4-mini",
            "google/gemini-3.1-pro-preview",
        }
        nous_ids = {m["id"] for m in _PROVIDER_MODELS.get("nous", [])}
        for bad in bad_ids:
            assert bad not in nous_ids, (
                f"Model ID '{bad}' found in Nous static list without @nous: prefix. "
                f"Use '@nous:{bad}' so routing matches the live-fetched path."
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
        """anthropic/claude-opus-4.6 with nous provider must route to nous with
        the full slash-prefixed ID preserved — Nous rejects bare names."""
        model, provider, _ = self._resolve("anthropic/claude-opus-4.6", "nous")
        assert provider == "nous", (
            f"Expected provider='nous', got '{provider}'. "
            f"Nous portal must handle cross-namespace models directly."
        )
        assert model == "anthropic/claude-opus-4.6", (
            f"Expected full slash-prefixed 'anthropic/claude-opus-4.6', got '{model}'. "
            f"Portals need the provider/model path to route upstream (Bug 1)."
        )

    def test_nous_routes_openai_model(self):
        """openai/gpt-5.4-mini with nous provider must route to nous with slash preserved."""
        model, provider, _ = self._resolve("openai/gpt-5.4-mini", "nous")
        assert provider == "nous", f"Expected provider='nous', got '{provider}'."
        assert model == "openai/gpt-5.4-mini", (
            f"Expected 'openai/gpt-5.4-mini', got '{model}' — portal must preserve namespace."
        )

    def test_nous_routes_google_model(self):
        """google/gemini-3.1-pro-preview with nous provider must route to nous with slash preserved."""
        model, provider, _ = self._resolve("google/gemini-3.1-pro-preview", "nous")
        assert provider == "nous", f"Expected provider='nous', got '{provider}'."
        assert model == "google/gemini-3.1-pro-preview", (
            f"Expected 'google/gemini-3.1-pro-preview', got '{model}'."
        )

    def test_opencode_zen_routes_cross_namespace(self):
        """opencode-zen is also a portal — cross-namespace models must route through it
        with the slash-prefixed ID preserved."""
        model, provider, _ = self._resolve(
            "anthropic/claude-sonnet-4.6", "opencode-zen"
        )
        assert provider == "opencode-zen", (
            f"Expected provider='opencode-zen', got '{provider}'."
        )
        assert model == "anthropic/claude-sonnet-4.6", (
            f"Expected 'anthropic/claude-sonnet-4.6', got '{model}'."
        )

    def test_portal_path_matches_at_prefix_path(self):
        """Static dropdown (bare slash) and live-fetched (@provider: prefix) paths
        must produce identical resolver output for the same model — otherwise
        Nous receives different forms depending on catalog source.
        """
        # Static dropdown form
        m1, p1, _ = self._resolve("anthropic/claude-opus-4.6", "nous")
        # Live-fetched form (after ui.js _fetchLiveModels prefixes with @nous:)
        m2, p2, _ = self._resolve("@nous:anthropic/claude-opus-4.6", "nous")
        assert (m1, p1) == (m2, p2), (
            f"Static path {m1, p1} and live path {m2, p2} must match — "
            f"both should send the same model ID to Nous."
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
