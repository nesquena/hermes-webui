"""Tests for custom provider CRUD operations (set/delete) via config.yaml.

Verifies that custom providers can be added, updated, and removed
through the set_custom_provider() and delete_custom_provider() functions,
and that changes persist to config.yaml and trigger reload_config().
"""

import json
import os
import sys
import types

import pytest

from tests._pytest_port import BASE


def _install_fake_hermes_cli(monkeypatch):
    """Stub hermes_cli so tests are deterministic and offline."""
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda pid: []

    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)

    try:
        from api.config import invalidate_models_cache
        invalidate_models_cache()
    except Exception:
        pass


# ───────────────────────────────
#  In-memory config helper
# ───────────────────────────────

class _ConfigFile:
    """Manage an isolated config.yaml at tmp_path."""

    def __init__(self, tmp_path):
        self.path = tmp_path / ".hermes" / "config.yaml"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write({})  # minimal valid config

    def _write(self, data):
        import yaml as _yaml
        self.path.write_text(
            _yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    def read(self):
        import yaml as _yaml
        return _yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}

    def set_custom_providers(self, entries):
        cfg = self.read()
        cfg["custom_providers"] = entries
        self._write(cfg)


def _stub_config_path(monkeypatch, tmp_path, profile_home):
    """Point _get_config_path to our isolated config.yaml."""
    import api.config as config
    cfg_path = profile_home / "config.yaml"

    monkeypatch.setattr(config, "_get_config_path", lambda: cfg_path)
    # Also stub cfg used by get_providers scan
    config.cfg.clear()
    config.cfg["model"] = {"provider": "anthropic"}
    config.cfg["custom_providers"] = []
    config._cfg_mtime = 0.0


# ───────────────────────────────
#  Tests
# ───────────────────────────────

