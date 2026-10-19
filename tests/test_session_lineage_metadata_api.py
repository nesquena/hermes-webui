"""Regression tests for /api/sessions lineage metadata used by sidebar collapse."""

import json
import sqlite3
import sys
import time
from contextlib import closing

import pytest

import api.agent_sessions as agent_sessions
import api.models as models
import api.routes as routes
from api.models import SESSIONS, STREAMS, Session, all_sessions
from tests.test_session_lineage_collapse import render_sidebar_rows


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    state_db = tmp_path / "state.db"
    index_file.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: state_db)
    monkeypatch.setattr(models, "_start_session_index_rebuild_thread", lambda: None)

    def uncached_persisted_session_ids():
        return frozenset(
            p.stem
            for p in models.SESSION_DIR.glob("*.json")
            if not p.name.startswith("_")
        )

    monkeypatch.setattr(models, "_persisted_session_ids_snapshot", uncached_persisted_session_ids)
    SESSIONS.clear()
    STREAMS.clear()
    yield state_db
    SESSIONS.clear()
    STREAMS.clear()


def _ensure_state_db(path):
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
            session_key TEXT,
            model_config TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        """
    )
    return conn


def _ensure_messages_table(conn):
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        )
        """
    )


def _insert_state_message(conn, sid, *, role, content, timestamp):
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (sid, role, content, timestamp),
    )
    conn.commit()


def _insert_state_row(
    conn,
    sid,
    *,
    title=None,
    parent=None,
    ended_at=None,
    end_reason=None,
    started_at=None,
    source='webui',
    session_source=None,
    session_key=None,
    model_config=None,
):
    conn.execute(
        """
        INSERT INTO sessions
        (id, source, session_source, title, model, started_at, message_count, parent_session_id, session_key, model_config, ended_at, end_reason)
        VALUES (?, ?, ?, ?, 'openai/gpt-5', ?, 2, ?, ?, ?, ?, ?)
        """,
        (
            sid,
            source,
            session_source,
            title or sid,
            started_at or time.time(),
            parent,
            session_key,
            json.dumps(model_config) if isinstance(model_config, dict) else model_config,
            ended_at,
            end_reason,
        ),
    )
    conn.commit()


def _save_webui_session(sid, *, title, updated_at):
    session = Session(
        session_id=sid,
        title=title,
        messages=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
        updated_at=updated_at,
    )
    session.save(touch_updated_at=False)
    return session


def test_all_sessions_exposes_state_db_lineage_metadata_for_webui_json_sessions(_isolate):
    """PR #1358 can only collapse rows when /api/sessions exposes lineage keys."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_root", title="Hermes WebUI", updated_at=t0)
        _save_webui_session("lineage_api_tip", title="Hermes WebUI #2", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_api_root",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_row(
            conn,
            "lineage_api_tip",
            parent="lineage_api_root",
            started_at=t0 + 6,
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        assert rows["lineage_api_tip"].get("parent_session_id") == "lineage_api_root"
        assert rows["lineage_api_tip"].get("_lineage_root_id") == "lineage_api_root"
        assert rows["lineage_api_tip"].get("_compression_segment_count") == 2
        assert "_lineage_root_id" not in rows["lineage_api_root"]
    finally:
        conn.close()


def test_all_sessions_keeps_explicit_forks_out_of_state_db_lineage_metadata(_isolate):
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_root", title="Visible root", updated_at=t0)
        _save_webui_session("lineage_api_fork", title="Explicit fork", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_api_root",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_row(
            conn,
            "lineage_api_fork",
            parent="lineage_api_root",
            started_at=t0 + 6,
            session_source="fork",
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        fork = rows["lineage_api_fork"]
        assert fork.get("parent_session_id") == "lineage_api_root"
        assert fork.get("relationship_type") == "child_session"
        assert fork.get("parent_title") == "lineage_api_root"
        assert fork.get("_parent_lineage_root_id") == "lineage_api_root"
        assert "_lineage_root_id" not in fork
        assert "_compression_segment_count" not in fork
    finally:
        conn.close()


def test_non_compression_state_db_parent_does_not_create_sidebar_lineage(_isolate):
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_plain_parent", title="Parent", updated_at=t0)
        _save_webui_session("lineage_api_plain_child", title="Child", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_api_plain_parent",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="user_stop",
        )
        _insert_state_row(
            conn,
            "lineage_api_plain_child",
            parent="lineage_api_plain_parent",
            started_at=t0 + 6,
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        # Non-continuation parents should remain visible child-session links,
        # not compression lineage. The frontend must nest them under the parent
        # without collapsing sibling child sessions into one lineage row.
        child = rows["lineage_api_plain_child"]
        assert child.get("parent_session_id") == "lineage_api_plain_parent"
        assert child.get("relationship_type") == "child_session"
        assert child.get("parent_title") == "lineage_api_plain_parent"
        assert child.get("_parent_lineage_root_id") == "lineage_api_plain_parent"
        assert "_lineage_root_id" not in child
    finally:
        conn.close()


def test_all_sessions_keeps_reset_successor_top_level_with_durable_lineage(_isolate):
    """JSON-enriched rows retain reset lineage without sidebar child metadata."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    parent_sid = 'lineage_api_reset_parent'
    child_sid = 'lineage_api_reset_child'
    try:
        _save_webui_session(parent_sid, title='Previous WeCom conversation', updated_at=t0)
        _save_webui_session(child_sid, title='New WeCom conversation', updated_at=t0 + 10)
        _insert_state_row(
            conn,
            parent_sid,
            source='wecom',
            started_at=t0,
            ended_at=t0 + 5,
            end_reason='session_reset',
            session_key='same-messaging-identity',
        )
        _insert_state_row(
            conn,
            child_sid,
            source='wecom',
            parent=parent_sid,
            started_at=t0 + 6,
            session_key='same-messaging-identity',
            model_config={'_reset_from': parent_sid},
        )

        child = {row['session_id']: row for row in all_sessions()}[child_sid]

        assert child.get('parent_session_id') == parent_sid
        assert child.get('relationship_type') == 'reset_successor'
        assert child.get('_lineage_root_id') == child_sid
        for key in (
            'parent_title',
            'parent_source',
            '_parent_lineage_root_id',
            '_parent_lineage_tip_id',
        ):
            assert key not in child
    finally:
        conn.close()


def test_reset_projection_fails_closed_for_conflicting_or_invalid_metadata():
    """Legacy reset fallback stays narrow; real branch/delegate rows stay children."""
    parent = {
        'id': 'projection_reset_parent',
        'title': 'Parent',
        'source': 'wecom',
        'session_key': 'same-messaging-identity',
        'end_reason': 'session_reset',
        'actual_message_count': 2,
        'started_at': 1,
        'ended_at': 2,
    }
    switch_parent = {
        **parent,
        'id': 'projection_switch_parent',
        'end_reason': 'session_switch',
    }
    base_child = {
        'source': 'wecom',
        'parent_session_id': parent['id'],
        'session_key': 'same-messaging-identity',
        'actual_message_count': 2,
        'started_at': 2,
    }
    switch_child = {
        **base_child,
        'id': 'projection_session_switch_reset',
        'parent_session_id': switch_parent['id'],
    }
    rows = [
        parent,
        switch_parent,
        {**base_child, 'id': 'projection_legacy_reset'},
        switch_child,
        {
            **base_child,
            'id': 'projection_delegate',
            'model_config': {'_reset_from': parent['id'], '_delegate_from': parent['id']},
        },
        {
            **base_child,
            'id': 'projection_mismatched_reset',
            'model_config': {'_reset_from': 'other-parent'},
        },
        {
            **base_child,
            'id': 'projection_invalid_config',
            'model_config': '{not-json',
        },
        {
            **base_child,
            'id': 'projection_orphan_canonical_reset',
            'parent_session_id': 'parent-outside-window',
            'model_config': {'_reset_from': 'parent-outside-window'},
        },
    ]

    projected = {row['id']: row for row in agent_sessions._project_agent_session_rows(rows)}

    for sid in (
        'projection_legacy_reset',
        'projection_session_switch_reset',
        'projection_orphan_canonical_reset',
    ):
        assert projected[sid].get('relationship_type') == 'reset_successor'
        assert projected[sid].get('_lineage_root_id') == sid
        for key in (
            'parent_title',
            'parent_source',
            '_parent_lineage_root_id',
            '_parent_lineage_tip_id',
        ):
            assert key not in projected[sid]
    for sid in (
        'projection_delegate',
        'projection_mismatched_reset',
        'projection_invalid_config',
    ):
        assert projected[sid].get('relationship_type') == 'child_session'
        assert '_lineage_root_id' not in projected[sid]



