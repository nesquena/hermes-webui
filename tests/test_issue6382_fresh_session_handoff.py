"""
Tests for Issue #6382: Fresh session with compressed-context handoff.

POST /api/session/handoff creates a new independent session carrying only:
  1. A system preamble explaining the context origin.
  2. The source session's compressed ``context_messages``.
  3. The latest completed user/assistant exchange from the visible transcript
     (session.messages), appended only when not already at the tail of
     context_messages.

The new session has an empty visible transcript (messages=[]) and feeds the
handoff payload only via ``context_messages`` (model-facing context).
"""
import copy
from types import SimpleNamespace

import pytest

from api import routes
from api.models import new_session
from api.routes import (
    _session_handoff_eligibility_error,
    _extract_latest_completed_exchange,
    _build_handoff_context_messages,
    _handle_session_handoff,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    """Point SESSION_DIR to a temp directory for test isolation."""
    monkeypatch.setattr(routes, "SESSION_DIR", tmp_path)
    monkeypatch.setattr("api.models.SESSION_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def captured_response(monkeypatch):
    """Capture calls to ``j()`` so tests can assert status + payload."""
    captures = {}

    def _capture(handler, payload, status=200, **kw):
        captures.update(payload=payload, status=status)
        return True

    monkeypatch.setattr(routes, "j", _capture)
    monkeypatch.setattr("api.helpers.j", _capture)
    monkeypatch.setattr(routes, "_check_csrf", lambda h: True)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *args, **kwargs: True)
    return captures


def _compression_marker(text="[context compaction] Summary of previous conversation."):
    """Return a dict that passes is_context_compression_marker()."""
    return {"role": "assistant", "content": text}


def _make_compressed_session(session_dir, context_messages=None, messages=None, **overrides):
    """Create a session with compressed context_messages for testing.

    The context_messages always start with a canonical compression marker
    so the eligibility check passes.
    """
    marker = _compression_marker()
    ctx = context_messages or [
        marker,
        {"role": "user", "content": "What is the capital of France?", "timestamp": 100.0},
        {"role": "assistant", "content": "The capital of France is Paris.", "timestamp": 101.0},
        {"role": "user", "content": "Tell me about the Eiffel Tower.", "timestamp": 102.0},
        {"role": "assistant", "content": "The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris.", "timestamp": 103.0},
    ]
    s = new_session(**overrides)
    s.context_messages = copy.deepcopy(ctx)
    if messages is not None:
        s.messages = messages
    s.save()
    return s, ctx


# ── _session_handoff_eligibility_error ─────────────────────────────────────


class TestEligibility:
    def test_eligible_when_compressed(self):
        """A session with a canonical compression marker is eligible."""
        s = SimpleNamespace(
            context_messages=[_compression_marker()],
            active_stream_id=None,
            pending_user_message=None,
            pre_compression_snapshot=False,
        )
        assert _session_handoff_eligibility_error(s) is None

    def test_not_eligible_plain_context_without_marker(self):
        """A nonempty context_messages without a compression marker is NOT eligible.

        This is the false-positive guard called out in the #6382 contract review:
        ordinary context (even nonempty) does not prove compression occurred.
        """
        s = SimpleNamespace(
            context_messages=[{"role": "user", "content": "hi"}],
            active_stream_id=None,
            pending_user_message=None,
            pre_compression_snapshot=False,
        )
        err = _session_handoff_eligibility_error(s)
        assert err is not None
        assert "no compressed context" in err.lower()

    def test_no_context(self):
        s = SimpleNamespace(
            context_messages=[],
            active_stream_id=None,
            pending_user_message=None,
            pre_compression_snapshot=False,
        )
        err = _session_handoff_eligibility_error(s)
        assert err is not None
        assert "no compressed context" in err.lower()

    def test_active_stream(self):
        s = SimpleNamespace(
            context_messages=[_compression_marker()],
            active_stream_id="stream-123",
            pending_user_message=None,
            pre_compression_snapshot=False,
        )
        err = _session_handoff_eligibility_error(s)
        assert err is not None
        assert "streaming" in err.lower()

    def test_pending_user_message(self):
        s = SimpleNamespace(
            context_messages=[_compression_marker()],
            active_stream_id=None,
            pending_user_message="hello?",
            pre_compression_snapshot=False,
        )
        err = _session_handoff_eligibility_error(s)
        assert err is not None
        assert "pending" in err.lower()

    def test_pre_compression_snapshot(self):
        s = SimpleNamespace(
            context_messages=[_compression_marker()],
            active_stream_id=None,
            pending_user_message=None,
            pre_compression_snapshot=True,
        )
        err = _session_handoff_eligibility_error(s)
        assert err is not None
        assert "snapshot" in err.lower()


# ── _extract_latest_completed_exchange ─────────────────────────────────────


class TestExtractLatestExchange:
    def test_basic_two_message_exchange(self):
        msgs = [
            {"role": "user", "content": "hello", "timestamp": 1.0},
            {"role": "assistant", "content": "hi there", "timestamp": 2.0},
        ]
        user, assistant = _extract_latest_completed_exchange(msgs)
        assert user["content"] == "hello"
        assert assistant["content"] == "hi there"

    def test_multi_turn_picks_last_exchange(self):
        msgs = [
            {"role": "user", "content": "first q", "timestamp": 1.0},
            {"role": "assistant", "content": "first a", "timestamp": 2.0},
            {"role": "user", "content": "second q", "timestamp": 3.0},
            {"role": "assistant", "content": "second a", "timestamp": 4.0},
            {"role": "user", "content": "third q", "timestamp": 5.0},
            {"role": "assistant", "content": "third a", "timestamp": 6.0},
        ]
        user, assistant = _extract_latest_completed_exchange(msgs)
        assert user["content"] == "third q"
        assert assistant["content"] == "third a"

    def test_skips_error_assistant_message(self):
        msgs = [
            {"role": "user", "content": "hello", "timestamp": 1.0},
            {"role": "assistant", "content": "ok", "_error": True, "timestamp": 2.0},
            {"role": "user", "content": "real q", "timestamp": 3.0},
            {"role": "assistant", "content": "real a", "timestamp": 4.0},
        ]
        user, assistant = _extract_latest_completed_exchange(msgs)
        assert user["content"] == "real q"
        assert assistant["content"] == "real a"

    def test_skips_interrupted_marker(self):
        msgs = [
            {"role": "user", "content": "hello", "timestamp": 1.0},
            {"role": "assistant", "content": "partial...", "type": "interrupted", "timestamp": 2.0},
            {"role": "user", "content": "retry", "timestamp": 3.0},
            {"role": "assistant", "content": "full answer", "timestamp": 4.0},
        ]
        user, assistant = _extract_latest_completed_exchange(msgs)
        assert user["content"] == "retry"
        assert assistant["content"] == "full answer"

    def test_empty_messages_returns_none(self):
        user, assistant = _extract_latest_completed_exchange([])
        assert user is None
        assert assistant is None

    def test_no_completed_exchange(self):
        msgs = [
            {"role": "user", "content": "hello", "timestamp": 1.0},
        ]
        user, assistant = _extract_latest_completed_exchange(msgs)
        assert user is None
        assert assistant is None

    def test_assistant_with_tool_calls_is_valid(self):
        msgs = [
            {"role": "user", "content": "search google", "timestamp": 1.0},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "web_search"}}], "timestamp": 2.0},
            {"role": "tool", "tool_call_id": "call1", "content": "results", "timestamp": 3.0},
            {"role": "assistant", "content": "Here are the results.", "timestamp": 4.0},
        ]
        user, assistant = _extract_latest_completed_exchange(msgs)
        assert user["content"] == "search google"
        assert assistant["content"] == "Here are the results."

    def test_assistant_with_only_tool_calls_no_content(self):
        msgs = [
            {"role": "user", "content": "run tool", "timestamp": 1.0},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "my_tool"}}], "timestamp": 2.0},
        ]
        user, assistant = _extract_latest_completed_exchange(msgs)
        assert user["content"] == "run tool"
        assert assistant is not None
        assert assistant.get("tool_calls") is not None


