"""Regression checks for issue #2508 session pinning bounds and context menu access."""

import json
import pathlib
import urllib.error
import urllib.request

from tests._pytest_port import BASE


ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTES_PY = (ROOT / "api" / "routes.py").read_text()
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text()
STYLE_CSS = (ROOT / "static" / "style.css").read_text()


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def make_session(created):
    payload = {
        "title": f"Pin cap {len(created) + 1}",
        "messages": [{"role": "user", "content": "keep this conversation handy"}],
        "model": "test/pin-cap",
    }
    d, status = post("/api/session/import", payload)
    assert status == 200
    sid = d["session"]["session_id"]
    created.append(sid)
    return sid


def test_session_pin_endpoint_caps_pinned_sessions_at_three():
    created = []
    try:
        pinned = [make_session(created) for _ in range(3)]
        for sid in pinned:
            d, status = post("/api/session/pin", {"session_id": sid, "pinned": True})
            assert status == 200
            assert d["session"]["pinned"] is True

        fourth = make_session(created)
        d, status = post("/api/session/pin", {"session_id": fourth, "pinned": True})
        assert status == 400
        assert "3 sessions" in d.get("error", "")

        d, status = post("/api/session/pin", {"session_id": pinned[0], "pinned": False})
        assert status == 200
        assert d["session"]["pinned"] is False

        d, status = post("/api/session/pin", {"session_id": fourth, "pinned": True})
        assert status == 200
        assert d["session"]["pinned"] is True
    finally:
        for sid in created:
            post("/api/session/delete", {"session_id": sid})


def test_session_pin_cap_has_backend_and_frontend_guards():
    assert 'pinned_ids = {' in ROUTES_PY
    assert 'pinned_ids.update(' in ROUTES_PY
    assert 'pinned_sessions_limit = int(load_settings().get("pinned_sessions_limit", 3) or 3)' in ROUTES_PY
    assert 'if len(pinned_ids) >= pinned_sessions_limit:' in ROUTES_PY
    assert 'Up to {pinned_sessions_limit} sessions can be pinned' in ROUTES_PY

    assert 'function _pinnedSessionCount()' in SESSIONS_JS
    assert 'function _getPinnedSessionsLimit()' in SESSIONS_JS
    assert 'function _pinnedSessionsLimit()' not in SESSIONS_JS
    assert 'const pinLimitReached=!session.pinned&&_pinnedSessionCount()>=_getPinnedSessionsLimit();' in SESSIONS_JS
    assert 'Only ${limit} conversations can be pinned' in SESSIONS_JS
    assert ".session-action-opt.is-disabled{opacity:.55;cursor:not-allowed;}" in STYLE_CSS


def test_session_rows_open_action_menu_from_right_click():
    assert 'el.oncontextmenu=(e)=>{' in SESSIONS_JS
    context_idx = SESSIONS_JS.find('el.oncontextmenu=(e)=>{')
    assert context_idx != -1
    block = SESSIONS_JS[context_idx:SESSIONS_JS.find('};', context_idx) + 2]
    assert 'e.preventDefault();' in block
    assert 'e.stopPropagation();' in block
    assert '_openSessionActionMenu(s, actions||el);' in block
