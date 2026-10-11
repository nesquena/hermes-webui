"""Browser regression coverage for the compact-viewport workspace drawer's tab order.

On a compact viewport the closed workspace drawer keeps ``display:flex`` and parks
300px off-screen, so its five controls (``btnUploadWorkspace``, ``btnWorkspacePrefs``,
``workspaceFileInput``, ``workspaceFilesTab``, ``workspaceArtifactsTab``) stay in the
keyboard tab order: a keyboard user tabbing through the composer walks into
invisible controls and focus disappears off-screen — and activating
``workspaceFileInput`` from there opens a file picker for a panel that is not on
screen (#7713).

The closed drawer must be inert, and the open drawer focusable — on the shipped
compact band (``max-width:640px`` slide-in overlay). The probe
drives the browser's REAL tab sequence from a control placed immediately before
the panel, because that is the walk the user takes; a computed-style or
``el.tabIndex`` assertion cannot see a future rule that re-enables focus on a
hidden subtree (and under ``visibility:hidden`` Chrome still honours a
programmatic ``el.focus()`` even though the element is no longer tabbable).

The 641-900px band is deliberately NOT covered here: its off-canvas geometry
belongs to #6952 (still open), and until that lands the band uses
``display:none``, which already keeps the subtree out of the tab order. Swapping
that band to visibility now would reserve 300px of layout for an invisible flex
item (#7866 review).

The production stylesheet is loaded unchanged, so the same test also pins that the
open drawer keeps working after the inert rules were added.
"""
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")

# The shipped compact band: the <=640px slide-in overlay, probed at its widest
# and narrowest points so the fix is pinned at both edges.
_BANDS = ({"width": 390, "height": 780}, {"width": 640, "height": 780})

# The drawer's control set as named in the issue: two icon buttons, the hidden
# file input, and the two tab-strip buttons — all of them reachable by Tab once
# the drawer is open, none of them when it is closed.
_TABBABLE_CONTROLS = (
    "btnUploadWorkspace",
    "btnWorkspacePrefs",
    "workspaceFileInput",
    "workspaceFilesTab",
    "workspaceArtifactsTab",
)

_TAB_STEPS = 14


def _panel_html() -> str:
    controls = "\n".join(
        f'      <button class="panel-icon-btn" id="{control_id}">{control_id}</button>'
        if not control_id.endswith("FileInput")
        else f'      <input type="file" id="{control_id}" class="file-input-visually-hidden">'
        for control_id in _TABBABLE_CONTROLS
    )
    return f"""<div class="layout" id="layout">
  <button id="beforePanel">Before the drawer</button>
  <main class="chat-shell"></main>
  <aside class="rightpanel" id="workspacePanel">
    <div class="panel-header">
{controls}
    </div>
    <div class="workspace-panel-tabs" role="tablist"></div>
  </aside>
</div>"""


def _page_script() -> str:
    """Helpers that report where the real tab sequence actually lands."""
    return """
    window.__focusBeforePanel = () => document.getElementById('beforePanel').focus();
    window.__activeDescriptor = () => {
      const el = document.activeElement;
      if (!el || el === document.body) return null;
      return {
        id: el.id || null,
        inPanel: !!el.closest('.rightpanel'),
      };
    };
    window.__openDrawer = () => {
      document.querySelector('.rightpanel').classList.add('mobile-open');
    };
    """


def _tab_hits_in_panel(page) -> list:
    """Walk the browser's real tab sequence and report panel hits, by control id."""
    page.evaluate("window.__focusBeforePanel()")
    hits: list[str] = []
    for _ in range(_TAB_STEPS):
        page.keyboard.press("Tab")
        state = page.evaluate("window.__activeDescriptor()")
        if state is None:
            break
        if state["inPanel"]:
            hits.append(state["id"] or "<unnamed>")
            if len(hits) >= len(_TABBABLE_CONTROLS):
                break
    return hits


@pytest.mark.parametrize("viewport", _BANDS, ids=["390px", "640px"])
def test_closed_compact_drawer_is_out_of_the_tab_order(viewport):
    """The closed compact drawer must expose none of its controls to the tab
    sequence, and the open drawer must expose all of them (#7713)."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the drawer a11y browser test")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = browser.new_page()
        try:
            page.set_viewport_size(viewport)
            page.set_content(f"""<!doctype html><html><head><style>{STYLE_CSS}</style></head>
<body>{_panel_html()}<script>{_page_script()}</script></body></html>""")
            closed_hits = _tab_hits_in_panel(page)
            closed_pointer_events = page.evaluate(
                "() => getComputedStyle(document.querySelector('.rightpanel')).pointerEvents"
            )
            page.evaluate("window.__openDrawer()")
            # The panel's controls animate (transition:all), so let the open
            # transition settle before walking the open tab order.
            page.wait_for_timeout(400)
            open_hits = _tab_hits_in_panel(page)
        finally:
            browser.close()

    assert closed_hits == [], (
        f"the closed compact drawer at {viewport['width']}px still puts "
        f"{closed_hits} in the keyboard tab order"
    )
    assert sorted(open_hits) == sorted(_TABBABLE_CONTROLS), (
        f"the open compact drawer must expose every control at "
        f"{viewport['width']}px, got {open_hits}"
    )
    assert closed_pointer_events == "none", (
        "the parked drawer must not swallow clicks aimed at the chat underneath it"
    )
