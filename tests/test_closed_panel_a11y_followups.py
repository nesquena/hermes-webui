"""Browser regression coverage for the follow-up a11y gaps left by #7866.

#7866 took the closed compact workspace drawer out of the tab order. Its review
comment (Codex, 2026-09-30) left three small non-blocking follow-ups, all with the
same root cause as the drawer itself: an off-canvas panel that stays laid out so it
can animate keeps its whole subtree reachable by Tab.

1. The closed LEFT sidebar parks off-screen via translateX(-100%) and carried the
   same defect: every control in it (session list, new-chat, file actions) stayed
   tabbable, so a keyboard user tabbing forward walked off-screen into an invisible
   tree.
2. #workspaceFileInput, the programmatic target of triggerWorkspaceUpload(), was a
   tab stop even with the drawer OPEN — one Tab press moved focus onto a
   1px, opacity:0, left:-9999px input and focus appeared to vanish.
3. Closing a panel that had focus inside it left document.activeElement on the now
   invisible control (visibility:hidden does not clear it), so the next Tab
   restarted from that dead node and focus vanished for a press.

These probes drive the browser's REAL tab sequence from a control placed immediately
before the panel, because that is the walk the user takes; a computed-style or
el.tabIndex assertion cannot see a future rule that re-enables focus on a hidden
subtree. The production stylesheet and the real index.html markup are loaded
unchanged, so the same tests pin that the open panels still work afterwards.

The 641-900px band is deliberately NOT covered, for the reason given in #7866: that
band uses display:none (its off-canvas geometry is #6952's open scope) and already
keeps its subtree out of the tab order.
"""
from pathlib import Path
import re

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
WORKSPACE_JS = (REPO_ROOT / "static" / "workspace.js").read_text(encoding="utf-8")

# The shipped compact band: the <=640px slide-in overlay, probed at its widest and
# narrowest points so the fix is pinned at both edges.
_BANDS = ({"width": 390, "height": 780}, {"width": 640, "height": 780})

# A representative slice of the sidebar's control set: the session-list search box
# plus the new-chat and reload buttons in the titlebar. If the closed sidebar is
# inert, none of them can be reached; if the fix regresses, the walk hits all three.
_SIDEBAR_CONTROLS = (
    "sidebarSearch",
    "btnNewChat",
    "btnReload",
)

_TAB_STEPS = 14


def _sidebar_html() -> str:
    controls = "\n".join(
        f'    <input id="{cid}">' if cid == "sidebarSearch" else f'    <button id="{cid}">{cid}</button>'
        for cid in _SIDEBAR_CONTROLS
    )
    return f"""<div class="layout" id="layout">
  <button id="beforeSidebar">Before the sidebar</button>
  <aside class="sidebar" id="sidebar">
{controls}
  </aside>
  <main class="chat-shell"></main>
</div>"""


def _page_script() -> str:
    """Helpers that report where the real tab sequence actually lands."""
    return """
    window.__focusBeforeSidebar = () => document.getElementById('beforeSidebar').focus();
    window.__activeDescriptor = () => {
      const el = document.activeElement;
      if (!el || el === document.body) return null;
      return {
        id: el.id || null,
        inSidebar: !!el.closest('.sidebar'),
      };
    };
    window.__openSidebar = () => {
      document.querySelector('.sidebar').classList.add('mobile-open');
    };
    window.__sidebarPointerEvents = () =>
      getComputedStyle(document.querySelector('.sidebar')).pointerEvents;
    window.__sidebarVisibility = () =>
      getComputedStyle(document.querySelector('.sidebar')).visibility;
    """


def _tab_hits_in_sidebar(page) -> list:
    """Walk the browser's real tab sequence and report sidebar hits, by control id."""
    page.evaluate("window.__focusBeforeSidebar()")
    hits: list[str] = []
    for _ in range(_TAB_STEPS):
        page.keyboard.press("Tab")
        state = page.evaluate("window.__activeDescriptor()")
        if state is None:
            break
        if state["inSidebar"]:
            hits.append(state["id"] or "<unnamed>")
            if len(hits) >= len(_SIDEBAR_CONTROLS):
                break
    return hits


def _chromium():
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the sidebar a11y browser test")
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    return playwright, browser


@pytest.mark.parametrize("viewport", _BANDS, ids=["390px", "640px"])
def test_closed_compact_sidebar_is_out_of_the_tab_order(viewport):
    """Follow-up 1: the closed left sidebar must expose none of its controls to the
    tab sequence, and the open sidebar must expose all of them."""
    playwright, browser = _chromium()
    page = browser.new_page()
    try:
        page.set_viewport_size(viewport)
        page.set_content(
            f"""<!doctype html><html><head><style>{STYLE_CSS}</style></head>
<body>{_sidebar_html()}<script>{_page_script()}</script></body></html>"""
        )
        closed_hits = _tab_hits_in_sidebar(page)
        closed_visibility = page.evaluate("window.__sidebarVisibility()")
        closed_pointer_events = page.evaluate("window.__sidebarPointerEvents()")
        page.evaluate("window.__openSidebar()")
        # The sidebar animates its transform, so let the open transition settle
        # before walking the open tab order.
        page.wait_for_timeout(400)
        open_visibility = page.evaluate("window.__sidebarVisibility()")
        open_hits = _tab_hits_in_sidebar(page)
    finally:
        browser.close()
        playwright.stop()

    assert closed_hits == [], (
        f"the closed compact sidebar at {viewport['width']}px still puts "
        f"{closed_hits} in the keyboard tab order"
    )
    assert sorted(open_hits) == sorted(_SIDEBAR_CONTROLS), (
        f"the open compact sidebar must expose every control at "
        f"{viewport['width']}px, got {open_hits}"
    )
    assert closed_visibility == "hidden", (
        "the parked sidebar must be visibility:hidden, which is what takes its "
        f"subtree out of the tab order (got {closed_visibility!r})"
    )
    assert open_visibility == "visible", (
        f"the open sidebar must be visible at {viewport['width']}px, "
        f"got {open_visibility!r}"
    )
    assert closed_pointer_events == "none", (
        "the parked sidebar must not swallow clicks aimed at the chat underneath it"
    )


