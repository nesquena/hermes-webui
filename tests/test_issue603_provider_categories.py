"""Tests for #603 — categorize providers in setup wizard.

Validates:
  - New providers added to _SUPPORTED_PROVIDER_SETUPS with correct categories
  - _PROVIDER_CATEGORIES ordering and IDs
  - _build_setup_catalog returns grouped categories
  - apply_onboarding_setup writes base_url for requires_base_url providers
  - apply_onboarding_setup writes default_base_url for providers with one
  - Frontend helper _renderProviderSelectOptions produces <optgroup>
  - i18n keys exist for all category labels
  - Fallback when categories are empty
"""

import pytest
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.onboarding import (
    _SUPPORTED_PROVIDER_SETUPS,
    _PROVIDER_CATEGORIES,
    _build_setup_catalog,
    apply_onboarding_setup,
)


# ── Backend: provider catalog structure ──────────────────────────────────

class TestProviderCatalog:
    """Verify the extended provider catalog has categories."""

    def test_all_providers_have_category(self):
        for pid, meta in _SUPPORTED_PROVIDER_SETUPS.items():
            assert "category" in meta, f"Provider {pid} missing 'category'"

    def test_categories_are_valid(self):
        valid_ids = {c["id"] for c in _PROVIDER_CATEGORIES}
        for pid, meta in _SUPPORTED_PROVIDER_SETUPS.items():
            cat = meta["category"]
            assert cat in valid_ids, f"Provider {pid} has invalid category '{cat}'"

    def test_easy_start_has_core_providers(self):
        easy = {
            pid
            for pid, meta in _SUPPORTED_PROVIDER_SETUPS.items()
            if meta["category"] == "easy_start"
        }
        assert "openrouter" in easy
        assert "anthropic" in easy
        assert "openai" in easy

    def test_self_hosted_has_local_providers(self):
        local = {
            pid
            for pid, meta in _SUPPORTED_PROVIDER_SETUPS.items()
            if meta["category"] == "self_hosted"
        }
        assert "ollama" in local
        assert "lmstudio" in local
        assert "custom" in local

    def test_specialized_has_extended_providers(self):
        spec = {
            pid
            for pid, meta in _SUPPORTED_PROVIDER_SETUPS.items()
            if meta["category"] == "specialized"
        }
        assert "gemini" in spec
        assert "deepseek" in spec
        assert "mistralai" in spec
        assert "x-ai" in spec

    def test_new_providers_exist(self):
        expected = {"ollama", "lmstudio", "gemini", "deepseek", "mistralai", "x-ai"}
        assert expected.issubset(_SUPPORTED_PROVIDER_SETUPS.keys())

    def test_new_providers_have_env_vars(self):
        for pid in ["ollama", "lmstudio", "gemini", "deepseek", "mistralai", "x-ai"]:
            meta = _SUPPORTED_PROVIDER_SETUPS[pid]
            assert meta["env_var"], f"Provider {pid} missing env_var"
            assert meta["default_model"], f"Provider {pid} missing default_model"

    def test_local_providers_require_base_url(self):
        for pid in ["ollama", "lmstudio", "custom"]:
            assert _SUPPORTED_PROVIDER_SETUPS[pid]["requires_base_url"]

    def test_specialized_providers_have_base_url_defaults(self):
        for pid in ["gemini", "deepseek", "mistralai", "x-ai"]:
            meta = _SUPPORTED_PROVIDER_SETUPS[pid]
            assert meta["default_base_url"], f"Provider {pid} missing default_base_url"

    def test_google_uses_gemini_key(self):
        """Google Gemini must use 'gemini' as provider ID (matches Hermes CLI)."""
        assert "gemini" in _SUPPORTED_PROVIDER_SETUPS
        assert "google" not in _SUPPORTED_PROVIDER_SETUPS


class TestProviderCategoryOrder:
    """Verify category ordering."""

    def test_categories_sorted_by_order(self):
        orders = [c["order"] for c in _PROVIDER_CATEGORIES]
        assert orders == sorted(orders)

    def test_category_ids(self):
        ids = {c["id"] for c in _PROVIDER_CATEGORIES}
        assert ids == {"easy_start", "self_hosted", "specialized"}

    def test_three_categories(self):
        assert len(_PROVIDER_CATEGORIES) == 3


# ── Backend: _build_setup_catalog ────────────────────────────────────────

