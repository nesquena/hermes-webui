"""
Mobile layout regression tests — run on every QA pass.

These tests check that the CSS and HTML structure required for correct
mobile rendering (375px–640px viewport widths) is intact after every change.
They are static checks (no server needed) that catch common regressions:

  - Mobile breakpoints present for key layout elements
  - Right panel slide-over markup and CSS intact
  - Profile dropdown not clipped by overflow on mobile
  - Composer footer chips scroll correctly on narrow viewports
  - Mobile sidebar navigation stays available on phones
  - No full-viewport overflow that would break scroll

Run as part of the standard test suite:
    pytest tests/test_mobile_layout.py -v
"""

import pathlib
import re
from html.parser import HTMLParser

REPO = pathlib.Path(__file__).parent.parent
HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def _max_width_media_blocks(width_px):
    """Return all @media(max-width:Npx) bodies using balanced braces."""
    pattern = re.compile(rf"@media\s*\(\s*max-width\s*:\s*{width_px}px\s*\)\s*\{{")
    blocks = []
    for match in pattern.finditer(CSS):
        open_brace = match.end() - 1
        depth = 0
        for idx in range(open_brace, len(CSS)):
            if CSS[idx] == "{":
                depth += 1
            elif CSS[idx] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(CSS[open_brace + 1 : idx])
                    break
    return blocks


def _composer_phone_media_block():
    for block in _max_width_media_blocks(640):
        if ".composer-footer" in block:
            return block
    raise AssertionError("Missing composer rules in @media(max-width:640px)")


def _strip_css_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _rule_body(css, selector):
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", _strip_css_comments(css)):
        selectors = {part.strip() for part in match.group(1).split(",")}
        if selector in selectors:
            return match.group(2)
    raise AssertionError(f"Missing CSS rule for {selector}")


def _declarations(rule_body):
    declarations = {}
    for item in rule_body.split(";"):
        if ":" not in item:
            continue
        prop, value = item.split(":", 1)
        declarations[prop.strip()] = re.sub(r"\s+", " ", value.strip())
    return declarations


def _optional_declarations(css, selector):
    try:
        return _declarations(_rule_body(css, selector))
    except AssertionError:
        return {}


def _display_hidden(declarations):
    return declarations.get("display", "").replace(" ", "") in {
        "none",
        "none!important",
    }


def _display_inline_flex(declarations):
    return declarations.get("display", "").replace(" ", "") in {
        "inline-flex",
        "inline-flex!important",
    }


class _ComposerLeftDropdownParser(HTMLParser):
    _VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self):
        super().__init__()
        self.stack = []
        self.violations = []

    def handle_starttag(self, tag, attrs):
        self._handle_element(tag, attrs, push=True)

    def handle_startendtag(self, tag, attrs):
        self._handle_element(tag, attrs, push=False)

    def handle_endtag(self, tag):
        tag = tag.lower()
        for idx in range(len(self.stack) - 1, -1, -1):
            if self.stack[idx]["tag"] == tag:
                del self.stack[idx:]
                break

    def _handle_element(self, tag, attrs, push):
        tag = tag.lower()
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        element_id = attrs.get("id") or ""
        inside_composer_left = any(
            "composer-left" in item["classes"] for item in self.stack
        )
        is_dropdown = element_id.endswith("Dropdown") or any(
            "dropdown" in class_name for class_name in classes
        )
        if inside_composer_left and is_dropdown:
            label = f"#{element_id}" if element_id else "." + ".".join(sorted(classes))
            self.violations.append(label)
        if push and tag not in self._VOID_TAGS:
            self.stack.append({"tag": tag, "classes": classes})


# ── Mobile breakpoint rules ───────────────────────────────────────────────────


def test_mobile_breakpoint_900px_present():
    """@media(max-width:900px) must hide the right panel and show mobile-files-btn."""
    assert "@media(max-width:900px)" in CSS or "@media (max-width: 900px)" in CSS, (
        "Missing @media(max-width:900px) breakpoint in style.css"
    )
    # Right panel should be hidden at 900px, replaced by slide-over
    assert (
        ".rightpanel{display:none" in CSS
        or ".rightpanel {display:none" in CSS
        or re.search(r"max-width:900px\).*?\.rightpanel\{display:none", CSS, re.DOTALL)
    ), ".rightpanel must be display:none at max-width:900px (slide-over replaces it)"


def test_mobile_breakpoint_640px_present():
    """@media(max-width:640px) must exist for narrow phone layouts."""
    assert "@media(max-width:640px)" in CSS or "@media (max-width: 640px)" in CSS, (
        "Missing @media(max-width:640px) breakpoint in style.css"
    )


def test_rightpanel_mobile_slide_over_css():
    """Right panel must have position:fixed slide-over CSS for mobile."""
    # At max-width:900px the rightpanel should be position:fixed, off-screen right
    assert "position:fixed" in CSS, (
        "style.css must have position:fixed for rightpanel mobile slide-over"
    )
    assert (
        ".rightpanel.mobile-open{right:0" in CSS
        or ".rightpanel.mobile-open {right:0" in CSS
    ), ".rightpanel.mobile-open must set right:0 to slide panel in from right"
    assert "min(300px, 100vw)" in CSS or "min(300px,100vw)" in CSS, (
        "rightpanel mobile width should be capped defensively with 100vw"
    )
    assert "var(--mobile-rightpanel-width)" in CSS, (
        "mobile rightpanel width variable should be used in compact mode rules"
    )
    assert "calc(-1 * var(--mobile-rightpanel-width))" in CSS, (
        "closed mobile rightpanel should be off-canvas using a width-based negative offset"
    )
    mobile_640 = re.search(
        r"@media\(max-width:640px\)\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", CSS, re.DOTALL
    )
    assert mobile_640, "@media(max-width:640px) block missing from style.css"
    rightpanel_block = mobile_640.group(1)
    assert re.search(
        r"\.rightpanel\{[^}]*width:\s*var\(--mobile-rightpanel-width\)\s*!important",
        rightpanel_block,
        re.DOTALL,
    ), (
        ".rightpanel width must use var(--mobile-rightpanel-width) with !important in mobile block"
    )
    assert re.search(
        r"\.rightpanel\.mobile-open\{[^}]*right:\s*0\s*!important",
        rightpanel_block,
        re.DOTALL,
    ), "mobile-open mobile rightpanel must force right:0 with !important"
    assert re.search(
        r"\.rightpanel\{[^}]*box-shadow:\s*none\s*!important",
        rightpanel_block,
        re.DOTALL,
    ), "closed mobile rightpanel should have no shadow to avoid right-edge bleed"
    assert re.search(
        r"\.rightpanel\.mobile-open\{[^}]*box-shadow:\s*-4px 0 24px rgba\(0,\s*0,\s*0,\s*\.?4\)",
        rightpanel_block,
        re.DOTALL,
    ), "open mobile rightpanel should keep the edge shadow"


