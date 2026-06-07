"""Regression coverage for custom logo upload validation and DOM hooks."""

import struct
from pathlib import Path
from io import BytesIO
from types import SimpleNamespace

import pytest

from api import branding
from api import routes
from api.branding import (
    _branding_version,
    _delete_logo_files_for_mode,
    _ico_dimensions,
    _validate_upload,
    logo_version_for_settings_value,
)


def _png_header(width: int, height: int, payload: bytes = b"") -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">I", width)
        + struct.pack(">I", height)
        + b"\x08\x06\x00\x00\x00"
        + b"\x00\x00\x00\x00"
        + payload
    )


def _ico_with_png(width: int, height: int) -> bytes:
    png = _png_header(width, height)
    offset = 6 + 16
    return (
        b"\x00\x00\x01\x00"
        + struct.pack("<H", 1)
        + bytes([width if width < 256 else 0, height if height < 256 else 0, 0, 0])
        + struct.pack("<H", 1)
        + struct.pack("<H", 32)
        + struct.pack("<I", len(png))
        + struct.pack("<I", offset)
        + png
    )


def test_logo_upload_rejects_large_png_dimensions():
    with pytest.raises(ValueError) as exc:
        _validate_upload(_png_header(1772, 1799), "Hepburn_light.png")

    message = str(exc.value)
    assert "max 256x256 px and 200 KB" in message
    assert "1772x1799px" in message


def test_logo_upload_rejects_large_file_size():
    header = _png_header(128, 128)
    body = _png_header(128, 128, b"x" * ((201 * 1024) - len(header)))

    with pytest.raises(ValueError) as exc:
        _validate_upload(body, "logo.png")

    message = str(exc.value)
    assert "max 256x256 px and 200 KB" in message
    assert "201 KB" in message


def test_logo_upload_accepts_small_png():
    body = _png_header(128, 128)

    data, ext = _validate_upload(body, "logo.png")

    assert data == body
    assert ext == ".png"


def test_logo_upload_rejects_large_ico_embedded_png_dimensions():
    with pytest.raises(ValueError) as exc:
        _validate_upload(_ico_with_png(512, 512), "logo.ico")

    message = str(exc.value)
    assert "max 256x256 px and 200 KB" in message
    assert "512x512px" in message


def test_logo_upload_accepts_small_ico():
    body = _ico_with_png(64, 64)

    data, ext = _validate_upload(body, "logo.ico")

    assert data == body
    assert ext == ".ico"
    assert _ico_dimensions(body) == (64, 64)


@pytest.mark.parametrize(
    "svg",
    [
        b"<svg><script>alert(1)</script></svg>",
        b'<svg onload="alert(1)"></svg>',
        b'<svg><a href="javascript:alert(1)">x</a></svg>',
        b"<svg><foreignObject><body></body></foreignObject></svg>",
    ],
)
def test_logo_upload_rejects_active_svg_content(svg):
    with pytest.raises(ValueError) as exc:
        _validate_upload(svg, "logo.svg")

    assert "Invalid SVG: active content is not allowed" in str(exc.value)


def test_logo_upload_accepts_inert_svg():
    body = b'<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/></svg>'

    data, ext = _validate_upload(body, "logo.svg")

    assert data == body
    assert ext == ".svg"


def test_branding_version_uses_file_mtime_token(tmp_path):
    logo = tmp_path / "logo-light.png"
    logo.write_bytes(_png_header(32, 32))

    version = _branding_version(logo)

    assert version
    assert version.isdigit()


def test_logo_replacement_deletes_prior_extension_variants(tmp_path, monkeypatch):
    monkeypatch.setattr(branding, "BRANDING_DIR", tmp_path)
    old_png = tmp_path / "logo-light.png"
    old_svg = tmp_path / "logo-light.svg"
    dark_png = tmp_path / "logo-dark.png"
    old_png.write_bytes(_png_header(32, 32))
    old_svg.write_text("<svg></svg>", encoding="utf-8")
    dark_png.write_bytes(_png_header(32, 32))

    deleted = _delete_logo_files_for_mode("light")

    assert set(deleted) == {"logo-light.png", "logo-light.svg"}
    assert not old_png.exists()
    assert not old_svg.exists()
    assert dark_png.exists()


