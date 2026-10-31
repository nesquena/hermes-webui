"""Named-profile config reads expand raw YAML using the file owner's env."""

import copy
import threading
from types import SimpleNamespace

import pytest
import yaml


@pytest.fixture
def profile_config_harness(tmp_path, monkeypatch):
    from api import config, profiles

    default_home = tmp_path / "default"
    work_home = tmp_path / "profiles" / "work"
    for home in (default_home, work_home):
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    state = SimpleNamespace(home=default_home, path=None)
    resolve_config_path = config._get_config_path
    saved = (
        copy.deepcopy(config._cfg_cache), config._cfg_path, config._cfg_mtime,
        config._cfg_fingerprint, config.cfg is not config._cfg_cache,
        copy.deepcopy(config.cfg),
    )
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: state.home)
    monkeypatch.setattr(config, "_get_config_path", lambda: state.path or state.home / "config.yaml")
    config.reload_config()
    yield SimpleNamespace(config=config, profiles=profiles, state=state,
                          default_home=default_home, work_home=work_home,
                          resolve_config_path=resolve_config_path)
    with config._cfg_lock:
        cache, path, mtime, fingerprint, rebound, cfg = saved
        config._cfg_cache.clear()
        config._cfg_cache.update(cache)
        config._cfg_path, config._cfg_mtime, config._cfg_fingerprint = path, mtime, fingerprint
        config.cfg = cfg if rebound else config._cfg_cache


def _raw_profile(harness, *, env="PROFILE_TOKEN=work-value\n"):
    (harness.work_home / "config.yaml").write_text(
        yaml.safe_dump({"providers": {"work": {"api_key": "${PROFILE_TOKEN}"}},
                        "nested": ["${PROFILE_TOKEN}", {"missing": "${MISSING_PROFILE_VALUE}"}]}),
        encoding="utf-8",
    )
    (harness.work_home / ".env").write_text(env, encoding="utf-8")
    harness.state.home = harness.work_home


def test_profile_snapshot_expands_env_from_request_profile_raw_yaml(profile_config_harness, monkeypatch):
    h = profile_config_harness
    _raw_profile(h)
    monkeypatch.setenv("PROFILE_TOKEN", "process-value")
    assert h.config.get_config_snapshot()["providers"]["work"]["api_key"] == "work-value"


def test_named_profile_snapshot_does_not_expand_missing_env_from_process(profile_config_harness, monkeypatch):
    h = profile_config_harness
    _raw_profile(h, env="")
    monkeypatch.setenv("PROFILE_TOKEN", "process-value")
    assert h.config.get_config_snapshot()["providers"]["work"]["api_key"] == "${PROFILE_TOKEN}"
    assert h.config.get_config_for_profile_home(h.work_home)["providers"]["work"]["api_key"] == "${PROFILE_TOKEN}"


def test_nested_expansion_ignores_other_thread_scope_and_restores_it(profile_config_harness, monkeypatch):
    h = profile_config_harness
    _raw_profile(h)
    monkeypatch.setenv("MISSING_PROFILE_VALUE", "process-missing")
    foreign = {"PROFILE_TOKEN": "foreign-value"}
    monkeypatch.setattr(h.config._thread_ctx, "env", foreign, raising=False)
    monkeypatch.setattr(h.config._thread_ctx, "block_process_env_fallback", False, raising=False)
    snapshot = h.config.get_config_snapshot()
    assert snapshot["nested"] == ["work-value", {"missing": "${MISSING_PROFILE_VALUE}"}]
    assert h.config._thread_ctx.env is foreign
    assert h.config._thread_ctx.block_process_env_fallback is False


def test_profile_env_changes_refresh_detached_snapshot_without_global_cache_mutation(profile_config_harness, monkeypatch):
    h = profile_config_harness
    _raw_profile(h)
    monkeypatch.setenv("PROFILE_TOKEN", "process-value")
    first = h.config.get_config_snapshot()
    cache = copy.deepcopy(h.config._cfg_cache)
    (h.work_home / ".env").write_text("PROFILE_TOKEN=next-value\n", encoding="utf-8")
    second = h.config.get_config_snapshot()
    assert first["providers"]["work"]["api_key"] == "work-value"
    assert second["providers"]["work"]["api_key"] == "next-value"
    second["providers"]["work"]["api_key"] = "caller-edit"
    assert h.config._cfg_cache == cache
    assert h.config.get_config_snapshot()["providers"]["work"]["api_key"] == "next-value"


