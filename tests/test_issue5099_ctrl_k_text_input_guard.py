"""Static-analysis tests for #5099 (New Chat shortcut must not steal text-input shortcuts).

Emacs-adjacent users expect Ctrl+K to kill to end-of-line while the composer
or other editable fields are focused. The new Cmd/Ctrl+Shift+O shortcut must
still skip new-chat creation in text inputs, but it suppresses the browser
default first when the page receives that chord. Ctrl+K no longer creates a new
chat globally.
"""
from pathlib import Path

BOOT_JS = (Path(__file__).parent.parent / "static" / "boot.js").read_text(encoding="utf-8")


def _new_chat_branch_window() -> str:
    idx = BOOT_JS.find("(e.metaKey||e.ctrlKey)&&e.shiftKey&&!e.altKey&&(e.key==='o'||e.key==='O')")
    assert idx >= 0, "Cmd/Ctrl+Shift+O handler not found in boot.js"
    return BOOT_JS[idx:idx + 1500]


class TestIssue5099NewChatShortcutTextInputGuard:
    def test_new_chat_shortcut_skips_text_inputs(self):
        branch = _new_chat_branch_window()
        assert "tagName==='INPUT'" in branch
        assert "tagName==='TEXTAREA'" in branch
        assert "isContentEditable" in branch
        assert "if(isText) return" in branch

    def test_new_chat_shortcut_prevents_browser_default_before_text_guard(self):
        branch = _new_chat_branch_window()
        guard_idx = branch.find("if(isText) return")
        prevent_idx = branch.find("e.preventDefault()")
        assert guard_idx >= 0 and prevent_idx >= 0, (
            "Ctrl/Cmd+Shift+O must both suppress browser defaults and guard text inputs"
        )
        assert prevent_idx < guard_idx, (
            "preventDefault() must run before the text-input early return"
        )

    def test_new_chat_shortcut_keeps_text_guard_shape(self):
        new_chat_block = _new_chat_branch_window()
        for needle in (
            "const t=e.target",
            "const isText=t&&",
            "tagName==='INPUT'",
            "tagName==='TEXTAREA'",
            "isContentEditable",
        ):
            assert needle in new_chat_block, f"Ctrl/Cmd+Shift+O guard missing {needle!r}"

    def test_new_chat_shortcut_still_creates_new_session_outside_inputs(self):
        branch = _new_chat_branch_window()
        assert "newSession()" in branch
        assert "closeMobileSidebar()" in branch

    def test_ctrl_k_is_not_global_new_chat_chord(self):
        """Ctrl/Cmd+K must no longer be the app-level New Chat shortcut."""
        # There must be no branch of the form (metaKey||ctrlKey)&&key==='k'
        # that also calls newSession() — the old global new-chat path is removed.
        import re
        matches = [m.start() for m in re.finditer(r"\(e\.metaKey\|\|e\.ctrlKey\)&&e\.key===.k.", BOOT_JS)]
        for m_idx in matches:
            window = BOOT_JS[m_idx:m_idx + 600]
            assert "newSession()" not in window, (
                "Ctrl/Cmd+K must not create a new session (old global shortcut removed)"
            )
