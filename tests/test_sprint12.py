"""
Sprint 12 Tests: settings panel, session pinning, session import, SSE reconnect.
"""
import json, pathlib, urllib.error, urllib.request, urllib.parse

from tests._pytest_port import BASE


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def make_session(created_list):
    d, _ = post("/api/session/new", {})
    sid = d["session"]["session_id"]
    created_list.append(sid)
    return sid


# ── Settings API ──────────────────────────────────────────────────────────

def test_settings_get_returns_defaults():
    """GET /api/settings returns default settings."""
    d, status = get("/api/settings")
    assert status == 200
    assert 'default_model' in d
    assert 'default_workspace' in d

def test_settings_post_persists():
    """POST /api/settings saves and returns merged settings."""
    d, status = post("/api/settings", {"default_model": "test/model-123"})
    assert status == 200
    assert d['default_model'] == 'test/model-123'
    # Verify it persisted
    d2, _ = get("/api/settings")
    assert d2['default_model'] == 'test/model-123'
    # Restore
    post("/api/settings", {"default_model": "openai/gpt-5.4-mini"})

def test_settings_partial_update():
    """POST /api/settings with partial data doesn't clobber other fields."""
    d1, _ = get("/api/settings")
    original_ws = d1['default_workspace']
    post("/api/settings", {"default_model": "anthropic/claude-sonnet-4.6"})
    d2, _ = get("/api/settings")
    assert d2['default_model'] == 'anthropic/claude-sonnet-4.6'
    assert d2['default_workspace'] == original_ws
    # Restore
    post("/api/settings", {"default_model": "openai/gpt-5.4-mini"})


# ── Session Pinning ───────────────────────────────────────────────────────

def test_pin_session():
    """POST /api/session/pin sets pinned=true."""
    created = []
    try:
        sid = make_session(created)
        d, status = post("/api/session/pin", {"session_id": sid, "pinned": True})
        assert status == 200
        assert d['ok'] is True
        assert d['session']['pinned'] is True
    finally:
        for sid in created:
            post("/api/session/delete", {"session_id": sid})

def test_unpin_session():
    """POST /api/session/pin with pinned=false unpins."""
    created = []
    try:
        sid = make_session(created)
        post("/api/session/pin", {"session_id": sid, "pinned": True})
        d, status = post("/api/session/pin", {"session_id": sid, "pinned": False})
        assert status == 200
        assert d['session']['pinned'] is False
    finally:
        for sid in created:
            post("/api/session/delete", {"session_id": sid})

def test_pinned_in_session_list():
    """Pinned sessions include pinned field in session list."""
    created = []
    try:
        sid = make_session(created)
        # Pin it and give it a title so it shows in the list
        post("/api/session/rename", {"session_id": sid, "title": "Pinned Test"})
        post("/api/session/pin", {"session_id": sid, "pinned": True})
        d, _ = get("/api/sessions")
        match = [s for s in d['sessions'] if s['session_id'] == sid]
        assert len(match) == 1
        assert match[0]['pinned'] is True
    finally:
        for sid in created:
            post("/api/session/delete", {"session_id": sid})

def test_pinned_persists_on_reload():
    """Pin status survives session reload from disk."""
    created = []
    try:
        sid = make_session(created)
        post("/api/session/pin", {"session_id": sid, "pinned": True})
        d, _ = get(f"/api/session?session_id={sid}")
        assert d['session']['pinned'] is True
    finally:
        for sid in created:
            post("/api/session/delete", {"session_id": sid})


# ── Session Import ────────────────────────────────────────────────────────

def test_import_session_basic():
    """POST /api/session/import creates a new session from JSON."""
    payload = {
        "title": "Imported Test",
        "messages": [
            {"role": "user", "content": "Hello from import"},
            {"role": "assistant", "content": "Hi there!"},
        ],
        "model": "test/import-model",
    }
    d, status = post("/api/session/import", payload)
    assert status == 200
    assert d['ok'] is True
    sid = d['session']['session_id']
    try:
        assert d['session']['title'] == 'Imported Test'
        assert len(d['session']['messages']) == 2
        # Verify it loads correctly
        d2, _ = get(f"/api/session?session_id={sid}")
        assert d2['session']['model'] == 'test/import-model'
    finally:
        post("/api/session/delete", {"session_id": sid})

def test_import_requires_messages():
    """Import fails without a messages array."""
    d, status = post("/api/session/import", {"title": "No messages"})
    assert status == 400

def test_import_creates_new_id():
    """Imported session gets a new session_id, not reusing any from the payload."""
    payload = {
        "session_id": "should_be_ignored",
        "title": "ID Test",
        "messages": [{"role": "user", "content": "test"}],
    }
    d, _ = post("/api/session/import", payload)
    sid = d['session']['session_id']
    try:
        # The import should create a new ID, not use the one from the payload
        assert sid != "should_be_ignored"
    finally:
        post("/api/session/delete", {"session_id": sid})

def test_import_with_pinned():
    """Imported session can be pinned."""
    payload = {
        "title": "Pinned Import",
        "messages": [{"role": "user", "content": "test"}],
        "pinned": True,
    }
    d, _ = post("/api/session/import", payload)
    sid = d['session']['session_id']
    try:
        d2, _ = get(f"/api/session?session_id={sid}")
        assert d2['session']['pinned'] is True
    finally:
        post("/api/session/delete", {"session_id": sid})
