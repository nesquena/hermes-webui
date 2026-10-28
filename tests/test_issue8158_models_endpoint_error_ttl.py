import time
import pytest
from api import config


def _valid_cache(groups):
    return {
        "active_provider": "openai",
        "default_model": "gpt-5.5",
        "configured_model_badges": {},
        "groups": groups,
    }


def test_has_models_endpoint_error_detection():
    assert not config._has_models_endpoint_error(None)
    assert not config._has_models_endpoint_error({})
    assert not config._has_models_endpoint_error({"groups": []})
    assert not config._has_models_endpoint_error(_valid_cache([{"provider_id": "openai", "models": []}]))
    assert config._has_models_endpoint_error(
        _valid_cache([
            {"provider_id": "openai", "models": []},
            {"provider_id": "custom:broken", "models_endpoint_error": {"kind": "network", "message": "unreachable"}},
        ])
    )


def test_memory_models_cache_uses_short_ttl_when_endpoint_error_present(monkeypatch):
    monkeypatch.setattr(config, "_models_cache_source_fingerprint", lambda: {"dummy": 1})
    now = 1000.0

    # Normal cache without endpoint errors
    config._available_models_cache = _valid_cache([{"provider_id": "openai", "models": []}])
    config._available_models_cache_ts = now
    config._available_models_cache_source_fingerprint = {"dummy": 1}

    # At now + 60s, normal cache is still valid (TTL = 86400s)
    assert config._get_fresh_memory_models_cache(now + 60.0) is not None

    # Cache with endpoint error
    config._available_models_cache = _valid_cache([
        {"provider_id": "custom:broken", "models_endpoint_error": {"kind": "network", "message": "unreachable"}},
    ])
    config._available_models_cache_ts = now
    config._available_models_cache_source_fingerprint = {"dummy": 1}

    # At now + 10s, cache is still within error TTL (30s)
    assert config._get_fresh_memory_models_cache(now + 10.0) is not None

    # At now + 35s, cache exceeds error TTL (30s) and expires
    assert config._get_fresh_memory_models_cache(now + 35.0) is None


def test_disk_models_cache_expires_when_endpoint_error_present(monkeypatch, tmp_path):
    cache_path = tmp_path / "models_cache.json"
    monkeypatch.setattr(config, "_get_models_cache_path", lambda: cache_path)
    monkeypatch.setattr(config, "_is_loadable_disk_cache", lambda c: True)

    now = time.time()
    cache_data = _valid_cache([
        {"provider_id": "custom:broken", "models_endpoint_error": {"kind": "network", "message": "unreachable"}},
    ])
    cache_data["_built_at"] = now - 35.0  # 35 seconds old

    import json
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache_data, f)

    # Built 35s ago with endpoint error -> should expire (TTL = 30s)
    assert config._load_models_cache_from_disk() is None

    # Built 10s ago with endpoint error -> should load
    cache_data["_built_at"] = now - 10.0
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache_data, f)

    loaded = config._load_models_cache_from_disk()
    assert loaded is not None
    assert config._has_models_endpoint_error(loaded)
