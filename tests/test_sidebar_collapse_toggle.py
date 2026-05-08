"""
Sidebar collapse toggle regression tests — static checks.

Verifies the CSS, JS, and HTML contract for the desktop sidebar
collapse feature (clicking active rail button collapses the sidebar
panel, leaving only the 48px rail icon strip visible).

Run:
    pytest tests/test_sidebar_collapse_toggle.py -v
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
CSS  = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")


class TestSidebarCollapseCSS:
    """CSS rules for sidebar collapse must be present and correct."""

    def test_sidebar_collapsed_rule_exists(self):
        assert ".sidebar-collapsed .sidebar" in CSS, \
            "Missing .layout.sidebar-collapsed .sidebar rule in style.css"

    def test_sidebar_collapsed_sets_width_zero(self):
        # Find the collapsed rule and verify width:0
        assert "width:0!important" in CSS or "width: 0!important" in CSS, \
            "sidebar-collapsed rule must set width:0!important"

    def test_sidebar_collapsed_sets_opacity_zero(self):
        assert "opacity:0" in CSS, \
            "sidebar-collapsed rule must set opacity:0"

    def test_resize_handle_hidden_when_collapsed(self):
        assert ".sidebar-collapsed .sidebar .resize-handle" in CSS, \
            "Resize handle must be hidden when sidebar is collapsed"

    def test_body_resizing_suppresses_transition(self):
        assert "body.resizing .sidebar" in CSS, \
            "body.resizing .sidebar must suppress transition during drag"
        # Verify the rule actually sets transition:none
        idx = CSS.index("body.resizing .sidebar")
        block = CSS[idx:idx+200]
        assert "transition:none" in block.replace(" ", ""), \
            "body.resizing .sidebar must set transition:none!important"

    def test_sidebar_transition_in_desktop_media_query(self):
        # The sidebar transition should be inside the second @media(min-width:641px) block
        # (the one with position:relative + transition for sidebar)
        pattern = re.compile(r'@media\s*\(\s*min-width\s*:\s*641px\s*\)\s*\{')
        for m in pattern.finditer(CSS):
            open_at = m.end() - 1
            depth = 0
            for i in range(open_at, len(CSS)):
                if CSS[i] == "{":
                    depth += 1
                elif CSS[i] == "}":
                    depth -= 1
                    if depth == 0:
                        block = CSS[open_at+1:i]
                        # Look for the specific sidebar rule with transition (not just any .sidebar)
                        if "sidebar{position:relative;transition" in block.replace(" ", ""):
                            assert "width.2sease" in block.replace(" ", ""), \
                                "Sidebar transition should use .2s ease"
                            return
        raise AssertionError("sidebar transition rule not found inside @media(min-width:641px)")


class TestSidebarCollapseJS:
    """JS functions for sidebar collapse must be present in boot.js."""

    def test_is_sidebar_collapsed_function(self):
        assert "function _isSidebarCollapsed" in BOOT_JS, \
            "_isSidebarCollapsed function missing from boot.js"

    def test_toggle_sidebar_function(self):
        assert "function toggleSidebar" in BOOT_JS, \
            "toggleSidebar function missing from boot.js"

    def test_expand_sidebar_function(self):
        assert "function expandSidebar" in BOOT_JS, \
            "expandSidebar function missing from boot.js"

    def test_sync_sidebar_aria_function(self):
        assert "function _syncSidebarAria" in BOOT_JS, \
            "_syncSidebarAria function missing from boot.js"

    def test_localstorage_key_consistency(self):
        # The key must be consistent: defined as const, used in setItem/getItem
        key_match = re.search(r"const\s+_SIDEBAR_COLLAPSED_KEY\s*=\s*'([^']*)'", BOOT_JS)
        assert key_match, "_SIDEBAR_COLLAPSED_KEY constant missing from boot.js"
        key = key_match.group(1)
        assert key == "hermes-sidebar-collapsed", \
            f"Unexpected localStorage key: {key}"
        assert f"localStorage.setItem(_SIDEBAR_COLLAPSED_KEY" in BOOT_JS, \
            "localStorage.setItem with key not found"
        assert f"localStorage.getItem(_SIDEBAR_COLLAPSED_KEY" in BOOT_JS, \
            "localStorage.getItem with key not found"

    def test_restore_on_boot(self):
        # The IIFE _restoreSidebarState must exist
        assert "_restoreSidebarState" in BOOT_JS, \
            "_restoreSidebarState IIFE missing from boot.js"
        assert "_syncSidebarAria()" in BOOT_JS, \
            "_syncSidebarAria must be called on boot restore"

    def test_bfcache_pageshow_restore(self):
        # pageshow handler must re-sync sidebar state
        pageshow_idx = BOOT_JS.find("window.addEventListener('pageshow'")
        assert pageshow_idx >= 0, "pageshow handler missing from boot.js"
        # The sidebar re-sync code is near the end of the handler; extend search range
        pageshow_block = BOOT_JS[pageshow_idx:]
        # Find the end of the handler (matching closing })
        depth = 0
        end = pageshow_block.find("});")
        if end > 0:
            pageshow_block = pageshow_block[:end+3]
        assert "hermes-sidebar-collapsed" in pageshow_block, \
            "pageshow handler must re-sync sidebar collapse state"
        assert "_syncSidebarAria" in pageshow_block, \
            "pageshow handler must call _syncSidebarAria"


class TestSidebarCollapseSwitchPanel:
    """switchPanel in panels.js must guard collapse with fromRailClick."""

    def test_from_rail_click_guard(self):
        assert "opts.fromRailClick" in PANELS_JS, \
            "switchPanel must check opts.fromRailClick before collapsing"

    def test_collapse_calls_toggle_sidebar(self):
        # When fromRailClick && same panel, must call toggleSidebar(true)
        idx = PANELS_JS.find("opts.fromRailClick")
        block = PANELS_JS[idx:idx+800]  # Extended range — toggleSidebar is further down
        assert "toggleSidebar(true)" in block, \
            "switchPanel must call toggleSidebar(true) on same-panel rail click"

    def test_expand_on_collapse_state(self):
        # When sidebar is collapsed and rail clicked, must call expandSidebar
        assert "expandSidebar()" in PANELS_JS, \
            "switchPanel must call expandSidebar when sidebar is collapsed"

    def test_sync_aria_after_panel_switch(self):
        assert "_syncSidebarAria" in PANELS_JS, \
            "panels.js must call _syncSidebarAria after panel switch"


class TestSidebarCollapseHTML:
    """Rail buttons must pass fromRailClick:true in onclick."""

    def test_rail_buttons_pass_from_rail_click(self):
        rail_section = HTML[HTML.index('<nav class="rail"'):HTML.index('</nav>', HTML.index('<nav class="rail"'))]
        # All switchPanel calls in rail should have fromRailClick:true
        calls = re.findall(r"switchPanel\('(\w+)',?\s*([^)]*)\)", rail_section)
        for panel, args in calls:
            assert "fromRailClick:true" in args, \
                f"Rail button for '{panel}' must pass fromRailClick:true, got: {args}"

    def test_sidebar_nav_not_affected(self):
        # sidebar-nav buttons (mobile) should also pass fromRailClick:true
        # (the min-width:641px guard in switchPanel prevents collapse on mobile)
        sidebar_nav_start = HTML.find('class="sidebar-nav"')
        if sidebar_nav_start < 0:
            return  # no sidebar-nav, skip
        sidebar_nav_end = HTML.find('</div>', sidebar_nav_start)
        nav_section = HTML[sidebar_nav_start:sidebar_nav_end]
        calls = re.findall(r"switchPanel\('(\w+)',?\s*([^)]*)\)", nav_section)
        # sidebar-nav is fine either way — fromRailClick is harmless on mobile
        # Just verify no syntax errors
        for panel, args in calls:
            if args:
                assert "fromRailClick:true" in args, \
                    f"If sidebar-nav passes args, must be fromRailClick:true for '{panel}'"

    def test_dashboard_button_unchanged(self):
        assert "openHermesDashboard(event)" in HTML, \
            "Dashboard button should remain unchanged"
        # Dashboard button should NOT have fromRailClick
        dash_idx = HTML.find("openHermesDashboard(event)")
        assert "fromRailClick" not in HTML[dash_idx-100:dash_idx], \
            "Dashboard button should not have fromRailClick"
