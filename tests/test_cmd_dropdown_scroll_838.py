"""Tests for #838 — slash command dropdown keyboard navigation keeps the
selected item in view."""
import os
import re


_SRC = os.path.join(os.path.dirname(__file__), "..")


def _read(name):
    return open(os.path.join(_SRC, name), encoding="utf-8").read()


class TestNavigateCmdDropdownScroll:
    """navigateCmdDropdown must scroll the newly selected item into view so
    keyboard navigation on a long list doesn't leave the highlight below the
    visible area of the dropdown."""

    def test_navigate_calls_scroll_into_view(self):
        js = _read("static/commands.js")
        m = re.search(r'function navigateCmdDropdown\(.*?\n\}', js, re.DOTALL)
        assert m, "navigateCmdDropdown not found"
        fn = m.group(0)
        assert 'scrollIntoView' in fn, (
            "navigateCmdDropdown must call scrollIntoView on the newly "
            "selected item so ↓/↑ keeps the highlight visible (#838)"
        )

    def test_scroll_uses_nearest_block_alignment(self):
        """`{block:'nearest'}` is the correct option: scrolls only when
        needed, minimum distance — won't jump the list around on every
        arrow-key press when the item is already in view."""
        js = _read("static/commands.js")
        m = re.search(r'function navigateCmdDropdown\(.*?\n\}', js, re.DOTALL)
        assert m
        fn = m.group(0)
        assert "block:'nearest'" in fn or 'block: "nearest"' in fn, (
            "scrollIntoView should use {block:'nearest'} to scroll the "
            "minimum amount needed"
        )

    def test_scroll_after_selected_class_update(self):
        """The scroll call must come AFTER adding the .selected class so
        the correct item is targeted."""
        js = _read("static/commands.js")
        m = re.search(r'function navigateCmdDropdown\(.*?\n\}', js, re.DOTALL)
        assert m
        fn = m.group(0)
        selected_pos = fn.find("classList.add('selected')")
        scroll_pos = fn.find("scrollIntoView")
        assert selected_pos != -1 and scroll_pos != -1
        assert selected_pos < scroll_pos, (
            "scrollIntoView must run after classList.add('selected') so it "
            "scrolls the newly-highlighted item into view"
        )

    def test_cmd_dropdown_is_scroll_container(self):
        """Regression guard: the .cmd-dropdown must have overflow-y:auto
        (or similar) so scrollIntoView finds it as the scroll ancestor
        rather than bubbling up to the viewport."""
        css = _read("static/style.css")
        m = re.search(r'\.cmd-dropdown\s*\{[^}]+\}', css)
        assert m, ".cmd-dropdown rule not found"
        block = m.group(0)
        assert 'overflow-y:auto' in block or 'overflow-y: auto' in block or \
               'overflow:auto' in block or 'overflow: auto' in block, (
            ".cmd-dropdown must have overflow-y:auto so scrollIntoView "
            "scrolls within the dropdown, not the whole page"
        )
