"""Regression coverage for #5220 config reload stampede collapse."""

import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor


def test_stale_readers_collapse_to_single_reload_when_config_is_hot_reload(tmp_path, monkeypatch):
    import api.config as config

    config_path = tmp_path / "config.yaml"
    config_path.write_text("marker: stale\n", encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    with config._yaml_file_cache_lock:
        config._yaml_file_cache.clear()

    config.reload_config()
    old_mtime = config._cfg_mtime
    config_path.write_text("marker: refreshed\n", encoding="utf-8")
    # Keep cache state stale for every thread entering the read path.
    config._cfg_mtime = old_mtime - 1.0

    calls = {"n": 0}
    calls_lock = threading.Lock()
    real_refresh = config._refresh_config_cache

    def _counted_refresh(path=None):
        with calls_lock:
            calls["n"] += 1
            call_no = calls["n"]
        if call_no == 1:
            time.sleep(0.1)
        return real_refresh(path)

    def _unexpected_reload():
        raise AssertionError(
            "stale model readers should route through reload_config_if_stale() instead of forced reload_config()"
        )

    monkeypatch.setattr(config, "_refresh_config_cache", _counted_refresh)
    monkeypatch.setattr(config, "reload_config", _unexpected_reload)

    def _get():
        cfg = config.get_config()
        return cfg.get("marker")

    with ThreadPoolExecutor(max_workers=6) as executor:
        values = [value for value in executor.map(lambda _idx: _get(), range(6))]

    assert all(value == "refreshed" for value in values), (
        f"stale readers should observe refreshed config, got {values!r}"
    )
    assert calls["n"] == 1, (
        f"expected one refresh for concurrent stale readers, got {calls['n']} "
        "(base would re-enter refresh work per stale reader)"
    )


def test_explicit_reload_config_forces_disk_refresh(tmp_path, monkeypatch):
    import api.config as config

    config_path = tmp_path / "config.yaml"
    config_path.write_text("marker: cold\n", encoding="utf-8")
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    with config._yaml_file_cache_lock:
        config._yaml_file_cache.clear()

    config.reload_config()
    assert config.get_config().get("marker") == "cold"

    calls = {"n": 0}
    calls_lock = threading.Lock()
    real_load = config._load_yaml_config_file_raw

    def _counted_load(path):
        with calls_lock:
            calls["n"] += 1
        return real_load(path)

    monkeypatch.setattr(config, "_load_yaml_config_file_raw", _counted_load)
    config_path.write_text("marker: hot\n", encoding="utf-8")

    config.reload_config()
    assert config.get_config().get("marker") == "hot"
    assert calls["n"] == 1, (
        "explicit reload_config() should refresh from disk even when caller requests it"
    )


def test_stale_model_readers_collapse_to_single_reload_when_models_are_requested(tmp_path, monkeypatch):
    import api.config as config

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "model:\n  provider: openai\n  default: cold-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    with config._yaml_file_cache_lock:
        config._yaml_file_cache.clear()
    config.invalidate_models_cache()

    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models._PROVIDER_ALIASES = {}
    fake_models.list_available_providers = lambda: []
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda provider_id: {"logged_in": False, "key_source": ""}
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.setattr(config, "_load_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(config, "_load_stale_models_cache_from_disk", lambda: None)
    monkeypatch.setattr(config, "_save_models_cache_to_disk", lambda _data, **_kwargs: None)
    monkeypatch.setattr(config, "_models_cache_source_fingerprint", lambda: {"config": "test"})

    config.reload_config()
    old_mtime = config._cfg_mtime
    config_path.write_text(
        "model:\n  provider: openai\n  default: refreshed-model\n",
        encoding="utf-8",
    )
    config._cfg_mtime = old_mtime - 1.0
    config.invalidate_models_cache()

    # Hermeticity guard (#7724 shard flake). ``_cfg_has_in_memory_overrides()``
    # returns True whenever the module-level ``cfg`` no longer aliases
    # ``_cfg_cache`` — the residue a sibling test in the same shard leaves
    # behind if its monkeypatch teardown ordering goes wrong, or an unrelated
    # test reloads api.config while ``cfg`` is rebound. When that happens the
    # cold-path mtime gate at the top of get_available_models() short-circuits
    # ``reload_config_if_stale()`` (the override wins over the stale mtime),
    # leaving ``_cfg_mtime`` stale, so the in-lock ``_cfg_changed`` branch calls
    # the mocked ``reload_config()`` and this test fails for a reason unrelated
    # to the behaviour under test. Re-alias ``cfg`` on ``_cfg_cache`` (and
    # re-stamp the fingerprint to the current cache) so the gate sees a clean
    # module state and only a genuine re-entrant refresh can trip the sentinel.
    config.cfg = config._cfg_cache
    config._cfg_fingerprint = config._fingerprint_config(config._cfg_cache)

    calls = {"n": 0}
    calls_lock = threading.Lock()
    real_refresh = config._refresh_config_cache

    def _counted_refresh(path=None):
        with calls_lock:
            calls["n"] += 1
            call_no = calls["n"]
        if call_no == 1:
            time.sleep(0.1)
        return real_refresh(path)

    def _unexpected_reload():
        raise AssertionError(
            "stale model readers should route through reload_config_if_stale() instead of forced reload_config()"
        )

    # The cold-cache path in get_available_models() only routes through
    # profiles scopes to rebind TLS/env for the rebuild worker. This test
    # covers the config-reload gate, not profile scoping, so neutralize the
    # scopes: a *named* process-level profile left behind by an earlier test
    # in the same shard would otherwise drive the root-binding branch
    # (bind_root=True) and pull this thread into profile-env work that reads
    # config through reload_config().
    import api.profiles as profiles_mod
    from contextlib import contextmanager as _cm

    @_cm
    def _noop_scope(*_a, **_k):
        yield

    monkeypatch.setattr(profiles_mod, "profile_scope_for_active_request", _noop_scope)
    monkeypatch.setattr(profiles_mod, "profile_scope_for_detached_worker", _noop_scope)

    monkeypatch.setattr(config, "_refresh_config_cache", _counted_refresh)
    monkeypatch.setattr(config, "reload_config", _unexpected_reload)

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda _idx: config.get_available_models(), range(6)))

    assert all(result.get("default_model") == "refreshed-model" for result in results), (
        f"stale model readers should observe refreshed config, got {results!r}"
    )
    assert calls["n"] == 1, (
        f"expected one config refresh for concurrent model reads, got {calls['n']} "
        "(base would re-enter refresh work per stale reader)"
    )
