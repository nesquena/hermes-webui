"""Regression coverage for the /api/models rebuild-budget fallback."""

from __future__ import annotations

import copy
import json
import time

import pytest

import api.config as cfg
import api.profiles as profiles


@pytest.fixture(autouse=True)
def isolate_models_catalog_state(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model: {}\n", encoding="utf-8")
    auth_store_path = tmp_path / "auth.json"
    auth_store_path.write_text("{}", encoding="utf-8")
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text("", encoding="utf-8")

    monkeypatch.setattr(cfg, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(cfg, "_cfg_path", config_path, raising=False)
    monkeypatch.setattr(cfg, "_cfg_mtime", config_path.stat().st_mtime, raising=False)
    monkeypatch.setattr(cfg, "_cfg_has_in_memory_overrides", lambda: True)
    monkeypatch.setattr(cfg, "_get_auth_store_path", lambda: auth_store_path)
    monkeypatch.setattr(cfg, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(cfg, "_save_models_cache_to_disk", lambda *_a, **_k: None)
    monkeypatch.setattr(cfg, "_delete_models_cache_on_disk", lambda: None)
    monkeypatch.setattr(
        cfg,
        "_models_cache_source_fingerprint",
        lambda: "unit-test-fingerprint",
    )
    monkeypatch.setattr(cfg, "_available_models_cache", None, raising=False)
    monkeypatch.setattr(cfg, "_available_models_cache_ts", 0.0, raising=False)
    monkeypatch.setattr(
        cfg,
        "_available_models_cache_source_fingerprint",
        None,
        raising=False,
    )
    monkeypatch.setattr(cfg, "_cache_build_in_progress", False, raising=False)
    monkeypatch.setattr(cfg, "cfg", {}, raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(cfg.os, "getenv", lambda key, default=None: default or "")

    return {
        "auth_store_path": auth_store_path,
    }


def _configure_local_sources(
    monkeypatch,
    auth_store_path,
    *,
    env_vars: dict[str, str] | None = None,
):
    cfg.cfg = {
        "model": {
            "provider": "openai-api",
            "default": "gpt-5.5",
        },
        "fallback_providers": [
            {"provider": "anthropic", "model": "claude-sonnet-4.6"},
        ],
        "providers": {
            "openai_api": {"api_key": "sk-openai"},
            "anthropic": {"api_key": "sk-anthropic"},
        },
        "custom_providers": [
            {
                "name": "ZenMux",
                "models": ["vendor/model-a", {"id": "vendor/model-b"}],
            }
        ],
    }
    auth_store_path.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openrouter": [
                        {
                            "source": "env:openrouter_api_key",
                            "label": "manual key",
                            "key_source": "env",
                        }
                    ],
                    "copilot": [
                        {
                            "source": "gh_cli",
                            "label": "gh auth token",
                            "key_source": "gh auth token",
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    env_lookup = dict(env_vars or {})
    monkeypatch.setattr(cfg.os, "getenv", lambda key, default=None: env_lookup.get(key, default or ""))


def test_static_catalog_budget_fallback_lists_local_providers(
    monkeypatch,
    isolate_models_catalog_state,
):
    _configure_local_sources(
        monkeypatch,
        isolate_models_catalog_state["auth_store_path"],
    )
    monkeypatch.setattr(
        cfg,
        "_read_live_provider_model_ids",
        lambda _provider_id: (_ for _ in ()).throw(
            AssertionError("static timeout fallback must stay network-free")
        ),
    )

    catalog = cfg._static_models_catalog_without_live_probes()
    provider_ids = [group["provider_id"] for group in catalog["groups"]]

    assert provider_ids[0] == "openai-api"
    assert "anthropic" in provider_ids
    assert "custom:zenmux" in provider_ids
    assert "copilot" not in provider_ids
    assert not (
        len(catalog["groups"]) == 1
        and catalog["groups"][0]["provider"] == "Default"
    )
    assert any(
        badge["role"] == "fallback" and badge["provider"] == "anthropic"
        for badge in catalog["configured_model_badges"].values()
    )


def test_budget_exceeded_foreground_uses_richer_static_catalog_and_refreshes_out_of_band(
    monkeypatch,
    isolate_models_catalog_state,
):
    _configure_local_sources(
        monkeypatch,
        isolate_models_catalog_state["auth_store_path"],
    )
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05, raising=False)

    live_result = {
        "active_provider": "openrouter",
        "default_model": "openrouter/google/gemini-2.5-pro",
        "configured_model_badges": {},
        "groups": [
            {
                "provider": "OpenRouter",
                "provider_id": "openrouter",
                "models": [
                    {
                        "id": "openrouter/google/gemini-2.5-pro",
                        "label": "Gemini 2.5 Pro",
                    }
                ],
            }
        ],
        "aliases": {},
    }
    rebuild_calls = {"count": 0}

    def _slow_rebuild(_builder):
        rebuild_calls["count"] += 1
        time.sleep(0.2)
        return copy.deepcopy(live_result)

    monkeypatch.setattr(cfg, "_invoke_models_rebuild", _slow_rebuild)

    expected_fallback = cfg._static_models_catalog_without_live_probes()
    result = cfg.get_available_models()

    assert result == expected_fallback
    assert not (
        len(result["groups"]) == 1 and result["groups"][0]["provider"] == "Default"
    )

    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        if cfg._available_models_cache == live_result:
            break
        time.sleep(0.01)

    assert rebuild_calls["count"] == 1
    assert cfg._available_models_cache == live_result
    assert cfg._cache_build_in_progress is False


def test_default_group_survives_only_as_emergency_last_resort(
    monkeypatch,
    isolate_models_catalog_state,
):
    cfg.cfg = {
        "model": {
            "provider": "anthropic",
            "default": "claude-sonnet-4.6",
        }
    }
    monkeypatch.setattr(
        cfg,
        "_get_providers_cfg",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    catalog = cfg._static_models_catalog_without_live_probes()

    assert catalog["active_provider"] == "anthropic"
    assert len(catalog["groups"]) == 1
    assert catalog["groups"][0]["provider"] == "Default"
    assert catalog["groups"][0]["provider_id"] == "anthropic"
    assert catalog["groups"][0]["models"][0]["id"] == "claude-sonnet-4.6"