class TestSetCustomProvider:
    """Tests for set_custom_provider()."""

    def test_create_new_provider(self, monkeypatch, tmp_path):
        """Adding a new custom provider creates entry in config.yaml."""
        _install_fake_hermes_cli(monkeypatch)
        profile_home = tmp_path / ".hermes"
        profile_home.mkdir(parents=True, exist_ok=True)
        cfg_file = profile_home / "config.yaml"
        cfg_file.write_text("model:\n  provider: anthropic\n", encoding="utf-8")

        import api.config as config
        monkeypatch.setattr(config, "_get_config_path", lambda: cfg_file)
        config.cfg.clear()
        config.cfg["model"] = {"provider": "anthropic"}
        config._cfg_mtime = 0.0

        from api.providers import set_custom_provider

        result = set_custom_provider(
            name="my-test-llm",
            base_url="https://test.example.com/v1",
            api_key="sk-test-123",
            api_mode="openai_compatible",
            models=["model-a", "model-b"],
        )

        assert result["ok"] is True, f"Expected ok, got: {result}"
        assert result["name"] == "my-test-llm"
        assert result["action"] == "created"

        # Verify it was written to config.yaml
        import yaml as _yaml
        cfg = _yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        custom_providers = cfg.get("custom_providers", [])
        assert len(custom_providers) == 1
        entry = custom_providers[0]
        assert entry["name"] == "my-test-llm"
        assert entry["base_url"] == "https://test.example.com/v1"
        assert entry["api_key"] == "sk-test-123"
        assert entry["api_mode"] == "openai_compatible"
        assert entry["models"] == ["model-a", "model-b"]

    def test_update_existing_provider(self, monkeypatch, tmp_path):
        """Updating a provider that exists should modify it in-place."""
        _install_fake_hermes_cli(monkeypatch)
        profile_home = tmp_path / ".hermes"
        profile_home.mkdir(parents=True, exist_ok=True)
        cfg_file = profile_home / "config.yaml"

        import yaml as _yaml
        initial_data = {
            "model": {"provider": "anthropic"},
            "custom_providers": [
                {
                    "name": "my-test-llm",
                    "base_url": "https://old-url.com/v1",
                    "api_key": "sk-old-999",
                    "api_mode": "openai_compatible",
                    "models": ["old-model"],
                },
            ],
        }
        cfg_file.write_text(
            _yaml.safe_dump(initial_data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

        import api.config as config
        monkeypatch.setattr(config, "_get_config_path", lambda: cfg_file)
        config.cfg.clear()
        config.cfg["model"] = {"provider": "anthropic"}
        config.cfg["custom_providers"] = list(initial_data["custom_providers"])
        config._cfg_mtime = 0.0

        from api.providers import set_custom_provider

        result = set_custom_provider(
            name="my-test-llm",
            base_url="https://new-url.com/v1",
            api_key="sk-new-888",
            api_mode="anthropic_messages",
            models=["new-model-x", "new-model-y"],
        )

        assert result["ok"] is True, f"Expected ok, got: {result}"
        assert result["name"] == "my-test-llm"
        assert result["action"] == "updated"

        # Verify config.yaml was updated
        cfg = _yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        custom_providers = cfg.get("custom_providers", [])
        assert len(custom_providers) == 1, (
            f"Should still have exactly 1 entry, got {len(custom_providers)}"
        )
        entry = custom_providers[0]
        assert entry["name"] == "my-test-llm"
        assert entry["base_url"] == "https://new-url.com/v1"
        assert entry["api_key"] == "sk-new-888"
        assert entry["api_mode"] == "anthropic_messages"
        assert entry["models"] == ["new-model-x", "new-model-y"]

    def test_update_skips_other_providers(self, monkeypatch, tmp_path):
        """Updating one custom provider must not affect others."""
        _install_fake_hermes_cli(monkeypatch)
        profile_home = tmp_path / ".hermes"
        profile_home.mkdir(parents=True, exist_ok=True)
        cfg_file = profile_home / "config.yaml"

        import yaml as _yaml
        initial_data = {
            "model": {"provider": "anthropic"},
            "custom_providers": [
                {
                    "name": "provider-a",
                    "base_url": "https://a.com/v1",
                    "api_key": "sk-a-111",
                },
                {
                    "name": "provider-b",
                    "base_url": "https://b.com/v1",
                    "api_key": "sk-b-222",
                    "models": ["model-b"],
                },
            ],
        }
        cfg_file.write_text(
            _yaml.safe_dump(initial_data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

        import api.config as config
        monkeypatch.setattr(config, "_get_config_path", lambda: cfg_file)
        config.cfg.clear()
        config.cfg["model"] = {"provider": "anthropic"}
        config.cfg["custom_providers"] = list(initial_data["custom_providers"])
        config._cfg_mtime = 0.0

        from api.providers import set_custom_provider

        result = set_custom_provider(
            name="provider-b",
            base_url="https://b-updated.com/v1",
            api_key="sk-b-333",
        )
        assert result["ok"] is True

        cfg = _yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        cps = cfg.get("custom_providers", [])

        # provider-a must be unchanged
        assert cps[0]["name"] == "provider-a"
        assert cps[0]["base_url"] == "https://a.com/v1"
        assert cps[0]["api_key"] == "sk-a-111"

        # provider-b must have the new values
        assert cps[1]["name"] == "provider-b"
        assert cps[1]["base_url"] == "https://b-updated.com/v1"
        assert cps[1]["api_key"] == "sk-b-333"

    def test_update_preserves_existing_api_key(self, monkeypatch, tmp_path):
        """Updating without providing api_key preserves the existing one."""
        _install_fake_hermes_cli(monkeypatch)
        profile_home = tmp_path / ".hermes"
        profile_home.mkdir(parents=True, exist_ok=True)
        cfg_file = profile_home / "config.yaml"

        import yaml as _yaml
        initial_data = {
            "model": {"provider": "openai"},
            "custom_providers": [
                {
                    "name": "deepseek",
                    "base_url": "https://api.deepseek.com",
                    "api_key": "sk-deepseek-original",
                    "api_mode": "openai_compatible",
                    "models": ["deepseek-v4-flash"],
                },
            ],
        }
        cfg_file.write_text(
            _yaml.safe_dump(initial_data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

        import api.config as config
        monkeypatch.setattr(config, "_get_config_path", lambda: cfg_file)
        config.cfg.clear()
        config.cfg["model"] = {"provider": "openai"}
        config.cfg["custom_providers"] = list(initial_data["custom_providers"])
        config._cfg_mtime = 0.0

        from api.providers import set_custom_provider

        # Update with empty api_key (simulating user not touching key field)
        result = set_custom_provider(
            name="deepseek",
            base_url="https://api.deepseek.com/v1",
            api_key="",  # empty — preserve the old key
            api_mode="openai_compatible",
        )
        assert result["ok"] is True
        assert result["action"] == "updated"

        cfg = _yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        entry = cfg["custom_providers"][0]
        assert entry["base_url"] == "https://api.deepseek.com/v1"
        assert entry["api_key"] == "sk-deepseek-original", (
            f"Expected existing api_key preserved, got {entry.get('api_key')!r}"
        )
        assert entry["api_mode"] == "openai_compatible"
        assert entry["models"] == ["deepseek-v4-flash"]

    def test_update_can_clear_models(self, monkeypatch, tmp_path):
        """Explicitly passing models=[] should remove existing models."""
        _install_fake_hermes_cli(monkeypatch)
        profile_home = tmp_path / ".hermes"
        profile_home.mkdir(parents=True, exist_ok=True)
        cfg_file = profile_home / "config.yaml"

        import yaml as _yaml
        initial_data = {
            "model": {"provider": "openai"},
            "custom_providers": [
                {
                    "name": "test-llm",
                    "base_url": "https://test.com/v1",
                    "api_key": "sk-test",
                    "models": ["model-a", "model-b"],
                },
            ],
        }
        cfg_file.write_text(
            _yaml.safe_dump(initial_data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

        import api.config as config
        monkeypatch.setattr(config, "_get_config_path", lambda: cfg_file)
        config.cfg.clear()
        config.cfg["model"] = {"provider": "openai"}
        config.cfg["custom_providers"] = list(initial_data["custom_providers"])
        config._cfg_mtime = 0.0

        from api.providers import set_custom_provider

        # Explicitly pass empty list to clear models
        result = set_custom_provider(
            name="test-llm",
            base_url="https://test.com/v1",
            api_key="sk-test",
            api_mode="openai_compatible",
            models=[],
        )
        assert result["ok"] is True
        assert result["action"] == "updated"

        cfg = _yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        entry = cfg["custom_providers"][0]
        assert "models" not in entry, (
            f"Expected models removed, got {entry.get('models')!r}"
        )

    def test_create_empty_name_fails(self, monkeypatch, tmp_path):
        """set_custom_provider with empty name should return error."""
        _install_fake_hermes_cli(monkeypatch)

        import api.config as config
        monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "config.yaml")
        config.cfg.clear()
        config._cfg_mtime = 0.0

        from api.providers import set_custom_provider

        result = set_custom_provider(name="", base_url="https://x.com/v1")
        assert result["ok"] is False
        assert "name is required" in result.get("error", "").lower()

    def test_create_empty_base_url_fails(self, monkeypatch, tmp_path):
        """set_custom_provider with empty base_url should return error."""
        _install_fake_hermes_cli(monkeypatch)

        import api.config as config
        monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "config.yaml")
        config.cfg.clear()
        config._cfg_mtime = 0.0

        from api.providers import set_custom_provider

        result = set_custom_provider(name="test", base_url="")
        assert result["ok"] is False
        assert "base url is required" in result.get("error", "").lower()

    def test_create_without_models_works(self, monkeypatch, tmp_path):
        """A custom provider without models should still be created."""
        _install_fake_hermes_cli(monkeypatch)
        profile_home = tmp_path / ".hermes"
        profile_home.mkdir(parents=True, exist_ok=True)
        cfg_file = profile_home / "config.yaml"
        cfg_file.write_text("model:\n  provider: anthropic\n", encoding="utf-8")

        import api.config as config
        monkeypatch.setattr(config, "_get_config_path", lambda: cfg_file)
        config.cfg.clear()
        config.cfg["model"] = {"provider": "anthropic"}
        config._cfg_mtime = 0.0

        from api.providers import set_custom_provider

        result = set_custom_provider(
            name="minimal",
            base_url="https://minimal.com/v1",
            api_key="sk-minimal",
        )
        assert result["ok"] is True
        assert result["action"] == "created"

        import yaml as _yaml
        cfg = _yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        cps = cfg.get("custom_providers", [])
        assert len(cps) == 1
        assert "models" not in cps[0] or cps[0].get("models") in (None, [])

    def test_create_without_api_key_works(self, monkeypatch, tmp_path):
        """A custom provider without API key (e.g. local ollama) should work."""
        _install_fake_hermes_cli(monkeypatch)
        profile_home = tmp_path / ".hermes"
        profile_home.mkdir(parents=True, exist_ok=True)
        cfg_file = profile_home / "config.yaml"
        cfg_file.write_text("model:\n  provider: anthropic\n", encoding="utf-8")

        import api.config as config
        monkeypatch.setattr(config, "_get_config_path", lambda: cfg_file)
        config.cfg.clear()
        config.cfg["model"] = {"provider": "anthropic"}
        config._cfg_mtime = 0.0

        from api.providers import set_custom_provider

        result = set_custom_provider(
            name="local-ollama",
            base_url="http://localhost:11434/v1",
            api_mode="openai_compatible",
            models=["llama3"],
        )
        assert result["ok"] is True

        import yaml as _yaml
        cfg = _yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        cps = cfg.get("custom_providers", [])
        assert len(cps) == 1
        assert "api_key" not in cps[0]  # no key field when empty


class TestDeleteCustomProvider:
    """Tests for delete_custom_provider()."""

    def test_delete_existing_provider(self, monkeypatch, tmp_path):
        """Deleting a custom provider removes it from config.yaml."""
        _install_fake_hermes_cli(monkeypatch)
        profile_home = tmp_path / ".hermes"
        profile_home.mkdir(parents=True, exist_ok=True)
        cfg_file = profile_home / "config.yaml"

        import yaml as _yaml
        initial_data = {
            "model": {"provider": "anthropic"},
            "custom_providers": [
                {"name": "provider-a", "base_url": "https://a.com/v1"},
                {"name": "provider-b", "base_url": "https://b.com/v1"},
                {"name": "provider-c", "base_url": "https://c.com/v1"},
            ],
        }
        cfg_file.write_text(
            _yaml.safe_dump(initial_data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

        import api.config as config
        monkeypatch.setattr(config, "_get_config_path", lambda: cfg_file)
        config.cfg.clear()
        config.cfg["model"] = {"provider": "anthropic"}
        config.cfg["custom_providers"] = list(initial_data["custom_providers"])
        config._cfg_mtime = 0.0

        from api.providers import delete_custom_provider

        result = delete_custom_provider("provider-b")
        assert result["ok"] is True
        assert result["name"] == "provider-b"
        assert result["action"] == "deleted"

        # Verify config.yaml
        cfg = _yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        cps = cfg.get("custom_providers", [])
        assert len(cps) == 2
        assert cps[0]["name"] == "provider-a"
        assert cps[1]["name"] == "provider-c"

    def test_delete_nonexistent_provider_fails(self, monkeypatch, tmp_path):
        """Deleting a provider that doesn't exist should return error."""
        _install_fake_hermes_cli(monkeypatch)
        profile_home = tmp_path / ".hermes"
        profile_home.mkdir(parents=True, exist_ok=True)
        cfg_file = profile_home / "config.yaml"
        cfg_file.write_text("model:\n  provider: anthropic\n", encoding="utf-8")

        import api.config as config
        monkeypatch.setattr(config, "_get_config_path", lambda: cfg_file)
        config.cfg.clear()
        config.cfg["model"] = {"provider": "anthropic"}
        config.cfg["custom_providers"] = []
        config._cfg_mtime = 0.0

        from api.providers import delete_custom_provider

        result = delete_custom_provider("nonexistent")
        assert result["ok"] is False
        assert "not found" in result.get("error", "").lower()

    def test_delete_empty_name_fails(self, monkeypatch, tmp_path):
        """delete_custom_provider with empty name should return error."""
        _install_fake_hermes_cli(monkeypatch)

        import api.config as config
        monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "config.yaml")
        config.cfg.clear()
        config._cfg_mtime = 0.0

        from api.providers import delete_custom_provider

        result = delete_custom_provider("")
        assert result["ok"] is False
        assert "name is required" in result.get("error", "").lower()

    def test_delete_only_removes_matching_by_name(self, monkeypatch, tmp_path):
        """Deleting by name should match the name field, not substring."""
        _install_fake_hermes_cli(monkeypatch)
        profile_home = tmp_path / ".hermes"
        profile_home.mkdir(parents=True, exist_ok=True)
        cfg_file = profile_home / "config.yaml"

        import yaml as _yaml
        initial_data = {
            "model": {"provider": "anthropic"},
            "custom_providers": [
                {"name": "provider", "base_url": "https://a.com/v1"},
                {"name": "provider-x", "base_url": "https://b.com/v1"},
            ],
        }
        cfg_file.write_text(
            _yaml.safe_dump(initial_data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

        import api.config as config
        monkeypatch.setattr(config, "_get_config_path", lambda: cfg_file)
        config.cfg.clear()
        config.cfg["model"] = {"provider": "anthropic"}
        config.cfg["custom_providers"] = list(initial_data["custom_providers"])
        config._cfg_mtime = 0.0

        from api.providers import delete_custom_provider

        result = delete_custom_provider("provider")
        assert result["ok"] is True

        cfg = _yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        cps = cfg.get("custom_providers", [])
        assert len(cps) == 1
        assert cps[0]["name"] == "provider-x"
