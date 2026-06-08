"""Tests for #1431 / PR #1433 — composer-footer toolsets chip is responsive.

The chip must:
  * Be hidden by default (CSS base rule).
  * Be shown only at wide composer-footer widths (>= 1100px container query).
  * Stay hidden on mobile (@media max-width:640px and @container 520px).
  * Have its visibility controlled by CSS, NOT by JS (single source of truth).
  * Continue to track state through _applyToolsetsChip() so /api/session/toolsets
    keeps working for scripted callers regardless of UI visibility.
"""
import re


def _src(name: str) -> str:
    with open(f"static/{name}") as f:
        return f.read()


class TestToolsetsChipResponsiveCSS:
    """Visibility is controlled by CSS — base hides, container query reveals."""

    def test_base_rule_defaults_chip_to_hidden(self):
        """The base .composer-toolsets-wrap rule must include display:none."""
        css = _src("style.css")
        # The base rule (outside any @container or @media block) must default-hide
        m = re.search(
            r'^\s*\.composer-toolsets-wrap\{[^}]*\}',
            css, re.MULTILINE,
        )
        assert m, "Base .composer-toolsets-wrap CSS rule must exist"
        rule = m.group(0)
        assert "display:none" in rule, (
            f"Base rule must default-hide the chip: got {rule!r}"
        )

    def test_wide_container_query_shows_chip(self):
        """An @container composer-footer (min-width: 1100px) rule must reveal the chip."""
        css = _src("style.css")
        # Find the min-width container query — accept either display:block or display:flex
        # (we use block to match sibling wraps but either is a valid reveal)
        m = re.search(
            r'@container\s+composer-footer\s*\(\s*min-width:\s*1100px\s*\)\s*\{[^}]*\.composer-toolsets-wrap\s*\{[^}]*display:\s*(block|flex)[^}]*\}',
            css, re.DOTALL,
        )
        assert m, (
            "Must have @container composer-footer (min-width: 1100px) rule "
            "that shows .composer-toolsets-wrap with display:block or display:flex"
        )

    def test_narrow_container_query_keeps_hiding(self):
        """The existing @container (max-width: 520px) rule must still hide the chip."""
        css = _src("style.css")
        # Look for the existing 520px rule that already hid composer-toolsets-wrap
        m = re.search(
            r'@container\s+composer-footer\s*\(\s*max-width:\s*520px\s*\).*?\.composer-toolsets-wrap\s*\{\s*display:\s*none\s*!important',
            css, re.DOTALL,
        )
        assert m, (
            "@container composer-footer (max-width: 520px) must continue to "
            "hide .composer-toolsets-wrap with !important"
        )

    def test_mobile_viewport_keeps_hiding(self):
        """The existing @media max-width:640px rule must still hide the chip on mobile."""
        css = _src("style.css")
        m = re.search(
            r'@media\s*\(\s*max-width:\s*640px\s*\).*?\.composer-toolsets-wrap\s*\{\s*display:\s*none\s*!important',
            css, re.DOTALL,
        )
        assert m, (
            "@media (max-width:640px) must continue to hide "
            ".composer-toolsets-wrap on mobile viewports"
        )


class TestToolsetsChipJSDoesNotForceHide:
    """JS must NOT set display:none directly — CSS owns visibility."""

    def test_applyToolsetsChip_does_not_set_display_none(self):
        """_applyToolsetsChip must not contain wrap.style.display = 'none'."""
        js = _src("ui.js")
        m = re.search(r'function _applyToolsetsChip\([^)]*\)\s*\{.*?\n\}', js, re.DOTALL)
        assert m, "_applyToolsetsChip function must exist"
        body = m.group(0)
        # The PR initially had wrap.style.display = 'none'; we replaced with CSS.
        assert "wrap.style.display = 'none'" not in body, (
            "_applyToolsetsChip must not hardcode display:none — visibility "
            "is controlled by responsive CSS (#1431)"
        )
        assert 'wrap.style.display = "none"' not in body, (
            "_applyToolsetsChip must not hardcode display:none — visibility "
            "is controlled by responsive CSS (#1431)"
        )

    def test_applyToolsetsChip_clears_inline_style(self):
        """_applyToolsetsChip must clear inline display so CSS rules can apply."""
        js = _src("ui.js")
        m = re.search(r'function _applyToolsetsChip\([^)]*\)\s*\{.*?\n\}', js, re.DOTALL)
        assert m, "_applyToolsetsChip function must exist"
        body = m.group(0)
        # Either ='' or ="" (clearing inline style)
        assert (
            "wrap.style.display = ''" in body
            or 'wrap.style.display = ""' in body
        ), (
            "_applyToolsetsChip must clear wrap.style.display so the CSS "
            "@container query is the single source of truth"
        )

    def test_applyToolsetsChip_still_tracks_state(self):
        """State tracking must be unchanged — /api/session/toolsets keeps working."""
        js = _src("ui.js")
        assert "_currentSessionToolsets = toolsets" in js, (
            "_applyToolsetsChip must continue to update _currentSessionToolsets "
            "so /api/session/toolsets reflects the active state"
        )


