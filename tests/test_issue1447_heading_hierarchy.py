"""Regression tests for issue #1447 — markdown heading visual hierarchy.

Cygnus reported (Discord, May 1 2026, relayed by @AvidFuturist):
    "Headings seem to be missing across the board in Hermes. They're there,
     but all plaintext. They get lost so easily in all the plaintext."

Pre-fix sizes (smaller-than-or-equal to body 14px):
  h1 18px, h2 16px, h3 14px (= body), h4 13px, h5 12px, h6 11px.

Post-fix sizes (clear hierarchy above body 14px):
  h1 24px, h2 20px, h3 17px, h4 15px, h5 14px (uppercase + tracked), h6 13px (uppercase + tracked + muted).

These tests pin:
  - Each heading level has a meaningful size delta from the body and from the
    next-deeper level
  - h1 and h2 carry a bottom border for "section title" affordance
  - h5 and h6 carry uppercase + letter-spacing for "label-style" affordance
  - The .preview-md (file preview pane) sizes match .msg-body so a markdown
    file preview and a chat message look the same
  - The data-font-size small/large overrides scale proportionally
"""

from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def _font_size(scope: str, level: str) -> int:
    """Extract the integer font-size (px) for the BARE `<scope> <level>` selector.

    Anchors at the start of a line (after whitespace) so the data-font-size
    overrides like `[data-font-size="small"] .msg-body h1` are not matched.
    """
    # Match `^<whitespace><scope> <level>{...font-size:Npx...}` (whole rule on one line)
    pat = re.compile(
        rf"^\s*{re.escape(scope)}\s+{level}\s*\{{[^}}]*font-size:\s*(\d+)px",
        re.M,
    )
    m = pat.search(CSS)
    assert m, f"font-size not found for `{scope} {level}` (line-anchored bare selector)"
    return int(m.group(1))


# ── Hierarchy: each level larger than the next ───────────────────────────────


def test_msg_body_heading_sizes_form_clear_hierarchy():
    """h1 > h2 > h3 > h4 in size, with at least 2px between adjacent levels.

    h5 and h6 use uppercase + letter-spacing rather than larger size for their
    visual distinction (they're "label-style" headings), so they don't strictly
    need to be larger than h4 — but they must still be at least body size (14px).
    """
    h1 = _font_size(".msg-body", "h1")
    h2 = _font_size(".msg-body", "h2")
    h3 = _font_size(".msg-body", "h3")
    h4 = _font_size(".msg-body", "h4")
    h5 = _font_size(".msg-body", "h5")
    h6 = _font_size(".msg-body", "h6")

    assert h1 >= h2 + 3, f"h1 ({h1}) must be at least 3px larger than h2 ({h2})"
    assert h2 >= h3 + 2, f"h2 ({h2}) must be at least 2px larger than h3 ({h3})"
    assert h3 >= h4 + 2, f"h3 ({h3}) must be at least 2px larger than h4 ({h4})"
    # Body is 14px.
    assert h4 >= 14, f"h4 ({h4}) must not be smaller than body (14px)"
    assert h5 >= 14, (
        f"h5 ({h5}) must not be smaller than body (14px) — uppercase compensates"
    )
    assert h6 >= 13, (
        f"h6 ({h6}) must not be much smaller than body (uppercase compensates) — got {h6}"
    )
    # h3 must be visibly above body — Cygnus's specific complaint.
    assert h3 > 14, (
        f"h3 ({h3}) must be larger than body (14px) so it is visibly a heading"
    )


def test_msg_body_h1_and_h2_have_bottom_border():
    """h1 and h2 carry a bottom border for visible 'section title' affordance.

    This mirrors GitHub/Notion convention and the existing .preview-md h1 rule.
    """
    h1_match = re.search(
        r"\.msg-body\s+h1\s*\{[^}]*border-bottom:\s*1px\s+solid",
        CSS,
    )
    assert h1_match, ".msg-body h1 must have border-bottom: 1px solid"
    h2_match = re.search(
        r"\.msg-body\s+h2\s*\{[^}]*border-bottom:\s*1px\s+solid",
        CSS,
    )
    assert h2_match, ".msg-body h2 must have border-bottom: 1px solid"


def test_msg_body_h5_and_h6_use_label_style_affordance():
    """h5 and h6 use uppercase + letter-spacing rather than larger sizes."""
    h5_match = re.search(
        r"\.msg-body\s+h5\s*\{[^}]*text-transform:\s*uppercase[^}]*letter-spacing:",
        CSS,
    )
    assert h5_match, ".msg-body h5 must have text-transform:uppercase + letter-spacing"
    h6_match = re.search(
        r"\.msg-body\s+h6\s*\{[^}]*text-transform:\s*uppercase[^}]*letter-spacing:",
        CSS,
    )
    assert h6_match, ".msg-body h6 must have text-transform:uppercase + letter-spacing"


