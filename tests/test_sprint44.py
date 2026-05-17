"""
Sprint 44 Tests: Workspace panel close button (PR #413).

Covers:
- btnClearPreview is the single close button for all devices. Clicking it calls
  handleWorkspaceClose(), which clears a file preview if open, or closes the
  entire workspace panel otherwise.
- The mobile-close-btn element was removed — only one X button remains.
- Tooltip text updated to "Close" (when no preview) and "Close preview"
  (when a file is being viewed).
"""
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).parent.parent
HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")


class TestSingleCloseButton(unittest.TestCase):
    """btnClearPreview is the only close button, visible on all devices."""

    def test_btn_clear_preview_exists_in_html(self):
        """#btnClearPreview must exist in index.html."""
        self.assertIn(
            'id="btnClearPreview"',
            HTML,
            "#btnClearPreview must be present in index.html",
        )

    def test_mobile_close_btn_removed_from_html(self):
        """mobile-close-btn element should no longer exist in index.html."""
        self.assertNotIn(
            "mobile-close-btn",
            HTML,
            "mobile-close-btn was removed — only btnClearPreview remains as close control",
        )

    def test_btn_clear_preview_wired_to_handle_workspace_close(self):
        """btnClearPreview onclick must be handleWorkspaceClose."""
        self.assertRegex(
            BOOT_JS,
            r"btnClearPreview.*onclick\s*=\s*handleWorkspaceClose",
            "btnClearPreview must call handleWorkspaceClose so that clicking X "
            "either clears a preview or closes the panel",
        )


class TestHandleWorkspaceCloseLogic(unittest.TestCase):
    """handleWorkspaceClose() clears preview first, falls back to close panel."""

    def test_function_defined(self):
        self.assertIn(
            "function handleWorkspaceClose()",
            BOOT_JS,
            "handleWorkspaceClose() must exist in boot.js",
        )

    def test_clears_preview_when_open(self):
        idx = BOOT_JS.find("function handleWorkspaceClose()")
        self.assertGreater(idx, 0, "handleWorkspaceClose() not found")
        body = BOOT_JS[idx:idx + 500]
        self.assertIn(
            "clearPreview()",
            body,
            "handleWorkspaceClose() must call clearPreview() when a preview is open",
        )

    def test_falls_back_to_close_panel(self):
        idx = BOOT_JS.find("function handleWorkspaceClose()")
        self.assertGreater(idx, 0, "handleWorkspaceClose() not found")
        body = BOOT_JS[idx:idx + 500]
        self.assertIn(
            "closeWorkspacePanel()",
            body,
            "handleWorkspaceClose() must call closeWorkspacePanel() as fallback",
        )


class TestTooltipText(unittest.TestCase):
    """The X button tooltip says 'Close' when no preview, 'Close preview' otherwise."""

    def test_tooltip_uses_close(self):
        """syncWorkspacePanelUI() sets tooltip to 'Close' (not 'Hide workspace panel')."""
        idx = BOOT_JS.find("function syncWorkspacePanelUI()")
        self.assertGreater(idx, 0, "syncWorkspacePanelUI() not found")
        body = BOOT_JS[idx:idx + 2000]
        # The tooltip line should contain 'Close' and NOT 'Hide workspace panel'
        self.assertIn(
            "'Close'",
            body,
            "btnClearPreview tooltip must use 'Close' instead of 'Hide workspace panel'",
        )


if __name__ == "__main__":
    unittest.main()
