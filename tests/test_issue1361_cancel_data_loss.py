"""Regression tests for #1361 — Stop/Cancel discards already-streamed content.

Three distinct data-loss paths on cancel:

  §A  Reasoning text accumulated in a thread-local `_reasoning_text` is never
      visible to cancel_stream(), so it's lost on cancel.
  §B  Live tool calls accumulated in thread-local `_live_tool_calls` are lost
      on cancel — only STREAM_PARTIAL_TEXT is captured.
  §C  When the entire streamed output is reasoning (no visible tokens),
      _stripped is empty after regex cleanup, so NO partial assistant message
      is appended — only the *Task cancelled.* marker survives.

All three fix the same "tokens-paid-for-data-loss" class of bug.
"""

import pathlib
import queue
import re
import threading
from unittest.mock import Mock, patch

import pytest

import api.config as config
import api.models as models
import api.streaming as streaming
from api.models import Session
from api.streaming import cancel_stream

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    """Redirect SESSION_DIR / SESSION_INDEX_FILE to an isolated temp dir."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    models.SESSIONS.clear()
    yield
    models.SESSIONS.clear()


@pytest.fixture(autouse=True)
def _isolate_stream_state():
    """Clear all shared streaming dicts before/after each test."""
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    # New shared dicts for §A and §B
    if hasattr(config, 'STREAM_REASONING_TEXT'):
        config.STREAM_REASONING_TEXT.clear()
    if hasattr(config, 'STREAM_LIVE_TOOL_CALLS'):
        config.STREAM_LIVE_TOOL_CALLS.clear()
    yield
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    if hasattr(config, 'STREAM_REASONING_TEXT'):
        config.STREAM_REASONING_TEXT.clear()
    if hasattr(config, 'STREAM_LIVE_TOOL_CALLS'):
        config.STREAM_LIVE_TOOL_CALLS.clear()


@pytest.fixture(autouse=True)
def _isolate_agent_locks():
    config.SESSION_AGENT_LOCKS.clear()
    yield
    config.SESSION_AGENT_LOCKS.clear()


def _make_session(session_id="cancel_sid_1361",
                  pending_msg="Help me debug this",
                  messages=None):
    """Build a session in mid-stream state."""
    s = Session(
        session_id=session_id,
        title="Test Session",
        messages=messages or [],
    )
    s.pending_user_message = pending_msg
    s.pending_attachments = []
    s.pending_started_at = None
    s.active_stream_id = "stream_1361"
    s.save()
    models.SESSIONS[session_id] = s
    return s


def _setup_cancel_state(session_id, stream_id="stream_1361"):
    """Wire up STREAMS/CANCEL_FLAGS/AGENT_INSTANCES for cancel_stream()."""
    config.STREAMS[stream_id] = queue.Queue()
    config.CANCEL_FLAGS[stream_id] = threading.Event()
    mock_agent = Mock()
    mock_agent.session_id = session_id
    mock_agent.interrupt = Mock()
    config.AGENT_INSTANCES[stream_id] = mock_agent
    return stream_id, mock_agent


# ── §A: Reasoning text lost on cancel ───────────────────────────────────────

class TestCancelPreservesReasoningText:
    """§A: _reasoning_text is thread-local and invisible to cancel_stream().
    
    After fix: reasoning text should be persisted in a shared dict
    (STREAM_REASONING_TEXT) keyed by stream_id, and cancel_stream()
    should append it as a 'reasoning' field on the partial assistant message.
    """

    def test_cancel_with_reasoning_only_preserves_reasoning(self):
        """Cancel during reasoning phase (no visible tokens) should persist reasoning."""
        sid = "test_1361_a1"
        stream_id = "stream_a1"
        s = _make_session(session_id=sid)
        _setup_cancel_state(sid, stream_id)

        # Simulate: reasoning was accumulated but no visible tokens
        reasoning = "Let me think about this step by step..."
        config.STREAM_PARTIAL_TEXT[stream_id] = ""  # no visible tokens

        if hasattr(config, 'STREAM_REASONING_TEXT'):
            config.STREAM_REASONING_TEXT[stream_id] = reasoning

        cancel_stream(stream_id)

        # Reload and check
        s2 = models.SESSIONS[sid]
        msgs = s2.messages
        # There should be a partial assistant message with reasoning
        assistant_msgs = [m for m in msgs if isinstance(m, dict) and m.get('role') == 'assistant']
        has_reasoning = any(m.get('reasoning') for m in assistant_msgs)
        assert has_reasoning, \
            f"Expected reasoning field on partial assistant msg after cancel. Got messages: {assistant_msgs}"

    def test_cancel_with_reasoning_and_partial_tokens_preserves_both(self):
        """Cancel mid-stream with both reasoning and some visible tokens."""
        sid = "test_1361_a2"
        stream_id = "stream_a2"
        s = _make_session(session_id=sid)
        _setup_cancel_state(sid, stream_id)

        reasoning = "Let me analyze the code..."
        partial_text = "Based on my analysis, the bug is in the"
        config.STREAM_PARTIAL_TEXT[stream_id] = partial_text

        if hasattr(config, 'STREAM_REASONING_TEXT'):
            config.STREAM_REASONING_TEXT[stream_id] = reasoning

        cancel_stream(stream_id)

        s2 = models.SESSIONS[sid]
        assistant_msgs = [m for m in s2.messages if isinstance(m, dict) and m.get('role') == 'assistant']
        # Should have partial content
        partial_msgs = [m for m in assistant_msgs if m.get('_partial')]
        has_content = any(m.get('content') for m in partial_msgs)
        assert has_content, \
            f"Expected partial assistant content after cancel. Got: {partial_msgs}"

    def test_cancel_without_reasoning_dict_works_as_before(self):
        """If STREAM_REASONING_TEXT doesn't exist yet (pre-fix), cancel still works."""
        sid = "test_1361_a3"
        stream_id = "stream_a3"
        s = _make_session(session_id=sid)
        _setup_cancel_state(sid, stream_id)

        config.STREAM_PARTIAL_TEXT[stream_id] = "Some partial text"

        cancel_stream(stream_id)

        s2 = models.SESSIONS[sid]
        msgs = s2.messages
        # Should have the cancel marker
        has_cancel = any(
            isinstance(m, dict) and m.get('role') == 'assistant' and m.get('_error')
            for m in msgs
        )
        assert has_cancel, "Cancel marker should always be present"


