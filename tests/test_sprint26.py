"""
Sprint 26 Tests: pluggable UI themes — settings persistence, theme default,
custom theme names accepted.
"""
import json, urllib.error, urllib.request

from tests._pytest_port import BASE


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


# ── Theme settings ───────────────────────────────────────────────────────

def test_settings_default_theme():
    """Default theme should be 'dark'."""
    d, status = get("/api/settings")
    assert status == 200
    assert d.get("theme") == "dark"


def test_settings_set_theme_light():
    """Setting theme to 'light' should persist and round-trip."""
    try:
        d, status = post("/api/settings", {"theme": "light"})
        assert status == 200
        d2, _ = get("/api/settings")
        assert d2.get("theme") == "light"
    finally:
        # Reset to dark
        post("/api/settings", {"theme": "dark"})


def test_settings_set_theme_light():
    """Setting theme to 'light' should persist."""
    try:
        post("/api/settings", {"theme": "light"})
        d, _ = get("/api/settings")
        assert d.get("theme") == "light"
    finally:
        post("/api/settings", {"theme": "dark"})


def test_settings_set_theme_system():
    """Setting theme to 'system' should persist."""
    try:
        post("/api/settings", {"theme": "system"})
        d, _ = get("/api/settings")
        assert d.get("theme") == "system"
    finally:
        post("/api/settings", {"theme": "dark"})


def test_settings_set_skin():
    """Setting skin should persist."""
    try:
        post("/api/settings", {"skin": "ares"})
        d, _ = get("/api/settings")
        assert d.get("skin") == "ares"
    finally:
        post("/api/settings", {"skin": "default"})


def test_settings_set_skin_poseidon():
    """Setting skin to 'poseidon' should persist."""
    try:
        post("/api/settings", {"skin": "poseidon"})
        d, _ = get("/api/settings")
        assert d.get("skin") == "poseidon"
    finally:
        post("/api/settings", {"skin": "default"})


def test_settings_custom_theme_accepted():
    """Custom theme names should be accepted (no enum gate)."""
    try:
        d, status = post("/api/settings", {"theme": "my-custom-theme"})
        assert status == 200
        d2, _ = get("/api/settings")
        assert d2.get("theme") == "my-custom-theme"
    finally:
        post("/api/settings", {"theme": "dark"})


def test_theme_does_not_break_other_settings():
    """Setting theme should not disturb other settings."""
    d_before, _ = get("/api/settings")
    send_key_before = d_before.get("send_key")
    try:
        post("/api/settings", {"theme": "light"})
        d_after, _ = get("/api/settings")
        assert d_after.get("send_key") == send_key_before
        assert d_after.get("theme") == "light"
    finally:
        post("/api/settings", {"theme": "dark"})


def test_theme_survives_round_trip():
    """Theme set via POST should appear in subsequent GET."""
    try:
        post("/api/settings", {"theme": "light"})
        d, status = get("/api/settings")
        assert status == 200
        assert d["theme"] == "light"
    finally:
        post("/api/settings", {"theme": "dark"})