class TestToolsetsChipHTMLNoInlineDisplay:
    """index.html must not have inline style='display:none' — CSS owns it."""

    def test_html_does_not_force_inline_hide(self):
        """The composerToolsetsWrap div must not have inline style='display:none'."""
        html = _src("index.html")
        # Find the composerToolsetsWrap element
        m = re.search(r'<div[^>]*id="composerToolsetsWrap"[^>]*>', html)
        assert m, "composerToolsetsWrap div must exist in index.html"
        tag = m.group(0)
        assert 'style="display:none"' not in tag, (
            "composerToolsetsWrap must not have inline style='display:none' — "
            "the CSS base rule handles default-hidden state (#1431)"
        )
        # Also catch variants with whitespace/quotes
        assert "display:none" not in tag, (
            f"composerToolsetsWrap must not have any inline display:none: {tag!r}"
        )


class TestToolsetsAPIStillWorks:
    """The /api/session/toolsets endpoint and dropdown must remain wired."""

    def test_session_toolsets_endpoint_exists(self):
        """The api/session/toolsets endpoint must still be registered."""
        # Check api/routes.py for the endpoint
        try:
            with open("api/routes.py") as f:
                src = f.read()
        except FileNotFoundError:
            # If routes.py is named differently, search
            import os
            found = False
            for root, _, files in os.walk("api"):
                for f in files:
                    if f.endswith(".py"):
                        with open(os.path.join(root, f)) as fp:
                            if "session/toolsets" in fp.read():
                                found = True
                                break
                if found:
                    break
            assert found, "api/session/toolsets endpoint must exist somewhere in api/"
            return
        assert "session/toolsets" in src, (
            "/api/session/toolsets endpoint must still be registered "
            "(only the visual chip is hidden, not the underlying state)"
        )

    def test_toolsets_dropdown_renderer_exists(self):
        """_renderToolsetsDropdown must still exist for when chip becomes visible."""
        js = _src("ui.js")
        # Some form of toolsets dropdown machinery must remain so when the
        # chip is visible at wide widths, clicking it still opens the picker.
        assert "toggleToolsetsDropdown" in js, (
            "toggleToolsetsDropdown must still exist — when the chip is "
            "visible at wide widths, clicking it must still open the picker"
        )
        assert "_populateToolsetsDropdown" in js, (
            "_populateToolsetsDropdown must still exist for picker population"
        )


class TestToolsetsDropdownResizeGuard:
    """Opus-found defense: dropdown must close when chip becomes hidden by CSS.

    The dropdown is a DOM sibling of the wrap, not a child. CSS hiding the
    wrap (e.g. by crossing the 1100px container threshold mid-session via the
    workspace-panel toggle) does NOT cascade-hide the open dropdown. Without
    a guard, the dropdown would either snap to the footer's left edge with no
    anchor, or stay open with no visible chip to dismiss it from.
    """

    def test_resize_handler_closes_dropdown_when_chip_hidden(self):
        """Resize listener must close dropdown when the chip is no longer visible."""
        js = _src("ui.js")
        # Find the resize handler block for the toolsets dropdown
        # It must check chip.offsetParent === null and close, not reposition
        m = re.search(
            r"window\.addEventListener\('resize',\s*\([^)]*\)\s*=>\s*\{[^}]*composerToolsetsDropdown[^}]*\}",
            js, re.DOTALL,
        )
        assert m, "Toolsets resize handler must exist"
        body = m.group(0)
        assert "offsetParent" in body, (
            "Resize handler must check chip.offsetParent === null — without it "
            "the open dropdown stays open after CSS hides the chip mid-session "
            "(e.g. workspace-panel toggle crossing 1100px threshold)"
        )
        assert "closeToolsetsDropdown" in body, (
            "Resize handler must call closeToolsetsDropdown() when chip is "
            "hidden — repositioning a hidden chip leaves the dropdown anchored "
            "to a zero-rect element"
        )

    def test_position_dropdown_guards_against_hidden_chip(self):
        """_positionToolsetsDropdown must close-not-reposition if chip hidden."""
        js = _src("ui.js")
        m = re.search(
            r"function _positionToolsetsDropdown\(\)\s*\{.*?\n\}",
            js, re.DOTALL,
        )
        assert m, "_positionToolsetsDropdown function must exist"
        body = m.group(0)
        # Defense-in-depth: even direct callers of _positionToolsetsDropdown
        # must not anchor to a hidden chip.
        assert "offsetParent" in body, (
            "_positionToolsetsDropdown must check chip.offsetParent === null "
            "before reading getBoundingClientRect — defense-in-depth"
        )

    def test_toggle_dropdown_guards_against_hidden_chip(self):
        """toggleToolsetsDropdown must early-return if chip is hidden by CSS."""
        js = _src("ui.js")
        m = re.search(
            r"function toggleToolsetsDropdown\(\)\s*\{.*?\n\}",
            js, re.DOTALL,
        )
        assert m, "toggleToolsetsDropdown function must exist"
        body = m.group(0)
        # Currently the only invoker is the chip's own onclick (so this is
        # latent), but defensive guard is needed because the function is in
        # global scope and could be called by future #1431 redesign code.
        assert "offsetParent" in body, (
            "toggleToolsetsDropdown must check chip.offsetParent === null "
            "before opening — function is global and could be invoked when "
            "the chip is hidden by responsive CSS"
        )