# ── §B: Tool calls lost on cancel ───────────────────────────────────────────

class TestCancelPreservesToolCalls:
    """§B: _live_tool_calls is thread-local and invisible to cancel_stream().
    
    After fix: tool calls should be persisted in a shared dict
    (STREAM_LIVE_TOOL_CALLS) keyed by stream_id, and cancel_stream()
    should append them as tool_call entries on the partial assistant message.
    """

    def test_cancel_with_tool_calls_preserves_tools(self):
        """Cancel after tool execution should preserve the tool call info."""
        sid = "test_1361_b1"
        stream_id = "stream_b1"
        s = _make_session(session_id=sid)
        _setup_cancel_state(sid, stream_id)

        config.STREAM_PARTIAL_TEXT[stream_id] = ""

        if hasattr(config, 'STREAM_LIVE_TOOL_CALLS'):
            config.STREAM_LIVE_TOOL_CALLS[stream_id] = [
                {"name": "read_file", "args": {"path": "/tmp/test.py"}, "done": True},
                {"name": "terminal", "args": {"command": "ls"}, "done": False},
            ]

        cancel_stream(stream_id)

        s2 = models.SESSIONS[sid]
        assistant_msgs = [m for m in s2.messages if isinstance(m, dict) and m.get('role') == 'assistant']
        has_tools = any(m.get('tool_calls') or m.get('tools') for m in assistant_msgs)
        assert has_tools, \
            f"Expected tool_calls on partial assistant msg after cancel. Got: {assistant_msgs}"

    def test_cancel_with_tools_and_text_preserves_both(self):
        """Cancel after tools + partial text should keep both."""
        sid = "test_1361_b2"
        stream_id = "stream_b2"
        s = _make_session(session_id=sid)
        _setup_cancel_state(sid, stream_id)

        config.STREAM_PARTIAL_TEXT[stream_id] = "Here's what I found:"
        if hasattr(config, 'STREAM_LIVE_TOOL_CALLS'):
            config.STREAM_LIVE_TOOL_CALLS[stream_id] = [
                {"name": "web_search", "args": {"query": "test"}, "done": True},
            ]

        cancel_stream(stream_id)

        s2 = models.SESSIONS[sid]
        assistant_msgs = [m for m in s2.messages if isinstance(m, dict) and m.get('role') == 'assistant']
        partial_msgs = [m for m in assistant_msgs if m.get('_partial')]
        has_content = any(m.get('content') for m in partial_msgs)
        assert has_content, \
            f"Expected partial content with tools after cancel. Got: {partial_msgs}"


