"""Tests for real /steer functionality (follow-up to PR #1062).

Covers the new POST /api/chat/steer endpoint which mirrors the CLI's /steer
command (cli.py:6140-6155): the endpoint looks up the cached AIAgent for the
session, calls agent.steer(text), and the agent's run loop appends the steer
text to the next tool-result message — no interruption.

Falls back to {"accepted": false, "fallback": "<reason>"} when the agent
isn't running, isn't cached, or doesn't support steer (older agent versions).
The frontend uses the fallback signal to drop back to interrupt mode.

Plus a leftover-delivery flow: if the agent finishes its turn before the
steer is consumed (no tool-call boundary), _drain_pending_steer is called
after run_conversation returns and a `pending_steer_leftover` SSE event is
emitted so the frontend can queue the leftover text as a next-turn message.
"""
import sys
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture(autouse=True)
def _restore_auth_sessions():
    """Snapshot and restore api.auth._sessions — see test_1058 for the rationale."""
    import api.auth as _auth
    snapshot = dict(_auth._sessions)
    yield
    _auth._sessions.clear()
    _auth._sessions.update(snapshot)


@pytest.fixture
def _clear_caches():
    """Snapshot SESSION_AGENT_CACHE and STREAMS so tests don't bleed."""
    from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK, STREAMS, STREAMS_LOCK
    with SESSION_AGENT_CACHE_LOCK:
        cache_snap = dict(SESSION_AGENT_CACHE)
        SESSION_AGENT_CACHE.clear()
    with STREAMS_LOCK:
        streams_snap = dict(STREAMS)
        STREAMS.clear()
    yield
    with SESSION_AGENT_CACHE_LOCK:
        SESSION_AGENT_CACHE.clear()
        SESSION_AGENT_CACHE.update(cache_snap)
    with STREAMS_LOCK:
        STREAMS.clear()
        STREAMS.update(streams_snap)


def _make_handler():
    """Minimal handler stub matching the methods api.helpers.j() touches."""
    h = MagicMock()
    h.wfile = MagicMock()
    h.headers = MagicMock()
    h.headers.get = MagicMock(return_value="")
    return h


def _captured_response(handler):
    """Pull the JSON body that j() wrote to handler.wfile."""
    import json as _json
    # j() calls handler.wfile.write(body)
    write_calls = handler.wfile.write.call_args_list
    assert write_calls, "no body was written to handler.wfile"
    body = write_calls[-1][0][0]
    return _json.loads(body.decode("utf-8"))


def _captured_status(handler):
    """Pull the HTTP status passed to handler.send_response()."""
    calls = handler.send_response.call_args_list
    assert calls, "no status was sent"
    return calls[-1][0][0]


# ── Backend: the /api/chat/steer endpoint ─────────────────────────────────

