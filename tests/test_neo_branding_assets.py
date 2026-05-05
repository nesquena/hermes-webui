"""Neo WebUI: brand assets and PWA metadata."""

import json
from pathlib import Path


REPO = Path(__file__).parent.parent
STATIC = REPO / "static"
BRAND = STATIC / "brand"
MANIFEST = json.loads((STATIC / "manifest.json").read_text(encoding="utf-8"))
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def test_neo_brand_svg_assets_exist_with_accessible_titles():
    for name in ("neo-avatar.svg", "neo-avatar-mono.svg", "neo-mark.svg"):
        path = BRAND / name
        assert path.exists(), f"Missing Neo brand asset: {path}"
        src = path.read_text(encoding="utf-8")
        assert "<svg" in src
        assert "<title" in src and "Neo" in src
        assert 'role="img"' in src
        assert "aria-labelledby=" in src


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def test_neo_favicon_uses_raster_assets_not_svg():
    assert not (STATIC / "favicon.svg").exists()
    assert 'href="static/favicon.svg"' not in INDEX_HTML
    assert all(icon["type"] != "image/svg+xml" for icon in MANIFEST["icons"])


def test_neo_png_and_ico_icons_are_present():
    favicon_16 = STATIC / "favicon-16.png"
    favicon_png = STATIC / "favicon-32.png"
    pwa_192 = STATIC / "favicon-192.png"
    pwa_512 = STATIC / "favicon-512.png"
    apple_icon = STATIC / "apple-touch-icon.png"
    favicon_ico = STATIC / "favicon.ico"

    assert _png_size(favicon_16) == (16, 16)
    assert _png_size(favicon_png) == (32, 32)
    assert _png_size(apple_icon) == (180, 180)
    assert _png_size(pwa_192) == (192, 192)
    assert _png_size(pwa_512) == (512, 512)
    assert favicon_ico.read_bytes().startswith(b"\x00\x00\x01\x00")


def test_manifest_uses_neo_identity_and_icons():
    assert MANIFEST["name"] == "Neo WebUI"
    assert MANIFEST["short_name"] == "Neo"
    assert "Neo" in MANIFEST["description"]
    assert MANIFEST["theme_color"].lower() == "#00e5ff"

    icon_srcs = {icon["src"] for icon in MANIFEST["icons"]}
    assert "static/favicon-16.png" in icon_srcs
    assert "static/favicon-32.png" in icon_srcs
    assert "static/apple-touch-icon.png" in icon_srcs
    assert "static/favicon-192.png" in icon_srcs
    assert "static/favicon-512.png" in icon_srcs


def test_index_initial_chrome_uses_neo_assets():
    assert "<title>Neo</title>" in INDEX_HTML
    assert 'apple-mobile-web-app-title" content="Neo"' in INDEX_HTML
    assert 'href="static/favicon-16.png"' in INDEX_HTML
    assert 'href="static/favicon-32.png"' in INDEX_HTML
    assert 'href="static/favicon.ico"' in INDEX_HTML
    assert 'href="static/apple-touch-icon.png"' in INDEX_HTML
    assert 'src="static/brand/neo-mark.svg"' in INDEX_HTML
    # empty-state logo migrated from neo-avatar.svg to neo-ico.png (visual refinement)
    assert 'src="static/brand/neo-ico.png"' in INDEX_HTML
    assert "Hermes caduceus" not in INDEX_HTML
    assert "Message Neo" in INDEX_HTML
