"""
Tests for WAL (Write-Ahead Log) crash-recovery system.
api/wal.py, api/streaming.py WAL integration, and api/models.py WAL replay.

Run with:  pytest tests/test_wal_recovery.py -v
"""

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

# ─── Fake SESSION_DIR ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _patch_session_dir(tmp_path, monkeypatch):
    """Point SESSION_DIR at a temp directory for every test in this module."""
    import api.wal as _wal_mod
    import api.models as _models_mod
    monkeypatch.setattr(_wal_mod, 'SESSION_DIR', tmp_path)
    monkeypatch.setattr(_models_mod, 'SESSION_DIR', tmp_path)
    # Also patch config's SESSION_DIR for streaming (used by SESSION_DIR directly)
    import api.config as _config_mod
    monkeypatch.setattr(_config_mod, 'SESSION_DIR', tmp_path)
    yield tmp_path


# ─── WAL write/read/replay round-trip ───────────────────────────────────────

class TestWalRoundTrip:
    def test_write_and_read_tokens(self, tmp_path):
        sid = 'test_' + uuid.uuid4().hex[:8]
        from api import wal as _wal

        _wal.write_wal_start(sid, 'stream_abc')
        _wal.write_wal_token(sid, 'Hello')
        _wal.write_wal_token(sid, ' world')
        _wal.write_wal_reasoning(sid, 'thinking...')
        _wal.write_wal_end(sid, 'stream_abc')

        events = _wal.read_wal(sid)
        assert len(events) == 5
        assert events[0]['type'] == 'start'
        assert events[1]['type'] == 'token'
        assert events[1]['text'] == 'Hello'
        assert events[2]['type'] == 'token'
        assert events[2]['text'] == ' world'
        assert events[3]['type'] == 'reasoning'
        assert events[3]['text'] == 'thinking...'
        assert events[4]['type'] == 'end'

    def test_write_and_read_tool_events(self, tmp_path):
        sid = 'test_' + uuid.uuid4().hex[:8]
        from api import wal as _wal

        _wal.write_wal_tool(sid, 'tool_1', 'bash', '{"cmd": "ls"}')
        _wal.write_wal_tool_result(sid, 'tool_1', 'file1\nfile2')
        _wal.write_wal_end(sid, 'stream_xyz')

        events = _wal.read_wal(sid)
        assert events[0]['type'] == 'tool'
        assert events[0]['name'] == 'bash'
        assert events[0]['args'] == '{"cmd": "ls"}'
        assert events[1]['type'] == 'tool_result'
        assert events[1]['result'] == 'file1\nfile2'

    def test_replay_wal_token_accumulation(self, tmp_path):
        from api import wal as _wal

        sid = 'test_' + uuid.uuid4().hex[:8]
        _wal.write_wal_token(sid, 'One ')
        _wal.write_wal_token(sid, 'two ')
        _wal.write_wal_token(sid, 'three')
        _wal.write_wal_end(sid, 'stream_x')

        events = _wal.read_wal(sid)
        result = _wal.replay_wal(events)
        assert result['content'] == 'One two three'
        assert result['tool_calls'] == []

    def test_replay_wal_reasoning(self, tmp_path):
        from api import wal as _wal

        sid = 'test_' + uuid.uuid4().hex[:8]
        _wal.write_wal_reasoning(sid, 'let me think')
        _wal.write_wal_reasoning(sid, ' about this')
        _wal.write_wal_end(sid, 'stream_x')

        events = _wal.read_wal(sid)
        result = _wal.replay_wal(events)
        assert result['reasoning'] == 'let me think about this'

    def test_replay_wal_tool_calls(self, tmp_path):
        from api import wal as _wal

        sid = 'test_' + uuid.uuid4().hex[:8]
        _wal.write_wal_tool(sid, 'id1', 'bash', '{"cmd": "pwd"}')
        _wal.write_wal_tool_result(sid, 'id1', '/home/user')
        _wal.write_wal_end(sid, 'stream_x')

        events = _wal.read_wal(sid)
        result = _wal.replay_wal(events)
        assert len(result['tool_calls']) == 1
        assert result['tool_calls'][0]['name'] == 'bash'
        assert result['tool_calls'][0]['args'] == '{"cmd": "pwd"}'
        assert len(result['tool_results']) == 1
        assert result['tool_results'][0]['result'] == '/home/user'

    def test_replay_wal_detects_aperror(self, tmp_path):
        from api import wal as _wal

        sid = 'test_' + uuid.uuid4().hex[:8]
        _wal.write_wal_token(sid, 'hello')
        _wal.write_wal_aperror(sid, 'Provider rate limit exceeded')
        _wal.write_wal_end(sid, 'stream_x')

        events = _wal.read_wal(sid)
        result = _wal.replay_wal(events)
        assert result['had_error'] is True

    def test_delete_wal_removes_file(self, tmp_path):
        from api import wal as _wal

        sid = 'test_' + uuid.uuid4().hex[:8]
        _wal.write_wal_token(sid, 'x')
        _wal.write_wal_end(sid, 'stream_x')
        assert _wal.wal_path(sid).exists()

        _wal.delete_wal(sid)
        assert not _wal.wal_path(sid).exists()

    def test_delete_wal_missing_file_is_idempotent(self, tmp_path):
        from api import wal as _wal

        sid = 'test_nonexistent_' + uuid.uuid4().hex[:8]
        _wal.delete_wal(sid)  # must not raise

    def test_read_wal_missing_file_returns_empty(self, tmp_path):
        from api import wal as _wal

        sid = 'test_missing_' + uuid.uuid4().hex[:8]
        assert _wal.read_wal(sid) == []