@pytest.mark.parametrize("rebound", [False, True])
def test_named_profile_in_memory_overrides_remain_authoritative(profile_config_harness, monkeypatch, rebound):
    h = profile_config_harness
    _raw_profile(h)
    h.config.get_config_snapshot()
    override = {"model": {"default": "override-model"}}
    if rebound:
        monkeypatch.setattr(h.config, "cfg", override)
    else:
        h.config._cfg_cache.clear()
        h.config._cfg_cache.update(override)
    snapshot = h.config.get_config_snapshot()
    assert snapshot == override
    snapshot["model"]["default"] = "caller-edit"
    assert h.config.cfg["model"]["default"] == "override-model"


def test_named_profile_explicit_read_does_not_borrow_ambient_profile_env(profile_config_harness, monkeypatch):
    h = profile_config_harness
    _raw_profile(h)
    h.state.home = h.default_home
    monkeypatch.setenv("PROFILE_TOKEN", "process-value")
    assert h.config.get_config_for_profile_home(h.work_home)["providers"]["work"]["api_key"] == "work-value"
    assert h.config._cfg_path == h.default_home / "config.yaml"


def test_config_override_filename_in_owned_home_remains_authoritative(profile_config_harness, monkeypatch):
    h = profile_config_harness
    _raw_profile(h)
    h.state.path = h.work_home / "operator.yaml"
    h.state.path.write_text("operator: ${PROFILE_TOKEN}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(h.state.path))
    monkeypatch.setattr(h.config, "_get_config_path", h.resolve_config_path)
    snapshot = h.config.get_config_for_profile_home(h.work_home)
    assert snapshot["operator"] == "work-value"
    assert "providers" not in snapshot


def test_external_config_override_keeps_existing_process_env_policy(profile_config_harness, monkeypatch, tmp_path):
    h = profile_config_harness
    _raw_profile(h)
    operator_path = tmp_path / "operator.yaml"
    operator_path.write_text("operator: ${PROFILE_TOKEN}\n", encoding="utf-8")
    monkeypatch.setenv("PROFILE_TOKEN", "process-value")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(operator_path))
    monkeypatch.setattr(h.config, "_get_config_path", h.resolve_config_path)
    snapshot = h.config.get_config_for_profile_home(h.work_home)
    assert snapshot["operator"] == "process-value"
    assert "providers" not in snapshot


def test_named_profile_symlink_alias_uses_owner_env(profile_config_harness, monkeypatch, tmp_path):
    h = profile_config_harness
    _raw_profile(h)
    alias = tmp_path / "alias"
    alias.symlink_to(h.work_home, target_is_directory=True)
    h.state.path = alias / "config.yaml"
    monkeypatch.setenv("PROFILE_TOKEN", "process-value")
    assert h.config.get_config_snapshot()["providers"]["work"]["api_key"] == "work-value"


def test_profile_env_read_failure_stays_isolated(profile_config_harness, monkeypatch):
    h = profile_config_harness
    _raw_profile(h)
    monkeypatch.setenv("PROFILE_TOKEN", "process-value")
    def fail(_home):
        raise OSError("synthetic env failure")
    monkeypatch.setattr(h.profiles, "get_profile_runtime_env", fail)
    assert h.config.get_config_snapshot()["providers"]["work"]["api_key"] == "${PROFILE_TOKEN}"


def test_default_profile_keeps_process_env_expansion(profile_config_harness, monkeypatch):
    h = profile_config_harness
    (h.default_home / "config.yaml").write_text("value: ${PROFILE_TOKEN}\n", encoding="utf-8")
    monkeypatch.setenv("PROFILE_TOKEN", "process-value")
    assert h.config.get_config_snapshot()["value"] == "process-value"


def test_concurrent_default_reload_cannot_change_named_snapshot(profile_config_harness, monkeypatch):
    h = profile_config_harness
    _raw_profile(h)
    (h.default_home / "config.yaml").write_text("value: ${PROFILE_TOKEN}\n", encoding="utf-8")
    monkeypatch.setenv("PROFILE_TOKEN", "process-value")
    scope = threading.local()
    monkeypatch.setattr(h.config, "_get_config_path", lambda: scope.home / "config.yaml")
    monkeypatch.setattr(h.profiles, "get_active_hermes_home", lambda: scope.home)
    barrier = threading.Barrier(2)
    results, errors = {}, []

    def read(name, home):
        try:
            scope.home = home
            first = h.config.get_config_snapshot()
            barrier.wait(timeout=5)
            h.config.reload_config()
            barrier.wait(timeout=5)
            results[name] = (first, h.config.get_config_snapshot())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=read, args=("work", h.work_home)),
               threading.Thread(target=read, args=("default", h.default_home))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert not errors
    assert all(s["providers"]["work"]["api_key"] == "work-value" for s in results["work"])
    assert all(s["value"] == "process-value" for s in results["default"])
