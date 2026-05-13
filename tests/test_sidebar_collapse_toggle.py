"""
Sidebar collapse toggle — static regression tests.

Covers the desktop sidebar collapse feature (clicking the already-active rail
button collapses the sidebar panel, or Cmd+B toggles it). Validates the HTML
contract (every rail/sidebar-nav switchPanel call passes fromRailClick:true),
the CSS rules (collapse states, transition, flash-prevention), and the JS
(toggleSidebar / expandSidebar / _isSidebarCollapsed / Cmd+B handler).

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


# ── CSS contract ───────────────────────────────────────────────────────────

class TestSidebarCollapseCSS:
    """CSS rules for collapse, flash-prevention, and resize-suppression."""

    def test_layout_sidebar_collapsed_rule_exists(self):
        assert ".layout.sidebar-collapsed .sidebar" in CSS, \
            ".layout.sidebar-collapsed .sidebar rule missing from style.css"

    def test_collapsed_sets_width_zero(self):
        assert "width:0 !important" in CSS or "width:0!important" in CSS, \
            "sidebar-collapsed rule must set width:0!important"

    def test_collapsed_sets_opacity_zero(self):
        # Find the collapsed block and verify opacity:0 is inside it
        idx = CSS.index(".layout.sidebar-collapsed .sidebar")
        block = CSS[idx:idx + 400]
        assert "opacity:0" in block, \
            "sidebar-collapsed rule must set opacity:0"

    def test_collapsed_uses_negative_translate(self):
        idx = CSS.index(".layout.sidebar-collapsed .sidebar")
        block = CSS[idx:idx + 400]
        assert "translateX(-14px)" in block, \
            "Sidebar should slide left when collapsed (mirrors workspace panel)"

    def test_collapsed_hides_resize_handle(self):
        assert ".layout.sidebar-collapsed .sidebar .resize-handle" in CSS, \
            "Resize handle must be hidden when collapsed"

    def test_flash_prevention_rule_exists(self):
        assert 'html[data-sidebar-collapsed="1"]' in CSS, \
            "Flash-prevention rule for html[data-sidebar-collapsed='1'] missing"

    def test_flash_prevention_suppresses_transition(self):
        idx = CSS.index('html[data-sidebar-collapsed="1"]')
        block = CSS[idx:idx + 400]
        assert "transition:none" in block, \
            "Flash-prevention rule must set transition:none to avoid initial slide"

    def test_sidebar_has_transition(self):
        # Find the desktop .sidebar rule (the one with width:300px) and verify
        # it has the slide transition
        m = re.search(r"\.sidebar\{width:300px[^}]*\}", CSS)
        assert m, "Desktop .sidebar{width:300px;...} block not found"
        assert "transition:" in m.group(0), \
            "Desktop .sidebar rule must have a transition for collapse animation"

    def test_body_resizing_suppresses_transition(self):
        assert "body.resizing .sidebar" in CSS, \
            "body.resizing .sidebar rule missing — drag-resize would animate"
        idx = CSS.index("body.resizing .sidebar")
        block = CSS[idx:idx + 100]
        assert "transition:none" in block, \
            "body.resizing .sidebar must set transition:none"

    def test_mobile_overlay_not_targeted(self):
        # Both collapse selectors must exclude .mobile-open so the
        # mobile slide-in overlay is never accidentally targeted.
        for selector_prefix in (".layout.sidebar-collapsed .sidebar",
                                'html[data-sidebar-collapsed="1"] .sidebar'):
            idx = CSS.index(selector_prefix)
            line_end = CSS.index("{", idx)
            selector = CSS[idx:line_end]
            assert ":not(.mobile-open)" in selector, \
                f"Collapse selector must exclude .mobile-open: {selector!r}"

    def test_css_breakpoint_matches_js_isdesktopwidth(self):
        # The CSS @media block guarding .layout.sidebar-collapsed must use the
        # same min-width threshold as JS _isDesktopWidth(). Otherwise a click
        # in the asymmetric band silently flips the class while CSS sits out
        # — confusing for the user, broken for screen readers.
        js_bp = re.search(
            r"function\s+_isDesktopWidth[^}]*?matchMedia\('([^']+)'\)",
            BOOT_JS, re.DOTALL,
        )
        assert js_bp, "Could not locate _isDesktopWidth matchMedia query in boot.js"
        js_query = js_bp.group(1)

        # Walk CSS to find which @media block encloses .layout.sidebar-collapsed
        idx = CSS.index(".layout.sidebar-collapsed .sidebar:not(.mobile-open)")
        # Search backward for the most recent unmatched `@media(...)`
        prefix = CSS[:idx]
        depth = 0
        media_stack = []
        last_open_media = None
        i = 0
        while i < len(prefix):
            ch = prefix[i]
            if ch == "@" and prefix[i:i + 6] == "@media":
                end = prefix.index("{", i)
                cond = prefix[i + 6:end].strip()
                media_stack.append((cond, depth + 1))
                i = end + 1
                depth += 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                while media_stack and media_stack[-1][1] > depth:
                    media_stack.pop()
            i += 1
        last_open_media = media_stack[-1][0] if media_stack else None
        assert last_open_media is not None, (
            "Collapse rule must be inside an @media block to gate it correctly"
        )
        # Normalise whitespace for comparison
        norm = lambda s: s.replace(" ", "")
        assert norm(last_open_media) == norm(js_query), (
            f"CSS @media('{last_open_media}') for .sidebar-collapsed must match JS "
            f"_isDesktopWidth() ('{js_query}'). Otherwise clicks in the asymmetric band "
            f"silently flip state without visual feedback."
        )


# ── boot.js contract ───────────────────────────────────────────────────────

class TestSidebarCollapseBootJS:
    """Functions, constants, and event-handler hooks in boot.js."""

    def test_localstorage_key_constant(self):
        m = re.search(r"const\s+_SIDEBAR_COLLAPSED_KEY\s*=\s*'([^']*)'", BOOT_JS)
        assert m, "_SIDEBAR_COLLAPSED_KEY constant missing from boot.js"
        assert m.group(1) == "hermes-webui-sidebar-collapsed", \
            f"Unexpected localStorage key: {m.group(1)!r}"

    def test_is_desktop_width_function(self):
        assert "function _isDesktopWidth" in BOOT_JS, \
            "_isDesktopWidth function missing — every collapse path must be desktop-gated"

    def test_is_sidebar_collapsed_function(self):
        assert "function _isSidebarCollapsed" in BOOT_JS, \
            "_isSidebarCollapsed function missing"

    def test_toggle_sidebar_function(self):
        assert "function toggleSidebar" in BOOT_JS, \
            "toggleSidebar function missing"

    def test_toggle_sidebar_short_circuits_on_mobile(self):
        idx = BOOT_JS.index("function toggleSidebar")
        # End of the function: find the next standalone "function " at column 0
        end = BOOT_JS.index("\nfunction ", idx + 1)
        body = BOOT_JS[idx:end]
        assert "_isDesktopWidth()" in body, \
            "toggleSidebar must short-circuit on mobile via _isDesktopWidth check"

    def test_expand_sidebar_function(self):
        assert "function expandSidebar" in BOOT_JS, \
            "expandSidebar function missing"

    def test_sync_sidebar_aria_function(self):
        assert "function _syncSidebarAria" in BOOT_JS, \
            "_syncSidebarAria function missing"

    def test_aria_uses_active_rail_button(self):
        idx = BOOT_JS.index("function _syncSidebarAria")
        end = BOOT_JS.index("\nfunction ", idx + 1)
        body = BOOT_JS[idx:end]
        assert ".rail .rail-btn.nav-tab.active[data-panel]" in body, \
            "_syncSidebarAria must target the active rail button"
        assert "aria-expanded" in body, \
            "_syncSidebarAria must set aria-expanded"

    def test_restore_on_boot_iife(self):
        assert "_restoreSidebarState" in BOOT_JS, \
            "_restoreSidebarState IIFE missing — collapsed state would not persist"

    def test_restore_clears_flash_prevention_attribute(self):
        # The IIFE must remove the root data-sidebar-collapsed attribute so it
        # doesn't override the CSS class system once JS owns the state.
        idx = BOOT_JS.index("_restoreSidebarState")
        end = BOOT_JS.index("})();", idx) + 5
        body = BOOT_JS[idx:end]
        assert "removeAttribute('data-sidebar-collapsed')" in body, \
            "_restoreSidebarState must clear the data-sidebar-collapsed attribute"

    def test_cmd_b_shortcut(self):
        # The Cmd/Ctrl+B handler must exist and be gated against text inputs.
        # Find it within the global keydown listener.
        idx = BOOT_JS.index("document.addEventListener('keydown'")
        # The handler is large; search a reasonable window for the shortcut block
        window = BOOT_JS[idx:idx + 8000]
        assert "metaKey" in window and "ctrlKey" in window and "'b'" in window, \
            "Cmd/Ctrl+B handler missing from global keydown listener"
        # Must check that target is not an input/textarea/contenteditable
        assert "TEXTAREA" in window and "isContentEditable" in window, \
            "Cmd/Ctrl+B handler must skip when typing in an input/textarea"

    def test_bfcache_pageshow_resync(self):
        idx = BOOT_JS.index("window.addEventListener('pageshow'")
        # find end of handler
        depth = 0
        end = BOOT_JS.index("});", idx)
        block = BOOT_JS[idx:end + 3]
        assert "hermes-webui-sidebar-collapsed" in block, \
            "pageshow handler must re-sync sidebar state from localStorage"
        assert "_syncSidebarAria" in block, \
            "pageshow handler must call _syncSidebarAria after re-sync"


# ── panels.js contract ─────────────────────────────────────────────────────

class TestSwitchPanelGuard:
    """switchPanel() must gate collapse behind opts.fromRailClick."""

    def test_from_rail_click_guard(self):
        assert "opts.fromRailClick" in PANELS_JS, \
            "switchPanel must gate collapse on opts.fromRailClick"

    def test_guard_uses_desktop_width(self):
        idx = PANELS_JS.index("opts.fromRailClick")
        # The fromRailClick branch is at the top of switchPanel — capture ~1KB
        block = PANELS_JS[idx:idx + 1500]
        assert "_isDesktopWidth" in block, \
            "Collapse guard must also check _isDesktopWidth so mobile is excluded"

    def test_same_panel_calls_toggle_sidebar(self):
        idx = PANELS_JS.index("opts.fromRailClick")
        block = PANELS_JS[idx:idx + 1500]
        assert "toggleSidebar(true)" in block, \
            "Same-panel rail click must call toggleSidebar(true)"

    def test_expand_when_collapsed(self):
        idx = PANELS_JS.index("opts.fromRailClick")
        block = PANELS_JS[idx:idx + 1500]
        assert "expandSidebar()" in block, \
            "Collapsed-state rail click must call expandSidebar() before switching"

    def test_aria_sync_after_panel_switch(self):
        # The post-switch aria refresh should be near the data-panel forEach
        assert "_syncSidebarAria" in PANELS_JS, \
            "panels.js must call _syncSidebarAria after panel switch"

    def test_legacy_proxy_forwards_opts(self):
        # The proxy at the bottom of the file must forward opts to keep the
        # rail-click gesture working when the proxy runs (it overrides the
        # function reference, so the original definition is unreachable).
        m = re.search(
            r"switchPanel\s*=\s*async\s+function\s*\(([^)]*)\)\s*\{[^}]*_origSwitchPanel\(([^)]*)\)",
            PANELS_JS
        )
        assert m, "switchPanel proxy not found at end of panels.js"
        params, args = m.group(1), m.group(2)
        assert "opts" in params and "opts" in args, \
            f"Proxy must forward opts to _origSwitchPanel — got params={params!r}, args={args!r}"


# ── HTML contract ──────────────────────────────────────────────────────────

class TestRailButtonsPassFromRailClick:
    """All rail-button and sidebar-nav switchPanel() calls must opt in."""

    def _rail_section(self):
        start = HTML.index('<nav class="rail"')
        end = HTML.index('</nav>', start)
        return HTML[start:end]

    def _sidebar_nav_section(self):
        start = HTML.index('class="sidebar-nav"')
        end = HTML.index('</div>', start)
        return HTML[start:end]

    def test_all_rail_buttons_pass_from_rail_click(self):
        section = self._rail_section()
        calls = re.findall(r"switchPanel\('(\w+)'(?:\s*,\s*([^)]*))?\)", section)
        assert calls, "No switchPanel() calls found in rail nav (unexpected)"
        for panel, args in calls:
            assert args and "fromRailClick:true" in args, \
                f"Rail button for {panel!r} must pass fromRailClick:true (got: {args!r})"

    def test_all_sidebar_nav_buttons_pass_from_rail_click(self):
        # sidebar-nav is the mobile mirror; passing fromRailClick is harmless
        # because the JS guards on _isDesktopWidth.
        section = self._sidebar_nav_section()
        calls = re.findall(r"switchPanel\('(\w+)'(?:\s*,\s*([^)]*))?\)", section)
        for panel, args in calls:
            assert args and "fromRailClick:true" in args, \
                f"sidebar-nav button for {panel!r} must pass fromRailClick:true (got: {args!r})"

    def test_dashboard_button_unchanged(self):
        # Dashboard opens an external page; must NOT pass fromRailClick
        assert "openHermesDashboard(event)" in HTML
        dash_idx = HTML.index("openHermesDashboard(event)")
        # 200-char window before the dashboard onclick should not mention fromRailClick
        assert "fromRailClick" not in HTML[dash_idx - 200:dash_idx + 50], \
            "Dashboard button should not receive fromRailClick"


# ── Flash-prevention contract ──────────────────────────────────────────────

class TestFlashPreventionScript:
    """The inline <script> in <head> sets data-sidebar-collapsed before CSS."""

    def test_inline_script_exists(self):
        assert "hermes-webui-sidebar-collapsed" in HTML, \
            "Inline flash-prevention script missing from index.html"

    def test_inline_script_uses_correct_dataset_key(self):
        # The dataset attribute on <html> must match what CSS targets
        script_idx = HTML.index("hermes-webui-sidebar-collapsed")
        # Find the enclosing <script>...</script>
        open_tag = HTML.rfind("<script>", 0, script_idx)
        close_tag = HTML.index("</script>", script_idx)
        block = HTML[open_tag:close_tag]
        assert "dataset.sidebarCollapsed" in block, \
            "Inline script must set document.documentElement.dataset.sidebarCollapsed"

    def test_inline_script_runs_before_stylesheet(self):
        # The script must appear before the main stylesheet <link>
        script_idx = HTML.index("hermes-webui-sidebar-collapsed")
        css_idx = HTML.index('href="static/style.css')
        assert script_idx < css_idx, \
            "Flash-prevention script must run before stylesheet to avoid paint flash"
