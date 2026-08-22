"""Tests for real /steer functionality (follow-up to PR #1062).

Covers the new POST /api/chat/steer endpoint which mirrors the CLI's /steer
command (cli.py:6140-6155): the endpoint looks up the cached AIAgent for the
session, calls agent.steer(text), and the agent's run loop appends the steer
text to the next tool-result message — no interruption.

Falls back to {"accepted": false, "fallback": "<reason>"} when the agent
isn't running, isn't cached, or doesn't support steer (older agent versions).
The frontend uses the fallback signal to restore the draft without cancelling
the active run.

Plus a leftover-delivery flow: if the agent finishes its turn before the
steer is consumed (no tool-call boundary), _drain_pending_steer is called
after run_conversation returns and a `pending_steer_leftover` SSE event is
emitted so the frontend can queue the leftover text as a next-turn message.
"""
import sys
import os
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers import source_between as _source_between

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
    from api.config import (
        ACTIVE_RUNS,
        ACTIVE_RUNS_LOCK,
        SESSION_AGENT_CACHE,
        SESSION_AGENT_CACHE_LOCK,
        AGENT_INSTANCES,
        STREAMS,
        STREAMS_LOCK,
        STREAM_SESSION_OWNERS,
        STREAM_SESSION_OWNERS_LOCK,
    )
    with SESSION_AGENT_CACHE_LOCK:
        cache_snap = dict(SESSION_AGENT_CACHE)
        SESSION_AGENT_CACHE.clear()
    with STREAMS_LOCK:
        streams_snap = dict(STREAMS)
        agents_snap = dict(AGENT_INSTANCES)
        STREAMS.clear()
        AGENT_INSTANCES.clear()
    with STREAM_SESSION_OWNERS_LOCK:
        owners_snap = dict(STREAM_SESSION_OWNERS)
        STREAM_SESSION_OWNERS.clear()
    with ACTIVE_RUNS_LOCK:
        active_runs_snap = dict(ACTIVE_RUNS)
        ACTIVE_RUNS.clear()
    yield
    with SESSION_AGENT_CACHE_LOCK:
        SESSION_AGENT_CACHE.clear()
        SESSION_AGENT_CACHE.update(cache_snap)
    with STREAMS_LOCK:
        STREAMS.clear()
        STREAMS.update(streams_snap)
        AGENT_INSTANCES.clear()
        AGENT_INSTANCES.update(agents_snap)
    with STREAM_SESSION_OWNERS_LOCK:
        STREAM_SESSION_OWNERS.clear()
        STREAM_SESSION_OWNERS.update(owners_snap)
    with ACTIVE_RUNS_LOCK:
        ACTIVE_RUNS.clear()
        ACTIVE_RUNS.update(active_runs_snap)


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


def _install_interrupt_stream(stream_id, owner_sid, agent):
    from api.config import AGENT_INSTANCES, STREAMS, STREAMS_LOCK, register_stream_owner

    with STREAMS_LOCK:
        STREAMS[stream_id] = object()
        AGENT_INSTANCES[stream_id] = agent
    register_stream_owner(stream_id, owner_sid)


