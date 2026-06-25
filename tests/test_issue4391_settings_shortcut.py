"""Static-analysis tests for Ctrl/Cmd+, settings shortcut.

The global keydown handler in boot.js must have a branch that:
- Detects Ctrl/Cmd+, (comma key)
- Fires globally without a text-input skip guard (VS Code convention)
- Calls toggleSettings() if it exists
- Uses e.key (not e.code) for keyboard layout independence

Issue: #4391 (Ctrl+, opens Settings)
"""
from pathlib import Path

BOOT_JS = (Path(__file__).parent.parent / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (Path(__file__).parent.parent / "static" / "panels.js").read_text(encoding="utf-8")


class TestSettingsShortcutPresence:
    """The Ctrl+, handler must be present and correctly positioned."""

    def test_e_key_comma_check_present(self):
        """Shortcut must use e.key===',' to detect the comma key."""
        assert "e.key===','" in BOOT_JS, (
            "boot.js must contain e.key===',' to detect the comma key; "
            "this is layout-independent (e.code varies by physical layout)"
        )

    def test_no_e_code_comma_check(self):
        """Must NOT use e.code===',' since that varies by keyboard layout."""
        assert "e.code===','" not in BOOT_JS, (
            "boot.js must NOT use e.code===','; use e.key instead for layout independence"
        )

    def test_prevent_default_called(self):
        """e.preventDefault() must be called in the Ctrl+, branch."""
        # Find the e.key===',' block and verify e.preventDefault() is nearby.
        idx = BOOT_JS.find("e.key===',' ")
        if idx == -1:
            idx = BOOT_JS.find("e.key===','")
        assert idx >= 0, "Shortcut e.key===','  branch not found"
        # Extract a reasonable window around the match to verify preventDefault is called.
        branch = BOOT_JS[max(0, idx - 200):idx + 500]
        assert "e.preventDefault()" in branch, (
            "e.preventDefault() must be called before toggleSettings() "
            "in the Ctrl+, branch"
        )

    def test_toggleSettings_called(self):
        """toggleSettings() must be called in the Ctrl+, branch."""
        idx = BOOT_JS.find("e.key===',' ")
        if idx == -1:
            idx = BOOT_JS.find("e.key===','")
        assert idx >= 0, "Shortcut e.key===',' branch not found"
        branch = BOOT_JS[max(0, idx - 200):idx + 500]
        assert "toggleSettings()" in branch, (
            "toggleSettings() must be called in the Ctrl+, branch"
        )

    def test_typeof_guard_on_toggleSettings(self):
        """toggleSettings() must be guarded with typeof toggleSettings==='function'."""
        idx = BOOT_JS.find("e.key===',' ")
        if idx == -1:
            idx = BOOT_JS.find("e.key===','")
        assert idx >= 0, "Shortcut e.key===',' branch not found"
        branch = BOOT_JS[max(0, idx - 200):idx + 500]
        assert "typeof toggleSettings===" in branch or "typeof toggleSettings =" in branch, (
            "toggleSettings must be guarded with typeof toggleSettings==='function' "
            "to avoid runtime errors"
        )

    def test_no_text_input_skip_guard(self):
        """Shortcut must fire globally — no isText or isTextInput guard (VS Code behavior)."""
        idx = BOOT_JS.find("e.key===',' ")
        if idx == -1:
            idx = BOOT_JS.find("e.key===','")
        assert idx >= 0, "Shortcut e.key===',' branch not found"
        # Find the next 'if(e.key===' after this one (end of the Ctrl+, branch).
        branch_end = BOOT_JS.find("if(e.key===", idx + 50)
        if branch_end == -1:
            branch_end = len(BOOT_JS)
        branch = BOOT_JS[idx:branch_end]
        assert "isText" not in branch and "isTextInput" not in branch, (
            "Shortcut must NOT have a text-input skip guard; fire globally like VS Code"
        )

    def test_modifier_idiom_matches_ctrl_b(self):
        """Modifier check must use the same pattern as Ctrl+B: (e.metaKey||e.ctrlKey)&&!e.shiftKey&&!e.altKey."""
        # Verify the Ctrl+B idiom exists.
        assert "(e.metaKey||e.ctrlKey)&&!e.shiftKey&&!e.altKey&&(e.key==='b'||e.key==='B')" in BOOT_JS, (
            "Ctrl+B idiom not found in expected form"
        )
        # Verify the Ctrl+, block uses the same modifier idiom.
        idx = BOOT_JS.find("e.key===',' ")
        if idx == -1:
            idx = BOOT_JS.find("e.key===','")
        assert idx >= 0, "Shortcut e.key===',' branch not found"
        branch = BOOT_JS[max(0, idx - 200):idx + 100]
        assert "(e.metaKey||e.ctrlKey)" in branch, (
            "Ctrl+, must use (e.metaKey||e.ctrlKey) to detect Cmd or Ctrl"
        )
        assert "!e.shiftKey" in branch, (
            "Ctrl+, must use !e.shiftKey to ensure Shift is not pressed"
        )
        assert "!e.altKey" in branch, (
            "Ctrl+, must use !e.altKey to ensure Alt is not pressed"
        )


class TestTargetFunctionExists:
    """The toggleSettings() function must exist in panels.js and be callable."""

    def test_toggleSettings_defined(self):
        """toggleSettings() function must be defined in panels.js."""
        assert "function toggleSettings(" in PANELS_JS, (
            "panels.js must define toggleSettings() function"
        )

    def test_toggleSettings_not_just_declared(self):
        """toggleSettings() must be a real function, not just referenced."""
        idx = PANELS_JS.find("function toggleSettings(")
        assert idx >= 0, "toggleSettings() function not found"
        # Verify the next few lines contain a body (very basic check).
        body = PANELS_JS[idx:idx + 300]
        assert "{" in body, (
            "toggleSettings() must have a function body with braces"
        )
