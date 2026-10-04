"""Accepted generations retain exact ownership when the initial save fails."""
import json
from types import SimpleNamespace

import pytest

import api.streaming as st


REGISTRIES = (
    '_STREAM_FALLBACK_NOTICES', '_STREAM_WORKER_SAVED',
    '_STREAM_CANCEL_CLAIMED', '_STREAM_SETTLEMENT_TERMINAL',
    '_STREAM_NOTICE_GENERATION', '_STREAM_SETTLEMENT_PARTICIPANTS',
    '_STREAM_SETTLEMENT_COMPLETED',
)


@pytest.mark.parametrize('same_content', [False, True])
@pytest.mark.parametrize('initial_notice', [False, True])
@pytest.mark.parametrize('retry', [False, True])
def test_initial_failure_transfers_latest_accepted_generation(
    tmp_path, same_content, initial_notice, retry,
):
    sid = 'initial-save-ownership'
    a = {'message': 'A', 'to_model': 'model', 'to_provider': 'provider'}
    b = dict(a, message='A' if same_content else 'B')
    path = tmp_path / 'durable.json'
    path.write_text(json.dumps([{'role': 'user', 'content': 'question'}]))
    baseline = path.read_bytes()
    row = {'role': 'assistant', 'content': 'answer'}
    session = SimpleNamespace(session_id='owner', profile=None, messages=[row])
    try:
        with st.STREAMS_LOCK:
            st.STREAMS[sid] = {'session_id': 'owner'}
        if initial_notice:
            assert st._publish_fallback_notice(sid, a, require_live_stream=True)
            row['_fallbackNotice'] = dict(a)
        generation = st._current_notice_generation(sid)
        for attempt in range(2 if retry else 1):
            with pytest.raises(OSError):
                with st._turn_final_save_commit(
                    sid, session, committed_generation=generation,
                    committed_notice=a if initial_notice else None, committed_row=row,
                ):
                    if attempt == 0:
                        assert st._publish_fallback_notice(sid, b, require_live_stream=True)
                    raise OSError('initial/retry disk failure')
        latest = generation + 1
        assert path.read_bytes() == baseline  # no failed write is declared durable
        assert sid not in st._STREAM_WORKER_SAVED
        # Observe the fence without introducing another generation into the race.
        fenced = sid in st._STREAM_SETTLEMENT_TERMINAL
        with st.STREAMS_LOCK:
            st.STREAMS.pop(sid)
        st._retire_worker_cancelled_state(sid)
        entry = st._STREAM_FALLBACK_DEAD_LETTER[sid]
        assert entry['notice'] == b
        assert entry['generation'] == latest
        assert entry['owner_session_id'] == 'owner'
        assert fenced
        assert not st._publish_fallback_notice(sid, {'message': 'late'}, require_live_stream=True)
        for name in REGISTRIES:
            assert sid not in getattr(st, name), name
        assert json.loads(path.read_text()) == [{'role': 'user', 'content': 'question'}]
    finally:
        with st.STREAMS_LOCK:
            st.STREAMS.pop(sid, None)
            for name in (*REGISTRIES, '_STREAM_FALLBACK_DEAD_LETTER'):
                registry = getattr(st, name)
                if isinstance(registry, set):
                    registry.discard(sid)
                else:
                    registry.pop(sid, None)


@pytest.mark.parametrize('retry_succeeds', [False, True])
@pytest.mark.parametrize('same_content', [False, True])
def test_cancelled_initial_failure_preserves_newer_owner(tmp_path, retry_succeeds, same_content):
    from unittest.mock import Mock
    from api import models, config

    sid, session_id = 'cancel-initial-race', 'cancel-initial-session'
    a = {'message': 'A', 'to_model': 'm', 'to_provider': 'p'}
    b = dict(a, message='A' if same_content else 'B')
    path = tmp_path / 'cancel.json'
    path.write_text('[]')
    ws = Mock(session_id=session_id, profile=None, active_stream_id=sid)
    ws.messages = []
    ws.pending_user_message = 'question'
    ws.pending_attachments = []
    ws.pending_started_at = 1.0
    ws.pending_user_source = None
    attempts = []

    def save():
        snapshot = json.dumps({'messages': ws.messages, 'active_stream_id': ws.active_stream_id})
        attempts.append(snapshot)
        if len(attempts) == 1:
            assert st._publish_fallback_notice(sid, b, require_live_stream=True)
            raise OSError('initial failure')
        if not retry_succeeds:
            raise OSError('retry failure')
        path.write_text(snapshot)

    ws.save.side_effect = save
    try:
        st.STREAMS[sid] = {'session_id': session_id}
        models.SESSIONS[session_id] = ws
        config.register_session_writeback_owner(session_id, sid)
        assert st._publish_fallback_notice(sid, a, require_live_stream=True)
        st._finalize_cancelled_turn(ws, stream_id=sid)
        assert len(attempts) == 2
        assert ws.active_stream_id is None
        if retry_succeeds:
            durable = json.loads(path.read_text())
            assert durable['active_stream_id'] is None
            assert any(row.get('_fallbackNotice') == a for row in durable['messages'])
            assert st._STREAM_WORKER_SAVED[sid] == 1
        else:
            assert json.loads(path.read_text()) == []
            assert sid not in st._STREAM_WORKER_SAVED
        assert not st._publish_fallback_notice(sid, {'message': 'late'}, require_live_stream=True)
        with st.STREAMS_LOCK:
            st.STREAMS.pop(sid)
        st._retire_worker_cancelled_state(sid)
        entry = st._STREAM_FALLBACK_DEAD_LETTER[sid]
        assert entry['notice'] == b
        assert entry['generation'] == 2
        for name in REGISTRIES:
            assert sid not in getattr(st, name), name
    finally:
        models.SESSIONS.pop(session_id, None)
        config.clear_session_writeback_owner_if_owned(session_id, sid)
        st.STREAMS.pop(sid, None)
        for name in (*REGISTRIES, '_STREAM_FALLBACK_DEAD_LETTER'):
            registry = getattr(st, name)
            if isinstance(registry, set):
                registry.discard(sid)
            else:
                registry.pop(sid, None)
