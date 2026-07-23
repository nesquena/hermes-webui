"""Tests for issue #4536 — main-model service_tier persistence and guarded forwarding."""

import sys

import pytest


class TestIssue4536ServiceTier:
    @pytest.fixture(autouse=True)
    def _isolate_config_globals(self):
        """Make these tests hermetic against leaked module-global config caches.

        Two shared caches in api.config can serve a PRIOR test's config into this
        one, causing a full-suite-only flake (passes in isolation):

        1. `_yaml_file_cache` — memoized YAML parse keyed on
           (str(config_path), st_mtime_ns, st_size). pytest reuses tmp_path base
           dirs across the session, so a prior test's config.yaml can share both
           the path string AND the (mtime_ns, size) of this test's tiny file
           (same coarse mtime tick) → set_hermes_default_model() reads the STALE
           cached dict (e.g. an lmstudio config) and writes THAT back, so the
           on-disk assertion sees the wrong provider. This is the actual root
           cause of the #4536 service_tier flake.
        2. `cfg` / `_cfg_fingerprint` — if a prior test rebound `config.cfg`,
           get_auxiliary_models() reads the stale dict after reload_config().

        Clear both before AND after each test so neither an inherited leak nor
        our own run can poison a neighbor.
        """
        from api import config

        def _reset():
            with config._cfg_lock:
                config._cfg_cache.clear()
                config.cfg = config._cfg_cache
                config._cfg_fingerprint = None
                config._cfg_mtime = 0.0
            with config._yaml_file_cache_lock:
                config._yaml_file_cache.clear()

        _reset()
        yield
        _reset()

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
        assert payload["main"]["supports_fast_tier"] is True

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

        codex_payload = config._main_model_request_overrides(
            {"model": {"provider": "openai-codex", "default": "gpt-5.5", "service_tier": "priority"}},
        )
        assert codex_payload == {"service_tier": "priority"}

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

        codex_nonfast_payload = config._main_model_request_overrides(
            {"model": {"provider": "openai-codex", "default": "gpt-5.3-codex", "service_tier": "priority"}},
        )
        assert codex_nonfast_payload == {}

        codex_empty_model_payload = config._main_model_request_overrides(
            {"model": {"provider": "openai-codex", "service_tier": "priority"}},
        )
        assert codex_empty_model_payload == {}

        stale_openai_payload = config._main_model_request_overrides(
            {"model": {"provider": "openai", "default": "meta-llama/llama-3.1", "service_tier": "priority"}},
        )
        assert stale_openai_payload == {}

        foreign_prefixed_openai_payload = config._main_model_request_overrides(
            {"model": {"provider": "openai", "default": "openrouter/gpt-5.5", "service_tier": "priority"}},
        )
        assert foreign_prefixed_openai_payload == {}

        codex_unknown_model_payload = config._main_model_request_overrides(
            {"model": {"provider": "openai-codex", "default": "gpt-7.0-codex", "service_tier": "priority"}},
        )
        assert codex_unknown_model_payload == {}

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

    def test_effective_model_gates_service_tier_forwarding(self):
        """service_tier must be gated on the effective send model, not the saved default."""
        from api import config

        cfg_priority = {"model": {"provider": "openai", "default": "gpt-5.5", "service_tier": "priority"}}

        openai_effective = config._main_model_request_overrides(
            cfg_priority, effective_model="gpt-5.5", effective_provider="openai",
        )
        assert openai_effective.get("service_tier") == "priority"

        openrouter_effective = config._main_model_request_overrides(
            cfg_priority, effective_model="meta-llama/llama-3.1", effective_provider="openrouter",
        )
        assert openrouter_effective == {}

        custom_effective = config._main_model_request_overrides(
            cfg_priority, effective_model="my-local-model", effective_provider="custom:local",
        )
        assert custom_effective == {}

        codex_effective = config._main_model_request_overrides(
            cfg_priority, effective_model="gpt-5.3-codex", effective_provider="openai-codex",
        )
        assert codex_effective == {}

    def test_no_effective_params_falls_back_to_saved_default(self):
        """Without effective_model/provider, the function still reads the saved config."""
        from api import config

        result = config._main_model_request_overrides(
            {"model": {"provider": "openai", "default": "gpt-5.5", "service_tier": "priority"}},
        )
        assert result == {"service_tier": "priority"}

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

    def test_standalone_agent_import_failure_preserves_supported_service_tier(self, monkeypatch, tmp_path):
        """If hermes_cli is unavailable, Settings saves should not delete supported service_tier state."""
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "model:\n  provider: openai-codex\n  default: gpt-5.5\n  service_tier: priority\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        monkeypatch.setattr(config, "invalidate_models_cache", lambda: None)
        monkeypatch.setattr(
            config,
            "cfg",
            {"model": {"provider": "openai-codex", "default": "gpt-5.5", "service_tier": "priority"}},
        )
        monkeypatch.setitem(sys.modules, "hermes_cli.models", None)

        assert config._main_model_request_overrides(
            {"model": {"provider": "openai-codex", "default": "gpt-5.5", "service_tier": "priority"}},
        ) == {"service_tier": "priority"}

        result = config.set_hermes_default_model("gpt-5.5", provider="openai-codex")

        assert result["ok"] is True
        text = config_path.read_text(encoding="utf-8")
        assert "service_tier: priority" in text

    def test_standalone_agent_import_failure_still_blocks_codex_slug(self, monkeypatch):
        """The compatibility fallback must not broadly enable Codex-specific model slugs."""
        from api import config

        monkeypatch.setitem(sys.modules, "hermes_cli.models", None)

        assert config._main_model_request_overrides(
            {"model": {"provider": "openai-codex", "default": "gpt-5.3-codex", "service_tier": "priority"}},
        ) == {}

    def test_auxiliary_payload_marks_false_fast_tier_for_unsupported_main_models(self, monkeypatch, tmp_path):
        """The frontend needs explicit false metadata, not an absent field that falls back stale."""
        from api import config

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "model:\n  provider: openai-codex\n  default: gpt-5.3-codex\n  service_tier: priority\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)

        payload = config.get_auxiliary_models()

        assert payload["main"]["service_tier"] == ""
        assert payload["main"]["supports_fast_tier"] is False