def test_workspace_panel_inline_width_is_desktop_only():
    """Persisted rightpanel width must only be restored above compact/mobile breakpoints."""
    boot_js = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    assert "function _syncWorkspacePanelInlineWidth()" in boot_js, (
        "_syncWorkspacePanelInlineWidth() must exist to keep panel width mobile-safe"
    )
    assert "_syncWorkspacePanelInlineWidth();" in boot_js, (
        "_syncWorkspacePanelInlineWidth() must be called when viewport changes"
    )
    assert "localStorage.getItem('hermes-panel-w')" in boot_js, (
        "Panel width helper must source hermes-panel-w from localStorage"
    )
    assert (
        "_workspacePanelEls();" in boot_js
        and "style.removeProperty('width')" in boot_js
    ), "Panel helper must clear inline width while in compact/mobile viewport"


def _container_query_block(css: str, container_query: str):
    query_pattern = re.compile(
        rf"@container\s+{re.escape(container_query)}\s*\{{",
        re.DOTALL,
    )
    for match in query_pattern.finditer(css):
        start = match.end() - 1
        end = css.find("@container", start + 1)
        if end == -1:
            end = css.find("@media", start + 1)
        if end == -1:
            end = len(css)
        block = css[start + 1 : end]
        return block
    return ""


def _container_media_block(css: str, media_query: str):
    query_pattern = re.compile(
        rf"@media\s*\(\s*max-width:\s*{re.escape(media_query)}\s*\)\s*\{{",
        re.DOTALL,
    )

    def _media_block_end(css_text: str, open_brace_idx: int) -> int:
        depth = 0
        for idx in range(open_brace_idx, len(css_text)):
            if css_text[idx] == "{":
                depth += 1
            elif css_text[idx] == "}":
                depth -= 1
                if depth == 0:
                    return idx
        return -1

    def _strip_nested_media(block: str) -> str:
        parts = []
        cursor = 0
        while True:
            nested = block.find("@media", cursor)
            if nested == -1:
                parts.append(block[cursor:])
                break
            parts.append(block[cursor:nested])
            nested_open = block.find("{", nested)
            if nested_open == -1:
                break
            nested_close = _media_block_end(block, nested_open)
            if nested_close == -1:
                break
            cursor = nested_close + 1
        return "".join(parts)

    for match in query_pattern.finditer(css):
        start = match.end() - 1
        end = _media_block_end(css, start)
        if end == -1:
            continue
        block = css[start + 1 : end]
        block = _strip_nested_media(block)
        if ".composer-profile-label" in block or ".composer-profile-chip" in block:
            return block
    return ""


def test_composer_controls_switch_to_icon_only_by_container_width():
    """Composer controls should progressively compact based on footer width."""
    assert re.search(
        r"\.composer-footer\s*\{[^}]*container-type:inline-size[^}]*container-name:composer-footer[^}]*\}",
        CSS,
    ), (
        ".composer-footer should define container-type:inline-size and container-name:composer-footer"
    )
    compact_700 = _container_query_block(CSS, "composer-footer (max-width: 700px)")
    assert compact_700, (
        "Expected composer mid-width compact rules at @container composer-footer (max-width: 700px)"
    )
    for selector in (
        ".composer-workspace-label",
        ".composer-model-label",
        ".composer-model-chevron",
        "#composerWorkspaceLabel",
        "#composerModelLabel",
        ".composer-workspace-chip",
        ".composer-model-chip",
        ".composer-divider",
    ):
        assert selector in compact_700, (
            f"{selector} should be present in the 700px composer compact block"
        )
    assert "display:none" in compact_700
    assert "max-width:52px" in compact_700
    # Ensure this first stage does not prematurely remove profile/reasoning labels.
    assert ".composer-profile-label" not in compact_700
    assert ".composer-reasoning-label" not in compact_700
    assert ".composer-profile-chevron" not in compact_700
    assert ".composer-reasoning-chevron" not in compact_700

    compact_520 = _container_query_block(CSS, "composer-footer (max-width: 520px)")
    assert compact_520, (
        "Expected full composer icon-only rules at @container composer-footer (max-width: 520px)"
    )
    for selector in (
        ".composer-profile-label",
        ".composer-workspace-label",
        ".composer-model-label",
        ".composer-reasoning-label",
        ".composer-profile-chevron",
        ".composer-workspace-chevron",
        ".composer-model-chevron",
        ".composer-reasoning-chevron",
        "#composerProfileLabel",
        "#composerWorkspaceLabel",
        "#composerModelLabel",
        "#composerReasoningLabel",
        ".composer-model-chip",
        ".composer-profile-chip",
        ".composer-reasoning-chip",
    ):
        assert selector in compact_520, (
            f"{selector} should be present in the 520px composer compact block"
        )
    assert "width:44px" in compact_520
    assert "display:none" in compact_520
    assert ".composer-workspace-chip{display:none!important" in compact_520.replace(
        " ", ""
    ), "520px container compact mode must remove the blank workspace switch slot"
    assert ".composer-left>*{flex-shrink:0" in compact_520.replace(" ", ""), (
        "520px container compact mode must stop controls from shrinking into each other"
    )
    assert (
        ".composer-mobile-config-btn" in compact_520
        and "display:inline-flex!important" in compact_520
    ), (
        "520px container compact mode must expose the mobile config button even when viewport is wider than 640px"
    )

    # Regression intent:
    # - this container rule should not depend on right-panel open/closed state.
    # - left-sidebar-only constriction must still collapse composer controls together.
    assert ".layout:not(.workspace-panel-collapsed)" not in compact_700, (
        "composer-footer compact rule should be state-agnostic (left sidebar + closed right panel case included)"
    )
    assert ".layout:not(.workspace-panel-collapsed)" not in compact_520, (
        "composer-footer compact rule should be state-agnostic (left sidebar + closed right panel case included)"
    )


