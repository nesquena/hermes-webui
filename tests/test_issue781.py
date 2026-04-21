"""
Tests for issue #781 — duplicate X close button in workspace preview header
on window resize below 900px breakpoint.

Verifies that:
  - .close-preview is hidden (display:none) inside the @media (max-width:900px) block
  - .mobile-close-btn is shown (display:flex) inside the same @media block
Both rules must appear inside the same @media(max-width:900px) block so that
at mobile widths only the mobile-close-btn is visible.
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


def _strip_media_blocks(css):
    """Remove all @media {...} blocks from CSS, returning base rules only."""
    result = []
    i = 0
    while i < len(css):
        # Look for @media keyword
        m = re.search(r"@media\b", css[i:])
        if not m:
            result.append(css[i:])
            break
        # Append everything before this @media
        result.append(css[i : i + m.start()])
        # Find the opening brace of this @media block
        brace_start = css.index("{", i + m.start())
        depth = 0
        j = brace_start
        while j < len(css):
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    i = j + 1
                    break
            j += 1
        else:
            break
    return "".join(result)


_MEDIA_900_PATTERN = r"@media\s*\(\s*max-width\s*:\s*900px\s*\)"


def test_mobile_close_btn_displayed_in_900px_block():
    """mobile-close-btn must be display:flex inside the 900px media query."""
    css = _load_css()
    block = _extract_media_block(css, _MEDIA_900_PATTERN)
    assert ".mobile-close-btn" in block, (
        ".mobile-close-btn rule is missing from @media(max-width:900px) block"
    )
    rule_match = re.search(r"\.mobile-close-btn\s*\{([^}]*)\}", block)
    assert rule_match, ".mobile-close-btn rule body not found in 900px block"
    assert "display:flex" in rule_match.group(1).replace(" ", ""), (
        ".mobile-close-btn should have display:flex in the 900px media query"
    )


def test_close_preview_hidden_in_900px_block():
    """.close-preview must be display:none inside the 900px media query (fix for #781)."""
    css = _load_css()
    block = _extract_media_block(css, _MEDIA_900_PATTERN)
    assert ".close-preview" in block, (
        ".close-preview rule is missing from @media(max-width:900px) block — "
        "the duplicate-button fix (#781) may have been reverted"
    )
    rule_match = re.search(r"\.close-preview\s*\{([^}]*)\}", block)
    assert rule_match, ".close-preview rule body not found in 900px block"
    assert "display:none" in rule_match.group(1).replace(" ", ""), (
        ".close-preview should have display:none in the 900px media query to hide "
        "the duplicate desktop X button at mobile widths"
    )


def test_both_rules_in_same_media_block():
    """Both .close-preview and .mobile-close-btn must appear in the same 900px block."""
    css = _load_css()
    block = _extract_media_block(css, _MEDIA_900_PATTERN)
    assert ".mobile-close-btn" in block, (
        ".mobile-close-btn missing from @media(max-width:900px) block"
    )
    assert ".close-preview" in block, (
        ".close-preview missing from @media(max-width:900px) block"
    )


def test_close_preview_visible_outside_media_query():
    """Outside the media query, .close-preview must NOT be display:none
    (it should remain visible on desktop)."""
    css = _load_css()
    base_css = _strip_media_blocks(css)
    close_rules = re.findall(r"\.close-preview\s*\{([^}]*)\}", base_css)
    for rule_body in close_rules:
        assert "display:none" not in rule_body.replace(" ", ""), (
            ".close-preview must not be hidden in base (desktop) CSS"
        )
