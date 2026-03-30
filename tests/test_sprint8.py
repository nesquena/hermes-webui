"""
Sprint 8 Tests: Edit/regenerate, clear conversation, truncate, reconnect banner fix, syntax highlight.
"""
import json, pathlib, urllib.error, urllib.parse, urllib.request

BASE = "http://127.0.0.1:8788"

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())

def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code

def make_session_tracked(created_list):
    d, _ = post("/api/session/new", {})
    sid = d["session"]["session_id"]
    created_list.append(sid)
    return sid

# ── /api/session/clear ─────────────────────────────────────────────

def test_session_clear_requires_session_id(cleanup_test_sessions):
    data, status = post("/api/session/clear", {})
    assert status == 400

def test_session_clear_unknown_session_404(cleanup_test_sessions):
    data, status = post("/api/session/clear", {"session_id": "nonexistent_xyz"})
    assert status == 404

def test_session_clear_wipes_messages(cleanup_test_sessions):
    created = []
    sid = make_session_tracked(created)
    # Inject a fake message directly into the session via rename (to give it a title first)
    post("/api/session/rename", {"session_id": sid, "title": "clear-test"})
    # Manually load and verify session exists
    sess = get(f"/api/session?session_id={urllib.parse.quote(sid)}")
    assert sess["session"]["session_id"] == sid
    # Clear it
    data, status = post("/api/session/clear", {"session_id": sid})
    assert status == 200, f"Expected 200, got {status}: {data}"
    assert data.get("ok") is True
    assert data.get("session") is not None
    # Load again and verify messages empty
    sess2 = get(f"/api/session?session_id={urllib.parse.quote(sid)}")
    assert sess2["session"]["messages"] == []
    assert sess2["session"]["title"] == "Untitled"
    # Cleanup
    post("/api/session/delete", {"session_id": sid})

def test_session_clear_returns_session_compact(cleanup_test_sessions):
    created = []
    sid = make_session_tracked(created)
    data, status = post("/api/session/clear", {"session_id": sid})
    assert status == 200
    assert "session" in data
    assert data["session"]["session_id"] == sid
    post("/api/session/delete", {"session_id": sid})

# ── /api/session/truncate ──────────────────────────────────────────

def test_session_truncate_requires_session_id(cleanup_test_sessions):
    data, status = post("/api/session/truncate", {"keep_count": 2})
    assert status == 400

def test_session_truncate_requires_keep_count(cleanup_test_sessions):
    data, status = post("/api/session/truncate", {"session_id": "xyz"})
    assert status == 400

def test_session_truncate_unknown_session_404(cleanup_test_sessions):
    data, status = post("/api/session/truncate", {"session_id": "nonexistent_xyz", "keep_count": 0})
    assert status == 404

def test_session_truncate_returns_messages(cleanup_test_sessions):
    created = []
    sid = make_session_tracked(created)
    data, status = post("/api/session/truncate", {"session_id": sid, "keep_count": 0})
    assert status == 200
    assert data.get("ok") is True
    assert "messages" in data["session"]
    assert data["session"]["messages"] == []
    post("/api/session/delete", {"session_id": sid})

# ── Static files contain new features ─────────────────────────────

def test_app_js_contains_edit_message(cleanup_test_sessions):
    """Verify editMessage function is present in ui.js (Sprint 9: module split)."""
    with urllib.request.urlopen(BASE + "/static/ui.js", timeout=10) as r:
        src = r.read().decode()
    assert "editMessage" in src
    assert "msg-edit-area" in src

def test_app_js_contains_regenerate(cleanup_test_sessions):
    with urllib.request.urlopen(BASE + "/static/ui.js", timeout=10) as r:
        src = r.read().decode()
    assert "regenerateResponse" in src

def test_app_js_contains_clear_conversation(cleanup_test_sessions):
    with urllib.request.urlopen(BASE + "/static/panels.js", timeout=10) as r:
        src = r.read().decode()
    assert "clearConversation" in src
    assert "api/session/clear" in src

def test_app_js_contains_highlight_code(cleanup_test_sessions):
    with urllib.request.urlopen(BASE + "/static/ui.js", timeout=10) as r:
        src = r.read().decode()
    assert "highlightCode" in src
    assert "Prism" in src

def test_index_html_contains_prism(cleanup_test_sessions):
    with urllib.request.urlopen(BASE + "/", timeout=10) as r:
        src = r.read().decode()
    assert "prismjs" in src.lower()

def test_index_html_contains_clear_button(cleanup_test_sessions):
    with urllib.request.urlopen(BASE + "/", timeout=10) as r:
        src = r.read().decode()
    assert "btnClearConv" in src
    assert "clearConversation" in src
