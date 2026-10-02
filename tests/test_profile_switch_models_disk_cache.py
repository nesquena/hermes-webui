"""/api/profile/switch keeps the target profile's models disk snapshot only while its sources are unchanged."""

from __future__ import annotations

import os
import shutil
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.config as cfg
import api.profiles as profiles


def _catalog(label: str) -> dict:
    group = {"provider": "OpenAI", "provider_id": "openai", "models": [{"id": label, "label": label, "supports_fast_tier": False}]}
    return {"active_provider": "openai", "default_model": label, "configured_model_badges": {}, "groups": [group], "aliases": {}}


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    default_home = tmp_path / ".hermes"
    demo_home = default_home / "profiles" / "demo"
    demo_home.mkdir(parents=True)
    (default_home / "config.yaml").write_text("model:\n  default: default-model\n", encoding="utf-8")
    _write_demo_config(demo_home)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", default_home)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(profiles._tls, "profile", None, raising=False)
    monkeypatch.setattr(cfg, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(cfg, "_invoke_models_rebuild", lambda _builder: _catalog("fresh-model"))
    for attr in ("_cfg_mtime", "_cfg_path", "_cfg_fingerprint"):
        monkeypatch.setattr(cfg, attr, getattr(cfg, attr), raising=False)
    saved_cfg = dict(cfg._cfg_cache)
    cfg.invalidate_models_cache(delete_disk=False)
    cfg.reload_config()  # the default profile's config is loaded, as at server boot
    yield SimpleNamespace(home=demo_home, cache=tmp_path / "models_cache.demo.json")
    profiles.clear_request_profile()
    cfg.invalidate_models_cache(delete_disk=False)
    cfg._cfg_cache.clear()
    cfg._cfg_cache.update(saved_cfg)


def _write_demo_config(home: Path, default: str = "demo-model", mtime: int = 1_000_000) -> None:
    (home / "config.yaml").write_text(f"model:\n  default: {default}\n", encoding="utf-8")
    os.utime(home / "config.yaml", (mtime, mtime))


def _save_demo_snapshot(env) -> None:
    profiles.set_request_profile("demo")
    try:
        cfg._save_models_cache_to_disk(_catalog("snapshot-model"))
    finally:
        profiles.clear_request_profile()
    assert env.cache.exists()


def _fetch_as_demo() -> dict:
    profiles.set_request_profile("demo")
    try:
        return cfg.get_available_models()
    finally:
        profiles.clear_request_profile()


def _switch_to_demo_and_fetch() -> str:
    profiles.switch_profile("demo", process_wide=False)
    cfg.invalidate_models_cache(delete_disk=False)  # as /api/profile/switch does
    return _fetch_as_demo()["default_model"]


def _write_plugin(home: Path, version: str = "1.0.0", *, flat: bool = False, code: str | None = None, sub: str | None = None, kind: str = "model-provider") -> Path:
    d = home / "plugins" / ("" if flat else "model-providers") / "acme"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.yaml").write_text(f"name: acme\nkind: {kind}\nversion: {version}\n", encoding="utf-8")
    for name, text in (("__init__.py", code), ("models.py", sub)):
        if text is not None:
            (d / name).write_text(text, encoding="utf-8")
            os.utime(d / name, (1_000_000, 1_000_000))
    return d


def _env(text: str):
    return lambda home: (home / ".env").write_text(text, encoding="utf-8")


# (id, source state before the snapshot is saved, change made after it, model served after the switch)
_CASES = [
    ("unchanged", None, None, "snapshot-model"),
    ("env-value-rotated", _env("DEEPSEEK_API_KEY=sk-one\n"), _env("DEEPSEEK_API_KEY=sk-two\n"), "fresh-model"),
    ("env-base-url-changed", _env("LM_BASE_URL=http://127.0.0.1:1234/v1\n"), _env("LM_BASE_URL=http://127.0.0.1:5678/v1\n"), "fresh-model"),
    ("env-key-added", None, _env("DEEPSEEK_API_KEY=sk-new\n"), "fresh-model"),
    ("env-key-removed", _env("DEEPSEEK_API_KEY=sk-old\nOTHER=1\n"), _env("OTHER=1\n"), "fresh-model"),
    # provider detection reads `export KEY=` as key `export KEY`, so the credential is gone
    ("env-export-prefix", _env("DEEPSEEK_API_KEY=sk-old\n"), _env("export DEEPSEEK_API_KEY=sk-old\n"), "fresh-model"),
    ("plugin-installed", None, lambda h: _write_plugin(h), "fresh-model"),
    ("plugin-installed-flat", None, lambda h: _write_plugin(h, flat=True), "fresh-model"),
    ("plugin-installed-flat-yaml-comment", None, lambda h: _write_plugin(h, flat=True, kind="model-provider # valid YAML comment"), "fresh-model"),
    ("plugin-removed", lambda h: _write_plugin(h), lambda h: shutil.rmtree(h / "plugins"), "fresh-model"),
    ("plugin-version-bumped", lambda h: _write_plugin(h, flat=True), lambda h: _write_plugin(h, "2.0.0", flat=True), "fresh-model"),
    ("plugin-code-edited", lambda h: _write_plugin(h, code="M = (1,)\n"), lambda h: (h / "plugins/model-providers/acme/__init__.py").write_text("M = (1, 2)\n"), "fresh-model"),
    ("plugin-code-edited-flat", lambda h: _write_plugin(h, flat=True, code="M = (1,)\n"), lambda h: (h / "plugins/acme/__init__.py").write_text("M = (1, 2)\n"), "fresh-model"),
    ("plugin-submodule-edited", lambda h: _write_plugin(h, code="from .models import M\n", sub="M = (1,)\n"), lambda h: (h / "plugins/model-providers/acme/models.py").write_text("M = (1, 2)\n"), "fresh-model"),
]


@pytest.mark.parametrize("before, change, expected", [c[1:] for c in _CASES], ids=[c[0] for c in _CASES])
def test_switch_serves_snapshot_only_while_sources_are_unchanged(env, before, change, expected):
    if before:
        before(env.home)
    _save_demo_snapshot(env)
    if change:
        change(env.home)
    assert _switch_to_demo_and_fetch() == expected
    assert "sk-" not in env.cache.read_text(encoding="utf-8")  # the fingerprint never records a secret


@pytest.mark.parametrize("change", [_env("LM_BASE_URL=http://127.0.0.1:5678/v1\n"), lambda h: _write_plugin(h, flat=True)], ids=["env", "plugin"])
def test_slow_rebuild_never_serves_fingerprint_mismatched_snapshot_as_stale_fallback(env, monkeypatch, change):
    _env("LM_BASE_URL=http://127.0.0.1:1234/v1\n")(env.home)
    _save_demo_snapshot(env)
    change(env.home)
    profiles.set_request_profile("demo")
    try:
        assert cfg._load_models_cache_from_disk() is None
        assert cfg._load_stale_models_cache_from_disk() is None
    finally:
        profiles.clear_request_profile()
    release = threading.Event()
    monkeypatch.setattr(cfg, "_LIVE_REBUILD_BUDGET_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(cfg, "_invoke_models_rebuild", lambda _b: release.wait(5) and _catalog("fresh-model"))
    try:
        assert _switch_to_demo_and_fetch() != "snapshot-model"
    finally:
        release.set()


def test_stale_fallback_still_serves_snapshot_when_only_webui_version_differs(env, monkeypatch):
    _save_demo_snapshot(env)
    monkeypatch.setattr(cfg, "_current_webui_version", lambda: "v-other")
    profiles.set_request_profile("demo")
    try:
        assert cfg._load_models_cache_from_disk() is None
        assert cfg._load_stale_models_cache_from_disk()["default_model"] == "snapshot-model"
    finally:
        profiles.clear_request_profile()


@pytest.mark.parametrize("manifest", [
    "kind: model-provider # valid YAML comment\n",
    "kind: model-provider\n",
    "kind: 'model-provider'\n",
    "name: x\nkind: tool\n",
    "meta:\n  kind: model-provider\n",
    "# kind: model-provider\n",
    "kind: [unclosed\nkind: model-provider\n",
])
def test_manifest_kind_parse_matches_agent(tmp_path, manifest):
    agent = pytest.importorskip("providers")
    (tmp_path / "plugin.yaml").write_text(manifest, encoding="utf-8")
    assert cfg._declares_model_provider_kind(tmp_path) == agent._declares_model_provider_kind(tmp_path)


def test_same_profile_config_edit_still_deletes_disk_cache(env, monkeypatch):
    _save_demo_snapshot(env)
    assert _switch_to_demo_and_fetch() == "snapshot-model"
    _write_demo_config(env.home, "demo-model-2", 2_000_000)
    cfg.invalidate_models_cache(delete_disk=False)
    cache_at_rebuild = []
    monkeypatch.setattr(cfg, "_invoke_models_rebuild", lambda _b: cache_at_rebuild.append(env.cache.exists()) or _catalog("x"))
    _fetch_as_demo()
    assert cache_at_rebuild == [False]


@pytest.mark.parametrize("delete_disk", [True, False])
def test_invalidate_drops_memory_and_deletes_disk_only_by_default(env, delete_disk):
    _save_demo_snapshot(env)
    _fetch_as_demo()
    profiles.set_request_profile("demo")
    try:
        cfg.invalidate_models_cache(delete_disk=delete_disk)
    finally:
        profiles.clear_request_profile()
    assert cfg._available_models_cache is None and cfg._models_cache_provenance is None
    assert env.cache.exists() is not delete_disk


def test_env_fingerprint_keys_match_provider_env_loader(env):
    from api.providers import _load_env_file

    _env("export A=1\nB='x'\nC=\n# D=1\n")(env.home)
    loaded = sorted(k for k, v in _load_env_file(env.home / ".env").items() if v)
    fp = cfg._models_cache_env_fingerprint(env.home / ".env")
    assert [k for k, _ in fp] == loaded
    assert all(len(h) == 64 and h not in ("1", "x") for _, h in fp)


def test_env_fingerprint_is_keyed_not_a_plain_value_hash(env):
    import hashlib

    _env("DEEPSEEK_API_KEY=sk-secret\n")(env.home)
    (_, digest), = cfg._models_cache_env_fingerprint(env.home / ".env")
    assert digest != hashlib.sha256(b"sk-secret").hexdigest()


def test_plugin_bytecode_cache_does_not_churn_fingerprint(env):
    plugin = _write_plugin(env.home, code="X = 1\n")
    before = cfg._models_cache_plugin_fingerprint(env.home)
    (plugin / "__pycache__").mkdir()
    (plugin / "__pycache__" / "__init__.cpython-311.pyc").write_bytes(b"x")
    assert cfg._models_cache_plugin_fingerprint(env.home) == before


@pytest.mark.parametrize("recreate", [False, True])
def test_delete_and_recreate_profile_drop_its_models_cache(env, monkeypatch, recreate):
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", None)
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [])
    _save_demo_snapshot(env)
    stale = env.cache.read_text(encoding="utf-8")
    profiles.delete_profile_api("demo")
    assert not env.home.exists() and not env.cache.exists()
    if recreate:
        env.cache.write_text(stale, encoding="utf-8")  # left behind by an older build or another process
        profiles.create_profile_api("demo")
        assert not env.cache.exists()
        _write_demo_config(env.home)
        assert _switch_to_demo_and_fetch() == "fresh-model"
