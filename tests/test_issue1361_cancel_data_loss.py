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
    models.SESSIONS[session_id].active_stream_id = stream_id
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
        has_tools = any(m.get('_partial_tool_calls') or m.get('tool_calls') or m.get('tools') for m in assistant_msgs)
        assert has_tools, \
            f"Expected _partial_tool_calls on partial assistant msg after cancel. Got: {assistant_msgs}"

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

# ── §D: Error paths must not lose pending user turn ─────────────────────────

def test_stream_error_materializes_pending_user_turn_before_clearing_runtime_state():
    """If a stream errors before normal merge, pending_user_message must become a
    durable user message before the error marker is saved. Otherwise reload/server
    reconcile makes the user's just-submitted prompt disappear.
    """
    from api.streaming import _materialize_pending_user_turn_before_error

    sid = "test_pending_error_d1"
    s = _make_session(
        session_id=sid,
        pending_msg="please restart the WebUI",
        messages=[{"role": "assistant", "content": "previous answer"}],
    )
    s.pending_started_at = 1778098700.0
    s.pending_attachments = [{"name": "screenshot.png"}]

    appended = _materialize_pending_user_turn_before_error(s)

    assert appended is True
    assert s.messages[-1]["role"] == "user"
    assert s.messages[-1]["content"] == "please restart the WebUI"
    assert s.messages[-1]["timestamp"] == 1778098700
    assert s.messages[-1]["attachments"] == [{"name": "screenshot.png"}]
    assert s.pending_user_message == "please restart the WebUI"


def test_stream_error_pending_materialization_does_not_duplicate_eager_checkpoint():
    """Eager session-save mode may already have checkpointed the current user turn;
    the error materializer must not append the same user message again.
    """
    from api.streaming import _materialize_pending_user_turn_before_error

    sid = "test_pending_error_d2"
    s = _make_session(
        session_id=sid,
        pending_msg="please restart the WebUI",
        messages=[
            {"role": "assistant", "content": "previous answer"},
            {"role": "user", "content": "please restart the WebUI"},
        ],
    )

    appended = _materialize_pending_user_turn_before_error(s)

    assert appended is False
    assert [m.get("role") for m in s.messages].count("user") == 1


def test_stale_stream_cleanup_materializes_pending_turn_before_clearing_state():
    """A zombie/stale stream repair must preserve the pending user prompt.

    If the process dies after chat_start saved pending_user_message but before the
    agent merges the user turn, /api/session stale cleanup must not clear that
    pending field without first appending a durable user message.
    """
    from api.routes import _clear_stale_stream_state

    sid = "test_pending_error_d3_stale"
    s = _make_session(
        session_id=sid,
        pending_msg="please make the GUI fully usable",
        messages=[{"role": "assistant", "content": "previous answer"}],
    )
    s.pending_started_at = 1778187755.0
    s.pending_attachments = [{"name": "visible-state.png"}]
    # No matching STREAMS entry: this simulates a dead worker/server restart.

    cleared = _clear_stale_stream_state(s)

    assert cleared is True
    assert s.active_stream_id is None
    assert s.pending_user_message is None
    assert s.messages[-1]["role"] == "user"
    assert s.messages[-1]["content"] == "please make the GUI fully usable"
    assert s.messages[-1]["timestamp"] == 1778187755
    assert s.messages[-1]["attachments"] == [{"name": "visible-state.png"}]

    reloaded = models.get_session(sid, metadata_only=False)
    assert reloaded.messages[-1]["role"] == "user"
    assert reloaded.messages[-1]["content"] == "please make the GUI fully usable"


# ── Structural guard: pin call sites of the materialize helper at error branches ──

def test_materialize_helper_called_immediately_before_error_path_clears():
    """Pin call sites of _materialize_pending_user_turn_before_error.

    Catches a future refactor that drops the call from the apperror-no-response
    or outer-Exception paths in api/streaming.py while leaving the
    `pending_user_message = None` clearing in place — which is exactly the
    user-turn-data-loss regression #1361 was filed for.

    Strategy: count how many `pending_user_message = None` clearings have the
    helper call within the preceding 4 lines. Currently 2 (apperror at 2610,
    outer-Exception at 3072). The success path (2716) and cancel path (3375)
    legitimately don't need the helper. If a future refactor drops the helper
    call from one of the error sites, this assertion fires.
    """
    from pathlib import Path
    src = Path(__file__).parent.parent.joinpath('api', 'streaming.py').read_text(encoding='utf-8')
    lines = src.splitlines()

    helper_name = '_materialize_pending_user_turn_before_error('
    clear_sites = [(i + 1, line) for i, line in enumerate(lines)
                   if 'pending_user_message = None' in line]
    assert len(clear_sites) >= 4, (
        f"Expected ≥4 sites that clear pending_user_message; found {len(clear_sites)}. "
        f"If api/streaming.py was refactored, re-audit this test."
    )

    sites_with_helper = []
    for lineno, _ in clear_sites:
        prev_block = '\n'.join(lines[max(0, lineno - 5):lineno - 1])
        if helper_name in prev_block:
            sites_with_helper.append(lineno)

    # Concretely, PR #1760 wired up the helper at the apperror-no-response
    # path and the outer-Exception path. Both must remain wired.
    assert len(sites_with_helper) >= 2, (
        f"Expected ≥2 clear sites preceded by {helper_name} within 4 lines; "
        f"found {sites_with_helper}. PR #1760 / #1361 regression — re-wire the "
        f"helper at the error-branch clear sites in api/streaming.py."
    )



