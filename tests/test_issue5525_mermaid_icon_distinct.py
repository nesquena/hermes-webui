"""Regression coverage for #5525 — Mermaid toolbar fit/fullscreen icons must differ.

Reported alongside the (already-fixed on master) mobile height:0 issue: the
"fit to screen" and "fullscreen" toolbar icons look identical. Confirmed by
inspection — the old `fullscreen` glyph was `fit`'s four corner-brackets path
drawn twice (`...M9 4H4v5M15 4h5v5...`), so it rendered pixel-identical to `fit`.

Fix: give `fullscreen` a distinct glyph (corner frame + outward diagonal expand
arrows) so the two controls are visually distinguishable.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "static" / "ui.js"


def _icon(kind: str) -> str:
    """Extract the SVG string for a given _mermaidViewerIcon kind."""
    src = UI.read_text(encoding="utf-8")
    # Match `kind: '<svg ...></svg>',` inside the icons map.
    m = re.search(rf"\b{kind}:\s*'(<svg.*?</svg>)'", src)
    assert m, f"icon {kind!r} not found in _mermaidViewerIcon"
    return m.group(1)


def test_fit_and_fullscreen_icons_are_distinct():
    """The two toolbar glyphs must not be byte-identical (they were: #5525)."""
    fit = _icon("fit")
    fullscreen = _icon("fullscreen")
    assert fit and fullscreen
    assert fit != fullscreen, (
        "fit and fullscreen mermaid toolbar icons are identical — users can't "
        "tell the controls apart (#5525)"
    )


def test_fullscreen_icon_has_distinct_expand_arrows():
    """Fullscreen keeps a corner frame but adds outward diagonal expand arrows,
    which `fit` does not have — a concrete distinguishing feature."""
    fit = _icon("fit")
    fullscreen = _icon("fullscreen")
    # Diagonal expand strokes from the corners (e.g. "M4 4l5 5") — present in
    # fullscreen, absent from fit's pure corner-bracket path.
    assert re.search(r"M4 4l5 5|l5 5|l-5 5|l5 -5", fullscreen), (
        "fullscreen icon should carry outward diagonal expand arrows"
    )
    assert not re.search(r"l5 5|l-5 5", fit), (
        "fit icon should stay corner-brackets only (no diagonal arrows)"
    )
