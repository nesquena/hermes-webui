"""#7996: /new and the other slash-command callers of newSession() do not await a
second session-list render.

newSession() refreshes the sidebar itself (forced, see #7936). An awaited
renderSessionList() after it queued a second full /api/sessions read in front of
whatever came next: the composer focus and toast for /new, opening the terminal
for /terminal with no session, and the goal request for /goal with no session.
tests/browser_new_chat_focus.py drives /new with the list response held.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = COMMANDS_JS.index(f"async function {name}(")
    return COMMANDS_JS[start : COMMANDS_JS.index("\n}\n", start)]


def test_new_focuses_and_toasts_right_after_new_session():
    body = _function("cmdNew")
    assert "await newSession();\n  $('msg').focus();\n  showToast(t('new_session'));" in body
    assert "renderSessionList" not in body


def test_terminal_does_not_await_a_list_render_after_creating_a_session():
    body = _function("cmdTerminal")
    assert "await newSession(false, {worktree: false});" in body
    assert "await renderSessionList" not in body


def test_goal_does_not_await_a_list_render_after_creating_a_session():
    body = _function("cmdGoal")
    assert "if(!S.session){await newSession();}" in body
    # A later fire-and-forget render after the goal is set stays.
    assert "await renderSessionList" not in body


def test_new_session_still_owns_a_forced_sidebar_refresh():
    start = SESSIONS_JS.index("async function newSession(")
    body = SESSIONS_JS[start : SESSIONS_JS.index("\n}\n", start)]
    assert "refreshSessionList('new-session',{force:true})" in body
