"""Tests for configurable monthly budget limit with percentage-used tracking (#692).

Covers:
  - Static analysis: hooks present in config.py, providers.py, panels.js, style.css, i18n.js
  - Backend: monthly_budget propagates through all three response paths
  - Backend: save_settings validates and coerces provider_cost_budget
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import urllib.error

import api.config as config
import api.profiles as profiles

ROOT = Path(__file__).resolve().parents[1]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# ── Static analysis ───────────────────────────────────────────────────────────


def test_provider_cost_budget_in_settings_defaults():
    assert "provider_cost_budget" in config._SETTINGS_DEFAULTS, (
        "provider_cost_budget must appear in _SETTINGS_DEFAULTS"
    )


def test_monthly_budget_in_providers_response_dicts():
    src = _read("api/providers.py")
    assert '"monthly_budget"' in src or "'monthly_budget'" in src, (
        "monthly_budget must appear in providers.py response dicts"
    )


def test_get_provider_cost_budget_defined():
    src = _read("api/providers.py")
    assert "_get_provider_cost_budget" in src, (
        "_get_provider_cost_budget helper must be defined in providers.py"
    )


def test_attach_budget_controls_defined():
    src = _read("static/panels.js")
    assert "function _attachBudgetControls" in src, (
        "_attachBudgetControls must be defined in panels.js"
    )


def test_attach_budget_controls_called_in_data_branch():
    src = _read("static/panels.js")
    # Must be called in the hasData branch (after body.appendChild(wrap))
    assert src.count("_attachBudgetControls") >= 2, (
        "_attachBudgetControls must be called in both the data and no-data branches"
    )


def test_budget_dom_classes_in_panels_js():
    src = _read("static/panels.js")
    for cls in ("provider-cost-budget-row", "provider-cost-budget-bar", "provider-cost-budget-input"):
        assert cls in src, f"DOM class '{cls}' must appear in panels.js"


def test_budget_dom_classes_in_style_css():
    src = _read("static/style.css")
    for cls in ("provider-cost-budget-row", "provider-cost-budget-bar", "provider-cost-budget-input"):
        assert cls in src, f"CSS class '{cls}' must appear in style.css"


def test_budget_i18n_keys_in_i18n_js():
    src = _read("static/i18n.js")
    for key in (
        "provider_cost_budget_label",
        "provider_cost_budget_pct",
        "provider_cost_budget_save_failed",
    ):
        assert key in src, f"i18n key '{key}' must appear in i18n.js"


def test_settings_post_in_panels_js_for_budget():
    src = _read("static/panels.js")
    assert "provider_cost_budget" in src, (
        "panels.js must POST provider_cost_budget to /api/settings for budget save"
    )


# ── Fixtures ──────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _with_config(model=None, providers=None):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = model or {}
    if providers is not None:
        config.cfg["providers"] = providers
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    return old_cfg, old_mtime


def _restore_config(old_cfg, old_mtime):
    config.cfg.clear()
    config.cfg.update(old_cfg)
    config._cfg_mtime = old_mtime


def _fake_urlopen_with_usage(usage=5.0, limit=20.0, label="Credits"):
    def fake_urlopen(req, timeout):
        payload = {"data": {"usage": usage, "limit": limit, "label": label}}
        return _FakeResponse(json.dumps(payload).encode("utf-8"))
    return fake_urlopen


# ── Behavioral: monthly_budget in responses ───────────────────────────────────


def test_monthly_budget_in_available_response(monkeypatch, tmp_path):
    """get_provider_cost_history returns monthly_budget when configured."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-key\n", encoding="utf-8")

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"provider_cost_budget": 75.0}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers
    monkeypatch.setattr(providers.urllib.request, "urlopen", _fake_urlopen_with_usage(5.0, 20.0))

    try:
        result = providers.get_provider_cost_history("openrouter", days=7)
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["monthly_budget"] == 75.0
    assert result["limit"] == 20.0


def test_monthly_budget_none_when_not_configured(monkeypatch, tmp_path):
    """get_provider_cost_history returns monthly_budget: None when no budget set."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-key\n", encoding="utf-8")

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers
    monkeypatch.setattr(providers.urllib.request, "urlopen", _fake_urlopen_with_usage(5.0, 20.0))

    try:
        result = providers.get_provider_cost_history("openrouter", days=7)
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["monthly_budget"] is None


def test_monthly_budget_in_unavailable_response(monkeypatch, tmp_path):
    """monthly_budget is included when upstream OpenRouter call fails."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-key\n", encoding="utf-8")

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"provider_cost_budget": 50.0}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    req = providers.urllib.request.Request("https://openrouter.ai/api/v1/key")

    def fail_urlopen(_req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, BytesIO(b""))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fail_urlopen)

    try:
        result = providers.get_provider_cost_history("openrouter", days=7)
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["status"] == "unavailable"
    assert result["monthly_budget"] == 50.0


