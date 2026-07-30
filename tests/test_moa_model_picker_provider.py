def test_moa_presets_render_as_virtual_provider_models(monkeypatch, tmp_path):
    import api.config as config

    cfg = {
        "model": {"default": "gpt-5.5", "provider": "copilot"},
        "moa": {
            "default_preset": "Frontier Tuned",
            "presets": {
                "default": {"enabled": True},
                "Frontier Tuned": {"enabled": True},
                "Disabled Preset": {"enabled": False},
            },
        },
    }

    monkeypatch.setattr(config, "cfg", cfg, raising=False)
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "config.yaml")
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_models_cache_source_fingerprint", lambda: {"test": "fingerprint"})
    monkeypatch.setattr(config, "reload_config_if_stale", lambda: None)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    monkeypatch.setattr(config, "_cfg_mtime", 0.0, raising=False)
    monkeypatch.setattr(config, "_LIVE_REBUILD_BUDGET_SECONDS", 0.0, raising=False)

    def fake_live_models(provider_id):
        if provider_id == "copilot":
            return ["gpt-5.5"]
        # MoA presets are local config, so the picker must still render them
        # when older Hermes Agent installs cannot provide provider_model_ids("moa").
        if provider_id == "moa":
            return []
        return []

    monkeypatch.setattr(config, "_read_live_provider_model_ids", fake_live_models)

    config.invalidate_models_cache()
    payload = config.get_available_models(force_refresh=True)
    moa_group = next(g for g in payload["groups"] if g.get("provider_id") == "moa")

    assert moa_group["provider"] == "Mixture of Agents"
    assert [m["id"] for m in moa_group["models"]] == ["@moa:default", "@moa:Frontier Tuned"]


def test_moa_picker_selection_resolves_to_preset_and_provider():
    from api.routes import _resolve_compatible_session_model_state

    model, provider, normalized = _resolve_compatible_session_model_state(
        "moa/Frontier Tuned",
        "moa",
        profile_provider="copilot",
        profile_default_model="gpt-5.5",
        explicit_model_pick=True,
    )

    assert model == "Frontier Tuned"
    assert provider == "moa"
    assert normalized is True


def test_resolve_moa_config_uses_selected_preset(monkeypatch):
    import sys
    from types import ModuleType
    from typing import Any, cast

    import api.commands as commands

    cfg = {
        "moa": {
            "default_preset": "default",
            "presets": {
                "default": {
                    "enabled": True,
                    "reference_models": [{"provider": "copilot", "model": "claude-sonnet-4.6"}],
                    "aggregator": {"provider": "copilot", "model": "gpt-5.5"},
                },
                "Frontier Tuned": {
                    "enabled": True,
                    "reference_models": [{"provider": "copilot", "model": "claude-opus-4.8"}],
                    "aggregator": {"provider": "copilot", "model": "gpt-5.4"},
                },
            },
        }
    }

    hermes_cli_pkg = sys.modules.get("hermes_cli") or ModuleType("hermes_cli")
    # monkeypatch.setattr restores the REAL hermes_cli.__path__ on teardown;
    # emptying it in place would strand the package for the rest of the suite.
    monkeypatch.setattr(hermes_cli_pkg, "__path__", [], raising=False)
    moa_config = ModuleType("hermes_cli.moa_config")

    def normalize_moa_config(raw):
        return {
            "default_preset": raw.get("default_preset", "default"),
            "presets": raw.get("presets", {}),
        }

    def resolve_moa_preset(raw, preset_name):
        presets = raw.get("presets", {}) if isinstance(raw, dict) else {}
        return dict(presets.get(preset_name or raw.get("default_preset") or "default", {}))

    moa_config_any = cast(Any, moa_config)
    moa_config_any.normalize_moa_config = normalize_moa_config
    moa_config_any.resolve_moa_preset = resolve_moa_preset
    moa_config_any.moa_usage = lambda: "Usage: /moa <prompt>"
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.moa_config", moa_config)
    monkeypatch.setattr(commands, "_load_config_for_moa_resolution", lambda: cfg, raising=False)

    resolved = commands.resolve_moa_config("Frontier Tuned")

    assert resolved["preset"] == "Frontier Tuned"
    assert resolved["reference_models"] == [{"provider": "copilot", "model": "claude-opus-4.8"}]
    assert resolved["aggregator"] == {"provider": "copilot", "model": "gpt-5.4"}


