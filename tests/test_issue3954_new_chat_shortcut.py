"""Focused regression tests for #3954 — move New Chat shortcut from Ctrl+K to Ctrl+Shift+O.

Base behavior: Ctrl/Cmd+K was the app-level New Chat chord; Ctrl/Cmd+Shift+O was absent.
Head behavior: Ctrl/Cmd+Shift+O is the New Chat chord; global Ctrl/Cmd+K new-chat path is removed.

The WebUI handler listens for the requested chord when the page receives the keydown event.
No claim is made about browser-chrome or OS delivery of Ctrl/Cmd+Shift+O.
"""
import re
from pathlib import Path

REPO = Path(__file__).parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")

NEW_CHAT_NEEDLE = "(e.metaKey||e.ctrlKey)&&e.shiftKey&&!e.altKey&&(e.key==='o'||e.key==='O')"


class TestIssue3954NewChatShortcutMoved:
    def test_new_shortcut_branch_exists(self):
        """Ctrl/Cmd+Shift+O branch is present in boot.js."""
        assert NEW_CHAT_NEEDLE in BOOT_JS, (
            "Cmd/Ctrl+Shift+O New Chat handler not found in static/boot.js"
        )

    def test_new_shortcut_requires_shift(self):
        """The new branch condition requires e.shiftKey."""
        idx = BOOT_JS.find(NEW_CHAT_NEEDLE)
        assert idx >= 0
        assert "e.shiftKey" in BOOT_JS[idx:idx + 120]

    def test_new_shortcut_excludes_alt(self):
        """The new branch condition excludes e.altKey to avoid AltGr conflicts."""
        idx = BOOT_JS.find(NEW_CHAT_NEEDLE)
        assert idx >= 0
        assert "!e.altKey" in BOOT_JS[idx:idx + 120]

    def test_new_shortcut_key_is_case_insensitive(self):
        """The key check covers both 'o' and 'O' for caps-lock tolerance."""
        idx = BOOT_JS.find(NEW_CHAT_NEEDLE)
        assert idx >= 0
        window = BOOT_JS[idx:idx + 120]
        assert "e.key==='o'" in window
        assert "e.key==='O'" in window

    def test_old_ctrl_k_new_chat_branch_removed(self):
        """Global Ctrl/Cmd+K no longer creates a new chat (old branch removed)."""
        matches = [m.start() for m in re.finditer(r"\(e\.metaKey\|\|e\.ctrlKey\)&&e\.key===.k.", BOOT_JS)]
        for m_idx in matches:
            window = BOOT_JS[m_idx:m_idx + 600]
            assert "newSession()" not in window, (
                "Ctrl/Cmd+K must not create a new session — old global shortcut must be removed"
            )

    def test_new_shortcut_calls_new_session(self):
        """Ctrl/Cmd+Shift+O branch calls newSession() to create the conversation."""
        idx = BOOT_JS.find(NEW_CHAT_NEEDLE)
        assert idx >= 0
        body = BOOT_JS[idx:idx + 1500]
        assert "newSession()" in body

    def test_new_shortcut_calls_render_session_list(self):
        """Ctrl/Cmd+Shift+O branch calls renderSessionList() to refresh the sidebar."""
        idx = BOOT_JS.find(NEW_CHAT_NEEDLE)
        assert idx >= 0
        body = BOOT_JS[idx:idx + 1500]
        assert "renderSessionList()" in body

    def test_new_shortcut_closes_mobile_sidebar(self):
        """Ctrl/Cmd+Shift+O branch calls closeMobileSidebar() so the chat is visible."""
        idx = BOOT_JS.find(NEW_CHAT_NEEDLE)
        assert idx >= 0
        body = BOOT_JS[idx:idx + 1500]
        assert "closeMobileSidebar()" in body

    def test_new_shortcut_focuses_composer(self):
        """Ctrl/Cmd+Shift+O branch focuses the composer after creating the session."""
        idx = BOOT_JS.find(NEW_CHAT_NEEDLE)
        assert idx >= 0
        body = BOOT_JS[idx:idx + 1500]
        assert "$('msg').focus()" in body

    def test_new_shortcut_uses_reusable_empty_guard(self):
        """Ctrl/Cmd+Shift+O branch reuses an existing empty session (#1171/#1432)."""
        idx = BOOT_JS.find(NEW_CHAT_NEEDLE)
        assert idx >= 0
        body = BOOT_JS[idx:idx + 1500]
        assert "_currentSessionIsReusableEmptyChat()" in body

    def test_new_shortcut_text_input_guard_before_prevent_default(self):
        """Text-input guard fires before preventDefault() (#5099 preserved for new chord)."""
        idx = BOOT_JS.find(NEW_CHAT_NEEDLE)
        assert idx >= 0
        body = BOOT_JS[idx:idx + 1500]
        guard_idx = body.find("if(isText) return")
        prevent_idx = body.find("e.preventDefault()")
        assert guard_idx >= 0, "Text-input guard missing from Ctrl/Cmd+Shift+O branch"
        assert prevent_idx >= 0, "preventDefault() missing from Ctrl/Cmd+Shift+O branch"
        assert guard_idx < prevent_idx, (
            "Text-input guard must come before preventDefault() in Ctrl/Cmd+Shift+O branch"
        )

    def test_tooltip_updated_to_new_shortcut(self):
        """btnNewChat tooltip advertises the new Cmd+Shift+O shortcut."""
        assert "Cmd+Shift+O" in INDEX_HTML, (
            "btnNewChat tooltip must advertise Cmd+Shift+O, not the old Cmd+K"
        )
        assert 'data-tooltip="New conversation (Cmd+K)"' not in INDEX_HTML, (
            "Old Cmd+K tooltip text must be removed from index.html"
        )

    def test_comment_updated_to_new_shortcut(self):
        """Boot.js shortcut comment names Cmd/Ctrl+Shift+O, not Cmd/Ctrl+K."""
        assert "Cmd/Ctrl+Shift+O creates a new chat" in BOOT_JS, (
            "Boot.js shortcut comment must be updated to Cmd/Ctrl+Shift+O"
        )