def test_msg_body_headings_use_strong_color_and_bold_weight():
    """All headings must use bold weight (700) and strong color, not light grey."""
    base_match = re.search(
        r"\.msg-body\s+h1,\s*\.msg-body\s+h2,\s*\.msg-body\s+h3,\s*\.msg-body\s+h4,"
        r"\s*\.msg-body\s+h5,\s*\.msg-body\s+h6\s*\{[^}]*font-weight:\s*700",
        CSS,
    )
    assert base_match, "Combined .msg-body h1..h6 selector must set font-weight:700"
    # Color must reference --strong (with --text fallback).
    color_match = re.search(
        r"\.msg-body\s+h1,\s*\.msg-body\s+h2,\s*\.msg-body\s+h3,\s*\.msg-body\s+h4,"
        r"\s*\.msg-body\s+h5,\s*\.msg-body\s+h6\s*\{[^}]*color:\s*var\(--strong",
        CSS,
    )
    assert color_match, "Combined heading selector must use color:var(--strong, ...)"


# ── preview-md sync: chat and file preview render headings the same ──────────


def test_preview_md_heading_sizes_match_msg_body():
    """Per the issue's 'companion fix' note: .preview-md heading sizes must mirror .msg-body
    so a markdown file preview and a chat message look identical."""
    for level in ("h1", "h2", "h3", "h4", "h5", "h6"):
        msg_size = _font_size(".msg-body", level)
        preview_size = _font_size(".preview-md", level)
        assert msg_size == preview_size, (
            f".preview-md {level} ({preview_size}px) must match .msg-body {level} ({msg_size}px)"
        )


def test_preview_md_has_h4_h5_h6_rules():
    """Pre-fix .preview-md only had h1-h3 rules. Post-fix must have all six."""
    for level in ("h4", "h5", "h6"):
        match = re.search(rf"\.preview-md\s+{level}\s*\{{[^}}]*font-size:\s*\d+px", CSS)
        assert match, f".preview-md {level} rule missing"


# ── data-font-size scaling: small/large stay proportional ────────────────────


def test_data_font_size_small_overrides_scale_with_new_defaults():
    """The data-font-size='small' h1 override must NOT be ≤ body 14px (the old 15px h1
    in small mode was effectively the same as body — the bug Cygnus complained about,
    just at small font-size).
    """
    # Walk h1..h6 small overrides
    small_h_sizes = {}
    for level in ("h1", "h2", "h3", "h4", "h5", "h6"):
        m = re.search(
            rf'data-font-size="small"\]\s*\.msg-body\s+{level}\s*\{{[^}}]*font-size:\s*(\d+)px',
            CSS,
        )
        if m:
            small_h_sizes[level] = int(m.group(1))
    # Must have all six.
    assert set(small_h_sizes.keys()) == {"h1", "h2", "h3", "h4", "h5", "h6"}, (
        f"data-font-size='small' missing override for some levels: got {sorted(small_h_sizes)}"
    )
    # Body in small mode is 12px.
    assert small_h_sizes["h1"] >= 18, f"small h1 too small: {small_h_sizes['h1']}"
    assert small_h_sizes["h2"] >= 16, f"small h2 too small: {small_h_sizes['h2']}"
    assert small_h_sizes["h3"] >= 14, f"small h3 too small: {small_h_sizes['h3']}"
    # Hierarchy preserved.
    assert small_h_sizes["h1"] > small_h_sizes["h2"]
    assert small_h_sizes["h2"] > small_h_sizes["h3"]
    assert small_h_sizes["h3"] > small_h_sizes["h4"]


def test_data_font_size_large_overrides_scale_with_new_defaults():
    """The data-font-size='large' h1 override must scale up proportionally with the new defaults."""
    large_h_sizes = {}
    for level in ("h1", "h2", "h3", "h4", "h5", "h6"):
        m = re.search(
            rf'data-font-size="large"\]\s*\.msg-body\s+{level}\s*\{{[^}}]*font-size:\s*(\d+)px',
            CSS,
        )
        if m:
            large_h_sizes[level] = int(m.group(1))
    assert set(large_h_sizes.keys()) == {"h1", "h2", "h3", "h4", "h5", "h6"}
    # Body in large mode is 16px. h1 must be a meaningful step above.
    assert large_h_sizes["h1"] >= 26, f"large h1 too small: {large_h_sizes['h1']}"
    assert large_h_sizes["h2"] >= 22, f"large h2 too small: {large_h_sizes['h2']}"
    assert large_h_sizes["h3"] >= 19, f"large h3 too small: {large_h_sizes['h3']}"
    # Hierarchy preserved.
    assert large_h_sizes["h1"] > large_h_sizes["h2"]
    assert large_h_sizes["h2"] > large_h_sizes["h3"]
    assert large_h_sizes["h3"] > large_h_sizes["h4"]


# ── Specific values from the issue spec ──────────────────────────────────────


def test_specific_heading_sizes_match_issue_spec():
    """Pin the exact spec'd sizes so unrelated CSS edits don't drift them."""
    assert _font_size(".msg-body", "h1") == 24
    assert _font_size(".msg-body", "h2") == 20
    assert _font_size(".msg-body", "h3") == 17
    assert _font_size(".msg-body", "h4") == 15
    # h5 and h6 use uppercase, so size is at body or just below.
    assert _font_size(".msg-body", "h5") == 14
    assert _font_size(".msg-body", "h6") == 13