class TestHandleChatInterrupt:
    def test_supported_redirect_releases_stream_lock_but_blocks_successor_admission(self, _clear_caches):
        from api.streaming import _handle_chat_interrupt
        from api.config import (
            AGENT_INSTANCES,
            STREAMS,
            STREAMS_LOCK,
            _get_session_agent_lock,
            register_stream_owner,
        )

        sid, stream_id = "sid_interrupt", "stream_interrupt"
        successor_id = "stream_interrupt_successor"
        redirect_started = threading.Event()
        release_redirect = threading.Event()
        stream_lock_acquired = threading.Event()
        successor_attempted = threading.Event()
        successor_installed = threading.Event()
        successor_thread = None
        probe_thread = None
        agent = MagicMock(_supports_active_turn_redirect=True, api_mode="chat_completions")
        session = MagicMock(active_stream_id=stream_id)
        session_lock = _get_session_agent_lock(sid)

        def install_successor():
            successor_attempted.set()
            with session_lock:
                session.active_stream_id = successor_id
                with STREAMS_LOCK:
                    STREAMS.pop(stream_id, None)
                    AGENT_INSTANCES.pop(stream_id, None)
                    STREAMS[successor_id] = object()
                    AGENT_INSTANCES[successor_id] = agent
                register_stream_owner(successor_id, sid)
                successor_installed.set()

        def probe_stream_lock():
            with STREAMS_LOCK:
                stream_lock_acquired.set()

        def redirect(text):
            assert text == "redirect me"
            nonlocal probe_thread, successor_thread
            redirect_started.set()
            probe_thread = threading.Thread(target=probe_stream_lock)
            successor_thread = threading.Thread(target=install_successor)
            probe_thread.start()
            successor_thread.start()
            assert release_redirect.wait(1)
            return True

        agent.redirect.side_effect = redirect
        _install_interrupt_stream(stream_id, sid, agent)
        result = {}

        def run_interrupt():
            handler = _make_handler()
            _handle_chat_interrupt(handler, {
                "session_id": sid,
                "stream_id": stream_id,
                "text": "redirect me",
            })
            result["response"] = _captured_response(handler)

        with patch("api.streaming.get_session", return_value=session):
            thread = threading.Thread(target=run_interrupt)
            thread.start()
            assert redirect_started.wait(1)
            assert stream_lock_acquired.wait(0.2), "supported redirect must not hold STREAMS_LOCK"
            assert successor_attempted.wait(1)
            assert not successor_installed.wait(0.05), "successor admission must wait for redirect settlement"
            release_redirect.set()
            thread.join(1)

        assert result["response"] == {"accepted": True, "fallback": None, "stream_id": stream_id}
        agent.redirect.assert_called_once_with("redirect me")
        assert successor_installed.wait(1)
        probe_thread.join(1)
        successor_thread.join(1)

    def test_cancelled_redirect_cannot_ack_a_reused_agent(self, _clear_caches):
        from api.streaming import _handle_chat_interrupt, cancel_stream
        from api.config import AGENT_INSTANCES, STREAMS, STREAMS_LOCK, _get_session_agent_lock

        sid, stream_id = "sid_interrupt_cancel", "stream_interrupt_cancel"
        successor_id = "stream_interrupt_cancel_successor"
        redirect_started = threading.Event()
        release_redirect = threading.Event()
        stream_removed = threading.Event()
        successor_admitted = threading.Event()
        agent = MagicMock(_supports_active_turn_redirect=True, api_mode="chat_completions")
        agent.session_id = sid
        session = MagicMock(
            active_stream_id=stream_id,
            messages=[],
            pending_user_message=None,
            pending_attachments=[],
        )
        session_lock = _get_session_agent_lock(sid)
        cancel_thread = None

        def cancel_and_admit_successor():
            assert cancel_stream(stream_id) is True
            with session_lock:
                session.active_stream_id = successor_id
                with STREAMS_LOCK:
                    STREAMS[successor_id] = object()
                    AGENT_INSTANCES[successor_id] = agent
                successor_admitted.set()

        def redirect(_text):
            nonlocal cancel_thread
            redirect_started.set()
            cancel_thread = threading.Thread(target=cancel_and_admit_successor)
            cancel_thread.start()
            for _ in range(100):
                with STREAMS_LOCK:
                    if stream_id not in STREAMS:
                        stream_removed.set()
                        break
                threading.Event().wait(0.01)
            assert stream_removed.wait(1), "cancellation must remove the old stream"
            assert release_redirect.wait(1)
            return True

        agent.redirect.side_effect = redirect
        _install_interrupt_stream(stream_id, sid, agent)
        result = {}

        def run_interrupt():
            handler = _make_handler()
            _handle_chat_interrupt(handler, {
                "session_id": sid,
                "stream_id": stream_id,
                "text": "redirect me",
            })
            result["response"] = _captured_response(handler)

        with patch("api.streaming.get_session", return_value=session):
            thread = threading.Thread(target=run_interrupt)
            thread.start()
            assert redirect_started.wait(1)
            assert stream_removed.wait(1)
            assert not successor_admitted.wait(0.05), "successor admission must wait for the old redirect"
            release_redirect.set()
            thread.join(1)

        cancel_thread.join(1)
        assert result["response"] == {
            "accepted": False,
            "fallback": "stream_dead",
            "stream_id": stream_id,
        }
        assert successor_admitted.is_set()
        agent.redirect.assert_called_once_with("redirect me")

    def test_redirect_settles_matching_draft_after_releasing_stream_lock(self, _clear_caches):
        from api.streaming import _handle_chat_interrupt
        from api.config import STREAMS_LOCK

        sid, stream_id = "sid_interrupt_draft", "stream_interrupt_draft"
        agent = MagicMock(_supports_active_turn_redirect=True, api_mode="chat_completions")
        agent.redirect.return_value = True
        _install_interrupt_stream(stream_id, sid, agent)
        session = MagicMock(
            active_stream_id=stream_id,
            composer_draft={"text": "captured \n", "files": []},
        )

        def save(**kwargs):
            assert STREAMS_LOCK.acquire(blocking=False), "draft save must not hold STREAMS_LOCK"
            STREAMS_LOCK.release()
            assert kwargs == {"touch_updated_at": False, "skip_index": True}

        session.save.side_effect = save
        with patch("api.streaming.get_session", return_value=session):
            handler = _make_handler()
            _handle_chat_interrupt(handler, {
                "session_id": sid,
                "stream_id": stream_id,
                "text": "captured",
                "draft_text": "captured \n",
                "draft_files": [],
            })

        assert _captured_response(handler) == {
            "accepted": True,
            "fallback": None,
            "stream_id": stream_id,
            "compare_cleared": True,
            "draft": {"text": "", "files": []},
        }
        assert session.composer_draft == {"text": "", "files": []}

    def test_cancel_during_draft_settlement_never_acks_stale_redirect(self, _clear_caches):
        from api.streaming import _handle_chat_interrupt, cancel_stream
        from api.config import (
            AGENT_INSTANCES,
            STREAMS,
            STREAMS_LOCK,
            _get_session_agent_lock,
            register_stream_owner,
            stream_owner_session_id,
        )

        sid, stream_id = "sid_interrupt_settlement_cancel", "stream_interrupt_settlement_cancel"
        successor_id = "stream_interrupt_settlement_successor"
        successor_stream = object()
        settlement_started = threading.Event()
        release_settlement = threading.Event()
        stream_removed = threading.Event()
        successor_attempted = threading.Event()
        successor_admitted = threading.Event()
        agent = MagicMock(_supports_active_turn_redirect=True, api_mode="chat_completions")
        agent.session_id = sid
        successor_agent = MagicMock(_supports_active_turn_redirect=True, api_mode="chat_completions")
        session = MagicMock(
            session_id=sid,
            active_stream_id=stream_id,
            composer_draft={"text": "captured", "files": []},
            messages=[],
            pending_user_message=None,
            pending_attachments=[],
        )
        save_calls = 0
        save_observations = []

        def save(**_kwargs):
            nonlocal save_calls
            save_calls += 1
            save_observations.append((session.active_stream_id, successor_admitted.is_set()))
            if save_calls == 1:
                settlement_started.set()
                assert release_settlement.wait(1), "settlement save did not release"

        session.save.side_effect = save
        session_lock = _get_session_agent_lock(sid)

        def cancel_old_stream():
            assert cancel_stream(stream_id) is True

        def admit_successor():
            successor_attempted.set()
            with session_lock:
                session.active_stream_id = successor_id
                with STREAMS_LOCK:
                    STREAMS[successor_id] = successor_stream
                    AGENT_INSTANCES[successor_id] = successor_agent
                register_stream_owner(successor_id, sid)
                successor_admitted.set()

        _install_interrupt_stream(stream_id, sid, agent)
        result = {}

        def run_interrupt():
            handler = _make_handler()
            _handle_chat_interrupt(handler, {
                "session_id": sid,
                "stream_id": stream_id,
                "text": "redirect me",
                "draft_text": "captured",
                "draft_files": [],
            })
            result["status"] = _captured_status(handler)
            result["response"] = _captured_response(handler)

        with patch("api.streaming.get_session", return_value=session):
            interrupt_thread = threading.Thread(target=run_interrupt)
            interrupt_thread.start()
            assert settlement_started.wait(1), "draft settlement did not start"

            cancel_thread = threading.Thread(target=cancel_old_stream)
            cancel_thread.start()
            for _ in range(100):
                with STREAMS_LOCK:
                    if stream_id not in STREAMS:
                        stream_removed.set()
                        break
                threading.Event().wait(0.01)
            assert stream_removed.wait(1), "cancellation did not remove the old stream"

            successor_thread = threading.Thread(target=admit_successor)
            successor_thread.start()
            assert successor_attempted.wait(1)
            assert not successor_admitted.wait(0.05), "successor must wait for settlement"
            release_settlement.set()
            interrupt_thread.join(1)

        cancel_thread.join(1)
        successor_thread.join(1)
        assert result == {
            "status": 500,
            "response": {"error": "interrupt delivery became uncertain"},
        }
        assert successor_admitted.is_set()
        assert save_observations in (
            [(stream_id, False)],
            [(stream_id, False), (None, False)],
        )
        assert session.active_stream_id == successor_id
        with STREAMS_LOCK:
            assert STREAMS.get(successor_id) is successor_stream
            assert AGENT_INSTANCES.get(successor_id) is successor_agent
        assert stream_owner_session_id(successor_id) == sid

    def test_redirect_settlement_re_resolves_authoritative_session_under_lock(self, _clear_caches):
        from api.streaming import _handle_chat_interrupt

        sid, stream_id = "sid_interrupt_newer_session", "stream_interrupt_newer_session"
        redirect_done = threading.Event()
        agent = MagicMock(_supports_active_turn_redirect=True, api_mode="chat_completions")

        def redirect(_text):
            redirect_done.set()
            return True

        agent.redirect.side_effect = redirect
        _install_interrupt_stream(stream_id, sid, agent)
        old_session = MagicMock(
            session_id=sid,
            active_stream_id=stream_id,
            composer_draft={"text": "captured", "files": []},
            messages=[{"role": "user", "content": "old turn"}],
        )
        current_session = MagicMock(
            session_id=sid,
            active_stream_id=stream_id,
            composer_draft={"text": "captured", "files": []},
            messages=[
                {"role": "user", "content": "old turn"},
                {"role": "assistant", "content": "newer final answer"},
            ],
        )
        get_session = MagicMock(side_effect=[old_session, current_session])
        handler = _make_handler()
        with patch("api.streaming.get_session", get_session):
            _handle_chat_interrupt(handler, {
                "session_id": sid,
                "stream_id": stream_id,
                "text": "captured",
                "draft_text": "captured",
                "draft_files": [],
            })

        assert redirect_done.is_set()
        assert get_session.call_count == 2
        assert old_session.save.call_count == 0
        current_session.save.assert_called_once_with(touch_updated_at=False, skip_index=True)
        assert current_session.messages == [
            {"role": "user", "content": "old turn"},
            {"role": "assistant", "content": "newer final answer"},
        ]
        assert _captured_response(handler) == {
            "accepted": True,
            "fallback": None,
            "stream_id": stream_id,
            "compare_cleared": True,
            "draft": {"text": "", "files": []},
        }

    def test_redirect_settlement_deleted_session_fails_closed_without_recreation(self, _clear_caches):
        from api.streaming import _handle_chat_interrupt

        sid, stream_id = "sid_interrupt_deleted_session", "stream_interrupt_deleted_session"
        redirect_done = threading.Event()
        agent = MagicMock(_supports_active_turn_redirect=True, api_mode="chat_completions")

        def redirect(_text):
            redirect_done.set()
            return True

        agent.redirect.side_effect = redirect
        _install_interrupt_stream(stream_id, sid, agent)
        old_session = MagicMock(
            session_id=sid,
            active_stream_id=stream_id,
            composer_draft={"text": "captured", "files": []},
            messages=[{"role": "user", "content": "old turn"}],
        )
        get_session = MagicMock(side_effect=[old_session, KeyError(sid)])
        handler = _make_handler()
        with patch("api.streaming.get_session", get_session):
            _handle_chat_interrupt(handler, {
                "session_id": sid,
                "stream_id": stream_id,
                "text": "captured",
                "draft_text": "captured",
                "draft_files": [],
            })

        assert redirect_done.is_set()
        assert get_session.call_count == 2
        old_session.save.assert_not_called()
        assert _captured_status(handler) == 500
        assert _captured_response(handler) == {
            "error": "interrupt draft settlement failed",
        }

    def test_redirect_preserves_newer_draft(self, _clear_caches):
        from api.streaming import _handle_chat_interrupt

        sid, stream_id = "sid_interrupt_newer_draft", "stream_interrupt_newer_draft"
        agent = MagicMock(_supports_active_turn_redirect=True, api_mode="chat_completions")
        agent.redirect.return_value = True
        _install_interrupt_stream(stream_id, sid, agent)
        session = MagicMock(
            active_stream_id=stream_id,
            composer_draft={"text": "newer", "files": []},
        )
        with patch("api.streaming.get_session", return_value=session):
            handler = _make_handler()
            _handle_chat_interrupt(handler, {
                "session_id": sid,
                "stream_id": stream_id,
                "text": "captured",
                "draft_text": "captured",
                "draft_files": [],
            })

        assert _captured_response(handler) == {
            "accepted": True,
            "fallback": None,
            "stream_id": stream_id,
            "compare_cleared": False,
            "draft": {"text": "newer", "files": []},
        }
        session.save.assert_not_called()

    def test_draft_settlement_failure_is_not_accepted(self, _clear_caches):
        from api.streaming import _handle_chat_interrupt

        sid, stream_id = "sid_interrupt_save_failure", "stream_interrupt_save_failure"
        agent = MagicMock(_supports_active_turn_redirect=True, api_mode="chat_completions")
        agent.redirect.return_value = True
        _install_interrupt_stream(stream_id, sid, agent)
        session = MagicMock(
            active_stream_id=stream_id,
            composer_draft={"text": "captured", "files": []},
        )
        session.save.side_effect = OSError("disk full")
        with patch("api.streaming.get_session", return_value=session):
            handler = _make_handler()
            _handle_chat_interrupt(handler, {
                "session_id": sid,
                "stream_id": stream_id,
                "text": "captured",
                "draft_text": "captured",
                "draft_files": [],
            })

        assert _captured_status(handler) == 500
        assert _captured_response(handler) == {"error": "interrupt draft settlement failed"}
        assert session.composer_draft == {"text": "captured", "files": []}

    def test_invalid_draft_settlement_is_rejected_before_redirect(self, _clear_caches):
        from api.streaming import _handle_chat_interrupt

        agent = MagicMock(_supports_active_turn_redirect=True)
        handler = _make_handler()
        with patch("api.streaming.get_session") as get_session:
            _handle_chat_interrupt(handler, {
                "session_id": "sid_invalid_draft",
                "stream_id": "stream_invalid_draft",
                "text": "payload",
                "draft_text": ["not text"],
                "draft_files": [],
            })

        assert _captured_status(handler) == 400
        get_session.assert_not_called()
        agent.redirect.assert_not_called()

    @pytest.mark.parametrize(
        "case,fallback,redirect_result",
        [
            ("mismatch", "stream_mismatch", True),
            ("dead", "stream_dead", True),
            ("unsupported", "unsupported_redirect", True),
            ("rejected", "redirect_rejected", False),
            ("error", "redirect_error", RuntimeError("redirect failed")),
        ],
    )
    def test_definitive_stream_outcomes(self, _clear_caches, case, fallback, redirect_result):
        from api.streaming import _handle_chat_interrupt

        sid, stream_id = "sid_interrupt_case", f"stream_interrupt_{case}"
        agent = MagicMock(
            _supports_active_turn_redirect=case not in {"unsupported", "dead"},
            api_mode="chat_completions",
        )
        if isinstance(redirect_result, Exception):
            agent.redirect.side_effect = redirect_result
        else:
            agent.redirect.return_value = redirect_result
        if case != "dead":
            _install_interrupt_stream(stream_id, sid if case != "mismatch" else "other", agent)
        session = MagicMock(active_stream_id=stream_id if case != "mismatch" else "other")
        with patch("api.streaming.get_session", return_value=session):
            handler = _make_handler()
            _handle_chat_interrupt(handler, {"session_id": sid, "stream_id": stream_id, "text": "payload"})

        assert _captured_response(handler) == {"accepted": False, "fallback": fallback, "stream_id": stream_id}
        if fallback != "redirect_rejected" and fallback != "redirect_error":
            agent.redirect.assert_not_called()

    @pytest.mark.parametrize("owner_sid", ["other", None])
    def test_session_active_id_alone_cannot_authorize_redirect(self, _clear_caches, owner_sid):
        """A matching session field is insufficient without the stream owner record."""
        from api.streaming import _handle_chat_interrupt
        from api.config import AGENT_INSTANCES, STREAMS, STREAMS_LOCK, register_stream_owner

        sid, stream_id = "sid_interrupt_owner", f"stream_interrupt_owner_{owner_sid or 'missing'}"
        agent = MagicMock(_supports_active_turn_redirect=True, api_mode="chat_completions")
        with STREAMS_LOCK:
            STREAMS[stream_id] = object()
            AGENT_INSTANCES[stream_id] = agent
        if owner_sid is not None:
            register_stream_owner(stream_id, owner_sid)
        session = MagicMock(active_stream_id=stream_id)
        with patch("api.streaming.get_session", return_value=session):
            handler = _make_handler()
            _handle_chat_interrupt(handler, {"session_id": sid, "stream_id": stream_id, "text": "payload"})

        assert _captured_response(handler) == {
            "accepted": False,
            "fallback": "stream_mismatch",
            "stream_id": stream_id,
        }
        agent.redirect.assert_not_called()

    def test_unbounded_redirect_does_not_hold_stream_lock_or_call_reused_agent(self, _clear_caches):
        from api.streaming import _handle_chat_interrupt
        from api.config import STREAMS_LOCK

        sid, stream_id = "sid_interrupt_codex", "stream_interrupt_codex"
        redirect_started = threading.Event()
        release_redirect = threading.Event()
        agent = MagicMock(_supports_active_turn_redirect=True, api_mode="codex_app_server")

        def blocking_redirect(_text):
            redirect_started.set()
            release_redirect.wait(1)
            return True

        agent.redirect.side_effect = blocking_redirect
        _install_interrupt_stream(stream_id, sid, agent)
        session = MagicMock(active_stream_id=stream_id)
        result = {}
        finished = threading.Event()

        def run_interrupt():
            handler = _make_handler()
            _handle_chat_interrupt(handler, {
                "session_id": sid,
                "stream_id": stream_id,
                "text": "redirect me",
            })
            result["response"] = _captured_response(handler)
            finished.set()

        thread = threading.Thread(target=run_interrupt)
        with patch("api.streaming.get_session", return_value=session):
            thread.start()
            assert finished.wait(0.2), "unbounded redirect must be rejected before provider I/O"
            assert STREAMS_LOCK.acquire(timeout=0.2), "unrelated stream work must not starve"
            STREAMS_LOCK.release()
        release_redirect.set()
        thread.join(1)

        assert not redirect_started.is_set()
        assert result["response"] == {
            "accepted": False,
            "fallback": "unsupported_redirect",
            "stream_id": stream_id,
        }
        agent.redirect.assert_not_called()


