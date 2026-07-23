"""Regression tests for #1699: /api/models cache must track external auth/config changes.

The bug: WebUI caches /api/models for 24h in memory and on disk. When a user
runs `hermes setup` in a terminal and the Hermes auth store switches the active
provider outside WebUI, the browser can keep seeing the previous provider's
PRIMARY badge until the cache is manually cleared or expires.
"""

import json
import sys
import time
import types

import api.config as config


def _reset_memory_cache() -> None:
    with config._available_models_cache_lock:
        config._available_models_cache = None
        config._available_models_cache_ts = 0.0
        if hasattr(config, "_available_models_cache_source_fingerprint"):
            config._available_models_cache_source_fingerprint = None
        if hasattr(config, "_sync_models_cache_provenance"):
            config._sync_models_cache_provenance()
        config._cache_build_in_progress = False
        config._cache_build_cv.notify_all()


def _valid_models_cache(provider_id: str, model_id: str) -> dict:
    return {
        "active_provider": provider_id,
        "default_model": model_id,
        "default_model_has_explicit_source": True,
        "configured_model_badges": {
            model_id: {"role": "primary", "label": "Primary", "provider": provider_id}
        },
        "groups": [
            {
                "provider": config._PROVIDER_DISPLAY.get(provider_id, provider_id.title()),
                "provider_id": provider_id,
                "models": [{"id": model_id, "label": model_id}],
            }
        ],
    }


def _write_auth_store(hermes_home, provider_id: str) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(
        json.dumps({"active_provider": provider_id, "credential_pool": {}}),
        encoding="utf-8",
    )


def _catalog_model_ids(data: dict) -> set[str]:
    ids: set[str] = set()
    for group in data.get("groups", []):
        for bucket in ("models", "extra_models"):
            for model in group.get(bucket, []) or []:
                model_id = str(model.get("id") or "").strip()
                if model_id:
                    ids.add(model_id)
    return ids


def _primary_badge_for(data: dict, model_id: str, provider: str) -> dict | None:
    badges = data.get("configured_model_badges", {}) or {}
    for key in (model_id, f"{provider}/{model_id}", f"@{provider}:{model_id}"):
        badge = badges.get(key)
        if badge:
            return badge
    return None


def _configure_isolated_sources(
    tmp_path,
    monkeypatch,
    provider_id: str,
    *,
    config_text: str = "model:\n  default: glm-5.1\n",
) -> None:
    hermes_home = tmp_path / "hermes-home"
    state_dir = tmp_path / "state"
    cache_path = state_dir / "models_cache.json"
    state_dir.mkdir(parents=True, exist_ok=True)

    hermes_home.mkdir(parents=True, exist_ok=True)
    config_path = hermes_home / "config.yaml"
    # Leave model.provider unset in the default fixture so get_available_models()
    # must honor the auth store's active_provider fallback, matching CLI
    # setup/auth-store drift. Callers can pass a provider-only config when the
    # default-model env should be the authority under test.
    config_path.write_text(config_text, encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(config_path))

    import api.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(config, "_models_cache_path", cache_path)

    # Keep the test hermetic without requiring hermes-agent to be installed in
    # CI: inject the tiny hermes_cli surface get_available_models() imports.
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models._PROVIDER_ALIASES = {}
    fake_models.list_available_providers = lambda: []
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda provider_id: {
        "logged_in": False,
        "key_source": "",
    }
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)

    _write_auth_store(hermes_home, provider_id)
    config.reload_config()
    _reset_memory_cache()


def test_memory_models_cache_invalidates_when_auth_store_active_provider_changes(
    tmp_path, monkeypatch
):
    _configure_isolated_sources(tmp_path, monkeypatch, "opencode-go")

    stale_openrouter = _valid_models_cache("openrouter", "minimax-m2.7")
    with config._available_models_cache_lock:
        config._available_models_cache = stale_openrouter
        config._available_models_cache_ts = time.monotonic()
        if hasattr(config, "_available_models_cache_source_fingerprint"):
            # Simulate a cache populated before the external CLI auth-store write.
            config._available_models_cache_source_fingerprint = {
                "auth_json": {"path": "old-auth.json", "mtime_ns": 1, "size": 10},
                "config_yaml": {"path": "old-config.yaml", "mtime_ns": 1, "size": 10},
            }

    result = config.get_available_models()

    assert result["active_provider"] == "opencode-go"
    assert not any(group.get("provider_id") == "openrouter" for group in result["groups"])
    assert any(group.get("provider_id") == "opencode-go" for group in result["groups"])


def test_disk_models_cache_invalidates_when_auth_store_active_provider_changes(
    tmp_path, monkeypatch
):
    _configure_isolated_sources(tmp_path, monkeypatch, "openrouter")
    stale_openrouter = _valid_models_cache("openrouter", "minimax-m2.7")
    config._save_models_cache_to_disk(stale_openrouter)
    assert config._models_cache_path.exists()

    # External terminal `hermes setup` changes auth.json, not WebUI's in-process cache.
    hermes_home = config._models_cache_path.parent.parent / "hermes-home"
    _write_auth_store(hermes_home, "opencode-go")
    _reset_memory_cache()

    result = config.get_available_models()

    assert result["active_provider"] == "opencode-go"
    assert not any(group.get("provider_id") == "openrouter" for group in result["groups"])
    assert any(group.get("provider_id") == "opencode-go" for group in result["groups"])


