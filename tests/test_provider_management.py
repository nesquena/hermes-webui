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

    # Flush the 60-second TTL model cache so no prior test's result bleeds in.
    try:
        from api.config import invalidate_models_cache
        invalidate_models_cache()
    except Exception:
        pass


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

    def test_openai_codex_provider_card_prefers_live_catalog(self, monkeypatch, tmp_path):
        """OpenAI Codex provider cards should not advertise stale static fallback models.

        /api/models already uses hermes_cli/Codex cache discovery for Codex.  The
        provider card should share that source order so rejected stale entries
        such as gpt-5.5-mini are not presented as currently available when the
        live account catalog excludes them (#1807).
        """
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        fake_models = sys.modules["hermes_cli.models"]
        fake_models.provider_model_ids = lambda pid: (
            ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.2"]
            if pid == "openai-codex"
            else []
        )
        codex_home = tmp_path / "empty-codex-home"
        codex_home.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {"provider": "openai-codex", "default": "gpt-5.5"}
        config.cfg["providers"] = {}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import get_providers
        try:
            result = get_providers()
            codex = next(p for p in result["providers"] if p["id"] == "openai-codex")
            model_ids = [m["id"] for m in codex["models"]]
            assert model_ids == [
                "gpt-5.5",
                "gpt-5.4",
                "gpt-5.4-mini",
                "gpt-5.3-codex",
                "gpt-5.2",
            ]
            assert "gpt-5.5-mini" not in model_ids
            assert codex["models_total"] == len(model_ids)
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
        # Also pin HERMES_HOME so code that reads it directly gets tmp_path,
        # not the conftest session TEST_STATE_DIR that bleeds into the main process.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

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
            assert env_path.exists(), f".env not written to {env_path}; HERMES_HOME={__import__('os').environ.get('HERMES_HOME')!r}"
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

    def test_clean_provider_key_uses_late_bound_config_path(self, monkeypatch, tmp_path):
        """Config cleanup must honor api.config._get_config_path monkeypatches.

        PR #1597 fixed provider-key cleanup by resolving the config path through
        the api.config module at call time. If the implementation goes back to
        the function imported into api.providers at module load, this test cleans
        stale_config instead of active_config.
        """
        import yaml

        import api.config as cfg_mod
        import api.providers as providers

        stale_config = tmp_path / "stale-config.yaml"
        active_config = tmp_path / "active-config.yaml"
        stale_config.write_text(
            "providers:\n  openai:\n    api_key: stale-secret\n",
            encoding="utf-8",
        )
        active_config.write_text(
            "providers:\n  openai:\n    api_key: active-secret\nmodel:\n  provider: openai\n  api_key: active-model-secret\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(providers, "_get_config_path", lambda: stale_config, raising=False)
        monkeypatch.setattr(cfg_mod, "_get_config_path", lambda: active_config)
        monkeypatch.setattr(providers, "reload_config", lambda: None)

        providers._clean_provider_key_from_config("openai")

        stale = yaml.safe_load(stale_config.read_text(encoding="utf-8"))
        active = yaml.safe_load(active_config.read_text(encoding="utf-8"))
        assert stale["providers"]["openai"]["api_key"] == "stale-secret"
        assert "api_key" not in active["providers"]["openai"]
        assert active["model"] == {"provider": "openai"}

    def test_clean_custom_provider_key_matches_safe_name_slug(self, monkeypatch, tmp_path):
        """Custom-provider key removal must match the canonical safe name slug."""
        import yaml

        import api.config as cfg_mod
        import api.providers as providers

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.safe_dump({
                "custom_providers": [{
                    "name": "Local (127.0.0.1:15721)",
                    "base_url": "http://127.0.0.1:15721/v1",
                    "api_key": "${LOCAL_PORT_API_KEY}",
                    "model": "deepseek-v4-flash",
                }],
            }),
            encoding="utf-8",
        )

        monkeypatch.setattr(cfg_mod, "_get_config_path", lambda: config_path)
        monkeypatch.setattr(providers, "reload_config", lambda: None)

        providers._clean_provider_key_from_config("custom:local-127.0.0.1-15721")

        reloaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        custom_provider = reloaded["custom_providers"][0]
        assert custom_provider["name"] == "Local (127.0.0.1:15721)"
        assert "api_key" not in custom_provider

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


class TestIssue1410OllamaEnvVarBleed:
    """Regression: Ollama Cloud key must not flip local Ollama to has_key=True.

    Both providers used to share OLLAMA_API_KEY in _PROVIDER_ENV_VAR. After
    a user added a key for Ollama Cloud, the local Ollama card also lit up
    "API key configured" — incorrect because the runtime in
    hermes_cli/runtime_provider.py only consumes OLLAMA_API_KEY when the
    base URL hostname is ollama.com. Local Ollama is keyless by default.

    Fix: drop bare "ollama" from _PROVIDER_ENV_VAR so the env-var check is
    only applied to ollama-cloud. Local Ollama users who genuinely need a
    key can still set providers.ollama.api_key in config.yaml.
    """

    def test_ollama_local_not_configured_when_only_cloud_env_var_set(
        self, monkeypatch, tmp_path,
    ):
        """OLLAMA_API_KEY in env should mark ollama-cloud configured but not bare ollama."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("OLLAMA_API_KEY", "sk-cloud-key-xyz")

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
            by_id = {p["id"]: p for p in result["providers"]}
            assert "ollama-cloud" in by_id, "ollama-cloud should appear in provider list"
            assert "ollama" in by_id, "ollama (local) should appear in provider list"
            assert by_id["ollama-cloud"]["has_key"] is True, \
                "ollama-cloud should be has_key=True when OLLAMA_API_KEY is set"
            assert by_id["ollama"]["has_key"] is False, (
                "ollama (local) must NOT be has_key=True when only the cloud env "
                "var is set — local Ollama is keyless and shares no env var with "
                "Ollama Cloud (#1410)."
            )
            # ollama-cloud should be configurable, but local ollama should not
            # (it has no env var mapping — keys go through providers.ollama.api_key
            # in config.yaml if the user explicitly opts in).
            assert by_id["ollama-cloud"]["configurable"] is True
            assert by_id["ollama"]["configurable"] is False
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime

    def test_ollama_local_still_configured_via_config_yaml(
        self, monkeypatch, tmp_path,
    ):
        """providers.ollama.api_key in config.yaml should still mark local ollama configured."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        # Important: clear the env var so the only signal is config.yaml.
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

        old_cfg = dict(config.cfg)
        old_mtime = config._cfg_mtime
        config.cfg.clear()
        config.cfg["model"] = {}
        config.cfg["providers"] = {"ollama": {"api_key": "local-token-abc"}}
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except Exception:
            config._cfg_mtime = 0.0

        from api.providers import get_providers
        try:
            result = get_providers()
            by_id = {p["id"]: p for p in result["providers"]}
            assert by_id["ollama"]["has_key"] is True, (
                "Local Ollama users with providers.ollama.api_key in config.yaml "
                "should still report configured (#1410 fix must not regress this)."
            )
            # And ollama-cloud should NOT be configured by ollama's config entry.
            assert by_id["ollama-cloud"]["has_key"] is False
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
            config._cfg_mtime = old_mtime