# ── _build_handoff_context_messages ────────────────────────────────────────


class TestBuildHandoffContext:
    def test_basic_structure(self):
        source_context = [
            {"role": "user", "content": "q1", "timestamp": 1.0},
            {"role": "assistant", "content": "a1", "timestamp": 2.0},
        ]
        last_user = {"role": "user", "content": "q2", "timestamp": 3.0}
        last_assistant = {"role": "assistant", "content": "a2", "timestamp": 4.0}

        result = _build_handoff_context_messages(source_context, last_user, last_assistant)

        # Must have system preamble + source context + last exchange
        assert len(result) == 1 + len(source_context) + 2

        # First message is system preamble
        assert result[0]["role"] == "system"
        assert "continuity context" in result[0]["content"]
        assert "background" in result[0]["content"]
        assert "authoritative" in result[0]["content"]

        # Source context preserved after preamble
        for i, msg in enumerate(source_context):
            assert result[1 + i]["role"] == msg["role"]
            assert result[1 + i]["content"] == msg["content"]

        # Last exchange appended (not already at source_context tail)
        assert result[-2]["role"] == "user"
        assert result[-2]["content"] == "q2"
        assert result[-1]["role"] == "assistant"
        assert result[-1]["content"] == "a2"

    def test_skips_duplicate_when_already_at_tail(self):
        """When last_user/last_assistant already match the tail of source_context,
        they must NOT be appended again (self-duplication guard)."""
        source_context = [
            {"role": "user", "content": "q1", "timestamp": 1.0},
            {"role": "assistant", "content": "a1", "timestamp": 2.0},
            {"role": "user", "content": "q2", "timestamp": 3.0},
            {"role": "assistant", "content": "a2", "timestamp": 4.0},
        ]
        # These match the tail of source_context
        last_user = {"role": "user", "content": "q2", "timestamp": 5.0}
        last_assistant = {"role": "assistant", "content": "a2", "timestamp": 6.0}

        result = _build_handoff_context_messages(source_context, last_user, last_assistant)

        # No duplication — tail pair already in source_context
        assert len(result) == 1 + len(source_context)  # preamble + source only
        assert result[-1]["content"] == "a2"
        assert result[-2]["content"] == "q2"

    def test_append_when_tail_different(self):
        """When source_context tail differs from extracted exchange, append."""
        source_context = [
            {"role": "user", "content": "q1", "timestamp": 1.0},
            {"role": "assistant", "content": "a1", "timestamp": 2.0},
        ]
        last_user = {"role": "user", "content": "q2", "timestamp": 3.0}
        last_assistant = {"role": "assistant", "content": "a2", "timestamp": 4.0}

        result = _build_handoff_context_messages(source_context, last_user, last_assistant)

        assert len(result) == 1 + len(source_context) + 2
        assert result[-2]["content"] == "q2"
        assert result[-1]["content"] == "a2"

    def test_deep_copy_preserves_independence(self):
        """Modifying source context after build must not affect the handoff copy."""
        source_context = [
            {"role": "user", "content": "original", "nested": {"key": "val"}},
        ]
        result = _build_handoff_context_messages(source_context, None, None)

        # Modify the original
        source_context[0]["content"] = "mutated"
        source_context[0]["nested"]["key"] = "mutated"

        # The handoff copy should be unchanged
        assert result[1]["content"] == "original"
        assert result[1]["nested"]["key"] == "val"

    def test_handoff_without_latest_exchange(self):
        """When no completed exchange exists, only preamble + source context."""
        source_context = [
            {"role": "user", "content": "q1", "timestamp": 1.0},
            {"role": "assistant", "content": "a1", "timestamp": 2.0},
        ]
        result = _build_handoff_context_messages(source_context, None, None)
        assert len(result) == 1 + len(source_context)
        assert result[-1]["role"] == "assistant"
        assert result[-1]["content"] == "a1"