def test_composer_700px_workspace_switch_does_not_become_blank_chip():
    """The 700px container state may hide the workspace label, but needs a switch affordance."""
    compact_700 = _container_query_block(CSS, "composer-footer (max-width: 700px)")
    assert compact_700, (
        "Expected composer mid-width compact rules at @container composer-footer (max-width: 700px)"
    )

    workspace_label = _declarations(
        _rule_body(compact_700, ".composer-workspace-label")
    )
    workspace_chip = _optional_declarations(compact_700, ".composer-workspace-chip")
    workspace_chevron = _optional_declarations(
        compact_700, ".composer-workspace-chevron"
    )
    mobile_config = _optional_declarations(compact_700, ".composer-mobile-config-btn")

    assert _display_hidden(workspace_label), (
        "700px container state should hide the long workspace label before tighter mobile rules"
    )
    if not _display_hidden(workspace_chip) and not _display_inline_flex(mobile_config):
        assert not _display_hidden(workspace_chevron), (
            "700px container state must not leave the visible workspace switch chip without a label or chevron"
        )


def test_composer_compact_switch_is_not_viewport_only():
    """Compact controls should be container-triggered, not bound to viewport width alone."""
    assert "composer-footer (max-width: 700px)" in CSS, (
        "Container-query breakpoint should track composer footer width"
    )
    assert "composer-footer (max-width: 520px)" in CSS, (
        "Container-query second-stage breakpoint should track composer footer width"
    )
    assert (
        re.search(r"@container\s+composer-footer\s*\(max-width:\s*860px\)", CSS) is None
    ), "Full icon-only should not be tied to a 860px threshold any more"
    assert (
        re.search(r"@container\s+composer-footer\s*\(max-width:\s*1000px\)", CSS)
        is None
    ), "Full icon-only/first-stage container gate should not be tied to 1000px"
    media_860 = _container_media_block(CSS, "860px")
    assert media_860 == "", (
        "Composer compact breakpoint should not be a dedicated 860px viewport media query"
    )
    media_900 = _container_media_block(CSS, "900px")
    assert media_900 == "", (
        "Composer compact breakpoint should use container queries, not viewport media at 900px"
    )


def test_mobile_overlay_present():
    """Mobile overlay element must exist for tap-to-close sidebar behavior."""
    assert 'id="mobileOverlay"' in HTML, (
        "#mobileOverlay element missing from index.html"
    )
    assert "mobile-overlay" in CSS, ".mobile-overlay CSS rule missing from style.css"


def test_sidebar_nav_present():
    """Sidebar top navigation tabs must be present."""
    assert 'class="sidebar-nav"' in HTML, ".sidebar-nav missing from index.html"
    assert ".sidebar-nav{" in CSS or ".sidebar-nav {" in CSS, (
        ".sidebar-nav CSS rule missing from style.css"
    )


def test_mobile_does_not_hide_sidebar_nav():
    """Phone breakpoint must keep the sidebar top navigation visible."""
    mobile_css = "\n".join(_max_width_media_blocks(640))
    assert mobile_css, "Missing @media(max-width:640px) block in style.css"
    assert ".sidebar-nav{display:none" not in mobile_css.replace(" ", ""), (
        ".sidebar-nav must stay visible on mobile"
    )


def test_mobile_files_button_present():
    """Mobile files toggle button (#btnWorkspacePanelToggle.workspace-toggle-btn) must be in HTML and CSS."""
    assert 'id="btnWorkspacePanelToggle"' in HTML, (
        "#btnWorkspacePanelToggle missing from index.html"
    )
    assert "workspace-toggle-btn" in CSS, (
        ".workspace-toggle-btn CSS missing from style.css"
    )


# ── Profile dropdown overflow ─────────────────────────────────────────────────


def test_profile_dropdown_not_clipped_by_overflow():
    """Profile dropdown must not be inside an overflow:hidden or overflow-x:auto ancestor
    without a higher z-index escape hatch.

    The topbar-chips container uses overflow-x:auto on mobile, which creates a
    stacking context that clips absolutely-positioned children. The profile dropdown
    must use position:fixed on mobile OR the topbar-chips must not clip it.
    """
    # The profile-chip wrapper must have position:relative so the dropdown can escape
    assert 'id="profileChipWrap"' in HTML, "#profileChipWrap missing from index.html"
    # Profile dropdown must have a z-index high enough to clear the topbar
    assert ".profile-dropdown{" in CSS or ".profile-dropdown {" in CSS, (
        ".profile-dropdown CSS rule missing"
    )
    # z-index must be at least 200 (topbar is z-index:10)
    m = re.search(r"\.profile-dropdown\{[^}]*z-index:(\d+)", CSS)
    if m:
        assert int(m.group(1)) >= 100, (
            f".profile-dropdown z-index {m.group(1)} is too low — must be >= 100 to clear topbar"
        )


def test_composer_dropdowns_are_not_nested_inside_left_control_row():
    """Composer dropdown surfaces should remain outside .composer-left.

    The left row can wrap/scroll on phones; dropdowns need to be siblings so
    that overflow rules on the control row cannot clip them.
    """
    parser = _ComposerLeftDropdownParser()
    parser.feed(HTML)
    assert not parser.violations, (
        "Composer dropdowns must not be nested inside .composer-left: "
        + ", ".join(parser.violations)
    )


def test_topbar_chips_mobile_overflow():
    """topbar-chips must use overflow-x:auto on mobile for chip scrolling.

    Chips (profile, workspace, model, files) must scroll horizontally on narrow
    viewports rather than wrapping onto a second line which would break the topbar layout.
    """
    # At narrow viewport, topbar-chips should scroll
    assert "overflow-x:auto" in CSS or "overflow-x: auto" in CSS, (
        "topbar-chips must have overflow-x:auto for mobile chip scrolling"
    )


# ── Workspace panel close ─────────────────────────────────────────────────────


def test_workspace_close_button_present():
    """Workspace panel must have a close/hide button accessible on mobile."""
    # Accept handleWorkspaceClose() (two-step close: file→browse→closed), or the
    # lower-level functions directly.  handleWorkspaceClose is preferred because
    # it dismisses a file preview first before closing the panel.
    has_close = (
        'onclick="handleWorkspaceClose()"' in HTML
        or 'onclick="closeWorkspacePanel()"' in HTML
        or 'onclick="toggleWorkspacePanel()"' in HTML
    )
    assert has_close, (
        "handleWorkspaceClose() or closeWorkspacePanel() must be wired to a button to close the workspace panel on mobile"
    )