def test_deep_model_config_does_not_hide_other_agent_sessions(_isolate, monkeypatch):
    """A single corrupt row must not abort the additive CLI bridge (#7179)."""
    with closing(_ensure_state_db(_isolate)) as conn:
        _ensure_messages_table(conn)
        for sid, parent, config in (
            ('parent', None, None),
            ('bad', 'parent', '[' * (sys.getrecursionlimit() + 100) + '0'
             + ']' * (sys.getrecursionlimit() + 100)),
            ('healthy', None, None),
        ):
            _insert_state_row(conn, sid, source='cli', parent=parent, model_config=config)
            _insert_state_message(conn, sid, role='user', content='hello', timestamp=time.time())
    monkeypatch.setattr(
        models, '_resolve_cli_sessions_context',
        lambda *args, **kwargs: (_isolate.parent, _isolate, 'default', ('deep-config', str(_isolate))),
    )
    rows = {row['session_id']: row for row in models.get_cli_sessions(include_claude_code=False)}
    assert set(rows) == {'parent', 'bad', 'healthy'}
    assert rows['bad']['relationship_type'] == 'child_session'
    assert all('model_config' not in row for row in rows.values())


@pytest.mark.parametrize('end_reason', sorted(agent_sessions._RESET_END_REASONS))
def test_legacy_reset_time_order_preserves_branch_in_all_projections(_isolate, end_reason):
    """Agent /branch creates its child before switch_session closes the parent."""
    with closing(_ensure_state_db(_isolate)) as conn:
        _ensure_messages_table(conn)
        _insert_state_row(conn, 'parent', started_at=1, ended_at=100,
                          end_reason=end_reason, session_key='same-key')
        for sid, started_at in (('branch', 50), ('reset', 150), ('boundary-reset', 100)):
            _insert_state_row(conn, sid, parent='parent', started_at=started_at, session_key='same-key')
        for sid in ('parent', 'branch', 'reset', 'boundary-reset'):
            _insert_state_message(conn, sid, role='user', content='hello', timestamp=200)
            _save_webui_session(sid, title=sid, updated_at=200)
    projected = {row['id']: row for row in agent_sessions.read_importable_agent_session_rows(_isolate, exclude_sources=None)}
    enriched = {row['session_id']: row for row in all_sessions()}
    for rows in (projected, enriched):
        assert rows['branch']['relationship_type'] == 'child_session'
        assert rows['branch']['parent_session_id'] == 'parent'
        for sid in ('reset', 'boundary-reset'):
            assert rows[sid]['relationship_type'] == 'reset_successor'
            assert rows[sid]['_lineage_root_id'] == sid
            assert 'parent_title' not in rows[sid]
    report = agent_sessions.read_session_lineage_report(_isolate, 'parent')
    assert [row['session_id'] for row in report['children']] == ['branch']


@pytest.mark.parametrize('field', ['started_at', 'ended_at'])
@pytest.mark.parametrize('invalid', [None, '', 'invalid', 'NaN', 'Infinity', '-Infinity', True])
def test_legacy_reset_rejects_unknown_time_boundary(field, invalid):
    parent = {'id': 'parent', 'session_key': 'key', 'end_reason': 'session_switch', 'ended_at': 100}
    child = {'parent_session_id': 'parent', 'session_key': 'key', 'started_at': 150}
    (parent if field == 'ended_at' else child)[field] = invalid
    assert not agent_sessions._is_user_visible_reset_successor(parent, child)
    # Canonical reset intent does not depend on legacy timestamp inference.
    child['model_config'] = {'_reset_from': 'parent'}
    assert agent_sessions._is_user_visible_reset_successor(parent, child)


@pytest.mark.parametrize('marker', ['_branched_from', '_delegate_from'])
@pytest.mark.parametrize('value', [None, '', False, 0])
@pytest.mark.parametrize('canonical', [False, True])
def test_explicit_branch_marker_presence_prevents_reset(marker, value, canonical):
    parent = {'id': 'parent', 'session_key': 'key', 'end_reason': 'session_switch', 'ended_at': 100}
    config = {marker: value}
    if canonical:
        config['_reset_from'] = 'parent'
    child = {'parent_session_id': 'parent', 'session_key': 'key', 'started_at': 150,
             'model_config': config}
    assert not agent_sessions._is_user_visible_reset_successor(parent, child)


def test_compression_walks_stop_at_canonical_reset_boundary(_isolate):
    """Even inconsistent reset/compression metadata must not alias or duplicate rows."""
    with closing(_ensure_state_db(_isolate)) as conn:
        _ensure_messages_table(conn)
        _insert_state_row(conn, 'parent', started_at=1, ended_at=10, end_reason='compression')
        _insert_state_row(conn, 'reset', parent='parent', started_at=11, ended_at=20,
                          end_reason='compression', model_config={'_reset_from': 'parent'})
        _insert_state_row(conn, 'tip', parent='reset', started_at=21)
        for sid, timestamp in (('parent', 2), ('reset', 12), ('tip', 22)):
            _insert_state_message(conn, sid, role='user', content='hello', timestamp=timestamp)
    rows = agent_sessions.read_importable_agent_session_rows(_isolate, exclude_sources=None)
    assert sorted(row['id'] for row in rows) == ['parent', 'tip']
    tip = next(row for row in rows if row['id'] == 'tip')
    assert tip['_lineage_root_id'] == 'reset'
    assert tip['relationship_type'] == 'reset_successor'
    metadata = agent_sessions.read_session_lineage_metadata(_isolate, {'parent', 'reset', 'tip'})
    assert metadata['reset']['_lineage_root_id'] == 'reset'
    assert metadata['tip']['_lineage_root_id'] == 'reset'
    assert metadata['tip']['_compression_segment_count'] == 2
    report = agent_sessions.read_session_lineage_report(_isolate, 'tip')
    assert report['lineage_key'] == 'reset'
    assert [row['session_id'] for row in report['segments']] == ['tip', 'reset']