def test_monthly_budget_in_no_key_response(monkeypatch, tmp_path):
    """monthly_budget is included in no_key response."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # No .env → no key

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"provider_cost_budget": 100.0}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    def explode(*_a, **_kw):
        raise AssertionError("must not call network without a key")

    monkeypatch.setattr(providers.urllib.request, "urlopen", explode)

    try:
        result = providers.get_provider_cost_history("openrouter", days=7)
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["status"] == "no_key"
    assert result["monthly_budget"] == 100.0


# ── Behavioral: save_settings validation ─────────────────────────────────────


def test_save_settings_persists_float_budget(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)
    monkeypatch.setattr(config, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "resolve_default_workspace", lambda x: tmp_path)

    config.save_settings({"provider_cost_budget": 100.0})
    loaded = config.load_settings()
    assert loaded["provider_cost_budget"] == 100.0


def test_save_settings_coerces_string_budget(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)
    monkeypatch.setattr(config, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "resolve_default_workspace", lambda x: tmp_path)

    config.save_settings({"provider_cost_budget": "50.00"})
    loaded = config.load_settings()
    assert loaded["provider_cost_budget"] == 50.0


def test_save_settings_clears_budget_with_none(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    # Pre-seed a budget
    settings_path.write_text(json.dumps({"provider_cost_budget": 75.0}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)
    monkeypatch.setattr(config, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "resolve_default_workspace", lambda x: tmp_path)

    config.save_settings({"provider_cost_budget": None})
    loaded = config.load_settings()
    assert loaded["provider_cost_budget"] is None


def test_save_settings_clears_budget_with_empty_string(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"provider_cost_budget": 75.0}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)
    monkeypatch.setattr(config, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "resolve_default_workspace", lambda x: tmp_path)

    config.save_settings({"provider_cost_budget": ""})
    loaded = config.load_settings()
    assert loaded["provider_cost_budget"] is None


def test_save_settings_rejects_zero_budget(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"provider_cost_budget": 50.0}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)
    monkeypatch.setattr(config, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "resolve_default_workspace", lambda x: tmp_path)

    config.save_settings({"provider_cost_budget": 0})
    loaded = config.load_settings()
    # Previous value must be unchanged
    assert loaded["provider_cost_budget"] == 50.0


def test_save_settings_rejects_negative_budget(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"provider_cost_budget": 50.0}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)
    monkeypatch.setattr(config, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "resolve_default_workspace", lambda x: tmp_path)

    config.save_settings({"provider_cost_budget": -10})
    loaded = config.load_settings()
    assert loaded["provider_cost_budget"] == 50.0


def test_save_settings_rejects_invalid_string_budget(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"provider_cost_budget": 50.0}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)
    monkeypatch.setattr(config, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "resolve_default_workspace", lambda x: tmp_path)

    config.save_settings({"provider_cost_budget": "invalid"})
    loaded = config.load_settings()
    assert loaded["provider_cost_budget"] == 50.0


def test_save_settings_rejects_infinity_budget(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"provider_cost_budget": 50.0}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)
    monkeypatch.setattr(config, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "resolve_default_workspace", lambda x: tmp_path)

    config.save_settings({"provider_cost_budget": float("inf")})
    loaded = config.load_settings()
    assert loaded["provider_cost_budget"] == 50.0


def test_save_settings_rejects_nan_budget(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"provider_cost_budget": 50.0}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)
    monkeypatch.setattr(config, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "resolve_default_workspace", lambda x: tmp_path)

    config.save_settings({"provider_cost_budget": float("nan")})
    loaded = config.load_settings()
    assert loaded["provider_cost_budget"] == 50.0


def test_save_settings_rejects_huge_budget(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"provider_cost_budget": 50.0}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)
    monkeypatch.setattr(config, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "resolve_default_workspace", lambda x: tmp_path)

    config.save_settings({"provider_cost_budget": 1e9})
    loaded = config.load_settings()
    assert loaded["provider_cost_budget"] == 50.0


def test_save_settings_rejects_subcent_budget_after_rounding(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"provider_cost_budget": 50.0}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)
    monkeypatch.setattr(config, "DEFAULT_WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "resolve_default_workspace", lambda x: tmp_path)

    config.save_settings({"provider_cost_budget": 0.004})
    loaded = config.load_settings()
    assert loaded["provider_cost_budget"] == 50.0


def test_get_provider_cost_budget_rejects_huge_manual_setting(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"provider_cost_budget": 5e12}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    import api.providers as providers

    assert providers._get_provider_cost_budget() is None
