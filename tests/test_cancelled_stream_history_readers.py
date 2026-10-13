"""Real Stop/worker history and private SQLite identity through settlement."""
import copy
import queue
import sqlite3

import pytest
from api import config, models, profiles, routes, session_ops, streaming
from tests.test_cancel_restart_journal_recovery import (
    _isolated_state,  # noqa: F401 - autouse isolated session/stream registries
    _start_cancelled_turn,
)
from tests.test_cancelled_journal_owner_occurrences import _recover
from tests.test_webui_state_db_reconciliation import _make_state_db


def _worker(monkeypatch, tmp_path, session, prompt, captured):
    class OlderAgent:
        def __init__(self, **kwargs):
            self.session_id = session.session_id
            self.context_compressor = None
            self.ephemeral_system_prompt = None

        # Real supported old signature: persist_user_timestamp is absent.
        def run_conversation(self, user_message, system_message,
                             conversation_history, task_id, persist_user_message):
            captured.append(copy.deepcopy(conversation_history))
            return {'completed': True, 'final_response': 'NEXT_ANSWER',
                    'messages': copy.deepcopy(conversation_history) + [
                        {'role': 'user', 'content': persist_user_message},
                        {'role': 'assistant', 'content': 'NEXT_ANSWER'}]}

    monkeypatch.setattr(streaming, '_get_ai_agent', lambda: OlderAgent)
    monkeypatch.setattr(streaming, 'resolve_model_provider',
                        lambda *args, **kwargs: ('test-model', None, None))
    monkeypatch.setattr(streaming, 'get_config', lambda: {})
    monkeypatch.setattr(config, 'get_config', lambda: {})
    monkeypatch.setattr(config, '_resolve_cli_toolsets', lambda *args, **kwargs: [])
    monkeypatch.setattr(profiles, 'get_active_hermes_home', lambda: tmp_path)
    stream = session.session_id + '-next-' + str(len(captured))
    routes._prepare_chat_start_session_for_stream(
        session, msg=prompt, attachments=[], workspace=str(tmp_path),
        model='test-model', model_provider=None, stream_id=stream, started_at=100 + len(captured))
    models.SESSIONS[session.session_id] = session
    config.STREAMS[stream] = queue.Queue()
    streaming._run_agent_streaming(session.session_id, prompt, 'test-model', str(tmp_path), stream, [])
    assert len(captured) > 0
    return models.Session.load(session.session_id)


@pytest.mark.requires_agent_modules
@pytest.mark.parametrize('saved_context', [False, True, 'stopped-projection'])
def test_same_process_stop_real_followup_worker_retains_subject(tmp_path, monkeypatch, saved_context):
    sid = 'live-worker-' + str(saved_context)
    db = tmp_path/'state.db'
    monkeypatch.setattr(models, '_active_state_db_path', lambda: db)
    prior = [{'role': 'user', 'content': 'EARLIER_QUESTION', 'timestamp': 1},
             {'role': 'assistant', 'content': 'EARLIER_ANSWER', 'timestamp': 2}]
    stopped = [{'role': 'user', 'content': 'Review the latest changes', 'timestamp': 10},
               {'role': 'assistant', 'content': 'PARTIAL_REVIEW', 'timestamp': 11}]
    session = _start_cancelled_turn(sid, sid+'-stop')
    session.messages = copy.deepcopy(prior)
    session.context_messages = copy.deepcopy(prior + (stopped if saved_context else []))
    session.pending_user_message = stopped[0]['content']
    session.save()
    _make_state_db(db, sid, prior+stopped)
    config.STREAM_PARTIAL_TEXT[sid+'-stop'] = stopped[1]['content']
    assert streaming.cancel_stream(sid+'-stop')
    if saved_context == 'stopped-projection':
        # The real live partial carries exact run authority, but model context
        # omits the display-only terminal. Late raw replay must stay excluded.
        session.context_messages = copy.deepcopy([row for row in session.messages if not row.get('_error')])
        session.save(touch_updated_at=False)
        clock = max(float(row.get('timestamp') or 0) for row in session.messages)
        with sqlite3.connect(db) as conn:
            conn.execute('INSERT INTO messages (session_id,role,content,timestamp) VALUES (?,?,?,?)',
                         (sid, 'assistant', 'FORBIDDEN_CANCELLED_RAW', clock+5))
    models.SESSIONS.clear()  # cold sidecar load, same interpreter/process token
    session = models.get_session(sid)
    assert any(row.get('_partial') for row in session.messages)
    captured = []
    _worker(monkeypatch, tmp_path, session, 'Continue that review', captured)
    assert [row.get('content') for row in captured[0]] == [row['content'] for row in prior+stopped]


