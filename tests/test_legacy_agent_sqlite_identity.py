"""Pre-timestamp Agent flush compatibility: real returned IDs, never text guesses."""
import copy
import inspect
import sqlite3
import threading

import pytest
from api import models, session_ops, streaming
from tests.test_cancel_restart_journal_recovery import _isolated_state  # noqa: F401
from tests.test_cancelled_journal_owner_occurrences import _recover
from tests.test_webui_state_db_reconciliation import _make_state_db


class SQLiteStore:
    def __init__(self, path):
        self.path = path

    def ensure_session(self, *args, **kwargs):
        pass

    def append_message(self, *, session_id, role, content, **kwargs):
        # The actual pre-timestamp API assigns insertion time and returns IDs.
        with sqlite3.connect(self.path) as conn:
            return conn.execute('INSERT INTO messages (session_id,role,content,timestamp) VALUES (?,?,?,?)',
                                (session_id, role, content, 1000)).lastrowid


class LegacyAgent:
    def __init__(self, sid, db):
        self.session_id, self._session_db = sid, db
        self._last_flushed_db_idx = 0

    def run_conversation(self, user_message, system_message, conversation_history, task_id, persist_user_message):
        result = copy.deepcopy(conversation_history) + [
            {'role': 'user', 'content': persist_user_message},
            {'role': 'assistant', 'content': 'ANSWER_'+persist_user_message}]
        self._persist_user_message_idx = len(conversation_history)
        self._flush_messages_to_session_db(result, conversation_history)
        return result

    def _flush_messages_to_session_db(self, messages, conversation_history=None):
        # Mirrors the Apr-30 Agent's indexed append loop. Independent review
        # additionally executes the immutable original method with its old DB.
        start = max(len(conversation_history or []), self._last_flushed_db_idx)
        for row in messages[start:]:
            self._session_db.append_message(session_id=self.session_id,
                                           role=row['role'], content=row['content'])
        self._last_flushed_db_idx = len(messages)


def _kwargs(agent, history, prompt, identity=None):
    extra = ({'legacy_row_identity_owner': identity or {'session_id': agent.session_id, 'token': 'test:100', 'text': prompt}}
             if 'legacy_row_identity_owner' in inspect.signature(streaming._build_run_conversation_kwargs).parameters else {})
    return streaming._build_run_conversation_kwargs(
        agent.run_conversation, user_message=prompt, system_message='',
        conversation_history=history, conversation_history_revision=None,
        task_id=agent.session_id, persist_user_message=prompt, persist_user_timestamp=100,
        **extra)


@pytest.mark.parametrize('turns', [3, 5])
def test_old_agent_new_turns_keep_real_row_identity_through_settlement(tmp_path, monkeypatch, turns):
    sid, db = 'legacy-multiple-'+str(turns), tmp_path/'state.db'
    monkeypatch.setattr(models, '_active_state_db_path', lambda: db)
    session, owner = _recover(sid)
    _make_state_db(db, sid, [owner, {'role': 'assistant', 'content': 'CANCELLED_REPLAY', 'timestamp': 11}])
    agent = LegacyAgent(sid, SQLiteStore(db))
    for turn in range(turns):
        # Real production state readers and history/settle functions; result
        # clocks are absent and SQLite insertion clocks deliberately differ.
        snapshot = models.get_state_db_session_messages(sid, with_revision=True, include_row_identity=True)
        previous = models.reconciled_state_db_messages_for_session(session, state_messages=snapshot)
        context = models.reconciled_state_db_messages_for_session(session, prefer_context=True, state_messages=snapshot)
        prompt = 'QUESTION_'+str(turn)
        session.pending_user_message, session.pending_started_at = prompt, 20+turn
        session.active_stream_id = 'legacy-turn-'+str(turn)
        authority = streaming._active_turn_authority(session, session.active_stream_id, prompt)
        history = streaming._sanitize_messages_for_agent(streaming._new_turn_context_from_messages(context, prompt))
        kwargs = _kwargs(agent, history, prompt, authority)
        assert 'persist_user_timestamp' not in kwargs
        result = agent.run_conversation(**kwargs)
        assert all(row.get('_state_db_row_id', 0) > 0 for row in result[-2:])
        streaming._settle_result_messages(session, previous, context, result, prompt, 'webui', authority)
        streaming._stamp_missing_message_timestamps(session.messages, now=50+turn)
        session.active_stream_id = session.pending_user_message = session.pending_started_at = None
        session.save(touch_updated_at=False)
        models.SESSIONS.clear()
        session = models.get_session(sid)
        expected = [text for i in range(turn+1) for text in ('QUESTION_'+str(i), 'ANSWER_QUESTION_'+str(i))]
        for rows in session_ops.regeneration_state(session, use_sidecar=True):
            later = [row.get('content') for row in rows if str(row.get('content')).startswith(('QUESTION_', 'ANSWER_'))]
            assert later == expected
            assert 'CANCELLED_REPLAY' not in [row.get('content') for row in rows]
    assert 'append_message' not in agent._session_db.__dict__


