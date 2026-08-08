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


class TestProjectDefaultWorkspaceMenu:
    """Project context menu exposes set/clear affordances for #5457."""

    def test_default_workspace_menu_item_exists(self):
        idx = SESSIONS_JS.find("function _showProjectContextMenu(")
        assert idx >= 0
        body = SESSIONS_JS[idx: idx + 3600]
        assert "Default workspace" in body
        assert "_showProjectDefaultWorkspacePicker(" in body

    def test_default_workspace_menu_loads_saved_workspaces(self):
        idx = SESSIONS_JS.find("function _showProjectContextMenu(")
        assert idx >= 0
        body = SESSIONS_JS[idx: idx + 3600]
        assert "api('/api/workspaces',{method:'GET'})" in body

    def test_default_workspace_picker_posts_project_update(self):
        idx = SESSIONS_JS.find("function _showProjectDefaultWorkspacePicker(")
        assert idx >= 0
        body = SESSIONS_JS[idx: idx + 2600]
        assert "api('/api/projects/rename'" in body
        assert "default_workspace:ws||null" in body
        assert "project_id:proj.project_id" in body
        assert "name:proj.name" in body

    def test_default_workspace_picker_has_clear_path(self):
        idx = SESSIONS_JS.find("function _showProjectDefaultWorkspacePicker(")
        assert idx >= 0
        body = SESSIONS_JS[idx: idx + 2600]
        assert "Clear default" in body
        assert "Default workspace cleared" in body

    def test_default_workspace_picker_clamps_to_viewport(self):
        idx = SESSIONS_JS.find("function _showProjectDefaultWorkspacePicker(")
        assert idx >= 0
        body = SESSIONS_JS[idx: idx + 2600]
        assert "window.innerWidth" in body
        assert "document.documentElement.clientHeight" in body
        assert "Math.max(8,Math.min(anchorEvent.clientX" in body
        assert "Math.max(8,Math.min(anchorEvent.clientY" in body

    def test_default_workspace_current_marker_normalizes_path_display_key(self):
        idx = SESSIONS_JS.find("function _showProjectDefaultWorkspacePicker(")
        assert idx >= 0
        body = SESSIONS_JS[idx: idx + 2600]
        assert "_projectWorkspaceDisplayKey(ws)" in body
        assert "_projectWorkspaceDisplayKey(proj.default_workspace)" in body


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


class TestProjectChipLongPressTouch:
    """Mobile long-press to open the project context menu (#3760).

    Project chips were deletable only via the right-click context menu, which has
    no touch equivalent — so mobile users could never remove a project. A 500ms
    long-press now opens the same menu.
    """

    def _chip_touch_block(self):
        # The chip touch handlers live just after the oncontextmenu wiring in the
        # project-chip render loop.
        idx = SESSIONS_JS.find("Touch long-press")
        assert idx != -1, "project-chip long-press touch block not found"
        return SESSIONS_JS[idx: idx + 2300]

    def test_long_press_opens_project_context_menu(self):
        block = self._chip_touch_block()
        assert "addEventListener('touchstart'" in block
        assert "setTimeout(" in block and "},500);" in block
        assert "_showProjectContextMenu(" in block
        # visual feedback + scroll-drift cancel, mirroring the session-item pattern
        assert "long-pressing" in block
        assert "addEventListener('touchmove'" in block
        assert ">10" in block  # >10px drift cancels the press

    def test_long_press_suppresses_synthetic_click_and_filter_tap(self):
        block = self._chip_touch_block()
        # touchend must be non-passive so it can preventDefault the synthetic click
        assert "addEventListener('touchend'" in block
        assert "{passive:false}" in block
        assert "e.preventDefault();e.stopPropagation();" in block
        # the long-press handler cancels the pending single-tap filter timer
        assert "clearTimeout(_pClickTimer)" in block

    def test_touchstart_clears_inflight_timer_before_scheduling(self):
        """Regression: a second finger / stray touchstart must not orphan the
        prior timer (which would then fire the menu after the gesture was
        cancelled). touchstart clears any in-flight _lpTimer before scheduling,
        and the timer body bails if the gesture was already consumed.
        """
        block = self._chip_touch_block()
        # clear-before-schedule at the top of touchstart
        assert "if(_lpTimer){clearTimeout(_lpTimer);_lpTimer=null;}" in block, (
            "touchstart must clear any in-flight long-press timer before scheduling "
            "a new one (orphaned-timer fix)"
        )
        # stale-fire guard inside the timer body
        assert "if(_lpHandled) return;" in block, (
            "the long-press timer body must no-op if the gesture was already consumed"
        )

    def test_long_pressing_style_feedback_present(self):
        assert ".project-chip.long-pressing" in STYLE_CSS
        # Target the base .project-chip rule (the one carrying the layout props),
        # not an unrelated theme override of the same selector.
        base_idx = STYLE_CSS.find(".project-chip{font-size")
        assert base_idx != -1, "base .project-chip rule not found"
        chip_rule = STYLE_CSS[base_idx: STYLE_CSS.find("}", base_idx) + 1]
        # touch tuning so the native callout/selection doesn't compete with the gesture
        assert "touch-action:manipulation" in chip_rule
        assert "user-select:none" in chip_rule


