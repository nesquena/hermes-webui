"""Tests for /api/providers CRUD endpoints (provider key management).

Closes #586 — allow provider key update from the WebUI.
Part of #604 — multi-provider model picker support.
"""

import json
import multiprocessing
import os
import sys
import time
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


def _increment_under_profile_transaction(home_text, start_event):
    from pathlib import Path
    from api.provider_transactions import active_profile_transaction

    home = Path(home_text)
    start_event.wait(5)
    with active_profile_transaction(lambda: home):
        counter_path = home / "counter.txt"
        current = int(counter_path.read_text(encoding="utf-8"))
        time.sleep(0.15)
        counter_path.write_text(str(current + 1), encoding="utf-8")


# ── Unit tests (api/providers.py functions directly) ──────────────────────


class TestGetProviders:
    """Unit tests for get_providers() function."""

    def test_reuses_short_ttl_cache_for_same_profile_home(self, monkeypatch, tmp_path):
        """Back-to-back provider reads should not re-run expensive probes (#6010)."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        from api import providers as prov

        calls = []
        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {"openai": "OpenAI"})
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {"openai": []})
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())
        monkeypatch.setattr(prov, "get_config", lambda: {"model": {}, "providers": {}})

        def _counting_has_key(pid):
            calls.append(pid)
            return False

        monkeypatch.setattr(prov, "_provider_has_key", _counting_has_key)

        try:
            first = prov.get_providers()
            second = prov.get_providers()
            assert first == second
            assert calls == ["openai"]
        finally:
            if hasattr(prov, "invalidate_providers_cache"):
                prov.invalidate_providers_cache()

    def test_summary_mode_skips_live_auth_and_model_probes(
        self, monkeypatch, tmp_path
    ):
        """Settings summary must stay local and never wait on provider networks."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

        from api import providers as prov

        monkeypatch.setattr(
            prov,
            "_PROVIDER_DISPLAY",
            {"nous": "Nous", "lmstudio": "LM Studio", "pluginx": "Plugin X"},
        )
        monkeypatch.setattr(
            prov,
            "_PROVIDER_MODELS",
            {"nous": [], "lmstudio": [], "pluginx": []},
        )
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset({"nous"}))
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: {"pluginx"})
        monkeypatch.setattr(
            prov, "is_plugin_model_provider", lambda pid: pid == "pluginx"
        )
        monkeypatch.setattr(prov, "get_config", lambda: {"model": {}, "providers": {}})
        monkeypatch.setattr(prov, "_provider_has_key", lambda _pid: False)
        auth_calls = []
        model_calls = []

        def live_model_ids(pid):
            model_calls.append(("live", pid))
            return []

        def auth_status(pid):
            auth_calls.append(pid)
            return {"logged_in": False}

        def provider_model_ids(pid):
            model_calls.append(("provider", pid))
            return []

        monkeypatch.setattr(prov, "_read_live_provider_model_ids", live_model_ids)
        sys.modules["hermes_cli.auth"].get_auth_status = auth_status
        sys.modules["hermes_cli.models"].provider_model_ids = provider_model_ids

        try:
            result = prov.get_providers(include_live=False)
            assert {item["id"] for item in result["providers"]} == {
                "nous",
                "lmstudio",
                "pluginx",
            }
            assert auth_calls == []
            assert model_calls == []

            # Summary and rich responses use distinct cache identities.
            prov.get_providers(include_live=True)
            assert auth_calls
            assert model_calls
        finally:
            prov.invalidate_providers_cache()

    def test_provider_cache_is_scoped_by_profile_home(self, monkeypatch, tmp_path):
        """Provider cache entries must not leak across profile homes (#3957/#6010)."""
        _install_fake_hermes_cli(monkeypatch)
        home_a = tmp_path / "a"
        home_b = tmp_path / "b"
        home_a.mkdir()
        home_b.mkdir()
        active_home = {"path": home_a}
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: active_home["path"])

        from api import providers as prov

        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {"openai": "OpenAI"})
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {"openai": []})
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())
        monkeypatch.setattr(prov, "get_config", lambda: {"model": {}, "providers": {}})
        monkeypatch.setattr(prov, "_provider_has_key", lambda _pid: active_home["path"] == home_b)

        try:
            first = prov.get_providers()
            active_home["path"] = home_b
            second = prov.get_providers()
            first_openai = next(p for p in first["providers"] if p["id"] == "openai")
            second_openai = next(p for p in second["providers"] if p["id"] == "openai")
            assert first_openai["has_key"] is False
            assert second_openai["has_key"] is True
        finally:
            if hasattr(prov, "invalidate_providers_cache"):
                prov.invalidate_providers_cache()

    def test_set_provider_key_invalidates_providers_cache(self, monkeypatch, tmp_path):
        """Saving a key should invalidate the cached Providers response (#6010)."""
        _install_fake_hermes_cli(monkeypatch)
        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        from api import providers as prov

        key_present = {"value": False}
        monkeypatch.setattr(prov, "_PROVIDER_DISPLAY", {"anthropic": "Anthropic"})
        monkeypatch.setattr(prov, "_PROVIDER_MODELS", {"anthropic": []})
        monkeypatch.setattr(prov, "_OAUTH_PROVIDERS", frozenset())
        monkeypatch.setattr(prov, "plugin_model_provider_ids", lambda: set())
        monkeypatch.setattr(prov, "get_config", lambda: {"model": {}, "providers": {}})
        monkeypatch.setattr(prov, "_provider_has_key", lambda _pid: key_present["value"])

        def _fake_write_env_file(_path, values):
            key_present["value"] = bool(values.get("ANTHROPIC_API_KEY"))

        monkeypatch.setattr(prov, "_write_env_file", _fake_write_env_file)
        monkeypatch.setattr(prov, "invalidate_models_cache", lambda: None)
        monkeypatch.setattr(prov, "invalidate_account_usage_status_cache", lambda _provider_id=None: None)

        try:
            before = prov.get_providers()
            result = prov.set_provider_key("anthropic", "sk-test-12345678")
            after = prov.get_providers()

            before_anthropic = next(p for p in before["providers"] if p["id"] == "anthropic")
            after_anthropic = next(p for p in after["providers"] if p["id"] == "anthropic")
            assert result["ok"] is True
            assert before_anthropic["has_key"] is False
            assert after_anthropic["has_key"] is True
        finally:
            if hasattr(prov, "invalidate_providers_cache"):
                prov.invalidate_providers_cache()

    def test_oauth_credential_updates_invalidate_providers_cache(self, monkeypatch, tmp_path):
        """OAuth credential updates should invalidate cached Providers responses (#6010)."""
        from api import oauth
        from api import providers as prov

        invalidated_credentials = []
        providers_invalidated = []
        monkeypatch.setattr(config, "invalidate_credential_pool_cache", invalidated_credentials.append)
        monkeypatch.setattr(prov, "invalidate_providers_cache", lambda: providers_invalidated.append(True))

        oauth._persist_codex_credentials(
            tmp_path,
            {"access_token": "access-token", "refresh_token": "refresh-token"},
        )

        assert invalidated_credentials == ["openai-codex"]
        assert providers_invalidated == [True]

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
                assert "id" in p, "Missing 'id' in provider entry"
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

    def test_remove_provider_key_succeeds_when_no_yaml_key_exists(self, monkeypatch, tmp_path):
        """Transactional removal is idempotent when no YAML key exists."""
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


