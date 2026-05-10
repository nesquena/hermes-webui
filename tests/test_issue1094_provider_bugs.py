"""Tests for issue #1094 — provider deletion and has_key false positive bugs.

Bug 1: _provider_has_key() returned True for all providers when
        config.yaml model.api_key was set (checked globally instead of
        only matching the active provider).

Bug 2: remove_provider_key() only removed from .env but left keys in
        config.yaml (providers.<id>.api_key and model.api_key), so the
        provider still showed as "configured" after deletion.
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
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
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

    try:
        from api.config import invalidate_models_cache

        invalidate_models_cache()
    except Exception:
        pass


def _setup_clean_config(monkeypatch, tmp_path):
    """Common setup: clean config, fake CLI, tmp hermes home.

    Also clears provider API key env vars so _provider_has_key()
    doesn't detect keys from the host environment.
    """
    _install_fake_hermes_cli(monkeypatch)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    # Clear provider API key env vars to prevent host env leaking into tests
    _provider_env_vars = [
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GLM_API_KEY",
        "KIMI_API_KEY",
        "DEEPSEEK_API_KEY",
        "MINIMAX_API_KEY",
        "MISTRAL_API_KEY",
        "XAI_API_KEY",
        "OLLAMA_API_KEY",
        "OPENCODE_ZEN_API_KEY",
        "OPENCODE_GO_API_KEY",
    ]
    for var in _provider_env_vars:
        monkeypatch.delenv(var, raising=False)

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0

    return old_cfg, old_mtime


def _restore_config(old_cfg, old_mtime):
    """Restore config after test."""
    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime


# ── Bug 1: has_key false positive ─────────────────────────────────────────


class TestBug1094HasKeyFalsePositive:
    """Bug 1: model.api_key in config.yaml should only mark the active
    provider as having a key, not all providers."""

    def test_model_api_key_only_marks_active_provider(self, monkeypatch, tmp_path):
        """If model.api_key is set with provider='anthropic', only
        anthropic should show has_key=True, not openai or deepseek."""
        old_cfg, old_mtime = _setup_clean_config(monkeypatch, tmp_path)

        try:
            from api.providers import _provider_has_key

            # Set up config with anthropic as active provider and a top-level api_key
            config.cfg["model"] = {
                "provider": "anthropic",
                "api_key": "sk-ant-test-key-12345678",
                "model": "claude-sonnet-4-20250514",
            }

            assert _provider_has_key("anthropic") is True, (
                "anthropic (active provider) should have key"
            )
            assert _provider_has_key("openai") is False, (
                "openai should NOT show has_key just because anthropic has model.api_key"
            )
            assert _provider_has_key("deepseek") is False, (
                "deepseek should NOT show has_key just because anthropic has model.api_key"
            )
            assert _provider_has_key("openrouter") is False, (
                "openrouter should NOT show has_key just because anthropic has model.api_key"
            )
        finally:
            _restore_config(old_cfg, old_mtime)

    def test_model_api_key_with_different_active_provider(self, monkeypatch, tmp_path):
        """model.api_key with provider=openai should only mark openai."""
        old_cfg, old_mtime = _setup_clean_config(monkeypatch, tmp_path)

        try:
            from api.providers import _provider_has_key

            config.cfg["model"] = {
                "provider": "openai",
                "api_key": "sk-openai-test-key-12345",
                "model": "gpt-4o",
            }

            assert _provider_has_key("openai") is True
            assert _provider_has_key("anthropic") is False
            assert _provider_has_key("deepseek") is False
        finally:
            _restore_config(old_cfg, old_mtime)

    def test_no_model_api_key_no_false_positive(self, monkeypatch, tmp_path):
        """Without model.api_key, no provider should show has_key from config."""
        old_cfg, old_mtime = _setup_clean_config(monkeypatch, tmp_path)

        try:
            from api.providers import _provider_has_key

            config.cfg["model"] = {
                "provider": "anthropic",
                "model": "claude-sonnet-4-20250514",
            }

            assert _provider_has_key("anthropic") is False
            assert _provider_has_key("openai") is False
        finally:
            _restore_config(old_cfg, old_mtime)

    def test_providers_section_api_key_still_detected(self, monkeypatch, tmp_path):
        """providers.<id>.api_key should still be detected correctly."""
        old_cfg, old_mtime = _setup_clean_config(monkeypatch, tmp_path)

        try:
            from api.providers import _provider_has_key

            config.cfg["model"] = {"provider": "anthropic"}
            config.cfg["providers"] = {
                "deepseek": {"api_key": "sk-deepseek-test-12345678"},
            }

            assert _provider_has_key("deepseek") is True
            assert _provider_has_key("anthropic") is False
        finally:
            _restore_config(old_cfg, old_mtime)


# ── Bug 2: remove_provider_key doesn't clean config.yaml ──────────────────


class TestBug1094RemoveProviderKey:
    """Bug 2: removing a provider key should also clean config.yaml."""

    def test_remove_key_from_providers_section(self, monkeypatch, tmp_path):
        """Removing a key stored in providers.<id>.api_key should delete it."""
        old_cfg, old_mtime = _setup_clean_config(monkeypatch, tmp_path)

        # Create a fake config.yaml with a provider key
        import yaml as _yaml

        config_path = tmp_path / "config.yaml"
        config_data = {
            "model": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
            "providers": {
                "deepseek": {"api_key": "sk-deepseek-test-12345678"},
            },
        }
        config_path.write_text(_yaml.safe_dump(config_data), encoding="utf-8")
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        config.cfg.clear()
        config.cfg.update(config_data)

        try:
            from api.providers import _provider_has_key, remove_provider_key

            # Verify key is detected before removal
            assert _provider_has_key("deepseek") is True

            # Remove the key
            result = remove_provider_key("deepseek")
            assert result["ok"] is True
            assert result["action"] == "removed"

            # Verify key is gone from config.yaml
            reloaded = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
            deepseek_cfg = reloaded.get("providers", {}).get("deepseek", {})
            assert "api_key" not in deepseek_cfg, (
                "api_key should be removed from providers.deepseek in config.yaml"
            )

            # Verify _provider_has_key no longer detects it
            config.cfg.clear()
            config.cfg.update(reloaded)
            assert _provider_has_key("deepseek") is False, (
                "deepseek should not have key after removal"
            )
        finally:
            _restore_config(old_cfg, old_mtime)

    def test_remove_key_from_model_section_when_active(self, monkeypatch, tmp_path):
        """Removing the active provider's key should clean model.api_key."""
        old_cfg, old_mtime = _setup_clean_config(monkeypatch, tmp_path)

        import yaml as _yaml

        config_path = tmp_path / "config.yaml"
        config_data = {
            "model": {
                "provider": "anthropic",
                "api_key": "sk-ant-test-key-12345678",
                "model": "claude-sonnet-4-20250514",
            },
        }
        config_path.write_text(_yaml.safe_dump(config_data), encoding="utf-8")
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        config.cfg.clear()
        config.cfg.update(config_data)

        try:
            from api.providers import _provider_has_key, remove_provider_key

            assert _provider_has_key("anthropic") is True

            result = remove_provider_key("anthropic")
            assert result["ok"] is True

            reloaded = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
            model_cfg = reloaded.get("model", {})
            assert "api_key" not in model_cfg, (
                "api_key should be removed from model section in config.yaml"
            )

            config.cfg.clear()
            config.cfg.update(reloaded)
            assert _provider_has_key("anthropic") is False
        finally:
            _restore_config(old_cfg, old_mtime)

    def test_remove_non_active_provider_does_not_touch_model_api_key(
        self, monkeypatch, tmp_path
    ):
        """Removing deepseek should NOT touch model.api_key if active is anthropic."""
        old_cfg, old_mtime = _setup_clean_config(monkeypatch, tmp_path)

        import yaml as _yaml

        config_path = tmp_path / "config.yaml"
        config_data = {
            "model": {
                "provider": "anthropic",
                "api_key": "sk-ant-test-key-12345678",
                "model": "claude-sonnet-4-20250514",
            },
            "providers": {
                "deepseek": {"api_key": "sk-deepseek-test-12345678"},
            },
        }
        config_path.write_text(_yaml.safe_dump(config_data), encoding="utf-8")
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
        config.cfg.clear()
        config.cfg.update(config_data)

        try:
            from api.providers import remove_provider_key

            result = remove_provider_key("deepseek")
            assert result["ok"] is True

            reloaded = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
            # anthropic's model.api_key should still exist (we only removed deepseek)
            assert reloaded["model"].get("api_key"), (
                "model.api_key for active provider should not be removed"
            )
            # deepseek's key should be gone
            assert "api_key" not in reloaded.get("providers", {}).get("deepseek", {})
        finally:
            _restore_config(old_cfg, old_mtime)

    def test_remove_key_with_no_config_file(self, monkeypatch, tmp_path):
        """Removing when no config.yaml exists should still succeed (env-only key)."""
        old_cfg, old_mtime = _setup_clean_config(monkeypatch, tmp_path)

        # No config.yaml — tmp_path is empty
        config_path = tmp_path / "config.yaml"
        assert not config_path.exists()
        monkeypatch.setattr(config, "_get_config_path", lambda: config_path)

        try:
            from api.providers import remove_provider_key

            result = remove_provider_key("anthropic")
            assert result["ok"] is True
            assert result["action"] == "removed"
        finally:
            _restore_config(old_cfg, old_mtime)


# ── Integration: HTTP endpoints ───────────────────────────────────────────


class TestBug1094Endpoints:
    """Integration tests via HTTP endpoints for #1094 fixes."""

    def test_delete_provider_via_http(self):
        """POST /api/providers/delete should return 200 and ok=True."""
        body, status = _post("/api/providers/delete", {"provider": "anthropic"})
        assert status == 200
        assert body.get("ok") is True

    def test_get_providers_after_delete(self):
        """After deleting a provider, GET /api/providers should show has_key=False."""
        # Ensure no env key for anthropic first
        _post("/api/providers/delete", {"provider": "anthropic"})

        result = _get("/api/providers")
        anthropic = next(
            (p for p in result["providers"] if p["id"] == "anthropic"),
            None,
        )
        assert anthropic is not None, "anthropic should be in providers list"
        # has_key should be False unless there's a config.yaml key set
        # (which integration tests won't have in tmp test state)
        assert anthropic["has_key"] is False, (
            f"anthropic should not have key after deletion, got has_key={anthropic['has_key']}"
        )
