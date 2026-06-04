"""Regression coverage for custom logo upload validation and DOM hooks."""

import struct
from pathlib import Path

import pytest

from api import branding
from api.branding import (
    _branding_version,
    _delete_logo_files_for_mode,
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
    assert "_setFavicon(lightSrc);" not in js
    assert "window._customLogoDarkMode" in js


def test_custom_logo_upload_cache_busting_contract():
    root = Path(__file__).parents[1]
    branding = (root / "api" / "branding.py").read_text(encoding="utf-8")
    routes = (root / "api" / "routes.py").read_text(encoding="utf-8")
    panels = (root / "static" / "panels.js").read_text(encoding="utf-8")

    assert '"version": version' in branding
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