def test_logo_version_for_settings_value_only_accepts_current_canonical_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(branding, "BRANDING_DIR", tmp_path)
    logo = tmp_path / "logo-light.png"
    logo.write_bytes(_png_header(32, 32))

    version = logo_version_for_settings_value("logo-light.png")

    assert version
    assert logo_version_for_settings_value("../logo-light.png") == ""
    assert logo_version_for_settings_value("legacy-light.png") == ""
    assert logo_version_for_settings_value("logo-light.gif") == ""


def test_logo_delete_uses_capped_json_body_reader(monkeypatch):
    called = {}
    monkeypatch.setattr(branding, "_delete_logo_files_for_mode", lambda mode: called.setdefault("mode", mode) or [])
    handler = SimpleNamespace(
        headers={"Content-Length": "17"},
        rfile=BytesIO(b'{"mode":"light"}'),
        sent_headers={},
        status=None,
        body=bytearray(),
    )

    class Writer:
        def write(self, data):
            handler.body.extend(data)

    handler.wfile = Writer()
    handler.send_response = lambda status: setattr(handler, "status", status)
    handler.send_header = lambda key, value: handler.sent_headers.setdefault(key, value)
    handler.end_headers = lambda: None

    branding.handle_logo_delete(handler)
    assert handler.status == 200
    assert called["mode"] == "light"


def test_logo_delete_rejects_oversized_content_length_without_reading():
    class RejectRead:
        def read(self, _length):
            raise AssertionError("oversized delete body must be rejected before reading")

    handler = SimpleNamespace(
        headers={"Content-Length": str(25 * 1024 * 1024)},
        rfile=RejectRead(),
        sent_headers={},
        status=None,
        body=bytearray(),
        close_connection=False,
    )

    class Writer:
        def write(self, data):
            handler.body.extend(data)

    handler.wfile = Writer()
    handler.send_response = lambda status: setattr(handler, "status", status)
    handler.send_header = lambda key, value: handler.sent_headers.setdefault(key, value)
    handler.end_headers = lambda: None

    branding.handle_logo_delete(handler)
    assert handler.status == 400
    assert handler.close_connection is True
    assert b"Request body too large" in bytes(handler.body)


def test_logo_upload_rejects_oversized_multipart_length_without_reading():
    class RejectRead:
        def read(self, _length):
            raise AssertionError("oversized logo upload must be rejected before reading")

    handler = SimpleNamespace(
        headers={
            "Content-Type": "multipart/form-data; boundary=logo-boundary",
            "Content-Length": str(branding._MAX_LOGO_UPLOAD_REQUEST_BYTES + 1),
        },
        rfile=RejectRead(),
        sent_headers={},
        status=None,
        body=bytearray(),
    )

    class Writer:
        def write(self, data):
            handler.body.extend(data)

    handler.wfile = Writer()
    handler.send_response = lambda status: setattr(handler, "status", status)
    handler.send_header = lambda key, value: handler.sent_headers.setdefault(key, value)
    handler.end_headers = lambda: None

    branding.handle_logo_upload(handler)
    assert handler.status == 413
    assert b"Logo must be PNG, SVG, or ICO" in bytes(handler.body)


def test_custom_logo_dom_hooks_exist():
    html = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")

    assert "app-titlebar-custom-logo" in html
    assert "empty-custom-logo" in html
    assert html.count("custom-logo-img") >= 2
    assert "<label>Logo</label>" in html
    assert "<label>Avatar</label>" not in html
    assert "logo-upload-grid" in html
    assert "PNG, SVG, or ICO. Max 256&times;256 px and 200 KB." in html


