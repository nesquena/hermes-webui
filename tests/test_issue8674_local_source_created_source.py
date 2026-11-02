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


def _lineage_db(path, rows, model_configs=None):
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
            parent_session_id TEXT, ended_at REAL, end_reason TEXT,
            model_config TEXT
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
                "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
                (f"m_{sid}_{j}", sid, role, f"{sid}-{role}", started + j / 10),
            )
    for sid, config in (model_configs or {}).items():
        conn.execute("UPDATE sessions SET model_config = ? WHERE id = ?", (config, sid))
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



def _patch_active_home(monkeypatch, home):
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: None)


def _texts(messages):
    return [m.get('content') for m in messages]


@pytest.mark.parametrize('surface', ['cli', 'tui', 'acp'])
def test_opening_the_collapsed_tip_reads_the_root_transcript_too(tmp_path, monkeypatch, surface):
    """The sidebar shows root + 'local' continuation as one row; opening it must stitch both."""
    _patch_active_home(monkeypatch, tmp_path)
    db = tmp_path / 'state.db'
    _lineage_db(db, [
        ('root', surface, None, None, None, 100.0, 200.0, 'compression'),
        ('tip', 'local', surface, None, 'root', 200.0, None, None),
    ])
    rows = _sidebar_rows_only(tmp_path, db)
    assert 'root' not in rows and 'tip' in rows
    texts = _texts(models.get_cli_session_messages('tip'))
    assert texts[:2] == ['root-user', 'root-assistant']
    assert texts[2:] == ['tip-user', 'tip-assistant']


def test_transcript_stitch_still_stops_at_another_surface(tmp_path, monkeypatch):
    """Control: a 'local' row made by another surface does not pull in the root's messages."""
    _patch_active_home(monkeypatch, tmp_path)
    db = tmp_path / 'state.db'
    _lineage_db(db, [
        ('root', 'tui', None, None, None, 100.0, 200.0, 'compression'),
        ('tip', 'local', 'cli', None, 'root', 200.0, None, None),
    ])
    assert _texts(models.get_cli_session_messages('tip')) == ['tip-user', 'tip-assistant']


def _sidebar_rows_only(tmp_path, db):
    with (
        unittest.mock.patch('api.models.get_claude_code_sessions', return_value=[]),
        unittest.mock.patch('api.models.ensure_cron_project', return_value='cron-project-id'),
    ):
        rows = models._load_cli_sessions_uncached(tmp_path, db, None)
    return {row['session_id']: row for row in rows}


@pytest.mark.parametrize('child_source, child_created', [('local', 'tui'), ('tui', None)])
@pytest.mark.parametrize('marker', ['_branched_from', '_delegate_from', '_reset_from'])
def test_project_filters_stop_at_an_explicit_branch_of_the_root(tmp_path, child_source, child_created, marker):
    """A branch/delegate/reset of a project root stays its own, unassigned conversation."""
    db = tmp_path / 'state.db'
    _lineage_db(db, [
        ('root', 'tui', None, 'p', None, 100.0, 200.0, 'compression'),
        ('branch', child_source, child_created, None, 'root', 200.0, None, None),
    ], model_configs={'branch': '{"%s": "root"}' % marker})
    assert _ids(db, project_assignment='assigned') == ['root']
    assert _ids(db, project_assignment='assigned', project_ids=('p',)) == ['root']
    assert _ids(db, project_assignment='unassigned') == ['branch']


@pytest.mark.parametrize('config', [
    '{"_delegate_from": "older-ancestor"}',  # inherited marker naming someone else
    '{"_branched_from": null}',
    '{}',
    '',
    None,
])
def test_project_filters_keep_real_continuations_with_harmless_model_config(tmp_path, config):
    """Control: only a marker naming THIS parent is a boundary, as in the listing."""
    db = tmp_path / 'state.db'
    _lineage_db(db, [
        ('root', 'tui', None, 'p', None, 100.0, 200.0, 'compression'),
        ('tip', 'local', 'tui', None, 'root', 200.0, None, None),
    ], model_configs={'tip': config})
    assert _ids(db) == ['tip']  # one collapsed conversation
    assert _ids(db, project_assignment='assigned') == ['tip']
    assert _ids(db, project_assignment='unassigned') == []


@pytest.mark.parametrize('config', ['not json', '[1, 2]', '{"_branched_from": 7}', '{"_reset_from": " "}'])
def test_project_filters_fail_closed_on_unreadable_markers(tmp_path, config):
    """Unreadable lineage evidence is a boundary, matching _branch_markers()."""
    db = tmp_path / 'state.db'
    _lineage_db(db, [
        ('root', 'tui', None, 'p', None, 100.0, 200.0, 'compression'),
        ('tip', 'local', 'tui', None, 'root', 200.0, None, None),
    ], model_configs={'tip': config})
    assert _ids(db, project_assignment='assigned') == ['root']
    assert _ids(db, project_assignment='unassigned') == ['tip']
