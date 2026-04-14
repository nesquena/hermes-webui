"""
Sprint 45 Tests: v0.50.36 upstream sync with minimal local patch retention.

Covers:
- First password enablement via POST /api/settings keeps the current browser logged in
- The returned auth metadata is present and onboarding can continue with the issued cookie
- Legacy assistant_language is no longer exposed and is removed on the next save
- The local reply-language UI/runtime enhancement is gone from the synced codebase
"""
import json
import pathlib
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8788"
REPO = pathlib.Path(__file__).parent.parent
SETTINGS_FILE = pathlib.Path.home() / ".hermes" / "webui-mvp-test" / "settings.json"


def get(path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code, dict(e.headers)


def post(path, body=None, headers=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code, dict(e.headers)


def read(path):
    return (REPO / path).read_text(encoding="utf-8")


def _snapshot_settings_file():
    if SETTINGS_FILE.exists():
        return SETTINGS_FILE.read_text(encoding="utf-8")
    return None


def _restore_settings_file(original_text):
    if original_text is None:
        SETTINGS_FILE.unlink(missing_ok=True)
        return
    SETTINGS_FILE.write_text(original_text, encoding="utf-8")


def test_first_password_enablement_returns_cookie_and_keeps_browser_logged_in():
    original_settings = _snapshot_settings_file()
    try:
        saved, status, headers = post("/api/settings", {"_set_password": "sprint45-secret"})
        assert status == 200
        assert saved["auth_enabled"] is True
        assert saved["logged_in"] is True
        assert saved["auth_just_enabled"] is True

        set_cookie = headers.get("Set-Cookie", "")
        assert "hermes_session=" in set_cookie
        cookie_header = set_cookie.split(";", 1)[0]

        auth, auth_status, _ = get("/api/auth/status", headers={"Cookie": cookie_header})
        assert auth_status == 200
        assert auth["auth_enabled"] is True
        assert auth["logged_in"] is True

        done, done_status, _ = post(
            "/api/onboarding/complete",
            {},
            headers={"Cookie": cookie_header},
        )
        assert done_status == 200
        assert done["completed"] is True
    finally:
        _restore_settings_file(original_settings)


def test_legacy_assistant_language_is_hidden_and_removed_on_next_save():
    original_settings = _snapshot_settings_file()
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(
                {
                    "assistant_language": "zh",
                    "send_key": "enter",
                    "onboarding_completed": False,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        loaded, status, _ = get("/api/settings")
        assert status == 200
        assert "assistant_language" not in loaded

        saved, save_status, _ = post("/api/settings", {"send_key": "ctrl+enter"})
        assert save_status == 200
        assert "assistant_language" not in saved
        assert saved["send_key"] == "ctrl+enter"

        persisted = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        assert "assistant_language" not in persisted
    finally:
        _restore_settings_file(original_settings)


def test_reply_language_customization_ui_and_runtime_are_removed():
    index_html = read("static/index.html")
    panels_js = read("static/panels.js")
    streaming_py = read("api/streaming.py")

    assert "settingsAssistantLanguage" not in index_html
    assert "assistant_language" not in panels_js
    assert "settingsAssistantLanguage" not in panels_js
    assert "assistant_language" not in streaming_py
    assert "Default reply language:" not in streaming_py


def test_synced_version_strings_show_local_patch_version():
    index_html = read("static/index.html")
    server_py = read("server.py")

    assert "v0.50.36-local.1" in index_html
    assert "HermesWebUI/0.50.36-local.1" in server_py