def test_custom_logo_favicon_uses_resolved_theme_variant():
    js = (Path(__file__).parents[1] / "static" / "boot.js").read_text(encoding="utf-8")

    assert "function customLogoAssetUrl" in js
    assert "window._customLogoThemeVersion" in js
    assert "s.custom_logo_light_version||''" in js
    assert "s.custom_logo_dark_version||''" in js
    assert "settings&&settings.custom_logo_light_version" in js
    assert "typeof event.matches==='boolean'?event.matches" in js
    assert "function _applyCachedCustomLogo" in js
    assert "hermes-custom-logo-state" in js
    assert "custom_logo_dark_mode: !!window._customLogoDarkMode" in js
    assert "document.getElementById('settingsCustomLogoDarkMode')" not in js
    assert "window.matchMedia('(prefers-color-scheme:dark)').matches" in js
    assert "dataset.customLogoMode" in js
    assert "function _customLogoNeedsSystemPoll" in js
    assert "if(_systemThemeMq&&_customLogoNeedsSystemPoll())" in js
    assert "if(typeof _systemThemeMq.addEventListener==='function')" in js
    assert "else if(typeof _systemThemeMq.addListener==='function')" in js
    assert "_setFavicon(src);" in js
    assert "_restoreDefaultFavicons();" in js
    assert "_setFavicon(lightSrc);" not in js
    assert "_setFavicon('static/favicon.svg')" not in js
    assert "window._customLogoDarkMode" in js


def test_custom_logo_disable_and_load_error_restore_defaults():
    js = (Path(__file__).parents[1] / "static" / "boot.js").read_text(encoding="utf-8")

    assert "function _restoreDefaultFavicons()" in js
    assert "href:'static/favicon.svg'" in js
    assert "href:'static/favicon-32.png'" in js
    assert "href:'static/favicon.ico'" in js
    assert "href:'static/apple-touch-icon.png'" in js
    assert "img.onerror=function()" in js
    assert "if(img.dataset.customLogoSrc===src) _handleCustomLogoLoadError(src);" in js
    assert "if(document.documentElement.dataset.customLogoSrc!==failedSrc) return;" in js
    assert "document.documentElement.removeAttribute('data-custom-logo-src');" in js


def test_custom_logo_upload_cache_busting_contract():
    root = Path(__file__).parents[1]
    branding = (root / "api" / "branding.py").read_text(encoding="utf-8")
    routes = (root / "api" / "routes.py").read_text(encoding="utf-8")
    panels = (root / "static" / "panels.js").read_text(encoding="utf-8")

    assert '"version": version' in branding
    assert "_resolve_logo_path" not in branding
    assert '"deleted": deleted' in branding
    assert "_delete_logo_files_for_mode(mode)" in branding
    assert "logo_version_for_settings_value" in routes
    assert 'Cache-Control", "no-store, max-age=0"' in routes
    assert 'X-Content-Type-Options", "nosniff"' in routes
    assert 'Content-Security-Policy", "sandbox"' in routes
    assert "window._customLogoLightVersion=version" in panels
    assert "window._customLogoDarkVersion=version" in panels
    assert "settings.custom_logo_light_version || ''" in panels
    assert "settings.custom_logo_dark_version || ''" in panels
    assert '_cacheCustomLogoState==="function"' in panels
    assert "function _setLogoPreviewFile" in panels
    assert "URL.createObjectURL(file)" in panels
    assert "_setLogoPreviewFile(preview,file);" in panels
    assert "_restoreLogoPreview(preview,previousPath,mode);" in panels


def test_branding_svg_response_is_sandboxed(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "BRANDING_DIR", tmp_path)
    logo = tmp_path / "logo-light.svg"
    logo.write_text("<svg></svg>", encoding="utf-8")

    class Handler:
        def __init__(self):
            self.headers = {}
            self.sent_headers = {}
            self.status = None
            self.body = bytearray()

            class Writer:
                def __init__(self, outer):
                    self.outer = outer

                def write(self, data):
                    self.outer.body.extend(data)

            self.wfile = Writer(self)

        def send_response(self, status):
            self.status = status

        def send_header(self, key, value):
            self.sent_headers[key] = value

        def end_headers(self):
            pass

    handler = Handler()

    assert routes._serve_branding(handler, SimpleNamespace(path="/branding/logo-light.svg"))
    assert handler.status == 200
    assert handler.sent_headers["Content-Type"] == "image/svg+xml; charset=utf-8"
    assert handler.sent_headers["Content-Security-Policy"] == "sandbox"
    assert handler.sent_headers["X-Content-Type-Options"] == "nosniff"
