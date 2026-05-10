"""
Sprint 17 Tests: send_key setting, commands.js static file, workspace subdir listing.
"""

import json
import urllib.error
import urllib.request

from tests._pytest_port import BASE


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def make_session(created_list):
    d, _ = post("/api/session/new", {})
    sid = d["session"]["session_id"]
    created_list.append(sid)
    return sid, d["session"]


# ── Settings: send_key ──────────────────────────────────────────────────────


def test_settings_send_key_default():
    """GET /api/settings returns send_key with default value 'enter'."""
    data, status = get("/api/settings")
    assert status == 200
    assert data.get("send_key") == "enter"


def test_settings_save_send_key():
    """POST /api/settings with send_key persists and round-trips."""
    try:
        # Save ctrl+enter
        _, status = post("/api/settings", {"send_key": "ctrl+enter"})
        assert status == 200
        # Verify it persisted
        data, _ = get("/api/settings")
        assert data["send_key"] == "ctrl+enter"
    finally:
        # Always restore default
        post("/api/settings", {"send_key": "enter"})
    data, _ = get("/api/settings")
    assert data["send_key"] == "enter"


def test_settings_invalid_send_key_rejected():
    """POST /api/settings with invalid send_key value is silently ignored."""
    # Set a known good value first
    post("/api/settings", {"send_key": "enter"})
    # Try to set an invalid value
    data, status = post("/api/settings", {"send_key": "invalid_value"})
    assert status == 200
    # Should still be 'enter' (invalid value ignored)
    assert data["send_key"] == "enter"


def test_settings_unknown_key_ignored():
    """POST /api/settings ignores unknown keys."""
    data, status = post("/api/settings", {"unknown_key": "value", "send_key": "enter"})
    assert status == 200
    assert "unknown_key" not in data


# ── Static file: commands.js ────────────────────────────────────────────────


def test_static_commands_js_served():
    """GET /static/commands.js returns 200 and contains COMMANDS registry."""
    req = urllib.request.Request(BASE + "/static/commands.js")
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read().decode()
        assert r.status == 200
        assert "COMMANDS" in body
        assert "executeCommand" in body


# ── Workspace: subdir listing ───────────────────────────────────────────────


def test_list_workspace_root():
    """GET /api/list with path=. returns entries for workspace root."""
    created = []
    sid, _ = make_session(created)
    data, status = get(f"/api/list?session_id={sid}&path=.")
    assert status == 200
    assert "entries" in data
    assert isinstance(data["entries"], list)
