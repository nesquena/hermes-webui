"""
Sprint 12 Tests: settings panel, session pinning, session import, SSE reconnect.
"""
import json, pathlib, urllib.error, urllib.request, urllib.parse

from api import models
from tests._pytest_port import BASE, TEST_DEFAULT_MODEL


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

def test_default_model_updates_hermes_config():
    """POST /api/default-model updates the effective Hermes default model.

    As of #895 the endpoint returns a lightweight ack {ok, model} rather than
    the full model catalog, to avoid triggering a blocking live-provider fetch
    on every Settings save.  The default model is verified via /api/settings.
    """
    try:
        d, status = post("/api/default-model", {"model": "anthropic/claude-sonnet-4.6"})
        assert status == 200
        # Lightweight ack — no longer the full catalog
        assert d.get("ok") is True, f"expected ok=True, got {d}"
        assert 'claude-sonnet-4.6' in d.get("model", ""), (
            f"response model field should echo the saved model: {d}"
        )
        # Verify the setting actually persisted
        d2, _ = get("/api/settings")
        assert 'claude-sonnet-4.6' in d2['default_model']
    finally:
        post("/api/default-model", {"model": TEST_DEFAULT_MODEL})


def test_settings_does_not_persist_default_model():
    """POST /api/settings with default_model in body is silently ignored."""
    d1, _ = get("/api/settings")
    original_model = d1['default_model']
    # Send default_model via /api/settings — it must be dropped (not persisted)
    post("/api/settings", {"default_model": "openai/fake-model-xyz"})
    d2, _ = get("/api/settings")
    assert d2['default_model'] == original_model, (
        "POST /api/settings must not persist default_model — use /api/default-model instead"
    )


def test_default_model_empty_returns_400():
    """POST /api/default-model with empty model returns 400."""
    d, status = post("/api/default-model", {"model": ""})
    assert status == 400

def test_settings_partial_update():
    """POST /api/settings with partial data doesn't clobber other fields."""
    d1, _ = get("/api/settings")
    original_ws = d1['default_workspace']
    post("/api/settings", {"send_key": "ctrl+enter"})
    d2, _ = get("/api/settings")
    assert d2['send_key'] == 'ctrl+enter'
    assert d2['default_workspace'] == original_ws
    post("/api/settings", {"send_key": "enter"})


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


def _integration_hidden_precompression_snapshots_do_not_count_toward_pin_limit():
    """Hidden pre-compression snapshots should not block pinning visible sessions."""
    created = []
    settings_before, _ = get("/api/settings")
    sessions_before, _ = get("/api/sessions")
    baseline_visible_pins = sum(
        1 for session in sessions_before["sessions"]
        if session.get("pinned") and not session.get("archived")
    )
    original_limit = settings_before.get("pinned_sessions_limit", 3)
    post("/api/settings", {"pinned_sessions_limit": baseline_visible_pins + 3})
    try:
        for idx in range(2):
            sid = make_session(created)
            post("/api/session/rename", {"session_id": sid, "title": f"Visible Pin {idx}"})
            d, status = post("/api/session/pin", {"session_id": sid, "pinned": True})
            assert status == 200, d

        hidden_sid = make_session(created)
        post("/api/session/rename", {"session_id": hidden_sid, "title": "Hidden Snapshot Pin"})
        d, status = post("/api/session/pin", {"session_id": hidden_sid, "pinned": True})
        assert status == 200, d

        continuation_payload, status = post(
            "/api/session/import",
            {
                "title": "Visible Continuation",
                "messages": [{"role": "user", "content": "continuation"}],
                "model": "test/continuation",
            },
        )
        assert status == 200, continuation_payload
        continuation_sid = continuation_payload["session"]["session_id"]
        created.append(continuation_sid)
        continuation_path = pathlib.Path(models.SESSION_DIR) / f"{continuation_sid}.json"
        continuation_payload = json.loads(continuation_path.read_text(encoding="utf-8"))
        continuation_payload["parent_session_id"] = hidden_sid
        continuation_payload["title"] = "Visible Continuation"
        continuation_path.write_text(json.dumps(continuation_payload), encoding="utf-8")
        with models.LOCK:
            continuation_live = models.SESSIONS.get(continuation_sid)
            if continuation_live is not None:
                continuation_live.parent_session_id = hidden_sid
                continuation_live.title = "Visible Continuation"
        continuation = models.Session.load(continuation_sid)
        assert continuation is not None
        models._write_session_index(updates=[continuation])

        session_path = pathlib.Path(models.SESSION_DIR) / f"{hidden_sid}.json"
        payload = json.loads(session_path.read_text(encoding="utf-8"))
        payload["pre_compression_snapshot"] = True
        session_path.write_text(json.dumps(payload), encoding="utf-8")
        with models.LOCK:
            live = models.SESSIONS.get(hidden_sid)
            if live is not None:
                live.pre_compression_snapshot = True
        snapshot = models.Session.load(hidden_sid)
        assert snapshot is not None
        models._write_session_index(updates=[snapshot])

        fourth_sid = make_session(created)
        post("/api/session/rename", {"session_id": fourth_sid, "title": "Fourth Visible Pin"})
        d, status = post("/api/session/pin", {"session_id": fourth_sid, "pinned": True})
        assert status == 200, d
        assert d["session"]["pinned"] is True
    finally:
        post("/api/settings", {"pinned_sessions_limit": original_limit})
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
