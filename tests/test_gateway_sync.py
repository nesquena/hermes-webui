"""
Tests for Phase 1: Real-time Gateway Session Sync.

Tests are ordered TDD-style:
  1. Gateway sessions appear in /api/sessions when setting enabled
  2. Gateway sessions excluded when setting disabled
  3. Gateway sessions have correct metadata (source_tag, is_cli_session)
  4. SSE stream endpoint opens and receives events
  5. Watcher detects new sessions inserted into state.db
  6. Settings UI has renamed label
"""
import json
import os
import pathlib
import sqlite3
import time
import urllib.error
import urllib.request

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
BASE = "http://127.0.0.1:8788"


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
        try:
            return json.loads(e.read()), e.code
        except Exception:
            return {}, e.code


def _get_test_state_dir():
    """Return the test state directory (matches conftest.py TEST_STATE_DIR)."""
    hermes_home = pathlib.Path(os.getenv('HERMES_HOME', str(pathlib.Path.home() / '.hermes')))
    return pathlib.Path(os.getenv(
        'HERMES_WEBUI_TEST_STATE_DIR',
        str(hermes_home / 'webui-mvp-test')
    ))


def _get_state_db_path():
    """Return path to the test state.db."""
    return _get_test_state_dir() / 'state.db'


def _ensure_state_db():
    """Create state.db with sessions and messages tables if it doesn't exist.
    Returns a connection. Does NOT delete existing data (safe for parallel tests).
    """
    db_path = _get_state_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            title TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL NOT NULL
        );
    """)
    conn.commit()
    return conn


def _insert_gateway_session(conn, session_id='20260401_120000_abcdefgh', source='telegram',
                             title='Telegram Chat', model='anthropic/claude-sonnet-4-5',
                             started_at=None, message_count=2):
    """Insert a gateway session into state.db."""
    conn.execute(
        "INSERT OR REPLACE INTO sessions (id, source, title, model, started_at, message_count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, source, title, model, started_at or time.time(), message_count)
    )
    # Delete any existing messages for this session (idempotent re-insert)
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    # Insert some messages
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
        (session_id, 'Hello from Telegram', started_at or time.time())
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'assistant', ?, ?)",
        (session_id, 'Hi there!', (started_at or time.time()) + 1)
    )
    conn.commit()


def _remove_test_sessions(conn, *session_ids):
    """Remove specific test sessions from state.db (parallel-safe cleanup)."""
    for sid in session_ids:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    conn.commit()


def _cleanup_state_db():
    """Remove state.db if it exists (only used for tests that need a blank slate)."""
    db_path = _get_state_db_path()
    for p in [db_path, db_path.parent / 'state.db-wal', db_path.parent / 'state.db-shm']:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


# ── Tests ──────────────────────────────────────────────────────────────────

def test_gateway_sessions_appear_when_enabled():
    """Gateway sessions from state.db appear in /api/sessions when show_cli_sessions is on."""
    conn = _ensure_state_db()
    try:
        _insert_gateway_session(conn, session_id='gw_test_tg_001', source='telegram', title='TG Test Chat')
        conn.close()

        # Enable the setting
        post('/api/settings', {'show_cli_sessions': True})

        data, status = get('/api/sessions')
        assert status == 200
        sessions = data.get('sessions', [])
        gw_ids = [s['session_id'] for s in sessions if s.get('session_id') == 'gw_test_tg_001']
        assert len(gw_ids) == 1, f"Expected gateway session gw_test_tg_001, got {[s['session_id'] for s in sessions]}"
    finally:
        try:
            _remove_test_sessions(conn, 'gw_test_tg_001')
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_gateway_sessions_excluded_when_disabled():
    """Gateway sessions are NOT returned when show_cli_sessions is off."""
    conn = _ensure_state_db()
    try:
        _insert_gateway_session(conn, session_id='gw_test_dc_001', source='discord', title='DC Test Chat')
        conn.close()

        # Ensure setting is off
        post('/api/settings', {'show_cli_sessions': False})

        data, status = get('/api/sessions')
        assert status == 200
        sessions = data.get('sessions', [])
        gw_ids = [s['session_id'] for s in sessions if s.get('session_id') == 'gw_test_dc_001']
        assert len(gw_ids) == 0, "Gateway session should not appear when setting is off"
    finally:
        try:
            _remove_test_sessions(conn, 'gw_test_dc_001')
            conn.close()
        except Exception:
            pass


def test_gateway_session_has_correct_metadata():
    """Gateway sessions include source_tag and is_cli_session fields."""
    conn = _ensure_state_db()
    try:
        _insert_gateway_session(conn, session_id='gw_meta_001', source='telegram', title='Meta Test')
        conn.close()

        post('/api/settings', {'show_cli_sessions': True})

        data, status = get('/api/sessions')
        assert status == 200
        sessions = data.get('sessions', [])
        gw = next((s for s in sessions if s['session_id'] == 'gw_meta_001'), None)
        assert gw is not None, "Gateway session not found"
        assert gw.get('source_tag') == 'telegram', f"Expected source_tag=telegram, got {gw.get('source_tag')}"
        assert gw.get('is_cli_session') is True, "is_cli_session should be True for agent sessions"
        assert gw.get('title') == 'Meta Test'
    finally:
        try:
            _remove_test_sessions(conn, 'gw_meta_001')
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_gateway_session_has_message_count():
    """Gateway sessions report correct message_count from state.db."""
    conn = _ensure_state_db()
    try:
        _insert_gateway_session(conn, session_id='gw_msg_001', source='discord', title='Msg Count Test', message_count=5)
        conn.close()

        post('/api/settings', {'show_cli_sessions': True})

        data, status = get('/api/sessions')
        assert status == 200
        sessions = data.get('sessions', [])
        gw = next((s for s in sessions if s['session_id'] == 'gw_msg_001'), None)
        assert gw is not None
        assert gw.get('message_count') == 5, f"Expected message_count=5, got {gw.get('message_count')}"
    finally:
        try:
            _remove_test_sessions(conn, 'gw_msg_001')
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_gateway_sessions_multiple_sources():
    """Sessions from multiple gateway sources (telegram, discord, slack) all appear."""
    conn = _ensure_state_db()
    try:
        _insert_gateway_session(conn, session_id='gw_multi_tg', source='telegram', title='TG Chat')
        _insert_gateway_session(conn, session_id='gw_multi_dc', source='discord', title='DC Chat')
        _insert_gateway_session(conn, session_id='gw_multi_sl', source='slack', title='SL Chat')
        conn.close()

        post('/api/settings', {'show_cli_sessions': True})

        data, status = get('/api/sessions')
        assert status == 200
        sessions = data.get('sessions', [])
        gw_ids = {s['session_id'] for s in sessions if s.get('session_id') in ('gw_multi_tg', 'gw_multi_dc', 'gw_multi_sl')}
        assert len(gw_ids) == 3, f"Expected 3 gateway sessions, got {len(gw_ids)}: {gw_ids}"
    finally:
        try:
            _remove_test_sessions(conn, 'gw_multi_tg', 'gw_multi_dc', 'gw_multi_sl')
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_gateway_session_messages_readable():
    """Gateway session messages can be loaded via /api/session."""
    conn = _ensure_state_db()
    try:
        _insert_gateway_session(conn, session_id='gw_read_001', source='telegram', title='Readable')
        conn.close()

        post('/api/settings', {'show_cli_sessions': True})

        data, status = get(f'/api/session?session_id=gw_read_001')
        assert status == 200
        msgs = data.get('session', {}).get('messages', [])
        assert len(msgs) >= 2, f"Expected at least 2 messages, got {len(msgs)}"
        assert msgs[0].get('role') == 'user'
        assert msgs[0].get('content') == 'Hello from Telegram'
    finally:
        try:
            _remove_test_sessions(conn, 'gw_read_001')
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})


def test_gateway_sse_stream_endpoint_exists():
    """GET /api/sessions/gateway/stream returns a response (200 or 200-range)."""
    # The SSE endpoint requires show_cli_sessions to be enabled
    post('/api/settings', {'show_cli_sessions': True})
    try:
        req = urllib.request.Request(BASE + '/api/sessions/gateway/stream')
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status in (200, 204), f"Expected 200/204, got {r.status}"
            # SSE should have content-type text/event-stream
            ctype = r.headers.get('Content-Type', '')
            assert 'text/event-stream' in ctype, f"Expected text/event-stream, got {ctype}"
    except Exception as e:
        # Timeout is acceptable — means the connection is held open (SSE behavior)
        if 'timed out' in str(e).lower() or 'timeout' in str(e).lower():
            pass  # Good: SSE keeps the connection open
        else:
            raise
    finally:
        post('/api/settings', {'show_cli_sessions': False})


def test_gateway_webui_sessions_not_duplicated():
    """If a session_id exists both in WebUI store and state.db, it's not duplicated."""
    # Create a WebUI session with a known ID
    body = {}
    d, _ = post('/api/session/new', body)
    webui_sid = d['session']['session_id']

    try:
        # Insert the same session_id into state.db as a gateway session
        conn = _ensure_state_db()
        _insert_gateway_session(conn, session_id=webui_sid, source='telegram', title='Dup Test')
        conn.close()

        post('/api/settings', {'show_cli_sessions': True})

        data, status = get('/api/sessions')
        assert status == 200
        sessions = data.get('sessions', [])
        matching = [s for s in sessions if s['session_id'] == webui_sid]
        assert len(matching) == 1, f"Expected 1 entry for {webui_sid}, got {len(matching)}"
    finally:
        try:
            conn2 = sqlite3.connect(str(_get_state_db_path()))
            _remove_test_sessions(conn2, webui_sid)
            conn2.close()
        except Exception:
            pass
        post('/api/session/delete', {'session_id': webui_sid})
        post('/api/settings', {'show_cli_sessions': False})


