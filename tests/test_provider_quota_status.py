"""Regression coverage for active-provider quota status (#706)."""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from pathlib import Path

import api.config as config
import api.profiles as profiles

ROOT = Path(__file__).resolve().parents[1]


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


def test_openrouter_quota_fetches_key_endpoint_and_sanitizes_response(monkeypatch, tmp_path):
    """OpenRouter's documented key endpoint should be called server-side only."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-openrouter-key-private\n", encoding="utf-8")
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        seen["authorization"] = req.headers.get("Authorization")
        payload = {"data": {"limit_remaining": "12.5", "usage": 3, "limit": 20, "key": "must-not-leak"}}
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    try:
        result = providers.get_provider_quota()
    finally:
        _restore_config(old_cfg, old_mtime)

    assert seen == {
        "url": "https://openrouter.ai/api/v1/key",
        "timeout": 3.0,
        "authorization": "Bearer test-openrouter-key-private",
    }
    assert result == {
        "ok": True,
        "provider": "openrouter",
        "display_name": "OpenRouter",
        "supported": True,
        "status": "available",
        "label": "OpenRouter credits",
        "quota": {"limit_remaining": 12.5, "usage": 3, "limit": 20},
        "message": "OpenRouter quota status loaded.",
    }
    assert "test-openrouter-key-private" not in repr(result)
    assert "must-not-leak" not in repr(result)


def test_openrouter_quota_no_key_returns_safe_no_key_without_network(monkeypatch, tmp_path):
    """No-key state must not call OpenRouter or leak environment details."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    def explode(*_args, **_kwargs):
        raise AssertionError("quota lookup should not call the network without a key")

    monkeypatch.setattr(providers.urllib.request, "urlopen", explode)
    try:
        result = providers.get_provider_quota()
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["ok"] is False
    assert result["provider"] == "openrouter"
    assert result["supported"] is True
    assert result["status"] == "no_key"
    assert result["quota"] is None
    assert "OPENROUTER_API_KEY" in result["message"]


def test_openrouter_quota_invalid_key_and_timeout_are_sanitized(monkeypatch, tmp_path):
    """Invalid-key and timeout/error paths should expose statuses, not secrets."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=test-openrouter-key-private\n", encoding="utf-8")
    old_cfg, old_mtime = _with_config(model={"provider": "openrouter"})

    import api.providers as providers

    req = providers.urllib.request.Request("https://openrouter.ai/api/v1/key")
    invalid = urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, BytesIO(b"secret body"))
    errors = [invalid, TimeoutError("slow secret")]

    try:
        for expected in ("invalid_key", "unavailable"):
            def fake_urlopen(_req, timeout=None, *, _err=errors.pop(0)):
                raise _err

            monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
            result = providers.get_provider_quota("openrouter")
            assert result["ok"] is False
            assert result["status"] == expected
            assert result["quota"] is None
            assert "test-openrouter-key-private" not in repr(result)
            assert "secret" not in repr(result).lower()
    finally:
        _restore_config(old_cfg, old_mtime)


def test_unsupported_provider_reports_followup_state(monkeypatch, tmp_path):
    """Providers without safe quota APIs should return a clear unsupported state."""
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path)
    old_cfg, old_mtime = _with_config(model={"provider": "openai"})

    import api.providers as providers
    try:
        result = providers.get_provider_quota()
    finally:
        _restore_config(old_cfg, old_mtime)

    assert result["ok"] is False
    assert result["provider"] == "openai"
    assert result["supported"] is False
    assert result["status"] == "unsupported"
    assert result["quota"] is None
    assert "follow-up" in result["message"]


def test_provider_quota_route_is_registered():
    """The backend must expose a route for the UI to poll quota status."""
    routes = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    assert 'parsed.path == "/api/provider/quota"' in routes
    assert "get_provider_quota(provider_id)" in routes


def test_provider_quota_card_is_rendered_in_providers_panel():
    """The Providers panel should show active provider quota/status before cards."""
    panels = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    assert "api('/api/provider/quota')" in panels
    assert "function _buildProviderQuotaCard" in panels
    assert "Active provider quota" in panels
    assert "provider-quota-card" in panels


def test_provider_quota_styles_exist():
    """Quota UI should have visible supported/unavailable/invalid states."""
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    for token in (
        ".provider-quota-card",
        ".provider-quota-metric",
        ".provider-quota-card-available",
        ".provider-quota-card-no_key",
        ".provider-quota-card-invalid_key",
    ):
        assert token in css