def test_closed_sidebar_slide_animation_is_not_cut_short():
    """The inert rules must not kill the close animation: visibility flips only
    after the 250ms slide-out, so the sidebar is still visible while it slides."""
    # Anchor on the compact-band rule itself rather than splitting the stylesheet
    # into media-query segments, which breaks as soon as another query follows.
    anchor = ".sidebar{position:fixed;left:0;top:0;bottom:0;width:100vw"
    start = STYLE_CSS.find(anchor)
    assert start != -1, "the compact sidebar rule is missing from style.css"
    sidebar_rule = STYLE_CSS[start : STYLE_CSS.index("}", start)]
    open_anchor = ".sidebar.mobile-open{"
    open_start = STYLE_CSS.index(open_anchor, start)
    open_rule = STYLE_CSS[open_start : STYLE_CSS.index("}", open_start)]

    # Closed: the delayed visibility step must outlast the transform, otherwise the
    # panel blanks out before it has finished sliding away.
    assert "visibility 0s linear .25s" in sidebar_rule, (
        "the closed sidebar must delay its visibility flip past the 250ms "
        f"transform transition, got {sidebar_rule!r}"
    )
    assert "transform .25s ease" in sidebar_rule, (
        f"the closed sidebar must keep its transform transition, got {sidebar_rule!r}"
    )
    assert "visibility:hidden" in sidebar_rule, (
        f"the closed sidebar must be visibility:hidden, got {sidebar_rule!r}"
    )
    # Open: the flip must be immediate so the panel is focusable the moment it
    # arrives, not one transition late.
    assert "visibility 0s linear 0s" in open_rule, (
        f"the open sidebar must apply visibility immediately, got {open_rule!r}"
    )
    assert "visibility:visible" in open_rule, (
        f"the open sidebar must be visible, got {open_rule!r}"
    )


def test_workspace_file_input_is_not_a_tab_stop():
    """Follow-up 2: the hidden upload target must be reachable only
    programmatically. The visible #btnUploadWorkspace button is the tab stop."""
    assert 'id="workspaceFileInput"' in INDEX_HTML, (
        "the workspace file input is missing from index.html"
    )
    input_tag = next(
        line
        for line in INDEX_HTML.splitlines()
        if 'id="workspaceFileInput"' in line
    )
    assert 'tabindex="-1"' in input_tag, (
        "#workspaceFileInput must carry tabindex=\"-1\" so it stops swallowing a Tab "
        f"press while focus is visibly nowhere; got {input_tag.strip()!r}"
    )
    # The user-facing affordance must remain a normal, focusable button.
    assert 'id="btnUploadWorkspace"' in INDEX_HTML, (
        "the visible upload button must stay in the markup"
    )
    upload_tag = next(
        line for line in INDEX_HTML.splitlines() if 'id="btnUploadWorkspace"' in line
    )
    assert "tabindex" not in upload_tag, (
        "the visible upload button must stay in the tab order; a tabindex here "
        f"would swap one lost focus for another: {upload_tag.strip()!r}"
    )


def test_upload_still_opens_the_picker_from_the_visible_button():
    """tabindex=\"-1\" must not break the upload path: triggerWorkspaceUpload()
    drives the input with a programmatic .click(), which does not need focus."""
    start = WORKSPACE_JS.find("function triggerWorkspaceUpload(")
    assert start != -1, "triggerWorkspaceUpload() is missing from workspace.js"
    body = WORKSPACE_JS[start : WORKSPACE_JS.find("\n}", start) + 2]
    assert "input.click()" in body, (
        "triggerWorkspaceUpload() must still open the picker via a programmatic "
        f"click (tabindex=\"-1\" only removes the tab stop); got {body!r}"
    )
    assert "focus()" not in body, (
        "triggerWorkspaceUpload() must not depend on focusing the input, or "
        "tabindex=\"-1\" would have to come back out"
    )


def test_closing_a_panel_releases_focus_from_inside_it():
    """Follow-up 3: closing a panel that held the focus must park focus on <body>,
    and must NOT steal it when the focus was somewhere else entirely (that would
    yank a user out of the composer mid-sentence)."""
    assert "function _releaseFocusFromClosedPanel(" in BOOT_JS, (
        "the focus-release helper is missing from boot.js"
    )
    start = BOOT_JS.find("function _releaseFocusFromClosedPanel(")
    helper = BOOT_JS[start : BOOT_JS.find("\n}", start) + 2]
    assert "panel.contains(active)" in helper, (
        "the helper must only rescue focus that is actually inside the panel being "
        f"closed; got {helper!r}"
    )
    assert "blur()" in helper, (
        f"the helper must release the focus; got {helper!r}"
    )

    # Both close paths must call it: the workspace drawer (via _setWorkspacePanelMode)
    # and the left sidebar (via closeMobileSidebar).
    mode_start = BOOT_JS.find("function _setWorkspacePanelMode(")
    mode_body = BOOT_JS[mode_start : BOOT_JS.find("\n}", mode_start) + 2]
    assert "_releaseFocusFromClosedPanel(panel, returnFocusTo)" in mode_body, (
        "_setWorkspacePanelMode() must release focus when it closes the drawer, "
        f"passing the explicit-dismiss fallback through; got {mode_body!r}"
    )
    # Only on the close leg: opening must never steal focus.
    assert "if(!open)_releaseFocusFromClosedPanel(panel, returnFocusTo);" in mode_body, (
        "the focus release must be guarded on the close leg so opening the panel "
        f"leaves the user's focus alone; got {mode_body!r}"
    )

    close_start = BOOT_JS.find("function closeMobileSidebar(")
    close_body = BOOT_JS[close_start : BOOT_JS.find("\n}", close_start) + 2]
    assert "_releaseFocusFromClosedPanel(sidebar, returnFocusTo)" in close_body, (
        "closeMobileSidebar() must release focus from the parked sidebar, passing "
        f"the explicit-dismiss fallback through; got {close_body!r}"
    )
    # The fallback is opt-in: every content-selection caller (there are dozens) calls
    # closeMobileSidebar() bare and must keep landing on <body>, because those paths
    # already move focus to the composer.
    assert "if(_isPhoneWidthViewport())" in close_body, (
        "the focus rescue must stay guarded to the phone-width band; "
        f"got {close_body!r}"
    )


