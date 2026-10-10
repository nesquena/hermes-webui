"""#8004: sending the first message with no conversation open does not await a
second session-list render before the message is posted.

newSession() refreshes the sidebar itself (forced, see #7936). The send path's
nine ``if(!S.session){...}`` branches also awaited renderSessionList(), so the
first message waited for a full /api/sessions + /api/projects read before
POST /api/chat/start. tests/browser_new_chat_focus.py sends a first message with
the list response held; these pins cover the command branches it does not drive.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")

# An awaited list render that follows the awaited newSession(): on the same line,
# on the next one, after the guard's closing brace, or behind a typeof check.
AWAITED_RENDER_AFTER_NEW_SESSION = re.compile(
    r"await\s+newSession\s*\(\s*\)\s*;?\s*\}?\s*"
    r"(?:if\s*\([^)]*\)\s*)?await\s+renderSessionList\s*\("
)
NO_SESSION_GUARD = re.compile(r"if\s*\(\s*!\s*S\.session\s*\)\s*\{\s*await\s+newSession\s*\(\s*\)")


def _send_body():
    start = MESSAGES_JS.index("async function send(){")
    return MESSAGES_JS[start:MESSAGES_JS.index("\n}\n", start)]


def test_the_send_path_creates_the_session_without_awaiting_a_list_render():
    body = _send_body()
    assert not AWAITED_RENDER_AFTER_NEW_SESSION.search(body)
    # The composer ownership helper also rejects failed/superseded creates.
    assert body.count("if(!S.session&&!(await _ensureSessionForComposerAction())) return;") >= 9
    start = MESSAGES_JS.index("async function _ensureSessionForComposerAction(){")
    helper = MESSAGES_JS[start:MESSAGES_JS.index("\n}\n", start)]
    assert "await newSession()" in helper
    assert "renderSessionList" not in helper


@pytest.mark.parametrize("source", [
    "if(!S.session){await newSession();await renderSessionList();}",
    "if(!S.session){await newSession();}\n        await renderSessionList();",
    "if (!S.session) {\n  await newSession();\n  await renderSessionList();\n}",
    "if(!S.session){await newSession();}\nif(typeof renderSessionList==='function') await renderSessionList();",
])
def test_the_pin_sees_an_awaited_render_in_any_spelling(source):
    assert AWAITED_RENDER_AFTER_NEW_SESSION.search(source)


def test_the_pin_allows_the_background_refresh_and_unrelated_renders():
    assert not AWAITED_RENDER_AFTER_NEW_SESSION.search(
        "if(!S.session){await newSession();}\nvoid renderSessionList();"
    )
    assert not AWAITED_RENDER_AFTER_NEW_SESSION.search(
        "if(!S.session){await newSession();}\nconst activeSid=S.session.session_id;"
    )
