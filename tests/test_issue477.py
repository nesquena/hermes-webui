"""Tests for fix #477: KaTeX font-src CSP fix."""

import pathlib

REPO = pathlib.Path(__file__).parent.parent
HELPERS_PY = (REPO / "api" / "helpers.py").read_text(encoding="utf-8")


def test_font_src_allows_jsdelivr():
    """font-src must include cdn.jsdelivr.net for KaTeX fonts."""
    assert "font-src 'self' data: https://cdn.jsdelivr.net" in HELPERS_PY, (
        "api/helpers.py CSP must allow cdn.jsdelivr.net in font-src "
        "so KaTeX math rendering fonts load without console errors."
    )


def test_font_src_still_allows_self_and_data():
    """font-src must still allow self and data: (used by other font assets)."""
    assert "'self'" in HELPERS_PY.split("font-src")[1].split(";")[0]
    assert "data:" in HELPERS_PY.split("font-src")[1].split(";")[0]


def test_script_src_already_allows_jsdelivr():
    """script-src already allows cdn.jsdelivr.net — font-src should too."""
    assert "https://cdn.jsdelivr.net" in HELPERS_PY.split("font-src")[0], (
        "script-src should already allow cdn.jsdelivr.net (KaTeX JS)"
    )