def test_explicit_dismiss_hands_focus_back_to_the_invoking_control():
    """UX follow-up: an explicit dismiss (the "Close menu" X, the overlay, tapping
    outside the workspace drawer) must return focus to the control that opened the
    panel, so the next Tab continues from where the user was instead of restarting at
    the top of the document.

    The fallback is opt-in, threaded from the dismiss path only. This pins both ends:
    a dismiss function that passes the invoker, and a default close that does not."""
    # The sidebar's explicit-dismiss entry point passes the hamburger...
    assert "function dismissMobileSidebar(" in BOOT_JS, (
        "boot.js must expose an explicit-dismiss sidebar closer"
    )
    dismiss_start = BOOT_JS.find("function dismissMobileSidebar(")
    dismiss_body = BOOT_JS[dismiss_start : BOOT_JS.find("\n}", dismiss_start) + 2]
    assert "closeMobileSidebar($('btnHamburger'))" in dismiss_body, (
        "the sidebar's explicit dismiss must hand focus back to the hamburger that "
        f"opened the drawer; got {dismiss_body!r}"
    )

    # ...and both markup paths use it, while the plain close stays available.
    assert 'onclick="dismissMobileSidebar()" data-tooltip="Close menu"' in INDEX_HTML, (
        'the "Close menu" X must use the explicit-dismiss path'
    )
    assert (
        '<div class="mobile-overlay" id="mobileOverlay" onclick="dismissMobileSidebar()">'
        in INDEX_HTML
    ), "the mobile overlay must use the explicit-dismiss path"
    # The content-selection contract is unchanged: the default takes no fallback, so
    # _releaseFocusFromClosedPanel blurs onto <body> exactly as #7866 left it.
    assert "function closeMobileSidebar(returnFocusTo){" in BOOT_JS, (
        "closeMobileSidebar() must keep taking the fallback as an OPTIONAL argument, "
        "so its many content-selection callers are unaffected"
    )

    # The workspace drawer's dismiss path (tapping outside it) returns focus to the
    # control that opens the drawer in the current band. Above 900px that is the edge
    # toggle; below it the toggle is display:none, so the composer's workspace
    # button is the reachable invoker.
    chat_start = BOOT_JS.find("function closeMobileWorkspacePanelFromChat(")
    assert chat_start != -1, "the workspace drawer's outside-tap closer is missing"
    chat_body = BOOT_JS[chat_start : BOOT_JS.find("\n}", chat_start) + 2]
    assert "closeWorkspacePanel(_workspacePanelInvokerForBand())" in chat_body, (
        "tapping outside the drawer is an explicit dismiss and must hand focus back "
        f"to the band-appropriate invoker; got {chat_body!r}"
    )


def test_release_helper_only_focuses_an_explicit_visible_fallback():
    """The helper may move focus ONLY to a caller-supplied fallback that is actually
    reachable. It must never invent a landing spot, and it must never focus a hidden
    control — that would strand focus on an invisible node, which is the very bug
    #7866's follow-up fixed. With no fallback (or an unreachable one) it still blurs
    onto <body>."""
    start = BOOT_JS.find("function _releaseFocusFromClosedPanel(")
    helper = BOOT_JS[start : BOOT_JS.find("\n}", start) + 2]
    assert "fallback&&_isFocusableControl(fallback)" in helper, (
        "the fallback must be gated on _isFocusableControl() before being focused, or "
        f"a hidden invoker re-creates the invisible-focus bug; got {helper!r}"
    )
    assert "document.activeElement===fallback" in helper, (
        "after focus() the helper must verify the fallback actually took it, and fall "
        "through to the blur path when it did not — a control that silently refuses "
        f"focus() would leave focus stranded on the hidden panel; got {helper!r}"
    )
    # The neutral landing survives as the fallback-of-last-resort.
    assert "blur()" in helper, (
        f"the helper must still blur when there is no usable fallback; got {helper!r}"
    )

    # The guard itself must reject the two ways a control can be unreachable.
    # Pin the exact code tokens, NOT the bare word: the guard's comments legitimately
    # mention "visibility:hidden" and "display:none", and a substring match on those
    # words stays green even with the checks deleted.
    guard_start = BOOT_JS.find("function _isFocusableControl(")
    assert guard_start != -1, "_isFocusableControl() is missing from boot.js"
    guard = BOOT_JS[guard_start : BOOT_JS.find("\n}", guard_start) + 2]
    # Strip comments so a word in prose can never satisfy the assertion.
    guard_code = re.sub(r"//[^\n]*", "", guard)
    assert "el.isConnected" in guard_code, (
        f"the guard must reject detached controls; got {guard_code!r}"
    )
    assert "getComputedStyle(el).visibility" in guard_code, (
        "the guard must reject visibility:hidden controls — a fallback that is itself "
        f"hidden would take focus and immediately strand it; got {guard_code!r}"
    )
    assert "el.offsetParent" in guard_code, (
        f"the guard must reject display:none controls; got {guard_code!r}"
    )
    assert "getClientRects().length" in guard_code, (
        "the guard must also reject controls with no client rects: a "
        "position:fixed element is exempt from the offsetParent test whether it "
        "is painted or not, so display:none fixed controls (#btnWorkspacePanel"
        "EdgeToggle in the <=900px band) pass it and focus() silently fails; "
        f"got {guard_code!r}"
    )


# A desktop-width viewport: the band where closeMobileSidebar() runs but the
# sidebar stays laid out and visible.
_DESKTOP_BAND = {"width": 1280, "height": 800}


