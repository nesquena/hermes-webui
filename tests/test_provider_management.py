"""Tests for /api/providers CRUD endpoints (provider key management).

Closes #586 — allow provider key update from the WebUI.
Part of #604 — multi-provider model picker support.
"""

import json
import sys
import types
import urllib.error
import urllib.request

import api.config as config
import api.profiles as profiles
from tests._pytest_port import BASE


# ── HTTP helpers ──────────────────────────────────────────────────────────


def _get(path):
    """GET helper — returns parsed JSON."""
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


def _post(path, body=None):
    """POST helper — returns (parsed_json, status_code)."""
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body_text), e.code
        except Exception:
            return {"error": body_text}, e.code


def _install_fake_hermes_cli(monkeypatch):
    """Stub hermes_cli modules so tests are deterministic and offline."""
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


# ── Unit tests (api/providers.py functions directly) ──────────────────────


class TestGetProviders:
    """Unit tests for get_providers() function."""

    def test_returns_list_of_known_providers(self, monkeypatch, tmp_path):
        """GET /api/providers should return a list of all known providers."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import get_providers
        try:
            result = get_providers()
            assert "providers" in result
            assert "active_provider" in result
            assert isinstance(result["providers"], list)
            # Should include at least the built-in providers
            provider_ids = {p["id"] for p in result["providers"]}
            assert "anthropic" in provider_ids
            assert "openai" in provider_ids
            assert "openrouter" in provider_ids
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime

    def test_provider_entries_have_required_fields(self, monkeypatch, tmp_path):
        """Each provider entry should have id, display_name, has_key, configurable."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import get_providers
        try:
            result = get_providers()
            for p in result["providers"]:
                assert "id" in p, f"Missing 'id' in provider entry"
                assert "display_name" in p, f"Missing 'display_name' for {p['id']}"
                assert "has_key" in p, f"Missing 'has_key' for {p['id']}"
                assert "configurable" in p, f"Missing 'configurable' for {p['id']}"
                assert "key_source" in p, f"Missing 'key_source' for {p['id']}"
                assert isinstance(p["has_key"], bool)
                assert isinstance(p["configurable"], bool)
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime

    def test_oauth_providers_not_configurable(self, monkeypatch, tmp_path):
        """OAuth providers (copilot, nous, openai-codex) should not be configurable."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import get_providers
        try:
            result = get_providers()
            for p in result["providers"]:
                if p["id"] in ("copilot", "nous", "openai-codex"):
                    assert p["configurable"] is False, f"{p['id']} should not be configurable"
                # ollama-cloud is now configurable (uses OLLAMA_API_KEY)
                if p["id"] == "ollama-cloud":
                    assert p["configurable"] is True, "ollama-cloud should be configurable"
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime


class TestSetProviderKey:
    """Unit tests for set_provider_key() function."""

    def test_set_key_writes_to_env_file(self, monkeypatch, tmp_path):
        """Setting a key should write the env var to ~/.hermes/.env."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import set_provider_key
        try:
            result = set_provider_key("anthropic", "sk-ant-test-key-12345678")
            assert result["ok"] is True
            assert result["provider"] == "anthropic"
            assert result["action"] == "updated"

            # Verify .env file was written
            env_path = tmp_path / ".env"
            assert env_path.exists()
            content = env_path.read_text()
            assert "ANTHROPIC_API_KEY=sk-ant-test-key-12345678" in content
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime

    def test_remove_key_deletes_from_env_file(self, monkeypatch, tmp_path):
        """Removing a key should delete the env var from .env."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import set_provider_key
        try:
            # First set a key
            set_provider_key("anthropic", "sk-ant-test-key-12345678")
            # Then remove it
            result = set_provider_key("anthropic", None)
            assert result["ok"] is True
            assert result["action"] == "removed"

            # Verify .env file no longer has the key
            env_path = tmp_path / ".env"
            content = env_path.read_text() if env_path.exists() else ""
            assert "ANTHROPIC_API_KEY" not in content
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime

    def test_oauth_provider_rejected(self, monkeypatch, tmp_path):
        """Setting a key for an OAuth provider should fail."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import set_provider_key
        try:
            result = set_provider_key("copilot", "some-key")
            assert result["ok"] is False
            assert "OAuth" in result["error"]
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime

    def test_short_key_rejected(self, monkeypatch, tmp_path):
        """API keys shorter than 8 chars should be rejected."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import set_provider_key
        try:
            result = set_provider_key("anthropic", "short")
            assert result["ok"] is False
            assert "too short" in result["error"]
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime

    def test_empty_provider_id_rejected(self, monkeypatch, tmp_path):
        """Empty provider ID should be rejected."""
        from api.providers import set_provider_key
        result = set_provider_key("", "some-key")
        assert result["ok"] is False
        assert "required" in result["error"]

    def test_newline_in_key_rejected(self, monkeypatch, tmp_path):
        """API keys with newlines should be rejected."""
        from api.providers import set_provider_key
        result = set_provider_key("anthropic", "sk-ant-key\nINJECTED=evil")
        assert result["ok"] is False
        assert "newline" in result["error"]


class TestRemoveProviderKey:
    """Unit tests for remove_provider_key() wrapper."""

    def test_remove_provider_key_calls_set_with_none(self, monkeypatch, tmp_path):
        """remove_provider_key should delegate to set_provider_key(id, None)."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import remove_provider_key
        try:
            result = remove_provider_key("anthropic")
            assert result["ok"] is True
            assert result["action"] == "removed"
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime


# ── Integration tests (via HTTP endpoints) ───────────────────────────────


class TestProvidersEndpoints:
    """Integration tests for /api/providers HTTP endpoints."""

    def test_get_providers_returns_200(self):
        """GET /api/providers should return 200 with provider list."""
        result = _get("/api/providers")
        assert "providers" in result
        assert isinstance(result["providers"], list)

    def test_post_provider_set_key(self):
        """POST /api/providers with provider + api_key should set the key."""
        body, status = _post("/api/providers", {
            "provider": "anthropic",
            "api_key": "sk-ant-integration-test-key-12345678",
        })
        assert status == 200
        assert body.get("ok") is True
        assert body.get("provider") == "anthropic"

    def test_post_provider_remove_key(self):
        """POST /api/providers with provider but no api_key should remove the key."""
        body, status = _post("/api/providers", {
            "provider": "anthropic",
            "api_key": None,
        })
        assert status == 200
        assert body.get("ok") is True
        assert body.get("action") == "removed"

    def test_post_provider_delete(self):
        """POST /api/providers/delete should remove the key."""
        body, status = _post("/api/providers/delete", {
            "provider": "anthropic",
        })
        assert status == 200
        assert body.get("ok") is True

    def test_post_provider_missing_id(self):
        """POST /api/providers without provider should return 400."""
        body, status = _post("/api/providers", {"api_key": "some-key"})
        assert status == 400
        assert "required" in body.get("error", "").lower()

    def test_post_provider_delete_missing_id(self):
        """POST /api/providers/delete without provider should return 400."""
        body, status = _post("/api/providers/delete", {})
        assert status == 400
