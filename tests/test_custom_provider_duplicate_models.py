import json
import sys
import types

import api.config as config
import api.profiles as profiles


def _install_fake_hermes_cli(monkeypatch):
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda _pid: []

    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {}

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)


def test_named_custom_providers_keep_duplicate_model_ids(monkeypatch, tmp_path):
    _install_fake_hermes_cli(monkeypatch)

    (tmp_path / "auth.json").write_text(json.dumps({"version": 1, "providers": {}}), encoding="utf-8")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg.update(
        {
            "model": {"default": "gpt-5.4", "provider": "custom:super-javis"},
            "custom_providers": [
                {"name": "edith", "models": {"gpt-5.4": {}}},
                {
                    "name": "super-javis",
                    "models": {
                        "gpt-5.3-codex": {},
                        "gpt-5.4": {},
                        "gpt-5.5": {},
                    },
                },
            ],
        }
    )
    config._cfg_mtime = 0.0
    config.invalidate_models_cache()

    try:
        result = config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config.invalidate_models_cache()

    groups = {g["provider"]: g["models"] for g in result.get("groups", [])}
    assert "edith" in groups
    assert "super-javis" in groups

    edith_ids = [m["id"] for m in groups["edith"]]
    super_ids = [m["id"] for m in groups["super-javis"]]

    assert "gpt-5.4" in edith_ids
    assert "@custom:super-javis:gpt-5.4" in super_ids
    assert "gpt-5.5" in super_ids
