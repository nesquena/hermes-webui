"""Regression checks for configurable pinned session limits."""

import json
import pathlib
import urllib.error
import urllib.request

from tests._pytest_port import BASE

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def make_session(created, title):
    payload = {
        "title": title,
        "messages": [{"role": "user", "content": "keep this conversation handy"}],
        "model": "test/pin-limit-setting",
    }
    d, status = post("/api/session/import", payload)
    assert status == 200
    sid = d["session"]["session_id"]
    created.append(sid)
    return sid


def test_pin_limit_setting_is_exposed_and_wired_through_ui():
    assert '"pinned_sessions_limit": 3' in CONFIG_PY
    assert '"pinned_sessions_limit": (1, 99)' in CONFIG_PY
    assert 'id="settingsPinnedSessionsLimit"' in INDEX_HTML
    assert 'type="number"' in INDEX_HTML
    assert 'min="1"' in INDEX_HTML
    assert 'max="99"' in INDEX_HTML
    assert 'payload.pinned_sessions_limit=parseInt(pinnedLimitField.value,10)' in PANELS_JS
    assert "settings.pinned_sessions_limit" in PANELS_JS
    assert "window._pinnedSessionsLimit=parseInt(s.pinned_sessions_limit||3,10)||3" in BOOT_JS
    assert "function _getPinnedSessionsLimit()" in SESSIONS_JS
    assert "_pinnedSessionCount()>=_getPinnedSessionsLimit()" in SESSIONS_JS


def test_settings_api_persists_integer_pin_limit_and_rejects_invalid_values():
    try:
        d, status = post("/api/settings", {"pinned_sessions_limit": 5})
        assert status == 200
        assert d["pinned_sessions_limit"] == 5

        d, status = post("/api/settings", {"pinned_sessions_limit": "7"})
        assert status == 200
        assert d["pinned_sessions_limit"] == 7

        d, status = post("/api/settings", {"pinned_sessions_limit": 0})
        assert status == 200
        assert d["pinned_sessions_limit"] == 7

        d, status = post("/api/settings", {"pinned_sessions_limit": 100})
        assert status == 200
        assert d["pinned_sessions_limit"] == 7
    finally:
        post("/api/settings", {"pinned_sessions_limit": 3})


def test_session_pin_endpoint_uses_configured_limit():
    created = []
    try:
        d, status = post("/api/settings", {"pinned_sessions_limit": 4})
        assert status == 200
        assert d["pinned_sessions_limit"] == 4

        pinned = [make_session(created, f"Configured pin cap {i}") for i in range(4)]
        for sid in pinned:
            d, status = post("/api/session/pin", {"session_id": sid, "pinned": True})
            assert status == 200
            assert d["session"]["pinned"] is True

        fifth = make_session(created, "Configured pin cap overflow")
        d, status = post("/api/session/pin", {"session_id": fifth, "pinned": True})
        assert status == 400
        assert "4 sessions" in d.get("error", "")
    finally:
        post("/api/settings", {"pinned_sessions_limit": 3})
        for sid in created:
            post("/api/session/delete", {"session_id": sid})
