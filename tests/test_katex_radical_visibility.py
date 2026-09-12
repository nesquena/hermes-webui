"""KaTeX radical rendering robustness.

The √ radical is drawn from two pieces: the SVG sign/overbar (which inherits
its color from ``currentColor``) and the glyph in the KaTeX_Main webfont.
Two failure modes made the radical invisible on dark themes:

1. The MathML accessibility layer (a duplicate of the equation) can surface
   visually when its ``clip`` hiding rule is overridden; it renders with thin
   system-font radicals that vanish on dark backgrounds.
2. The service worker precaches KaTeX's CSS/JS but not its webfonts, so a
   flaky connection or a cache miss falls back to the system √ glyph.

These tests pin the guards that prevent both failures. The SVG fill color
itself is owned by the vendored ``katex.min.css`` (``.katex svg
{fill:currentColor}``) and is exercised at the browser level by
``test_katex_radical_browser.py``.
"""
from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
SW_JS = (REPO / "static" / "sw.js").read_text(encoding="utf-8")
KATEX_DIR = REPO / "static" / "vendor" / "katex" / "0.16.22"


def test_style_pins_mathml_accessibility_layer_to_sr_only():
    """The MathML duplicate must never surface visually, even if the vendored
    katex.min.css clip rule is overridden by a theme or a user stylesheet."""
    assert ".msg-body .katex-mathml," in STYLE_CSS
    assert "clip:rect(1px,1px,1px,1px) !important" in STYLE_CSS
    assert "clip-path:inset(50%) !important" in STYLE_CSS


def test_sw_decouples_katex_fonts_from_atomic_shell_precache():
    """KaTeX webfonts must not ride the atomic SHELL_ASSETS addAll batch:
    cache.addAll is all-or-nothing, so one failed font request would discard
    the whole shell pre-cache. Fonts are pre-cached best-effort per font."""
    fonts = sorted(p.name for p in (KATEX_DIR / "fonts").glob("*.woff2"))
    assert fonts, "no vendored KaTeX woff2 fonts found"

    # Fonts live in their own list, separate from the atomic shell batch.
    assert "const FONT_ASSETS = [" in SW_JS
    for font in fonts:
        assert f"./static/vendor/katex/0.16.22/fonts/{font}" in SW_JS, (
            f"missing {font} in sw.js FONT_ASSETS"
        )

    shell_start = SW_JS.index("const SHELL_ASSETS = [")
    shell_end = SW_JS.index("];", shell_start)
    shell_block = SW_JS[shell_start:shell_end]
    for font in fonts:
        assert f"fonts/{font}" not in shell_block, (
            f"{font} must not be in the atomic SHELL_ASSETS batch"
        )

    # Per-font failure tolerance during install.
    assert "Promise.allSettled(FONT_ASSETS.map((font) => cache.add(font)))" in SW_JS

    # The fetch handler still serves fonts from the shell cache (network-first
    # with cache fallback).
    assert "!FONT_ASSETS.includes(shellPath)" in SW_JS
