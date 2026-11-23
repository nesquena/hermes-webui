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

    def test_pinned_provider_group_is_saved_as_provider_id(self):
        import yaml

        result = set_hermes_default_model(
            "meituan/longcat-2.5:free", provider="nous:meituan/longcat-2.5"
        )

        assert result == {"ok": True, "model": "meituan/longcat-2.5:free", "provider": "nous"}
        saved = yaml.safe_load(config._get_config_path().read_text(encoding="utf-8"))
        assert saved["model"]["provider"] == "nous"
        assert saved["model"]["base_url"] == "https://router.nous.ai/v1"

    @pytest.mark.parametrize("section", ["providers", "custom_providers"])
    def test_config_declared_colon_provider_is_not_split(self, section):
        import yaml

        entry = {"base_url": "https://eu-proxy.example/v1", "api_key": "DUMMY_EU"}
        if section == "providers":
            declared = {"openrouter:eu": entry}
        else:
            declared = [{"name": "openrouter:eu", **entry}]
        path = config._get_config_path()
        path.write_text(
            yaml.safe_dump(
                {
                    "model": {"provider": "openrouter:eu", "default": "gpt-x", "base_url": "https://eu-proxy.example/v1"},
                    section: declared,
                }
            ),
            encoding="utf-8",
        )
        config.cfg.clear()
        config.cfg.update(yaml.safe_load(path.read_text(encoding="utf-8")))

        result = set_hermes_default_model("gpt-x", provider="openrouter:eu")

        assert result["provider"] == "openrouter:eu"
        saved = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert saved["model"]["provider"] == "openrouter:eu"
        assert saved["model"].get("base_url") != "https://openrouter.ai/api/v1"

    def test_env_named_colon_provider_is_not_split(self, monkeypatch):
        """A custom provider whose NAME is a ${VAR} reference is declared only once
        expanded: the raw file says ``${EU_PROXY_NAME}``, the runtime config says
        ``openrouter:eu``. The save must keep it, not reroute to public OpenRouter."""
        import yaml

        monkeypatch.setenv("EU_PROXY_NAME", "openrouter:eu")
        path = config._get_config_path()
        path.write_text(
            yaml.safe_dump(
                {
                    "model": {"provider": "openrouter:eu", "default": "gpt-x", "base_url": "https://eu-proxy.example/v1"},
                    "custom_providers": [
                        {"name": "${EU_PROXY_NAME}", "base_url": "https://eu-proxy.example/v1", "api_key": "DUMMY_EU"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        config.reload_config()
        assert any(
            (e or {}).get("name") == "openrouter:eu" for e in config.cfg.get("custom_providers") or []
        ), config.cfg.get("custom_providers")

        result = set_hermes_default_model("gpt-x", provider="openrouter:eu")

        assert result["provider"] == "openrouter:eu"
        saved = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert saved["model"]["provider"] == "openrouter:eu"
        assert saved["model"].get("base_url") != "https://openrouter.ai/api/v1"

    def test_at_nous_prefix_strips_to_bare_for_cli_compatibility(self, tmp_path, monkeypatch):
        """set_hermes_default_model must persist the RESOLVED bare/slash form, not the
        `@provider:` prefix. The `@provider:` syntax is a WebUI-internal routing hint;
        the hermes-agent CLI reads `config.yaml -> model.default` directly and passes
        it to the provider API verbatim (see run_agent.py:887 — aggregator providers
        like Nous skip normalize_model_for_provider, so the raw string flows through).
        Storing `@nous:anthropic/...` would break any user who runs `hermes` in the
        terminal right after saving via WebUI — the CLI would send the literal
        prefixed string to Nous and hit a 404. The Settings picker handles the bare
        form via the smart matcher in `_applyModelToDropdown()`.
        """
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

        # Result ack echoes the resolved bare/slash form (CLI-compatible)
        assert result.get("ok") is True
        assert result.get("model") == "anthropic/claude-opus-4.6", (
            f"result.model should echo the CLI-compatible resolved form, not the "
            f"WebUI-internal @-prefix: {result.get('model')!r}"
        )

        saved = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert saved["model"]["default"] == "anthropic/claude-opus-4.6", (
            f"Config must persist the resolved bare form so the hermes-agent CLI "
            f"can read it and pass it to the provider API: "
            f"{saved['model']['default']!r}"
        )

    def test_settings_picker_applies_saved_default_via_smart_matcher(self):
        """The Settings picker must use `_applyModelToDropdown()` (smart matcher),
        not raw `modelSel.value = ...`, when initialising from the saved default.

        Raw `.value =` silently fails if no option matches exactly — blank picker
        on reopen for any saved default whose canonical form doesn't equal an option
        value (e.g. CLI-saved `anthropic/claude-opus-4.6` vs Nous dropdown option
        `@nous:anthropic/claude-opus-4.6`). `_applyModelToDropdown()` normalises
        on both sides and picks the matching option.
        """
        js = (Path(__file__).resolve().parent.parent / "static" / "panels.js").read_text()
        # Find the block that sets _settingsHermesDefaultModelOnOpen
        anchor = "_settingsHermesDefaultModelOnOpen=(models&&models.default_model)||"
        idx = js.find(anchor)
        assert idx != -1, "Settings default-model initialisation not found in panels.js"
        block = js[idx:idx + 1200]
        assert "_applyModelToDropdown" in block, (
            "Settings picker must use _applyModelToDropdown() so a saved bare form "
            "(e.g. anthropic/claude-opus-4.6) still selects the matching "
            "@nous:anthropic/claude-opus-4.6 option. A raw .value assignment leaves "
            "the picker blank when the saved ID doesn't match an option verbatim."
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