# ── #5457: project default workspace context menu editor ──────────────────────


class TestProjectDefaultWorkspaceContextMenu:
    """The project context menu must expose a default-workspace set/clear flow (#5457)."""

    def _ctx_menu_block(self):
        idx = SESSIONS_JS.find("function _showProjectContextMenu(")
        assert idx != -1, "_showProjectContextMenu not found in sessions.js"
        end_idx = SESSIONS_JS.find("\nfunction ", idx + 1)
        return SESSIONS_JS[idx: end_idx if end_idx > 0 else idx + 4000]

    def _picker_block(self):
        idx = SESSIONS_JS.find("function _showProjectDefaultWorkspacePicker(")
        assert idx != -1, "_showProjectDefaultWorkspacePicker not found in sessions.js"
        end_idx = SESSIONS_JS.find("\nfunction ", idx + 1)
        return SESSIONS_JS[idx: end_idx if end_idx > 0 else idx + 3000]

    def test_picker_function_exists(self):
        assert "function _showProjectDefaultWorkspacePicker(" in SESSIONS_JS, (
            "_showProjectDefaultWorkspacePicker function not found — default workspace "
            "picker is not implemented."
        )

    def test_context_menu_has_default_workspace_item(self):
        """The project context menu must include a 'Default workspace' item."""
        block = self._ctx_menu_block()
        assert "Default workspace" in block, (
            "Project context menu does not contain a 'Default workspace' item."
        )

    def test_context_menu_default_workspace_fetches_workspaces_api(self):
        """The Default workspace item must call /api/workspaces to populate the picker."""
        block = self._ctx_menu_block()
        assert "/api/workspaces" in block, (
            "Context menu Default workspace item must fetch /api/workspaces for the picker."
        )

    def test_context_menu_default_workspace_opens_picker(self):
        """The Default workspace onclick must delegate to _showProjectDefaultWorkspacePicker."""
        block = self._ctx_menu_block()
        assert "_showProjectDefaultWorkspacePicker(" in block, (
            "Context menu item must call _showProjectDefaultWorkspacePicker."
        )

    def test_picker_posts_default_workspace_to_projects_rename(self):
        """The picker must persist the selected workspace via /api/projects/rename."""
        block = self._picker_block()
        assert "/api/projects/rename" in block, (
            "_showProjectDefaultWorkspacePicker must POST to /api/projects/rename."
        )
        assert "default_workspace" in block, (
            "_showProjectDefaultWorkspacePicker must include default_workspace in the payload."
        )

    def test_picker_has_clear_option(self):
        """The picker must include a 'Clear default' option to remove the project default."""
        block = self._picker_block()
        assert "Clear default" in block, (
            "_showProjectDefaultWorkspacePicker must expose a 'Clear default' action."
        )

    def test_picker_refreshes_session_list_after_change(self):
        """The picker must call renderSessionList() after setting or clearing the default."""
        block = self._picker_block()
        assert "renderSessionList(" in block, (
            "_showProjectDefaultWorkspacePicker must refresh the session list after saving."
        )

    def test_picker_clamps_fixed_position_to_viewport(self):
        """The picker must keep its fixed menu inside the current viewport."""
        block = self._picker_block()
        assert "window.innerWidth" in block
        assert "document.documentElement.clientWidth" in block
        assert "Math.max(8,Math.min(anchorEvent.clientX" in block
        assert "Math.max(8,Math.min(anchorEvent.clientY" in block

    def test_picker_current_marker_uses_workspace_display_key(self):
        """Current marker comparison must tolerate harmless path display differences."""
        block = self._picker_block()
        assert "_projectWorkspaceDisplayKey(ws)" in block
        assert "_projectWorkspaceDisplayKey(proj.default_workspace)" in block


class TestCrossProfileProjectQuickCreate:
    """Cross-profile project quick-create must switch profile before newSession()."""

    def test_new_session_has_project_profile_switch_helper(self):
        assert "async function _ensureProjectProfileForNewSession(" in SESSIONS_JS, (
            "_ensureProjectProfileForNewSession helper not found in sessions.js."
        )

    def test_new_session_awaits_project_profile_switch_before_building_request(self):
        idx = SESSIONS_JS.find("async function newSession(")
        assert idx != -1, "newSession not found in sessions.js"
        block = SESSIONS_JS[idx: idx + 2600]
        assert "await _ensureProjectProfileForNewSession(_newSessionProj)" in block, (
            "newSession must switch to the target project profile before posting the request."
        )
        assert "Switch to '+targetProfile+' before opening a new chat in this project" in block, (
            "newSession must refuse a foreign project when profile switching cannot make it current."
        )

    def test_quick_create_button_still_routes_through_new_session_project_path(self):
        idx = SESSIONS_JS.find("function _attachProjectQuickCreateButton(")
        assert idx != -1, "_attachProjectQuickCreateButton not found in sessions.js"
        block = SESSIONS_JS[idx: idx + 2200]
        assert "await newSession(false,{project_id:project.project_id});" in block, (
            "Quick-create must keep using newSession() with project_id so the shared profile-switch guard runs."
        )