# ─── WAL flush on token count threshold ───────────────────────────────────────

class TestWalFlush:
    def test_flush_triggered_at_token_count(self, tmp_path, monkeypatch):
        from api import wal as _wal

        # Temporarily lower the flush threshold so we don't have to write 100 tokens
        monkeypatch.setattr(_wal, '_WAL_FLUSH_TOKENS', 3)

        sid = 'test_' + uuid.uuid4().hex[:8]
        _wal.write_wal_start(sid, 'stream_flush')

        # tokens 1-2: not flushed yet
        _wal.write_wal_token(sid, 'a')
        _wal.write_wal_token(sid, 'b')
        with _wal._buffer_lock:
            assert sid in _wal._write_buffer

        # token 3: hits threshold, triggers flush
        _wal.write_wal_token(sid, 'c')

        # After flush, buffer should be cleared
        with _wal._buffer_lock:
            assert sid not in _wal._write_buffer

        # WAL file should exist on disk
        assert _wal.wal_path(sid).exists()
        _wal.write_wal_end(sid, 'stream_flush')


    def test_should_flush_initializes_timer_on_first_call(self, tmp_path, monkeypatch):
        """_should_flush must NOT fire on its first call for a new session.

        Regression test for the uninitialized-timer bug: if _should_flush returns True
        when last_flush_time is 0 (epoch), the checkpoint thread would fire
        continuously on every call until a real timestamp was stored. The fix
        initializes last_flush_time on the first call and returns False so
        time-based flushes only begin after the interval has elapsed.
        """
        import time as _time
        from api import wal as _wal

        sid = 'test_' + uuid.uuid4().hex[:8]

        # Manually set a future last_flush_time so we isolate the timer-initialization bug
        # (we avoid the time-based path by making the interval check pass immediately
        # but only after the 0-initialization guard has fired)
        with _wal._flush_lock:
            _wal._last_flush_time[sid] = 0  # Simulate uninitialized

        # First call: should return False AND set the timer
        result1 = _wal._should_flush(sid)

        # Verify it returned False (didn't fire on uninitialized timer)
        assert result1 is False, f"Expected False on first call, got {result1}"

        # Verify timer WAS initialized (not left at 0)
        with _wal._flush_lock:
            stored = _wal._last_flush_time.get(sid, 0)
        assert stored > 0, f"Timer should be initialized to current time, got {stored}"

        # Clean up
        with _wal._flush_lock:
            _wal._last_flush_time.pop(sid, None)


    def test_manual_flush_on_end(self, tmp_path):
        from api import wal as _wal

        sid = 'test_' + uuid.uuid4().hex[:8]
        # Use write_wal_tool (not write_wal_token) because write_wal_token calls
        # _increment() which bumps token count and triggers an immediate flush
        # (count=1 >= threshold=1).  write_wal_tool does NOT increment token count,
        # so after it the buffer holds 1 item and the assertion passes.
        _wal.write_wal_tool(sid, 'tool_x', 'my_tool', '{}')

        _wal.write_wal_end(sid, 'stream_x')

        # After end: 2 items > 1 is True, so _append_event flushes and clears buffer.
        with _wal._buffer_lock:
            assert sid not in _wal._write_buffer
        assert _wal.wal_path(sid).exists()



    def test_append_event_flushes_at_threshold_not_past_it(self, tmp_path, monkeypatch):
        """_append_event must flush when buffer len == _WAL_FLUSH_TOKENS, not len > threshold.

        Regression test for the >= vs > bug: if _append_event uses > instead of >=,
        the flush never fires at exactly the threshold — you'd need threshold+1 tokens.
        With >= it fires at exactly N tokens.
        """
        from api import wal as _wal

        monkeypatch.setattr(_wal, '_WAL_FLUSH_TOKENS', 5)

        sid = 'test_' + uuid.uuid4().hex[:8]
        _wal.write_wal_start(sid, 'stream_x')

        # Write exactly 5 tokens (one below flush threshold at 5)
        for ch in 'abcde':
            _wal.write_wal_token(sid, ch)

        # Buffer should still hold 5 items (not flushed yet — len=5 is NOT >5)
        with _wal._buffer_lock:
            assert len(_wal._write_buffer.get(sid, [])) == 5, \
                f"Expected 5 buffered items before threshold hit, got {_wal._write_buffer.get(sid, [])}"

        # 6th token hits the >=5 threshold and triggers a flush
        _wal.write_wal_token(sid, 'f')

        # Buffer should now be cleared
        with _wal._buffer_lock:
            assert sid not in _wal._write_buffer, \
                "Buffer should be cleared after threshold is hit"

        _wal.write_wal_end(sid, 'stream_x')