def test_resolve_moa_config_falls_back_when_preset_resolution_raises(monkeypatch):
    import sys
    from types import ModuleType
    from typing import Any, cast

    import api.commands as commands

    cfg = {
        "moa": {
            "default_preset": "default",
            "presets": {"default": {"enabled": True}},
        }
    }
    hermes_cli_pkg = sys.modules.get("hermes_cli") or ModuleType("hermes_cli")
    # monkeypatch.setattr restores the REAL hermes_cli.__path__ on teardown;
    # emptying it in place would strand the package for the rest of the suite.
    monkeypatch.setattr(hermes_cli_pkg, "__path__", [], raising=False)
    moa_config = ModuleType("hermes_cli.moa_config")

    def normalize_moa_config(raw):
        return {
            "default_preset": raw.get("default_preset", "default"),
            "presets": raw.get("presets", {}),
        }

    def resolve_moa_preset(_raw, _preset_name):
        raise ValueError("boom")

    moa_config_any = cast(Any, moa_config)
    moa_config_any.normalize_moa_config = normalize_moa_config
    moa_config_any.resolve_moa_preset = resolve_moa_preset
    moa_config_any.moa_usage = lambda: "Usage: /moa <prompt>"
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.moa_config", moa_config)
    monkeypatch.setattr(commands, "_load_config_for_moa_resolution", lambda: cfg, raising=False)

    resolved = commands.resolve_moa_config("broken")

    assert resolved["preset"] == "default"
    assert resolved["default_preset"] == "default"
    assert resolved["usage"] == "Usage: /moa <prompt>"


def test_resolve_moa_config_ignores_non_dict_preset_result(monkeypatch):
    import sys
    from types import ModuleType
    from typing import Any, cast

    import api.commands as commands

    cfg = {
        "moa": {
            "default_preset": "default",
            "presets": {"default": {"enabled": True}},
        }
    }
    hermes_cli_pkg = sys.modules.get("hermes_cli") or ModuleType("hermes_cli")
    # monkeypatch.setattr restores the REAL hermes_cli.__path__ on teardown;
    # emptying it in place would strand the package for the rest of the suite.
    monkeypatch.setattr(hermes_cli_pkg, "__path__", [], raising=False)
    moa_config = ModuleType("hermes_cli.moa_config")

    def normalize_moa_config(raw):
        return {
            "default_preset": raw.get("default_preset", "default"),
            "presets": raw.get("presets", {}),
        }

    # A hermes-agent build that returns ``None`` for a missing/unknown preset
    # instead of raising. Without a dict guard, ``resolved.update(None)`` would
    # raise ``TypeError`` and bypass the routes.py ``except RuntimeError`` guard,
    # surfacing as an unhandled 500 on chat start.
    def resolve_moa_preset(_raw, _preset_name):
        return None

    moa_config_any = cast(Any, moa_config)
    moa_config_any.normalize_moa_config = normalize_moa_config
    moa_config_any.resolve_moa_preset = resolve_moa_preset
    moa_config_any.moa_usage = lambda: "Usage: /moa <prompt>"
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.moa_config", moa_config)
    monkeypatch.setattr(commands, "_load_config_for_moa_resolution", lambda: cfg, raising=False)

    resolved = commands.resolve_moa_config("default")

    assert isinstance(resolved, dict)
    assert resolved["preset"] == "default"
    assert resolved["default_preset"] == "default"
    assert resolved["usage"] == "Usage: /moa <prompt>"
