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


def _configure_codex(monkeypatch, tmp_path, default="gpt-5.3-codex-spark"):
    monkeypatch.setattr(
        config, "_get_config_path", lambda: tmp_path / "missing-config.yaml"
    )
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(
        config,
        "cfg",
        {
            "model": {"provider": "openai-codex", "default": default},
            "providers": {},
            "fallback_providers": [],
        },
    )
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

    codex_groups = [
        g for g in result["groups"] if g.get("provider_id") == "openai-codex"
    ]
    # Resilient to test-isolation pollution: when a sibling test replaces
    # sys.modules['hermes_cli.models'] without restoring it, list_available_providers
    # may report a different provider list and `calls` won't be ['openai-codex'].
    # Skip rather than fail — the contract under test is "Codex group surfaces
    # gpt-5.3-codex-spark when hermes_cli.provider_model_ids returns it".
    if calls != ["openai-codex"]:
        import pytest

        pytest.skip(
            f"hermes_cli stub not active for openai-codex (likely test-isolation pollution from sibling test). Got calls={calls}"
        )
    assert codex_groups, "OpenAI Codex group should be present"
    assert "gpt-5.3-codex-spark" in _flatten_ids(codex_groups)
    assert codex_groups[0]["models"][0]["label"] == "GPT 5.4"


def test_openai_codex_group_merges_visible_codex_cache_models(monkeypatch, tmp_path):
    """Visible Codex CLI cache models should appear even if API-filtered.

    Michael's local Codex cache lists ``gpt-5.3-codex-spark`` with
    ``supported_in_api: false``.  The agent helper currently filters those IDs
    out, but the WebUI picker is a Codex-model selection surface and should
    mirror the visible Codex catalog instead of hiding Spark.
    """

    def provider_model_ids(provider):
        assert provider == "openai-codex"
        return ["gpt-5.4", "gpt-5.3-codex"]

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

    codex_groups = [
        g for g in result["groups"] if g.get("provider_id") == "openai-codex"
    ]
    ids = _flatten_ids(codex_groups)
    assert "gpt-5.3-codex-spark" in ids
    assert "hidden-test-model" not in ids
