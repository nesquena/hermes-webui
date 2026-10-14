"""Production worker preserves repeated prompts with older persistence shapes.

Compact local producers exercise indexed, object-tracked and marker-tracked
flushes. Independent verification also executes immutable historical methods.
"""
import copy
import queue

import pytest

from api import config, models, profiles, routes, session_ops, streaming
from tests.test_cancel_restart_journal_recovery import _isolated_state  # noqa: F401
from tests.test_cancelled_journal_owner_occurrences import _recover
from tests.test_legacy_agent_sqlite_identity import LegacyAgent, SQLiteStore
from tests.test_webui_state_db_reconciliation import _make_state_db


@pytest.mark.parametrize('shape', ['indexed', 'objects', 'timestamp-objects', 'timestamp-markers'])
@pytest.mark.parametrize('prompts', [['continue', 'X', 'continue', 'Y'], ['Q2', 'Q3', 'Q4', 'Q5']],
                         ids=['repeated', 'distinct'])
def test_old_persistence_shapes_keep_each_turn_in_actual_worker(tmp_path, monkeypatch, shape, prompts):
    sid = 'old-worker-'+shape+('-repeat' if prompts[0] == 'continue' else '-distinct')
    path = tmp_path/'state.db'
    monkeypatch.setattr(models, '_active_state_db_path', lambda: path)
    session, owner = _recover(sid)
    _make_state_db(path, sid, [owner, {'role': 'assistant', 'content': 'CANCELLED_REPLAY', 'timestamp': 11}])
    histories, writes = [], []

    class Agent(LegacyAgent):
        def __init__(self, **kwargs):
            super().__init__(sid, SQLiteStore(path))
            self.context_compressor = self.ephemeral_system_prompt = None
            self.platform, self.model = 'webui', 'test-model'
            self._flushed_objects = set()
            # ``id(row)`` is only unique while the dict is alive. Keep the
            # rows alive for every ID retained by this historical mock so a
            # recycled CPython address cannot suppress a later turn.
            self._flushed_object_refs = {}

        def local_provider(self, prompt, history, clean, timestamp):
            histories.append(copy.deepcopy(history))
            self._persist_user_message_idx = len(history)
            self._persist_user_message_override = clean
            result = list(history)+[{'role': 'user', 'content': prompt}]
            # Historical _persist_session retains the live list before flush.
            self._session_messages = result
            if shape != 'timestamp-markers':
                result[len(history)]['content'] = clean
            # Timestamp-era Agents also persist the user before the provider
            # call. Marker flushes write the clean override only to SQLite.
            if shape.startswith('timestamp-'):
                self._flush_messages_to_session_db(result, history)
                assert result[-1].get('_state_db_row_id', 0) > 0
            result.append({'role': 'assistant', 'content': 'ANSWER_'+str(len(histories))})
            # Their finalizer cleans the live user before its final persist.
            result[len(history)]['content'] = clean
            self._flush_messages_to_session_db(result, history)
            writes.append(copy.deepcopy(result[-2:]))
            return {'completed': True, 'final_response': result[-1]['content'],
                    'messages': result, 'current_turn_user_idx': len(history)}

        def run_conversation(self, user_message, system_message, conversation_history, task_id, persist_user_message):
            return self.local_provider(user_message, conversation_history, persist_user_message, None)

        def _flush_messages_to_session_db(self, messages, conversation_history=None):
            if shape == 'indexed':
                return super()._flush_messages_to_session_db(messages, conversation_history)
            history_ids = {id(row) for row in conversation_history or []}
            for index, row in enumerate(messages):
                if id(row) in history_ids:
                    continue
                if shape == 'timestamp-markers' and row.get('_db_persisted'):
                    continue
                if shape != 'timestamp-markers' and id(row) in self._flushed_objects:
                    # A seen ID must still identify this same live object, not
                    # a fresh row whose address CPython happened to recycle.
                    assert self._flushed_object_refs[id(row)] is row
                    continue
                content = row['content']
                if shape == 'timestamp-markers' and index == self._persist_user_message_idx:
                    content = self._persist_user_message_override
                self._session_db.append_message(session_id=sid, role=row['role'], content=content)
                if shape == 'timestamp-markers':
                    row['_db_persisted'] = True
                else:
                    row_id = id(row)
                    self._flushed_objects.add(row_id)
                    self._flushed_object_refs[row_id] = row
            self._last_flushed_db_idx = len(messages)

    if shape.startswith('timestamp-'):
        def timestamp_run(self, user_message, system_message, conversation_history, task_id,
                          persist_user_message, persist_user_timestamp=None):
            return self.local_provider(user_message, conversation_history, persist_user_message, persist_user_timestamp)
        Agent.run_conversation = timestamp_run
    monkeypatch.setattr(streaming, '_get_ai_agent', lambda: Agent)
    # This producer owns its local SQLite writer. The optional native search
    # DB is a separate service; do not replace that writer on cached turns.
    monkeypatch.setattr(streaming, '_build_session_db_for_stream', lambda _: None)
    monkeypatch.setattr(streaming, 'resolve_model_provider', lambda *a, **k: ('test-model', None, None))
    monkeypatch.setattr(streaming, 'get_config', lambda: {})
    monkeypatch.setattr(config, 'get_config', lambda: {})
    monkeypatch.setattr(config, '_resolve_cli_toolsets', lambda *a, **k: [])
    monkeypatch.setattr(profiles, 'get_active_hermes_home', lambda: tmp_path)
    config.SESSION_AGENT_CACHE.clear()
    def transcript_values(rows):
        values = []
        for row in rows:
            text = row.get('content')
            if row.get('role') == 'user':
                text = next((prompt for prompt in prompts
                             if streaming._submitted_user_text_matches(text, prompt)), text)
            values.append(text)
        return values

    try:
        for number, prompt in enumerate(prompts, 2):
            stream = sid+'-run-'+str(number)
            routes._prepare_chat_start_session_for_stream(
                session, msg=prompt, attachments=[], workspace=str(tmp_path), model='test-model',
                model_provider=None, stream_id=stream, started_at=number*10.0)
            models.SESSIONS[sid] = session
            config.STREAMS[stream] = queue.Queue()
            streaming._run_agent_streaming(sid, prompt, 'test-model', str(tmp_path), stream, [])
            assert len(histories) == number-1
            session = models.Session.load(sid)
            assert all(row.get('_state_db_row_id', 0) > 0 for row in writes[-1])
            expected = [text for i, value in enumerate(prompts[:number-1], 1)
                        for text in (value, 'ANSWER_'+str(i))]
            for rows in [session.messages, session.context_messages,
                         *session_ops.regeneration_state(session, use_sidecar=True)]:
                actual = [text for text in transcript_values(rows) if text in set(expected)]
                assert actual == expected
                assert 'CANCELLED_REPLAY' not in [row.get('content') for row in rows]
                # Historical provider-prefix metadata must survive sanitizing,
                # settlement and cold load, not just the newest flush batch.
                assert all(row.get('_state_db_row_id', 0) > 0 for row in rows
                           if row.get('content') in {'ANSWER_'+str(i) for i in range(1, number)})
            # The next invocation receives each prior completed exchange once.
            if number > 2:
                prior = expected[:-2]
                assert [text for text in transcript_values(histories[-1]) if text in set(expected)] == prior
    finally:
        config.SESSION_AGENT_CACHE.clear()