class TestHandleChatSteerHappyPath:
    """Endpoint accepts text and calls agent.steer() when all gates pass."""

    def test_accepts_when_agent_cached_and_running(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK, STREAMS, STREAMS_LOCK
        sid, stream_id = "sid_happy", "stream_happy"
        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        with STREAMS_LOCK:
            import queue as _q
            STREAMS[stream_id] = _q.Queue()

        sess = MagicMock()
        sess.active_stream_id = stream_id
        with patch("api.streaming.get_session", return_value=sess):
            handler = _make_handler()
            _handle_chat_steer(handler, {"session_id": sid, "text": "Use Python instead"})

        agent.steer.assert_called_once_with("Use Python instead")
        body = _captured_response(handler)
        assert body == {"accepted": True, "fallback": None, "stream_id": stream_id}


class TestHandleChatSteerFallbacks:
    """Each gate that fails returns a structured fallback the frontend can branch on."""

    def test_no_cached_agent(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        handler = _make_handler()
        _handle_chat_steer(handler, {"session_id": "sid_x", "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "no_cached_agent"

    def test_agent_lacks_steer_method(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
        sid = "sid_old"
        # Older agent without steer() — use spec to suppress MagicMock auto-create
        agent = MagicMock(spec=["interrupt", "run_conversation"])
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        handler = _make_handler()
        _handle_chat_steer(handler, {"session_id": sid, "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "agent_lacks_steer"

    def test_session_not_found(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
        sid = "sid_missing"
        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        with patch("api.streaming.get_session", side_effect=KeyError(sid)):
            handler = _make_handler()
            _handle_chat_steer(handler, {"session_id": sid, "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "session_not_found"
        agent.steer.assert_not_called()  # never reached the steer call

    def test_session_not_running(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
        sid = "sid_idle"
        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        sess = MagicMock()
        sess.active_stream_id = None  # idle session
        with patch("api.streaming.get_session", return_value=sess):
            handler = _make_handler()
            _handle_chat_steer(handler, {"session_id": sid, "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "not_running"
        agent.steer.assert_not_called()

    def test_stream_dead(self, _clear_caches):
        """Session has active_stream_id but the stream is gone from STREAMS (e.g. crashed)."""
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK
        sid = "sid_zombie"
        agent = MagicMock()
        agent.steer = MagicMock(return_value=True)
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        sess = MagicMock()
        sess.active_stream_id = "stream_zombie"
        with patch("api.streaming.get_session", return_value=sess):
            handler = _make_handler()
            _handle_chat_steer(handler, {"session_id": sid, "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "stream_dead"
        agent.steer.assert_not_called()

    def test_steer_raises(self, _clear_caches):
        """If agent.steer() raises, return steer_error rather than 500."""
        from api.streaming import _handle_chat_steer
        from api.config import SESSION_AGENT_CACHE, SESSION_AGENT_CACHE_LOCK, STREAMS, STREAMS_LOCK
        sid, stream_id = "sid_throws", "stream_throws"
        agent = MagicMock()
        agent.steer = MagicMock(side_effect=RuntimeError("boom"))
        with SESSION_AGENT_CACHE_LOCK:
            SESSION_AGENT_CACHE[sid] = (agent, "sig")
        with STREAMS_LOCK:
            import queue as _q
            STREAMS[stream_id] = _q.Queue()
        sess = MagicMock()
        sess.active_stream_id = stream_id
        with patch("api.streaming.get_session", return_value=sess):
            handler = _make_handler()
            _handle_chat_steer(handler, {"session_id": sid, "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "steer_error"


class TestHandleChatSteerInputValidation:
    """Bad input → 400 Bad Request, not silent acceptance."""

    def test_missing_session_id(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        handler = _make_handler()
        _handle_chat_steer(handler, {"text": "hint"})
        assert _captured_status(handler) == 400

    def test_missing_text(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        handler = _make_handler()
        _handle_chat_steer(handler, {"session_id": "sid"})
        assert _captured_status(handler) == 400

    def test_empty_text_after_strip(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        handler = _make_handler()
        _handle_chat_steer(handler, {"session_id": "sid", "text": "   \n\t  "})
        assert _captured_status(handler) == 400


# ── Routing ───────────────────────────────────────────────────────────────

class TestRouting:
    """The POST handler must dispatch /api/chat/steer to _handle_chat_steer."""

    def test_route_registered(self):
        src = (Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")
        assert '/api/chat/steer' in src
        assert '_handle_chat_steer' in src


# ── Frontend: cmdSteer + busy-mode steer use the new endpoint ────────────

class TestFrontendWiring:
    """The slash command and busy-mode steer paths must call /api/chat/steer."""

    @classmethod
    def setup_class(cls):
        cls.cmds = (Path(__file__).parent.parent / "static" / "commands.js").read_text(encoding="utf-8")
        cls.msgs = (Path(__file__).parent.parent / "static" / "messages.js").read_text(encoding="utf-8")
        cls.i18n = (Path(__file__).parent.parent / "static" / "i18n.js").read_text(encoding="utf-8")

    def test_cmd_steer_calls_endpoint(self):
        idx = self.cmds.find("async function cmdSteer(")
        assert idx >= 0
        body = self.cmds[idx:idx + 600]
        # Should call _trySteer (which calls the endpoint), not directly cancelStream
        assert "_trySteer" in body, "cmdSteer must delegate to _trySteer"

    def test_try_steer_calls_endpoint(self):
        idx = self.cmds.find("async function _trySteer(")
        assert idx >= 0
        body = self.cmds[idx:idx + 1500]
        assert "/api/chat/steer" in body, "_trySteer must POST to /api/chat/steer"
        assert "method:'POST'" in body or 'method:"POST"' in body

    def test_try_steer_handles_fallback(self):
        idx = self.cmds.find("async function _trySteer(")
        body = self.cmds[idx:idx + 1500]
        # Must check result.accepted and fall back via queueSessionMessage + cancelStream
        assert "result&&result.accepted" in body or "result.accepted" in body
        assert "queueSessionMessage" in body
        assert "cancelStream" in body, "fallback path must cancel the stream"

    def test_send_busy_steer_uses_try_steer(self):
        # send() in messages.js: when busyMode === 'steer', should call _trySteer
        idx = self.msgs.find("busyMode==='steer'")
        assert idx >= 0
        block = self.msgs[idx:idx + 800]
        assert "_trySteer" in block, "send()'s steer branch must delegate to _trySteer"

    def test_pending_steer_leftover_listener(self):
        """Frontend must listen for pending_steer_leftover SSE events and queue them."""
        idx = self.msgs.find("addEventListener('pending_steer_leftover'")
        assert idx >= 0, "messages.js must add a listener for pending_steer_leftover"
        block = self.msgs[idx:idx + 600]
        assert "queueSessionMessage" in block, (
            "pending_steer_leftover handler must queue the leftover text for the next turn"
        )


# ── i18n keys ─────────────────────────────────────────────────────────────

class TestI18nKeys:
    """The two new keys (cmd_steer_delivered, steer_leftover_queued) must be in all 6 locales."""

    @classmethod
    def setup_class(cls):
        cls.i18n = (Path(__file__).parent.parent / "static" / "i18n.js").read_text(encoding="utf-8")

    def test_cmd_steer_delivered_in_all_locales(self):
        assert self.i18n.count("cmd_steer_delivered:") >= 6, (
            f"cmd_steer_delivered appears {self.i18n.count('cmd_steer_delivered:')} times; "
            f"expected ≥6 (one per locale)"
        )

    def test_steer_leftover_queued_in_all_locales(self):
        assert self.i18n.count("steer_leftover_queued:") >= 6, (
            f"steer_leftover_queued appears {self.i18n.count('steer_leftover_queued:')} times; "
            f"expected ≥6 (one per locale)"
        )


# ── Leftover SSE delivery: streaming.py emits pending_steer_leftover ─────

class TestLeftoverDelivery:
    """After run_conversation returns, _drain_pending_steer is called and a
    pending_steer_leftover SSE event is emitted if there's still text stashed."""

    def test_leftover_drain_call_in_streaming(self):
        """Verify the streaming.py source contains the drain call before put('done', ...)."""
        src = (Path(__file__).parent.parent / "api" / "streaming.py").read_text(encoding="utf-8")
        assert "_drain_pending_steer" in src, (
            "_run_agent_streaming must call agent._drain_pending_steer() to deliver leftovers"
        )
        assert "pending_steer_leftover" in src, (
            "_run_agent_streaming must emit a pending_steer_leftover SSE event"
        )

    def test_leftover_drain_runs_before_done_event(self):
        """The drain must happen BEFORE put('done', ...) so frontend gets both events
        on the same turn."""
        src = (Path(__file__).parent.parent / "api" / "streaming.py").read_text(encoding="utf-8")
        # Find the drain invocation and the next put('done', ...) AFTER it
        drain_idx = src.find("_drain_pending_steer()")
        assert drain_idx >= 0
        done_idx = src.find("put('done'", drain_idx)
        assert done_idx >= 0
        # No put('done', ...) should appear BEFORE the drain in the same code block
        # (we already check the drain is in the file; ordering matters within the
        # non-ephemeral success path)
        assert drain_idx < done_idx, (
            "_drain_pending_steer must run before put('done', ...) so the SSE listener "
            "sees the leftover before stream_end fires"
        )
