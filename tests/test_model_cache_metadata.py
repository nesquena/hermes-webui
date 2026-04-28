"""Regression tests for /api/models disk cache metadata."""

import json

import api.config as config


def _reset_memory_cache() -> None:
    with config._available_models_cache_lock:
        config._available_models_cache = None
        config._available_models_cache_ts = 0.0
        config._cache_build_in_progress = False
        config._cache_build_cv.notify_all()


def test_save_models_cache_to_disk_preserves_response_metadata(tmp_path, monkeypatch):
    cache_path = tmp_path / "models_cache.json"
    monkeypatch.setattr(config, "_models_cache_path", cache_path)

    payload = {
        "active_provider": "openai",
        "default_model": "gpt-5.4-mini",
        "groups": [
            {
                "provider": "OpenAI",
                "provider_id": "openai",
                "models": [{"id": "gpt-5.4-mini", "label": "GPT 5.4 Mini"}],
            }
        ],
    }

    config._save_models_cache_to_disk(payload)

    assert json.loads(cache_path.read_text(encoding="utf-8")) == payload
    assert config._load_models_cache_from_disk() == payload


def test_load_models_cache_from_disk_rejects_legacy_groups_only_cache(tmp_path, monkeypatch):
    cache_path = tmp_path / "models_cache.json"
    monkeypatch.setattr(config, "_models_cache_path", cache_path)
    cache_path.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "provider": "Legacy",
                        "provider_id": "legacy",
                        "models": [{"id": "legacy-model", "label": "Legacy Model"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert config._load_models_cache_from_disk() is None


def test_get_available_models_ignores_legacy_disk_cache_and_rebuilds(
    tmp_path,
    monkeypatch,
):
    cache_path = tmp_path / "models_cache.json"
    monkeypatch.setattr(config, "_models_cache_path", cache_path)
    cache_path.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "provider": "Legacy",
                        "provider_id": "legacy",
                        "models": [{"id": "legacy-model", "label": "Legacy Model"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    _reset_memory_cache()

    saved_mtime = config._cfg_mtime
    try:
        try:
            config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
        except OSError:
            config._cfg_mtime = 0.0

        result = config.get_available_models()
    finally:
        config._cfg_mtime = saved_mtime
        _reset_memory_cache()

    assert "active_provider" in result
    assert "default_model" in result
    assert "groups" in result
    assert not any(group.get("provider") == "Legacy" for group in result["groups"])

    written = json.loads(cache_path.read_text(encoding="utf-8"))
    assert "active_provider" in written
    assert "default_model" in written
    assert "groups" in written
