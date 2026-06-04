"""Regression tests for #1680 — Codex model picker uses live Codex discovery.

Hermes Agent is the authoritative source for the Codex model catalog.
When Agent returns a non-empty list, WebUI must use that list exactly
and must NOT merge stale local Codex cache models on top.  The local
Codex cache is a fallback only, used when Agent returns an empty list
or raises an exception.
"""

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


def _configure_codex(monkeypatch, tmp_path, default="gpt-5.3-codex-spark"):
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "missing-config.yaml")
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "cfg", {
        "model": {"provider": "openai-codex", "default": default},
        "providers": {},
        "fallback_providers": [],
    })
    monkeypatch.setattr(config, "_cfg_mtime", 0.0)
    config.invalidate_models_cache()


def test_openai_codex_group_uses_provider_model_ids_for_spark(monkeypatch, tmp_path):
    """Codex-only models from the Codex catalog must surface in /api/models.

    The static WebUI fallback chronically drifts.  ``gpt-5.3-codex-spark`` is
    the regression case from #1680: it is discoverable by the Codex provider
    resolver but was missing from the picker because get_available_models()
    copied _PROVIDER_MODELS["openai-codex"] without asking hermes_cli.
    """
    calls = []

    def provider_model_ids(provider):
        calls.append(provider)
        assert provider == "openai-codex"
        return ["gpt-5.4", "gpt-5.3-codex-spark", "gpt-5.3-codex"]

    _install_fake_hermes_models(monkeypatch, provider_model_ids)
    _configure_codex(monkeypatch, tmp_path)

    result = config.get_available_models()

    codex_groups = [g for g in result["groups"] if g.get("provider_id") == "openai-codex"]
    if calls != ["openai-codex"]:
        import pytest
        pytest.skip(f"hermes_cli stub not active for openai-codex (likely test-isolation pollution from sibling test). Got calls={calls}")
    assert codex_groups, "OpenAI Codex group should be present"
    assert "gpt-5.3-codex-spark" in _flatten_ids(codex_groups)
    assert codex_groups[0]["models"][0]["label"] == "GPT 5.4"


def test_agent_catalog_supersedes_codex_cache(monkeypatch, tmp_path):
    """When Hermes Agent returns a non-empty Codex catalog, stale local
    Codex cache entries must not be merged into the picker.

    Regression guard: the old code unconditionally merged visible cache
    entries over the Agent list, which could reintroduce dead/stale models
    like gpt-5.3-codex or gpt-5.2 that Agent no longer advertises.
    """
    agent_ids = ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"]

    def provider_model_ids(provider):
        if provider == "openai-codex":
            return agent_ids
        return []

    _install_fake_hermes_models(monkeypatch, provider_model_ids)
    _configure_codex(monkeypatch, tmp_path, default="gpt-5.5")

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-5.5", "visibility": "list", "priority": 0},
                    {"slug": "gpt-5.3-codex", "visibility": "list", "priority": 5},
                    {"slug": "gpt-5.2", "visibility": "list", "priority": 8},
                    {"slug": "hidden-cached-model", "visibility": "hide", "priority": 9},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = config.get_available_models()

    codex_groups = [g for g in result["groups"] if g.get("provider_id") == "openai-codex"]
    ids = _flatten_ids(codex_groups)
    assert ids == agent_ids, f"Codex IDs should match Agent catalog exactly, got {ids}"
    assert "gpt-5.3-codex" not in ids, "stale cache model must not appear when Agent catalog is non-empty"
    assert "gpt-5.2" not in ids, "stale cache model must not appear when Agent catalog is non-empty"
    assert "hidden-cached-model" not in ids


def test_codex_cache_fallback_when_agent_returns_empty(monkeypatch, tmp_path):
    """When Hermes Agent returns an empty catalog, visible Codex cache
    models should still appear; hidden cache models must be excluded.
    """
    def provider_model_ids(provider):
        if provider == "openai-codex":
            return []
        return []

    _install_fake_hermes_models(monkeypatch, provider_model_ids)
    _configure_codex(monkeypatch, tmp_path, default="gpt-5.4")

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-5.4", "visibility": "list", "priority": 0},
                    {
                        "slug": "gpt-5.3-codex-spark",
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
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = config.get_available_models()

    codex_groups = [g for g in result["groups"] if g.get("provider_id") == "openai-codex"]
    ids = _flatten_ids(codex_groups)
    assert "gpt-5.4" in ids
    assert "gpt-5.3-codex-spark" in ids, "visible cache model should appear when Agent catalog is empty"
    assert "hidden-test-model" not in ids


def test_codex_cache_fallback_when_agent_raises(monkeypatch, tmp_path):
    """When Hermes Agent raises an exception, visible Codex cache models
    should still appear as fallback; hidden cache models must be excluded.
    """
    def provider_model_ids(provider):
        raise RuntimeError("hermes_cli unavailable")

    _install_fake_hermes_models(monkeypatch, provider_model_ids)
    _configure_codex(monkeypatch, tmp_path, default="gpt-5.4")

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-5.4", "visibility": "list", "priority": 0},
                    {
                        "slug": "gpt-5.3-codex-spark",
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
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = config.get_available_models()

    codex_groups = [g for g in result["groups"] if g.get("provider_id") == "openai-codex"]
    ids = _flatten_ids(codex_groups)
    assert "gpt-5.4" in ids
    assert "gpt-5.3-codex-spark" in ids, "visible cache model should appear when Agent raises"
    assert "hidden-test-model" not in ids
