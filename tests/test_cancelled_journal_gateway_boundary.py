"""Cancellation owns its replay, not later durable Gateway turns."""
import copy

import pytest
from api import models


def _sidecar(live=False):
    return [
        {'role': 'user', 'content': 'STOP_OWNER', 'timestamp': 10},
        {'role': 'assistant', 'content': 'RECOVERED_ANSWER', 'timestamp': 11,
         '_recovered_from_cancel_journal': True, '_partial': live},
        {'role': 'assistant', 'content': 'Task cancelled.', 'timestamp': 12, '_error': True},
    ]


@pytest.mark.parametrize('live', [False, True])
@pytest.mark.parametrize('boundary', ['matched-owner', 'later-only', 'repeated-prompt', 'same-second'])
def test_cancelled_journal_replay_cutoff_is_shared_by_display_and_context(live, boundary):
    sidecar = _sidecar(live)
    state = [] if boundary == 'later-only' else [copy.deepcopy(sidecar[0])]
    state += [{'role': 'assistant', 'content': 'CANCELLED_REPLAY', 'timestamp': 13},
              {'role': 'tool', 'content': 'CANCELLED_TOOL', 'timestamp': 14}]
    user = 'STOP_OWNER' if boundary == 'repeated-prompt' else 'LATER_USER'
    stamp = 10 if boundary == 'same-second' else 15
    state += [{'role': 'user', 'content': user, 'timestamp': stamp},
              {'role': 'assistant', 'content': 'LATER_ANSWER', 'timestamp': stamp+1}]
    session = models.Session(session_id='cancel-boundary', messages=copy.deepcopy(sidecar),
                             context_messages=copy.deepcopy(sidecar[:2]))
    for prefer_context in (False, True):
        merged = models.reconciled_state_db_messages_for_session(
            session, prefer_context=prefer_context, state_messages=copy.deepcopy(state))
        contents = [row.get('content') for row in merged]
        assert 'CANCELLED_REPLAY' not in contents and 'CANCELLED_TOOL' not in contents
        # A terminal Stop owns its raw execution, including a live partial;
        # a proved later Gateway exchange remains part of provider/display history.
        assert contents[-2:] == [user, 'LATER_ANSWER']
    assert session.messages == sidecar


@pytest.mark.parametrize('shape', ['no-user', 'old-unmatched', 'missing-time', 'invalid-time',
                                   'ambiguous-owner', 'foreign-owner'])
def test_unproved_state_user_does_not_authorize_cancelled_replay(shape):
    sidecar = _sidecar()
    owner = copy.deepcopy(sidecar[0])
    if shape == 'no-user':
        state = []
    elif shape == 'ambiguous-owner':
        state = [owner, copy.deepcopy(owner), {'role': 'user', 'content': 'UNKNOWN', 'timestamp': 10}]
    elif shape == 'foreign-owner':
        sidecar[0]['message_id'] = 'correct-owner'
        owner['message_id'] = 'foreign-owner'
        state = [owner, {'role': 'user', 'content': 'UNKNOWN', 'timestamp': 10}]
    else:
        stamp = {'old-unmatched': 9, 'missing-time': None, 'invalid-time': 'bad'}[shape]
        state = [{'role': 'user', 'content': 'UNKNOWN', 'timestamp': stamp}]
    state.append({'role': 'assistant', 'content': 'UNPROVEN_REPLAY', 'timestamp': 20})
    merged = models.merge_session_messages_append_only(sidecar, state, incoming_provenance='state_db')
    assert merged == sidecar


def test_cancel_cutoff_keeps_existing_truncation_authority():
    sidecar = _sidecar()
    state = [copy.deepcopy(sidecar[0]),
             {'role': 'assistant', 'content': 'CANCELLED_REPLAY', 'timestamp': 11},
             {'role': 'user', 'content': 'DELETED_USER', 'timestamp': 15},
             {'role': 'assistant', 'content': 'DELETED_ANSWER', 'timestamp': 16}]
    merged = models.merge_session_messages_append_only(sidecar, state, truncation_watermark=12,
                                                       incoming_provenance='state_db')
    assert merged == sidecar


@pytest.mark.parametrize('later_error', [False, True])
def test_historical_cancel_cutoff_survives_later_rows_and_unrelated_error(later_error):
    sidecar = _sidecar()
    state = [copy.deepcopy(sidecar[0]),
             {'role': 'assistant', 'content': 'CANCELLED_REPLAY', 'timestamp': 20},
             {'role': 'user', 'content': 'LATER_USER', 'timestamp': 21, 'message_id': 'later-user'},
             {'role': 'assistant', 'content': 'LATER_ANSWER', 'timestamp': 22, 'message_id': 'later-answer'}]
    first = models.merge_session_messages_append_only(sidecar, state, incoming_provenance='state_db')
    assert [row['content'] for row in first][-2:] == ['LATER_USER', 'LATER_ANSWER']
    if later_error:
        first.append({'role': 'assistant', 'content': 'UNRELATED_ERROR', 'timestamp': 23, '_error': True})
    for _ in range(2):
        second = models.merge_session_messages_append_only(copy.deepcopy(first), copy.deepcopy(state),
                                                          incoming_provenance='state_db')
        assert second == first


