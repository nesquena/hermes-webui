"""
Tests for issue #781 — duplicate X close button in workspace preview header.

The fix: a single btnClearPreview (.close-preview) is the only close button,
visible on all devices. The mobile-close-btn element was removed entirely.

Verifies that:
  - .close-preview is NOT hidden by any media query (visible everywhere)
  - .mobile-close-btn has no CSS rules remaining (element removed from HTML)
"""

import re
import os

CSS_PATH = os.path.join(os.path.dirname(__file__), "..", "static", "style.css")


def _load_css():
    with open(CSS_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _extract_media_block(css, media_query_pattern):
    """Extract the content of a @media block by tracking brace depth.

    Returns the inner text (between the outermost braces) of the first
    @media block matching media_query_pattern (a regex applied to the @media
    line itself).
    """
    # Find the start of the @media declaration
    m = re.search(media_query_pattern, css)
    assert m, f"Media query matching {media_query_pattern!r} not found in style.css"

    # Walk forward from the opening brace to find its matching close brace
    start = css.index("{", m.start())
    depth = 0
    for i in range(start, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start + 1 : i]  # content between { and }
    raise AssertionError("Unmatched brace in CSS after @media block")


_MEDIA_900_PATTERN = r"@media\s*\(\s*max-width\s*:\s*900px\s*\)"


def test_close_preview_not_hidden_in_900px_block():
    """The single close button (.close-preview) must NOT be hidden in any media query."""
    css = _load_css()
    block = _extract_media_block(css, _MEDIA_900_PATTERN)
    assert ".close-preview" not in block, (
        ".close-preview must not appear in @media(max-width:900px) block — "
        "the single X button should be visible on all devices"
    )


def test_mobile_close_btn_not_in_css():
    """mobile-close-btn CSS rules should have been removed entirely."""
    css = _load_css()
    assert ".mobile-close-btn" not in css, (
        ".mobile-close-btn CSS rule still present — the element was removed "
        "from HTML so its styles should be cleaned up too"
    )


def test_close_preview_visible_in_base_css():
    """Outside media queries, .close-preview must NOT be display:none."""
    css = _load_css()
    # Simple check: find all .close-preview rules and ensure none set display:none
    for m in re.finditer(r"\.close-preview\s*\{([^}]*)\}", css):
        assert "display:none" not in m.group(1).replace(" ", ""), (
            ".close-preview must not be hidden by any CSS rule"
        )
