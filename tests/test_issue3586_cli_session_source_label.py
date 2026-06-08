"""Regression coverage for is_cli_session wrongly True for messaging sessions (#3586).

_load_cli_sessions_uncached() previously hardcoded 'is_cli_session': True for
every state.db row. Sessions from Discord, Telegram, Slack, and cron must have
is_cli_session == False; only rows whose source normalises to 'cli' should be True.
"""

import sqlite3
import time
import unittest.mock

import api.models as models


def _make_state_db(path, sessions):
    """Create a minimal state.db with the given sessions list.

    Each item is a dict with at least: id, source, title.
    Messaging sessions get one user + one assistant message (enough to be
    importable since they are not CLI and bypass the user-turn threshold).
    CLI sessions get a non-generic title so is_cli_session_row_visible does
    not apply the CLI_MIN_UNTITLED_USER_MESSAGE_COUNT guard; they also get
    two user messages as belt-and-suspenders.
    """
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
        # Two user messages so CLI rows pass CLI_MIN_UNTITLED_USER_MESSAGE_COUNT (=2).
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
    """Build a state.db from ``sessions``, call _load_cli_sessions_uncached, return results."""
    db = tmp_path / 'state.db'
    _make_state_db(db, sessions)
    hermes_home = tmp_path

    # Patch out side-effects that touch the real filesystem.
    with (
        unittest.mock.patch('api.models.get_claude_code_sessions', return_value=[]),
        unittest.mock.patch('api.models.ensure_cron_project', return_value='cron-project-id'),
    ):
        return models._load_cli_sessions_uncached(hermes_home, db, None)


def _find(results, sid):
    return next((s for s in results if s['session_id'] == sid), None)


def test_discord_session_is_not_cli(tmp_path):
    """Discord-sourced sessions must have is_cli_session == False."""
    sessions = [
        {'id': 'discord_abc123', 'source': 'discord', 'title': 'Discord Chat'},
    ]
    results = _call_uncached(tmp_path, sessions)
    row = _find(results, 'discord_abc123')
    assert row is not None, "discord session should appear in results"
    assert row['is_cli_session'] is False, (
        f"expected is_cli_session=False for discord session, got {row['is_cli_session']!r}"
    )


def test_telegram_session_is_not_cli(tmp_path):
    """Telegram-sourced sessions must have is_cli_session == False."""
    sessions = [
        {'id': 'telegram_xyz789', 'source': 'telegram', 'title': 'Telegram Chat'},
    ]
    results = _call_uncached(tmp_path, sessions)
    row = _find(results, 'telegram_xyz789')
    assert row is not None, "telegram session should appear in results"
    assert row['is_cli_session'] is False, (
        f"expected is_cli_session=False for telegram session, got {row['is_cli_session']!r}"
    )


def test_slack_session_is_not_cli(tmp_path):
    """Slack-sourced sessions must have is_cli_session == False."""
    sessions = [
        {'id': 'slack_def456', 'source': 'slack', 'title': 'Slack Chat'},
    ]
    results = _call_uncached(tmp_path, sessions)
    row = _find(results, 'slack_def456')
    assert row is not None, "slack session should appear in results"
    assert row['is_cli_session'] is False, (
        f"expected is_cli_session=False for slack session, got {row['is_cli_session']!r}"
    )


def test_cron_session_is_not_cli(tmp_path):
    """Cron-sourced sessions must have is_cli_session == False."""
    sessions = [
        {'id': 'cron_job1_1717000000', 'source': 'cron', 'title': 'Scheduled Job'},
    ]
    results = _call_uncached(tmp_path, sessions)
    row = _find(results, 'cron_job1_1717000000')
    assert row is not None, "cron session should appear in results"
    assert row['is_cli_session'] is False, (
        f"expected is_cli_session=False for cron session, got {row['is_cli_session']!r}"
    )


def test_cli_session_is_cli(tmp_path):
    """CLI-sourced sessions must have is_cli_session == True."""
    sessions = [
        # Non-generic title bypasses the _looks_like_default_cli_title guard;
        # two user messages are inserted by _make_state_db for belt-and-suspenders.
        {'id': 'cli_session_001', 'source': 'cli', 'title': 'Refactor auth module'},
    ]
    results = _call_uncached(tmp_path, sessions)
    row = _find(results, 'cli_session_001')
    assert row is not None, "cli session should appear in results"
    assert row['is_cli_session'] is True, (
        f"expected is_cli_session=True for cli session, got {row['is_cli_session']!r}"
    )


def test_mixed_sources_classified_correctly(tmp_path):
    """Mixed source types are each classified correctly in a single db scan."""
    sessions = [
        {'id': 'disc_1', 'source': 'discord', 'title': 'Discord'},
        {'id': 'tele_1', 'source': 'telegram', 'title': 'Telegram'},
        # Non-generic title so the CLI visibility check passes without extra tuning.
        {'id': 'cli_1', 'source': 'cli', 'title': 'Improve parser performance'},
        {'id': 'cron_job2_1717000001', 'source': 'cron', 'title': 'Cron'},
    ]
    results = _call_uncached(tmp_path, sessions)
    by_id = {s['session_id']: s for s in results}

    assert by_id['disc_1']['is_cli_session'] is False
    assert by_id['tele_1']['is_cli_session'] is False
    assert by_id['cli_1']['is_cli_session'] is True
    assert by_id['cron_job2_1717000001']['is_cli_session'] is False