def test_disk_models_cache_still_loads_when_auth_and_config_sources_are_unchanged(
    tmp_path, monkeypatch
):
    _configure_isolated_sources(tmp_path, monkeypatch, "opencode-go")
    fresh_opencode = _valid_models_cache("opencode-go", "glm-5.1")
    config._save_models_cache_to_disk(fresh_opencode)
    _reset_memory_cache()

    result = config.get_available_models()

    # The disk-cache hit reconstructs `aliases` from current config (the save
    # path doesn't persist aliases); no config aliases here, so it's {}.
    assert result == {**fresh_opencode, "aliases": {}}


def test_memory_models_cache_invalidates_when_static_catalog_changes(tmp_path, monkeypatch):
    _configure_isolated_sources(tmp_path, monkeypatch, "opencode-go")
    stale_opencode = _valid_models_cache("opencode-go", "glm-5.1")
    with config._available_models_cache_lock:
        config._available_models_cache = stale_opencode
        config._available_models_cache_ts = time.monotonic()
        config._available_models_cache_source_fingerprint = config._models_cache_source_fingerprint()

    updated_models = list(config._PROVIDER_MODELS["opencode-go"])
    updated_models.append({"id": "new-catalog-model", "label": "New Catalog Model"})
    monkeypatch.setitem(config._PROVIDER_MODELS, "opencode-go", updated_models)

    result = config.get_available_models()

    opencode_group = next(g for g in result["groups"] if g.get("provider_id") == "opencode-go")
    assert any(m.get("id") == "new-catalog-model" for m in opencode_group["models"])


def test_disk_models_cache_invalidates_when_static_catalog_changes(tmp_path, monkeypatch):
    _configure_isolated_sources(tmp_path, monkeypatch, "opencode-go")
    stale_opencode = _valid_models_cache("opencode-go", "glm-5.1")
    config._save_models_cache_to_disk(stale_opencode)
    assert config._models_cache_path.exists()

    updated_models = list(config._PROVIDER_MODELS["opencode-go"])
    updated_models.append({"id": "new-disk-catalog-model", "label": "New Disk Catalog Model"})
    monkeypatch.setitem(config._PROVIDER_MODELS, "opencode-go", updated_models)
    _reset_memory_cache()

    result = config.get_available_models()

    assert result != stale_opencode
    opencode_group = next(g for g in result["groups"] if g.get("provider_id") == "opencode-go")
    assert any(m.get("id") == "new-disk-catalog-model" for m in opencode_group["models"])


def test_disk_models_cache_invalidates_when_webui_default_model_env_changes(
    tmp_path,
    monkeypatch,
):
    provider_id = "opencode-go"
    model_a = "provider/model-a"
    model_b = "provider/model-b"
    _configure_isolated_sources(
        tmp_path,
        monkeypatch,
        provider_id,
        config_text=f"model:\n  provider: {provider_id}\n",
    )
    monkeypatch.setattr(config, "DEFAULT_MODEL", model_a)
    cached_a = _valid_models_cache(provider_id, model_a)
    config._save_models_cache_to_disk(cached_a)
    assert config._models_cache_path.exists()

    monkeypatch.setattr(config, "DEFAULT_MODEL", model_b)
    _reset_memory_cache()

    assert config._load_models_cache_from_disk() is None

    result = config.get_available_models()

    assert result["default_model"] == model_b
    assert result["default_model_has_explicit_source"] is True
    assert model_b in _catalog_model_ids(result) or f"@{provider_id}:{model_b}" in _catalog_model_ids(result)
    assert _primary_badge_for(result, model_b, provider_id) == {
        "role": "primary",
        "label": "Primary",
        "provider": provider_id,
    }


def test_memory_models_cache_invalidates_when_model_env_override_changes(
    tmp_path,
    monkeypatch,
):
    provider_id = "opencode-go"
    model_a = "env/model-a"
    model_b = "env/model-b"
    _configure_isolated_sources(
        tmp_path,
        monkeypatch,
        provider_id,
        config_text=f"model:\n  provider: {provider_id}\n",
    )
    monkeypatch.setattr(config, "DEFAULT_MODEL", "webui/default")
    monkeypatch.setenv("HERMES_MODEL", model_a)
    cached_a = _valid_models_cache(provider_id, model_a)
    with config._available_models_cache_lock:
        config._available_models_cache = cached_a
        config._available_models_cache_ts = time.monotonic()
        config._available_models_cache_source_fingerprint = (
            config._models_cache_source_fingerprint()
        )
        config._sync_models_cache_provenance()

    monkeypatch.setenv("HERMES_MODEL", model_b)

    result = config.get_available_models()

    assert result["default_model"] == model_b
    assert result["default_model_has_explicit_source"] is True
    assert not any(
        str(model.get("id") or "") == model_a
        for group in result.get("groups", [])
        for bucket in ("models", "extra_models")
        for model in group.get(bucket, []) or []
    )
    assert model_b in _catalog_model_ids(result) or f"@{provider_id}:{model_b}" in _catalog_model_ids(result)
    assert _primary_badge_for(result, model_b, provider_id) == {
        "role": "primary",
        "label": "Primary",
        "provider": provider_id,
    }