@pytest.mark.requires_agent_modules
def test_real_worker_keeps_admitted_row_identity_over_three_settles(tmp_path, monkeypatch):
    sid = 'worker-private-prefix'
    db = tmp_path/'state.db'
    monkeypatch.setattr(models, '_active_state_db_path', lambda: db)
    session, owner = _recover(sid)
    pair = [{'role': 'user', 'content': 'LATER_USER', 'timestamp': 20},
            {'role': 'assistant', 'content': 'LATER_ANSWER', 'timestamp': 21}]
    _make_state_db(db, sid, [owner,
        {'role': 'assistant', 'content': 'CANCELLED_REPLAY', 'timestamp': 11}, *pair])
    # SQLite admission owns IDs; the Agent strips them on return. Every worker
    # must restore the proven historical mapping before saving the next sidecar.
    session.messages = models.reconciled_state_db_messages_for_session(session)
    session.context_messages = models.reconciled_state_db_messages_for_session(session, prefer_context=True)
    session.save(touch_updated_at=False)
    captured = []
    for turn in range(3):
        with sqlite3.connect(db) as conn:
            conn.execute('UPDATE messages SET timestamp=timestamp+10 WHERE session_id=? AND id>2', (sid,))
        session = _worker(monkeypatch, tmp_path, session, 'NEXT_'+str(turn), captured)
        for rows in (session.messages, session.context_messages):
            assert sum(row.get('content') == 'LATER_USER' for row in rows) == 1
            assert sum(row.get('content') == 'LATER_ANSWER' for row in rows) == 1
            assert next(row for row in rows if row.get('content') == 'LATER_USER')['_state_db_row_id'] == 3
            assert next(row for row in rows if row.get('content') == 'LATER_ANSWER')['_state_db_row_id'] == 4
        assert all('_state_db_row_id' not in row for row in captured[-1])
    rows, context = session_ops.regeneration_state(session, use_sidecar=True)
    for selected in (rows, context):
        assert sum(row.get('content') == 'LATER_USER' for row in selected) == 1
        assert sum(row.get('content') == 'LATER_ANSWER' for row in selected) == 1


@pytest.mark.parametrize('clock', [{'timestamp': True}, {'_ts': False},
    {'timestamp': 1, '_ts': True}, {'timestamp': False, '_ts': 1}])
def test_boolean_clock_cannot_prove_identity(clock):
    assert models._message_exact_timestamp_details(clock) == (None, False)


def test_single_represented_row_does_not_consume_saved_pair():
    sidecar = [{'role': 'user', 'content': 'OWNER', 'timestamp': 10},
        {'role': 'assistant', 'content': 'STOP', 'timestamp': 11, '_recovered_from_cancel_journal': True},
        {'role': 'assistant', 'content': 'Cancelled', 'timestamp': 12, '_error': True},
        {'role': 'user', 'content': 'SAME', 'timestamp': 20, '_state_db_row_id': 3},
        {'role': 'assistant', 'content': 'ANSWER', 'timestamp': 21, '_state_db_row_id': 4}]
    state = copy.deepcopy(sidecar[-2:])
    assert models._state_db_after_saved_cancel_successors(sidecar, state, sidecar[-2:-1]) == state
    assert models._state_db_after_saved_cancel_successors(sidecar, state, sidecar[-2:]) == []


