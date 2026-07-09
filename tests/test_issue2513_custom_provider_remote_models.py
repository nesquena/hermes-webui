from api import config


def test_custom_provider_model_field_does_not_block_remote_catalog(monkeypatch, tmp_path):
    """custom_providers[].model is sticky metadata, not the whole picker catalog."""

    calls = []

    def fake_get(url, headers, *, timeout):
        calls.append(url)
        if "alpha.example" in url:
            return {
                "data": [
                    {"id": "alpha/sticky", "name": "Alpha Sticky"},
                    {"id": "alpha/remote", "name": "Alpha Remote"},
                ]
            }
        if "beta.example" in url:
            return {
                "models": [
                    {"id": "beta/remote", "name": "Beta Remote"},
                ]
            }
        raise AssertionError(f"unexpected urlopen: {url}")

    monkeypatch.setattr(config, "_credentialed_json_get_no_redirect", fake_get)
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: tmp_path / "auth.json")

    old_cfg = config.cfg
    old_mtime = config._cfg_mtime
    old_cache = config._available_models_cache
    old_cache_ts = config._available_models_cache_ts
    old_cache_fp = config._available_models_cache_source_fingerprint
    try:
        config.cfg = {
            "model": {"provider": "openai-codex", "default": "gpt-5.5"},
            "providers": {},
            "fallback_providers": [],
            "custom_providers": [
                {
                    "name": "Alpha Proxy",
                    "base_url": "https://alpha.example/v1",
                    "api_key": "alpha-key",
                    "model": "alpha/sticky",
                },
                {
                    "name": "Beta Proxy",
                    "base_url": "https://beta.example/v1",
                    "api_key": "beta-key",
                    "model": "beta/sticky",
                },
            ],
        }
        config._cfg_mtime = 0.0
        config._available_models_cache = None
        config._available_models_cache_ts = 0.0
        config._available_models_cache_source_fingerprint = None

        data = config.get_available_models()
    finally:
        config.cfg = old_cfg
        config._cfg_mtime = old_mtime
        config._available_models_cache = old_cache
        config._available_models_cache_ts = old_cache_ts
        config._available_models_cache_source_fingerprint = old_cache_fp

    groups = {group["provider_id"]: group for group in data["groups"]}
    assert "custom:alpha-proxy" in groups
    assert "custom:beta-proxy" in groups

    alpha_ids = {model["id"] for model in groups["custom:alpha-proxy"]["models"]}
    beta_ids = {model["id"] for model in groups["custom:beta-proxy"]["models"]}

    assert "@custom:alpha-proxy:alpha/remote" in alpha_ids
    assert "@custom:alpha-proxy:alpha/sticky" in alpha_ids
    assert "@custom:beta-proxy:beta/remote" in beta_ids
    assert "@custom:beta-proxy:beta/sticky" in beta_ids
    assert any("alpha.example/v1/models" in url for url in calls)
    assert any("beta.example/v1/models" in url for url in calls)
