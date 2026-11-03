"""Ordered cancellation ownership and persisted successor mirrors in SQLite."""
import copy
import sqlite3

import pytest
from api import models
from api.run_journal import RunJournalWriter
from api.streaming import cancel_stream
from tests.test_cancel_restart_journal_recovery import (
    _isolated_state,  # noqa: F401 - imported autouse isolation fixture
    _simulate_restart, _start_cancelled_turn,
)
from tests.test_webui_state_db_reconciliation import _make_state_db


def _recover(sid, earlier=None):
    stream = sid+'-run'
    session = _start_cancelled_turn(sid, stream)
    owner = {'role': 'user', 'content': session.pending_user_message,
             'timestamp': session.pending_started_at}
    session.messages = copy.deepcopy(earlier or [])+[owner]
    session.context_messages = copy.deepcopy(session.messages)
    session.save()
    assert cancel_stream(stream)
    RunJournalWriter(sid, stream).append_sse_event('token', {'text': 'STOP_OUTPUT'})
    _simulate_restart()
    return models.get_session(sid), owner


@pytest.mark.parametrize('prefer_context', [False, True])
@pytest.mark.parametrize('owner_shape', ['absent', 'restamped'])
def test_prior_plaintext_occurrence_cannot_prove_cancel_owner(
    tmp_path, monkeypatch, prefer_context, owner_shape,
):
    sid = 'prior-owner-'+owner_shape+str(prefer_context)
    db = tmp_path/'state.db'
    monkeypatch.setattr(models, '_active_state_db_path', lambda: db)
    earlier = [{'role': 'user', 'content': 'Do the cancellable task.', 'timestamp': 10},
               {'role': 'assistant', 'content': 'OLD_ANSWER', 'timestamp': 11},
               {'role': 'user', 'content': 'INTERVENING_USER', 'timestamp': 12},
               {'role': 'assistant', 'content': 'INTERVENING_ANSWER', 'timestamp': 13}]
    session, owner = _recover(sid, earlier)
    state = copy.deepcopy(earlier)
    if owner_shape == 'restamped':
        state.append({**owner, 'timestamp': 14})
    state += [{'role': 'assistant', 'content': 'CANCELLED_REPLAY', 'timestamp': 15},
              {'role': 'user', 'content': 'UNPROVED_USER', 'timestamp': 20},
              {'role': 'assistant', 'content': 'UNPROVED_ANSWER', 'timestamp': 21}]
    _make_state_db(db, sid, state)
    assert '_state_db_row_id' not in models.get_state_db_session_messages(sid)[0]
    original = copy.deepcopy(session.messages)
    for _ in range(2):
        merged = models.reconciled_state_db_messages_for_session(session, prefer_context=prefer_context)
        assert 'CANCELLED_REPLAY' not in [row.get('content') for row in merged]
        assert session.messages == original


@pytest.mark.parametrize('prefer_context', [False, True])
@pytest.mark.parametrize('copies', [1, 2])
@pytest.mark.parametrize('new_identical', [False, True])
def test_restamped_saved_successor_prefix_preserves_occurrence_count(
    tmp_path, monkeypatch, prefer_context, copies, new_identical,
):
    sid = 'saved-successor-'+str(prefer_context)+str(copies)+str(new_identical)
    db = tmp_path/'state.db'
    monkeypatch.setattr(models, '_active_state_db_path', lambda: db)
    session, owner = _recover(sid)
    pair = [{'role': 'user', 'content': 'LATER_USER', 'timestamp': 20},
            {'role': 'assistant', 'content': 'LATER_ANSWER', 'timestamp': 21}]
    saved = [{**row, 'timestamp': row['timestamp']+2*i}
             for i in range(copies) for row in pair]
    # Content-only restamping is ambiguous with a new identical turn. Build
    # real SQLite admission first, then save/cold-load its durable provenance.
    initial_state = [owner, {'role': 'assistant', 'content': 'CANCELLED_REPLAY', 'timestamp': 11}, *saved]
    _make_state_db(db, sid, initial_state)
    session.messages = models.reconciled_state_db_messages_for_session(session)
    session.context_messages = models.reconciled_state_db_messages_for_session(session, prefer_context=True)
    assert all(row.get('_state_db_row_id', 0) > 0 for row in session.messages[-len(saved):])
    session.save(touch_updated_at=False)
    _simulate_restart()
    session = models.get_session(sid)
    fresh = pair if new_identical else [
        {'role': 'user', 'content': 'FRESH_USER', 'timestamp': 60},
        {'role': 'assistant', 'content': 'FRESH_ANSWER', 'timestamp': 61}]
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE messages SET timestamp=timestamp+30 WHERE session_id=? AND id>2', (sid,))
        conn.executemany('INSERT INTO messages (session_id,role,content,timestamp) VALUES (?,?,?,?)',
                         [(sid, row['role'], row['content'], 60+i) for i, row in enumerate(fresh)])
    original = copy.deepcopy(session.messages)
    for _ in range(2):
        merged = models.reconciled_state_db_messages_for_session(session, prefer_context=prefer_context)
        contents = [row.get('content') for row in merged]
        assert contents.count('LATER_USER') == copies+int(new_identical)
        assert contents.count('LATER_ANSWER') == copies+int(new_identical)
        assert contents[-2:] == [row['content'] for row in fresh]
        assert 'CANCELLED_REPLAY' not in contents
        assert session.messages == original


