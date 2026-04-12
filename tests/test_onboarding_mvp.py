import json
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from conftest import TEST_STATE_DIR

BASE = "http://127.0.0.1:8788"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def post(path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def test_onboarding_status_defaults_incomplete():
    data, status = get("/api/onboarding/status")
    assert status == 200
    assert data["completed"] is False
    assert data["settings"]["password_enabled"] is False
    assert "provider_note" in data["system"]
    assert isinstance(data["workspaces"]["items"], list)
    assert data["models"]["groups"]


def test_onboarding_complete_persists_flag():
    data, status = post("/api/onboarding/complete", {})
    assert status == 200
    assert data["completed"] is True

    settings = json.loads(
        (TEST_STATE_DIR / "settings.json").read_text(encoding="utf-8")
    )
    assert settings["onboarding_completed"] is True

    data2, status2 = get("/api/onboarding/status")
    assert status2 == 200
    assert data2["completed"] is True


def test_onboarding_complete_preserves_other_settings():
    saved, status = post(
        "/api/settings", {"default_model": "openai/gpt-4o", "bot_name": "Guide"}
    )
    assert status == 200
    assert saved["default_model"] == "openai/gpt-4o"

    done, status2 = post("/api/onboarding/complete", {})
    assert status2 == 200
    assert done["settings"]["default_model"] == "openai/gpt-4o"