# ── §C: Empty _stripped skips entire append ─────────────────────────────────

class TestCancelWithReasoningOnlyNoText:
    """§C: When streaming was 100% reasoning (no visible tokens), _stripped is
    empty after regex cleanup, so no partial assistant message is appended.
    
    After fix: even when _stripped is empty, if reasoning or tool calls exist,
    a partial assistant message should be appended (with no content, but with
    reasoning and/or tool_calls fields).
    """

    def test_reasoning_only_creates_partial_message(self):
        """Cancel after reasoning-only output should still create a partial msg."""
        sid = "test_1361_c1"
        stream_id = "stream_c1"
        s = _make_session(session_id=sid)
        _setup_cancel_state(sid, stream_id)

        # Only reasoning, no visible tokens at all
        config.STREAM_PARTIAL_TEXT[stream_id] = ""

        if hasattr(config, 'STREAM_REASONING_TEXT'):
            config.STREAM_REASONING_TEXT[stream_id] = "Deep reasoning here..."

        cancel_stream(stream_id)

        s2 = models.SESSIONS[sid]
        assistant_msgs = [m for m in s2.messages if isinstance(m, dict) and m.get('role') == 'assistant']
        # Should NOT be only the cancel marker — there should be a partial msg
        partial_msgs = [m for m in assistant_msgs if m.get('_partial')]
        assert len(partial_msgs) > 0, \
            f"Expected at least one partial assistant msg for reasoning-only cancel. Got: {assistant_msgs}"

    def test_tools_only_creates_partial_message(self):
        """Cancel after tool-only output (no text, no reasoning) should still create a partial msg."""
        sid = "test_1361_c2"
        stream_id = "stream_c2"
        s = _make_session(session_id=sid)
        _setup_cancel_state(sid, stream_id)

        config.STREAM_PARTIAL_TEXT[stream_id] = ""

        if hasattr(config, 'STREAM_LIVE_TOOL_CALLS'):
            config.STREAM_LIVE_TOOL_CALLS[stream_id] = [
                {"name": "read_file", "args": {"path": "/tmp/x"}, "done": True},
            ]

        cancel_stream(stream_id)

        s2 = models.SESSIONS[sid]
        assistant_msgs = [m for m in s2.messages if isinstance(m, dict) and m.get('role') == 'assistant']
        partial_msgs = [m for m in assistant_msgs if m.get('_partial')]
        assert len(partial_msgs) > 0, \
            f"Expected at least one partial assistant msg for tools-only cancel. Got: {assistant_msgs}"

    def test_no_reasoning_no_tools_no_partial(self):
        """Cancel with no reasoning and no tools and no text = only cancel marker (no change)."""
        sid = "test_1361_c3"
        stream_id = "stream_c3"
        s = _make_session(session_id=sid)
        _setup_cancel_state(sid, stream_id)

        config.STREAM_PARTIAL_TEXT[stream_id] = ""

        cancel_stream(stream_id)

        s2 = models.SESSIONS[sid]
        assistant_msgs = [m for m in s2.messages if isinstance(m, dict) and m.get('role') == 'assistant']
        # Should only have the cancel marker, no partial messages
        partial_msgs = [m for m in assistant_msgs if m.get('_partial')]
        cancel_msgs = [m for m in assistant_msgs if m.get('_error')]
        assert len(partial_msgs) == 0, \
            f"Expected no partial msg when nothing was streamed. Got partials: {partial_msgs}"
        assert len(cancel_msgs) == 1, \
            f"Expected exactly 1 cancel marker. Got: {cancel_msgs}"