def _boot_functions_source() -> str:
    """The real production helpers under test: the phone-width predicate, the focus
    rescue and closeMobileSidebar(), lifted verbatim from boot.js. Sourcing them
    (rather than restating them) keeps the probe honest — it fails if boot.js
    changes its mind, instead of testing a copy that drifted away from it."""
    source: list[str] = []
    for name in (
        "function _isPhoneWidthViewport(",
        "function _setPanelInert(",
        "function _isFocusableControl(",
        "function _releaseFocusFromClosedPanel(",
        "function closeMobileSidebar(",
        "function dismissMobileSidebar(",
    ):
        start = BOOT_JS.find(name)
        assert start != -1, f"{name} is missing from boot.js"
        source.append(BOOT_JS[start : BOOT_JS.find("\n}", start) + 2])
    return "\n".join(source)


@pytest.mark.parametrize(
    ("band", "expects_rescue"),
    [(_DESKTOP_BAND, False), ({"width": 390, "height": 780}, True)],
    ids=["1280px", "390px"],
)
def test_closed_sidebar_focus_rescue_is_guarded_to_the_phone_band(band, expects_rescue):
    """maintainer regression: closeMobileSidebar() runs on desktop too — opening a
    session calls it unconditionally — and at desktop width the sidebar is still
    laid out and visible, so releasing focus there would dump keyboard users on
    <body> on every session open. The rescue must fire only in the ≤640px band
    where the sidebar actually hides; a focused control elsewhere on the page
    (the session button) keeps its focus at desktop width."""
    playwright, browser = _chromium()
    page = browser.new_page()
    try:
        page.set_viewport_size(band)
        page.set_content(
            f"""<!doctype html><html><head><style>{STYLE_CSS}</style></head>
<body>
  <button id="openSessionBtn">Session</button>
  <div class="mobile-overlay" id="mobileOverlay"></div>
  <aside class="sidebar" id="sidebar"><button id="btnNewChat">New</button></aside>
  <script>$ = (id) => document.getElementById(id);</script>
  <script>{_boot_functions_source()}</script>
</body></html>"""
        )
        # Park focus directly on the sidebar's own control — the real scenario:
        # the button that was in focus when the panel got closed.
        page.evaluate("document.getElementById('btnNewChat').focus()")
        page.evaluate("closeMobileSidebar()")
        focused = page.evaluate("() => document.activeElement && document.activeElement.id")
    finally:
        browser.close()
        playwright.stop()

    if expects_rescue:
        assert focused != "btnNewChat", (
            f"at {band['width']}px the closed sidebar is visibility:hidden; the "
            f"rescue must park focus off its now-invisible control (got {focused!r})"
        )
    else:
        assert focused == "btnNewChat", (
            f"at {band['width']}px the sidebar stays visible, so closeMobileSidebar() "
            f"must not steal focus from a focused control (got {focused!r})"
        )


def _dismiss_page_html() -> str:
    """The markup the real walk needs: the hamburger that opens the drawer, the
    sidebar with its "Close menu" X, and the composer control a content-selection
    close would target instead."""
    return f"""<!doctype html><html><head><style>{STYLE_CSS}</style></head>
<body>
  <button id="btnHamburger">Menu</button>
  <div class="mobile-overlay" id="mobileOverlay"></div>
  <aside class="sidebar" id="sidebar">
    <button id="btnCloseMenu" onclick="dismissMobileSidebar()">Close menu</button>
    <button id="btnNewChat">New</button>
  </aside>
  <main class="chat-shell"><textarea id="msg"></textarea></main>
  <script>$ = (id) => document.getElementById(id);</script>
  <script>{_boot_functions_source()}</script>
  <script>
    // No inline visibility: the production stylesheet already parks the closed
    // sidebar at visibility:hidden and only .mobile-open lifts it, so adding a
    // style attribute here would shadow the very rule under test.
    window.__openSidebar = () => {{
      document.querySelector('.sidebar').classList.add('mobile-open');
    }};
    window.__activeId = () => document.activeElement && document.activeElement.id;
  </script>
</body></html>"""


def test_tab_to_close_menu_then_enter_lands_focus_on_the_hamburger():
    """The UX review's ask, as the walk a user actually takes: focus the hamburger,
    open the drawer, Tab onto the "Close menu" X, press Enter, and the next Tab must
    continue from the hamburger rather than restarting at the top of the document.

    A computed-style or el.tabIndex assertion cannot see this: Chrome still honours a
    programmatic .focus() under visibility:hidden, so the failure mode is invisible to
    anything that does not drive the real key events."""
    playwright, browser = _chromium()
    page = browser.new_page()
    try:
        page.set_viewport_size({"width": 390, "height": 780})
        page.set_content(_dismiss_page_html())
        page.evaluate("window.__openSidebar()")

        # Tab onto the X from inside the drawer, then dismiss it with the keyboard.
        page.evaluate("document.getElementById('btnCloseMenu').focus()")
        page.keyboard.press("Enter")
        after_dismiss = page.evaluate("window.__activeId()")
        # The close transition delays the visibility flip past the 250ms slide-out
        # (transition: transform .25s ease, visibility 0s linear .25s), so the panel
        # is still hit-testable while it animates. Wait it out — that is the window a
        # real user acts in, and it is when the parked sidebar must be inert.
        page.wait_for_timeout(400)
        assert page.evaluate(
            "getComputedStyle(document.querySelector('.sidebar')).visibility"
        ) == "hidden", "the sidebar must be invisible once its close animation ends"
        page.keyboard.press("Tab")
        after_tab = page.evaluate("window.__activeId()")
    finally:
        browser.close()
        playwright.stop()

    assert after_dismiss == "btnHamburger", (
        "an explicit dismiss must hand focus to the hamburger that opened the drawer, "
        f"not strand it on <body> (focus went to {after_dismiss!r})"
    )
    # The walk must resume from the hamburger and SKIP the whole parked sidebar —
    # landing on msg (the composer, next in DOM order after the aside) is exactly
    # right. Landing on btnNewChat or btnCloseMenu would mean the closed sidebar is
    # back in the tab order, which is the bug this PR exists to fix.
    assert after_tab == "msg", (
        "the next Tab must continue from the hamburger, skipping the closed sidebar "
        f"entirely and landing on the composer (expected msg, got {after_tab!r})"
    )


