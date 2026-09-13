"""Grok 4.6 session switches must not keep the Codex 272k context window.

A default Codex route (272,000) leftover in S.lastUsage used to win after the
user switched the chat to xai-oauth/grok-4.6 (500,000). The resolver already
knows the Grok window. The leftover 272k came from:

1. POST /api/session/new never resolved context_length, so a new Grok chat
   started at 0 and the UI merged the previous session's 272k.
2. After /api/session/update, the frontend updated S.session.context_length
   but left S.lastUsage.context_length at 272k. The next indicator sync
   prefers a positive lastUsage value, so the ring snapped back to 272k.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import api.helpers as helpers
import api.models as models
import api.routes as routes
from api.models import SESSIONS

from tests.test_issue3256_context_length_default_only_guard import (
    _install_fake_get_model_context_length,
    _stub_route_session,
)


ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")


def _capture_json(monkeypatch, captured):
    def _j(handler, payload, status=200, extra_headers=None):
        captured.update(payload=payload, status=status)
        return True

    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(helpers, "j", _j)


def test_resolver_returns_grok46_window_not_codex_272k(monkeypatch):
    import api.config as config

    rec = {}
    _install_fake_get_model_context_length(monkeypatch, rec, default_context=500_000)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda *a, **k: {
            "model": {
                "default": "gpt-5.6-sol",
                "provider": "openai-codex",
            }
        },
    )

    assert routes._resolve_context_length_for_session_model("grok-4.6", "xai-oauth") == 500_000
    assert rec["model"] == "grok-4.6"
    assert rec["config_context_length"] is None


def test_new_session_persists_grok46_window(tmp_path, monkeypatch):
    import api.config as config

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    SESSIONS.clear()

    rec = {}
    _install_fake_get_model_context_length(monkeypatch, rec, default_context=500_000)
    monkeypatch.setattr(
        config,
        "get_config",
        lambda *a, **k: {
            "model": {
                "default": "gpt-5.6-sol",
                "provider": "openai-codex",
            }
        },
    )
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda handler: {
            "workspace": str(tmp_path),
            "profile": "default",
            "model": "grok-4.6",
            "model_provider": "xai-oauth",
            "worktree": False,
        },
    )
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda raw: tmp_path)
    monkeypatch.setattr(routes, "_worktree_default_from_config", lambda profile: False)

    captured = {}
    _capture_json(monkeypatch, captured)
    try:
        assert routes.handle_post(object(), SimpleNamespace(path="/api/session/new")) is True
        session = captured["payload"]["session"]
        assert captured["status"] == 200
        assert rec["model"] == "grok-4.6"
        assert session["context_length"] == 500_000
        assert session["model"] in {"grok-4.6", "@xai-oauth:grok-4.6"}
    finally:
        SESSIONS.clear()


def test_session_update_replaces_codex_272k_with_grok46_window(monkeypatch):
    rec = {}
    _install_fake_get_model_context_length(monkeypatch, rec, default_context=500_000)

    s = _stub_route_session(context_length=272_000, threshold_tokens=190_400, model="gpt-5.6-sol")
    s.model_provider = "openai-codex"
    s.workspace = "/tmp"
    s.save = MagicMock()
    s.compact.return_value = {
        **s.compact.return_value,
        "model": "grok-4.6",
        "model_provider": "xai-oauth",
        "context_length": 500_000,
        "threshold_tokens": 0,
        "last_prompt_tokens": 0,
        "messages": [],
    }

    captured = {}

    def fake_j(h, data, status=200, extra_headers=None):
        captured["data"] = data
        captured["status"] = status
        return True

    body = {
        "session_id": "test-4248",
        "workspace": "/tmp",
        "model": "grok-4.6",
        "model_provider": "xai-oauth",
    }
    with patch("api.routes._check_csrf", return_value=True), \
         patch("api.routes.read_body", return_value=body), \
         patch("api.routes._get_or_materialize_session", return_value=s), \
         patch("api.routes.resolve_trusted_workspace", return_value="/tmp"), \
         patch("api.routes.j", side_effect=fake_j), \
         patch("api.routes._get_session_agent_lock", return_value=MagicMock()):
        assert routes.handle_post(object(), urlparse("/api/session/update")) is True

    assert s.model == "grok-4.6"
    assert s.model_provider == "xai-oauth"
    assert rec["model"] == "grok-4.6"
    assert s.context_length == 500_000
    assert s.threshold_tokens == 0
    assert s.last_prompt_tokens == 0
    s.save.assert_called_once()
    assert captured["data"]["session"]["context_length"] == 500_000


def test_model_switch_updates_last_usage_context_window():
    start = BOOT_JS.find("function _applySessionContextMetadataUpdate")
    assert start != -1
    end = BOOT_JS.find("$('modelSelect').onchange", start)
    block = BOOT_JS[start:end]
    assert "S.lastUsage" in block
    assert "S.lastUsage.context_length" in block, (
        "model switch must overwrite lastUsage.context_length so the next "
        "indicator sync cannot prefer the previous model's 272k window"
    )


def test_deferred_model_resolve_overwrites_stale_last_usage_window():
    start = SESSIONS_JS.find("function _resolveSessionModelForDisplaySoon")
    assert start != -1
    end = SESSIONS_JS.find("\nfunction ", start + 1)
    if end == -1 or end < start:
        end = start + 1200
    block = SESSIONS_JS[start:end]
    assert "S.lastUsage" in block and "context_length" in block
    assert "S.lastUsage.context_length" in block or "lastUsage.context_length" in block, (
        "deferred resolve_model=1 must also replace lastUsage.context_length"
    )


def test_slash_model_switch_applies_session_context_metadata():
    assert "_applySessionContextMetadataUpdate" in COMMANDS_JS, (
        "slash /model updates that POST /api/session/update must apply the "
        "returned context_length instead of leaving the Codex 272k ring in place"
    )