def test_toggle_mobile_files_js_defined():
    """toggleMobileFiles() must be defined in boot.js."""
    boot_js = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    assert "function toggleMobileFiles()" in boot_js, (
        "toggleMobileFiles() missing from static/boot.js"
    )
    assert "mobile-open" in boot_js, (
        "toggleMobileFiles() must toggle mobile-open class on the right panel"
    )


def test_new_conversation_closes_mobile_sidebar():
    """New conversation must close the mobile drawer so the chat pane is visible immediately."""
    boot_js = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    # Handler is now multi-line — search for the full block rather than a single line.
    assert "$('btnNewChat').onclick" in boot_js, (
        "btnNewChat onclick handler missing from static/boot.js"
    )
    # Find the handler block and verify closeMobileSidebar appears in it.
    # The handler grew comments after #1432 (in-flight guard refactor), so use a
    # generous window to cover the full handler body.
    idx = boot_js.find("$('btnNewChat').onclick")
    handler_block = boot_js[idx : idx + 1500]
    assert "closeMobileSidebar" in handler_block, (
        "btnNewChat handler must closeMobileSidebar() after creating the new session"
    )

    shortcut_line = next(
        (
            ln
            for ln in boot_js.splitlines()
            if "e.key==='k'" in ln or "e.key === 'k'" in ln
        ),
        "",
    )
    assert shortcut_line, "Cmd/Ctrl+K new chat shortcut missing from static/boot.js"
    shortcut_block = "\n".join(
        boot_js.splitlines()[
            boot_js.splitlines().index(shortcut_line) : boot_js.splitlines().index(
                shortcut_line
            )
            + 24
        ]
    )
    assert "closeMobileSidebar" in shortcut_block, (
        "Cmd/Ctrl+K new chat shortcut must closeMobileSidebar() after creating the new session"
    )


def test_new_conversation_shortcut_works_while_busy():
    """Cmd/Ctrl+K should still create a new conversation while the current one is busy.

    The previous behavior gated the shortcut on !S.busy, which meant users had
    to wait for a long generation to finish before they could start something
    new — the exact moment they want to switch context.
    """
    boot_js = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    shortcut_line = next(
        (
            ln
            for ln in boot_js.splitlines()
            if "e.key==='k'" in ln or "e.key === 'k'" in ln
        ),
        "",
    )
    assert shortcut_line, "Cmd/Ctrl+K new chat shortcut missing from static/boot.js"
    # Inspect the next 10 lines after the keybinding match — the gating block
    # would live there if it had been kept.
    idx = boot_js.splitlines().index(shortcut_line)
    shortcut_block = "\n".join(boot_js.splitlines()[idx : idx + 10])
    # Strip the existing message-count guard (which is unrelated and stays) so
    # we only check for an S.busy gate on the newSession() call itself.
    assert "if(!S.busy)" not in shortcut_block, (
        "Cmd/Ctrl+K must not be blocked by the current session's busy state"
    )
    assert "if (!S.busy)" not in shortcut_block, (
        "Cmd/Ctrl+K must not be blocked by the current session's busy state"
    )


# ── Viewport and scroll safety ────────────────────────────────────────────────


def test_body_overflow_hidden():
    """body must have overflow:hidden to prevent double scrollbars on mobile."""
    assert "body{" in CSS or "body {" in CSS, "body rule missing from style.css"
    assert re.search(r"body\{[^}]*overflow:hidden", CSS), (
        "body must have overflow:hidden to prevent double scrollbars"
    )


def test_flex_parents_allow_message_scroller_to_shrink():
    """The top-level flex containers must opt into min-height:0 so .messages can scroll on mobile.

    Mobile Safari/Chrome can trap scroll when a flex child with overflow:auto sits inside
    parents whose min-height remains auto. Both .layout and .main need min-height:0.
    """
    assert re.search(r"\.layout\{[^}]*min-height:0", CSS), (
        ".layout must set min-height:0 so the chat column can shrink and scroll"
    )
    assert re.search(r"\.main\{[^}]*min-height:0", CSS), (
        ".main must set min-height:0 so .messages remains scrollable while busy"
    )


def test_messages_touch_scrolling_hints_present():
    """The messages scroller must advertise touch-friendly scrolling behavior.

    On mobile browsers, momentum scrolling and explicit pan-y/overscroll behavior help
    prevent the chat area from feeling locked while the app body itself stays overflow:hidden.
    """
    assert re.search(r"\.messages\{[^}]*-webkit-overflow-scrolling:\s*touch", CSS), (
        ".messages must enable -webkit-overflow-scrolling:touch for mobile momentum scroll"
    )
    assert re.search(r"\.messages\{[^}]*touch-action:\s*pan-y", CSS), (
        ".messages must set touch-action:pan-y so vertical swipe gestures scroll the transcript"
    )
    assert re.search(r"\.messages\{[^}]*overscroll-behavior-y:\s*contain", CSS), (
        ".messages must contain vertical overscroll so the transcript keeps the gesture"
    )


def test_100dvh_viewport_height():
    """Layout must use 100dvh (dynamic viewport height) for correct mobile sizing.

    On mobile Safari and Chrome, 100vh includes the browser chrome (address bar),
    causing content to be hidden. 100dvh accounts for the actual available height.
    """
    assert "100dvh" in CSS, (
        "style.css must use 100dvh for correct mobile viewport height (100vh hides content under address bar)"
    )


def test_titlebar_safe_area_top_not_double_counted_in_browser_viewport():
    """The base titlebar must not always add env(safe-area-inset-top).

    Normal mobile browsers and webview wrappers already lay out the page below
    their own chrome/status area. Applying the top env inset unconditionally can
    double-count that space and push the titlebar down.
    """
    m = re.search(r"\.app-titlebar\{(?P<body>[^}]*)\}", CSS)
    assert m, ".app-titlebar rule missing from style.css"
    rule = m.group("body")
    assert "padding-top:var(--app-titlebar-safe-top)" in rule, (
        ".app-titlebar must use the scoped safe-area variable for top padding"
    )
    assert "padding-top:env(safe-area-inset-top" not in rule, (
        ".app-titlebar must not apply env(safe-area-inset-top) directly in "
        "the base browser/webview layout"
    )


def test_titlebar_safe_area_top_preserved_for_standalone_modes():
    """Installed/fullscreen app modes should still protect notched devices."""
    assert "--app-titlebar-safe-top:0px" in CSS, (
        "titlebar top safe-area variable must default to 0px for browser/webview layouts"
    )
    pattern = re.compile(
        r"@media\s*\(display-mode:\s*standalone\)\s*,\s*"
        r"\(display-mode:\s*fullscreen\)\s*\{[^}]*"
        r"--app-titlebar-safe-top:\s*env\(safe-area-inset-top",
        re.DOTALL,
    )
    assert pattern.search(CSS), (
        "standalone/fullscreen display modes must opt back into "
        "env(safe-area-inset-top) for notched installed-app layouts"
    )