def test_child_of_hidden_compression_segment_exposes_parent_lineage_root(_isolate):
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_root", title="Visible root", updated_at=t0)
        _save_webui_session("lineage_api_tip", title="Visible tip", updated_at=t0 + 10)
        _save_webui_session("lineage_api_subtask", title="Subtask", updated_at=t0 + 20)
        _insert_state_row(
            conn,
            "lineage_api_root",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_row(
            conn,
            "lineage_api_tip",
            parent="lineage_api_root",
            started_at=t0 + 6,
            ended_at=t0 + 15,
            end_reason="user_stop",
        )
        _insert_state_row(
            conn,
            "lineage_api_subtask",
            parent="lineage_api_tip",
            started_at=t0 + 12,
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        child = rows["lineage_api_subtask"]
        assert child.get("relationship_type") == "child_session"
        assert child.get("parent_session_id") == "lineage_api_tip"
        assert child.get("_parent_lineage_root_id") == "lineage_api_root"
        assert child.get("_parent_lineage_tip_id") == "lineage_api_tip"
        serialized = routes._sidebar_session_response_item(child, redact_enabled=False)
        assert serialized.get("_parent_lineage_tip_id") == "lineage_api_tip"
        assert "_lineage_root_id" not in child
    finally:
        conn.close()



def test_cli_close_parent_preserves_cross_surface_continuation_lineage(_isolate):
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_cli_parent", title="Hermes WebUI #8", updated_at=t0)
        _save_webui_session("lineage_api_webui_child", title="Hermes WebUI #8", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_api_cli_parent",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="cli_close",
        )
        _insert_state_row(
            conn,
            "lineage_api_webui_child",
            parent="lineage_api_cli_parent",
            started_at=t0 + 6,
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        assert rows["lineage_api_webui_child"].get("parent_session_id") == "lineage_api_cli_parent"
        assert rows["lineage_api_webui_child"].get("_lineage_root_id") == "lineage_api_cli_parent"
    finally:
        conn.close()


def test_cross_surface_child_session_metadata_marks_orphan_top_level_candidate(_isolate):
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_telegram_parent", title="Telegram parent", updated_at=t0)
        _save_webui_session("lineage_api_webui_tip", title="WebUI tip", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_api_telegram_parent",
            source="telegram",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_row(
            conn,
            "lineage_api_webui_tip",
            source="webui",
            parent="lineage_api_telegram_parent",
            started_at=t0 + 6,
        )

        rows = {row["session_id"]: row for row in all_sessions()}
        tip = rows["lineage_api_webui_tip"]

        assert tip.get("relationship_type") == "child_session"
        assert tip.get("parent_source") == "telegram"
        assert tip.get("_cross_surface_child_session") is True
    finally:
        conn.close()


def test_state_db_webui_source_overrides_stale_cli_json_metadata(_isolate):
    """State-db WebUI mirrors should clear stale CLI source fields in sidebar rows."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        session = Session(
            session_id="lineage_api_stale_cli_source",
            title="WebUI Chatnachrichten verschwinden nach Neustart #9",
            messages=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
            updated_at=t0,
            is_cli_session=True,
            source_tag="cli",
            raw_source="cli",
            session_source="cli",
            source_label="CLI",
        )
        session.save(touch_updated_at=False)
        _insert_state_row(
            conn,
            "lineage_api_stale_cli_source",
            source="webui",
            started_at=t0,
        )

        row = {row["session_id"]: row for row in all_sessions()}["lineage_api_stale_cli_source"]

        assert row["source_tag"] == "webui"
        assert row["raw_source"] == "webui"
        assert row["session_source"] == "webui"
        assert row["source_label"] == "WebUI"
        assert row["is_cli_session"] is False
    finally:
        conn.close()


def test_state_db_webui_source_normalizes_projection_without_rewriting_fork(_isolate):
    """Used branches remain independent; durable sidecar provenance is unchanged."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        session = Session(
            session_id='lineage_api_native_fork_source',
            title='Forked conversation',
            messages=[{'role': 'user', 'content': 'hello'}, {'role': 'assistant', 'content': 'hi'}],
            updated_at=t0,
            parent_session_id='lineage_api_fork_parent',
            session_source='fork',
        )
        session.save(touch_updated_at=False)
        _insert_state_row(
            conn,
            'lineage_api_native_fork_source',
            source='webui',
            started_at=t0,
        )

        row = {row['session_id']: row for row in all_sessions()}['lineage_api_native_fork_source']

        assert row['session_source'] == 'webui'
        assert row['parent_session_id'] == 'lineage_api_fork_parent'
        saved = json.loads((models.SESSION_DIR / f'{session.session_id}.json').read_text())
        assert saved['session_source'] == 'fork'
        assert saved['parent_session_id'] == 'lineage_api_fork_parent'
        assert row['source_tag'] == 'webui'
        assert row['raw_source'] == 'webui'
        assert row['source_label'] == 'WebUI'
        assert row['is_cli_session'] is False
    finally:
        conn.close()


def test_sessions_route_keeps_state_db_webui_row_with_stale_cli_json_when_cli_hidden(_isolate, monkeypatch):
    """The hot route must apply state.db source correction before CLI filtering."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        session = Session(
            session_id="lineage_api_route_stale_cli_source",
            title="WebUI Chatnachrichten verschwinden nach Neustart #9",
            messages=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
            updated_at=t0,
            is_cli_session=True,
            source_tag="cli",
            raw_source="cli",
            session_source="cli",
            source_label="CLI",
        )
        session.save(touch_updated_at=False)
        _insert_state_row(
            conn,
            "lineage_api_route_stale_cli_source",
            source="webui",
            started_at=t0,
        )

        monkeypatch.setattr(routes, "all_sessions", models.all_sessions)
        monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", models._enrich_sidebar_lineage_metadata)
        monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _sessions: False)

        payload = routes._build_session_list_cache_payload(
            active_profile="default",
            all_profiles=False,
            show_cli_sessions=False,
            show_previous_messaging_sessions=False,
            show_cron_sessions=False,
            include_archived=False,
        )

        rows = {row["session_id"]: row for row in payload["sessions"]}
        row = rows["lineage_api_route_stale_cli_source"]
        assert row["source_tag"] == "webui"
        assert row["raw_source"] == "webui"
        assert row["session_source"] == "webui"
        assert row["source_label"] == "WebUI"
        assert row["is_cli_session"] is False
    finally:
        conn.close()


def test_generic_webui_title_gets_read_only_state_db_display_title(_isolate):
    """Sidebar rows can display the fresher state.db title without mutating JSON."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_stale_title", title="Hermes WebUI #8", updated_at=t0)
        _insert_state_row(
            conn,
            "lineage_api_stale_title",
            title="Hermes WebUI #177",
            started_at=t0,
        )

        row = {row["session_id"]: row for row in all_sessions()}["lineage_api_stale_title"]

        assert row["title"] == "Hermes WebUI #8"
        assert row["display_title"] == "Hermes WebUI #177"
        assert row["_state_db_title"] == "Hermes WebUI #177"
    finally:
        conn.close()


def test_generic_subagent_title_gets_goal_display_title(_isolate):
    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_subagent_goal", title="Subagent Session", updated_at=t0)
        _insert_state_row(
            conn,
            "lineage_api_subagent_goal",
            title="Subagent Session",
            source="subagent",
            started_at=t0,
        )
        _insert_state_message(
            conn,
            "lineage_api_subagent_goal",
            role="user",
            content="Find the root cause of the failing sidebar test",
            timestamp=t0 + 1,
        )

        row = {row["session_id"]: row for row in all_sessions(include_lineage_metadata=False)}["lineage_api_subagent_goal"]

        assert row["title"] == "Subagent Session"
        assert row["display_title"] == "Find the root cause of the failing sidebar test"
    finally:
        conn.close()


def test_custom_subagent_title_stays_authoritative(_isolate):
    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_subagent_custom", title="Investigate auth", updated_at=t0)
        _insert_state_row(
            conn,
            "lineage_api_subagent_custom",
            title="Investigate auth",
            source="subagent",
            started_at=t0,
        )
        _insert_state_message(
            conn,
            "lineage_api_subagent_custom",
            role="user",
            content="A different goal",
            timestamp=t0 + 1,
        )

        row = {row["session_id"]: row for row in all_sessions(include_lineage_metadata=False)}["lineage_api_subagent_custom"]

        assert row["title"] == "Investigate auth"
        assert "display_title" not in row
    finally:
        conn.close()


def test_generic_subagent_title_falls_back_without_first_user_message(_isolate):
    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_subagent_empty", title="Subagent Session", updated_at=t0)
        _insert_state_row(
            conn,
            "lineage_api_subagent_empty",
            title="Subagent Session",
            source="subagent",
            started_at=t0,
        )
        _insert_state_message(
            conn,
            "lineage_api_subagent_empty",
            role="assistant",
            content="Only assistant output",
            timestamp=t0 + 1,
        )

        row = {row["session_id"]: row for row in all_sessions(include_lineage_metadata=False)}["lineage_api_subagent_empty"]

        assert row["title"] == "Subagent Session"
        assert "display_title" not in row
    finally:
        conn.close()


def test_generic_subagent_title_skips_null_first_user_message(_isolate):
    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_subagent_null_first", title="Subagent Session", updated_at=t0)
        _insert_state_row(
            conn,
            "lineage_api_subagent_null_first",
            title="Subagent Session",
            source="subagent",
            started_at=t0,
        )
        _insert_state_message(
            conn,
            "lineage_api_subagent_null_first",
            role="user",
            content=None,
            timestamp=t0 + 1,
        )
        _insert_state_message(
            conn,
            "lineage_api_subagent_null_first",
            role="user",
            content="Recover the next usable delegated title",
            timestamp=t0 + 2,
        )

        row = {row["session_id"]: row for row in all_sessions(include_lineage_metadata=False)}["lineage_api_subagent_null_first"]

        assert row["title"] == "Subagent Session"
        assert row["display_title"] == "Recover the next usable delegated title"
    finally:
        conn.close()


def test_generic_subagent_title_respects_sidebar_override_cap(_isolate, monkeypatch):
    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    older = time.time() - 200
    newer = time.time() - 100
    try:
        monkeypatch.setenv("HERMES_WEBUI_STATE_DB_OVERRIDE_TOP_N", "1")
        _save_webui_session("lineage_api_subagent_old", title="Subagent Session", updated_at=older)
        _save_webui_session("lineage_api_subagent_new", title="Subagent Session", updated_at=newer)
        _insert_state_row(
            conn,
            "lineage_api_subagent_old",
            title="Subagent Session",
            source="subagent",
            started_at=older,
        )
        _insert_state_row(
            conn,
            "lineage_api_subagent_new",
            title="Subagent Session",
            source="subagent",
            started_at=newer,
        )
        _insert_state_message(
            conn,
            "lineage_api_subagent_old",
            role="user",
            content="Older delegated title",
            timestamp=older + 1,
        )
        _insert_state_message(
            conn,
            "lineage_api_subagent_new",
            role="user",
            content="Newest delegated title",
            timestamp=newer + 1,
        )

        rows = {row["session_id"]: row for row in all_sessions(include_lineage_metadata=False)}

        assert rows["lineage_api_subagent_new"]["display_title"] == "Newest delegated title"
        assert "display_title" not in rows["lineage_api_subagent_old"]
    finally:
        conn.close()
def test_generic_subagent_title_falls_back_without_messages_table(_isolate):
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_subagent_no_messages", title="Subagent Session", updated_at=t0)
        _insert_state_row(
            conn,
            "lineage_api_subagent_no_messages",
            title="Subagent Session",
            source="subagent",
            started_at=t0,
        )

        row = {row["session_id"]: row for row in all_sessions(include_lineage_metadata=False)}["lineage_api_subagent_no_messages"]

        assert row["title"] == "Subagent Session"
        assert "display_title" not in row
    finally:
        conn.close()


def test_state_db_display_title_does_not_override_custom_json_title(_isolate):
    """Manual/custom JSON titles stay authoritative even when state.db differs."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_api_custom_title", title="Customer escalation notes", updated_at=t0)
        _insert_state_row(
            conn,
            "lineage_api_custom_title",
            title="Hermes WebUI #177",
            started_at=t0,
        )

        row = {row["session_id"]: row for row in all_sessions()}["lineage_api_custom_title"]

        assert row["title"] == "Customer escalation notes"
        assert "display_title" not in row
        assert "_state_db_title" not in row
    finally:
        conn.close()


@pytest.mark.parametrize('session_source', ['webui', 'fork'])
def test_sessions_route_preserves_visible_child_lineage_when_archived_parent_filtered(
    _isolate, monkeypatch, session_source,
):
    """Default /api/sessions omits archived rows but keeps their lineage metadata.

    The route builds the hot sidebar payload with archived rows filtered out by
    default. A visible continuation child still needs lineage metadata from its
    archived parent so the client can collapse/display the logical conversation
    correctly.
    """
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        archived_parent = _save_webui_session(
            "lineage_api_archived_parent",
            title="Hermes WebUI",
            updated_at=t0,
        )
        archived_parent.archived = True
        archived_parent.pre_compression_snapshot = session_source == 'fork'
        archived_parent.session_source = session_source
        archived_parent.parent_session_id = 'origin-outside-sidebar' if session_source == 'fork' else None
        archived_parent.save(touch_updated_at=False)
        live_tip = _save_webui_session(
            "lineage_api_visible_tip",
            title="Hermes WebUI #2",
            updated_at=t0 + 10,
        )
        # Compression keeps the Session object's source but rewrites its parent
        # to the archived snapshot, including on a fork of an out-of-scope row.
        live_tip.session_source = session_source
        live_tip.parent_session_id = archived_parent.session_id
        live_tip.save(touch_updated_at=False)
        _insert_state_row(
            conn,
            "lineage_api_archived_parent",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_row(
            conn,
            "lineage_api_visible_tip",
            parent="lineage_api_archived_parent",
            started_at=t0 + 6,
        )

        monkeypatch.setattr(routes, "all_sessions", models.all_sessions)
        monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", models._enrich_sidebar_lineage_metadata)
        monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _sessions: False)

        default_payload = routes._build_session_list_cache_payload(
            active_profile="default",
            all_profiles=False,
            show_cli_sessions=False,
            show_previous_messaging_sessions=False,
            show_cron_sessions=False,
            include_archived=False,
        )

        assert [row["session_id"] for row in default_payload["sessions"]] == ["lineage_api_visible_tip"]
        assert default_payload["archived_count"] == (0 if session_source == "fork" else 1)
        tip = default_payload["sessions"][0]
        assert tip.get("parent_session_id") == "lineage_api_archived_parent"
        assert tip.get("_lineage_root_id") == "lineage_api_archived_parent"
        assert tip.get("_compression_segment_count") == 2
        # Reconciliation only changes the response; persisted fork history stays.
        assert json.loads((models.SESSION_DIR / f'{live_tip.session_id}.json').read_text())['session_source'] == session_source

        archived_payload = routes._build_session_list_cache_payload(
            active_profile="default",
            all_profiles=False,
            show_cli_sessions=False,
            show_previous_messaging_sessions=False,
            show_cron_sessions=False,
            include_archived=True,
        )
        expected_ids = ["lineage_api_visible_tip"]
        if session_source != "fork":
            expected_ids.append("lineage_api_archived_parent")
        # A normalized compression snapshot follows the existing hidden-snapshot
        # rule; an ordinary archived parent remains available in the archive list.
        assert [row["session_id"] for row in archived_payload["sessions"]] == expected_ids
        visible_rows = render_sidebar_rows(default_payload['sessions'], archived_payload['sessions'])
        assert [row['session_id'] for row in visible_rows] == ['lineage_api_visible_tip']
        assert visible_rows[0]['_lineage_root_id'] == 'lineage_api_archived_parent'
        assert tip['session_source'] == 'webui'
    finally:
        conn.close()


def test_compression_continuation_tolerates_sub_second_timestamp_overlap(_isolate):
    """#6931: a compression continuation recorded a few ms BEFORE the parent's
    ended_at (write-order race) is still one lineage in /api/sessions."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_race_root", title="Shared conversation", updated_at=t0)
        _save_webui_session("lineage_race_tip", title="Shared conversation", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_race_root",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        # child.started_at lands 0.1s BEFORE parent.ended_at — the #6931 race.
        _insert_state_row(
            conn,
            "lineage_race_tip",
            parent="lineage_race_root",
            started_at=t0 + 5 - 0.1,
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        tip = rows["lineage_race_tip"]
        assert tip.get("parent_session_id") == "lineage_race_root"
        assert tip.get("_lineage_root_id") == "lineage_race_root"
        assert tip.get("_lineage_tip_id") == "lineage_race_tip"
        assert tip.get("_compression_segment_count") == 2
        assert tip.get("relationship_type") != "child_session"
    finally:
        conn.close()


def test_materially_overlapping_compression_child_stays_child_session(_isolate):
    """#6931: a child that started long before the compression parent ended
    (beyond tolerance, no matching title) must remain a separate child."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_overlap_root", title="Root title", updated_at=t0)
        _save_webui_session("lineage_overlap_child", title="Child title", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_overlap_root",
            started_at=t0,
            ended_at=t0 + 60,
            end_reason="compression",
        )
        # child started 30s before the parent ended — a genuine concurrent child.
        _insert_state_row(
            conn,
            "lineage_overlap_child",
            parent="lineage_overlap_root",
            started_at=t0 + 30,
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        child = rows["lineage_overlap_child"]
        assert child.get("relationship_type") == "child_session"
        assert child.get("parent_session_id") == "lineage_overlap_root"
        assert "_lineage_root_id" not in child
        assert "_compression_segment_count" not in child
    finally:
        conn.close()


def test_same_title_independent_child_outside_tolerance_stays_visible(_isolate):
    """#7021 re-gate: a same-title independent child started well outside the
    tolerance window must NOT collapse on the title match — titles are
    user-controlled and non-unique, so the only continuation evidence is the
    bounded early-side timestamp window."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_title_root", title="Shared conversation", updated_at=t0)
        _save_webui_session("lineage_title_tip", title="Shared conversation", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_title_root",
            title="Shared conversation",
            started_at=t0,
            ended_at=t0 + 60,
            end_reason="compression",
        )
        # Same title, but started 50s before the parent ended — far outside any
        # handoff race window. Must remain a visible child session.
        _insert_state_row(
            conn,
            "lineage_title_tip",
            title="Shared conversation",
            parent="lineage_title_root",
            started_at=t0 + 10,
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        tip = rows["lineage_title_tip"]
        assert tip.get("relationship_type") == "child_session"
        assert tip.get("parent_session_id") == "lineage_title_root"
        assert "_lineage_root_id" not in tip
        assert "_compression_segment_count" not in tip
    finally:
        conn.close()


def test_model_config_branched_from_child_stays_visible_within_tolerance(_isolate):
    """#7021 re-gate: an explicit Agent branch is marked in
    model_config._branched_from (not session_source). Even when it starts
    inside the 2s tolerance window of the parent's compression, the real
    marker must keep it visible instead of collapsing it into the lineage."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_branch_root", title="Shared conversation", updated_at=t0)
        _save_webui_session("lineage_branch_tip", title="Shared conversation", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_branch_root",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        # The reviewer's adversarial probe: an Agent branch starting 1.5s
        # BEFORE the parent's compression ended_at. No session_source — the
        # fork identity lives in model_config._branched_from.
        _insert_state_row(
            conn,
            "lineage_branch_tip",
            parent="lineage_branch_root",
            started_at=t0 + 5 - 1.5,
            model_config=json.dumps({"_branched_from": "lineage_branch_root"}),
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        tip = rows["lineage_branch_tip"]
        assert tip.get("relationship_type") == "child_session"
        assert tip.get("parent_session_id") == "lineage_branch_root"
        assert "_lineage_root_id" not in tip
        assert "_compression_segment_count" not in tip
    finally:
        conn.close()


def test_model_config_delegate_from_child_stays_visible_within_tolerance(_isolate):
    """#7021 re-gate: delegate/subagent runs are marked in
    model_config._delegate_from. A delegate child starting inside the
    tolerance window of the parent's compression must stay visible."""
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 100
    try:
        _save_webui_session("lineage_delegate_root", title="Shared conversation", updated_at=t0)
        _save_webui_session("lineage_delegate_tip", title="Shared conversation", updated_at=t0 + 10)
        _insert_state_row(
            conn,
            "lineage_delegate_root",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_row(
            conn,
            "lineage_delegate_tip",
            parent="lineage_delegate_root",
            started_at=t0 + 5 - 1.0,
            model_config=json.dumps({"_delegate_from": "lineage_delegate_root"}),
        )

        rows = {row["session_id"]: row for row in all_sessions()}

        tip = rows["lineage_delegate_tip"]
        assert tip.get("relationship_type") == "child_session"
        assert tip.get("parent_session_id") == "lineage_delegate_root"
        assert "_lineage_root_id" not in tip
        assert "_compression_segment_count" not in tip
    finally:
        conn.close()


def test_continuation_classification_timestamp_tolerance_and_guards():
    """#6931/#7021 focused unit coverage of _is_continuation_session: bounded
    tolerance, model_config branch markers, and preserved fork/cross-source/
    end_reason guards."""
    from api.agent_sessions import _is_continuation_session

    def make_parent(**over):
        row = {
            'id': 'parent-1',
            'source': 'webui',
            'end_reason': 'compression',
            'ended_at': 1000.0,
            'title': 'Shared conversation',
        }
        row.update(over)
        return row

    def make_child(**over):
        row = {
            'id': 'child-1',
            'source': 'webui',
            'started_at': 1000.05,
            'title': 'Shared conversation',
        }
        row.update(over)
        return row

    # Normal non-overlapping ordering: continuation.
    assert _is_continuation_session(make_parent(), make_child())
    # Sub-second overlap (observed -0.06..-0.07s in #6931): continuation.
    assert _is_continuation_session(make_parent(), make_child(started_at=999.95))
    # Overlap inside the 2s tolerance: continuation.
    assert _is_continuation_session(make_parent(), make_child(started_at=998.5))
    # Overlap beyond tolerance: separate child, even with an exact title match
    # (titles are user-controlled and non-unique — no title fallback).
    assert not _is_continuation_session(make_parent(), make_child(started_at=950.0))
    assert not _is_continuation_session(
        make_parent(), make_child(started_at=950.0, title='Another conversation')
    )
    # model_config._branched_from pointing at the parent: never a
    # continuation, regardless of timing.
    assert not _is_continuation_session(
        make_parent(),
        make_child(
            started_at=999.95,
            model_config=json.dumps({'_branched_from': 'parent-1'}),
        ),
    )
    assert not _is_continuation_session(
        make_parent(),
        make_child(
            started_at=998.5,
            model_config={'_branched_from': 'parent-1'},
        ),
    )
    # model_config._delegate_from pointing at the parent: never a
    # continuation, regardless of timing.
    assert not _is_continuation_session(
        make_parent(),
        make_child(
            started_at=999.95,
            model_config=json.dumps({'_delegate_from': 'parent-1'}),
        ),
    )
    # A marker pointing at a DIFFERENT session does not disqualify: compression
    # continuations inherit the rotated agent's model_config verbatim, so a
    # delegate's continuation still carries the delegate's own marker.
    assert _is_continuation_session(
        make_parent(),
        make_child(
            started_at=999.95,
            model_config=json.dumps({'_delegate_from': 'some-other-session'}),
        ),
    )
    # Unparsable model_config is untrusted lineage identity: it fails closed
    # as a boundary (no crash), even inside the tolerance window.
    assert not _is_continuation_session(
        make_parent(),
        make_child(started_at=999.95, model_config='not-json'),
    )
    assert not _is_continuation_session(
        make_parent(),
        make_child(started_at=950.0, model_config='not-json'),
    )
    # Fork guard holds regardless of timing.
    assert not _is_continuation_session(
        make_parent(), make_child(started_at=999.95, session_source='fork')
    )
    # Cross-source guard holds regardless of timing.
    assert not _is_continuation_session(
        make_parent(), make_child(started_at=999.95, source='telegram')
    )
    # Non-compression/cli_close parents never continue.
    assert not _is_continuation_session(
        make_parent(end_reason='user_stop'), make_child(started_at=999.95)
    )
    # Missing/unparsable boundary timestamps degrade to False (no crash).
    assert not _is_continuation_session(make_parent(ended_at='not-a-number'), make_child())
    assert not _is_continuation_session(make_parent(), make_child(started_at='not-a-number'))


@pytest.mark.parametrize("parent_source", ["tool", ""])
def test_tool_child_is_not_stitched_into_parent_lineage(_isolate, parent_source):
    """A marker-less tool child stays independent even with a matching/empty source."""
    from api.agent_sessions import (
        read_importable_agent_session_rows,
        read_session_lineage_metadata,
        read_session_lineage_report,
    )

    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    t0 = time.time() - 100
    parent_id, child_id = "tool_boundary_parent", "tool_boundary_child"
    try:
        _insert_state_row(
            conn, parent_id, source=parent_source, title="Parent conversation",
            started_at=t0, ended_at=t0 + 5, end_reason="compression",
        )
        _insert_state_row(
            conn, child_id, source="tool", title="Tool conversation",
            parent=parent_id, started_at=t0 + 4.5,
        )
        _insert_state_message(
            conn, parent_id, role="user", content="parent request", timestamp=t0 + 1,
        )
        _insert_state_message(
            conn, child_id, role="user", content="tool request", timestamp=t0 + 6,
        )

        projected = read_importable_agent_session_rows(
            _isolate, limit=None, exclude_sources=None,
        )
        rows = {row["id"]: row for row in projected}
        assert set(rows) == {parent_id, child_id}
        assert rows[child_id]["relationship_type"] == "child_session"
        assert "_lineage_root_id" not in rows[child_id]
        assert "_lineage_tip_id" not in rows[parent_id]

        metadata = read_session_lineage_metadata(_isolate, {parent_id, child_id})
        assert metadata[child_id]["relationship_type"] == "child_session"
        assert "_lineage_root_id" not in metadata[child_id]
        assert read_session_lineage_report(_isolate, parent_id)["total_segments"] == 1
        assert read_session_lineage_report(_isolate, child_id)["total_segments"] == 1

        messages = models.get_state_db_session_messages(
            child_id, stitch_continuations=True,
        )
        assert [message["content"] for message in messages] == ["tool request"]
    finally:
        conn.close()


def test_state_db_stitch_keeps_branched_child_out_of_parent_transcript(_isolate):
    """#7021 r2: the open/import transcript stitcher (get_state_db_session_messages
    with stitch_continuations=True) must apply the same model_config branch-marker
    guard as the listing classifier. A child whose model_config._branched_from /
    _delegate_from points at the parent must NOT have its messages stitched into
    the parent transcript even when it starts inside the tolerance window.
    """
    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    t0 = time.time() - 100
    try:
        # Compression parent whose child starts INSIDE the 2s tolerance window.
        _insert_state_row(
            conn,
            "stitch_branch_parent",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_message(
            conn, "stitch_branch_parent", role="user", content="parent turn", timestamp=t0 + 1
        )
        # Explicit Agent branch: fork identity lives in model_config, not
        # session_source. Starting 1.5s before the parent's ended_at — inside
        # the tolerance — it must still stay out of the parent's transcript.
        _insert_state_row(
            conn,
            "stitch_branch_child",
            parent="stitch_branch_parent",
            started_at=t0 + 5 - 1.5,
            model_config=json.dumps({"_branched_from": "stitch_branch_parent"}),
        )
        _insert_state_message(
            conn, "stitch_branch_child", role="user", content="branch turn", timestamp=t0 + 6
        )

        msgs = models.get_state_db_session_messages(
            "stitch_branch_child", stitch_continuations=True
        )
        contents = [m["content"] for m in msgs]
        assert "branch turn" in contents
        assert "parent turn" not in contents

        # Control: a genuine compression continuation (no branch marker) starting
        # inside the same window IS stitched into the parent transcript.
        _insert_state_row(
            conn,
            "stitch_continuation_child",
            parent="stitch_branch_parent",
            started_at=t0 + 5 - 0.5,
        )
        _insert_state_message(
            conn,
            "stitch_continuation_child",
            role="user",
            content="continuation turn",
            timestamp=t0 + 7,
        )
        msgs = models.get_state_db_session_messages(
            "stitch_continuation_child", stitch_continuations=True
        )
        contents = [m["content"] for m in msgs]
        assert "continuation turn" in contents
        assert "parent turn" in contents
    finally:
        conn.close()


def test_state_db_stitch_keeps_delegate_child_out_of_parent_transcript(_isolate):
    """#7021 r2: same guard for model_config._delegate_from (subagent runs) in
    the open/import transcript stitcher."""
    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    t0 = time.time() - 100
    try:
        _insert_state_row(
            conn,
            "stitch_delegate_parent",
            started_at=t0,
            ended_at=t0 + 5,
            end_reason="compression",
        )
        _insert_state_message(
            conn, "stitch_delegate_parent", role="user", content="parent turn", timestamp=t0 + 1
        )
        _insert_state_row(
            conn,
            "stitch_delegate_child",
            parent="stitch_delegate_parent",
            started_at=t0 + 5 - 1.0,
            model_config=json.dumps({"_delegate_from": "stitch_delegate_parent"}),
        )
        _insert_state_message(
            conn, "stitch_delegate_child", role="user", content="delegate turn", timestamp=t0 + 6
        )

        msgs = models.get_state_db_session_messages(
            "stitch_delegate_child", stitch_continuations=True
        )
        contents = [m["content"] for m in msgs]
        assert "delegate turn" in contents
        assert "parent turn" not in contents
    finally:
        conn.close()


def test_state_db_stitch_old_schema_without_identity_columns_still_stitches(_isolate):
    """#7021 r2: older state.db schemas lacking session_source/model_config degrade
    to NULL and keep the pre-fix behavior — a genuine compression continuation
    within the tolerance is still stitched, without crashing."""
    conn = sqlite3.connect(str(_isolate))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            started_at REAL NOT NULL,
            message_count INTEGER DEFAULT 0,
            parent_session_id TEXT,
            ended_at REAL,
            end_reason TEXT
        );
        """
    )
    _ensure_messages_table(conn)
    t0 = time.time() - 100
    try:
        conn.execute(
            """
            INSERT INTO sessions
            (id, source, title, started_at, message_count, parent_session_id, ended_at, end_reason)
            VALUES (?, ?, ?, ?, 2, ?, ?, ?)
            """,
            (
                "stitch_old_parent",
                "webui",
                "stitch_old_parent",
                t0,
                None,
                t0 + 5,
                "compression",
            ),
        )
        conn.execute(
            """
            INSERT INTO sessions
            (id, source, title, started_at, message_count, parent_session_id, ended_at, end_reason)
            VALUES (?, ?, ?, ?, 2, ?, NULL, NULL)
            """,
            ("stitch_old_child", "webui", "stitch_old_child", t0 + 4.5, "stitch_old_parent"),
        )
        conn.commit()
        _insert_state_message(
            conn, "stitch_old_parent", role="user", content="parent turn", timestamp=t0 + 1
        )
        _insert_state_message(
            conn, "stitch_old_child", role="user", content="old child turn", timestamp=t0 + 6
        )

        msgs = models.get_state_db_session_messages(
            "stitch_old_child", stitch_continuations=True
        )
        contents = [m["content"] for m in msgs]
        assert "old child turn" in contents
        assert "parent turn" in contents
    finally:
        conn.close()


def test_branch_markers_report_fail_closed_unknown_state():
    """Untrusted model_config lineage evidence is 'unknown', never 'no markers'."""
    from api.agent_sessions import _branch_markers

    assert _branch_markers(None) == ('none', {})
    assert _branch_markers({'model_config': None}) == ('none', {})
    assert _branch_markers({'model_config': ''}) == ('none', {})
    assert _branch_markers({'model_config': '   '}) == ('none', {})
    assert _branch_markers({'model_config': json.dumps({'model': 'x'})}) == ('none', {})
    assert _branch_markers({'model_config': json.dumps({'_reset_from': None})}) == ('none', {})
    assert _branch_markers(
        {'model_config': json.dumps({'_reset_from': 'p', '_delegate_from': 'a'})}
    ) == ('markers', {'_reset_from': 'p', '_delegate_from': 'a'})
    assert _branch_markers({'model_config': {'_branched_from': ' p '}}) == (
        'markers',
        {'_branched_from': 'p'},
    )

    for raw in ('{not-json', '[]', '"a string"', '17', 'null', '{"a":'):
        assert _branch_markers({'model_config': raw}) == ('unknown', {}), raw
    assert _branch_markers({'model_config': ['_delegate_from']}) == ('unknown', {})
    for hostile in (
        {'_delegate_from': 123},
        {'_delegate_from': ['p']},
        {'_delegate_from': {'id': 'p'}},
        {'_branched_from': True},
        {'_branched_from': '   '},
        {'_reset_from': ''},
        # A usable first marker never hides a malformed second one.
        {'_delegate_from': 'ancestor', '_branched_from': 123},
    ):
        assert _branch_markers({'model_config': json.dumps(hostile)}) == ('unknown', {}), hostile

    # A valid but pathologically deep payload overflows the decoder with
    # RecursionError (not ValueError); it must degrade to 'unknown', not crash.
    depth = 12_000
    deep_payload = '[' * depth + ']' * depth
    with pytest.raises(RecursionError):
        json.loads(deep_payload)
    assert _branch_markers({'model_config': deep_payload}) == ('unknown', {})


def test_untrusted_model_config_markers_fail_closed_inside_tolerance():
    """Master tolerance/predicate shape, but unknown identity is a boundary."""
    from api.agent_sessions import _is_continuation_session

    parent = {'id': 'fc_parent', 'source': 'webui', 'ended_at': 200.0, 'end_reason': 'compression'}
    child = {'id': 'fc_child', 'source': 'webui', 'started_at': 199.7, 'parent_session_id': 'fc_parent'}

    # Containment: marker-free and null-marker children still continue.
    assert _is_continuation_session(parent, child)
    assert _is_continuation_session(
        parent, {**child, 'model_config': json.dumps({'_delegate_from': None})}
    )
    # Master's shape is kept: a marker naming another session (inherited from
    # the rotated agent's model_config) does not split a real continuation.
    assert _is_continuation_session(
        parent, {**child, 'model_config': json.dumps({'_delegate_from': 'somewhere_else'})}
    )

    for raw in (
        '{not-json',
        '[]',
        json.dumps({'_delegate_from': 123}),
        json.dumps({'_branched_from': ''}),
        json.dumps({'_reset_from': ['fc_parent']}),
        json.dumps({'_delegate_from': 'somewhere_else', '_branched_from': 123}),
        '[' * 12_000 + ']' * 12_000,
    ):
        assert not _is_continuation_session(parent, {**child, 'model_config': raw}), raw[:40]

    # Legacy rows without ended_at keep continuing, unless identity is untrusted.
    legacy_parent = {**parent, 'ended_at': None}
    assert _is_continuation_session(legacy_parent, child)
    assert not _is_continuation_session(legacy_parent, {**child, 'model_config': '{not-json'})


def test_reset_from_marker_naming_parent_is_a_boundary():
    """Hermes Agent binds ``_reset_from`` like ``_branched_from``/``_delegate_from``.

    ``gateway/session_recovery.py`` stamps ``model_config._reset_from`` on a
    reset child and ``_NON_CONTINUATION_CHILD_FILTER_SQL`` rejects it as a
    compression continuation. The WebUI predicate must agree inside the
    tolerance window and on legacy rows without ``ended_at``.
    """
    from api.agent_sessions import _is_continuation_session

    for end_reason in ('compression', 'cli_close'):
        parent = {'id': 'reset_parent', 'source': 'telegram', 'ended_at': 300.0, 'end_reason': end_reason}
        child = {'id': 'reset_child', 'source': 'telegram', 'started_at': 299.5, 'parent_session_id': 'reset_parent'}
        assert _is_continuation_session(parent, child), end_reason
        assert not _is_continuation_session(
            parent, {**child, 'model_config': json.dumps({'_reset_from': 'reset_parent'})}
        ), end_reason
        assert not _is_continuation_session(
            parent, {**child, 'model_config': {'max_iterations': 40, '_reset_from': 'reset_parent'}}
        ), end_reason
        assert not _is_continuation_session(
            {**parent, 'ended_at': None},
            {**child, 'model_config': json.dumps({'_reset_from': 'reset_parent'})},
        ), end_reason
        # A direct reset marker is not masked by an inherited delegate marker.
        assert not _is_continuation_session(
            parent,
            {
                **child,
                'model_config': json.dumps(
                    {'_delegate_from': 'ancestor', '_reset_from': 'reset_parent'}
                ),
            },
        ), end_reason
        # An inherited reset marker naming an ancestor does not split the lineage.
        assert _is_continuation_session(
            parent, {**child, 'model_config': json.dumps({'_reset_from': 'ancestor'})}
        ), end_reason


def test_reset_child_inside_tolerance_stays_independent_across_readers(_isolate):
    """Sidebar listing, lineage metadata/report and transcript stitcher agree.

    A resumed parent closed by compression within the tolerance window of its
    reset child's creation must not absorb that reset conversation.
    """
    from api.agent_sessions import (
        read_importable_agent_session_rows,
        read_session_lineage_metadata,
        read_session_lineage_report,
    )

    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    t0 = time.time() - 100
    parent_id = 'reset_reader_parent'
    child_id = 'reset_reader_child'
    try:
        _insert_state_row(
            conn,
            parent_id,
            title='Reset parent conversation',
            started_at=t0,
            ended_at=t0 + 5,
            end_reason='compression',
        )
        _insert_state_message(conn, parent_id, role='user', content='parent turn', timestamp=t0 + 1)
        _insert_state_row(
            conn,
            child_id,
            title='Reset child conversation',
            parent=parent_id,
            started_at=t0 + 5 - 0.5,
            model_config=json.dumps({'_reset_from': parent_id}),
        )
        _insert_state_message(conn, child_id, role='user', content='reset turn', timestamp=t0 + 6)

        projected = {
            row['id']: row
            for row in read_importable_agent_session_rows(
                _isolate, limit=None, exclude_sources=None
            )
        }
        assert {parent_id, child_id} <= set(projected)
        assert projected[child_id]['title'] == 'Reset child conversation'
        assert projected[child_id]['relationship_type'] == 'reset_successor'
        assert projected[child_id]['_lineage_root_id'] == child_id
        assert '_compression_segment_count' not in projected[parent_id]
        assert 'model_config' not in projected[child_id]

        metadata = read_session_lineage_metadata(_isolate, {parent_id, child_id})
        assert metadata[child_id]['relationship_type'] == 'reset_successor'
        assert metadata[child_id]['_lineage_root_id'] == child_id

        report = read_session_lineage_report(_isolate, child_id)
        assert report['tip_session_id'] == child_id
        assert report['total_segments'] == 1

        stitched = models.get_state_db_session_messages(child_id, stitch_continuations=True)
        assert [m['content'] for m in stitched] == ['reset turn']

        # Control: the same row without the reset marker is a continuation.
        conn.execute('UPDATE sessions SET model_config = NULL WHERE id = ?', (child_id,))
        conn.commit()
        stitched = models.get_state_db_session_messages(child_id, stitch_continuations=True)
        assert [m['content'] for m in stitched] == ['parent turn', 'reset turn']
        projected_ids = {
            row['id']
            for row in read_importable_agent_session_rows(
                _isolate, limit=None, exclude_sources=None
            )
        }
        assert parent_id not in projected_ids and child_id in projected_ids
    finally:
        conn.close()


@pytest.mark.parametrize("canonical", [False, True])
def test_reset_metadata_never_promotes_tool_child(_isolate, canonical):
    """Tool provenance wins even when key/time or a marker looks like a reset."""
    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    try:
        _insert_state_row(conn, "tool_reset_parent", source="telegram", started_at=100,
                          ended_at=200, end_reason="session_reset", session_key="telegram:tool")
        _insert_state_row(conn, "tool_reset_child", source="tool", started_at=210,
                          parent="tool_reset_parent", session_key="telegram:tool",
                          model_config={"_reset_from": "tool_reset_parent"} if canonical else None)
        _insert_state_message(conn, "tool_reset_parent", role="user", content="parent", timestamp=101)
        _insert_state_message(conn, "tool_reset_child", role="user", content="task", timestamp=211)
        metadata = agent_sessions.read_session_lineage_metadata(_isolate, ["tool_reset_child"])
        assert metadata["tool_reset_child"]["relationship_type"] == "child_session"
        projected = {row["id"]: row for row in agent_sessions.read_importable_agent_session_rows(
            _isolate, limit=None, exclude_sources=None,
        )}
        assert projected["tool_reset_child"]["relationship_type"] == "child_session"
        report = agent_sessions.read_session_lineage_report(_isolate, "tool_reset_parent")
        assert [row["session_id"] for row in report["children"]] == ["tool_reset_child"]
    finally:
        conn.close()


@pytest.mark.parametrize("newer_count", [5, 300, 400])
def test_compressed_fork_beyond_lineage_cap_remains_visible(_isolate, monkeypatch, newer_count):
    """A retained fork source must not hide a live tip under its archived snapshot.

    Bind the gate's 300/400-row reproduction to the real payload builder and
    production JS grouping. Newer ordinary rows consume the default lineage cap.
    """
    from urllib.parse import urlparse
    from tests.test_465_session_branching import _FakeHandler, _capture_route

    monkeypatch.delenv("HERMES_WEBUI_LINEAGE_TOP_N", raising=False)
    conn = _ensure_state_db(_isolate)
    t0 = time.time() - 1000
    parent_id, tip_id = "old_fork_snapshot", "old_fork_live_tip"
    try:
        snapshot = _save_webui_session(parent_id, title="Fork snapshot", updated_at=t0)
        snapshot.messages[0]["content"] = "shared inherited discussion"
        snapshot.archived = True
        snapshot.pre_compression_snapshot = True
        snapshot.session_source = "fork"
        snapshot.parent_session_id = "original-fork-parent"
        snapshot.save(touch_updated_at=False)
        tip = _save_webui_session(tip_id, title="Live compressed fork", updated_at=t0 + 10)
        tip.messages[0]["content"] = "shared inherited discussion"
        tip.session_source = "fork"
        tip.parent_session_id = parent_id
        tip.save(touch_updated_at=False)
        _insert_state_row(conn, parent_id, started_at=t0, ended_at=t0 + 5, end_reason="compression")
        _insert_state_row(conn, tip_id, started_at=t0 + 6, parent=parent_id)
        for i in range(newer_count):
            _save_webui_session(f"newer_{i}", title=f"Newer conversation {i}", updated_at=t0 + 20 + i)

        monkeypatch.setattr(routes, "all_sessions", models.all_sessions)
        monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", models._enrich_sidebar_lineage_metadata)
        monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
        args = dict(active_profile="default", all_profiles=False, show_cli_sessions=False,
                    show_previous_messaging_sessions=False, show_cron_sessions=False)
        payload = routes._build_session_list_cache_payload(**args, include_archived=False)
        references = routes._build_session_list_cache_payload(**args, include_archived=True)
        assert len(payload["sessions"]) == newer_count + 1
        assert payload["sessions"][-1]["session_id"] == tip_id
        assert payload["archived_count"] == 0  # compression snapshot stays hidden
        visible = render_sidebar_rows(payload["sessions"], references["sessions"])
        assert tip_id in {row["session_id"] for row in visible}
        assert parent_id not in {row["session_id"] for row in visible}
        projected_tip = next(row for row in payload["sessions"] if row["session_id"] == tip_id)
        assert projected_tip["session_source"] == "webui"
        if newer_count < 300:
            assert projected_tip["_lineage_root_id"] == parent_id
        else:
            assert "_lineage_root_id" not in projected_tip
        assert projected_tip["parent_session_id"] == parent_id
        # Content-only search uses a separate API projection. Its hits are merged
        # by the real client before grouping, so list visibility alone is not enough.
        response = _capture_route(monkeypatch)
        monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
        routes._handle_sessions_search(
            _FakeHandler(),
            urlparse("/api/sessions/search?q=shared+inherited+discussion&content=1&depth=0"),
        )
        assert response["status"] == 200
        hits = response["ok"]["sessions"]
        assert tip_id in {row["session_id"] for row in hits}
        assert all(row["match_type"] == "content" for row in hits)
        for search_references in (references["sessions"], hits):
            visible_hits = render_sidebar_rows(
                payload["sessions"], search_references,
                query="shared inherited discussion", content_matches=hits,
            )
            assert tip_id in {row["session_id"] for row in visible_hits}
            assert parent_id not in {row["session_id"] for row in visible_hits}
        # Projection never overwrites original fork or compression provenance.
        for sid in (parent_id, tip_id):
            saved = json.loads((models.SESSION_DIR / f"{sid}.json").read_text())
            assert saved["session_source"] == "fork"
    finally:
        conn.close()


@pytest.mark.parametrize("parent_archived", [False, True])
@pytest.mark.parametrize("show_archived", [False, True])
@pytest.mark.parametrize("search", ["", "title", "id", "link", "content"])
@pytest.mark.parametrize("lineage_cap", [300, 1])
def test_used_branch_stays_visible_after_original_archived(
    _isolate, monkeypatch, parent_archived, show_archived, search, lineage_cap,
):
    """Branch/Archive handlers feed the real payload and production search/grouping."""
    from urllib.parse import urlparse
    from tests.test_465_session_branching import _FakeHandler, _capture_route

    conn = _ensure_state_db(_isolate)
    _ensure_messages_table(conn)
    parent = _save_webui_session("used_branch_original", title="Original discussion", updated_at=time.time() - 20)
    monkeypatch.setenv("HERMES_WEBUI_LINEAGE_TOP_N", str(lineage_cap))
    _save_webui_session("newer_unrelated", title="Unrelated conversation", updated_at=time.time() + 60)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda _sid: {})
    monkeypatch.setattr(routes, "all_sessions", models.all_sessions)
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", models._enrich_sidebar_lineage_metadata)
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    response = _capture_route(monkeypatch)
    body = {"session_id": parent.session_id, "title": "Independent branch discussion"}
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    try:
        routes.handle_post(_FakeHandler(), urlparse("/api/session/branch"))
        assert "bad" not in response, response
        branch_id = response["ok"]["session_id"]
        branch = models.get_session(branch_id)
        assert branch.parent_session_id == parent.session_id
        assert branch.session_source == "fork"
        # Model the first submitted turn's sidecar and plain WebUI state.db mirror.
        # Branch creation itself does not put the sidecar parent into that row.
        branch.messages.append({"role": "user", "content": "Continue only in this branch"})
        branch.save()
        _insert_state_row(conn, branch_id, source="webui", started_at=time.time())
        for index, message in enumerate(branch.messages):
            _insert_state_message(conn, branch_id, role=message["role"],
                                  content=message["content"], timestamp=time.time() + index)
        if parent_archived:
            body = {"session_id": parent.session_id, "archived": True}
            response.clear()
            routes.handle_post(_FakeHandler(), urlparse("/api/session/archive"))
            assert response["ok"]["session"]["archived"] is True
        before = (models.SESSION_DIR / f"{branch_id}.json").read_bytes()
        payload = routes._build_session_list_cache_payload(
            active_profile="default", all_profiles=False, show_cli_sessions=False,
            show_previous_messaging_sessions=False, show_cron_sessions=False,
            include_archived=show_archived,
        )
        query = {"": "", "title": branch.title, "id": branch_id,
                 "link": f"http://webui.local/session/{branch_id}",
                 "content": "Continue only in this branch"}[search]
        hits = []
        if search == "content":
            monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
            response.clear()
            routes._handle_sessions_search(
                _FakeHandler(),
                urlparse("/api/sessions/search?q=Continue+only+in+this+branch&content=1&depth=0"),
            )
            assert response["status"] == 200
            hits = response["ok"]["sessions"]
            assert {row["session_id"] for row in hits} == {branch_id}
        visible = render_sidebar_rows(
            payload["sessions"],
            [*(hits if search == "content" else payload["sessions"]),
             *payload.get("sidebar_reference_sessions", [])],
            query=query, show_archived=show_archived, include_indicators=True,
            content_matches=hits,
        )
        assert branch_id in {row["session_id"] for row in visible}
        rendered_branch = next(row for row in visible if row["session_id"] == branch_id)
        assert rendered_branch["parent_session_id"] == parent.session_id
        assert rendered_branch["_test_branch_indicator"] is True
        assert rendered_branch.get("relationship_type") != "reset_successor"
        assert (models.SESSION_DIR / f"{branch_id}.json").read_bytes() == before
        assert json.loads(before)["session_source"] == "fork"
    finally:
        conn.close()