# ── _handle_session_handoff ────────────────────────────────────────────────


class TestHandleSessionHandoff:
    def test_requires_session_id(self, session_dir, captured_response):
        """Missing session_id returns 400."""
        _handle_session_handoff(object(), {}, diag=None)
        assert captured_response.get("status") == 400
        err = str(captured_response.get("payload", "")).lower()
        assert "session_id" in err

    def test_handoff_creates_fresh_session(self, session_dir, captured_response):
        """End-to-end: compressed source -> handoff produces a fresh session.

        The source has context_messages including a compression marker + 2 exchanges,
        and a separate visible transcript with the latest exchange. The handoff
        result should have preamble + source context (no duplication of the tail
        since it's already in source_context).
        """
        marker = _compression_marker()
        latest_user = {"role": "user", "content": "Tell me about the Eiffel Tower.", "timestamp": 102.0}
        latest_asst = {"role": "assistant", "content": "The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris.", "timestamp": 103.0}

        # Context messages: marker + 2 exchanges
        ctx = [
            marker,
            {"role": "user", "content": "What is the capital of France?", "timestamp": 100.0},
            {"role": "assistant", "content": "The capital of France is Paris.", "timestamp": 101.0},
            latest_user,
            latest_asst,
        ]

        # Visible transcript — the latest exchange
        vis_msgs = [latest_user, latest_asst]

        source, _ = _make_compressed_session(
            session_dir,
            context_messages=ctx,
            messages=vis_msgs,
        )
        source_sid = source.session_id

        _handle_session_handoff(
            object(), {"session_id": source_sid}, diag=None
        )

        assert captured_response.get("status") == 200
        payload = captured_response["payload"]
        assert "session" in payload
        assert payload["handoff_source"] == source_sid
        assert payload["handoff_compressed"] is True
        assert payload["announcement"] is not None

        new_s = payload["session"]
        # New session has empty visible transcript
        assert new_s.get("messages") == []
        # New session has its own id
        assert new_s["session_id"] != source_sid
        # Parent is set
        assert new_s.get("parent_session_id") == source_sid
        # Model and workspace inherited from source
        assert new_s.get("model") == source.model

        # Verify the new session object in memory has the handoff context
        from api.models import get_session
        new_session_obj = get_session(new_s["session_id"])
        assert new_session_obj is not None
        # The handoff context = 1 preamble + len(source_context) — no duplication
        # because the latest exchange from messages matches the tail of context_messages.
        expected_len = 1 + len(ctx)
        assert len(new_session_obj.context_messages) == expected_len
        assert new_session_obj.context_messages[0]["role"] == "system"
        assert "continuity context" in new_session_obj.context_messages[0]["content"]
        # Source messages preserved (compression marker + exchanges)
        assert new_session_obj.context_messages[1]["role"] == "assistant"
        assert "[context compaction" in new_session_obj.context_messages[1]["content"]
        assert new_session_obj.context_messages[2]["content"] == ctx[1]["content"]

        # Verify the marker message is a compression marker
        from api.compression_anchor import is_context_compression_marker
        assert is_context_compression_marker(new_session_obj.context_messages[1])

        # The last exchange from visible transcript matches the tail of context_messages
        # and is NOT duplicated (no extra pair at the end)
        assert new_session_obj.context_messages[-2]["role"] == "user"
        assert new_session_obj.context_messages[-2]["content"] == latest_user["content"]
        assert new_session_obj.context_messages[-1]["role"] == "assistant"
        assert new_session_obj.context_messages[-1]["content"] == latest_asst["content"]

    def test_handoff_appends_fresh_exchange_when_not_in_context(self, session_dir, captured_response):
        """When the visible transcript has a newer exchange than the compressed
        context_messages tail, it gets appended."""
        marker = _compression_marker()
        ctx = [
            marker,
            {"role": "user", "content": "q1", "timestamp": 100.0},
            {"role": "assistant", "content": "a1", "timestamp": 101.0},
        ]
        # Visible transcript has a NEW exchange not present in context_messages
        new_user = {"role": "user", "content": "fresh question", "timestamp": 200.0}
        new_asst = {"role": "assistant", "content": "fresh answer", "timestamp": 201.0}
        vis_msgs = [new_user, new_asst]

        source, _ = _make_compressed_session(
            session_dir,
            context_messages=ctx,
            messages=vis_msgs,
        )

        _handle_session_handoff(
            object(), {"session_id": source.session_id}, diag=None
        )

        assert captured_response.get("status") == 200
        from api.models import get_session
        new_session_obj = get_session(captured_response["payload"]["session"]["session_id"])
        assert new_session_obj is not None

        # Expected: 1 preamble + 3 source ctx + 2 fresh exchange = 6
        assert len(new_session_obj.context_messages) == 1 + len(ctx) + 2
        assert new_session_obj.context_messages[-2]["content"] == "fresh question"
        assert new_session_obj.context_messages[-1]["content"] == "fresh answer"

    def test_handoff_from_ineligible_session_returns_400(self, session_dir, captured_response):
        """A session without a compression marker is not eligible."""
        # Create a session with plain context_messages (no compression marker)
        s = new_session()
        s.context_messages = [{"role": "user", "content": "hi"}]
        s.save()
        sid = s.session_id

        _handle_session_handoff(object(), {"session_id": sid}, diag=None)
        assert captured_response.get("status") == 400
        assert "no compressed context" in str(captured_response.get("payload", "")).lower()

    def test_handoff_bad_session_id_404(self, session_dir, captured_response):
        """Requesting a non-existent session returns 404."""
        _handle_session_handoff(object(), {"session_id": "nonexistent"}, diag=None)
        assert captured_response.get("status") == 404

    def test_handoff_preserves_workspace_model_provider(self, session_dir, captured_response):
        """Request-level overrides for workspace/model should work."""
        import copy
        marker = _compression_marker()
        ctx = [marker, {"role": "user", "content": "q1"}]
        source, _ = _make_compressed_session(
            session_dir,
            context_messages=ctx,
            model="gpt-4",
            model_provider="openai",
        )

        _handle_session_handoff(
            object(),
            {
                "session_id": source.session_id,
                "model": "gpt-4",
                "model_provider": "openai",
            },
            diag=None,
        )
        assert captured_response.get("status") == 200
        session = captured_response["payload"]["session"]
        assert session["model"] == "gpt-4"
        assert session["model_provider"] == "openai"

    def test_profile_gate_rejects_cross_profile(self, session_dir, monkeypatch):
        """The profile gate blocks cross-profile source access."""
        captures = {}
        def _capture(handler, payload, status=200, **kw):
            captures.update(payload=payload, status=status)
            return True

        # Simulate active profile = "default", source = "other"
        def _visible_to_active_profile(profile, handler=None):
            return profile is None or profile == "default"

        monkeypatch.setattr(routes, "j", _capture)
        monkeypatch.setattr("api.helpers.j", _capture)
        monkeypatch.setattr(routes, "_check_csrf", lambda h: True)
        monkeypatch.setattr(routes, "publish_session_list_changed", lambda *a, **kw: None)
        monkeypatch.setattr(routes, "_session_visible_to_active_profile", _visible_to_active_profile)

        marker = _compression_marker()
        ctx = [marker, {"role": "user", "content": "q1"}]
        source, _ = _make_compressed_session(
            session_dir,
            context_messages=ctx,
            profile="other",
        )

        _handle_session_handoff(
            object(),
            {"session_id": source.session_id},
            diag=None,
        )
        assert captures.get("status") == 404
        assert "not found" in str(captures.get("payload", "")).lower()
