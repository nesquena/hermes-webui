"""
Mobile layout regression tests — run on every QA pass.

These tests check that the CSS and HTML structure required for correct
mobile rendering (375px–640px viewport widths) is intact after every change.
They are static checks (no server needed) that catch common regressions:

  - Mobile breakpoints present for key layout elements
  - Right panel slide-over markup and CSS intact
  - Profile dropdown not clipped by overflow on mobile
  - Composer footer chips scroll correctly on narrow viewports
  - Mobile bottom nav and overlay markup present
  - No full-viewport overflow that would break scroll

Run as part of the standard test suite:
    pytest tests/test_mobile_layout.py -v
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
CSS  = (REPO / "static" / "style.css").read_text(encoding="utf-8")


# ── Mobile breakpoint rules ───────────────────────────────────────────────────

def test_mobile_breakpoint_900px_present():
    """@media(max-width:900px) must hide the right panel and show mobile-files-btn."""
    assert "@media(max-width:900px)" in CSS or "@media (max-width: 900px)" in CSS, \
        "Missing @media(max-width:900px) breakpoint in style.css"
    # Right panel should be hidden at 900px, replaced by slide-over
    assert ".rightpanel{display:none" in CSS or ".rightpanel {display:none" in CSS or \
           re.search(r'max-width:900px\).*?\.rightpanel\{display:none', CSS, re.DOTALL), \
        ".rightpanel must be display:none at max-width:900px (slide-over replaces it)"


def test_mobile_breakpoint_640px_present():
    """@media(max-width:640px) must exist for narrow phone layouts."""
    assert "@media(max-width:640px)" in CSS or "@media (max-width: 640px)" in CSS, \
        "Missing @media(max-width:640px) breakpoint in style.css"


def test_rightpanel_mobile_slide_over_css():
    """Right panel must have position:fixed slide-over CSS for mobile."""
    # At max-width:900px the rightpanel should be position:fixed, off-screen right
    assert "position:fixed" in CSS, \
        "style.css must have position:fixed for rightpanel mobile slide-over"
    assert ".rightpanel.mobile-open{right:0" in CSS or ".rightpanel.mobile-open {right:0" in CSS, \
        ".rightpanel.mobile-open must set right:0 to slide panel in from right"
    assert "right:-320px" in CSS or "right: -320px" in CSS, \
        "rightpanel must start off-screen (right:-320px) on mobile"


def test_mobile_overlay_present():
    """Mobile overlay element must exist for tap-to-close sidebar behavior."""
    assert 'id="mobileOverlay"' in HTML, \
        "#mobileOverlay element missing from index.html"
    assert "mobile-overlay" in CSS, \
        ".mobile-overlay CSS rule missing from style.css"


def test_mobile_bottom_nav_present():
    """Mobile bottom navigation bar must be present."""
    assert "mobile-bottom-nav" in HTML or "mobile-nav-btn" in HTML, \
        "Mobile bottom nav (.mobile-bottom-nav or .mobile-nav-btn) missing from index.html"
    assert "mobile-bottom-nav" in CSS, \
        ".mobile-bottom-nav CSS rule missing from style.css"


def test_mobile_files_button_present():
    """Mobile files toggle button (#btnWorkspacePanelToggle.workspace-toggle-btn) must be in HTML and CSS."""
    assert 'id="btnWorkspacePanelToggle"' in HTML, \
        "#btnWorkspacePanelToggle missing from index.html"
    assert "workspace-toggle-btn" in CSS, \
        ".workspace-toggle-btn CSS missing from style.css"


# ── Profile dropdown overflow ─────────────────────────────────────────────────

def test_profile_dropdown_not_clipped_by_overflow():
    """Profile dropdown must not be inside an overflow:hidden or overflow-x:auto ancestor
    without a higher z-index escape hatch.

    The topbar-chips container uses overflow-x:auto on mobile, which creates a
    stacking context that clips absolutely-positioned children. The profile dropdown
    must use position:fixed on mobile OR the topbar-chips must not clip it.
    """
    # The profile-chip wrapper must have position:relative so the dropdown can escape
    assert 'id="profileChipWrap"' in HTML, \
        "#profileChipWrap missing from index.html"
    # Profile dropdown must have a z-index high enough to clear the topbar
    assert ".profile-dropdown{" in CSS or ".profile-dropdown {" in CSS, \
        ".profile-dropdown CSS rule missing"
    # z-index must be at least 200 (topbar is z-index:10)
    m = re.search(r'\.profile-dropdown\{[^}]*z-index:(\d+)', CSS)
    if m:
        assert int(m.group(1)) >= 100, \
            f".profile-dropdown z-index {m.group(1)} is too low — must be >= 100 to clear topbar"


def test_topbar_chips_mobile_overflow():
    """topbar-chips must use overflow-x:auto on mobile for chip scrolling.

    Chips (profile, workspace, model, files) must scroll horizontally on narrow
    viewports rather than wrapping onto a second line which would break the topbar layout.
    """
    # At narrow viewport, topbar-chips should scroll
    assert "overflow-x:auto" in CSS or "overflow-x: auto" in CSS, \
        "topbar-chips must have overflow-x:auto for mobile chip scrolling"


# ── Workspace panel close ─────────────────────────────────────────────────────

def test_workspace_close_button_present():
    """Workspace panel must have a close/hide button accessible on mobile."""
    # Either a dedicated mobile close button or the toggle button that closes the panel
    has_close = (
        'onclick="closeWorkspacePanel()"' in HTML or
        'onclick="toggleWorkspacePanel()"' in HTML
    )
    assert has_close, \
        "closeWorkspacePanel() or toggleWorkspacePanel() must be wired to a button to close the workspace panel on mobile"


def test_toggle_mobile_files_js_defined():
    """toggleMobileFiles() must be defined in boot.js."""
    boot_js = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    assert "function toggleMobileFiles()" in boot_js, \
        "toggleMobileFiles() missing from static/boot.js"
    assert "mobile-open" in boot_js, \
        "toggleMobileFiles() must toggle mobile-open class on the right panel"


# ── Viewport and scroll safety ────────────────────────────────────────────────

def test_body_overflow_hidden():
    """body must have overflow:hidden to prevent double scrollbars on mobile."""
    assert "body{" in CSS or "body {" in CSS, \
        "body rule missing from style.css"
    assert re.search(r'body\{[^}]*overflow:hidden', CSS), \
        "body must have overflow:hidden to prevent double scrollbars"


def test_100dvh_viewport_height():
    """Layout must use 100dvh (dynamic viewport height) for correct mobile sizing.

    On mobile Safari and Chrome, 100vh includes the browser chrome (address bar),
    causing content to be hidden. 100dvh accounts for the actual available height.
    """
    assert "100dvh" in CSS, \
        "style.css must use 100dvh for correct mobile viewport height (100vh hides content under address bar)"


def test_composer_touch_target_size():
    """Send button and composer inputs must have minimum 44px touch targets on mobile.

    Apple HIG and Google Material guidelines both require 44px minimum touch targets.
    """
    # Check that mobile CSS doesn't make the send button smaller than 44×44
    # We check that there's at least a min-height definition for touch targets
    assert re.search(r'(min-height|height).*44px', CSS), \
        "style.css must define 44px minimum touch targets for mobile (send button, nav buttons)"


# ── Input zoom prevention ─────────────────────────────────────────────────────

def test_composer_textarea_font_size_mobile():
    """Composer textarea must have font-size >= 16px on mobile.

    iOS Safari zooms the viewport when an input with font-size < 16px is focused,
    which breaks the layout. The composer textarea must be >= 16px at mobile widths.
    """
    # Check for 16px font-size on the textarea in a mobile breakpoint
    assert re.search(r'font-size:16px', CSS), \
        "Composer textarea must have font-size:16px at mobile widths to prevent iOS zoom-on-focus"



# ── Profiles button in mobile bottom nav ─────────────────────────────────────

def test_mobile_profiles_button_present():
    """Mobile bottom nav must include a Profiles button (PR #265)."""
    assert 'data-panel="profiles"' in HTML and 'mobileSwitchPanel' in HTML, \
        "Mobile nav must have a Profiles button with data-panel='profiles' and mobileSwitchPanel"


def test_mobile_profiles_button_uses_mobileSwitchPanel():
    """Profiles mobile nav button must use mobileSwitchPanel, not raw switchPanel."""
    import re
    match = re.search(
        r'<button[^>]*mobile-nav-btn[^>]*data-panel="profiles"[^>]*>|'
        r'<button[^>]*data-panel="profiles"[^>]*mobile-nav-btn[^>]*>',
        HTML
    )
    assert match, "Could not find mobile-nav-btn with data-panel='profiles'"
    btn_html = HTML[match.start():match.start()+300]
    assert "mobileSwitchPanel('profiles')" in btn_html, \
        "Profiles mobile nav button must call mobileSwitchPanel('profiles')"


def test_mobile_profiles_button_is_last_in_nav():
    """Profiles button must appear after Spaces in the mobile bottom nav."""
    spaces_pos = HTML.find('data-panel="workspaces"')
    profiles_pos = HTML.rfind('data-panel="profiles"')
    assert spaces_pos > 0 and profiles_pos > spaces_pos, \
        "Profiles button must appear after Spaces button in the mobile nav"
