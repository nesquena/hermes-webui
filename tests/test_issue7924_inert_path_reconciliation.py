"""#7924: `inert` must be reconciled on every path, not just the compact close.

The bug
-------
`inert` is a DOM attribute, not a CSS state. `_setPanelInert` armed it only
inside the panel's own compact drawer band and **returned early** on a close
that landed outside that band, and the desktop branch of
`_setWorkspacePanelMode` never called the setter at all. So the attribute
survived paths `_setPanelInert` didn't see, stranding a panel that was laid
out and visible:

1. **Close on a phone, then widen.** Close the sidebar at 390px, resize to
   641/804/1280px (rotation, iPad split view, window resize): the desktop
   sidebar stayed inert. A click on New Chat hit `.layout`, `focus()` was
   refused, there were no tab stops, and neither `toggleSidebar()` nor
   `_applySidebarState()` recovered it.
2. Same for the workspace drawer: close at 800px, resize to 1280px, reopen
   → Files was dead.
3. **Three sidebar-open paths never cleared it.** `mobileSwitchPanel`,
   `switchPanel(..., {fromRailClick:true})` and
   `_openProfileSwitchSessionBrowser` added `mobile-open` without
   un-inerting, so the drawer they opened rejected focus and clicks — and a
   tap on its own nav tab fell through to the hamburger beneath and closed
   it. Profile switching on a phone is the common trigger.

The fix
-------
`_setPanelInert` computes `armed = !open && compact` and sets OR removes the
attribute accordingly, so an out-of-band call clears it. `_setWorkspacePanelMode`
calls the setter above its compact-only branch. The three open paths call
`_setPanelInert(sidebar, true)` right after adding `mobile-open`.

These probes drive the REAL functions from `static/boot.js` /
`static/panels.js` in a real browser with the production stylesheet, because
the symptom is a live DOM attribute, and pin arm-then-resize for both panels
plus each open path after a close.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_closed_panel_a11y_followups import (  # noqa: E402
    BOOT_JS,
    STYLE_CSS,
    _AFTER_PANEL_HTML,
    _WORKSPACE_PANEL_HTML,
    _chromium,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def _extract(source: str, header: str) -> str:
    """Lift one top-level ``function name(...) { ... }`` verbatim from a file."""
    start = source.find(header)
    assert start != -1, f"{header!r} is missing from the source"
    body = source[start : source.find("\n}", start) + 2]
    assert body.count("{") == body.count("}"), f"{header!r} extraction is unbalanced"
    return body


def _sidebar_source() -> str:
    """The sidebar-side helpers under test, lifted verbatim from boot.js."""
    return "\n".join(
        _extract(BOOT_JS, name)
        for name in (
            "function _isPhoneWidthViewport(",
            "function _setPanelInert(",
        )
    )


def _workspace_source() -> str:
    """The workspace-drawer close path, lifted verbatim from boot.js."""
    names = (
        "function _workspacePanelEls(",
        "function _hasWorkspacePreviewVisible(",
        "function _isCompactWorkspaceViewport(",
        "function _isPhoneWidthViewport(",
        "function _setPanelInert(",
        "function _isFocusableControl(",
        "function _releaseFocusFromClosedPanel(",
        "function _setButtonTooltip(",
        "function _uiText(",
        "function _setWorkspacePanelMode(",
        "function syncWorkspacePanelUI(",
        "function _workspacePanelInvokerForBand(",
    )
    return "\n".join(_extract(BOOT_JS, n) for n in names)


def _inert(page, selector: str) -> bool:
    return bool(page.evaluate(
        "sel => { const el = document.querySelector(sel);"
        "  return !!(el && el.hasAttribute('inert')); }", selector))


def _sidebar_page(page, width: int):
    page.set_viewport_size({"width": width, "height": 800})
    page.set_content(
        f"""<!doctype html><html><head><style>{STYLE_CSS}</style></head>
<body>
  <div class="layout" id="layout">
    <main class="chat-shell" id="mainChat">{_AFTER_PANEL_HTML}</main>
  </div>
  <aside class="sidebar" id="sidebar"><button id="btnNewChat">New</button></aside>
  <script>$ = (id) => document.getElementById(id);</script>
  <script>{_sidebar_source()}</script>
  <script>window.__inert = () => document.getElementById('sidebar').hasAttribute('inert');</script>
</body></html>"""
    )


def _workspace_page(page, width: int):
    page.set_viewport_size({"width": width, "height": 800})
    page.set_content(
        f"""<!doctype html><html><head><style>{STYLE_CSS}</style></head><body>
<div class="layout" id="layout">
  <main class="chat-shell" id="mainChat">
    <div class="composer-left">
      <div class="composer-ws-wrap">
        <div class="composer-workspace-group ws-chip" id="composerWorkspaceGroup">
          <button class="composer-workspace-files-btn" id="btnWorkspacePanelToggle"
            type="button" title="Show workspace panel">Files</button>
          <button class="composer-workspace-chip" id="composerWorkspaceChip"
            type="button" disabled>ws</button>
        </div>
      </div>
    </div>
    {_AFTER_PANEL_HTML}
  </main>
