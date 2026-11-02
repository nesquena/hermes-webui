"""Sessions Hermes stores as ``source='local'`` keep their real surface (#8674).

Newer Hermes Agent writes every local interactive session (cli, tui, gui,
acp) with ``source='local'`` and the real surface in ``created_source``.
WebUI read only ``source``, so a TUI chat made after that change was
classified 'other' and filed under the WebUI sidebar tab instead of TUI.
"""

import sqlite3
import time
import unittest.mock

import api.models as models
from api.agent_sessions import read_importable_agent_session_rows


def _make_state_db(path, sessions, *, with_created_source=True):
    conn = sqlite3.connect(str(path))
    created_col = "created_source TEXT," if with_created_source else ""
    conn.executescript(
        f"""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            {created_col}
            session_source TEXT,
            title TEXT,
            model TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
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
        if with_created_source:
            conn.execute(
                "INSERT INTO sessions (id, source, created_source, title, model, started_at, message_count)"
                " VALUES (?, ?, ?, ?, 'm', ?, 2)",
                (sid, sess['source'], sess.get('created_source'), sess.get('title', sid), started),
            )
        else:
            conn.execute(
                "INSERT INTO sessions (id, source, title, model, started_at, message_count)"
                " VALUES (?, ?, ?, 'm', ?, 2)",
                (sid, sess['source'], sess.get('title', sid), started),
            )
        for j, role in enumerate(('user', 'user', 'assistant')):
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, ?, 'x', ?)",
                (f"m_{sid}_{j}", sid, role, started + j / 10),
            )
    conn.commit()
    conn.close()


def _sidebar(tmp_path, sessions, **kw):
    db = tmp_path / 'state.db'
    _make_state_db(db, sessions, **kw)
    with (
        unittest.mock.patch('api.models.get_claude_code_sessions', return_value=[]),
        unittest.mock.patch('api.models.ensure_cron_project', return_value='cron-project-id'),
    ):
        rows = models._load_cli_sessions_uncached(tmp_path, db, None)
    return {row['session_id']: row for row in rows}


def test_local_tui_session_files_under_tui_like_a_legacy_tui_row(tmp_path):
    rows = _sidebar(tmp_path, [
        {'id': 'legacy-tui', 'source': 'tui', 'title': 'Old TUI chat'},
        {'id': 'local-tui', 'source': 'local', 'created_source': 'tui', 'title': 'New TUI chat'},
    ])
    legacy, new = rows['legacy-tui'], rows['local-tui']
    for key in ('is_cli_session', 'session_source', 'source_label'):
        assert new[key] == legacy[key], key
    assert new['is_cli_session'] is True
    assert new['source_label'] == 'TUI'


def test_local_cli_and_acp_sessions_keep_their_surface(tmp_path):
    rows = _sidebar(tmp_path, [
        {'id': 'local-cli', 'source': 'local', 'created_source': 'cli', 'title': 'CLI chat'},
        {'id': 'local-acp', 'source': 'local', 'created_source': 'ACP', 'title': 'ACP chat'},
    ])
    assert rows['local-cli']['source_label'] == 'CLI'
    assert rows['local-acp']['source_label'] == 'ACP'
    assert rows['local-cli']['is_cli_session'] is True
    assert rows['local-acp']['is_cli_session'] is True


def test_local_without_created_source_and_old_schema_are_unchanged(tmp_path):
    rows = _sidebar(tmp_path, [
        {'id': 'local-unknown', 'source': 'local', 'created_source': None, 'title': 'Who made me'},
        {'id': 'local-gui', 'source': 'local', 'created_source': 'gui', 'title': 'Desktop chat'},
    ])
    assert rows['local-unknown']['session_source'] == 'other'
    # Desktop keeps today's behaviour; only CLI-family surfaces are mapped.
    assert rows['local-gui']['source_label'] == rows['local-unknown']['source_label']

    old = tmp_path / 'old'
    old.mkdir()
    rows = _sidebar(old, [{'id': 'plain-tui', 'source': 'tui', 'title': 'TUI chat'}],
                    with_created_source=False)
    assert rows['plain-tui']['source_label'] == 'TUI'


def test_source_filters_use_the_created_source(tmp_path):
    db = tmp_path / 'state.db'
    _make_state_db(db, [
        {'id': 'local-tui', 'source': 'local', 'created_source': 'tui'},
        {'id': 'local-cli', 'source': 'local', 'created_source': 'cli'},
        {'id': 'telegram', 'source': 'telegram'},
    ])
    tui_only = read_importable_agent_session_rows(db, include_sources=('tui',))
    assert [r['id'] for r in tui_only] == ['local-tui']
    excluded = read_importable_agent_session_rows(db, exclude_sources=('cron', 'webui', 'tui'))
    assert {r['id'] for r in excluded} == {'local-cli', 'telegram'}


def _lineage_db(path, rows):
    """state.db with project + compression-lineage columns.

    ``rows`` are (id, source, created_source, project_id, parent, started_at,
    ended_at, end_reason).
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, created_source TEXT,
            title TEXT, model TEXT, started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0, project_id TEXT,
            parent_session_id TEXT, ended_at REAL, end_reason TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, session_id TEXT, role TEXT,
            content TEXT, timestamp REAL
        );
        CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
        """
    )
    for sid, source, created, project, parent, started, ended, reason in rows:
        conn.execute(
            "INSERT INTO sessions (id, source, created_source, title, model, started_at,"
            " message_count, project_id, parent_session_id, ended_at, end_reason)"
            " VALUES (?, ?, ?, ?, 'm', ?, 2, ?, ?, ?, ?)",
            (sid, source, created, sid, started, project, parent, ended, reason),
        )
        for j, role in enumerate(('user', 'assistant')):
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, ?, 'x', ?)",
                (f"m_{sid}_{j}", sid, role, started + j / 10),
            )
    conn.commit()
    conn.close()


def _ids(db, **kw):
    return sorted(r['id'] for r in read_importable_agent_session_rows(db, exclude_sources=None, **kw))


import pytest  # noqa: E402


@pytest.mark.parametrize('surface', ['cli', 'tui', 'acp'])
@pytest.mark.parametrize('owner', ['root', 'tip'])
def test_project_filters_follow_a_local_continuation_like_the_listing(tmp_path, surface, owner):
    """A legacy-stored root continued by a 'local' row is one conversation in every project view."""
    db = tmp_path / 'state.db'
    root_project = 'p' if owner == 'root' else None
    tip_project = 'p' if owner == 'tip' else None
    _lineage_db(db, [
        ('root', surface, None, root_project, None, 100.0, 200.0, 'compression'),
        ('tip', 'local', surface, tip_project, 'root', 200.0, None, None),
    ])
    everything = _ids(db)
    assigned = _ids(db, project_assignment='assigned')
    narrowed = _ids(db, project_assignment='assigned', project_ids=('p',))
    other_project = _ids(db, project_assignment='assigned', project_ids=('q',))
    unassigned = _ids(db, project_assignment='unassigned')
    assert assigned == everything
    assert narrowed == everything
    assert other_project == []
    assert unassigned == []


def test_project_filters_still_split_a_real_cross_surface_continuation(tmp_path):
    """Control: a 'local' row made by another surface is not pulled into the lineage."""
    db = tmp_path / 'state.db'
    _lineage_db(db, [
        ('root', 'tui', None, 'p', None, 100.0, 200.0, 'compression'),
        ('tip', 'local', 'cli', None, 'root', 200.0, None, None),
    ])
    assert 'tip' not in _ids(db, project_assignment='assigned')
    assert 'tip' in _ids(db, project_assignment='unassigned')


def test_project_filters_unchanged_for_legacy_storage(tmp_path):
    """Control: plain-surface lineage (no 'local') behaves as before."""
    db = tmp_path / 'state.db'
    _lineage_db(db, [
        ('root', 'tui', None, 'p', None, 100.0, 200.0, 'compression'),
        ('tip', 'tui', None, None, 'root', 200.0, None, None),
    ])
    assert _ids(db, project_assignment='assigned') == _ids(db)
    assert _ids(db, project_assignment='unassigned') == []
