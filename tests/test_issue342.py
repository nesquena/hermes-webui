"""
Tests for GitHub issue #342: auto-link plain URLs in chat messages.

These are structural tests that verify the fix is present in static/ui.js
without requiring a running server or JavaScript engine.
"""
import os
import re

UI_JS = os.path.join(os.path.dirname(__file__), '..', 'static', 'ui.js')


def read_ui_js():
    with open(UI_JS, 'r') as f:
        return f.read()


def test_autolink_comment_present():
    """The Autolink comment should be present in renderMd() to document the feature."""
    content = read_ui_js()
    assert 'Autolink: convert plain URLs' in content, (
        "Expected 'Autolink: convert plain URLs' comment not found in static/ui.js. "
        "Did the autolink pass get added?"
    )


def test_autolink_regex_in_rendermd():
    """The autolink regex pattern (https?://) should appear in renderMd()."""
    content = read_ui_js()
    # Locate the renderMd function body
    rendermd_start = content.find('function renderMd(raw){')
    assert rendermd_start != -1, "renderMd function not found in ui.js"
    # Find the closing brace after renderMd (look for the autolink pattern within it)
    rendermd_body = content[rendermd_start:rendermd_start + 10000]
    assert 'https?:\\/\\/' in rendermd_body, (
        "Autolink regex (https?:\\/\\/) not found inside renderMd() body."
    )


def test_autolink_uses_esc_for_xss_safety():
    """The autolink code must use esc() to escape the display text of URLs, preventing XSS.
    Note: esc() is intentionally NOT applied to the href value (that would corrupt & in
    query strings). It IS applied to the visible link text (esc(clean)) to prevent XSS."""
    content = read_ui_js()
    # Find the autolink section (between the SAFE_TAGS pass and paragraph wrap)
    autolink_idx = content.find('// Autolink: convert plain URLs')
    assert autolink_idx != -1, "Autolink comment not found in ui.js"
    # Extract the autolink block (next ~600 chars after the comment)
    autolink_block = content[autolink_idx:autolink_idx + 600]
    # esc() must be used on the visible link text to prevent XSS
    assert 'esc(clean)' in autolink_block, (
        "Autolink block should use esc(clean) for the link display text (XSS safety), "
        "but it was not found."
    )
    # esc() must NOT be used on the href value — that breaks URLs containing &
    assert 'href="${esc(clean)}"' not in autolink_block, (
        "Autolink block should use href=\"${clean}\" (not esc'd) to preserve & in query strings."
    )


def test_autolink_in_inline_md():
    """The autolink pass should also be present inside the inlineMd() helper."""
    content = read_ui_js()
    # Find inlineMd function
    inline_start = content.find('function inlineMd(t){')
    assert inline_start != -1, "inlineMd function not found in ui.js"
    # Find closing brace of inlineMd by looking for 'return t;' followed by '}'
    inline_end = content.find('return t;\n  }', inline_start)
    assert inline_end != -1, "Could not locate end of inlineMd function"
    inline_body = content[inline_start:inline_end + 20]
    assert 'https?:\\/\\/' in inline_body, (
        "Autolink regex not found inside inlineMd() — plain URLs in list items "
        "and blockquotes won't be autolinked."
    )


def test_autolink_after_safe_tags_pass():
    """The autolink pass must come AFTER the SAFE_TAGS escape pass (ordering matters)."""
    content = read_ui_js()
    safe_tags_idx = content.find('s=s.replace(/<\\/?[a-z][^>]*>/gi,tag=>SAFE_TAGS.test(tag)?tag:esc(tag));')
    autolink_idx = content.find('// Autolink: convert plain URLs')
    parts_idx = content.find('const parts=s.split(/\\n{2,}/);')
    assert safe_tags_idx != -1, "SAFE_TAGS pass not found"
    assert autolink_idx != -1, "Autolink pass not found"
    assert parts_idx != -1, "Paragraph-wrap parts line not found"
    assert safe_tags_idx < autolink_idx < parts_idx, (
        f"Ordering wrong: SAFE_TAGS at {safe_tags_idx}, autolink at {autolink_idx}, "
        f"parts (paragraph wrap) at {parts_idx}. "
        "Autolink must come between SAFE_TAGS pass and paragraph wrap."
    )


def test_autolink_target_blank_and_rel():
    """Autolinked URLs should open in a new tab with rel=noopener for security."""
    content = read_ui_js()
    autolink_idx = content.find('// Autolink: convert plain URLs')
    assert autolink_idx != -1, "Autolink comment not found"
    # Use a larger window to account for the stash preamble added by the fix
    autolink_block = content[autolink_idx:autolink_idx + 700]
    assert 'target="_blank"' in autolink_block, (
        'Autolinked URLs should have target="_blank"'
    )
    assert 'rel="noopener"' in autolink_block, (
        'Autolinked URLs should have rel="noopener" for security'
    )


def test_safe_tags_includes_anchor():
    """SAFE_TAGS regex must include 'a' so <a> tags from autolink are not escaped."""
    content = read_ui_js()
    # Find the SAFE_TAGS definition line — the pattern contains slashes so we
    # search for the line directly rather than extracting the regex literal.
    safe_tags_line = None
    for line in content.splitlines():
        if 'const SAFE_TAGS=' in line:
            safe_tags_line = line
            break
    assert safe_tags_line is not None, "SAFE_TAGS const definition not found in ui.js"
    # The pattern should include 'a' as a tag alternative (e.g. |a|)
    assert '|a|' in safe_tags_line or '|a)' in safe_tags_line, (
        f"SAFE_TAGS line does not include 'a' tag — "
        "<a> tags emitted by autolink would be escaped!\n"
        f"Line: {safe_tags_line}"
    )
