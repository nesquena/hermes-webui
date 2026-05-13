"""Custom branding image settings persistence and static wiring tests."""
import base64
import json
import struct
import urllib.error
import urllib.request
import zlib
from pathlib import Path

from tests._pytest_port import BASE

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")


def png_data_url(width=64, height=64, extra_bytes=0):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + (b"\x00\x00\x00\x00" * width) for _ in range(height))
    padding = chunk(b"tEXt", b"x" * extra_bytes) if extra_bytes else b""
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + padding
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


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


def clear_branding():
    post("/api/settings", {"bot_logo": "", "bot_favicon": ""})


def test_settings_default_branding_images_empty():
    clear_branding()
    data, status = get("/api/settings")
    assert status == 200
    assert data.get("bot_logo", "") == ""
    assert data.get("bot_favicon", "") == ""


def test_settings_rejects_unsafe_branding_image_schemes():
    data, status = post(
        "/api/settings",
        {"bot_logo": "javascript:alert(1)", "bot_favicon": "file:///etc/passwd"},
    )
    assert status == 200
    assert data.get("bot_logo", "") == ""
    assert data.get("bot_favicon", "") == ""


def test_settings_allows_safe_base64_logo_and_favicon_with_their_own_limits():
    logo = png_data_url(64, 64)
    favicon = png_data_url(16, 16)
    try:
        data, status = post("/api/settings", {"bot_logo": logo, "bot_favicon": favicon})
        assert status == 200
        assert data["bot_logo"] == logo
        assert data["bot_favicon"] == favicon
    finally:
        clear_branding()


def test_settings_allows_large_exact_max_dimension_logo_over_legacy_256kb_cap():
    logo = png_data_url(4096, 4096, extra_bytes=300_000)
    assert len(logo) > 256_000
    try:
        data, status = post("/api/settings", {"bot_logo": logo})
        assert status == 200
        assert data["bot_logo"] == logo
    finally:
        clear_branding()


def test_settings_rejects_images_that_only_fit_the_other_slot_or_are_malformed():
    cases = [
        ({"bot_logo": png_data_url(16, 16)}, "bot_logo"),  # favicon-sized, not logo-sized
        ({"bot_favicon": png_data_url(64, 64)}, "bot_favicon"),  # valid favicon
        ({"bot_favicon": png_data_url(513, 16)}, "bot_favicon"),
        ({"bot_logo": png_data_url(4097, 64)}, "bot_logo"),
        ({"bot_logo": "data:image/png;base64,iVBORw0KGgo="}, "bot_logo"),
    ]
    try:
        # First prove the 64x64 case is accepted as favicon under favicon limits.
        data, status = post("/api/settings", cases[1][0])
        assert status == 200
        assert data["bot_favicon"] == cases[1][0]["bot_favicon"]
        post("/api/settings", {"bot_favicon": ""})

        for payload, key in cases[:1] + cases[2:]:
            data, status = post("/api/settings", payload)
            assert status == 200
            assert data.get(key, "") == ""
    finally:
        clear_branding()


def test_settings_backend_normalizes_logo_and_favicon_separately():
    assert '"bot_logo"' in CONFIG_PY
    assert '"bot_favicon"' in CONFIG_PY
    assert "def _normalize_bot_logo" in CONFIG_PY
    assert "def _normalize_bot_favicon" in CONFIG_PY
    assert "_BOT_LOGO_MAX_FILE_BYTES = 12 * 1024 * 1024" in CONFIG_PY
    assert "_BOT_LOGO_MAX_VALUE_LENGTH = ((_BOT_LOGO_MAX_FILE_BYTES + 2) // 3) * 4 + 128" in CONFIG_PY
    assert "_BOT_LOGO_MIN_DIMENSION = 64" in CONFIG_PY
    assert "_BOT_LOGO_MAX_DIMENSION = 4096" in CONFIG_PY
    assert "_BOT_FAVICON_MIN_DIMENSION = 16" in CONFIG_PY
    assert "_BOT_FAVICON_MAX_DIMENSION = 512" in CONFIG_PY
    normalizer = CONFIG_PY.split("def _normalize_bot_image", 1)[1].split("def _normalize_bot_logo", 1)[0]
    assert '"file"' not in normalizer
    assert '"javascript"' not in normalizer