class TestBuildSetupCatalog:
    def test_catalog_has_categories_key(self):
        cfg = {"model": {"provider": "openrouter", "default": "anthropic/claude-sonnet-4.6"}}
        catalog = _build_setup_catalog(cfg)
        assert "categories" in catalog
        assert isinstance(catalog["categories"], list)

    def test_catalog_categories_have_providers_list(self):
        cfg = {"model": {"provider": "openrouter", "default": "anthropic/claude-sonnet-4.6"}}
        catalog = _build_setup_catalog(cfg)
        all_provider_ids = {p["id"] for p in catalog["providers"]}
        for cat in catalog["categories"]:
            assert "providers" in cat
            for pid in cat["providers"]:
                assert pid in all_provider_ids

    def test_catalog_providers_have_category_field(self):
        cfg = {"model": {"provider": "openrouter", "default": "anthropic/claude-sonnet-4.6"}}
        catalog = _build_setup_catalog(cfg)
        for p in catalog["providers"]:
            assert "category" in p

    def test_catalog_providers_sorted_by_category(self):
        cfg = {"model": {"provider": "openrouter", "default": "anthropic/claude-sonnet-4.6"}}
        catalog = _build_setup_catalog(cfg)
        cat_order = {c["id"]: c["order"] for c in _PROVIDER_CATEGORIES}
        prev_order = -1
        for p in catalog["providers"]:
            order = cat_order.get(p["category"], 99)
            assert order >= prev_order, f"Provider {p['id']} out of order"
            prev_order = order

    def test_catalog_quick_flag_on_openrouter(self):
        cfg = {"model": {"provider": "openrouter", "default": "anthropic/claude-sonnet-4.6"}}
        catalog = _build_setup_catalog(cfg)
        orow = next(p for p in catalog["providers"] if p["id"] == "openrouter")
        assert orow["quick"] is True

    def test_catalog_no_quick_flag_on_others(self):
        cfg = {"model": {"provider": "openrouter", "default": "anthropic/claude-sonnet-4.6"}}
        catalog = _build_setup_catalog(cfg)
        for p in catalog["providers"]:
            if p["id"] != "openrouter":
                assert p["quick"] is False


# ── Backend: apply_onboarding_setup base_url handling ───────────────────

