"""
Sprint 26 Tests: canonical appearance settings persist and legacy theme names
map onto the new theme + skin system.
"""

import json
import urllib.error
import urllib.request
import pathlib
import sys

from tests._pytest_port import BASE

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api import config


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


# ── Theme settings ───────────────────────────────────────────────────────


def test_settings_default_theme():
    """Default theme should be 'dark'."""
    d, status = get("/api/settings")
    assert status == 200
    assert d.get("theme") == "dark"


def test_settings_set_theme_light_persists():
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


def test_settings_legacy_theme_maps_to_dark_skin_pair():
    """Legacy theme names should map to the closest supported theme + skin."""
    try:
        d, status = post("/api/settings", {"theme": "slate"})
        assert status == 200
        d2, _ = get("/api/settings")
        assert d2.get("theme") == "dark"
        assert d2.get("skin") == "slate"
    finally:
        post("/api/settings", {"theme": "dark", "skin": "default"})


def test_settings_legacy_monokai_maps_to_sisyphus_skin():
    """Monokai should migrate onto the closest supported accent skin."""
    try:
        d, status = post("/api/settings", {"theme": "monokai"})
        assert status == 200
        d2, _ = get("/api/settings")
        assert d2.get("theme") == "dark"
        assert d2.get("skin") == "sisyphus"
    finally:
        post("/api/settings", {"theme": "dark", "skin": "default"})


def test_settings_unknown_theme_falls_back_to_dark_default():
    """Unknown themes should normalize to a safe canonical appearance."""
    try:
        d, status = post("/api/settings", {"theme": "my-custom-theme"})
        assert status == 200
        d2, _ = get("/api/settings")
        assert d2.get("theme") == "dark"
        assert d2.get("skin") == "default"
    finally:
        post("/api/settings", {"theme": "dark", "skin": "default"})


def test_settings_invalid_skin_falls_back_to_default():
    """Unknown skin names should normalize back to the default accent."""
    try:
        d, status = post("/api/settings", {"skin": "not-a-skin"})
        assert status == 200
        d2, _ = get("/api/settings")
        assert d2.get("skin") == "default"
    finally:
        post("/api/settings", {"skin": "default"})


def test_load_settings_normalizes_legacy_theme_from_file(monkeypatch, tmp_path):
    """Existing settings.json files with legacy theme names should normalize on load."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"theme": "solarized"}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    loaded = config.load_settings()

    assert loaded["theme"] == "dark"
    assert loaded["skin"] == "poseidon"


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