def test_the_closing_sidebar_is_inert_before_its_visibility_flip():
    """The 250ms closing window must not re-admit the sidebar to the tab order.

    The closed sidebar delays its visibility flip past the 0.25s slide-out so the
    animation still runs (pinned by
    test_closed_sidebar_slide_animation_is_not_cut_short), and until that flip the
    subtree is still laid out and tabbable. A user who dismisses the drawer and
    immediately Tabs walks straight back into the panel that is disappearing, and
    when the flip lands the focus it just moved is stranded on an invisible
    control — the exact failure this PR series removes.

    The inert CSS property closes the window: it takes the subtree out of the tab
    order the moment the panel closes, with no delay, and still lets the transform
    transition play. The walk below drives a Tab into the window (immediately after
    Enter, with no wait) and asserts it lands on the composer rather than back on
    the drawer's controls.
    """
    playwright, browser = _chromium()
    page = browser.new_page()
    try:
        page.set_viewport_size({"width": 390, "height": 780})
        page.set_content(_dismiss_page_html())
        page.evaluate("window.__openSidebar()")
        page.evaluate("document.getElementById('btnCloseMenu').focus()")
        page.keyboard.press("Enter")
        # No wait_for_timeout here on purpose: this is the walk inside the 250ms
        # slide-out window, before visibility has flipped to hidden.
        page.keyboard.press("Tab")
        during_transition = page.evaluate("window.__activeId()")
        still_visible = page.evaluate(
            "() => getComputedStyle(document.querySelector('.sidebar')).visibility"
        )
    finally:
        browser.close()
        playwright.stop()

    assert still_visible == "visible", (
        "the probe must have Tabbed inside the 250ms slide-out window, before the "
        f"delayed visibility flip (got {still_visible!r}) — waiting longer here would "
        "silently turn this test into a copy of the settled-state walk"
    )
    assert during_transition == "msg", (
        "a Tab during the closing animation must skip the sidebar and continue from "
        "the hamburger into the composer — landing on a sidebar control means the "
        f"panel is back in the tab order mid-slide (got {during_transition!r})"
    )


def test_the_closed_panels_are_marked_inert_when_they_close():
    """The closing-window coverage above only proves the browser honours inert in
    this Chromium. This pins the boot.js wiring itself, comment-stripped, so the
    250ms window cannot reopen: every close path must set the inert attribute, and
    every open path must clear it — a missing clear would leave a closed-then-
    reopened panel permanently unusable.

    The static half is deliberate: the attribute is the contract, and a browser
    probe cannot see a path boot.js never takes."""
    closed_call = "_setPanelInert(sidebar, false);"
    open_calls = (
        "_setPanelInert(sidebar, true);",
        "_setPanelInert(panel, open);",
    )
    assert "function _setPanelInert(" in BOOT_JS, (
        "_setPanelInert() is missing from boot.js"
    )
    for call in (closed_call, *open_calls):
        assert call in BOOT_JS, f"{call} is missing from boot.js"
    # The inert setter must arm on close by default and only clear on open: the
    # helper itself is what decides, so an inverted call site cannot ship silently.
    setter = BOOT_JS[BOOT_JS.index("function _setPanelInert(") :]
    setter = setter[: setter.index("\n}")]
    assert "removeAttribute('inert')" in setter, (
        "an open panel must clear the inert attribute, or a reopened drawer stays "
        f"inert — got {setter!r}"
    )
    assert "setAttribute('inert'" in setter, (
        "a closed panel must take the inert attribute, or the closing window stays "
        f"tabbable — got {setter!r}"
    )


@pytest.mark.parametrize(
    ("band", "arms"),
    [
        ({"width": 390, "height": 780}, True),
        ({"width": 1280, "height": 800}, False),
    ],
    ids=["390px", "1280px"],
)
def test_inert_arms_only_inside_the_panels_own_drawer_band(band, arms):
    """inert is an HTML attribute: unlike the CSS panel states it does not become
    harmless outside the drawer band — it kills the visible desktop pane outright.

    closeMobileSidebar() runs at every width (opening a session calls it
    unconditionally, and _applySidebarState() calls it when leaving phone widths),
    so arming without a band guard would strand the whole desktop sidebar. The
    guard is pinned here rather than assumed: the 1280px case below fails if the
    band check is ever dropped, which no existing probe would otherwise see."""
    playwright, browser = _chromium()
    page = browser.new_page()
    try:
        page.set_viewport_size(band)
        page.set_content(_dismiss_page_html())
        page.evaluate("window.__openSidebar()")
        page.evaluate("document.getElementById('btnCloseMenu').focus()")
        page.evaluate("dismissMobileSidebar()")
        inert_armed = page.evaluate(
            "() => document.getElementById('sidebar').hasAttribute('inert')"
        )
        page.evaluate("document.getElementById('btnCloseMenu').focus()")
        sidebar_still_focusable = page.evaluate("window.__activeId()") == "btnCloseMenu"
    finally:
        browser.close()
        playwright.stop()

    if arms:
        assert inert_armed, (
            f"at {band['width']}px the compact drawer must take the inert attribute "
            "on close, or the 250ms closing window stays tabbable"
        )
    else:
        assert not inert_armed, (
            f"at {band['width']}px the sidebar is a normal visible pane: inert would "
            "kill it outright, so the band guard must skip arming it"
        )
        assert sidebar_still_focusable, (
            f"the sidebar must stay usable at {band['width']}px after a close — "
            "inert had stranded the desktop pane"
        )


