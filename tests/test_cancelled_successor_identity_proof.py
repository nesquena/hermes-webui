"""A new identical SQLite turn needs proof before it can be discarded."""
import copy

import pytest
from api import models
from api.helpers import public_session_projection
from tests.test_cancel_restart_journal_recovery import (
    _isolated_state,  # noqa: F401 - imported autouse isolation fixture
    _simulate_restart,
)
from tests.test_cancelled_journal_owner_occurrences import _recover
from tests.test_webui_state_db_reconciliation import _make_state_db


@pytest.mark.parametrize('prefer_context', [False, True])
@pytest.mark.parametrize('kind', ['new-identical', 'new-distinct', 'exact-mirror'])
def test_sidecar_only_successor_does_not_own_a_new_sqlite_pair(
    tmp_path, monkeypatch, prefer_context, kind,
):
    sid = 'successor-proof-'+str(prefer_context)+kind
    db = tmp_path/'state.db'
    monkeypatch.setattr(models, '_active_state_db_path', lambda: db)
    session, owner = _recover(sid)
    pair = [{'role': 'user', 'content': 'SAME_USER', 'timestamp': 20},
            {'role': 'assistant', 'content': 'SAME_ANSWER', 'timestamp': 21}]
    session.messages += copy.deepcopy(pair)
    session.context_messages += copy.deepcopy(pair)
    session.save(touch_updated_at=False)
    _simulate_restart()
    session = models.get_session(sid)
    incoming = copy.deepcopy(pair)
    if kind != 'exact-mirror':
        incoming[0]['timestamp'], incoming[1]['timestamp'] = 30, 31
    if kind == 'new-distinct':
        incoming[0]['content'], incoming[1]['content'] = 'NEW_USER', 'NEW_ANSWER'
    _make_state_db(db, sid, [owner,
        {'role': 'assistant', 'content': 'CANCELLED_REPLAY', 'timestamp': 11}, *incoming])
    result = models.reconciled_state_db_messages_for_session(session, prefer_context=prefer_context)
    contents = [row.get('content') for row in result]
    assert contents.count('SAME_USER') == contents.count('SAME_ANSWER') == (2 if kind == 'new-identical' else 1)
    assert 'CANCELLED_REPLAY' not in contents
    if kind == 'new-distinct':
        assert contents[-2:] == ['NEW_USER', 'NEW_ANSWER']
    # The default reader is unchanged, while admitted cancelled successors
    # retain private SQLite identity that public display/context do not expose.
    assert all('_state_db_row_id' not in row for row in models.get_state_db_session_messages(sid))
    public = public_session_projection({'messages': result, 'context_messages': result})
    assert all('_state_db_row_id' not in row for key in ['messages', 'context_messages'] for row in public[key])
    if kind != 'exact-mirror':
        assert all(row['_state_db_row_id'] > 0 for row in result[-2:])


@pytest.mark.parametrize('shape', ['no-clock', 'boolean-clock', 'nonfinite-clock', 'zero-row-id'])
def test_unknown_successor_identity_does_not_authorize_a_mirror(shape):
    sidecar = [{'role': 'user', 'content': 'OWNER', 'timestamp': 10},
               {'role': 'assistant', 'content': 'STOP', 'timestamp': 11,
                '_recovered_from_cancel_journal': True},
               {'role': 'assistant', 'content': 'Cancelled', 'timestamp': 12, '_error': True},
               {'role': 'user', 'content': 'SAME_USER', 'timestamp': 20},
               {'role': 'assistant', 'content': 'SAME_ANSWER', 'timestamp': 21}]
    state = copy.deepcopy(sidecar[-2:])
    for row in state:
        if shape == 'no-clock':
            row.pop('timestamp')
        elif shape == 'boolean-clock':
            row['timestamp'] = True
        elif shape == 'nonfinite-clock':
            row['timestamp'] = float('nan')
        else:
            row['timestamp'] += 10
            row['_state_db_row_id'] = 0
    if shape == 'zero-row-id':
        for row in sidecar[-2:]:
            row['_state_db_row_id'] = 0
    assert models._state_db_after_saved_cancel_successors(sidecar, state, sidecar) == state


@pytest.mark.parametrize('role', ['user', 'assistant'])
def test_private_row_identity_is_opt_in_to_the_shared_reader(tmp_path, monkeypatch, role):
    sid = 'opt-in-'+role
    db = tmp_path/'state.db'
    monkeypatch.setattr(models, '_active_state_db_path', lambda: db)
    _make_state_db(db, sid, [{'role': role, 'content': 'PLAIN', 'timestamp': 20}])
    plain = models.get_state_db_session_messages(sid)[0]
    private = models.get_state_db_session_messages(sid, include_row_identity=True)[0]
    assert set(plain) == {'role', 'content', 'timestamp'}
    assert private == {**plain, '_state_db_row_id': 1}
