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

from tests.test_issue3256_context_length_default_only_guard import (
    _install_fake_get_model_context_length,
)


ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")


def _session_get_block():
    import api.routes as routes

    return Path(routes.__file__).read_text(encoding="utf-8")


def test_resolver_returns_grok46_window_not_codex_272k(monkeypatch):
    import api.config as config
    import api.routes as routes

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


def test_new_session_resolves_context_length_for_selected_model():
    src = _session_get_block()
    new_start = src.find('if parsed.path == "/api/session/new":')
    assert new_start != -1
    new_end = src.find('if parsed.path == "/api/session/compression-recovery/start":', new_start)
    block = src[new_start:new_end]
    assert "_resolve_context_length_for_session_model" in block, (
        "POST /api/session/new must resolve the selected model's context window "
        "instead of leaving context_length unset"
    )


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
