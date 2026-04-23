"""
Regression tests for #895 (set_hermes_default_model strips @nous: prefix + blocks on live fetch)
and #894 (resolve_model_provider strips cross-namespace prefix for portal providers with base_url).
"""
import threading
import pytest
from pathlib import Path

import api.config as config
from api.config import resolve_model_provider, set_hermes_default_model


# ── Shared fixture ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_cache = config._available_models_cache
    old_cache_ts = config._available_models_cache_ts

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "model:\n  provider: nous\n  base_url: https://router.nous.ai/v1\n  default: anthropic/claude-opus-4.6\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: Path(str(config_file)))
    config.cfg.clear()
    config.cfg.update({
        "model": {
            "provider": "nous",
            "base_url": "https://router.nous.ai/v1",
            "default": "anthropic/claude-opus-4.6",
        }
    })
    try:
        config._cfg_mtime = config_file.stat().st_mtime
    except OSError:
        config._cfg_mtime = 0.0
    config.invalidate_models_cache()

    yield

    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime
    config._available_models_cache = old_cache
    config._available_models_cache_ts = old_cache_ts


# ── #894: portal-provider + config_base_url prefix-stripping ───────────────

class TestResolveModelProviderPortalPriority:

    def test_minimax_prefix_preserved_for_nous(self):
        """Nous with base_url must NOT strip minimax/ prefix (#894)."""
        m, p, _ = resolve_model_provider("minimax/minimax-m2.7")
        assert m == "minimax/minimax-m2.7", f"prefix was stripped: {m!r}"
        assert p == "nous"

    def test_qwen_prefix_preserved_for_nous(self):
        """Nous with base_url must NOT strip qwen/ prefix (#894)."""
        m, p, _ = resolve_model_provider("qwen/qwen3.5-35b-a3b")
        assert m == "qwen/qwen3.5-35b-a3b", f"prefix was stripped: {m!r}"
        assert p == "nous"

    def test_anthropic_prefix_preserved_for_nous(self):
        """Core case: anthropic/claude-opus-4.6 must route to nous intact."""
        m, p, _ = resolve_model_provider("anthropic/claude-opus-4.6")
        assert m == "anthropic/claude-opus-4.6"
        assert p == "nous"

    def test_at_nous_prefix_unpacked_correctly(self):
        """@nous:anthropic/claude-opus-4.6 should unpack to bare model and nous provider."""
        m, p, _ = resolve_model_provider("@nous:anthropic/claude-opus-4.6")
        assert m == "anthropic/claude-opus-4.6"
        assert p == "nous"

    def test_unknown_prefix_preserved_for_nous(self):
        """Non-PROVIDER_MODELS prefix like moonshotai/ must also pass through intact."""
        m, p, _ = resolve_model_provider("moonshotai/kimi-k2.6")
        assert m == "moonshotai/kimi-k2.6"
        assert p == "nous"


# ── #895: set_hermes_default_model persists @provider: prefix ──────────────

class TestSetDefaultModelPreservesAtPrefix:

    def test_at_nous_prefix_persisted_verbatim(self, tmp_path, monkeypatch):
        """set_hermes_default_model must store @nous:... verbatim (#895)."""
        import yaml
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "model:\n  provider: nous\n  base_url: https://router.nous.ai/v1\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_get_config_path", lambda: Path(str(config_file)))
        config.cfg["model"] = {"provider": "nous", "base_url": "https://router.nous.ai/v1"}
        try:
            config._cfg_mtime = config_file.stat().st_mtime
        except OSError:
            config._cfg_mtime = 0.0

        result = set_hermes_default_model("@nous:anthropic/claude-opus-4.6")

        # Result should be a simple ack, not the full model catalog
        assert result.get("ok") is True
        assert result.get("model") == "@nous:anthropic/claude-opus-4.6"

        # Persisted value in YAML must match exactly
        saved = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert saved["model"]["default"] == "@nous:anthropic/claude-opus-4.6", (
            f"@nous: prefix was stripped before saving: {saved['model']['default']!r}"
        )

    def test_save_does_not_return_full_model_catalog(self, tmp_path, monkeypatch):
        """set_hermes_default_model must return a lightweight ack, not call get_available_models (#895)."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "model:\n  provider: openrouter\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "_get_config_path", lambda: Path(str(config_file)))
        config.cfg["model"] = {"provider": "openrouter"}
        try:
            config._cfg_mtime = config_file.stat().st_mtime
        except OSError:
            config._cfg_mtime = 0.0

        result = set_hermes_default_model("openai/gpt-5.4-mini")
        # Must be a simple dict with ok+model, NOT the full catalog (which has "groups")
        assert result.get("ok") is True
        assert "groups" not in result, (
            "set_hermes_default_model must not return the full model catalog — "
            "doing so triggers a live provider fetch that blocks the HTTP response"
        )