def test_cancel_owner_session_ops_never_uses_unproved_bounded_tail(tmp_path, monkeypatch):
    sid = 'session-ops-owner-full-read'
    db = tmp_path/'state.db'
    monkeypatch.setattr(models, '_active_state_db_path', lambda: db)
    session, owner = _recover(sid)
    pair = [{'role': 'user', 'content': 'LATER_USER', 'timestamp': 20},
            {'role': 'assistant', 'content': 'LATER_ANSWER', 'timestamp': 21}]
    _make_state_db(db, sid, [owner, {'role': 'assistant', 'content': 'REPLAY', 'timestamp': 11}, *pair])
    session.messages = models.reconciled_state_db_messages_for_session(session)
    session.context_messages = models.reconciled_state_db_messages_for_session(session, prefer_context=True)
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE messages SET timestamp=timestamp+50 WHERE id>2')
    monkeypatch.setattr(session_ops, '_sidecar_regeneration_read_floor', lambda _session: 100)
    monkeypatch.setattr(session_ops, '_bounded_tail_snapshot_if_safe', lambda *_args: [])
    full = session_ops.regeneration_state(session)
    assert session_ops.regeneration_state(session, use_sidecar=True) == full
    for selected in full:
        assert [row.get('content') for row in selected].count('LATER_USER') == 1
        assert [row.get('content') for row in selected].count('LATER_ANSWER') == 1


@pytest.mark.parametrize('text,expected', [('\ud83d\ude42', '🙂'), ('plain普通🙂', 'plain普通🙂'),
                                         ('prefix\ud83dafter', 'prefix�after')])
def test_html_export_repairs_pair_before_first_save(tmp_path, monkeypatch, text, expected):
    import io
    from urllib.parse import urlparse
    session = models.Session(session_id='unsaved-html', messages=[{'role': 'assistant', 'content': text}])
    original = copy.deepcopy(session.messages)
    monkeypatch.setattr(routes, 'get_session', lambda _sid: session)
    monkeypatch.setattr(routes, '_session_profile_for_request', lambda *_args, **_kwargs: None, raising=False)
    class Handler:
        def __init__(self):
            self.wfile = io.BytesIO()
            self.headers = {}
            self.status = None
        def send_response(self, status):
            self.status = status
        def send_header(self, key, value):
            self.headers[key] = value
        def end_headers(self):
            pass
    handler = Handler()
    routes._handle_session_export(handler, urlparse('/api/session/export?session_id=unsaved-html&format=html'))
    body = handler.wfile.getvalue()
    assert handler.status == 200
    assert int(handler.headers['Content-Length']) == len(body)
    assert expected in body.decode('utf-8')
    assert session.messages == original and not session.path.exists()


def test_partial_and_error_without_selected_user_cannot_veto_sqlite():
    selected = [{'role': 'assistant', 'content': 'PARTIAL', '_partial': True, 'timestamp': 11},
                {'role': 'assistant', 'content': 'Cancelled', '_error': True, 'timestamp': 12}]
    state = [{'role': 'user', 'content': 'MISSING_SUBJECT', 'timestamp': 13}]
    result = models.merge_session_messages_append_only(selected, state, incoming_provenance='state_db')
    assert any(row.get('content') == 'MISSING_SUBJECT' for row in result)


@pytest.mark.parametrize('same_question', [False, True])
@pytest.mark.parametrize('older_clock', [10, 11])
def test_earlier_clock_collision_does_not_own_current_partial(tmp_path, monkeypatch, same_question, older_clock):
    db = tmp_path/'state.db'
    monkeypatch.setattr(models, '_active_state_db_path', lambda: db)
    user = {'role': 'user', 'content': 'CURRENT_QUESTION', 'timestamp': 10}
    partial = {'role': 'assistant', 'content': 'Working on it', 'timestamp': 11, '_partial': True}
    older = [{'role': 'user', 'content': 'CURRENT_QUESTION' if same_question else 'OTHER_QUESTION', 'timestamp': 10},
             {'role': 'assistant', 'content': 'Working on it', 'timestamp': older_clock}]
    visible = copy.deepcopy(older)+[user, partial,
        {'role': 'assistant', 'content': 'Cancelled', 'timestamp': 12, '_error': True}]
    session = models.Session(session_id='clock-collision-'+str(same_question), messages=visible, context_messages=older)
    _make_state_db(db, session.session_id, copy.deepcopy(older)+[user,
        {'role': 'assistant', 'content': 'NEW_STOPPED_DB_CONTEXT', 'timestamp': 13}])
    assert not models._selected_history_owns_live_partial(older, visible)
    snapshot = models.get_state_db_session_messages(session.session_id, with_revision=True)
    merged = models.reconciled_state_db_messages_for_session(session, prefer_context=True, state_messages=snapshot)
    assert 'NEW_STOPPED_DB_CONTEXT' in [row.get('content') for row in merged]


