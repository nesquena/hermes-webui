"""
Regression tests for #893 — cancel_stream() now preserves partial streamed
assistant content rather than discarding it.

Before this fix, clicking Stop Generation threw away all streamed text. The
session was saved with only '*Task cancelled.*' appended, so the user lost
whatever the agent had produced up to that point.

After this fix:
- Partial text is accumulated in STREAM_PARTIAL_TEXT[stream_id] via on_token()
- cancel_stream() reads that buffer, strips thinking markup, and persists it
  as a '_partial: True' assistant message before the cancel marker
- _sanitize_messages_for_api() does NOT strip _partial messages, so the model
  sees the partial content as prior context on the next turn
- The cancel marker itself keeps _error=True so the model does not see it
"""

import threading

import pytest

import api.config as config
from api.config import STREAM_PARTIAL_TEXT, STREAMS_LOCK


@pytest.fixture(autouse=True)
def _isolate_stream_state():
    """Isolate shared stream state between tests."""
    STREAM_PARTIAL_TEXT.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    yield
    STREAM_PARTIAL_TEXT.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()


class TestStreamPartialTextAccumulation:
    def test_stream_partial_text_initialized_on_stream_creation(
        self, tmp_path, monkeypatch
    ):
        """STREAM_PARTIAL_TEXT[stream_id] starts empty when a stream is registered."""
        import queue

        sid = "test_init_stream"
        q = queue.Queue()
        cancel_event = threading.Event()
        with STREAMS_LOCK:
            config.STREAMS[sid] = q
            config.CANCEL_FLAGS[sid] = cancel_event
            STREAM_PARTIAL_TEXT[sid] = ""
        assert STREAM_PARTIAL_TEXT.get(sid) == ""

    def test_stream_partial_text_cleaned_up_on_stream_end(self):
        """STREAM_PARTIAL_TEXT[stream_id] is removed when the stream dict is cleaned up."""
        import queue

        sid = "test_cleanup_stream"
        q = queue.Queue()
        with STREAMS_LOCK:
            config.STREAMS[sid] = q
            STREAM_PARTIAL_TEXT[sid] = "some partial text"
        with STREAMS_LOCK:
            config.STREAMS.pop(sid, None)
            STREAM_PARTIAL_TEXT.pop(sid, None)
        assert sid not in STREAM_PARTIAL_TEXT