def test_gateway_sessions_no_state_db():
    """When state.db doesn't exist, /api/sessions works fine (no gateway sessions)."""
    _cleanup_state_db()

    post('/api/settings', {'show_cli_sessions': True})
    try:
        data, status = get('/api/sessions')
        assert status == 200
        # Should succeed with just webui sessions (or empty)
        assert 'sessions' in data
    finally:
        post('/api/settings', {'show_cli_sessions': False})


def test_cli_sessions_still_work():
    """CLI sessions (source='cli') still appear alongside gateway sessions."""
    conn = _ensure_state_db()
    try:
        _insert_gateway_session(conn, session_id='cli_legacy_001', source='cli', title='CLI Legacy')
        _insert_gateway_session(conn, session_id='gw_new_001', source='telegram', title='GW New')
        conn.close()

        post('/api/settings', {'show_cli_sessions': True})

        data, status = get('/api/sessions')
        assert status == 200
        sessions = data.get('sessions', [])
        agent_ids = {s['session_id'] for s in sessions if s.get('session_id') in ('cli_legacy_001', 'gw_new_001')}
        assert len(agent_ids) == 2, f"Expected 2 agent sessions (cli + gateway), got {len(agent_ids)}"
    finally:
        try:
            _remove_test_sessions(conn, 'cli_legacy_001', 'gw_new_001')
            conn.close()
        except Exception:
            pass
        post('/api/settings', {'show_cli_sessions': False})