def test_a_content_selection_close_still_lands_on_body():
    """The other half of the contract: picking a session or a panel item already moves
    focus to the composer, so the plain close must NOT pull it back to the hamburger.
    Guarding the fallback to the explicit-dismiss path is what keeps #7866's behaviour
    intact for the dozens of content-selection callers."""
    playwright, browser = _chromium()
    page = browser.new_page()
    try:
        page.set_viewport_size({"width": 390, "height": 780})
        page.set_content(_dismiss_page_html())
        page.evaluate("window.__openSidebar()")
        # A session button has focus, then the content-selection path runs and focuses
        # the composer — the real sequence in _openSidebarSession().
        page.evaluate("document.getElementById('btnNewChat').focus()")
        page.evaluate("closeMobileSidebar(); $('msg').focus();")
        after_close = page.evaluate("window.__activeId()")
    finally:
        browser.close()
        playwright.stop()

    assert after_close == "msg", (
        "closeMobileSidebar() called bare is the content-selection path and must not "
        f"steal focus back to the hamburger (focus went to {after_close!r})"
    )


def test_an_unreachable_fallback_is_never_focused():
    """Why _isFocusableControl() exists, stated as a measured browser fact.

    The guard is defensive rather than load-bearing in current Chrome: focusing a
    visibility:hidden control is already a no-op, so the helper's blur-fallback and
    the guard's rejection of the hidden fallback are indistinguishable from the
    outside. The test documents that fact instead of pretending to prove the guard —
    the guard itself is pinned by test_release_helper_only_focuses_an_explicit_visible_fallback,
    which fails when the check is deleted.

    Keeping the measurement here matters for the next reader: it stops anyone from
    'simplifying' the guard away on the assumption that the hidden case is load-bearing,
    and it records that a focus stranded on a hidden node can only come from the panel
    hiding under focus (the #7866 bug), never from focusing a hidden control."""
    playwright, browser = _chromium()
    page = browser.new_page()
    try:
        page.set_viewport_size({"width": 390, "height": 780})
        page.set_content(_dismiss_page_html())
        page.evaluate("window.__openSidebar()")
        page.evaluate(
            "document.getElementById('btnHamburger').style.visibility = 'hidden'"
        )
        assert page.evaluate(
            "getComputedStyle(document.getElementById('btnHamburger')).visibility"
        ) == "hidden", "the probe must actually have hidden the fallback"
        # Chrome refuses focus on an invisible control outright.
        page.evaluate("document.getElementById('btnHamburger').focus()")
        refused = not page.evaluate(
            "() => document.activeElement === document.getElementById('btnHamburger')"
        )
    finally:
        browser.close()
        playwright.stop()

    assert refused, (
        "this browser DOES honour focus() on a visibility:hidden control — the guard in "
        "_isFocusableControl() is then load-bearing, and this test must be rewritten to "
        "assert the helper's behaviour instead of documenting a no-op"
    )


# ── Maintainer review, PR #7924: the workspace drawer's outside-tap dismiss ────
#
# closeMobileWorkspacePanelFromChat() only runs at <=900px, and that whole band
# hides #btnWorkspacePanelEdgeToggle with display:none!important. The position:fixed
# exemption in _isFocusableControl() let it through, focus() silently failed, and the
# keyboard user was dropped on <body> and then onto browser chrome at the next Tab.
#
# These probes drive the REAL close path against the REAL production stylesheet at
# the band where it broke. A static token assertion cannot see this: the failure is
# "the browser refused the focus() call", which only shows up as an activeElement.

# The band the bug lived in: 641-900px, where the edge toggle is display:none but
# _isCompactWorkspaceViewport() still routes the workspace panel through the drawer.
_COMPACT_WORKSPACE_BAND = {"width": 800, "height": 800}

# The panel markup the walk needs: the drawer's own Close X plus a Files tab to park
# focus on, exactly as in production.
_WORKSPACE_PANEL_HTML = """<aside class="rightpanel">
  <div class="panel-header"><div class="panel-actions">
    <button class="panel-icon-btn close-preview" id="btnClearPreview" aria-label="Close preview">X</button>
  </div></div>
  <div class="workspace-panel-tabs" role="tablist">
    <button class="workspace-panel-tab active" id="workspaceFilesTab" type="button">Files</button>
  </div>
</aside>"""

# A control OUTSIDE the drawer, inside #mainChat so the pointerdown reaches the real
# listener (boot.js binds it to #mainChat, and a synthetic event on a descendant
# bubbles to it exactly as a real outside-tap does).
_AFTER_PANEL_HTML = """<textarea id="msg"></textarea>"""


def _workspace_functions_source() -> str:
    """The production helpers the workspace-drawer walk needs, lifted verbatim.

    Sourcing them (rather than restating them) keeps the probe honest — it fails if
    boot.js changes its mind instead of testing a copy that drifted away from it.
    The close path is incomplete without the panel-state functions it delegates to,
    so closeWorkspacePanel -> _setWorkspacePanelMode -> syncWorkspacePanelUI are
    extracted too; otherwise the real handler throws ReferenceError and the probe
    would silently measure nothing.
    """
    source: list[str] = []
    for name in (
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
        "function openWorkspacePanel(",
        "function _setWorkspacePanelDismissed(",
        "function _markWorkspacePanelClosedByUser(",
        "function closeWorkspacePanel(",
        "function closeMobileWorkspacePanelFromChat(",
    ):
        start = BOOT_JS.find(name)
        assert start != -1, f"{name} is missing from boot.js"
        body = BOOT_JS[start : BOOT_JS.find("\n}", start) + 2]
        # Brace-matching on the raw text stops at the first "\n}", which for an
        # object-literal body is the inner "};" line, not the function's own end.
        # Verify the extraction is balanced so a truncated function (which would
        # throw in the page and silently no-op the probe) cannot pass.
        assert body.count("{") == body.count("}"), (
            f"{name} extraction is unbalanced — the probe page would throw"
        )
        source.append(body)
    return "\n".join(source)