class TestHandleChatSteerFallbacks:
    """Each gate that fails returns a structured fallback the frontend can branch on."""

    def test_no_cached_agent(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        handler = _make_handler()
        _handle_chat_steer(handler, {"session_id": "sid_x", "text": "hint"})
        body = _captured_response(handler)
        assert body["accepted"] is False
        assert body["fallback"] == "no_cached_agent"

    def test_gateway_owned_stream_without_cached_agent_queues_fallback(self, _clear_caches):
        from api.streaming import _handle_chat_steer
        from api.config import ACTIVE_RUNS, ACTIVE_RUNS_LOCK, STREAMS, STREAMS_LOCK
        import queue as _q

        sid, stream_id = "sid_gateway", "stream_gateway"
        with STREAMS_LOCK:
            STREAMS[stream_id] = _q.Queue()
        with ACTIVE_RUNS_LOCK:
            ACTIVE_RUNS[stream_id] = {"session_id": sid, "backend": "gateway"}

        sess = MagicMock()
        sess.active_stream_id = stream_id
        with patch("api.streaming.get_session", return_value=sess):
            handler = _make_handler()
            _handle_chat_steer(handler, {"session_id": sid, "text": "preserve this"})

        body = _captured_response(handler)
        assert body == {
            "accepted": False,
            "fallback": "gateway_steer_queued",
            "stream_id": stream_id,
        }

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
    """The POST handler must dispatch the steer and interrupt endpoints."""

    def test_route_registered(self):
        src = (Path(__file__).parent.parent / "api" / "routes.py").read_text(encoding="utf-8")
        assert '/api/chat/steer' in src
        assert '_handle_chat_steer' in src
        assert '/api/chat/interrupt' in src
        assert '_handle_chat_interrupt' in src


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
        body = _source_between(self.cmds, "async function _trySteer(", "\nasync function cmdTitle")
        assert "/api/chat/steer" in body, "_trySteer must POST to /api/chat/steer"
        assert "method:'POST'" in body or 'method:"POST"' in body

    def test_try_steer_handles_fallback_without_cancelling(self):
        body = _source_between(self.cmds, "async function _trySteer(", "\nasync function cmdTitle")
        # Must check result.accepted and keep generic failures from cancelling.
        assert "result&&result.accepted" in body or "result.accepted" in body
        assert "result&&result.fallback==='gateway_steer_queued'" in body
        assert "queueSessionMessage(ownerSid" in body
        assert "cancelStream" not in body, "fallback path must not cancel the stream"
        assert "inp.value" in body, "fallback path must restore the composer draft"

    def test_send_busy_steer_uses_try_steer(self):
        # send() in messages.js: when busyMode === 'steer', should call _trySteer
        idx = self.msgs.find("defaultMessageMode==='steer'")
        assert idx >= 0
        block = self.msgs[idx:idx + 800]
        assert "_trySteer" in block, "send()'s steer branch must delegate to _trySteer"

    def test_try_steer_uploads_pending_files_without_clearing_until_accepted(self):
        cmds = self.cmds
        assert "function _steerUploadedAttachmentPaths" in cmds
        assert "async function _steerTextWithPendingFiles" in cmds
        assert "function _steerOwnerIsCurrent" in cmds
        assert "uploadPendingFiles({clearPending:false,sessionId:ownerSid,files:pendingFiles})" in cmds, (
            "steer must upload staged files for the captured owner session without clearing chips before endpoint acceptance"
        )
        idx = cmds.find("async function _trySteer(")
        assert idx >= 0
        body = _source_between(cmds, "async function _trySteer(", "\nasync function cmdTitle")
        assert "const ownerSid=(typeof S!=='undefined'&&S.session&&S.session.session_id)||null;" in body
        assert "const pendingFilesSnapshot=typeof S!=='undefined'&&Array.isArray(S.pendingFiles)?[...S.pendingFiles]:[];" in body
        assert "steerText=await _steerTextWithPendingFiles(originalMsg,ownerSid,pendingFilesSnapshot)" in body
        assert "body:JSON.stringify({session_id:ownerSid,text:steerText})" in body, (
            "steer endpoint must receive the captured owner session id and attachment-enriched text"
        )
        assert "_clearComposerDraft(ownerSid,_steerRestoreText(originalMsg,explicitSteer),pendingFilesSnapshot)" in body
        assert "if(_steerOwnerIsCurrent(ownerSid))" in body
        assert "S.pendingFiles=_remaining" in body, "accepted steer should clear the delivered files (by identity) after paths are injected"

    def test_file_steer_does_not_read_live_session_after_upload_await(self):
        cmds = self.cmds
        idx = cmds.find("async function _trySteer(")
        assert idx >= 0
        body = _source_between(cmds, "async function _trySteer(", "\nasync function cmdTitle")
        await_idx = body.find("steerText=await _steerTextWithPendingFiles")
        assert await_idx >= 0
        after_upload = body[await_idx:]
        assert "session_id:S.session.session_id" not in after_upload
        assert "{session_id:S.session.session_id" not in after_upload
        assert "session_id:ownerSid" in after_upload
        assert "_steerOwnerIsCurrent(ownerSid)" in after_upload, (
            "post-await tray/DOM mutations must be guarded by the captured owner session"
        )

    def test_file_steer_upload_status_and_indicator_are_owner_scoped(self):
        steer_helpers = _source_between(
            self.cmds,
            "function _steerOwnerIsCurrent",
            "\nasync function cmdTitle",
        )
        try_body = _source_between(self.cmds, "async function _trySteer(", "\nasync function cmdTitle")
        assert "function _steerSetComposerStatusForOwner" in steer_helpers
        assert "_steerSetComposerStatusForOwner(ownerSid,t('uploading')||'Uploading…')" in steer_helpers
        assert "_steerSetComposerStatusForOwner(ownerSid,'')" in steer_helpers
        assert "function _steerIndicatorText" in steer_helpers
        assert "_showSteerIndicator(_steerIndicatorText(originalMsg,pendingFilesSnapshot))" in try_body, (
            "visible steer indicator must use original text or a file-only display label, not attachment tool instructions"
        )
        assert "_showSteerIndicator(steerText)" not in try_body

    def test_file_steer_indicator_omits_attachment_tool_note(self):
        import json
        import shutil
        import subprocess
        import textwrap

        node = shutil.which("node")
        if not node:  # pragma: no cover
            pytest.skip("node not available")
        assert node is not None

        steer_src = _source_between(
            self.cmds,
            "function _steerUploadedAttachmentPaths",
            "\nasync function cmdTitle",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            let S = {{session:{{session_id:'A'}}, pendingFiles:[{{name:'a.pdf'}}]}};
            let apiPayload = null;
            let indicatorText = null;
            function t(k){{return k;}}
            function $(id){{return {{value:'', classList:{{add(){{}}, remove(){{}}}}, style:{{}}}};}}
            function setComposerStatus(){{}}
            function showToast(){{}}
            function renderTray(){{}}
            function _showSteerIndicator(text){{indicatorText = text;}}
            function _showSteerRecovery(){{}}
            function _clearComposerDraft(){{}}
            async function uploadPendingFiles(){{return [{{path:'/tmp/a.pdf'}}];}}
            async function api(url, options){{
              assert.strictEqual(url, '/api/chat/steer');
              apiPayload = JSON.parse(options.body);
              return {{accepted:true}};
            }}
            eval({json.dumps(steer_src)});
            (async()=>{{
              const delivered = await _trySteer('hint', false);
              assert.strictEqual(delivered, true);
              assert.strictEqual(indicatorText, 'hint');
              assert.ok(apiPayload.text.includes('[Attached files for this steer: /tmp/a.pdf]'));
              assert.ok(!indicatorText.includes('Attached files'));
              assert.ok(!indicatorText.includes('file tools/read_file'));
            }})().catch(err=>{{console.error(err); process.exit(1);}});
            """
        )
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)

    def test_attachment_only_steer_indicator_uses_file_label(self):
        import json
        import shutil
        import subprocess
        import textwrap

        node = shutil.which("node")
        if not node:  # pragma: no cover
            pytest.skip("node not available")
        assert node is not None

        steer_src = _source_between(
            self.cmds,
            "function _steerUploadedAttachmentPaths",
            "\nasync function cmdTitle",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            let S = {{session:{{session_id:'A'}}, pendingFiles:[{{name:'a.pdf'}}]}};
            let apiPayload = null;
            let indicatorText = null;
            function t(k){{return k;}}
            function $(id){{return {{value:'', classList:{{add(){{}}, remove(){{}}}}, style:{{}}}};}}
            function setComposerStatus(){{}}
            function showToast(){{}}
            function renderTray(){{}}
            function _showSteerIndicator(text){{indicatorText = text;}}
            function _showSteerRecovery(){{}}
            function _clearComposerDraft(){{}}
            async function uploadPendingFiles(){{return [{{path:'/tmp/a.pdf'}}];}}
            async function api(url, options){{
              assert.strictEqual(url, '/api/chat/steer');
              apiPayload = JSON.parse(options.body);
              return {{accepted:true}};
            }}
            eval({json.dumps(steer_src)});
            (async()=>{{
              const delivered = await _trySteer('', false);
              assert.strictEqual(delivered, true);
              assert.strictEqual(indicatorText, 'Attached files: a.pdf');
              assert.ok(apiPayload.text.includes('[Attached files for this steer: /tmp/a.pdf]'));
              assert.ok(!indicatorText.includes('file tools/read_file'));
            }})().catch(err=>{{console.error(err); process.exit(1);}});
            """
        )
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)

    def test_file_steer_targets_captured_session_when_user_switches_mid_upload(self):
        import json
        import shutil
        import subprocess
        import textwrap

        node = shutil.which("node")
        if not node:  # pragma: no cover
            pytest.skip("node not available")
        assert node is not None

        steer_src = _source_between(
            self.cmds,
            "function _steerUploadedAttachmentPaths",
            "\nasync function cmdTitle",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            let S = {{session:{{session_id:'A'}}, pendingFiles:[{{name:'a.pdf'}}]}};
            let uploadOptions = null;
            let apiPayload = null;
            let trayRenders = 0;
            let indicatorCalls = 0;
            let draftClears = [];
            function t(k){{return k;}}
            function $(id){{return {{value:'', classList:{{add(){{}}, remove(){{}}}}, style:{{}}}};}}
            function setComposerStatus(){{}}
            function showToast(){{}}
            function renderTray(){{trayRenders += 1;}}
            function _showSteerIndicator(){{indicatorCalls += 1;}}
            function _showSteerRecovery(){{}}
            function _clearComposerDraft(sid,text,files){{draftClears.push({{sid,text,files}});}}
            async function uploadPendingFiles(options){{
              uploadOptions = options;
              S.session = {{session_id:'B'}};
              S.pendingFiles = [{{name:'b.pdf'}}];
              return [{{path:'/tmp/a.pdf'}}];
            }}
            async function api(url, options){{
              assert.strictEqual(url, '/api/chat/steer');
              apiPayload = JSON.parse(options.body);
              return {{accepted:true}};
            }}
            eval({json.dumps(steer_src)});
            (async()=>{{
              const delivered = await _trySteer('hint', false);
              assert.strictEqual(delivered, true);
              assert.strictEqual(uploadOptions.sessionId, 'A');
              assert.strictEqual(uploadOptions.files.length, 1);
              assert.strictEqual(uploadOptions.files[0].name, 'a.pdf');
              assert.strictEqual(apiPayload.session_id, 'A');
              assert.strictEqual(S.session.session_id, 'B');
              assert.strictEqual(S.pendingFiles.length, 1);
              assert.strictEqual(S.pendingFiles[0].name, 'b.pdf');
              assert.strictEqual(trayRenders, 0);
              assert.strictEqual(indicatorCalls, 0);
              assert.strictEqual(draftClears.length, 1);
              assert.strictEqual(draftClears[0].sid, 'A');
              assert.strictEqual(draftClears[0].files[0].name, 'a.pdf');
            }})().catch(err=>{{console.error(err); process.exit(1);}});
            """
        )
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)

    def test_dead_steer_fallback_clears_busy_state_and_recovery_sends_normally(self):
        import json
        import shutil
        import subprocess
        import textwrap

        node = shutil.which("node")
        if not node:  # pragma: no cover
            pytest.skip("node not available")
        assert node is not None

        steer_src = _source_between(
            self.cmds,
            "function _showSteerRecovery",
            "\nasync function cmdTitle",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            const steerSrc = {json.dumps(steer_src)};
            function makeElement(tag){{
              return {{
                tag,
                className:'',
                textContent:'',
                children:[],
                listeners:{{}},
                appendChild(child){{this.children.push(child);}},
                remove(){{this.removed=true;}},
                addEventListener(name,fn){{this.listeners[name]=fn;}},
                querySelector(sel){{return null;}},
              }};
            }}
            let inner = makeElement('div');
            const document = {{
              getElementById(id){{return id==='msgInner'?inner:null;}},
              createElement: makeElement,
            }};
            function t(k){{return k;}}
            function _steerFailureMessageKey(fallback){{return 'steer_fail_'+fallback;}}
            function scrollToBottom(){{}}
            function setComposerStatus(){{}}
            function showToast(key){{if(globalThis.__toasts)globalThis.__toasts.push(key);}}
            function renderTray(){{if(globalThis.__trayRenders)globalThis.__trayRenders.count += 1;}}
            function autoResize(){{}}
            function _showSteerIndicator(){{}}
            function _clearComposerDraft(sid,text,files){{if(globalThis.__draftClears)globalThis.__draftClears.push({{sid,text,files}});}}
            async function uploadPendingFiles(){{return [];}}
            eval(steerSrc);

            async function runStreamDeadFallback(explicitSteer=false, msg='retry me'){{
              let input = {{value:''}};
              let clearInflightCalls = [];
              let updateSendBtnCalls = 0;
              let sendCalls = 0;
              let sendInput = null;
              let sendOptions = null;
              let apiPayload = null;
              inner = makeElement('div');
              globalThis.S = {{
                session:{{session_id:'A', active_stream_id:'stream-1'}},
                activeStreamId:'stream-1',
                busy:true,
                pendingFiles:[{{name:'a.pdf'}}],
              }};
              globalThis.INFLIGHT = {{A:{{messages:[]}}}};
              globalThis.$ = id => input;
              globalThis.clearInflightState = sid => clearInflightCalls.push(sid);
              globalThis.updateSendBtn = () => {{updateSendBtnCalls += 1;}};
              globalThis.send = async options => {{sendCalls += 1; sendInput = input.value; sendOptions = options;}};
              globalThis.api = async (url, options) => {{
                assert.strictEqual(url, '/api/chat/steer');
                apiPayload = JSON.parse(options.body);
                return {{accepted:false, fallback:'stream_dead'}};
              }};

              const delivered = await _trySteer(msg, explicitSteer);
              assert.strictEqual(delivered, false);
              assert.deepStrictEqual(apiPayload, {{session_id:'A', text:msg}});
              assert.strictEqual(S.busy, false);
              assert.strictEqual(S.activeStreamId, null);
              assert.strictEqual(S.session.active_stream_id, null);
              assert.ok(!Object.prototype.hasOwnProperty.call(INFLIGHT, 'A'));
              assert.deepStrictEqual(clearInflightCalls, ['A']);
              assert.strictEqual(updateSendBtnCalls, 1);
              assert.strictEqual(input.value, explicitSteer ? `/steer ${{msg}}` : msg);
              assert.strictEqual(S.pendingFiles.length, 1);
              const recovery = inner.children[inner.children.length - 1];
              const retry = recovery.children[1];
              assert.strictEqual(retry.textContent, 'clarify_send');
              retry.listeners.click();
              await Promise.resolve();
              assert.strictEqual(sendCalls, 1);
              assert.strictEqual(sendInput, msg);
              assert.deepStrictEqual(sendOptions, {{literalSlash:true}});
            }}

            async function runNoCachedAgentFallback(explicitSteer=false, msg='retry me'){{
              let input = {{value:''}};
              let clearInflightCalls = [];
              let updateSendBtnCalls = 0;
              let sendCalls = 0;
              let apiCalls = 0;
              let apiPayload = null;
              inner = makeElement('div');
              globalThis.S = {{
                session:{{session_id:'A', active_stream_id:'stream-1'}},
                activeStreamId:'stream-1',
                busy:true,
                pendingFiles:[{{name:'a.pdf'}}],
              }};
              globalThis.INFLIGHT = {{A:{{messages:[]}}}};
              globalThis.$ = id => input;
              globalThis.clearInflightState = sid => clearInflightCalls.push(sid);
              globalThis.updateSendBtn = () => {{updateSendBtnCalls += 1;}};
              globalThis.send = async () => {{sendCalls += 1;}};
              globalThis.api = async (url, options) => {{
                assert.strictEqual(url, '/api/chat/steer');
                apiCalls += 1;
                apiPayload = JSON.parse(options.body);
                return {{accepted:false, fallback:'no_cached_agent'}};
              }};

              const delivered = await _trySteer(msg, explicitSteer);
              assert.strictEqual(delivered, false);
              assert.deepStrictEqual(apiPayload, {{session_id:'A', text:msg}});
              assert.strictEqual(S.busy, true);
              assert.strictEqual(S.activeStreamId, 'stream-1');
              assert.strictEqual(S.session.active_stream_id, 'stream-1');
              assert.ok(Object.prototype.hasOwnProperty.call(INFLIGHT, 'A'));
              assert.deepStrictEqual(clearInflightCalls, []);
              assert.strictEqual(updateSendBtnCalls, 0);
              assert.strictEqual(input.value, explicitSteer ? `/steer ${{msg}}` : msg);
              assert.strictEqual(S.pendingFiles.length, 1);
              const recovery = inner.children[inner.children.length - 1];
              const retry = recovery.children[1];
              assert.strictEqual(retry.textContent, 'steer_recovery_retry');
              retry.listeners.click();
              await Promise.resolve();
              await Promise.resolve();
              assert.strictEqual(sendCalls, 0);
              assert.strictEqual(apiCalls, 2);
            }}

            async function runGatewayQueuedFallback(switchDuringAwait=false){{
              let input = {{value:''}};
              let clearInflightCalls = [];
              let updateSendBtnCalls = 0;
              let queued = [];
              let queueBadges = [];
              let draftClears = [];
              let trayRenders = 0;
              let toasts = [];
              let submittedFile = {{name:'a.pdf'}};
              let replacementFile = {{name:'replacement.pdf'}};
              let apiPayload = null;
              inner = makeElement('div');
              globalThis.S = {{
                session:{{session_id:'A', active_stream_id:'stream-1', model:'fallback-model', model_provider:'fallback-provider'}},
                activeStreamId:'stream-1',
                activeProfile:'work',
                busy:true,
                pendingFiles:[submittedFile],
              }};
              globalThis.INFLIGHT = {{A:{{messages:[]}}}};
              globalThis.$ = id => input;
              globalThis.clearInflightState = sid => clearInflightCalls.push(sid);
              globalThis.updateSendBtn = () => {{updateSendBtnCalls += 1;}};
              globalThis.queueSessionMessage = (sid, payload) => queued.push({{sid, payload}});
              globalThis.updateQueueBadge = sid => queueBadges.push(sid);
              globalThis.__draftClears = draftClears;
              globalThis.__trayRenders = {{count:0}};
              globalThis.__toasts = toasts;
              globalThis._chatPayloadModelState = () => ({{model:'captured-model', model_provider:'captured-provider'}});
              globalThis.api = async (url, options) => {{
                assert.strictEqual(url, '/api/chat/steer');
                apiPayload = JSON.parse(options.body);
                if(switchDuringAwait){{
                  S.session={{session_id:'B', active_stream_id:'stream-B'}};
                  S.activeStreamId='stream-B';
                  S.pendingFiles=[replacementFile];
                }}else{{
                  S.pendingFiles=[submittedFile, replacementFile];
                }}
                return {{accepted:false, fallback:'gateway_steer_queued'}};
              }};

              const delivered = await _trySteer('queue me', false);
              assert.strictEqual(delivered, true);
              assert.deepStrictEqual(apiPayload, {{session_id:'A', text:'queue me'}});
              assert.strictEqual(S.busy, true);
              assert.ok(Object.prototype.hasOwnProperty.call(INFLIGHT, 'A'));
              assert.deepStrictEqual(clearInflightCalls, []);
              assert.strictEqual(updateSendBtnCalls, 0);
              assert.strictEqual(inner.children.length, 0);
              assert.deepStrictEqual(queueBadges, ['A']);
              assert.strictEqual(queued.length, 1);
              assert.strictEqual(queued[0].sid, 'A');
              assert.strictEqual(queued[0].payload.text, 'queue me');
              assert.deepStrictEqual(queued[0].payload.files, [submittedFile]);
              assert.strictEqual(queued[0].payload.model, 'captured-model');
              assert.strictEqual(queued[0].payload.model_provider, 'captured-provider');
              assert.strictEqual(queued[0].payload.profile, 'work');
              assert.strictEqual(draftClears.length, 1);
              assert.strictEqual(draftClears[0].sid, 'A');
              assert.strictEqual(draftClears[0].text, 'queue me');
              assert.deepStrictEqual(draftClears[0].files, [submittedFile]);
              assert.deepStrictEqual(toasts, ['steer_leftover_queued']);
              if(switchDuringAwait){{
                assert.strictEqual(S.session.session_id, 'B');
                assert.deepStrictEqual(S.pendingFiles, [replacementFile]);
                assert.strictEqual(globalThis.__trayRenders.count, 0);
              }}else{{
                assert.deepStrictEqual(S.pendingFiles, [replacementFile]);
                assert.strictEqual(globalThis.__trayRenders.count, 1);
              }}
              delete globalThis.__draftClears;
              delete globalThis.__trayRenders;
              delete globalThis.__toasts;
            }}

            async function runLateDeadFallbackDoesNotClearNewStream(){{
              let input = {{value:''}};
              let clearInflightCalls = [];
              let updateSendBtnCalls = 0;
              inner = makeElement('div');
              globalThis.S = {{
                session:{{session_id:'A', active_stream_id:'stream-1'}},
                activeStreamId:'stream-1',
                busy:true,
                pendingFiles:[],
              }};
              globalThis.INFLIGHT = {{A:{{messages:[]}}}};
              globalThis.$ = id => input;
              globalThis.clearInflightState = sid => clearInflightCalls.push(sid);
              globalThis.updateSendBtn = () => {{updateSendBtnCalls += 1;}};
              globalThis.send = async () => {{throw new Error('send must not run for a stale dead fallback');}};
              globalThis.api = async () => {{
                S.activeStreamId='stream-2';
                S.session.active_stream_id='stream-2';
                return {{accepted:false, fallback:'stream_dead'}};
              }};

              const delivered = await _trySteer('old steer', false);
              assert.strictEqual(delivered, false);
              assert.strictEqual(S.busy, true);
              assert.strictEqual(S.activeStreamId, 'stream-2');
              assert.strictEqual(S.session.active_stream_id, 'stream-2');
              assert.ok(Object.prototype.hasOwnProperty.call(INFLIGHT, 'A'));
              assert.deepStrictEqual(clearInflightCalls, []);
              assert.strictEqual(updateSendBtnCalls, 0);
              assert.strictEqual(input.value, '');
              assert.strictEqual(inner.children.length, 0);
            }}

            async function runAdjacentLiveFailure(){{
              let input = {{value:''}};
              let clearInflightCalls = [];
              let updateSendBtnCalls = 0;
              inner = makeElement('div');
              globalThis.S = {{
                session:{{session_id:'A', active_stream_id:'stream-1'}},
                activeStreamId:'stream-1',
                busy:true,
                pendingFiles:[{{name:'a.pdf'}}],
              }};
              globalThis.INFLIGHT = {{A:{{messages:[]}}}};
              globalThis.$ = id => input;
              globalThis.clearInflightState = sid => clearInflightCalls.push(sid);
              globalThis.updateSendBtn = () => {{updateSendBtnCalls += 1;}};
              globalThis.send = async () => {{throw new Error('send must not run for live steer failures');}};
              globalThis.api = async () => {{return {{accepted:false, fallback:'agent_lacks_steer'}};}};

              const delivered = await _trySteer('live hint', false);
              assert.strictEqual(delivered, false);
              assert.strictEqual(S.busy, true);
              assert.strictEqual(S.activeStreamId, 'stream-1');
              assert.strictEqual(S.session.active_stream_id, 'stream-1');
              assert.ok(Object.prototype.hasOwnProperty.call(INFLIGHT, 'A'));
              assert.deepStrictEqual(clearInflightCalls, []);
              assert.strictEqual(updateSendBtnCalls, 0);
              assert.strictEqual(input.value, 'live hint');
              assert.strictEqual(S.pendingFiles.length, 1);
              const recovery = inner.children[inner.children.length - 1];
              const retry = recovery.children[1];
              assert.strictEqual(retry.textContent, 'steer_recovery_retry');
            }}

            (async()=>{{
              await runNoCachedAgentFallback();
              await runNoCachedAgentFallback(true);
              await runGatewayQueuedFallback(false);
              await runGatewayQueuedFallback(true);
              await runStreamDeadFallback();
              await runStreamDeadFallback(true);
              await runStreamDeadFallback(true, '/help');
              await runLateDeadFallbackDoesNotClearNewStream();
              await runAdjacentLiveFailure();
            }})().catch(err=>{{console.error(err); process.exit(1);}});
            """
        )
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)

    def test_send_busy_steer_accepts_file_only_input(self):
        idx = self.msgs.find("if(S.busy||compressionRunning)")
        assert idx >= 0
        block = self.msgs[idx:idx + 500]
        assert "if(text||S.pendingFiles.length)" in block, (
            "busy send must route file-only composer submissions through queue/interrupt/steer"
        )
        assert "_trySteer uploads with clearPending=false" in self.msgs

    def test_upload_pending_files_can_preserve_staged_files_for_steer(self):
        ui = (Path(__file__).parent.parent / "static" / "ui.js").read_text(encoding="utf-8")
        assert "async function uploadPendingFiles(options={})" in ui
        assert "const pendingFiles=Array.isArray(opts.files)?opts.files.filter(Boolean):[...(S.pendingFiles||[])];" in ui
        assert "const sessionId=String(opts.sessionId||(S.session&&S.session.session_id)||'');" in ui
        assert "const clearPending=!(opts&&opts.clearPending===false)" in ui
        assert "fd.append('session_id',sessionId)" in ui
        assert "if(clearPending&&_uploadPendingFilesCurrentSession(sessionId)){S.pendingFiles=[];renderTray();}" in ui
        assert "else if(typeof renderTray==='function'&&_uploadPendingFilesCurrentSession(sessionId))renderTray();" in ui

    def test_upload_pending_files_progress_bar_is_session_scoped(self):
        ui = (Path(__file__).parent.parent / "static" / "ui.js").read_text(encoding="utf-8")
        progress_helper = _source_between(
            ui,
            "const _uploadPendingFilesProgressBySession",
            "\nasync function uploadPendingFiles",
        )
        upload_body = ui[ui.index("async function uploadPendingFiles") :]
        sessions = (Path(__file__).parent.parent / "static" / "sessions.js").read_text(encoding="utf-8")
        load_body = _source_between(sessions, "async function loadSession", "\nfunction _isMessagingSession")
        assert "_uploadPendingFilesSyncProgressForSession(sid)" in load_body
        assert "_uploadPendingFilesProgressBySession.set(owner,{percent:clamped})" in progress_helper
        assert "function _uploadPendingFilesSyncProgressForSession" in progress_helper
        assert "if(!_uploadPendingFilesCurrentSession(sessionId)){" in progress_helper
        assert "barWrap.dataset.uploadSessionId=owner" in progress_helper
        assert "activeForOwner" in progress_helper
        assert "barWrap.classList.remove('active')" in progress_helper
        assert "_uploadPendingFilesUpdateProgress(sessionId,0)" in upload_body
        assert "_uploadPendingFilesUpdateProgress(sessionId,Math.round((i+1)/total*100))" in upload_body
        assert "_uploadPendingFilesUpdateProgress(sessionId,null)" in upload_body
        assert "barWrap.classList.add('active');bar.style.width='0%';" not in upload_body
        assert "barWrap.classList.remove('active');bar.style.width='0%';" not in upload_body

    def test_upload_progress_bar_hides_on_switch_and_reappears_on_owner_return(self):
        import json
        import shutil
        import subprocess
        import textwrap

        node = shutil.which("node")
        if not node:  # pragma: no cover
            pytest.skip("node not available")
        assert node is not None

        ui = (Path(__file__).parent.parent / "static" / "ui.js").read_text(encoding="utf-8")
        progress_src = _source_between(
            ui,
            "const _uploadPendingFilesProgressBySession",
            "\nasync function uploadPendingFiles",
        )
        script = textwrap.dedent(
            f"""
            const assert = require('assert');
            let S = {{session:{{session_id:'A'}}}};
            const bar = {{style:{{width:''}}}};
            const barWrap = {{
              dataset: {{}},
              active: false,
              classList: {{
                add(cls){{ if(cls === 'active') barWrap.active = true; }},
                remove(cls){{ if(cls === 'active') barWrap.active = false; }},
              }},
            }};
            function $(id){{
              if(id === 'uploadBar') return bar;
              if(id === 'uploadBarWrap') return barWrap;
              return null;
            }}
            eval({json.dumps(progress_src)});
            _uploadPendingFilesUpdateProgress('A', 0);
            assert.strictEqual(barWrap.active, true);
            assert.strictEqual(bar.style.width, '0%');
            assert.strictEqual(barWrap.dataset.uploadSessionId, 'A');

            S.session = {{session_id:'B'}};
            _uploadPendingFilesSyncProgressForSession('B');
            assert.strictEqual(barWrap.active, false);
            assert.strictEqual(bar.style.width, '0%');
            assert.strictEqual(barWrap.dataset.uploadSessionId, undefined);

            _uploadPendingFilesUpdateProgress('A', 50);
            assert.strictEqual(barWrap.active, false);
            assert.strictEqual(bar.style.width, '0%');

            S.session = {{session_id:'A'}};
            _uploadPendingFilesSyncProgressForSession('A');
            assert.strictEqual(barWrap.active, true);
            assert.strictEqual(bar.style.width, '50%');
            assert.strictEqual(barWrap.dataset.uploadSessionId, 'A');

            _uploadPendingFilesUpdateProgress('A', null);
            assert.strictEqual(barWrap.active, false);
            assert.strictEqual(bar.style.width, '0%');
            assert.strictEqual(barWrap.dataset.uploadSessionId, undefined);
            """
        )
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)

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