def test_static_logo_and_favicon_controls_and_targets_exist():
    assert 'id="settingsBotLogo"' in INDEX_HTML
    assert 'id="settingsBotLogoFile"' in INDEX_HTML
    assert 'id="settingsBotLogoUpload"' in INDEX_HTML
    assert 'id="settingsBotLogoWorkspace"' in INDEX_HTML
    assert 'id="settingsBotLogoFileName"' in INDEX_HTML
    assert 'id="settingsBotLogoPreview"' not in INDEX_HTML
    assert 'id="settingsBotLogoClear"' in INDEX_HTML
    assert 'id="settingsBotFavicon"' in INDEX_HTML
    assert 'id="settingsBotFaviconFile"' in INDEX_HTML
    assert 'id="settingsBotFaviconUpload"' in INDEX_HTML
    assert 'id="settingsBotFaviconWorkspace"' in INDEX_HTML
    assert 'id="settingsBotFaviconFileName"' in INDEX_HTML
    assert 'id="settingsBotFaviconPreview"' not in INDEX_HTML
    assert 'id="settingsBotFaviconClear"' in INDEX_HTML
    assert 'Upload file' in INDEX_HTML
    assert 'Upload from workspace' in INDEX_HTML
    assert 'PNG, JPG, WebP, GIF, or ICO' in INDEX_HTML
    assert 'up to 12 MB' in INDEX_HTML
    assert 'type="url" id="settingsBotLogo"' not in INDEX_HTML
    assert 'type="url" id="settingsBotFavicon"' not in INDEX_HTML
    assert 'https://example.com/logo.png' not in INDEX_HTML
    assert 'https://example.com/favicon.png' not in INDEX_HTML
    assert 'id="faviconSvg"' in INDEX_HTML
    assert 'id="favicon32"' in INDEX_HTML
    assert 'id="faviconShortcut"' in INDEX_HTML
    assert 'id="appleTouchIcon"' in INDEX_HTML
    assert 'id="appTitlebarLogo"' in INDEX_HTML
    assert 'id="emptyStateLogo"' in INDEX_HTML
    assert 'id="toast" role="status" aria-live="polite" aria-atomic="true"' in INDEX_HTML


def test_boot_branding_logo_and_favicon_wiring_exists():
    assert "function applyBrandingLogo" in BOOT_JS
    assert "function applyBrandingFavicon" in BOOT_JS
    assert "function validateBrandingLogoForSave" in BOOT_JS
    assert "function validateBrandingFaviconForSave" in BOOT_JS
    assert "function validateBrandingLogoDetailed" in BOOT_JS
    assert "function validateBrandingFaviconDetailed" in BOOT_JS
    assert "HERMES_BRANDING_DIMENSIONS" in BOOT_JS
    assert "_setBrandingFavicons(url)" in BOOT_JS
    assert "applyBrandingLogo(window._botLogo)" in BOOT_JS
    assert "applyBrandingFavicon(window._botFavicon)" in BOOT_JS
    assert "HERMES_BRANDING_MAX_FILE_BYTES = 12 * 1024 * 1024" in BOOT_JS
    assert "HERMES_BRANDING_MAX_VALUE_LENGTH = Math.ceil(HERMES_BRANDING_MAX_FILE_BYTES * 4 / 3) + 128" in BOOT_JS
    assert "raw.length>HERMES_BRANDING_MAX_VALUE_LENGTH" in BOOT_JS
    assert "_brandingDataUrlByteLength(raw)>HERMES_BRANDING_MAX_FILE_BYTES" in BOOT_JS
    assert "HERMES_BRANDING_CACHE_KEY" in BOOT_JS
    assert "data-branding-logo-cached" in INDEX_HTML
    assert "data-branding-logo-cached" in BOOT_JS
    assert "_loadCachedBranding" in BOOT_JS
    assert "_cacheBrandingLogo(window._botLogo)" in BOOT_JS
    assert "setTimeout(()=>finish({value:'', error:{code:'load_timeout'}}),15000)" in BOOT_JS
    assert "javascript:" not in BOOT_JS