def test_cancel_copy_uses_configured_bot_name(monkeypatch):
    """Cancellation copy should use the configured assistant display name."""
    import api.streaming as streaming

    monkeypatch.setattr(streaming, 'load_settings', lambda: {'bot_name': 'Obryn'})

    assert streaming._cancelled_turn_hint() == (
        'The run was cancelled by the user before Obryn finished. '
        'No provider failure occurred.'
    )
    assert 'before Obryn finished' in streaming._cancelled_turn_content()
    assert streaming._classify_provider_error('Task cancelled by user')['hint'] == (
        'The run was cancelled by the user before Obryn finished. '
        'No provider failure occurred.'
    )


def test_cancel_copy_falls_back_to_hermes_for_blank_bot_name(monkeypatch):
    """Blank or missing bot_name should not leak old persona copy."""
    import api.streaming as streaming

    monkeypatch.setattr(streaming, 'load_settings', lambda: {'bot_name': '   '})

    assert streaming._cancelled_turn_hint() == (
        'The run was cancelled by the user before Hermes finished. '
        'No provider failure occurred.'
    )


class TestCancelStreamIdempotentWithWorkerFinalizer:
    """The worker and explicit cancel endpoint can both finalize the same turn."""

    def test_cancel_stream_does_not_duplicate_existing_worker_cancel_marker(self):
        sid = "test_1361_idempotent"
        stream_id = "stream_idempotent"
        _make_session(
            session_id=sid,
            messages=[
                {'role': 'user', 'content': 'Help me debug this', 'timestamp': 100},
                {'role': 'assistant', 'content': '**Task cancelled:** Task cancelled.\n\n*The run was cancelled by the user before Hermes finished. No provider failure occurred.*', '_error': True, 'timestamp': 101},
            ],
        )
        _setup_cancel_state(sid, stream_id)
        config.STREAM_PARTIAL_TEXT[stream_id] = "partial text before cancel"

        cancel_stream(stream_id)

        msgs = models.SESSIONS[sid].messages
        cancel_markers = [
            m for m in msgs
            if isinstance(m, dict)
            and m.get('role') == 'assistant'
            and 'task cancelled' in str(m.get('content') or '').lower()
        ]
        partial_idx = next(
            i for i, m in enumerate(msgs)
            if isinstance(m, dict) and m.get('_partial') and m.get('content') == 'partial text before cancel'
        )
        marker_idx = next(i for i, m in enumerate(msgs) if m in cancel_markers)

        assert len(cancel_markers) == 1
        assert partial_idx < marker_idx

    def test_late_cancel_after_worker_finalized_does_not_add_cancel_marker(self):
        sid = "test_1361_late_done"
        stream_id = "stream_late_done"
        s = Session(
            session_id=sid,
            title="Done Session",
            messages=[
                {'role': 'user', 'content': 'finish normally', 'timestamp': 100},
                {'role': 'assistant', 'content': 'done normally', 'timestamp': 101},
            ],
        )
        s.active_stream_id = None
        s.pending_user_message = None
        s.pending_attachments = []
        s.pending_started_at = None
        s.save()
        models.SESSIONS[sid] = s

        q = queue.Queue()
        config.STREAMS[stream_id] = q
        config.CANCEL_FLAGS[stream_id] = threading.Event()
        mock_agent = Mock()
        mock_agent.session_id = sid
        mock_agent.interrupt = Mock()
        config.AGENT_INSTANCES[stream_id] = mock_agent
        config.STREAM_PARTIAL_TEXT[stream_id] = 'stale partial snapshot'

        assert cancel_stream(stream_id) is True

        msgs = models.SESSIONS[sid].messages
        assert msgs == [
            {'role': 'user', 'content': 'finish normally', 'timestamp': 100},
            {'role': 'assistant', 'content': 'done normally', 'timestamp': 101},
        ]
        assert q.empty(), "late cancel must not emit a terminal cancel event after done"
