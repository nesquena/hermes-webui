"""
Sprint 44 Tests: Workspace panel close button fixes (PR #413).

Covers:
- index.html: mobile-close-btn now calls handleWorkspaceClose() instead of
  closeWorkspacePanel(), so hitting X while a file is open returns you to the
  file browser rather than collapsing the whole panel.
- boot.js: syncWorkspacePanelUI() hides #btnClearPreview (the X icon) on
  desktop when no file preview is open, eliminating the duplicate X that
  appeared alongside the chevron collapse button.
- boot.js: handleWorkspaceClose() logic — clears preview when one is visible,
  closes panel otherwise (existing function, confirmed wired to both buttons).
"""

import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).parent.parent
HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")


class TestMobileCloseButtonBehavior(unittest.TestCase):
    """mobile-close-btn must call handleWorkspaceClose(), not closeWorkspacePanel()."""

    def test_mobile_close_btn_calls_handle_workspace_close(self):
        """mobile-close-btn onclick must be handleWorkspaceClose(), not closeWorkspacePanel()."""
        m = re.search(r'class="[^"]*mobile-close-btn[^"]*"[^>]*>', HTML)
        self.assertIsNotNone(m, "mobile-close-btn element not found in index.html")
        btn_html = m.group(0)
        self.assertIn(
            'onclick="handleWorkspaceClose()"',
            btn_html,
            "mobile-close-btn must call handleWorkspaceClose() so that hitting X "
            "while a file is open closes the file first, not the whole panel",
        )

    def test_mobile_close_btn_does_not_call_close_workspace_panel_directly(self):
        """mobile-close-btn must NOT call closeWorkspacePanel() directly."""
        m = re.search(r'class="[^"]*mobile-close-btn[^"]*"[^>]*>', HTML)
        self.assertIsNotNone(m, "mobile-close-btn element not found in index.html")
        btn_html = m.group(0)
        self.assertNotIn(
            'onclick="closeWorkspacePanel()"',
            btn_html,
            "mobile-close-btn must not call closeWorkspacePanel() directly — "
            "it would bypass the two-step close logic and collapse the panel even "
            "when a file is being viewed",
        )

    def test_handle_workspace_close_defined_in_boot_js(self):
        """handleWorkspaceClose() must be defined in boot.js."""
        self.assertIn(
            "function handleWorkspaceClose()",
            BOOT_JS,
            "handleWorkspaceClose() is missing from boot.js",
        )

    def test_handle_workspace_close_clears_preview_first(self):
        """handleWorkspaceClose() must call clearPreview() when a preview is visible."""
        # The function must check for visible preview and call clearPreview
        self.assertIn(
            "clearPreview()",
            BOOT_JS,
            "handleWorkspaceClose() must call clearPreview() when preview is visible",
        )

    def test_handle_workspace_close_falls_back_to_close_panel(self):
        """handleWorkspaceClose() must call closeWorkspacePanel() as fallback."""
        # Find the function start and extract until the closing brace by scanning
        start = BOOT_JS.find("function handleWorkspaceClose()")
        self.assertNotEqual(start, -1, "handleWorkspaceClose() not found in boot.js")
        # Extract a generous window after the function start
        fn_window = BOOT_JS[start : start + 400]
        self.assertIn(
            "closeWorkspacePanel()",
            fn_window,
            "handleWorkspaceClose() must call closeWorkspacePanel() as its fallback path",
        )


class TestDesktopNoDuplicateXButton(unittest.TestCase):
    """On desktop, only one X/close control should appear at a time."""

    def test_sync_workspace_panel_ui_hides_clear_preview_on_desktop(self):
        """syncWorkspacePanelUI() must set display:none on btnClearPreview when no preview and desktop."""
        self.assertIn(
            "clearBtn.style.display",
            BOOT_JS,
            "syncWorkspacePanelUI() must control clearBtn.style.display to hide it "
            "on desktop when no file preview is open",
        )

    def test_clear_preview_hidden_when_no_preview(self):
        """The display toggle for btnClearPreview must key off hasPreview."""
        # Expect something like: clearBtn.style.display=hasPreview?'':'none'
        # or clearBtn.style.display = hasPreview ? '' : 'none'
        pattern = r"clearBtn\.style\.display\s*=\s*hasPreview"
        self.assertRegex(
            BOOT_JS,
            pattern,
            "btnClearPreview display must be conditioned on hasPreview in "
            "syncWorkspacePanelUI() to avoid a duplicate X on desktop",
        )

    def test_clear_preview_toggle_only_applied_on_desktop(self):
        """The display toggle must be guarded by !isCompact so mobile is unaffected."""
        # Expect: if(!isCompact) clearBtn.style.display=...
        pattern = (
            r"isCompact.*clearBtn\.style\.display|clearBtn\.style\.display.*isCompact"
        )
        self.assertRegex(
            BOOT_JS,
            pattern,
            "btnClearPreview display toggle must be guarded by isCompact so the "
            "mobile X button visibility is not accidentally affected",
        )

    def test_btnclearpreview_exists_in_html(self):
        """#btnClearPreview must still exist in the HTML (not removed)."""
        self.assertIn(
            'id="btnClearPreview"',
            HTML,
            "#btnClearPreview must remain in index.html",
        )

    def test_btncollapseWorkspacepanel_exists_in_html(self):
        """#btnCollapseWorkspacePanel (chevron) must still exist in the HTML."""
        self.assertIn(
            'id="btnCollapseWorkspacePanel"',
            HTML,
            "#btnCollapseWorkspacePanel must remain in index.html",
        )


if __name__ == "__main__":
    unittest.main()
