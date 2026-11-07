"""Keyless external-process plugins follow Hermes auth, not API-key presence.

The fixtures model an installed plugin with no providers config or key. They
exercise real static/live catalog assembly, not a real executable's login.
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

import api.config as config
import api.profiles as profiles
from api.plugin_providers import invalidate_plugin_model_provider_cache


PID = "external-process-plugin"
ALIAS = "external-plugin"
MODEL = "gemini-2.5-flash"


@pytest.fixture
def plugin_catalog(monkeypatch, tmp_path):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    config.cfg.clear()
    config.cfg.update({"model": {"provider": "gemini", "default": MODEL}, "providers": {}})
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except OSError:
        config._cfg_mtime = 0.0

    def install(*, logged_in=True, model_ids=(), auth_status=None):
        profile = SimpleNamespace(
            name=PID,
            display_name="External Process Plugin",
            env_vars=(),
            auth_type="external_process",
            aliases=(ALIAS,),
            fallback_models=tuple(model_ids),
        )
        providers = types.ModuleType("providers")
        providers.list_providers = lambda: [profile]
        monkeypatch.setitem(sys.modules, "providers", providers)
        package = types.ModuleType("hermes_cli")
        package.__path__ = []
        models = types.ModuleType("hermes_cli.models")
        models._PROVIDER_ALIASES = {ALIAS: PID}
        models.list_available_providers = lambda: [
            {"id": PID, "label": profile.display_name,
             "aliases": [ALIAS], "authenticated": logged_in}
        ]
        models.provider_model_ids = lambda pid: list(model_ids) if pid == PID else []
        auth = types.ModuleType("hermes_cli.auth")

        def status(pid):
            if pid != PID:
                return {}
            if isinstance(auth_status, Exception):
                raise auth_status
            if auth_status is not None:
                return auth_status
            return {"logged_in": logged_in, "configured": logged_in,
                    "key_source": "external_process"}

        auth.get_auth_status = status
        monkeypatch.setitem(sys.modules, "hermes_cli", package)
        monkeypatch.setitem(sys.modules, "hermes_cli.models", models)
        monkeypatch.setitem(sys.modules, "hermes_cli.auth", auth)
        invalidate_plugin_model_provider_cache()
        config.invalidate_models_cache()
        from api.providers import _provider_has_key

        assert _provider_has_key(PID) is False

    yield install
    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime
    config.invalidate_models_cache()
    invalidate_plugin_model_provider_cache()


def _catalog(live):
    if live:
        return config.get_available_models(force_refresh=True)
    return config._static_models_catalog_without_live_probes()


@pytest.mark.parametrize("live", [False, True], ids=["static", "live"])
@pytest.mark.parametrize("model_ids", [(), (MODEL,)], ids=["empty", "populated"])
def test_authenticated_keyless_plugin_is_visible(plugin_catalog, live, model_ids):
    plugin_catalog(model_ids=model_ids)
    groups = [g for g in _catalog(live)["groups"] if g["provider_id"] == PID]
    assert len(groups) == 1
    assert groups[0]["provider"] == "External Process Plugin"
    assert [m["id"] for m in groups[0]["models"]] == [f"@{PID}:{m}" for m in model_ids]


@pytest.mark.parametrize("live", [False, True], ids=["static", "live"])
@pytest.mark.parametrize("model_ids", [(), (MODEL,)], ids=["empty", "populated"])
def test_unauthenticated_plugin_is_not_discovered(plugin_catalog, live, model_ids):
    plugin_catalog(logged_in=False, model_ids=model_ids)
    assert all(g["provider_id"] != PID for g in _catalog(live)["groups"])


@pytest.mark.parametrize("live", [False, True], ids=["static", "live"])
def test_plugin_alias_resolves_to_one_canonical_group(plugin_catalog, live):
    plugin_catalog(model_ids=(MODEL,))
    assert config._resolve_provider_alias(ALIAS) == PID
    groups = [g for g in _catalog(live)["groups"] if g["provider_id"] in (PID, ALIAS)]
    assert len(groups) == 1
    assert groups[0]["provider_id"] == PID
    assert [m["id"] for m in groups[0]["models"]] == [f"@{PID}:{MODEL}"]


@pytest.mark.parametrize("logged_in", [False, True])
def test_usable_helper_resolves_plugin_alias_before_auth_lookup(plugin_catalog, logged_in):
    plugin_catalog(logged_in=logged_in)
    assert config._plugin_provider_is_usable(f" {ALIAS.upper()} ") is logged_in
    assert config._plugin_provider_is_usable("unknown-plugin") is False
    assert config._plugin_provider_is_usable("") is False


@pytest.mark.parametrize("status", [{}, [], RuntimeError("auth unavailable")],
                         ids=["missing", "malformed", "exception"])
def test_static_catalog_fails_closed_without_auth_evidence(plugin_catalog, status):
    plugin_catalog(logged_in=False, model_ids=(MODEL,), auth_status=status)
    assert all(g["provider_id"] != PID for g in _catalog(False)["groups"])