def test_composer_touch_target_size():
    """Send button and composer inputs must have minimum 44px touch targets on mobile.

    Apple HIG and Google Material guidelines both require 44px minimum touch targets.
    """
    # Check that mobile CSS doesn't make the send button smaller than 44×44
    # We check that there's at least a min-height definition for touch targets
    assert re.search(r"(min-height|height).*44px", CSS), (
        "style.css must define 44px minimum touch targets for mobile (send button, nav buttons)"
    )


def test_mobile_composer_footer_stays_single_row():
    """Phone composer controls should stay in one footer row."""
    mobile_css = _composer_phone_media_block()

    footer = _declarations(_rule_body(mobile_css, ".composer-footer"))
    assert footer.get("flex-wrap") == "nowrap", (
        "mobile composer footer must stay visually single-row"
    )

    left = _declarations(_rule_body(mobile_css, ".composer-left"))
    assert left.get("flex") != "1 1 100%", (
        "mobile composer-left controls must not take their own full-width row"
    )
    assert left.get("width") != "100%", (
        "mobile composer-left controls must not span a separate row"
    )
    assert left.get("flex-wrap") == "nowrap", (
        "mobile composer-left controls must remain in one row"
    )

    right = _declarations(_rule_body(mobile_css, ".composer-right"))
    assert right.get("flex") != "1 1 100%", (
        "mobile composer-right actions must not take their own full-width row"
    )
    assert right.get("width") != "100%", (
        "mobile composer-right actions must not span a separate row"
    )
    assert right.get("justify-content") == "flex-end", (
        "mobile composer-right actions must stay end-aligned"
    )


def test_mobile_composer_left_scrolls_horizontally_without_wrapping():
    """If many primary controls are visible, the single control row should scroll."""
    left = _declarations(_rule_body(_composer_phone_media_block(), ".composer-left"))
    assert left.get("overflow-x") == "auto", (
        "mobile composer-left must allow horizontal overflow in the single row"
    )
    assert left.get("overflow-y") == "hidden", (
        "mobile composer-left must not create a second vertical control row"
    )
    assert left.get("max-height") == "none", (
        "mobile composer-left must not preserve the old bounded two-row height"
    )


def test_mobile_composer_left_children_do_not_shrink_into_each_other():
    """Phone composer controls must scroll or compact, never shrink/overlap siblings."""
    mobile_css = _composer_phone_media_block()
    left = _declarations(_rule_body(mobile_css, ".composer-left"))
    assert left.get("gap") == "10px", (
        "mobile composer-left needs explicit spacing between 44px touch targets"
    )

    children = _declarations(_rule_body(mobile_css, ".composer-left > *"))
    assert children.get("flex-shrink") == "0", (
        "mobile composer-left children must not shrink and visually overlap"
    )

    for selector in (
        ".composer-profile-wrap",
        ".composer-ws-wrap",
    ):
        declarations = _declarations(_rule_body(mobile_css, selector))
        assert declarations.get("flex") == "0 0 auto", (
            f"{selector} must opt out of flex shrinking on phones"
        )

    workspace_group = _declarations(_rule_body(mobile_css, ".composer-workspace-group"))
    assert workspace_group.get("flex") == "0 0 44px", (
        ".composer-workspace-group must reserve exactly one 44px slot on phones"
    )


def test_legacy_320px_composer_tightens_spacing_without_shrinking_targets():
    """At 320px, keep 44px controls but use smaller gutters so config stays visible."""
    narrow_blocks = [
        block for block in _max_width_media_blocks(340) if ".composer-left" in block
    ]
    assert narrow_blocks, "Missing 320px/legacy-phone composer spacing override"
    narrow_css = narrow_blocks[0]

    footer = _declarations(_rule_body(narrow_css, ".composer-footer"))
    left = _declarations(_rule_body(narrow_css, ".composer-left"))
    wrap = _declarations(_rule_body(narrow_css, ".composer-wrap"))

    assert footer.get("gap") == "4px", (
        "320px footer should tighten only the gutter between left controls and send"
    )
    assert left.get("gap") == "2px", (
        "320px left controls need compact gutters to fit config before the fixed send button"
    )
    assert wrap.get("padding-left") == "8px!important", (
        "320px composer should reclaim a little side padding without shrinking touch targets"
    )
    assert ".send-btn{width:44px;height:44px;" in _composer_phone_media_block(), (
        "narrow spacing override must not shrink the 44px send button"
    )
    assert (
        ".composer-mobile-config-btn{box-sizing:border-box;position:relative;display:inline-flex!important;width:44px;height:44px"
        in _composer_phone_media_block()
    ), "narrow spacing override must not shrink the 44px mobile config button"


def test_mobile_composer_workspace_switch_does_not_leave_empty_icon_slot():
    """The phone footer should keep only the useful workspace files button inline."""
    mobile_css = _composer_phone_media_block()
    workspace_group = _declarations(_rule_body(mobile_css, ".composer-workspace-group"))
    workspace_files = _declarations(
        _rule_body(mobile_css, ".composer-workspace-files-btn")
    )
    workspace_chip = _declarations(_rule_body(mobile_css, ".composer-workspace-chip"))

    assert workspace_group.get("max-width") == "44px", (
        "workspace group should collapse to one 44px files button on phones"
    )
    assert workspace_group.get("width") == "44px", (
        "workspace group should have an exact border-box phone width"
    )
    assert workspace_group.get("box-sizing") == "border-box", (
        "workspace group must use border-box for its 44px phone slot"
    )
    assert workspace_group.get("border") == "none", (
        "workspace files shortcut should not keep the desktop pill/circle border on phones"
    )
    assert workspace_group.get("background") == "transparent", (
        "workspace files shortcut should visually match other transparent mobile icon buttons"
    )
    assert workspace_files.get("max-width") == "44px", (
        "workspace files button should be the only visible workspace footer target on phones"
    )
    assert workspace_files.get("width") == "44px", (
        "workspace files button should have an exact border-box phone width"
    )
    assert workspace_files.get("box-sizing") == "border-box", (
        "workspace files button must not grow beyond its 44px phone slot due to padding"
    )
    assert workspace_chip.get("display") == "none!important", (
        "workspace switch chip has no visible mobile label/icon and must not consume a blank slot"
    )