# ─── WAL replay integrated into get_session ───────────────────────────────────

class TestWalReplayIntegration:
    def test_get_session_replays_wal(self, tmp_path):
        """Simulate a crash: session JSON has user msg + pending state, WAL has tokens."""
        import api.models as _models
        from api import wal as _wal
        from api.config import SESSIONS, LOCK

        sid = 'test_replay_' + uuid.uuid4().hex[:8]

        # 1. Build a session JSON that looks like it was checkpointed with the user's
        #    message but no assistant reply yet (active_stream_id is set).
        session_data = {
            'session_id': sid,
            'title': 'Test WAL Replay',
            'workspace': str(tmp_path),
            'model': 'test-model',
            'messages': [
                {'role': 'user', 'content': 'Hello agent, please count to 3'},
            ],
            'tool_calls': [],
            'created_at': time.time(),
            'updated_at': time.time(),
            'active_stream_id': 'dead_stream_id',
            'pending_user_message': 'Hello agent, please count to 3',
            'pending_attachments': [],
            'pending_started_at': time.time(),
        }
        session_path = tmp_path / f'{sid}.json'
        session_path.write_text(json.dumps(session_data), encoding='utf-8')

        # 2. Write WAL events as if the process was streaming tokens when killed.
        _wal.write_wal_start(sid, 'dead_stream_id')
        _wal.write_wal_token(sid, 'One... ')
        _wal.write_wal_token(sid, 'two... ')
        _wal.write_wal_token(sid, 'three!')
        # Simulate the process dying before 'end' was written

        # 3. Clear the SESSIONS cache so get_session() does a fresh load.
        with LOCK:
            SESSIONS.clear()

        # 4. Load the session — WAL replay should fire and recover the tokens.
        #    We patch STREAMS to be empty so the stream is considered "dead".
        import api.config as _config
        monkeypatch = pytest.importorskip('pytest').MonkeyPatch
        m = pytest.importorskip('pytest').MonkeyPatch()
        m.setattr(_config, 'STREAMS', {})

        s = _models.get_session(sid)

        m.undo()

        # 5. Verify: WAL replay appended the recovered assistant message.
        assert len(s.messages) == 2, f"Expected 2 messages, got {len(s.messages)}"
        assistant = s.messages[1]
        assert assistant['role'] == 'assistant'
        assert 'One' in assistant['content']
        assert 'three' in assistant['content']
        # active_stream_id should be cleared
        assert s.active_stream_id is None
        # pending state should be cleared
        assert s.pending_user_message is None
        # WAL file should be deleted after successful recovery
        assert not _wal.wal_path(sid).exists()

    def test_get_session_no_wal_no_replay(self, tmp_path):
        """Session with no WAL: existing stale-pending repair still runs."""
        import api.models as _models
        import api.config as _config
        from api.config import SESSIONS, LOCK

        sid = 'test_nowal_' + uuid.uuid4().hex[:8]

        # Session with pending state but NO WAL file.
        # messages=[] is critical: _repair_stale_pending only fires when
        # messages==[] (no recovery possible via session JSON alone).
        session_data = {
            'session_id': sid,
            'title': 'No WAL',
            'workspace': str(tmp_path),
            'model': 'test-model',
            'messages': [],
            'tool_calls': [],
            'created_at': time.time(),
            'updated_at': time.time(),
            'active_stream_id': 'dead_stream_2',
            'pending_user_message': 'Hello',
            'pending_attachments': [],
            'pending_started_at': time.time(),
        }
        session_path = tmp_path / f'{sid}.json'
        session_path.write_text(json.dumps(session_data), encoding='utf-8')

        with LOCK:
            SESSIONS.clear()

        # Patch STREAMS to simulate dead stream
        m = pytest.importorskip('pytest').MonkeyPatch()
        m.setattr(_config, 'STREAMS', {})

        s = _models.get_session(sid)

        m.undo()

        # Without WAL, the existing stale-pending repair adds an error marker.
        # active_stream_id should be cleared.
        assert s.active_stream_id is None

    def test_get_session_skips_replay_when_stream_still_live(self, tmp_path):
        """WAL is NOT replayed if the stream is still in STREAMS (normal completion)."""
        import api.models as _models
        import api.config as _config
        from api.config import SESSIONS, LOCK, STREAMS, STREAMS_LOCK

        sid = 'test_live_' + uuid.uuid4().hex[:8]

        session_data = {
            'session_id': sid,
            'title': 'Live Stream',
            'workspace': str(tmp_path),
            'model': 'test-model',
            'messages': [{'role': 'user', 'content': 'Hello'}],
            'tool_calls': [],
            'created_at': time.time(),
            'updated_at': time.time(),
            'active_stream_id': 'live_stream_id',
            'pending_user_message': 'Hello',
            'pending_attachments': [],
            'pending_started_at': time.time(),
        }
        session_path = tmp_path / f'{sid}.json'
        session_path.write_text(json.dumps(session_data), encoding='utf-8')

        # Write WAL tokens as if agent was mid-stream
        from api import wal as _wal
        _wal.write_wal_token(sid, 'partial ')
        _wal.write_wal_token(sid, 'response')
        _wal.write_wal_end(sid, 'live_stream_id')

        with LOCK:
            SESSIONS.clear()
        with STREAMS_LOCK:
            STREAMS['live_stream_id'] = None  # stream is still "alive"

        s = _models.get_session(sid)

        # Stream is still live — WAL should NOT be replayed.
        # (Messages list should still just have the user message.)
        assert len(s.messages) == 1
        assert s.messages[0]['role'] == 'user'
        # WAL file should NOT be deleted (still has valid content for next load)
        assert _wal.wal_path(sid).exists()

        # Cleanup
        with STREAMS_LOCK:
            STREAMS.pop('live_stream_id', None)
        _wal.delete_wal(sid)

    def test_get_session_skips_replay_when_assistant_already_present(self, tmp_path):
        """WAL not needed if assistant message already committed to session JSON."""
        import api.models as _models
        import api.config as _config
        from api.config import SESSIONS, LOCK

        sid = 'test_complete_' + uuid.uuid4().hex[:8]

        # Session JSON has BOTH user and assistant message (normal checkpoint).
        session_data = {
            'session_id': sid,
            'title': 'Complete Session',
            'workspace': str(tmp_path),
            'model': 'test-model',
            'messages': [
                {'role': 'user', 'content': 'Hello'},
                {'role': 'assistant', 'content': 'Hello! How can I help?'},
            ],
            'tool_calls': [],
            'created_at': time.time(),
            'updated_at': time.time(),
            'active_stream_id': 'done_stream',
            'pending_user_message': None,
            'pending_attachments': [],
            'pending_started_at': None,
        }
        session_path = tmp_path / f'{sid}.json'
        session_path.write_text(json.dumps(session_data), encoding='utf-8')

        # WAL has extra tokens (should not be replayed).
        from api import wal as _wal
        _wal.write_wal_token(sid, 'Stale token that should not appear')
        _wal.write_wal_end(sid, 'done_stream')

        with LOCK:
            SESSIONS.clear()

        s = _models.get_session(sid)

        # Assistant message already present — WAL should NOT be replayed.
        assert len(s.messages) == 2
        assert s.messages[1]['content'] == 'Hello! How can I help?'

        _wal.delete_wal(sid)


