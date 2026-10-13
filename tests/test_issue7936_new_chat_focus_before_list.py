"""#7936: a fresh chat focuses the composer without waiting for the session list.

newSession() schedules the sidebar refresh itself. The New Chat button and
Cmd/Ctrl+K awaited a second renderSessionList() before the focus, and the render
queue ran it only after the first refresh's /api/sessions and /api/projects reads.
The browser gate tests/browser_new_chat_focus.py drives both paths with the list
response held; these pins keep the shape in the regular suite.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

FRESH_CHAT = "await newSession();closeMobileSidebar();$('msg').focus();"


def _block(marker: str, end: str) -> str:
    start = BOOT_JS.index(marker)
    return BOOT_JS[start : BOOT_JS.index(end, start)]


def test_the_new_chat_button_focuses_right_after_new_session():
    handler = _block("$('btnNewChat').onclick=async()=>{", "\n};")
    assert FRESH_CHAT in handler
    assert "await newSession();await renderSessionList()" not in handler


def test_the_new_chat_shortcut_focuses_right_after_new_session():
    branch = _block("if((e.metaKey||e.ctrlKey)&&e.key==='k'){", "\n  }")
    assert FRESH_CHAT in branch
    assert "renderSessionList" not in branch


def test_new_session_still_owns_the_sidebar_refresh():
    start = SESSIONS_JS.index("async function newSession(")
    body = SESSIONS_JS[start : SESSIONS_JS.index("\n}\n", start)]
    # newSession() owns the sole sidebar refresh, and it must force the paint
    # (deferWhileInteracting:false) so the new row/active highlight is not parked
    # while the pointer hovers #sessionList after the handlers dropped their
    # own awaited render (#7936).
    assert "refreshSessionList('new-session',{force:true})" in body
