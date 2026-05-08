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

    assert "gpt-5.4" in super_ids
    assert "@custom:edith:gpt-5.4" in edith_ids
    assert "gpt-5.5" in super_ids

    # Active-provider-aware dedup: when active_provider is super-javis,
    # super-javis keeps the bare gpt-5.4 and others are prefixed.
    # (For gpt-5.5, super-javis is the only custom provider with it, so
    # it stays bare.)

    # Badge must NOT leak across providers: only super-javis's gpt-5.4 gets
    # the PRIMARY badge — Edith's gpt-5.4 must be left alone.
    badges = result.get("configured_model_badges", {})
    # Only super-javis's gpt-5.4 (now bare as active provider) should
    # carry the "primary" badge, not Edith's.
    primary_keys = [k for k, v in badges.items() if v.get("role") == "primary"]
    assert "gpt-5.4" in primary_keys, (
        f"Expected bare gpt-5.4 PRIMARY badge (active provider is super-javis); got {primary_keys}"
    )
    # Edith's prefixed gpt-5.4 must NOT receive the PRIMARY badge.
    edith_prefixed_badge = badges.get("@custom:edith:gpt-5.4")
    assert not edith_prefixed_badge or edith_prefixed_badge.get("role") != "primary", (
        f"Edith's gpt-5.4 leaked a PRIMARY badge: {edith_prefixed_badge}"
    )
    # The provider field on the PRIMARY badge must correctly identify super-javis.
    for k in primary_keys:
        assert badges[k].get("provider") == "custom:super-javis", (
            f"Badge provider mismatch: {badges[k]}"
        )
