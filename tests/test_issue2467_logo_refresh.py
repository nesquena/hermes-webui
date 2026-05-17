from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_modern_geometric_logo_used_for_favicon_and_empty_state():
    favicon = (ROOT / "static" / "favicon.svg").read_text()
    index = (ROOT / "static" / "index.html").read_text()

    assert "Hermes WebUI modern geometric caduceus logo" in favicon
    assert "Hermes WebUI modern geometric caduceus logo" in index
    assert "hermes-empty-logo-gold" in index
    assert 'M18 23 L32 14 L46 23' in favicon
    assert 'M18 23 L32 14 L46 23' in index
    assert 'M30 18 C24 14' not in favicon
    assert 'M30 18 C24 14' not in index


def test_generated_raster_logo_assets_are_present_and_valid():
    expected = {
        "favicon-32.png": (32, 32),
        "favicon-192.png": (192, 192),
        "favicon-512.png": (512, 512),
        "apple-touch-icon.png": (512, 512),
    }
    for name, (width, height) in expected.items():
        data = (ROOT / "static" / name).read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        assert int.from_bytes(data[16:20], "big") == width
        assert int.from_bytes(data[20:24], "big") == height

    ico = (ROOT / "static" / "favicon.ico").read_bytes()
    assert ico[:4] == b"\x00\x00\x01\x00"