def _workspace_drawer_page_html() -> str:
    return f"""<!doctype html><html><head><style>{STYLE_CSS}</style></head><body>
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
<script>let _workspacePanelMode = 'closed'; let _workspacePanelUserDismissed=false; let _workspacePanelDismissGen=0;</script>
<script>{_workspace_functions_source()}</script>
<script>
  window.__openDrawer = () => {{
    document.querySelector('.rightpanel').classList.add('mobile-open');
    _workspacePanelMode = 'browse';
  }};
  window.__activeId = () => {{
    const el = document.activeElement;
    return el ? (el.id || el.tagName) : null;
  }};
  // The production wiring: boot.js binds closeMobileWorkspacePanelFromChat to
  // pointerdown on #mainChat. Reproduce that binding here (the surrounding IIFE
  // is not extractable as a standalone function) so the real handler body runs.
  document.getElementById('mainChat')
    .addEventListener('pointerdown', closeMobileWorkspacePanelFromChat);
  // The real close leg of _setWorkspacePanelMode(): drop the class, then release
  // focus the way the production function does.
  window.__closeDrawer = (returnTo) => {{
    const panel = document.querySelector('.rightpanel');
    panel.classList.remove('mobile-open');
    _workspacePanelMode = 'closed';
    _releaseFocusFromClosedPanel(panel, returnTo);
  }};
  window.__edgeToggleDisplay = () =>
    getComputedStyle(document.getElementById('btnWorkspacePanelEdgeToggle')).display;
  window.__composerToggleRects = () =>
    document.getElementById('btnWorkspacePanelToggle').getClientRects().length;
</script>
</body></html>"""


def _run_outside_tap_focus_landing(width: int) -> str:
    """Drive the outside-tap dismiss and report where document.activeElement lands.

    Runs the real closeMobileWorkspacePanelFromChat() body against the real
    stylesheet, so the pointerdown-guard branch, the band predicate and the fallback
    choice all execute exactly as in production.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # pragma: no cover - dependency missing path
        pytest.skip("playwright is unavailable; run the sidebar a11y browser test")

    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    page = browser.new_page()
    try:
        page.set_viewport_size({"width": width, "height": 800})
        page.set_content(_workspace_drawer_page_html())
        page.evaluate("window.__openDrawer()")
        # Focus the Files tab inside the open drawer: the state the maintainer's
        # trace found when the dismiss misfired.
        page.evaluate("document.getElementById('workspaceFilesTab').focus()")
        # Fire the real handler. closeMobileWorkspacePanelFromChat bails on taps
        # inside the panel or on a workspace toggle control, so target the chat
        # textarea — the genuine outside-tap. A real mousedown blurs to <body>
        # first, which is why real taps masked the bug; dispatch the event
        # without that blur so the keyboard/AT dismiss path is what is measured.
        page.evaluate(
            """() => {
                 document.getElementById('msg').dispatchEvent(
                   new PointerEvent('pointerdown', {bubbles: true}));
               }"""
        )
        page.wait_for_timeout(350)  # the 250ms slide-out + visibility flip
        return page.evaluate("window.__activeId()")
    finally:
        browser.close()
        playwright.stop()


def test_outside_tap_dismiss_lands_focus_on_a_visible_invoker():
    """The maintainer's #7924 should-fix, measured in the band that broke.

    In the 641-900px band the drawer's outside-tap dismiss must hand focus to a
    control the user can actually see and tab from. Before the fix it passed the
    edge toggle, which is display:none across that whole band: focus() silently
    failed, focus stayed on the hidden Files tab, and the next Tab escaped to
    browser chrome. Real taps masked it (mousedown blurs to <body> first), so only
    a programmatic/keyboard dismiss — an assistive-tech dismiss, or Enter on an
    element — caught it."""
    landed = _run_outside_tap_focus_landing(_COMPACT_WORKSPACE_BAND["width"])

    assert landed == "btnWorkspacePanelToggle", (
        "tapping outside the drawer at 800px must return focus to the composer's "
        f"workspace toggle, the one visible invoker in that band (got {landed!r}). "
        "Anything else — <body>, the hidden Files tab, browser chrome — means the "
        "dismiss handed focus to a control the user cannot see or tab from."
    )


def test_edge_toggle_is_display_none_in_the_band_that_choose_the_fallback():
    """Pin the premise, so the two probes above cannot silently go vacuous.

    The whole reason the compact band needs a different fallback is that the edge
    toggle is display:none there while the composer toggle is laid out. If a stylesheet
    change moves either of those, the fallback choice has to be revisited.
    """
    playwright, browser = _chromium()
    page = browser.new_page()
    try:
        page.set_viewport_size(_COMPACT_WORKSPACE_BAND)
        page.set_content(_workspace_drawer_page_html())
        page.evaluate("window.__openDrawer()")
        edge_display = page.evaluate("window.__edgeToggleDisplay()")
        composer_rects = page.evaluate("window.__composerToggleRects()")
    finally:
        browser.close()
        playwright.stop()

    assert edge_display == "none", (
        f"the edge toggle must be display:none at {_COMPACT_WORKSPACE_BAND['width']}px "
        f"for the compact-band fallback to be necessary; got display:{edge_display!r}"
    )
    assert composer_rects > 0, (
        "the composer workspace toggle must be laid out at "
        f"{_COMPACT_WORKSPACE_BAND['width']}px, or the replacement fallback is itself "
        "unreachable"
    )


def test_a_fallback_that_refuses_focus_still_releases_focus():
    """The safety net under the guard, measured rather than asserted.

    _isFocusableControl() rejects a reachable-looking control that is display:none, so
    the fallback branch is normally never entered for one. But the guard is a
    prediction, not a guarantee — a control can hide between the check and the
    focus() call — so the helper must confirm the focus actually landed and fall
    through to the blur path when it did not. Staying on the dead node inside the
    now-hidden panel is precisely the bug this series exists to remove.

    A hidden-but-laid-out control (visibility:hidden) is the browser case that makes
    focus() a no-op while still passing the offsetParent/position checks, so it is
    what proves the fall-through.
    """
    playwright, browser = _chromium()
    page = browser.new_page()
    try:
        page.set_viewport_size({"width": 390, "height": 780})
        page.set_content(_workspace_drawer_page_html())
        page.evaluate("window.__openDrawer()")
        # Park focus inside the drawer, then hand the helper a fallback that will
        # refuse focus().
        page.evaluate("document.getElementById('workspaceFilesTab').focus()")
        page.evaluate(
            """() => {
                 const panel = document.querySelector('.rightpanel');
                 const doomed = document.getElementById('btnWorkspacePanelToggle');
                 // visibility:hidden keeps the element laid out (offsetParent is
                 // non-null) but makes focus() a no-op in Chromium.
                 doomed.style.visibility = 'hidden';
                 _releaseFocusFromClosedPanel(panel, doomed);
               }"""
        )
        landed = page.evaluate("window.__activeId()")
    finally:
        browser.close()
        playwright.stop()

    assert landed != "workspaceFilesTab", (
        "a fallback that refuses focus() must not leave focus stranded on the dead "
        f"node inside the hidden panel; focus stayed on {landed!r}"
    )


def _close_x_drawer_page_html() -> str:
    """The drawer's own Close X, wired to the REAL handleWorkspaceClose().

    Needs the panel-state functions that path delegates to (closeWorkspacePanel ->
    _setWorkspacePanelMode -> syncWorkspacePanelUI), plus a session so
    syncWorkspacePanelUI() leaves the composer toggle enabled — with no session it
    disables it (canBrowse is false) and the fallback is correctly refused, which
    is a different scenario from the one under test.
    """
    functions = "\n".join(
        BOOT_JS[start : BOOT_JS.find("\n}", start) + 2]
        for name in (
            "function _workspacePanelEls(",
            "function _hasWorkspacePreviewVisible(",
            "function _setPanelInert(",
            "function _isFocusableControl(",
            "function _releaseFocusFromClosedPanel(",
            "function _setButtonTooltip(",
            "function _uiText(",
            "function _setWorkspacePanelMode(",
            "function syncWorkspacePanelUI(",
            "function _workspacePanelInvokerForBand(",
            "function openWorkspacePanel(",
            "function _setWorkspacePanelDismissed(",
            "function _markWorkspacePanelClosedByUser(",
            "function closeWorkspacePanel(",
            "function handleWorkspaceClose(",
            "function clearPreview(",
        )
        for start in [BOOT_JS.find(name)]
        if start != -1
    )
    return (
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
    <textarea id="msg"></textarea>
  </main>
</div>
<button class="workspace-panel-edge-toggle" id="btnWorkspacePanelEdgeToggle"
  type="button" aria-label="Show workspace panel">E</button>
<aside class="rightpanel">
  <div class="panel-header"><div class="panel-actions">
    <button class="panel-icon-btn close-preview" id="btnClearPreview" aria-label="Close preview">X</button>
  </div></div>
  <div class="workspace-panel-tabs" role="tablist">
    <button class="workspace-panel-tab active" id="workspaceFilesTab" type="button">Files</button>
  </div>
</aside>
<script>$ = (id) => document.getElementById(id);</script>
<script>__SESSION__</script>
<script>
let _workspacePanelMode = 'closed'; let _workspacePanelUserDismissed=false; let _workspacePanelDismissGen=0;
let _previewCurrentPath='', _previewCurrentMode='', _previewDirty=false;
function t(k,f){{ return f; }}
function renderBreadcrumb(){{}}
</script>
<script>
function _isCompactWorkspaceViewport(){{ return window.matchMedia('(max-width: 900px)').matches; }}
</script>
<script>{functions}</script>
<script>
  window.__openDrawer = () => {{
    document.querySelector('.rightpanel').classList.add('mobile-open');
    _workspacePanelMode = 'browse';
    syncWorkspacePanelUI();
  }};
  window.__activeId = () => {{
    const el = document.activeElement;
    return el ? (el.id || el.tagName) : null;
  }};
  $('btnClearPreview').onclick = handleWorkspaceClose;
</script>
</body></html>"""
        # The session object has braces that f-string syntax would eat; substitute
        # it after the fact. A session is what makes the composer toggle enabled
        # (canBrowse), which is the scenario under test.
        .replace("__SESSION__", "const S = {session:{session_id:1}, _profileDefaultWorkspace:null};")
    )


