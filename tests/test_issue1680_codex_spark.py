"""Regression tests for #1680 — Codex model picker uses live Codex discovery."""

import json
import sys
import types

from api import config


def _flatten_ids(groups):
    return [m.get("id") for g in groups for m in g.get("models", [])]


def _install_fake_hermes_models(monkeypatch, provider_model_ids):
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    models = types.ModuleType("hermes_cli.models")
    models._PROVIDER_ALIASES = {}
    models.provider_model_ids = provider_model_ids
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", models)


def _configure_codex(monkeypatch, tmp_path, default="gpt-5.5"):
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "missing-config.yaml")
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "cfg", {
        "model": {"provider": "openai-codex", "default": default},
        "providers": {},
        "fallback_providers": [],
    })
    monkeypatch.setattr(config, "_cfg_mtime", 0.0)
    monkeypatch.setattr(config, "_cfg_path", config._get_config_path(), raising=False)
    config.invalidate_models_cache()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))


def test_openai_codex_group_uses_provider_model_ids(monkeypatch, tmp_path):
    """Models from live Codex discovery must surface in /api/models.

    The live account catalog can contain models absent from the static WebUI
    fallback, so get_available_models() must ask hermes_cli for Codex models.
    """
    calls = []

    def provider_model_ids(provider):
        calls.append(provider)
        assert provider == "openai-codex"
        return ["gpt-6-astra"]

    _install_fake_hermes_models(monkeypatch, provider_model_ids)
    _configure_codex(monkeypatch, tmp_path)

    result = config.get_available_models()

    codex_groups = [g for g in result["groups"] if g.get("provider_id") == "openai-codex"]
    assert "openai-codex" in calls
    assert codex_groups, "OpenAI Codex group should be present"
    assert "gpt-6-astra" not in [m["id"] for m in config._PROVIDER_MODELS["openai-codex"]]
    assert any(m["id"] == "gpt-6-astra" and m["label"] == "GPT 6 Astra" for g in codex_groups for m in g["models"])


def test_openai_codex_group_merges_visible_codex_cache_models(monkeypatch, tmp_path):
    """Visible Codex CLI cache models should appear even if API-filtered.

    A visible cache entry may have ``supported_in_api: false`` and be absent
    from live discovery; the WebUI picker should still include that entry.
    """
    def provider_model_ids(provider):
        assert provider == "openai-codex"
        return ["gpt-5.5"]

    _install_fake_hermes_models(monkeypatch, provider_model_ids)
    _configure_codex(monkeypatch, tmp_path)

    codex_home = tmp_path / "codex-home"
    (codex_home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-5.5", "visibility": "list", "priority": 0},
                    {
                        "slug": "codex-cache-only-test",
                        "visibility": "list",
                        "supported_in_api": False,
                        "priority": 7,
                    },
                    {"slug": "hidden-test-model", "visibility": "hide", "priority": 8},
                ]
            }
        ),
        encoding="utf-8",
    )
    result = config.get_available_models()

    codex_groups = [g for g in result["groups"] if g.get("provider_id") == "openai-codex"]
    ids = _flatten_ids(codex_groups)
    assert "codex-cache-only-test" in ids
    assert "hidden-test-model" not in ids