def test_live_selected_guard_preserves_existing_role_casing_contract():
    owner = [{'role': 'USER', 'content': 'QUESTION', 'timestamp': 10},
             {'role': 'ASSISTANT', 'content': 'PARTIAL', 'timestamp': 11, '_partial': True},
             {'role': 'ASSISTANT', 'content': 'Cancelled', 'timestamp': 12, '_error': True}]
    assert models._sidecar_has_terminal_partial_error(owner, live_only=True)
    assert models._selected_history_owns_live_partial(owner[:2], owner)


@pytest.mark.requires_agent_modules
@pytest.mark.parametrize('older_partial', [False, True])
def test_real_stop_earlier_identical_clocks_cannot_veto_next_worker(tmp_path, monkeypatch, older_partial):
    sid = 'live-worker-identical-clocks-' + str(older_partial)
    db = tmp_path/'state.db'
    monkeypatch.setattr(models, '_active_state_db_path', lambda: db)
    import time
    from types import SimpleNamespace
    actual_clock = streaming.time
    monkeypatch.setattr(streaming, 'time', SimpleNamespace(
        time=lambda: 11.0, monotonic=time.monotonic,
        perf_counter=time.perf_counter, sleep=time.sleep))
    prior = [{'role': 'user', 'content': 'SAME_QUESTION', 'timestamp': 10},
             {'role': 'assistant', 'content': 'SAME_ANSWER', 'timestamp': 11}]
    if older_partial:
        prior[1]['_partial'] = True
    session = _start_cancelled_turn(sid, sid+'-stop')
    # A real chat-start checkpoint already contains the new user before Stop.
    session.messages = copy.deepcopy(prior + [prior[0]])
    session.context_messages = copy.deepcopy(prior)
    session.pending_user_message = prior[0]['content']
    session.save()
    _make_state_db(db, sid, prior+[prior[0],
        {'role': 'assistant', 'content': 'NEW_STOPPED_DB_CONTEXT', 'timestamp': 13}])
    config.STREAM_PARTIAL_TEXT[sid+'-stop'] = prior[1]['content']
    assert streaming.cancel_stream(sid+'-stop')
    monkeypatch.setattr(streaming, 'time', actual_clock)
    models.SESSIONS.clear()
    session = models.get_session(sid)
    captured = []
    _worker(monkeypatch, tmp_path, session, 'Continue', captured)
    assert 'NEW_STOPPED_DB_CONTEXT' in [row.get('content') for row in captured[0]]


@pytest.mark.parametrize('authority', ['token', 'row-id', 'stable-id'])
def test_trusted_current_owner_can_veto_despite_earlier_identical_clocks(authority):
    earlier = [{'role': 'user', 'content': 'QUESTION', 'timestamp': 10},
               {'role': 'assistant', 'content': 'PARTIAL', 'timestamp': 11, '_partial': True}]
    current = copy.deepcopy(earlier)
    key, value = {'token': ('_active_turn_token', 'CURRENT_RUN'),
                  'row-id': ('_state_db_row_id', 7),
                  'stable-id': ('id', 'CURRENT_USER')}[authority]
    current[0][key] = value
    owner = earlier + current + [{'role': 'assistant', 'content': 'Cancelled',
                                  'timestamp': 12, '_error': True}]
    assert not models._selected_history_owns_live_partial(earlier, owner)
    assert models._selected_history_owns_live_partial(current, owner)


def test_clock_only_settled_answer_cannot_own_partial_without_earlier_display_rows():
    owner = [{'role': 'user', 'content': 'QUESTION', 'timestamp': 10},
             {'role': 'assistant', 'content': 'PARTIAL', 'timestamp': 11, '_partial': True},
             {'role': 'assistant', 'content': 'Cancelled', 'timestamp': 12, '_error': True}]
    selected = copy.deepcopy(owner[:2])
    selected[1].pop('_partial')
    assert not models._selected_history_owns_live_partial(selected, owner)
    assert models._selected_history_owns_live_partial(owner[:2], owner)