def test_panels_preferences_payload_load_and_save_branding_images():
    assert "if(botLogoField&&_brandingDirty.has('logo')) payload.bot_logo=botLogoField.value" in PANELS_JS
    assert "if(botFaviconField&&_brandingDirty.has('favicon')) payload.bot_favicon=botFaviconField.value" in PANELS_JS
    assert "_brandingDirty.add(kind)" in PANELS_JS
    assert "_brandingDirty.delete('logo')" in PANELS_JS
    assert "_brandingDirty.delete('favicon')" in PANELS_JS
    assert "_wireBrandingFileControl('logo', settings.bot_logo||'')" in PANELS_JS
    assert "_wireBrandingFileControl('favicon', settings.bot_favicon||'')" in PANELS_JS
    assert "applyBrandingLogo(savedLogo)" in PANELS_JS
    assert "applyBrandingFavicon(savedFavicon)" in PANELS_JS
    assert "if(botLogoField&&_brandingDirty.has('logo')) body.bot_logo=(botLogoField.value||'').trim()" in PANELS_JS
    assert "if(botFaviconField&&_brandingDirty.has('favicon')) body.bot_favicon=(botFaviconField.value||'').trim()" in PANELS_JS
    assert "FileReader" in PANELS_JS
    assert "BRANDING_MIME_TYPES" in PANELS_JS
    assert "image/vnd.microsoft.icon" in PANELS_JS
    assert "BRANDING_MAX_FILE_BYTES = 12 * 1024 * 1024" in PANELS_JS
    assert "BRANDING_ALLOWED_COPY = 'PNG, JPG, WebP, GIF, or ICO'" in PANELS_JS
    assert "_brandingUploadErrorMessage" in PANELS_JS
    assert "image is smaller than" in PANELS_JS
    assert "is larger than 12 MB" in PANELS_JS
    assert "_cacheBrandingLogo(savedLogo)" in PANELS_JS
    assert "window._consumeBrandingWorkspacePick" in PANELS_JS
    assert "api/file/raw?session_id=" in PANELS_JS
    assert "_brandingWorkspacePickToast(kind)" in PANELS_JS
    assert "Press Escape to cancel" in PANELS_JS
    assert "contentLength>BRANDING_MAX_FILE_BYTES" in PANELS_JS
    assert "Number(blob.size)>BRANDING_MAX_FILE_BYTES" in PANELS_JS
    assert "await switchToWorkspace(defaultWs" in PANELS_JS
    assert "switchPanel('workspaces')" in PANELS_JS
    assert "Open a workspace session before choosing a workspace image" not in PANELS_JS
    assert "_clearBrandingImage(kind)" in PANELS_JS


def test_custom_logo_retains_skin_accent_glow():
    style = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert ".brand-logo.is-custom{background:linear-gradient(145deg,var(--accent-bg),var(--accent-bg));border-color:var(--accent-bg);box-shadow:0 4px 20px var(--accent-bg);" in style
    assert ".app-titlebar-icon.brand-logo.is-custom{box-shadow:0 2px 10px var(--accent-bg);}" in style
    assert ".brand-logo.is-custom{background:transparent" not in style
    assert ".brand-logo.is-custom{background:transparent;border-color:var(--border2);box-shadow:none" not in style


def test_cached_custom_logo_hides_default_logo_before_boot_reconciliation():
    style = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert "html[data-branding-logo-cached] #appTitlebarLogo:not(.is-custom) > svg" in style
    assert "html[data-branding-logo-cached] #emptyStateLogo:not(.is-custom) > svg" in style
    assert "opacity: 0" in style