def test_mobile_composer_overflow_control_present():
    """Phone composer must expose a compact overflow/settings control."""
    assert 'id="composerMobileConfigBtn"' in HTML, (
        "#composerMobileConfigBtn missing from index.html"
    )
    assert 'id="composerMobileConfigPanel"' in HTML, (
        "#composerMobileConfigPanel missing from index.html"
    )
    assert 'aria-controls="composerMobileConfigPanel"' in HTML, (
        "mobile config button must be associated with its panel"
    )
    left_start = HTML.index('<div class="composer-left">')
    left_end = HTML.index('<div class="composer-right">', left_start)
    assert 'id="composerMobileConfigPanel"' not in HTML[left_start:left_end], (
        "mobile overflow panel must not be nested inside .composer-left where overflow can clip it"
    )
    assert "function toggleMobileComposerConfig()" in (
        REPO / "static" / "ui.js"
    ).read_text(encoding="utf-8"), (
        "toggleMobileComposerConfig() must be defined in static/ui.js"
    )

    mobile_css = _composer_phone_media_block()
    btn = _declarations(_rule_body(mobile_css, ".composer-mobile-config-btn"))
    panel = _declarations(_rule_body(CSS, ".composer-mobile-config-panel"))
    panel_open = _declarations(
        _rule_body(mobile_css, ".composer-mobile-config-panel.open")
    )
    assert btn.get("display") == "inline-flex!important", (
        "mobile overflow button must be visible at phone width"
    )
    assert panel.get("display") == "none", (
        "mobile overflow panel should be closed by default"
    )
    assert panel.get("position") == "absolute", (
        "mobile overflow panel should open above the composer footer"
    )
    assert panel.get("flex-wrap") == "wrap", (
        "mobile overflow panel must allow the context details row to span below primary actions"
    )
    assert panel_open.get("display") == "flex", (
        "mobile overflow panel must become visible when opened"
    )


def test_model_and_reasoning_controls_live_in_mobile_overflow_panel():
    """Model and reasoning controls must remain reachable through the phone overflow."""
    panel_start = HTML.index('id="composerMobileConfigPanel"')
    panel_end = HTML.index('<div class="profile-dropdown"', panel_start)
    panel_html = HTML[panel_start:panel_end]
    assert 'id="composerMobileModelAction"' in panel_html, (
        "mobile model action must be inside the overflow panel"
    )
    assert 'id="composerMobileReasoningAction"' in panel_html, (
        "mobile reasoning action must be inside the overflow panel"
    )
    assert 'onclick="toggleModelDropdown()"' in panel_html, (
        "mobile model action must reuse the existing model dropdown"
    )
    assert 'onclick="toggleReasoningDropdown()"' in panel_html, (
        "mobile reasoning action must reuse the existing reasoning dropdown"
    )
    assert 'id="composerMobileModelLabel"' in panel_html, (
        "mobile model action must expose the selected model label"
    )
    assert 'id="composerMobileReasoningLabel"' in panel_html, (
        "mobile reasoning action must expose the selected reasoning label"
    )
    ui_js = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    assert "composerMobileModelAction" in ui_js, (
        "model dropdown positioning/click handling must know the mobile model action"
    )
    assert "composerMobileReasoningAction" in ui_js, (
        "reasoning dropdown positioning/click handling must know the mobile reasoning action"
    )

    mobile_css = _composer_phone_media_block()
    assert ".composer-left > .composer-model-wrap" in mobile_css, (
        "phone width must hide the footer model chip behind overflow"
    )
    assert ".composer-left > .composer-reasoning-wrap" in mobile_css, (
        "phone width must hide the footer reasoning chip behind overflow"
    )
    assert ".composer-mobile-config-action" in mobile_css, (
        "mobile overflow panel must size the model/reasoning actions"
    )


def test_model_and_reasoning_dropdowns_use_mobile_panel_anchors():
    """Model/reasoning dropdowns must anchor to mobile actions while the overflow is open."""
    ui_js = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    model_start = ui_js.index("function _positionModelDropdown()")
    model_end = ui_js.index("function renderModelDropdown()", model_start)
    model_body = ui_js[model_start:model_end]
    for expected in (
        "composerMobileConfigPanel",
        "composerMobileModelAction",
        "classList.contains('open')",
    ):
        assert expected in model_body, (
            f"_positionModelDropdown must keep mobile-panel anchor logic ({expected})"
        )

    reasoning_start = ui_js.index("function _positionReasoningDropdown()")
    reasoning_end = ui_js.index("function closeReasoningDropdown()", reasoning_start)
    reasoning_body = ui_js[reasoning_start:reasoning_end]
    for expected in (
        "composerMobileConfigPanel",
        "composerMobileReasoningAction",
        "classList.contains('open')",
    ):
        assert expected in reasoning_body, (
            f"_positionReasoningDropdown must keep mobile-panel anchor logic ({expected})"
        )


def test_context_details_live_in_mobile_overflow_panel():
    """Context details should be reachable in overflow without adding a composer slot."""
    panel_start = HTML.index('id="composerMobileConfigPanel"')
    panel_end = HTML.index('<div class="profile-dropdown"', panel_start)
    panel_html = HTML[panel_start:panel_end]
    for element_id in (
        "composerMobileContextAction",
        "composerMobileContextUsage",
        "composerMobileContextTokens",
        "composerMobileContextThreshold",
        "composerMobileContextCost",
        "composerMobileCtxCompressBtn",
    ):
        assert f'id="{element_id}"' in panel_html, (
            f"#{element_id} must be inside the mobile overflow panel"
        )

    right_start = HTML.index(
        '<div class="composer-right">', HTML.index('<div class="composer-footer">')
    )
    right_end = HTML.index('<div class="composer-mobile-config-panel"', right_start)
    right_html = HTML[right_start:right_end]
    assert 'id="composerMobileContextAction"' not in right_html, (
        "mobile context details must not live in composer-right as another phone slot"
    )
    assert 'id="composerMobileCtxBadge"' not in right_html, (
        "mobile context badge must stay on the config button, not composer-right"
    )

    ui_js = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    sync_start = ui_js.index("function _syncMobileCtxDisplay(state)")
    sync_end = ui_js.index("// ── Touch support", sync_start)
    sync_body = ui_js[sync_start:sync_end]
    for expected in (
        "DEFAULT_CTX=128*1024",
        "hasExplicitCtx",
        "hasPromptTok",
        "rawPct",
        "overflowed",
        "composerMobileContextUsage",
        "composerMobileContextTokens",
        "composerMobileCtxCompressBtn",
    ):
        assert expected in sync_body, (
            f"_syncCtxIndicator must preserve upstream context logic while updating mobile context UI ({expected})"
        )

    mobile_css = _composer_phone_media_block()
    ctx_wrap = _declarations(_rule_body(mobile_css, ".ctx-indicator-wrap"))
    assert ctx_wrap.get("display") == "none!important", (
        "standalone context indicator must remain hidden from the phone composer row"
    )

    context_row = _declarations(_rule_body(CSS, ".composer-mobile-context-action"))
    assert context_row.get("flex") == "1 0 100%", (
        "mobile context details should span the overflow panel instead of crowding the action row"
    )
    context_button = _declarations(_rule_body(CSS, ".composer-mobile-context-compress"))
    assert context_button.get("width") == "auto", (
        "mobile compress affordance should be compact inside the context row"
    )