</div>
<button class="workspace-panel-edge-toggle" id="btnWorkspacePanelEdgeToggle"
  type="button" data-tooltip="Show workspace panel" aria-label="Show workspace panel">E</button>
{_WORKSPACE_PANEL_HTML}
<script>$ = (id) => document.getElementById(id);</script>
<script>let _workspacePanelMode = 'closed';</script>
<script>const S = {{ workspacePanel: 'closed' }};</script>
<script>{_workspace_source()}</script>
</body></html>"""
    )


class TestArmThenResize:
    """Close inside the drawer band, then leave it: the panel must recover."""

    @pytest.mark.parametrize("width", [641, 804, 1280], ids=["641px", "804px", "1280px"])
    def test_sidebar_close_then_widen_clears_inert(self, width):
        playwright, browser = _chromium()
        page = browser.new_page()
        try:
            _sidebar_page(page, 390)
            # Arm: a close inside the phone band takes the attribute.
            page.evaluate("_setPanelInert(document.getElementById('sidebar'), false)")
            assert _inert(page, "#sidebar") is True, (
                "precondition: closing at 390px must arm inert on the sidebar."
            )
            # Now leave the band (rotation / iPad split view / window resize).
            page.set_viewport_size({"width": width, "height": 800})
            page.evaluate("_setPanelInert(document.getElementById('sidebar'), false)")
            still = _inert(page, "#sidebar")
        finally:
            browser.close()
            playwright.stop()
        assert still is False, (
            f"after closing at 390px and widening to {width}px the sidebar must "
            f"not stay inert — it is laid out and visible there, and an inert "
            f"attribute kills its New Chat button, its tab stops and every "
            f"focus() call (#7924)."
        )

    @pytest.mark.parametrize("width", [1280], ids=["1280px"])
    def test_sidebar_resize_back_into_band_rearms(self, width):
        """The clear must not become a permanent un-arm: going back arms again."""
        playwright, browser = _chromium()
        page = browser.new_page()
        try:
            _sidebar_page(page, 390)
            page.evaluate("_setPanelInert(document.getElementById('sidebar'), false)")
            assert _inert(page, "#sidebar") is True
            page.set_viewport_size({"width": 1280, "height": 800})
            page.evaluate("_setPanelInert(document.getElementById('sidebar'), false)")
            assert _inert(page, "#sidebar") is False
            # Back into the phone band and closed again → armed.
            page.set_viewport_size({"width": 390, "height": 800})
            page.evaluate("_setPanelInert(document.getElementById('sidebar'), false)")
            armed = _inert(page, "#sidebar")
        finally:
            browser.close()
            playwright.stop()
        assert armed is True, (
            "returning to the phone band while closed must re-arm inert."
        )

    def test_workspace_close_then_widen_clears_inert(self):
        playwright, browser = _chromium()
        page = browser.new_page()
        try:
            _workspace_page(page, 800)
            # Arm inside the compact workspace band.
            page.evaluate("_setWorkspacePanelMode('closed')")
            assert _inert(page, ".rightpanel") is True, (
                "precondition: closing the drawer at 800px must arm inert."
            )
            # Widen to desktop and reopen — the desktop branch used to skip the
            # setter entirely, so the reopened Files panel was dead.
            page.set_viewport_size({"width": 1280, "height": 800})
            page.evaluate("_setWorkspacePanelMode('browse')")
            still = _inert(page, ".rightpanel")
        finally:
            browser.close()
            playwright.stop()
        assert still is False, (
            "closing the workspace drawer at 800px and widening to 1280px must "
            "not leave it inert: reopening gave a Files panel whose controls "
            "rejected focus and clicks (#7924)."
        )


class TestOpenPathsClearInert:
    """Every path that adds `mobile-open` must un-inert the drawer."""

    @pytest.mark.parametrize(
        ("open_path", "source_attr"),
        [
            ("mobileSwitchPanel", BOOT_JS),
            ("switchPanel", PANELS_JS),
            ("_openProfileSwitchSessionBrowser", PANELS_JS),
        ],
        ids=["mobileSwitchPanel", "switchPanel(fromRailClick)", "_openProfileSwitchSessionBrowser"],
    )
    def test_open_path_clears_inert(self, open_path, source_attr):
        """Each open path must call the setter with `true` after adding mobile-open.

        Source-level rather than behavioural: the three functions live in two
        different files and need their own module scope plus a large amount of
        surrounding machinery (rail state, profile switching) to run in a page,
        and what the reviewer asked for is that the call exists on each path.
        The behavioural half is covered by TestArmThenResize, which proves the
        setter now clears on an out-of-band call — the thing these paths rely on.
        """
        start = source_attr.find(f"function {open_path}(")
        assert start != -1, f"{open_path} is missing from the source"
        body = source_attr[start : source_attr.find("\n}", start) + 2]
        assert "_setPanelInert(" in body, (
            f"{open_path} adds mobile-open without un-inerting: the drawer it "
            f"opens rejects focus and clicks, and a tap on its own nav tab "
            f"falls through to the hamburger beneath (#7924)."
        )
        # It must be the OPEN direction, not a re-arm.
        for line in body.split("\n"):
            if "_setPanelInert(" in line and "typeof" not in line:
                assert "true" in line, (
                    f"{open_path} must clear inert (open direction), got {line!r}."
                )

    def test_switch_panel_rail_click_path_clears_inert(self):
        """`switchPanel` gates the open on fromRailClick; pin that exact branch."""
        start = PANELS_JS.find("function switchPanel(")
        assert start != -1
        body = PANELS_JS[start : PANELS_JS.find("\n}", start) + 2]
        branch = body[body.find("fromRailClick") :]
        assert "_setPanelInert(" in branch, (
            "the fromRailClick open branch of switchPanel must clear inert (#7924)."
        )
