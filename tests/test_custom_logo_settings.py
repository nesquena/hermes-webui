"""Custom logo settings persistence and static wiring tests."""
import json
import urllib.error
import urllib.request
from pathlib import Path

from tests._pytest_port import BASE

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


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


def test_settings_default_bot_logo_empty():
    data, status = get("/api/settings")
    assert status == 200
    assert data.get("bot_logo", "") == ""


def test_settings_save_bot_logo_https_round_trips():
    url = "https://example.com/logo.png"
    try:
        data, status = post("/api/settings", {"bot_logo": url})
        assert status == 200
        assert data["bot_logo"] == url
        data, _ = get("/api/settings")
        assert data["bot_logo"] == url
    finally:
        post("/api/settings", {"bot_logo": ""})


def test_settings_rejects_unsafe_bot_logo_scheme():
    data, status = post("/api/settings", {"bot_logo": "javascript:alert(1)"})
    assert status == 200
    assert data.get("bot_logo", "") == ""
    data, status = post("/api/settings", {"bot_logo": "file:///etc/passwd"})
    assert status == 200
    assert data.get("bot_logo", "") == ""


def test_settings_allows_safe_base64_data_image():
    data_url = "data:image/png;base64,iVBORw0KGgo="
    try:
        data, status = post("/api/settings", {"bot_logo": data_url})
        assert status == 200
        assert data["bot_logo"] == data_url
    finally:
        post("/api/settings", {"bot_logo": ""})


def test_settings_backend_normalizes_bot_logo():
    assert '"bot_logo"' in CONFIG_PY
    assert "def _normalize_bot_logo" in CONFIG_PY
    assert 'parsed.scheme in {"http", "https"}' in CONFIG_PY
    assert '"file"' not in CONFIG_PY.split("def _normalize_bot_logo", 1)[1].split("def load_settings", 1)[0]


def test_static_logo_controls_and_targets_exist():
    assert 'id="settingsBotLogo"' in INDEX_HTML
    assert 'id="settingsBotLogoPreview"' in INDEX_HTML
    assert 'id="settingsBotLogoClear"' in INDEX_HTML
    assert 'id="faviconSvg"' in INDEX_HTML
    assert 'id="favicon32"' in INDEX_HTML
    assert 'id="faviconShortcut"' in INDEX_HTML
    assert 'id="appleTouchIcon"' in INDEX_HTML
    assert 'id="appTitlebarLogo"' in INDEX_HTML
    assert 'id="emptyStateLogo"' in INDEX_HTML


def test_boot_branding_logo_wiring_exists():
    assert "function applyBrandingLogo" in BOOT_JS
    assert "function _isSafeLogoUrl" in BOOT_JS
    assert "faviconShortcut" in BOOT_JS
    assert "appTitlebarLogo" in BOOT_JS
    assert "emptyStateLogo" in BOOT_JS
    assert "javascript:" not in BOOT_JS


def test_panels_preferences_payload_load_and_save_bot_logo():
    assert "payload.bot_logo=botLogoField.value" in PANELS_JS
    assert "botLogoField.value=settings.bot_logo||''" in PANELS_JS
    assert "applyBrandingLogo((saved&&saved.bot_logo)||'')" in PANELS_JS
    assert "body.bot_logo=(($('settingsBotLogo')||{}).value||'').trim()" in PANELS_JS
