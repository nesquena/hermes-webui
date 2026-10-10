"""#8004: first-send session creation does not await a redundant list render.

newSession() owns the forced sidebar refresh (see #7936). send() captures the
new session and routes slash creation paths through _ensureSlashSession(); the
creation and pane-ownership checks must not await another renderSessionList()
before dispatch. tests/browser_new_chat_focus.py covers the browser send path;
these pins cover the command branches it does not drive.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")

# Check only session-creation regions, so unrelated awaited renders such as the
# /sessions command remain valid.
AWAITED_RENDER_AFTER_NEW_SESSION = re.compile(
    r"await\s+newSession\s*\(\s*\)\s*;?[\s\S]*?"
    r"\bawait\s+renderSessionList\s*\("
)


def _send_body():
    start = MESSAGES_JS.index("async function send(){")
    return MESSAGES_JS[start:MESSAGES_JS.index("\n}\n", start)]


def test_the_send_path_creates_the_session_without_awaiting_a_list_render():
    body = _send_body()

    # The busy/compression path creates a session directly before queue or
    # control dispatch. Keep its captured result and pane guard, and make sure
    # no extra awaited list render was inserted anywhere in that path.
    busy_start = body.index("if(S.busy||compressionRunning){")
    busy_end = body.index(
        "if(S.session&&(S.session.read_only||S.session.is_read_only))", busy_start
    )
    busy_path = body[busy_start:busy_end]
    assert "const _createdSession=await newSession();" in busy_path
    assert "if(!_createdSession||!S.session||S.session.session_id!==_createdSession.session_id)return;" in busy_path
    assert "if(typeof _isSessionCurrentPane==='function'&&!_isSessionCurrentPane(_createdSession.session_id))return;" in busy_path
    assert not AWAITED_RENDER_AFTER_NEW_SESSION.search(busy_path)

    # Regular sends and creation-capable slash routes share this helper. It
    # accepts only the session newSession actually returned and verifies that
    # it still owns the visible pane before callers dispatch.
    helper_start = body.index("const _ensureSlashSession=async()=>{")
    helper_end = body.index("\n  };", helper_start)
    helper = body[helper_start:helper_end]
    assert "const _createdSession=await newSession();" in helper
    assert "if(!_createdSession||!S.session||S.session.session_id!==_createdSession.session_id)return false;" in helper
    assert "return typeof _isSessionCurrentPane==='function'\n      ?_isSessionCurrentPane(_createdSession.session_id)" in helper
    assert not AWAITED_RENDER_AFTER_NEW_SESSION.search(helper)

    # These are the only two newSession() callers in send(); both capture the
    # returned session. Every shared-helper call site must gate dispatch.
    assert len(re.findall(r"\bawait\s+newSession\s*\(\s*\)", body)) == 2
    helper_calls = body.count("await _ensureSlashSession()")
    guarded_helper_calls = body.count("if(!(await _ensureSlashSession()))return;")
    assert helper_calls >= 8
    assert guarded_helper_calls == helper_calls
    assert "if(!(await _ensureSlashSession()))return;\n\n  const activeSid=S.session.session_id;" in body

    # send() has one intentional awaited list render: the /sessions browser
    # command. Any other awaited list render would delay a send or slash dispatch.
    list_renders = list(re.finditer(r"\bawait\s+renderSessionList\s*\(", body))
    sessions_start = body.index("if(_parsedCmd.name==='sessions' || _parsedCmd.name==='resume'){")
    sessions_end = body.index("\n      }", sessions_start)
    assert len(list_renders) == 1
    assert sessions_start < list_renders[0].start() < sessions_end


@pytest.mark.parametrize("source", [
    "if(!S.session){const _createdSession=await newSession();await renderSessionList();}",
    "if(!S.session){\n  const _createdSession=await newSession();\n  if(!_createdSession||!S.session)return;\n  await renderSessionList();\n}",
    "if(!S.session){\n  const _createdSession=await newSession();\n  if(!_createdSession||!S.session)return;\n}\nif(typeof renderSessionList==='function') await renderSessionList();",
    "const _ensureSlashSession=async()=>{if(S.session)return true;const _createdSession=await newSession();if(!_createdSession||!S.session)return false;if(typeof renderSessionList==='function')await renderSessionList();return _isSessionCurrentPane(_createdSession.session_id);};\nif(!(await _ensureSlashSession()))return;",
    "const _ensureSlashSession=async()=>{if(S.session)return true;const _createdSession=await newSession();if(!_createdSession||!S.session)return false;return _isSessionCurrentPane(_createdSession.session_id);};\nif(!(await _ensureSlashSession()))return;\nif(typeof renderSessionList==='function')await renderSessionList();\nS.messages.push(userMessage);",
])
def test_the_pin_sees_an_awaited_render_in_any_spelling(source):
    assert AWAITED_RENDER_AFTER_NEW_SESSION.search(source)


def test_the_pin_allows_the_background_refresh_and_unrelated_renders():
    assert not AWAITED_RENDER_AFTER_NEW_SESSION.search(
        "if(!S.session){const _createdSession=await newSession();}\nvoid renderSessionList();"
    )
    assert not AWAITED_RENDER_AFTER_NEW_SESSION.search(
        "if(!S.session){const _createdSession=await newSession();}\nconst activeSid=S.session.session_id;"
    )