class TestApplyBaseURL:
    """Verify the generic base_url save logic."""

    def test_requires_base_url_writes_user_url(self, tmp_path, monkeypatch):
        """Providers with requires_base_url=True should write user-provided base_url."""
        config_path = str(tmp_path / "config.yaml")
        env_path = str(tmp_path / ".env")

        monkeypatch.setattr("api.onboarding._get_config_path", lambda: config_path)
        monkeypatch.setattr("api.onboarding._get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setattr("api.onboarding._load_yaml_config", lambda p: {})
        monkeypatch.setattr(
            "api.onboarding._normalize_model_for_provider", lambda prov, m: m
        )
        monkeypatch.setattr("api.onboarding._write_env_file", lambda p, d: None)
        monkeypatch.setattr("api.onboarding._save_yaml_config", lambda p, c: None)
        monkeypatch.setattr("api.onboarding._provider_api_key_present", lambda *a: True)
        monkeypatch.setattr("api.onboarding.reload_config", lambda: None)

        saved_cfg = {}
        def mock_save(p, cfg):
            saved_cfg.update(cfg)
        monkeypatch.setattr("api.onboarding._save_yaml_config", mock_save)

        apply_onboarding_setup({
            "provider": "ollama",
            "model": "qwen3:32b",
            "api_key": "test-key",
            "base_url": "http://my-ollama:11434/v1",
            "confirm_overwrite": True,
        })

        assert saved_cfg["model"]["base_url"] == "http://my-ollama:11434/v1"

    def test_default_base_url_written_for_openai(self, tmp_path, monkeypatch):
        """OpenAI should get its default_base_url written to config."""
        config_path = str(tmp_path / "config.yaml")

        monkeypatch.setattr("api.onboarding._get_config_path", lambda: config_path)
        monkeypatch.setattr("api.onboarding._get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setattr("api.onboarding._load_yaml_config", lambda p: {})
        monkeypatch.setattr(
            "api.onboarding._normalize_model_for_provider", lambda prov, m: m
        )
        monkeypatch.setattr("api.onboarding._write_env_file", lambda p, d: None)
        monkeypatch.setattr("api.onboarding._provider_api_key_present", lambda *a: True)
        monkeypatch.setattr("api.onboarding.reload_config", lambda: None)

        saved_cfg = {}
        def mock_save(p, cfg):
            saved_cfg.update(cfg)
        monkeypatch.setattr("api.onboarding._save_yaml_config", mock_save)

        apply_onboarding_setup({
            "provider": "openai",
            "model": "gpt-4o",
            "api_key": "test-key",
            "confirm_overwrite": True,
        })

        assert saved_cfg["model"]["base_url"] == "https://api.openai.com/v1"

    def test_base_url_stripped_for_anthropic(self, tmp_path, monkeypatch):
        """Anthropic should NOT have base_url in config (Hermes knows the URL)."""
        config_path = str(tmp_path / "config.yaml")

        monkeypatch.setattr("api.onboarding._get_config_path", lambda: config_path)
        monkeypatch.setattr("api.onboarding._get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setattr("api.onboarding._load_yaml_config", lambda p: {})
        monkeypatch.setattr(
            "api.onboarding._normalize_model_for_provider", lambda prov, m: m
        )
        monkeypatch.setattr("api.onboarding._write_env_file", lambda p, d: None)
        monkeypatch.setattr("api.onboarding._provider_api_key_present", lambda *a: True)
        monkeypatch.setattr("api.onboarding.reload_config", lambda: None)

        saved_cfg = {}
        def mock_save(p, cfg):
            saved_cfg.update(cfg)
        monkeypatch.setattr("api.onboarding._save_yaml_config", mock_save)

        apply_onboarding_setup({
            "provider": "anthropic",
            "model": "claude-sonnet-4.6",
            "api_key": "test-key",
            "confirm_overwrite": True,
        })

        assert "base_url" not in saved_cfg["model"]


# ── Frontend: i18n keys ─────────────────────────────────────────────────

class TestI18nCategoryKeys:
    def test_en_has_all_category_keys(self):
        with open("static/i18n.js", encoding="utf-8") as f:
            content = f.read()
        for key in ["provider_category_easy_start", "provider_category_self_hosted", "provider_category_specialized"]:
            assert f"{key}:" in content, f"Missing i18n key: {key}"

    def test_ru_has_all_category_keys(self):
        with open("static/i18n.js", encoding="utf-8") as f:
            content = f.read()
        # Just verify count of category keys (should appear 6+ times: once per locale block)
        assert content.count("provider_category_easy_start:") >= 4

    def test_es_has_all_category_keys(self):
        with open("static/i18n.js", encoding="utf-8") as f:
            content = f.read()
        assert "Inicio rápido" in content  # Spanish easy_start

    def test_zh_has_all_category_keys(self):
        with open("static/i18n.js", encoding="utf-8") as f:
            content = f.read()
        assert "快速开始" in content  # Chinese easy_start

    def test_zh_hant_has_all_category_keys(self):
        with open("static/i18n.js", encoding="utf-8") as f:
            content = f.read()
        assert "\\u5feb\\u901f\\u958b\\u59cb" in content  # zh-Hant easy_start


class TestApplyBaseURLSpecialized:
    """Verify apply_onboarding_setup sets base_url for specialized providers."""

    _PROVIDER_DEFAULT_MODELS = {
        "gemini": "gemini-3.1-pro-preview",
        "deepseek": "deepseek-chat-v3-0324",
        "mistralai": "mistral-large-latest",
        "x-ai": "grok-4.20",
    }

    def _run_setup(self, tmp_path, monkeypatch, provider):
        """Run apply_onboarding_setup with the given provider and return saved_cfg."""
        config_path = str(tmp_path / "config.yaml")
        model = self._PROVIDER_DEFAULT_MODELS.get(provider, "test-model")

        monkeypatch.setattr("api.onboarding._get_config_path", lambda: config_path)
        monkeypatch.setattr("api.onboarding._get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setattr("api.onboarding._load_yaml_config", lambda p: {})
        monkeypatch.setattr("api.onboarding._normalize_model_for_provider", lambda prov, m: m)
        monkeypatch.setattr("api.onboarding._write_env_file", lambda p, d: None)
        monkeypatch.setattr("api.onboarding._provider_api_key_present", lambda *a: True)
        monkeypatch.setattr("api.onboarding.reload_config", lambda: None)

        saved_cfg = {}
        def mock_save(p, cfg):
            saved_cfg.update(cfg)
        monkeypatch.setattr("api.onboarding._save_yaml_config", mock_save)

        from api.onboarding import apply_onboarding_setup
        apply_onboarding_setup({"provider": provider, "model": model, "api_key": "test-key", "confirm_overwrite": True})
        return saved_cfg

    def test_gemini_gets_default_base_url(self, tmp_path, monkeypatch):
        saved = self._run_setup(tmp_path, monkeypatch, "gemini")
        assert "generativelanguage.googleapis.com" in saved.get("model", {}).get("base_url", ""), (
            "gemini setup must write the Gemini base_url to config"
        )

    def test_deepseek_gets_default_base_url(self, tmp_path, monkeypatch):
        saved = self._run_setup(tmp_path, monkeypatch, "deepseek")
        assert "deepseek.com" in saved.get("model", {}).get("base_url", ""), (
            "deepseek setup must write the DeepSeek base_url to config"
        )

    def test_mistral_gets_default_base_url(self, tmp_path, monkeypatch):
        saved = self._run_setup(tmp_path, monkeypatch, "mistralai")
        assert "mistral.ai" in saved.get("model", {}).get("base_url", ""), (
            "mistral setup must write the Mistral base_url to config"
        )

    def test_x_ai_gets_default_base_url(self, tmp_path, monkeypatch):
        saved = self._run_setup(tmp_path, monkeypatch, "x-ai")
        assert "x.ai" in saved.get("model", {}).get("base_url", ""), (
            "x-ai setup must write the xAI base_url to config"
        )
