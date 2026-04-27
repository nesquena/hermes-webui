from pathlib import Path
import json

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTES = REPO_ROOT / "api" / "routes.py"
STATIC = REPO_ROOT / "static"


def test_index_declares_capy_favicons_and_apple_webclip_icon():
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    expected = [
        '<link rel="icon" type="image/png" sizes="16x16" href="static/capy-icon-16.png">',
        '<link rel="icon" type="image/png" sizes="32x32" href="static/capy-icon-32.png">',
        '<link rel="icon" type="image/png" sizes="180x180" href="static/capy-icon-180.png">',
        '<link rel="icon" type="image/png" sizes="512x512" href="static/capy-icon-512.png">',
        '<link rel="shortcut icon" href="static/favicon.ico">',
        '<link rel="apple-touch-icon" sizes="180x180" href="static/apple-touch-icon.png">',
        '<meta name="apple-mobile-web-app-title" content="Capy">',
    ]
    for snippet in expected:
        assert snippet in html


def test_manifest_uses_capy_name_and_installable_png_icon_set():
    manifest = json.loads((STATIC / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "Capy"
    assert manifest["short_name"] == "Capy"
    icons = {(icon["src"], icon["sizes"], icon["type"]) for icon in manifest["icons"]}
    assert ("static/capy-icon-192.png", "192x192", "image/png") in icons
    assert ("static/capy-icon-512.png", "512x512", "image/png") in icons
    assert any(icon["src"] == "static/capy-maskable-512.png" and icon.get("purpose") == "maskable" for icon in manifest["icons"])


def test_login_page_declares_capy_icons_for_bookmarks_before_auth():
    src = ROUTES.read_text(encoding="utf-8")
    assert 'href="/static/capy-icon-32.png"' in src
    assert 'href="/static/apple-touch-icon.png"' in src
    assert 'href="/manifest.json"' in src


def test_capy_icon_asset_files_exist():
    expected_files = [
        "capy-source.png",
        "capy-icon-16.png",
        "capy-icon-32.png",
        "capy-icon-180.png",
        "capy-icon-192.png",
        "capy-icon-512.png",
        "capy-maskable-512.png",
        "apple-touch-icon.png",
        "telegram-capy-avatar.png",
        "favicon.ico",
    ]
    for name in expected_files:
        path = STATIC / name
        assert path.exists(), f"missing {path}"
        assert path.stat().st_size > 0, f"empty {path}"