@pytest.mark.parametrize('kind', ['invalid-id', 'conflicting-id', 'partial-failure', 'foreign-session', 'other-thread', 'wrong-role', 'wrong-content'])
def test_legacy_append_observer_never_invents_identity(kind):
    class Store:
        count = 41
        def append_message(self, **kwargs):
            self.count += 1
            return True if kind == 'invalid-id' else self.count
    db = Store()
    agent = LegacyAgent('owned', db)
    rows = [{'role': 'user', 'content': 'same'}, {'role': 'assistant', 'content': 'same'}]
    if kind == 'conflicting-id':
        rows[0]['_state_db_row_id'] = 99

    def flush(messages, conversation_history=None):
        if kind == 'other-thread':
            def foreign_worker():
                db.append_message(session_id='owned', role='user', content='same')
                db.append_message(session_id='owned', role='assistant', content='same')
            thread = threading.Thread(target=foreign_worker)
            thread.start()
            thread.join()
            return
        db.append_message(session_id='foreign' if kind == 'foreign-session' else 'owned', role='user', content='same')
        if kind == 'partial-failure':
            raise OSError('injected append failure')
        db.append_message(session_id='owned', role='user' if kind == 'wrong-role' else 'assistant',
                          content='different' if kind == 'wrong-content' else 'same')
    agent._flush_messages_to_session_db = flush
    _kwargs(agent, [], 'same')
    if kind == 'partial-failure':
        with pytest.raises(OSError):
            agent._flush_messages_to_session_db(rows)
    else:
        agent._flush_messages_to_session_db(rows)
    assert rows == ([{'role': 'user', 'content': 'same', '_state_db_row_id': 99}, rows[1]]
                    if kind == 'conflicting-id' else [{'role': 'user', 'content': 'same'}, {'role': 'assistant', 'content': 'same'}])
    assert 'append_message' not in db.__dict__


def test_adapter_deactivates_without_current_cancel_owner(tmp_path):
    db = tmp_path/'state.db'
    _make_state_db(db, 'owned', [])
    agent = LegacyAgent('owned', SQLiteStore(db))
    _kwargs(agent, [], 'one')
    first = agent.run_conversation('one', '', [], 'owned', 'one')
    assert first[0]['_state_db_row_id'] == 1
    streaming._build_run_conversation_kwargs(agent.run_conversation,
        user_message='two', system_message='', conversation_history=[],
        conversation_history_revision=None, task_id='owned', persist_user_message='two',
        persist_user_timestamp=100, legacy_row_identity_owner=None)
    agent._last_flushed_db_idx = 0
    second = agent.run_conversation('two', '', [], 'owned', 'two')
    assert all('_state_db_row_id' not in row and '_active_turn_token' not in row for row in second)
    assert 'append_message' not in agent._session_db.__dict__