def test_later_live_partial_veto_wins_over_historical_cancelled_journal():
    sidecar = _sidecar()+[
        {'role': 'user', 'content': 'LATER_USER', 'timestamp': 21},
        {'role': 'assistant', 'content': 'LIVE_PARTIAL', 'timestamp': 22, '_partial': True},
        {'role': 'assistant', 'content': 'ERROR', 'timestamp': 23, '_error': True},
    ]
    state = [copy.deepcopy(sidecar[0]),
             {'role': 'user', 'content': 'LATER_USER', 'timestamp': 21},
             {'role': 'assistant', 'content': 'LIVE_REPLAY', 'timestamp': 24}]
    assert models.merge_session_messages_append_only(sidecar, state, incoming_provenance='state_db') == sidecar


@pytest.mark.parametrize('previous_stop', [False, True])
@pytest.mark.parametrize('terminal_type', ['interrupted', 'provider_error'])
def test_typed_non_cancelled_partial_does_not_inherit_stop_successor_authority(previous_stop, terminal_type):
    sidecar = (_sidecar() if previous_stop else []) + [
        {'role': 'user', 'content': 'CRASH_OWNER', 'timestamp': 21},
        {'role': 'assistant', 'content': 'LIVE_PARTIAL', 'timestamp': 22, '_partial': True},
        {'role': 'assistant', 'content': 'ERROR', 'timestamp': 23, '_error': True, 'type': terminal_type},
    ]
    state = [
        {'role': 'user', 'content': 'CRASH_OWNER', 'timestamp': 21},
        {'role': 'assistant', 'content': 'RAW_REPLAY', 'timestamp': 24},
        {'role': 'user', 'content': 'LATER_USER', 'timestamp': 25},
        {'role': 'assistant', 'content': 'LATER_ANSWER', 'timestamp': 26},
    ]
    assert models.merge_session_messages_append_only(sidecar, state, incoming_provenance='state_db') == sidecar


@pytest.mark.parametrize('field', ['timestamp', '_ts'])
@pytest.mark.parametrize('value', [True, float('inf'), float('nan')])
def test_invalid_terminal_clock_does_not_authorize_later_only_store(field, value):
    sidecar = _sidecar()
    sidecar[-1].pop('timestamp')
    sidecar[-1][field] = value
    state = [{'role': 'user', 'content': 'UNPROVED', 'timestamp': 15},
             {'role': 'assistant', 'content': 'UNPROVED_REPLY', 'timestamp': 16}]
    assert models.merge_session_messages_append_only(sidecar, state, incoming_provenance='state_db') == sidecar


def test_historical_cancel_does_not_change_unverified_child_sidecar_stitching():
    sidecar = _sidecar()+[{'role': 'user', 'content': 'AFTER_STOP', 'timestamp': 20}]
    child = [{'role': 'assistant', 'content': 'CHILD_SIDECAR', 'timestamp': 21}]
    assert models.merge_session_messages_append_only(sidecar, child) == sidecar+child


def test_cancelled_tail_does_not_grant_unverified_source_state_authority():
    sidecar = _sidecar()
    child = [copy.deepcopy(sidecar[0]), {'role': 'user', 'content': 'UNVERIFIED_USER', 'timestamp': 20}]
    assert models.merge_session_messages_append_only(sidecar, child) == sidecar


@pytest.mark.parametrize('identity', ['message_id', '_state_db_row_id'])
def test_changed_known_owner_identity_is_not_a_later_only_successor(identity):
    sidecar = _sidecar()
    sidecar[0][identity] = '1'
    state = [{'role': 'user', 'content': 'CONFLICTING_OWNER', 'timestamp': 20, identity: '1'},
             {'role': 'assistant', 'content': 'CANCELLED_REPLAY', 'timestamp': 21}]
    assert models.merge_session_messages_append_only(sidecar, state, incoming_provenance='state_db') == sidecar


def test_visible_compaction_anchor_retains_full_read_cancel_owner_proof():
    sidecar = _sidecar()
    sidecar[1]['timestamp'] = 1000
    sidecar[2]['timestamp'] = 1001
    state = [copy.deepcopy(sidecar[0]), {'role': 'assistant', 'content': 'CANCELLED_REPLAY', 'timestamp': 11},
             {'role': 'user', 'content': 'LATER_USER', 'timestamp': 12},
             {'role': 'assistant', 'content': 'LATER_ANSWER', 'timestamp': 13},
             {'role': 'user', 'content': 'AFTER_COMPACTION_USER', 'timestamp': 14},
             {'role': 'assistant', 'content': 'AFTER_COMPACTION_ANSWER', 'timestamp': 15}]
    sidecar += copy.deepcopy(state[2:4])
    context = [{'role': 'assistant', 'content': '[Context compaction: prior work]',
                'timestamp': 1005, '_compressed_summary': True}]
    session = models.Session(session_id='cancel-compaction', messages=sidecar, context_messages=context,
                             compression_anchor_message_key={'role': 'assistant', 'text': 'LATER_ANSWER',
                                                             'ts': 13, 'attachments': 0})
    merged = models.reconciled_state_db_messages_for_session(session, prefer_context=True,
                                                            state_messages=copy.deepcopy(state))
    assert [row['content'] for row in merged] == [context[0]['content'], 'AFTER_COMPACTION_USER',
                                               'AFTER_COMPACTION_ANSWER']
