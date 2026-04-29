"""Regression tests for the project chip UI fixes (issue #1085).

Two bugs:

1. The right-click context menu opened by `_showProjectContextMenu` was styled
   with `background: var(--panel)`, but `--panel` is NOT defined anywhere in
   style.css.  CSS falls back to `transparent` for undefined variables, so the
   menu appeared see-through and the session list bled through.  The fix
   replaces `var(--panel)` with `var(--surface)` — the same opaque variable
   used by `.session-action-menu` and other floating popovers.

2. The `.project-create-input` (used for both rename and new-project creation)
   had `width: 100px` hard-coded, so the field was always exactly 100px wide
   regardless of the project name being edited.  Fix: bound the field with
   `min-width: 40px` / `max-width: 180px` and `width: auto`, plus a
   `_resizeProjectInput()` JS helper that measures the current value with a
   hidden span and sets the pixel width accordingly.

These are static-source tests — CSS/JS behaviour of a popover and an input
sizer can't be exercised faithfully without a browser, but the patterns
worth pinning are the variable names, the absence of the bad ones, and the
presence of the resize helper at both call sites.
"""

import pathlib

REPO = pathlib.Path(__file__).parent.parent
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


# ── Bug 1: context menu background ────────────────────────────────────────────


class TestContextMenuBackground:

    def test_panel_variable_not_defined_in_stylesheet(self):
        """`--panel` is not defined as a CSS custom property anywhere — so
        any rule using `var(--panel)` falls back to `transparent`, which is
        the actual root cause of the menu bleed-through.  This test
        documents that fact: if `--panel` is ever defined, the test will
        need updating but the fix is still safer using `--surface`."""
        # Match either ":root --panel:" or "--panel:" assignments; absence
        # confirms the fallback-to-transparent failure mode.
        assert "--panel:" not in STYLE_CSS, (
            "If --panel is now defined, update this test, but the menu "
            "should still use --surface for consistency with other popovers."
        )

    def test_context_menu_uses_surface_not_panel(self):
        """`_showProjectContextMenu` must set the menu background to
        `var(--surface)`, not `var(--panel)`."""
        # Locate the menu construction
        idx = SESSIONS_JS.find("project-ctx-menu")
        assert idx >= 0, "project-ctx-menu className not found in sessions.js"
        # Look at the surrounding 800 chars where the cssText is set
        window = SESSIONS_JS[idx: idx + 1200]
        assert "background:var(--surface)" in window, (
            "Project context menu must use background:var(--surface) for an "
            "opaque surface — var(--panel) is undefined and falls back to "
            "transparent."
        )
        assert "background:var(--panel)" not in window, (
            "Project context menu still uses background:var(--panel) — "
            "this CSS variable is not defined and renders transparent."
        )

    def test_session_action_menu_also_uses_surface_for_consistency(self):
        """Sanity check: the existing .session-action-menu (the analogous
        right-click menu for session items) uses `var(--surface)` — so the
        fix is consistent with the rest of the codebase."""
        assert "session-action-menu" in STYLE_CSS
        # Find the rule and confirm it uses --surface
        idx = STYLE_CSS.find(".session-action-menu")
        assert idx >= 0
        rule = STYLE_CSS[idx: idx + 400]
        assert "var(--surface)" in rule, (
            ".session-action-menu should use var(--surface) — kept here as "
            "the canonical reference for opaque popover surfaces."
        )


# ── Bug 2: project-create-input width ─────────────────────────────────────────


class TestProjectCreateInputWidth:

    def test_no_hardcoded_100px_width(self):
        """The fixed `width: 100px` on .project-create-input is gone."""
        idx = STYLE_CSS.find(".project-create-input{")
        assert idx >= 0, ".project-create-input rule not found in style.css"
        rule = STYLE_CSS[idx: idx + 400]
        assert "width:100px" not in rule and "width: 100px" not in rule, (
            "Fixed 100px width must be replaced with min-width/max-width/"
            "width:auto so the input grows with its content."
        )

    def test_min_and_max_width_present(self):
        """Both min-width and max-width must be set on .project-create-input."""
        idx = STYLE_CSS.find(".project-create-input{")
        rule = STYLE_CSS[idx: idx + 400]
        assert "min-width:40px" in rule, (
            f"min-width:40px not found in .project-create-input rule: {rule}"
        )
        assert "max-width:180px" in rule, (
            f"max-width:180px not found in .project-create-input rule: {rule}"
        )
        assert "width:auto" in rule, (
            f"width:auto not found in .project-create-input rule: {rule}"
        )


class TestResizeProjectInputHelper:
    """The `_resizeProjectInput` helper must exist and be wired into both
    rename and create call sites."""

    def test_resize_helper_defined(self):
        assert "function _resizeProjectInput(" in SESSIONS_JS, (
            "_resizeProjectInput helper not found in sessions.js"
        )

    def test_resize_helper_uses_hidden_span(self):
        """The standard pattern is to measure with a hidden absolute span
        sharing the same font/padding as the input. Font and family are read
        via getComputedStyle so the sizer stays calibrated if CSS changes."""
        idx = SESSIONS_JS.find("function _resizeProjectInput(")
        assert idx >= 0
        body = SESSIONS_JS[idx: idx + 900]
        assert "position:absolute" in body and "visibility:hidden" in body, (
            "_resizeProjectInput should use a hidden absolute span to "
            "measure the value's rendered width."
        )
        assert "getComputedStyle(inp)" in body, (
            "_resizeProjectInput should use getComputedStyle to read font "            "properties so the sizer stays calibrated if CSS changes."
        )
        assert "Math.min(180" in body, (
            "max bound (180) not applied in _resizeProjectInput"
        )
        assert "Math.max(40" in body, (
            "min bound (40) not applied in _resizeProjectInput"
        )

    def test_rename_calls_resize_helper(self):
        """`_startProjectRename` must call `_resizeProjectInput` once on
        creation and again on every input event."""
        idx = SESSIONS_JS.find("function _startProjectRename(")
        assert idx >= 0
        body = SESSIONS_JS[idx: idx + 1200]
        assert "_resizeProjectInput(inp)" in body, (
            "_startProjectRename must call _resizeProjectInput so the "
            "input width matches the existing project name."
        )
        # Wired into the input event so it grows as the user types
        assert "addEventListener('input'" in body and "_resizeProjectInput" in body, (
            "_startProjectRename must wire input events to _resizeProjectInput"
        )

    def test_create_calls_resize_helper(self):
        """Same for `_startProjectCreate` (new-project entry field)."""
        idx = SESSIONS_JS.find("function _startProjectCreate(")
        assert idx >= 0
        body = SESSIONS_JS[idx: idx + 1200]
        assert "_resizeProjectInput(inp)" in body, (
            "_startProjectCreate must call _resizeProjectInput on focus"
        )
        assert "addEventListener('input'" in body, (
            "_startProjectCreate must wire input events to _resizeProjectInput"
        )
