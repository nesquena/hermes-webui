"""Regression checks for issue #2508 session pinning bounds and context menu access."""

import json
import pathlib
import time
from types import SimpleNamespace
import urllib.error
import urllib.request

from tests._pytest_port import BASE, TEST_STATE_DIR


ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


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



def inject_hidden_pinned_snapshot(sid="hidden-pinned-snapshot"):
    """Add a persisted legacy hidden snapshot without touching server memory."""
    sessions_dir = TEST_STATE_DIR / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    row = {
        "session_id": sid,
        "title": "Hidden pinned snapshot",
        "workspace": str(TEST_STATE_DIR / "test-workspace"),
        "model": "test/pin-cap",
        "created_at": now,
        "updated_at": now,
        "last_message_at": now,
        "message_count": 1,
        "messages": [{"role": "user", "content": "legacy hidden snapshot"}],
        "tool_calls": [],
        "pinned": True,
        "archived": False,
        "pre_compression_snapshot": True,
        "_show_pre_compression_snapshot": False,
    }
    (sessions_dir / f"{sid}.json").write_text(json.dumps(row), encoding="utf-8")
    index_path = sessions_dir / "_index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        index = []
    compact = {k: v for k, v in row.items() if k not in {"messages", "tool_calls"}}
    index = [item for item in index if item.get("session_id") != sid]
    index.append(compact)
    index_path.write_text(json.dumps(index), encoding="utf-8")
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


def test_session_pin_endpoint_ignores_hidden_snapshot_when_enforcing_cap():
    created = []
    hidden_sid = "hidden-pinned-snapshot-quota-route"
    try:
        hidden = inject_hidden_pinned_snapshot(hidden_sid)
        pinned = [make_session(created) for _ in range(2)]
        for sid in pinned:
            d, status = post("/api/session/pin", {"session_id": sid, "pinned": True})
            assert status == 200
            assert d["session"]["pinned"] is True

        third_visible = make_session(created)
        d, status = post("/api/session/pin", {"session_id": third_visible, "pinned": True})
        assert status == 200, d
        assert d["session"]["pinned"] is True
        assert hidden not in {third_visible, *pinned}
    finally:
        for sid in created:
            post("/api/session/delete", {"session_id": sid})
        (TEST_STATE_DIR / "sessions" / f"{hidden_sid}.json").unlink(missing_ok=True)
        index_path = TEST_STATE_DIR / "sessions" / "_index.json"
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index = [item for item in index if item.get("session_id") != hidden_sid]
            index_path.write_text(json.dumps(index), encoding="utf-8")
        except (FileNotFoundError, json.JSONDecodeError):
            pass


def test_hidden_pre_compression_snapshot_does_not_count_toward_pin_quota():
    from api.routes import _session_counts_toward_pin_quota

    assert _session_counts_toward_pin_quota({
        "session_id": "hidden-snapshot",
        "pinned": True,
        "archived": False,
        "pre_compression_snapshot": True,
    }) is False
    assert _session_counts_toward_pin_quota({
        "session_id": "visible-session",
        "pinned": True,
        "archived": False,
        "pre_compression_snapshot": False,
    }) is True


def test_hidden_in_memory_snapshot_does_not_count_toward_pin_quota():
    from api.routes import _session_counts_toward_pin_quota

    snapshot = SimpleNamespace(
        session_id="hidden-memory-snapshot",
        pinned=True,
        archived=False,
        pre_compression_snapshot=True,
    )
    assert _session_counts_toward_pin_quota(snapshot) is False


def test_session_pin_cap_has_backend_and_frontend_guards():
    # #3288 renamed the in-LOCK pin counter to count visible lineages
    # (pinned_lineage_ids) instead of raw session ids (pinned_ids), so a
    # continuation lineage no longer consumes multiple pin slots. The guard
    # behaviour (snapshot, merge under LOCK, compare against the limit, 400) is
    # unchanged.
    assert 'persisted_rows = [' in ROUTES_PY
    assert 'candidate_rows.extend(' in ROUTES_PY
    assert 'pinned_lineage_ids = _visible_pinned_lineage_ids(candidate_rows)' in ROUTES_PY
    assert 'pinned_sessions_limit = int(load_settings().get("pinned_sessions_limit", 3) or 3)' in ROUTES_PY
    assert 'if len(pinned_lineage_ids) >= pinned_sessions_limit:' in ROUTES_PY
    assert 'Up to {pinned_sessions_limit} sessions can be pinned' in ROUTES_PY

    assert 'function _pinnedSessionCount()' in SESSIONS_JS
    assert 'function _getPinnedSessionsLimit()' in SESSIONS_JS
    assert 'function _pinnedSessionsLimit()' not in SESSIONS_JS
    assert 'const pinLimitReached=!session.pinned&&_pinnedSessionCount()>=_getPinnedSessionsLimit();' not in SESSIONS_JS
    assert 'if(pinLimitReached)' not in SESSIONS_JS
    assert "await api('/api/session/pin'" in SESSIONS_JS
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