def test_workspace_control_lives_in_mobile_overflow_panel():
    """Workspace switching must stay reachable even when the inline switch chip is hidden."""
    panel_start = HTML.index('id="composerMobileConfigPanel"')
    panel_end = HTML.index('<div class="profile-dropdown"', panel_start)
    panel_html = HTML[panel_start:panel_end]
    assert 'id="composerMobileWorkspaceAction"' in panel_html, (
        "mobile workspace action must be inside the overflow panel"
    )
    assert 'onclick="toggleComposerWsDropdown()"' in panel_html, (
        "mobile workspace action must reuse the existing workspace dropdown"
    )
    assert 'id="composerMobileWorkspaceLabel"' in panel_html, (
        "mobile workspace action must expose the current workspace label"
    )

    mobile_css = _composer_phone_media_block()
    workspace_chip = _declarations(_rule_body(mobile_css, ".composer-workspace-chip"))
    assert workspace_chip.get("display") == "none!important", (
        "inline workspace switch chip must remain hidden on phones"
    )

    panels_js = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    pos_start = panels_js.index("function _positionComposerWsDropdown()")
    pos_end = panels_js.index("function _positionProfileDropdown()", pos_start)
    position_body = panels_js[pos_start:pos_end]
    assert "composerMobileWorkspaceAction" in position_body, (
        "workspace dropdown positioning must know the mobile workspace action"
    )
    assert "composerMobileConfigPanel" in position_body, (
        "workspace dropdown positioning must anchor to the mobile panel action while open"
    )
    assert "anchor to #composerMobileWorkspaceAction" in position_body, (
        "workspace dropdown positioning should document the mobile-panel anchor choice"
    )

    toggle_start = panels_js.index("function toggleComposerWsDropdown()")
    toggle_end = panels_js.index("function closeWsDropdown()", toggle_start)
    toggle_body = panels_js[toggle_start:toggle_end]
    assert "usingMobileAction" in toggle_body and "chip.disabled" in toggle_body, (
        "mobile workspace action must bypass only the hidden/disabled desktop chip guard"
    )
    assert "!e.target.closest('#composerMobileWorkspaceAction')" in panels_js, (
        "workspace dropdown click-away handling must include the mobile workspace action"
    )

    ui_js = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    assert "e.target.closest('#composerWsDropdown')" in ui_js, (
        "mobile overflow click-away handling must allow interaction with the workspace dropdown"
    )


def test_mobile_config_panel_escape_closes_panel_and_dropdowns():
    """Escape should close mobile overflow state without touching desktop-only dropdowns."""
    ui_js = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    keydown_start = ui_js.index(
        "document.addEventListener('keydown',function(e){",
        ui_js.index("function toggleMobileComposerConfig()"),
    )
    keydown_end = ui_js.index("\n});", keydown_start)
    keydown_body = ui_js[keydown_start:keydown_end]
    assert "e.key!=='Escape'" in keydown_body, (
        "mobile config Escape handler must only handle Escape"
    )
    assert "composerMobileConfigPanel" in keydown_body, (
        "mobile config Escape handler must look up the mobile config panel"
    )
    assert "classList.contains('open')" in keydown_body, (
        "mobile config Escape handler must be gated on the open mobile panel"
    )
    for expected in (
        "closeMobileComposerConfig()",
        "closeWsDropdown",
        "closeModelDropdown()",
        "closeReasoningDropdown()",
    ):
        assert expected in keydown_body, (
            f"mobile config Escape handler must close related state ({expected})"
        )


def test_reasoning_chip_updates_desktop_and_mobile_controls():
    """Reasoning chip sync should keep both footer and mobile overflow labels current."""
    ui_js = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    chip_start = ui_js.index("function _applyReasoningChip(eff)")
    chip_end = ui_js.index("function fetchReasoningChip()", chip_start)
    chip_body = ui_js[chip_start:chip_end]
    for expected in (
        "composerReasoningWrap",
        "composerMobileReasoningAction",
        "composerReasoningLabel",
        "composerMobileReasoningLabel",
        "label.textContent=text",
        "mobileLabel.textContent=text",
    ):
        assert expected in chip_body, (
            f"_applyReasoningChip must update desktop and mobile reasoning UI ({expected})"
        )


def test_mobile_config_kickers_have_i18n_fallbacks():
    """Mobile overflow kicker labels should be localizable without losing HTML fallback text."""
    panel_start = HTML.index('id="composerMobileConfigPanel"')
    panel_end = HTML.index('<div class="profile-dropdown"', panel_start)
    panel_html = HTML[panel_start:panel_end]
    i18n_js = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
    en_start = i18n_js.index("  en: {")
    en_end = i18n_js.index("\n  ru: {", en_start)
    english = i18n_js[en_start:en_end]
    for key, label in (
        ("composer_mobile_workspace", "Workspace"),
        ("composer_mobile_model", "Model"),
        ("composer_mobile_reasoning", "Reasoning"),
        ("composer_mobile_context", "Context"),
    ):
        assert f'data-i18n="{key}">{label}</span>' in panel_html, (
            f"mobile panel kicker {label} must keep data-i18n and fallback text"
        )
        assert f"{key}: '{label}'" in english, f"English locale must define {key}"


