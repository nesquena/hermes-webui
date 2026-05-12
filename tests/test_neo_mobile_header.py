"""Phase 1 — Neo mobile header contract tests.

Verify the mobile header meets the spec:
- Lucide-style hamburger icon (24x24, stroke-width 1.75)
- Brand: neo-ico.png 24x24 + title "Neo" (not neo-mark.svg 16x16)
- Contextual actions: new-chat and more buttons present
- Height 52px in mobile breakpoint
- viewport-fit=cover in viewport meta
- theme-color meta tags present
- No -webkit-app-region:drag outside window-controls-overlay
- app-titlebar-action targets 44x44
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_viewport_fit_cover():
    assert "viewport-fit=cover" in HTML, \
        "viewport meta must include viewport-fit=cover for safe-area support"


def test_theme_color_meta_present():
    assert 'name="theme-color"' in HTML, \
        "theme-color meta tag must be present for PWA status bar"


def test_hamburger_lucide_style():
    hamburger_match = re.search(
        r'id="btnHamburger"[^>]*>.*?<svg[^>]*width="24"[^>]*height="24"[^>]*stroke-width="1\.75"',
        HTML, re.DOTALL
    )
    assert hamburger_match, \
        "Hamburger must be 24x24 SVG with stroke-width 1.75 (Lucide style)"


def test_brand_neo_ico_24():
    assert 'src="static/brand/neo-ico.png"' in HTML, \
        "Brand icon must use neo-ico.png (not neo-mark.svg)"
    ico_pos = HTML.find("neo-ico.png")
    snippet = HTML[ico_pos - 50:ico_pos + 100]
    assert 'width="24" height="24"' in snippet, \
        "Brand icon must be 24x24"


def test_title_neo_in_titlebar():
    title_match = re.search(r'id="appTitlebarTitle"[^>]*>Neo<', HTML)
    assert title_match, "Titlebar must show 'Neo' text"


def test_contextual_action_new_chat():
    assert 'id="btnTitlebarNewChat"' in HTML, \
        "New chat action button must be in titlebar"


def test_contextual_action_more():
    assert 'id="btnTitlebarMore"' in HTML, \
        "More (drawer toggle) action button must be in titlebar"


def test_action_buttons_44x44():
    action_rule = re.search(r'\.app-titlebar-action\{[^}]*width:44px[^}]*height:44px', CSS)
    assert action_rule, \
        "app-titlebar-action must have 44x44 touch target"


def test_hamburger_44x44():
    hamburger_rule = re.search(r'\.app-titlebar-hamburger\{[^}]*width:44px[^}]*height:44px', CSS)
    assert hamburger_rule, \
        "Hamburger button must have 44x44 touch target (was 32x32)"


def test_header_52px_mobile():
    mobile_640 = CSS[CSS.find("@media(max-width:640px)"):]
    assert "height:52px" in mobile_640[:2000], \
        "app-titlebar must be 52px height in mobile breakpoint"


def test_no_app_region_drag_base():
    titlebar_base = re.search(r'\.app-titlebar\{([^}]+)\}', CSS)
    assert titlebar_base, "app-titlebar base rule must exist"
    assert "-webkit-app-region:drag" not in titlebar_base.group(1), \
        "app-titlebar must NOT have -webkit-app-region:drag in base (only in window-controls-overlay)"


def test_app_region_drag_only_in_wco():
    assert "display-mode: window-controls-overlay" in CSS, \
        "-webkit-app-region:drag must be guarded by @media (display-mode: window-controls-overlay)"


def test_titlebar_sub_removed():
    assert 'app-titlebar-sub' not in HTML, \
        "app-titlebar-sub element should be removed from HTML (unused debug pill)"