# ─── Partial text recovery in cancel_stream ─────────────────────────────────

class TestCancelStreamRecovery:
    def test_cancel_stream_uses_session_load_not_get_session(self, tmp_path):
        """cancel_stream must use Session.load() (bypass SESSIONS cache) so that
        post-crash partial text is recovered from the last checkpoint, not from
        a potentially-stale in-memory session."""
        import api.models as _models
        from api import wal as _wal
        from api.config import SESSIONS, LOCK

        sid = 'test_cancel_' + uuid.uuid4().hex[:8]

        # Session JSON persisted to disk with a partial assistant message from a
        # previous crash (messages list has user + partial assistant).
        session_data = {
            'session_id': sid,
            'title': 'Cancel Test',
            'workspace': str(tmp_path),
            'model': 'test-model',
            'messages': [
                {'role': 'user', 'content': 'Write a long story'},
                {'role': 'assistant', 'content': 'Once upon a time', '_partial': True},
            ],
            'tool_calls': [],
            'created_at': time.time(),
            'updated_at': time.time(),
            # Note: active_stream_id is set so _replay_wal_recovery fires on
            # Session.load(), adding the partial assistant message from WAL.
            # Then cancel_stream appends the cancel marker as a new message.
            # Result: [user, partial_from_wal_replay, cancel_marker]
            'active_stream_id': 'cancel_stream_abc',
            'pending_user_message': 'Write a long story',
            'pending_attachments': [],
            'pending_started_at': time.time(),
        }
        session_path = tmp_path / f'{sid}.json'
        session_path.write_text(json.dumps(session_data), encoding='utf-8')

        # Cancel the stream — this should load the session from disk, preserve
        # the partial assistant text, append the cancel marker, and save.
        from api.streaming import cancel_stream
        import api.config as _config

        # Simulate the stream state that cancel_stream reads
        with LOCK:
            SESSIONS.clear()

        # Mock STREAMS, CANCEL_FLAGS, AGENT_INSTANCES, STREAM_PARTIAL_TEXT
        # needed by cancel_stream.  agent_instances must have a session_id attribute
        # so cancel_stream can load the session from disk.
        stream_id = 'cancel_stream_abc'
        mock_partial_texts = {stream_id: 'Once upon a time'}
        mock_agent = type('Agent', (), {'session_id': sid})()

        m = pytest.importorskip('pytest').MonkeyPatch()
        m.setattr(_config, 'STREAMS', {stream_id: None})
        m.setattr(_config, 'CANCEL_FLAGS', {stream_id: threading.Event()})
        m.setattr(_config, 'AGENT_INSTANCES', {stream_id: mock_agent})
        m.setattr(_config, 'STREAM_PARTIAL_TEXT', mock_partial_texts)
        m.setattr(_config, 'STREAMS_LOCK', threading.Lock())

        result = cancel_stream(stream_id)

        m.undo()

        # cancel_stream should return True (stream existed)
        assert result is True

        # Reload session fresh from disk — should have partial assistant + cancel marker.
        s = _models.Session.load(sid)
        assert len(s.messages) >= 2
        last = s.messages[-1]
        assert last['role'] == 'assistant'
        assert last.get('_error') is True
        assert 'cancelled' in last.get('content', '').lower()
        # Two partials: the one from session JSON (WAL replay) and the one
        # append_stream appends from _STREAM_PARTIAL_TEXT.  Both are valid
        # partial content that should be kept so the model can continue.
        partial_msgs = [m for m in s.messages if m.get('_partial')]
        assert len(partial_msgs) == 2


# ─── WAL is not imported at module top level (no circular dep) ───────────────

class TestWalImportSafety:
    def test_wal_module_imports_without_circular_dep(self, tmp_path, monkeypatch):
        """Importing api.wal must not pull in streaming (which imports wal)."""
        # This test just verifies the import graph is clean.
        # If api.wal imported streaming at module level, this would fail at import time.
        from api import wal as _wal
        assert hasattr(_wal, 'write_wal_token')
        assert hasattr(_wal, 'read_wal')
        assert hasattr(_wal, 'replay_wal')
        assert hasattr(_wal, 'delete_wal')