def test_matching_existing_append_id_and_instance_override_are_preserved(tmp_path):
    db = tmp_path/'state.db'
    _make_state_db(db, 'owned', [])
    store = SQLiteStore(db)
    original = store.append_message
    store.append_message = original
    agent = LegacyAgent('owned', store)
    _kwargs(agent, [], 'one')
    rows = [{'role': 'user', 'content': 'one', '_state_db_row_id': 1},
            {'role': 'assistant', 'content': 'answer'}]
    agent._persist_user_message_idx = 0
    agent._flush_messages_to_session_db(rows)
    assert rows[0]['_active_turn_token'] == 'test:100'
    assert store.__dict__['append_message'] is original


def test_another_worker_builder_cannot_replace_captured_run_owner(tmp_path):
    db = tmp_path/'state.db'
    _make_state_db(db, 'owned', [])
    agent = LegacyAgent('owned', SQLiteStore(db))
    _kwargs(agent, [], 'same', {'session_id': 'owned', 'token': 'older:100', 'text': 'same'})
    successor = threading.Thread(target=lambda: _kwargs(agent, [], 'same',
        {'session_id': 'owned', 'token': 'newer:101', 'text': 'same'}))
    successor.start()
    successor.join()
    result = agent.run_conversation('same', '', [], 'owned', 'same')
    assert result[0]['_active_turn_token'] == 'older:100'


def test_native_ids_and_clocks_are_preserved_without_adapter_metadata(tmp_path):
    class CurrentAgent(LegacyAgent):
        def run_conversation(self, persist_user_timestamp=None, **kwargs):
            pass
    db = tmp_path/'state.db'
    _make_state_db(db, 'owned', [])
    agent = CurrentAgent('owned', SQLiteStore(db))
    _kwargs(agent, [], 'same')
    rows = [{'role': 'user', 'content': 'same', '_row_id': 1, 'timestamp': 42},
            {'role': 'assistant', 'content': 'answer', '_row_id': 2}]
    before = copy.deepcopy(rows)
    agent._persist_user_message_idx = 0
    agent._flush_messages_to_session_db(rows)
    assert rows == before
    assert 'append_message' not in agent._session_db.__dict__


@pytest.mark.parametrize('clock,expected', [(None, 100), (42, 42), (True, True), ('bad', 'bad')])
def test_timestamp_accepting_agent_with_missing_ids_preserves_owner_clock(tmp_path, clock, expected):
    class IntermediateAgent(LegacyAgent):
        def run_conversation(self, persist_user_timestamp=None, **kwargs):
            pass
    db = tmp_path/'state.db'
    _make_state_db(db, 'owned', [])
    agent = IntermediateAgent('owned', SQLiteStore(db))
    _kwargs(agent, [], 'same', {'session_id': 'owned', 'token': 'test:100', 'text': 'same', 'timestamp': 100})
    rows = [{'role': 'user', 'content': 'same'}, {'role': 'assistant', 'content': 'answer'}]
    if clock is not None:
        rows[0]['timestamp'] = clock
    agent._persist_user_message_idx = 0
    agent._flush_messages_to_session_db(rows)
    assert [row['_state_db_row_id'] for row in rows] == [1, 2]
    assert rows[0]['timestamp'] == expected


@pytest.mark.parametrize('changed_prefix', [False, True])
def test_historical_row_identity_restores_only_on_unchanged_pre_turn_prefix(changed_prefix):
    previous = [{'role': 'user', 'content': 'continue', '_state_db_row_id': 17},
                {'role': 'assistant', 'content': 'saved answer', '_state_db_row_id': 18},
                {'role': 'user', 'content': 'continue', '_state_db_row_id': 19}]
    result = [{'role': 'user', 'content': 'continue'},
              {'role': 'assistant', 'content': 'different answer' if changed_prefix else 'saved answer'},
              {'role': 'user', 'content': 'continue'}]
    restored = streaming._restore_reasoning_metadata_before_boundary(previous, result, current_turn_boundary=2)
    assert restored[0]['_state_db_row_id'] == 17
    if changed_prefix:
        assert '_state_db_row_id' not in restored[1]
    else:
        assert restored[1]['_state_db_row_id'] == 18
    assert '_state_db_row_id' not in restored[2]