def test_mobile_composer_primary_controls_keep_touch_friendly_sizing():
    """Visible phone composer controls and overflow controls must keep 44px targets."""
    mobile_css = _composer_phone_media_block()
    for selector in (
        ".composer-mobile-config-btn",
        ".composer-profile-chip",
        ".composer-mobile-config-action",
    ):
        declarations = _declarations(_rule_body(mobile_css, selector))
        assert declarations.get("box-sizing") == "border-box", (
            f"{selector} must use border-box so padding/border cannot exceed 44px"
        )
        assert declarations.get("min-height") == "44px", (
            f"{selector} must keep a 44px minimum height on phones"
        )
        if selector != ".composer-mobile-config-action":
            assert declarations.get("min-width") == "44px", (
                f"{selector} must keep a 44px minimum width on phones"
            )

    send = _declarations(_rule_body(mobile_css, ".send-btn"))
    assert send.get("width") == "44px", ".send-btn must keep 44px width on phones"
    assert send.get("height") == "44px", ".send-btn must keep 44px height on phones"

    ctx_wrap = _declarations(_rule_body(mobile_css, ".ctx-indicator-wrap"))
    assert ctx_wrap.get("display") == "none!important", (
        "context indicator must not add a late-appearing composer-right slot on phones"
    )

    ctx_badge = _declarations(_rule_body(CSS, ".composer-mobile-ctx-badge"))
    assert ctx_badge.get("position") == "absolute", (
        "mobile context usage should be shown as a badge on the config button, not a separate slot"
    )
    assert ctx_badge.get("pointer-events") == "none", (
        "mobile context badge must not shrink or steal the config button touch target"
    )
    assert 'id="composerMobileCtxBadge"' in HTML, (
        "mobile context badge element must exist in the composer config button"
    )

    icon_btn = _declarations(_rule_body(mobile_css, ".icon-btn"))
    assert icon_btn.get("min-width") == "44px", (
        ".icon-btn controls such as attach/mic must keep 44px minimum width on phones"
    )
    assert icon_btn.get("min-height") == "44px", (
        ".icon-btn controls such as attach/mic must keep 44px minimum height on phones"
    )

    if ".composer-workspace-files-btn" in mobile_css:
        files_btn = _declarations(
            _rule_body(mobile_css, ".composer-workspace-files-btn")
        )
        workspace_group = _declarations(
            _rule_body(mobile_css, ".composer-workspace-group")
        )
        assert files_btn.get("min-width") == "44px", (
            ".composer-workspace-files-btn must keep a 44px minimum width on phones"
        )
        assert workspace_group.get("min-height") == "44px", (
            ".composer-workspace-group must preserve 44px touch height on phones"
        )


# ── Input zoom prevention ─────────────────────────────────────────────────────


def test_composer_textarea_font_size_mobile():
    """Composer textarea must have font-size >= 16px on mobile.

    iOS Safari zooms the viewport when an input with font-size < 16px is focused,
    which breaks the layout. The composer textarea must be >= 16px at mobile widths.
    """
    # Check for 16px font-size on the textarea in a mobile breakpoint
    assert re.search(r"font-size:16px", CSS), (
        "Composer textarea must have font-size:16px at mobile widths to prevent iOS zoom-on-focus"
    )


def test_touch_device_inputs_meet_zoom_threshold():
    """All input/textarea/select must clear iOS Safari's 16px zoom threshold
    on touch-primary devices, not just the composer textarea (#1167).

    This locks the global media-query floor so future per-element font-size
    tweaks (sidebar search 13px, settings selects 12px, dialog inputs 14px,
    onboarding fields 13px) cannot accidentally re-introduce auto-zoom.
    """
    # The hover:none + pointer:coarse pair is the canonical touch-primary
    # detection (won't match desktop with mouse, won't match touch laptops
    # that report hover:hover).
    pattern = re.compile(
        r"@media\s*\(hover:none\)\s*and\s*\(pointer:coarse\)\s*\{[^}]*"
        r"input\s*,\s*textarea\s*,\s*select\s*\{[^}]*"
        r"font-size:\s*max\(\s*16px",
        re.DOTALL,
    )
    assert pattern.search(CSS), (
        "style.css must contain a (hover:none) and (pointer:coarse) media "
        "query that bumps input/textarea/select to font-size:max(16px,…) "
        "so iOS Safari does not auto-zoom on focus (#1167)"
    )


# ── Sidebar tabs on mobile ───────────────────────────────────────────────────


def test_profiles_sidebar_tab_present():
    """Sidebar tab strip must include Profiles."""
    # Tolerate additional utility classes (e.g. `has-tooltip` from #1775).
    # We just need a nav-tab classed button targeting the profiles panel.
    import re

    pattern = r'class="[^"]*\bnav-tab\b[^"]*"[^>]*data-panel="profiles"'
    assert re.search(pattern, HTML), (
        'Sidebar nav must have a nav-tab button with data-panel="profiles"'
    )


def test_mobile_bottom_nav_removed():
    """The old fixed mobile bottom nav should not be present anymore."""
    assert "mobile-bottom-nav" not in HTML, (
        "mobile-bottom-nav markup should be removed from index.html"
    )
    assert "mobile-bottom-nav" not in CSS, (
        "mobile-bottom-nav CSS should be removed from style.css"
    )


# ── Mobile Enter key inserts newline (PR #315, fixes #269) ───────────────────


def test_mobile_enter_newline_condition_present():
    """boot.js keydown handler must detect touch-primary devices via pointer:coarse."""
    boot_js = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    assert "pointer:coarse" in boot_js, (
        "boot.js must use pointer:coarse media query for mobile Enter detection"
    )


def test_mobile_enter_newline_uses_match_media():
    """boot.js must call matchMedia for pointer detection, not a hardcoded flag."""
    boot_js = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    assert (
        "matchMedia('(pointer:coarse)')" in boot_js
        or 'matchMedia("(pointer:coarse)")' in boot_js
    ), "boot.js must use matchMedia('(pointer:coarse)') for mobile detection"


def test_mobile_enter_newline_only_overrides_enter_default():
    """Mobile newline override must only apply when _sendKey is the default 'enter'."""
    boot_js = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    # The _mobileDefault check must gate on _sendKey==='enter' so ctrl+enter users aren't affected
    assert "_sendKey===" in boot_js and "'enter'" in boot_js, (
        "Mobile newline fallback must check window._sendKey==='enter' to avoid overriding user preference"
    )


def test_mobile_enter_does_not_affect_desktop_logic():
    """The mobile Enter override must not alter the existing else branch for desktop users."""
    boot_js = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    # The else branch (desktop, sends on Enter without Shift) must still be present
    assert "if(!e.shiftKey){e.preventDefault();send();" in boot_js, (
        "Desktop Enter-to-send logic (else branch) must still be present in boot.js"
    )
