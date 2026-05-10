"""
Sprint 35 Tests: Breadcrumb nav + wider panel + responsive message width (PR #302).

Covers:
  1. PANEL_MAX raised from 500 to 1200 in boot.js
  2. Responsive .messages-inner breakpoints in style.css (no hardcoded 800px)
  3. renderFileBreadcrumb() function exists in workspace.js
  4. renderFileBreadcrumb() is called from openFile()
  5. clearPreview() calls renderBreadcrumb() to restore dir breadcrumb
  6. Breadcrumb segments use correct CSS classes
  7. breadcrumbBar element exists in index.html
  8. Breadcrumb CSS rules exist in style.css
"""

import pathlib

REPO = pathlib.Path(__file__).parent.parent


def read(path):
    return (REPO / path).read_text(encoding="utf-8")


# ── 1. PANEL_MAX raised ──────────────────────────────────────────────────────


def test_panel_max_raised_to_1200():
    """PANEL_MAX must be 1200 (raised from 500) for wider right panel."""
    src = read("static/boot.js")
    assert "PANEL_MAX=1200" in src or "PANEL_MAX = 1200" in src, (
        "PANEL_MAX was not raised to 1200 — right panel cannot be widened on ultrawide screens"
    )


def test_panel_max_is_not_500():
    """Old PANEL_MAX=500 must no longer be present."""
    src = read("static/boot.js")
    assert "PANEL_MAX=500" not in src and "PANEL_MAX = 500" not in src, (
        "Old PANEL_MAX=500 still present — right panel width not updated"
    )


# ── 2. Responsive messages-inner ─────────────────────────────────────────────


def test_messages_inner_has_responsive_breakpoints():
    """style.css must have @media breakpoints for .messages-inner."""
    css = read("static/style.css")
    assert "min-width:1400px" in css or "min-width: 1400px" in css, (
        "Missing @media(min-width:1400px) breakpoint for .messages-inner"
    )
    assert "min-width:1800px" in css or "min-width: 1800px" in css, (
        "Missing @media(min-width:1800px) breakpoint for .messages-inner"
    )


def test_messages_inner_no_hardcoded_800px():
    """The base .messages-inner rule must not hardcode max-width:800px."""
    css = read("static/style.css")
    # Find the .messages-inner base rule (not inside a @media block)
    # It should not have max-width:800px on the same line
    for line in css.splitlines():
        if ".messages-inner{" in line and "max-width:800px" in line:
            raise AssertionError(
                "Base .messages-inner still has hardcoded max-width:800px — "
                "responsive breakpoints not applied"
            )


def test_messages_inner_breakpoint_values():
    """The breakpoints should expand max-width at 1400px and 1800px."""
    css = read("static/style.css")
    assert "max-width:1100px" in css or "max-width: 1100px" in css, (
        "Expected max-width:1100px at 1400px breakpoint"
    )
    assert "max-width:1200px" in css or "max-width: 1200px" in css, (
        "Expected max-width:1200px at 1800px breakpoint"
    )


# ── 3–6. Breadcrumb navigation ───────────────────────────────────────────────


def test_render_file_breadcrumb_function_exists():
    """workspace.js must expose renderFileBreadcrumb()."""
    src = read("static/workspace.js")
    assert "function renderFileBreadcrumb" in src, (
        "renderFileBreadcrumb() not defined in workspace.js"
    )


def test_render_file_breadcrumb_called_from_open_file():
    """openFile() must call renderFileBreadcrumb(path) to show path segments."""
    src = read("static/workspace.js")
    assert "renderFileBreadcrumb(path)" in src, (
        "openFile() does not call renderFileBreadcrumb(path)"
    )


def test_breadcrumb_has_root_segment():
    """renderFileBreadcrumb must add a root '~' segment."""
    src = read("static/workspace.js")
    idx = src.find("function renderFileBreadcrumb")
    block = src[idx : idx + 800]
    assert "'~'" in block or '"~"' in block, (
        "renderFileBreadcrumb missing root '~' segment"
    )


def test_breadcrumb_segments_use_correct_classes():
    """Breadcrumb segments must use breadcrumb-seg breadcrumb-link/current classes."""
    src = read("static/workspace.js")
    assert "breadcrumb-seg" in src, "breadcrumb-seg class not used"
    assert "breadcrumb-link" in src, "breadcrumb-link class not used"
    assert "breadcrumb-current" in src, "breadcrumb-current class not used"


def test_clear_preview_calls_render_breadcrumb():
    """clearPreview() in boot.js must call renderBreadcrumb() to restore dir view."""
    src = read("static/boot.js")
    # Find clearPreview and check renderBreadcrumb is called nearby
    idx = src.find("function clearPreview")
    assert idx != -1, "clearPreview not found in boot.js"
    block = src[idx : idx + 600]
    assert "renderBreadcrumb" in block, (
        "clearPreview() does not call renderBreadcrumb() — "
        "directory breadcrumb won't restore after closing file preview"
    )


# ── 7. HTML markup ───────────────────────────────────────────────────────────


def test_breadcrumb_bar_in_index_html():
    """index.html must have the breadcrumbBar element."""
    html = read("static/index.html")
    assert 'id="breadcrumbBar"' in html, (
        "breadcrumbBar element missing from index.html — "
        "renderFileBreadcrumb() has nowhere to render"
    )


# ── 8. Breadcrumb CSS ────────────────────────────────────────────────────────


def test_breadcrumb_css_rules_exist():
    """style.css must have breadcrumb CSS rules."""
    css = read("static/style.css")
    for selector in (".breadcrumb-seg", ".breadcrumb-link", ".breadcrumb-current"):
        assert selector in css, f"Missing CSS rule: {selector}"
