"""Onda 12 — make the dashboard chat the mobile protagonist.

The user reported that on the live VPS the mobile dashboard rendered the
hero card and KPI grid as the dominant column, with the chat embed
visibly cropped — the opposite of how Claude/ChatGPT mobile apps lay out
the same kind of UI. Pin the contract that:

* the dashboard right rail collapses behind a slide-in drawer at <=760px;
* the chat panel fills the viewport so messages and composer have room;
* a topbar button toggles the drawer; an overlay closes it on backdrop tap.
"""
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
HTML = (REPO_ROOT / "static" / "index.html").read_text()
STYLE = (REPO_ROOT / "static" / "style.css").read_text()
DASHBOARD_JS = (REPO_ROOT / "static" / "dashboard.js").read_text()


def test_mobile_rail_button_present_in_topbar():
    assert 'id="btnDashboardMobileRail"' in HTML, (
        "topbar must expose the mobile rail toggle button"
    )
    assert 'onclick="toggleDashboardMobileRail()"' in HTML


def test_mobile_overlay_present():
    assert 'id="dashboardMobileOverlay"' in HTML, (
        "backdrop overlay must exist so tapping outside the drawer closes it"
    )
    overlay_idx = HTML.find('id="dashboardMobileOverlay"')
    snip = HTML[overlay_idx:overlay_idx + 200]
    assert 'toggleDashboardMobileRail(false)' in snip


def test_dashboard_js_defines_toggle():
    assert "function toggleDashboardMobileRail(" in DASHBOARD_JS
    body_idx = DASHBOARD_JS.find("function toggleDashboardMobileRail(")
    body = DASHBOARD_JS[body_idx:body_idx + 1500]
    assert "'mobile-open'" in body, "must toggle the mobile-open class on the rail"
    assert "aria-expanded" in body, "must update aria-expanded on the trigger"


def test_mobile_breakpoint_collapses_grid_to_single_column():
    """Mobile media query must override the 1fr/280px grid so the chat embed
    is the only column on screen; without this the hero card squeezes the
    chat into a sliver."""
    media = STYLE[STYLE.find("@media(max-width:760px)"):]
    media = media[: media.find("@media(min-width:761px)")]
    assert "main.main.showing-dashboard .dashboard-grid{grid-template-columns:1fr" in media, (
        "showing-dashboard .dashboard-grid must collapse to a single column on mobile"
    )


def test_mobile_breakpoint_makes_right_rail_a_drawer():
    media = STYLE[STYLE.find("@media(max-width:760px)"):]
    media = media[: media.find("@media(min-width:761px)")]
    assert "main.main.showing-dashboard .dashboard-right" in media
    rail_block = media[media.find("main.main.showing-dashboard .dashboard-right"):]
    rail_block = rail_block[: rail_block.find("}") + 1]
    assert "position:fixed" in rail_block
    assert "transition:right" in rail_block
    assert ".dashboard-right.mobile-open" in media, "open state must slide rail to right:0"


def test_mobile_breakpoint_chat_panel_fills_viewport():
    media = STYLE[STYLE.find("@media(max-width:760px)"):]
    media = media[: media.find("@media(min-width:761px)")]
    assert "main.main.showing-dashboard .dashboard-chat-panel{height:100%" in media, (
        "chat panel must fill the viewport on mobile"
    )


def test_rail_button_hidden_on_desktop():
    assert ".dashboard-mobile-rail-btn{display:none!important;}" in STYLE, (
        "mobile rail button is desktop-noise — must be hidden above the breakpoint"
    )