class TestCancelStreamPreservesPartial:
    def test_cancel_stream_saves_partial_text_to_session(self, tmp_path, monkeypatch):
        """cancel_stream() persists accumulated partial text as an assistant message."""
        import queue
        from api.models import Session
        from api.streaming import cancel_stream

        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        import api.models as _models

        monkeypatch.setattr(config, "SESSION_DIR", session_dir)
        monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json")
        monkeypatch.setattr(_models, "SESSION_DIR", session_dir)
        monkeypatch.setattr(_models, "SESSION_INDEX_FILE", session_dir / "_index.json")
        config.SESSIONS.clear()
        _models.SESSIONS.clear()

        # Create a session and a fake running stream
        s = Session(session_id="sess_partial", title="Test")
        s.messages.append({"role": "user", "content": "Tell me about Python"})
        s.active_stream_id = "stream_partial"
        s.save()
        config.SESSIONS["sess_partial"] = s

        q = queue.Queue()
        cancel_event = threading.Event()
        with STREAMS_LOCK:
            config.STREAMS["stream_partial"] = q
            config.CANCEL_FLAGS["stream_partial"] = cancel_event
            STREAM_PARTIAL_TEXT["stream_partial"] = (
                "Python is a high-level programming language"
            )

        # Fake agent with session_id attribute
        class FakeAgent:
            session_id = "sess_partial"

            def interrupt(self, _):
                pass

        config.AGENT_INSTANCES["stream_partial"] = FakeAgent()

        result = cancel_stream("stream_partial")

        assert result is True

        # Reload the session and check messages
        from api.models import Session

        saved = Session.load("sess_partial")
        assert saved is not None

        msg_contents = [m.get("content", "") for m in saved.messages]
        # Should have: user message, partial assistant content, cancel marker
        assert any(
            "Python is a high-level programming language" in c for c in msg_contents
        ), f"Partial text not found in session messages: {msg_contents}"
        assert any("*Task cancelled.*" in c for c in msg_contents), (
            "Cancel marker missing from session messages"
        )
        # Partial message should NOT have _error=True (it's real content)
        partial_msg = next(
            m
            for m in saved.messages
            if "Python is a high-level" in m.get("content", "")
        )
        assert partial_msg.get("_partial") is True
        assert not partial_msg.get("_error")
        # Cancel marker should have _error=True
        cancel_msg = next(
            m for m in saved.messages if "*Task cancelled.*" in m.get("content", "")
        )
        assert cancel_msg.get("_error") is True

    def test_cancel_stream_with_no_partial_text_still_saves_cancel_marker(
        self, tmp_path, monkeypatch
    ):
        """If no tokens were streamed before cancel, only the cancel marker is saved."""
        import queue
        from api.models import Session
        from api.streaming import cancel_stream

        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        import api.models as _models

        monkeypatch.setattr(config, "SESSION_DIR", session_dir)
        monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json")
        monkeypatch.setattr(_models, "SESSION_DIR", session_dir)
        monkeypatch.setattr(_models, "SESSION_INDEX_FILE", session_dir / "_index.json")
        config.SESSIONS.clear()
        _models.SESSIONS.clear()

        s = Session(session_id="sess_nopartial", title="Test")
        s.messages.append({"role": "user", "content": "Hello"})
        s.active_stream_id = "stream_nopartial"
        s.save()
        config.SESSIONS["sess_nopartial"] = s

        q = queue.Queue()
        cancel_event = threading.Event()
        with STREAMS_LOCK:
            config.STREAMS["stream_nopartial"] = q
            config.CANCEL_FLAGS["stream_nopartial"] = cancel_event
            STREAM_PARTIAL_TEXT["stream_nopartial"] = (
                ""  # empty — cancel before any tokens
            )

        class FakeAgent:
            session_id = "sess_nopartial"

            def interrupt(self, _):
                pass

        config.AGENT_INSTANCES["stream_nopartial"] = FakeAgent()

        cancel_stream("stream_nopartial")

        saved = Session.load("sess_nopartial")
        msg_contents = [m.get("content", "") for m in saved.messages]
        assert any("*Task cancelled.*" in c for c in msg_contents)
        # No extra partial message when there was nothing streamed
        assert not any(m.get("_partial") for m in saved.messages), (
            "Should not add partial message when no tokens were streamed"
        )

    def test_cancel_stream_strips_thinking_markup_from_partial(
        self, tmp_path, monkeypatch
    ):
        """Thinking blocks in partial text are stripped before saving."""
        import queue
        from api.models import Session
        from api.streaming import cancel_stream

        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        import api.models as _models

        monkeypatch.setattr(config, "SESSION_DIR", session_dir)
        monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json")
        monkeypatch.setattr(_models, "SESSION_DIR", session_dir)
        monkeypatch.setattr(_models, "SESSION_INDEX_FILE", session_dir / "_index.json")
        config.SESSIONS.clear()
        _models.SESSIONS.clear()

        s = Session(session_id="sess_thinking", title="Test")
        s.messages.append({"role": "user", "content": "Think about this"})
        s.active_stream_id = "stream_thinking"
        s.save()
        config.SESSIONS["sess_thinking"] = s

        q = queue.Queue()
        cancel_event = threading.Event()
        with STREAMS_LOCK:
            config.STREAMS["stream_thinking"] = q
            config.CANCEL_FLAGS["stream_thinking"] = cancel_event
            STREAM_PARTIAL_TEXT["stream_thinking"] = (
                "<think>internal reasoning here</think>\nThe answer is 42"
            )

        class FakeAgent:
            session_id = "sess_thinking"

            def interrupt(self, _):
                pass

        config.AGENT_INSTANCES["stream_thinking"] = FakeAgent()

        cancel_stream("stream_thinking")

        saved = Session.load("sess_thinking")
        partial_msg = next((m for m in saved.messages if m.get("_partial")), None)
        assert partial_msg is not None, (
            "Partial message should be saved when content remains after stripping"
        )
        assert "<think>" not in partial_msg["content"], (
            "Closed thinking block should be stripped"
        )
        assert "The answer is 42" in partial_msg["content"], (
            "Visible content should be preserved"
        )

    def test_cancel_stream_strips_unclosed_think_tag(self, tmp_path, monkeypatch):
        """The common cancel-mid-reasoning case: <think> block without a closing tag."""
        import queue
        from api.models import Session
        from api.streaming import cancel_stream

        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        import api.models as _models

        monkeypatch.setattr(config, "SESSION_DIR", session_dir)
        monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json")
        monkeypatch.setattr(_models, "SESSION_DIR", session_dir)
        monkeypatch.setattr(_models, "SESSION_INDEX_FILE", session_dir / "_index.json")
        config.SESSIONS.clear()
        _models.SESSIONS.clear()

        s = Session(session_id="sess_unclosed", title="Test")
        s.messages.append({"role": "user", "content": "Please reason step by step"})
        s.active_stream_id = "stream_unclosed"
        s.save()
        config.SESSIONS["sess_unclosed"] = s

        q = queue.Queue()
        cancel_event = threading.Event()
        with STREAMS_LOCK:
            config.STREAMS["stream_unclosed"] = q
            config.CANCEL_FLAGS["stream_unclosed"] = cancel_event
            # Simulates user hitting Stop mid-reasoning — <think> never closed
            STREAM_PARTIAL_TEXT["stream_unclosed"] = (
                "<think>\nStep 1: consider the problem...\nStep 2: the user wants"
            )

        class FakeAgent:
            session_id = "sess_unclosed"

            def interrupt(self, _):
                pass

        config.AGENT_INSTANCES["stream_unclosed"] = FakeAgent()

        cancel_stream("stream_unclosed")

        saved = Session.load("sess_unclosed")
        # The entire content was inside an unclosed <think> block — nothing visible
        # remains after stripping, so no _partial message should be saved
        partial_msg = next((m for m in saved.messages if m.get("_partial")), None)
        assert partial_msg is None, (
            "Unclosed think block with no visible content should not produce a partial message"
        )
        # Cancel marker should still be present
        assert any("Task cancelled" in m.get("content", "") for m in saved.messages)


class TestPartialMessageInContext:
    def test_partial_message_included_in_api_sanitization(self):
        """Partial messages (_partial=True) are included in API history (model should see them)."""
        from api.streaming import _sanitize_messages_for_api

        messages = [
            {"role": "user", "content": "Tell me about Python"},
            {
                "role": "assistant",
                "content": "Python is a high-level",
                "_partial": True,
            },
            {"role": "assistant", "content": "*Task cancelled.*", "_error": True},
        ]
        clean = _sanitize_messages_for_api(messages)
        roles = [m["role"] for m in clean]
        contents = [m.get("content", "") for m in clean]

        # User message and partial assistant message should be included
        assert "user" in roles
        assert any("Python is a high-level" in c for c in contents), (
            "Partial assistant content should be in API context so model can continue from it"
        )
        # Cancel marker (_error=True) should be excluded
        assert not any("Task cancelled" in c for c in contents), (
            "Cancel marker with _error=True must be stripped from API context"
        )
