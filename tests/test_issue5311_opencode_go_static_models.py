"""#5311 / #5611 / #7184: OpenCode Go picker uses the live catalog, safely.

Regression coverage for review #7184: the OpenCode Go picker must consume the
live provider catalog (the curated static list is only a fallback), while
filtering out models the Hermes core cannot yet route to a working send
endpoint (``grok-*`` / ``muse-*`` require ``/v1/responses`` but core maps them
to ``chat_completions`` — hermes-agent#85589, #89836). These tests are
behavioral: they stub ``hermes_cli`` and assert the observable model lists, so
a regression cannot hide inside a source scan.
"""

import json
import sys
import types

import api.config as config
import api.profiles as profiles

OPENCODE_GO = "opencode-go"
LIVE_IDS = [
    "minimax-m3",
    "deepseek-v4-flash",
    "grok-4.5",
    "muse-spark-1.2-contributor",
    "qwen3.7-max",
]


def _install_fake_hermes_cli(monkeypatch, *, live_ids):
    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []

    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: [{"id": OPENCODE_GO, "authenticated": True}]
    fake_models.provider_model_ids = lambda pid: list(live_ids) if pid == OPENCODE_GO else []

    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {"key_source": "env"}

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)

    # WebUI CI never imports the bundled agent package.
    monkeypatch.delitem(sys.modules, "agent.credential_pool", raising=False)
    monkeypatch.delitem(sys.modules, "agent", raising=False)


def _call_get_available_models(monkeypatch, tmp_path, live_ids):
    _install_fake_hermes_cli(monkeypatch, live_ids=live_ids)

    (tmp_path / "auth.json").write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)

    for var in ("OPENAI_API_KEY", "HERMES_API_KEY", "HERMES_OPENAI_API_KEY",
                "LOCAL_API_KEY", "OPENROUTER_API_KEY", "API_KEY"):
        monkeypatch.delenv(var, raising=False)

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0

    config.invalidate_models_cache()
    try:
        return config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config.invalidate_models_cache()


def _opencode_go_group(result):
    for group in result.get("groups", []):
        if group.get("provider_id") == OPENCODE_GO or group.get("provider") == OPENCODE_GO:
            return group
    return None


def test_opencode_go_uses_live_results(monkeypatch, tmp_path):
    """(a) Live results are used: the picker group reflects the live catalog."""
    result = _call_get_available_models(monkeypatch, tmp_path, LIVE_IDS)
    group = _opencode_go_group(result)
    assert group is not None, f"opencode-go group missing; groups={result.get('groups', [])}"
    ids = {m["id"] for m in group["models"]}
    assert "minimax-m3" in ids
    assert "deepseek-v4-flash" in ids
    assert "qwen3.7-max" in ids


def test_opencode_go_filters_unrouteable_models(monkeypatch, tmp_path):
    """(c) Every selectable model routes to a working send endpoint.

    grok-* and muse-* need /v1/responses but core maps them to
    chat_completions — they must not appear in the picker (review #7184).
    """
    result = _call_get_available_models(monkeypatch, tmp_path, LIVE_IDS)
    group = _opencode_go_group(result)
    assert group is not None
    ids = {m["id"] for m in group["models"]}
    assert "grok-4.5" not in ids
    assert "muse-spark-1.2-contributor" not in ids


def test_opencode_go_falls_back_to_static_on_empty_probe(monkeypatch, tmp_path):
    """(b) Static fallback on empty probe: curated list is used when the live
    probe yields no models."""
    result = _call_get_available_models(monkeypatch, tmp_path, [])
    group = _opencode_go_group(result)
    assert group is not None, "opencode-go group missing on empty probe"
    ids = {m["id"] for m in group["models"]}
    assert ids, "static fallback should still produce models on empty probe"
