"""Tests for issue #4536 — main-model service_tier persistence and guarded forwarding."""


class TestIssue4536ServiceTier:
    def test_main_service_tier_roundtrip_via_auxiliary_endpoint(self, monkeypatch, tmp_path):
        """service_tier set on main model should persist in config and return via /api/model/auxiliary payload."""
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "model:\n  provider: openai\n  default: gpt-5.5\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)

        result = config.set_hermes_default_model(
            "gpt-5.5",
            advanced={"service_tier": "priority"},
        )
        assert result["ok"] is True
        assert "service_tier: priority" in config_path.read_text(encoding="utf-8")

        payload = config.get_auxiliary_models()
        assert payload["main"]["service_tier"] == "priority"

    def test_main_service_tier_default_clears_persisted_value(self, monkeypatch, tmp_path):
        """Choosing Default/off should clear service_tier from persisted main-model options."""
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "model:\n  provider: openai\n  default: gpt-5.5\n  service_tier: priority\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)

        result = config.set_hermes_default_model(
            "gpt-5.5",
            advanced={"service_tier": "default"},
        )
        assert result["ok"] is True
        payload = config.get_auxiliary_models()
        assert payload["main"]["service_tier"] == ""
        assert "service_tier" not in config_path.read_text(encoding="utf-8")

    def test_main_request_overrides_only_for_openai_family(self, monkeypatch):
        """service_tier forwarding should only happen for OpenAI-family providers."""
        from api import config

        openai_payload = config._main_model_request_overrides(
            {"model": {"provider": "openai", "default": "gpt-5.5", "service_tier": "priority"}},
        )
        assert openai_payload == {"service_tier": "priority"}

        openrouter_payload = config._main_model_request_overrides(
            {"model": {"provider": "openrouter", "default": "meta-llama/llama-3.1", "service_tier": "priority"}},
        )
        assert openrouter_payload == {}

        def resolve_alias(model: str, *_args):
            return model, "openai", ""

        monkeypatch.setattr(config, "resolve_model_provider", resolve_alias)

        openai_alias_payload = config._main_model_request_overrides(
            {"model": {"default": "gpt-5.5", "service_tier": "priority"}},
        )
        assert openai_alias_payload == {"service_tier": "priority"}

        stale_openai_payload = config._main_model_request_overrides(
            {"model": {"provider": "openai", "default": "meta-llama/llama-3.1", "service_tier": "priority"}},
        )
        assert stale_openai_payload == {}

    def test_auxiliary_payload_hides_service_tier_for_non_openai_main_models(self, monkeypatch, tmp_path):
        """Saved service_tier should not be re-exposed once the main model switches away from OpenAI."""
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "model:\n  provider: openrouter\n  default: meta-llama/llama-3.1\n  service_tier: priority\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)

        payload = config.get_auxiliary_models()

        assert payload["main"]["service_tier"] == ""

    def test_switching_main_model_away_from_openai_clears_service_tier(self, monkeypatch, tmp_path):
        """A non-OpenAI default-model save should remove stale OpenAI service-tier state."""
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "model:\n  provider: openai\n  default: gpt-5.5\n  service_tier: priority\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)
        monkeypatch.setattr(
            config,
            "cfg",
            {"model": {"provider": "openai", "default": "gpt-5.5"}},
        )

        result = config.set_hermes_default_model("meta-llama/llama-3.1")

        assert result["ok"] is True
        text = config_path.read_text(encoding="utf-8")
        assert "service_tier" not in text