def test_the_drawers_own_close_x_returns_focus_to_the_invoker():
    """The senior review's carried-over should-fix, measured.

    The workspace drawer's own Close used to leave focus on <body>: the panel sync
    disables that very button (syncWorkspacePanelUI sets clearBtn.disabled=!isOpen),
    and the X is what handleWorkspaceClose() is wired to, so by the time any rescue
    could run the focused node had already been dropped. The next Tab then escaped
    the document. handleWorkspaceClose() now captures the band-appropriate invoker
    before closing, exactly as the outside-tap dismiss does.
    """
    playwright, browser = _chromium()
    page = browser.new_page()
    try:
        page.set_viewport_size({"width": 390, "height": 780})
        page.set_content(_close_x_drawer_page_html())
        page.evaluate("window.__openDrawer()")
        page.wait_for_timeout(400)  # the 250ms slide-in transition
        # Focus the drawer's own X — what a keyboard user does before dismissing.
        page.evaluate("document.getElementById('btnClearPreview').focus()")
        assert page.evaluate("window.__activeId()") == "btnClearPreview", (
            "the probe must start with the drawer's Close focused"
        )
        page.evaluate("document.getElementById('btnClearPreview').click()")
        after_close = page.evaluate("window.__activeId()")
        page.wait_for_timeout(350)  # the 250ms slide-out transition
        after_settle = page.evaluate("window.__activeId()")
        page.keyboard.press("Tab")
        after_tab = page.evaluate("window.__activeId()")
    finally:
        browser.close()
        playwright.stop()

    assert after_close == "btnWorkspacePanelToggle", (
        "the drawer's own Close is an explicit dismiss and must hand focus to the "
        f"invoker, not strand it on <body> (focus went to {after_close!r})"
    )
    assert after_settle == "btnWorkspacePanelToggle", (
        "focus must stay on the invoker once the close animation settles, "
        f"not fall back to <body> (focus went to {after_settle!r})"
    )
    # Walking on from the invoker must not re-enter the now-hidden drawer: the
    # composer is what follows the drawer in the tab order.
    assert after_tab == "msg", (
        "the next Tab must continue from the invoker into the composer, not walk "
        f"back into the closed drawer (got {after_tab!r})"
    )