class TestProviderCredentialTransactions:
    def test_conditional_env_restore_holds_lock_across_compare_and_restore(
        self, monkeypatch, tmp_path
    ):
        from api import provider_transactions, streaming

        state = {"depth": 0, "compared": False, "restored": False}

        class TrackingLock:
            def __enter__(self):
                state["depth"] += 1

            def __exit__(self, *_args):
                state["depth"] -= 1

        published = provider_transactions.FileSnapshot(True, b"published", 0o600)
        original = provider_transactions.FileSnapshot(True, b"original", 0o600)

        def snapshot_under_lock(_path):
            assert state["depth"] == 1
            state["compared"] = True
            return published

        def restore_under_same_lock(_path, snapshot):
            assert state["depth"] == 1
            assert snapshot is original
            state["restored"] = True

        monkeypatch.setattr(streaming, "_ENV_LOCK", TrackingLock())
        monkeypatch.setattr(provider_transactions, "snapshot_file", snapshot_under_lock)
        monkeypatch.setattr(provider_transactions, "restore_file", restore_under_same_lock)

        assert provider_transactions.restore_file_if_unchanged(
            tmp_path / ".env", published, original
        ) is True
        assert state == {"depth": 0, "compared": True, "restored": True}

    def test_env_writer_fails_closed_when_existing_file_is_unreadable(
        self, monkeypatch, tmp_path
    ):
        from pathlib import Path

        import pytest

        from api.providers import _write_env_file

        env_path = tmp_path / ".env"
        original = b"OPENAI_API_KEY=original-secret\n"
        env_path.write_bytes(original)
        real_read_text = Path.read_text

        def fail_target_read(path, *args, **kwargs):
            if path == env_path:
                raise PermissionError("simulated unreadable credential file")
            return real_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail_target_read)
        with pytest.raises(RuntimeError, match="could not be read safely"):
            _write_env_file(env_path, {"OPENAI_API_KEY": "replacement-secret"})
        assert env_path.read_bytes() == original

    def test_env_writer_never_mutates_process_environment(self, monkeypatch, tmp_path):
        from api.providers import _write_env_file

        env_path = tmp_path / ".env"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "process-owned-value")
        _write_env_file(env_path, {"ANTHROPIC_API_KEY": "file-owned-value"})
        assert os.environ["ANTHROPIC_API_KEY"] == "process-owned-value"
        assert "ANTHROPIC_API_KEY=file-owned-value" in env_path.read_text(
            encoding="utf-8"
        )
        _write_env_file(env_path, {"ANTHROPIC_API_KEY": None})
        assert os.environ["ANTHROPIC_API_KEY"] == "process-owned-value"

    def test_default_profile_reads_new_key_without_process_env_mutation(
        self, monkeypatch, tmp_path
    ):
        from api import providers
        from api.config import _thread_local_env_value
        from api.profiles import profile_env_for_active_request_readonly

        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = providers.set_provider_key(
            "anthropic", "sk-ant-default-profile-test-12345678"
        )
        assert result["ok"] is True
        assert os.environ.get("ANTHROPIC_API_KEY") is None
        with profile_env_for_active_request_readonly("test default profile key"):
            assert (
                _thread_local_env_value("ANTHROPIC_API_KEY")
                == "sk-ant-default-profile-test-12345678"
            )
        assert os.environ.get("ANTHROPIC_API_KEY") is None

    def test_remove_reports_persisted_partial_failure_when_runtime_reload_fails(
        self, monkeypatch, tmp_path
    ):
        from api import providers

        monkeypatch.setattr(providers, "_get_hermes_home", lambda: tmp_path)
        (tmp_path / ".env").write_text(
            "ANTHROPIC_API_KEY=profile-secret\n", encoding="utf-8"
        )
        (tmp_path / "config.yaml").write_text(
            "model:\n  provider: anthropic\n  default: claude-test\n  api_key: ${ANTHROPIC_API_KEY}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            providers,
            "reload_config",
            lambda: (_ for _ in ()).throw(RuntimeError("reload failed")),
        )

        result = providers.remove_provider_key("anthropic")

        assert result == {
            "ok": False,
            "persisted": True,
            "provider": "anthropic",
            "error": "Credential was removed, but runtime configuration could not be refreshed.",
        }
        assert "ANTHROPIC_API_KEY" not in (
            tmp_path / ".env"
        ).read_text(encoding="utf-8")
        assert "api_key" not in (
            tmp_path / "config.yaml"
        ).read_text(encoding="utf-8")

    def test_remove_route_preserves_persisted_partial_failure_payload(
        self, monkeypatch
    ):
        from urllib.parse import urlparse

        from api import routes

        expected = {
            "ok": False,
            "persisted": True,
            "provider": "anthropic",
            "error": "Credential was removed, but runtime configuration could not be refreshed.",
        }
        captured = {}
        monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
        monkeypatch.setattr(
            routes, "_handle_extension_sidecar_proxy", lambda *_a, **_k: False
        )
        monkeypatch.setattr(routes, "read_body", lambda _handler: {"provider": "anthropic"})
        monkeypatch.setattr(
            routes,
            "_guard_request_session_visibility",
            lambda *_a, **_k: True,
        )
        monkeypatch.setattr(routes, "remove_provider_key", lambda _provider: expected)

        def capture_json(_handler, payload, status=200, **_kwargs):
            captured.update(payload=payload, status=status)
            return True

        monkeypatch.setattr(routes, "j", capture_json)
        result = routes.handle_post(
            object(), urlparse("/api/providers/delete")
        )

        assert result is True
        assert captured == {"payload": expected, "status": 500}

    def test_remove_rolls_back_env_and_yaml_when_yaml_save_fails(
        self, monkeypatch, tmp_path
    ):
        from api import providers

        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        env_path = tmp_path / ".env"
        config_path = tmp_path / "config.yaml"
        env_path.write_text("ANTHROPIC_API_KEY=original-secret\n", encoding="utf-8")
        config_path.write_text(
            "providers:\n  anthropic:\n    api_key: ${ANTHROPIC_API_KEY}\n",
            encoding="utf-8",
        )
        before_env = env_path.read_bytes()
        before_config = config_path.read_bytes()
        monkeypatch.setattr(
            providers,
            "_save_yaml_config_file",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("boom")),
        )

        result = providers.remove_provider_key("anthropic")

        assert result["ok"] is False
        assert "transactionally" in result["error"]
        assert env_path.read_bytes() == before_env
        assert config_path.read_bytes() == before_config

    def test_profile_switch_before_write_fails_closed(self, monkeypatch, tmp_path):
        from api import providers

        other_home = tmp_path / "other"
        homes = iter((tmp_path, tmp_path, other_home))
        monkeypatch.setattr(providers, "_get_hermes_home", lambda: next(homes))
        result = providers.set_provider_key(
            "anthropic", "sk-ant-profile-race-test-12345678"
        )
        assert result["ok"] is False
        assert not (tmp_path / ".env").exists()
        assert not (other_home / ".env").exists()

    def test_two_processes_serialize_same_profile_mutation(self, tmp_path):
        if sys.platform == "win32":
            import pytest

            pytest.skip("test-only file lock uses fcntl")
        counter_path = tmp_path / "counter.txt"
        counter_path.write_text("0", encoding="utf-8")
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        workers = [
            ctx.Process(
                target=_increment_under_profile_transaction,
                args=(str(tmp_path), start),
            )
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0
        assert counter_path.read_text(encoding="utf-8") == "2"

    def test_shared_profile_lock_is_outermost_for_key_write(
        self, monkeypatch, tmp_path
    ):
        from contextlib import contextmanager
        from api import provider_transactions, providers

        monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
        events = []

        @contextmanager
        def fake_transaction(resolver=None):
            events.append("lock-enter")
            yield tmp_path
            events.append("lock-exit")

        def fake_env_write(path, updates):
            assert events == ["lock-enter"]
            events.append("env-write")

        monkeypatch.setattr(
            provider_transactions, "active_profile_transaction", fake_transaction
        )
        monkeypatch.setattr(providers, "_write_env_file", fake_env_write)
        result = providers.set_provider_key(
            "anthropic", "sk-ant-transaction-test-12345678"
        )
        assert result["ok"] is True
        assert events == ["lock-enter", "env-write", "lock-exit"]


# ── Integration tests (via HTTP endpoints) ───────────────────────────────


class TestProvidersEndpoints:
    """Integration tests for /api/providers HTTP endpoints."""

    def test_get_providers_returns_200(self):
        """GET /api/providers should return 200 with provider list."""
        result = _get("/api/providers")
        assert "providers" in result

    def test_get_provider_summary_returns_200(self):
        """The Settings summary route keeps the provider response shape."""
        result = _get("/api/providers?summary=1")
        assert "providers" in result
        assert "active_provider" in result
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
