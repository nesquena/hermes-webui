"""ACP-sourced state.db sessions must be visible in the sidebar.

The gateway's ACP adapter (Agent Client Protocol — Zed, external device
bridges such as a Rabbit R1) persists sessions to state.db with
``source='acp'``. Before this fix, 'acp' normalised to session_source
'other', which fell through BOTH sidebar buckets:

- ``sidebar_source=webui`` skips the state.db projection entirely, and an
  ACP session has no WebUI sidecar, so it never appeared there.
- ``sidebar_source=cli`` keeps only CLI-classified rows
  (``_is_cli_session_for_settings``), which dropped 'other' rows.

ACP is a local interactive agent client like the CLI/TUI, so it is now
classified into the CLI family.
"""

import sqlite3
import time
import unittest.mock

import api.models as models
from api.agent_sessions import (
    is_cli_session_row,
    is_cli_session_row_visible,
    normalize_agent_session_source,
)


def test_acp_normalizes_to_cli_family():
    meta = normalize_agent_session_source('acp')
    assert meta['session_source'] == 'cli'
    assert meta['raw_source'] == 'acp'
    assert meta['source_label'] == 'ACP'


def test_acp_row_is_cli_classified():
    row = {'id': 'e91ffb31-654c-4b62-9ca1-6ecc16423899', 'source': 'acp'}
    assert is_cli_session_row({**row, **normalize_agent_session_source('acp')}) is True
    # Raw rows (no prior normalization) must classify the same way — several
    # callers pass sidecar/state rows that only carry source metadata.
    assert is_cli_session_row(row) is True


def test_acp_row_with_messages_stays_visible_even_when_ended():
    """Ended/untitled ACP rows are user-driven conversations, never framework noise."""
    row = {
        'id': 'acp-ended',
        'source': 'acp',
        'title': 'Acp Session',  # default-shaped title
        'message_count': 4,
        'actual_message_count': 4,
        'actual_user_message_count': 1,  # below CLI_MIN_UNTITLED_USER_MESSAGE_COUNT
        'ended_at': 1751000000.0,
        'end_reason': 'client_disconnect',
    }
    assert is_cli_session_row_visible({**row, **normalize_agent_session_source('acp')}) is True


def test_empty_acp_row_stays_hidden():
    """Zero-message ACP rows (connect/reconnect stubs) must not clutter the sidebar."""
    row = {
        'id': 'acp-empty',
        'source': 'acp',
        'title': None,
        'message_count': 0,
        'actual_message_count': 0,
        'actual_user_message_count': 0,
    }
    assert is_cli_session_row_visible({**row, **normalize_agent_session_source('acp')}) is False


def test_acp_row_without_user_turns_stays_hidden():
    """An ACP row holding only assistant/tool/system messages is not user-driven.

    A replayed or aborted turn can leave an ended ACP connection with a
    positive message_count but zero user turns — it must not surface in the
    sidebar (Greptile review on #5939).
    """
    row = {
        'id': 'acp-no-user-turns',
        'source': 'acp',
        'title': 'Replayed segment',
        'message_count': 3,
        'actual_message_count': 3,
        'actual_user_message_count': 0,
        'ended_at': 1751000000.0,
        'end_reason': 'client_disconnect',
    }
    assert is_cli_session_row_visible({**row, **normalize_agent_session_source('acp')}) is False


# --- state.db projection (mirrors tests/test_issue3586_cli_session_source_label.py) ---

def _make_state_db(path, sessions):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            session_source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE INDEX idx_sessions_started ON sessions(started_at);
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    base = time.time() - len(sessions)
    for i, sess in enumerate(sessions):
        sid = sess['id']
        started = base + i
        conn.execute(
            """
            INSERT INTO sessions
            (id, source, session_source, title, model, started_at, message_count,
             parent_session_id, ended_at, end_reason)
            VALUES (?, ?, NULL, ?, 'deepseek/deepseek-chat', ?, 2, NULL, NULL, NULL)
            """,
            (sid, sess['source'], sess.get('title', sid), started),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, 'user', 'hello', ?)",
            (f"msg_{sid}_0", sid, started),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, 'user', 'follow-up', ?)",
            (f"msg_{sid}_1", sid, started + 0.1),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, 'assistant', 'hi', ?)",
            (f"msg_{sid}_2", sid, started + 0.2),
        )
    conn.commit()
    conn.close()


def _call_uncached(tmp_path, sessions):
    db = tmp_path / 'state.db'
    _make_state_db(db, sessions)
    with (
        unittest.mock.patch('api.models.get_claude_code_sessions', return_value=[]),
        unittest.mock.patch('api.models.ensure_cron_project', return_value='cron-project-id'),
    ):
        return models._load_cli_sessions_uncached(tmp_path, db, None)


def test_acp_session_appears_in_projection_as_cli(tmp_path):
    """An ACP state.db row must surface in the sidebar projection, CLI-classified."""
    sessions = [
        {
            'id': 'e91ffb31-654c-4b62-9ca1-6ecc16423899',
            'source': 'acp',
            'title': 'Raumanalyse mit Schimmelfleck-Beurteilung',
        },
    ]
    results = _call_uncached(tmp_path, sessions)
    row = next(
        (s for s in results if s['session_id'] == 'e91ffb31-654c-4b62-9ca1-6ecc16423899'),
        None,
    )
    assert row is not None, "acp session should appear in the sidebar projection"
    assert row['is_cli_session'] is True
    assert row['session_source'] == 'cli'
    assert row['source_label'] == 'ACP'