@pytest.mark.parametrize('identity', ['message_id', '_state_db_row_id'])
def test_distinct_successor_ids_do_not_collapse_an_identical_new_turn(identity):
    sidecar = [{'role': 'user', 'content': 'OWNER', 'timestamp': 10},
               {'role': 'assistant', 'content': 'STOP', 'timestamp': 11,
                '_recovered_from_cancel_journal': True},
               {'role': 'assistant', 'content': 'Cancelled', 'timestamp': 12, '_error': True},
               {'role': 'user', 'content': 'SAME', 'timestamp': 20, identity: '1'},
               {'role': 'assistant', 'content': 'SAME_REPLY', 'timestamp': 21, identity: '2'}]
    state = [copy.deepcopy(sidecar[0]),
             {'role': 'assistant', 'content': 'CANCELLED_REPLAY', 'timestamp': 11},
             {'role': 'user', 'content': 'SAME', 'timestamp': 30, identity: '3'},
             {'role': 'assistant', 'content': 'SAME_REPLY', 'timestamp': 31, identity: '4'}]
    merged = models.merge_session_messages_append_only(sidecar, state, incoming_provenance='state_db')
    assert [row['content'] for row in merged].count('SAME') == 2
    assert merged[-2:] == state[-2:]


@pytest.mark.parametrize('identity', ['message_id', '_state_db_row_id', '_active_turn_token'])
def test_shared_owner_identity_disambiguates_an_earlier_legacy_tuple(identity):
    owner = {'role': 'user', 'content': 'REPEAT', 'timestamp': 10, identity: '3'}
    sidecar = [{'role': 'user', 'content': 'REPEAT', 'timestamp': 10},
               {'role': 'assistant', 'content': 'OLD_ANSWER', 'timestamp': 11}, owner,
               {'role': 'assistant', 'content': 'STOP', 'timestamp': 20,
                '_recovered_from_cancel_journal': True},
               {'role': 'assistant', 'content': 'Cancelled', 'timestamp': 21, '_error': True}]
    state = [copy.deepcopy(owner), {'role': 'assistant', 'content': 'CANCELLED_REPLAY', 'timestamp': 11},
             {'role': 'user', 'content': 'LATER_USER', 'timestamp': 12},
             {'role': 'assistant', 'content': 'LATER_ANSWER', 'timestamp': 13}]
    merged = models.merge_session_messages_append_only(sidecar, state, incoming_provenance='state_db')
    assert [row['content'] for row in merged][-2:] == ['LATER_USER', 'LATER_ANSWER']
    assert 'CANCELLED_REPLAY' not in [row['content'] for row in merged]


@pytest.mark.parametrize('restamped', [False, True])
def test_visible_saved_successors_remain_available_to_older_model_context(
    tmp_path, monkeypatch, restamped,
):
    sid = 'older-context-'+str(restamped)
    db = tmp_path/'state.db'
    monkeypatch.setattr(models, '_active_state_db_path', lambda: db)
    session, owner = _recover(sid)
    accepted = [{'role': 'user', 'content': 'LATER_USER', 'timestamp': 20},
                {'role': 'assistant', 'content': 'LATER_ANSWER', 'timestamp': 21}]
    session.messages += copy.deepcopy(accepted)
    session.save(touch_updated_at=False)
    _simulate_restart()
    session = models.get_session(sid)
    assert 'LATER_USER' not in [row.get('content') for row in session.context_messages]
    state = [owner, {'role': 'assistant', 'content': 'CANCELLED_REPLAY', 'timestamp': 11}]
    state += [{**row, 'timestamp': row['timestamp']+(30 if restamped else 0)} for row in accepted]
    _make_state_db(db, sid, state)
    merged = models.reconciled_state_db_messages_for_session(session, prefer_context=True)
    contents = [row.get('content') for row in merged]
    assert contents[-2:] == ['LATER_USER', 'LATER_ANSWER']
    assert contents.count('LATER_USER') == contents.count('LATER_ANSWER') == 1
    assert 'CANCELLED_REPLAY' not in contents
