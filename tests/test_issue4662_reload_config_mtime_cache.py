"""Phase 2 (#4662): reload_config() must not re-run yaml.safe_load on the hot path
when config.yaml is unchanged. It now routes its parse through the mtime-keyed
_load_yaml_config_file cache (#4652). Behavior-preserving: the process-global
_cfg_cache is still pinned to the unscoped process-env expansion (#798 TLS), env
expansion still runs per call, and an mtime change still busts the cache.
"""
import os
import time

import yaml as _yaml


def test_reload_config_uses_mtime_cache(tmp_path, monkeypatch):
    import api.config as cfg

    config_path = tmp_path / "config.yaml"
    config_path.write_text("providers:\n  openai:\n    models: [gpt-5.5]\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "_get_config_path", lambda: config_path)

    parse_calls = {"n": 0}
    real_safe_load = _yaml.safe_load

    def _counting_safe_load(s):
        parse_calls["n"] += 1
        return real_safe_load(s)

    monkeypatch.setattr(_yaml, "safe_load", _counting_safe_load)
    # Clear the shared file cache so the first reload is a genuine miss.
    with cfg._yaml_file_cache_lock:
        cfg._yaml_file_cache.clear()

    cfg.reload_config()
    first = parse_calls["n"]
    cfg.reload_config()          # same file, unchanged mtime -> must hit cache
    second = parse_calls["n"]

    assert first >= 1, "first reload should parse the file at least once"
    assert second == first, f"unchanged config.yaml was reparsed (parse went {first}->{second})"


def test_reload_config_busts_on_mtime_change(tmp_path, monkeypatch):
    import api.config as cfg

    config_path = tmp_path / "config.yaml"
    config_path.write_text("providers: {}\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "_get_config_path", lambda: config_path)
    with cfg._yaml_file_cache_lock:
        cfg._yaml_file_cache.clear()

    cfg.reload_config()
    assert (cfg.get_config().get("providers") or {}) == {}

    # Edit + bump mtime; the next reload must pick up the change, not the cache.
    time.sleep(0.01)
    config_path.write_text("providers:\n  openai: {}\n", encoding="utf-8")
    os.utime(config_path, None)
    cfg.reload_config()
    assert "openai" in (cfg.get_config().get("providers") or {}), "mtime change not picked up"


def test_reload_config_expands_env_vars(tmp_path, monkeypatch):
    """The pinned process-env expansion must still run: a ${VAR} in config.yaml
    resolves against os.environ after reload_config (the #798 invariant)."""
    import api.config as cfg

    monkeypatch.setenv("HERMES_TEST_RELOAD_TOKEN", "expanded-value-xyz")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "providers:\n  openai:\n    api_key: ${HERMES_TEST_RELOAD_TOKEN}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg, "_get_config_path", lambda: config_path)
    with cfg._yaml_file_cache_lock:
        cfg._yaml_file_cache.clear()

    cfg.reload_config()
    key = ((cfg.get_config().get("providers") or {}).get("openai") or {}).get("api_key")
    assert key == "expanded-value-xyz", f"env var not expanded after reload: {key!r}"


def test_reload_config_reexpands_env_when_mtime_unchanged(tmp_path, monkeypatch):
    """#798 hardening (Opus gate): the mtime cache must store the RAW, un-expanded
    YAML — not the expanded result — so that a ${VAR} re-expands against the CURRENT
    os.environ on every reload even when config.yaml's mtime is unchanged. If the
    cache stored the expanded value, a profile/env change with an unchanged file
    would serve a stale expansion (cross-profile credential bleed)."""
    import api.config as cfg

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "providers:\n  openai:\n    api_key: ${HERMES_TEST_REEXPAND_TOKEN}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg, "_get_config_path", lambda: config_path)
    with cfg._yaml_file_cache_lock:
        cfg._yaml_file_cache.clear()

    # First reload under env value A.
    monkeypatch.setenv("HERMES_TEST_REEXPAND_TOKEN", "value-A")
    cfg.reload_config()
    key_a = ((cfg.get_config().get("providers") or {}).get("openai") or {}).get("api_key")
    assert key_a == "value-A", f"first expansion wrong: {key_a!r}"

    # Change ONLY the env var — the file (and its mtime) is untouched, so the YAML
    # parse cache will hit. The expansion must still pick up the new env value.
    monkeypatch.setenv("HERMES_TEST_REEXPAND_TOKEN", "value-B")
    cfg.reload_config()
    key_b = ((cfg.get_config().get("providers") or {}).get("openai") or {}).get("api_key")
    assert key_b == "value-B", (
        f"env change with unchanged mtime did not re-expand (got {key_b!r}); "
        "the mtime cache is storing the EXPANDED value instead of raw YAML "
        "-> cross-profile credential-bleed risk (#798)"
    )


def test_reload_config_does_not_cross_serve_between_profile_paths(tmp_path, monkeypatch):
    """#798 hardening (Opus gate): the YAML parse cache is keyed on the config PATH
    (plus mtime+size), so two different profiles' config.yaml files must never serve
    each other's parsed content — even within the same process. A path-blind cache
    would leak one profile's providers/keys into another."""
    import api.config as cfg

    path_a = tmp_path / "profile_a" / "config.yaml"
    path_b = tmp_path / "profile_b" / "config.yaml"
    path_a.parent.mkdir(parents=True, exist_ok=True)
    path_b.parent.mkdir(parents=True, exist_ok=True)
    path_a.write_text("providers:\n  openai:\n    models: [model-a]\n", encoding="utf-8")
    path_b.write_text("providers:\n  anthropic:\n    models: [model-b]\n", encoding="utf-8")

    with cfg._yaml_file_cache_lock:
        cfg._yaml_file_cache.clear()

    monkeypatch.setattr(cfg, "_get_config_path", lambda: path_a)
    cfg.reload_config()
    providers_a = cfg.get_config().get("providers") or {}
    assert "openai" in providers_a and "anthropic" not in providers_a, (
        f"profile A served the wrong config: {sorted(providers_a)}"
    )

    monkeypatch.setattr(cfg, "_get_config_path", lambda: path_b)
    cfg.reload_config()
    providers_b = cfg.get_config().get("providers") or {}
    assert "anthropic" in providers_b and "openai" not in providers_b, (
        f"profile B was cross-served profile A's config (cache not path-keyed): "
        f"{sorted(providers_b)}"
    )


def test_reload_config_empty_dict_config_does_not_spin(tmp_path, monkeypatch):
    """An empty ``{}`` config must still stamp _cfg_mtime, or get_config()'s
    `current_mtime != _cfg_mtime` stale check fires forever and re-enters
    reload_config() under _cfg_lock on every call. The cache-update is correctly
    skipped for {} (no-op), but the mtime stamp must not be. (Opus gate finding —
    a {} config is reachable on the profile-switch hot path via a freshly
    created/reset profile, and this also fixes the pre-existing empty/None-config
    spin on master.)
    """
    import api.config as cfg

    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "_get_config_path", lambda: config_path)
    with cfg._yaml_file_cache_lock:
        cfg._yaml_file_cache.clear()

    cfg.reload_config()
    # _cfg_mtime must equal the file's real mtime, not 0.0.
    assert cfg._cfg_mtime == config_path.stat().st_mtime, (
        f"_cfg_mtime not stamped for empty-dict config (got {cfg._cfg_mtime!r}); "
        "get_config() would spin reload_config() forever"
    )

    # And get_config() must NOT re-enter reload_config() on subsequent calls.
    reloads = {"n": 0}
    real_reload = cfg.reload_config

    def _counting_reload():
        reloads["n"] += 1
        return real_reload()

    monkeypatch.setattr(cfg, "reload_config", _counting_reload)
    cfg.get_config()
    cfg.get_config()
    cfg.get_config()
    assert reloads["n"] == 0, f"get_config() re-entered reload_config() {reloads['n']}x on a stable {{}} config (spin)"

