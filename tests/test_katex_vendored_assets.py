"""Regression coverage for vendored KaTeX assets."""
from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).parent.parent
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
VENDOR_DIR = REPO / "static" / "vendor" / "katex" / "0.16.22"


def test_index_loads_vendored_katex_css_instead_of_cdn():
    assert "static/vendor/katex/0.16.22/katex.min.css" in INDEX_HTML
    assert "https://cdn.jsdelivr.net/npm/katex@0.16.22/dist/katex.min.css" not in INDEX_HTML


def test_runtime_loads_vendored_katex_js_instead_of_cdn():
    assert "static/vendor/katex/0.16.22/katex.min.js" in UI_JS
    assert "https://cdn.jsdelivr.net/npm/katex@0.16.22/dist/katex.min.js" not in UI_JS


def test_vendored_katex_fonts_are_present_for_local_font_src():
    css = (VENDOR_DIR / "katex.min.css").read_text(encoding="utf-8")
    assert "url(fonts/KaTeX_Main-Regular.woff2)" in css
    assert (VENDOR_DIR / "katex.min.js").is_file()
    assert (VENDOR_DIR / "fonts" / "KaTeX_Main-Regular.woff2").is_file()
    assert (VENDOR_DIR / "fonts" / "KaTeX_AMS-Regular.woff2").is_file()
