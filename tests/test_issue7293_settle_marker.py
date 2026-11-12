"""#7293 — a run the WebUI server settles must not be marked "cancelled by the user".

The restart/shutdown drain settles still-active runs through ``cancel_stream``
(the same partial-preserving path as a user Stop) with ``initiator="server"``.
These tests drive the real ``cancel_stream`` against an isolated session and pin:

* the server-initiated marker names the server, never the user, and keeps the
  ``**Task cancelled:**`` heading every cancel-marker detector relies on;
* partial output streamed before the settle is preserved exactly like a user
  cancel preserves it;
* the default user-cancel marker stays byte-identical;
* the client-side fallback marker follows ``cancel_initiator`` too.
"""

from __future__ import annotations

import pathlib
import queue
import threading
import time
from unittest.mock import Mock

import pytest

import api.config as config
import api.models as models
import api.streaming as streaming
from api.models import Session
from api.run_drain import RESTART_SETTLE_REASON, SHUTDOWN_SETTLE_REASON

REPO = pathlib.Path(__file__).resolve().parent.parent

USER_CANCEL_MARKER = (
    "**Task cancelled:** Task cancelled.\n\n"
    "*The run was cancelled by the user before Hermes finished. No provider failure occurred.*"
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "load_settings", lambda: {})
    models.SESSIONS.clear()
    maps = (
        config.STREAMS, config.CANCEL_FLAGS, config.AGENT_INSTANCES,
        config.STREAM_PARTIAL_TEXT, config.STREAM_REASONING_TEXT, config.STREAM_LIVE_TOOL_CALLS,
    )
    saved = [dict(m) for m in maps]
    for m in maps:
        m.clear()
    config.SESSION_AGENT_LOCKS.clear()
    try:
        yield
    finally:
        models.SESSIONS.clear()
        for m, old in zip(maps, saved, strict=True):
            m.clear()
            m.update(old)
        config.SESSION_AGENT_LOCKS.clear()


def _live_turn(
    sid: str, stream_id: str, *, partial: str = "", started_at: float | None = None
) -> tuple[queue.Queue, Mock]:
    s = Session(session_id=sid, title="drain", messages=[])
    s.pending_user_message = "write the report"
    s.pending_attachments = []
    s.pending_started_at = started_at
    s.active_stream_id = stream_id
    s.save()
    models.SESSIONS[sid] = s
    q = queue.Queue()
    config.STREAMS[stream_id] = q
    config.CANCEL_FLAGS[stream_id] = threading.Event()
    agent = Mock()
    agent.session_id = sid
    config.AGENT_INSTANCES[stream_id] = agent
    config.STREAM_PARTIAL_TEXT[stream_id] = partial
    return q, agent


def _assistant(sid: str) -> list[dict]:
    return [m for m in models.SESSIONS[sid].messages if isinstance(m, dict) and m.get("role") == "assistant"]


def _cancel_event(q: queue.Queue) -> dict:
    while True:
        kind, payload = q.get_nowait()
        if kind == "cancel":
            return payload


@pytest.mark.parametrize("reason", [SHUTDOWN_SETTLE_REASON, RESTART_SETTLE_REASON])
def test_server_settle_marker_names_the_server_not_the_user(reason):
    q, agent = _live_turn("s7293_srv", "st7293_srv", partial="Halfway through the report")

    assert streaming.cancel_stream("st7293_srv", reason=reason, initiator="server") is True

    msgs = _assistant("s7293_srv")
    marker = msgs[-1]
    content = marker["content"]
    assert content.startswith(f"**Task cancelled:** {reason}.")
    assert "cancelled by the user" not in content.lower()
    assert "The WebUI server stopped this run before Hermes finished" in content
    assert "you did not cancel it" in content
    assert marker["provider_details"] == reason
    assert marker["_error"] is True
    assert marker["cancel_initiator"] == "server"
    # Still a cancel marker for every dedupe / recovery detector.
    assert streaming._session_has_cancel_marker(models.SESSIONS["s7293_srv"])
    agent.interrupt.assert_called_once_with(reason)

    event = _cancel_event(q)
    assert event["message"] == reason
    assert event["cancel_initiator"] == "server"


def test_server_settle_preserves_partial_output_like_a_user_cancel():
    _live_turn("s7293_part", "st7293_part", partial="tick0 tick1")
    streaming.cancel_stream("st7293_part", reason=SHUTDOWN_SETTLE_REASON, initiator="server")

    msgs = _assistant("s7293_part")
    partials = [m for m in msgs if m.get("_partial")]
    assert [m.get("content") for m in partials] == ["tick0 tick1"]
    # Partial first, then the settle marker; the user's turn is kept.
    assert msgs.index(partials[0]) < len(msgs) - 1
    assert msgs[-1].get("cancel_initiator") == "server"
    s = models.SESSIONS["s7293_part"]
    assert any(m.get("role") == "user" and m.get("content") == "write the report" for m in s.messages)
    assert s.active_stream_id is None and s.pending_user_message is None
    # Partial output persisted durably, not only in memory.
    on_disk = Session.load("s7293_part")
    assert any(m.get("_partial") and m.get("content") == "tick0 tick1" for m in on_disk.messages)


def test_server_settle_wins_against_a_worker_that_finalizes_once_interrupted():
    """Codex r1 #1: the settle must not lose partials to the worker's own cancel.

    ``agent.interrupt()`` releases the worker. If the released worker's cancel
    finalizer ran before ``cancel_stream`` took the session lock, it cleared
    ``active_stream_id`` and wrote the *user* cancel marker without partials;
    the settle then saw a stale stream and skipped its own writeback. Here the
    fake ``interrupt`` releases a worker that finalizes as fast as it can (the
    worst case), and the settle must still own the durable terminal state.
    """
    sid, stream_id = "s7293_race", "st7293_race"
    q, agent = _live_turn(sid, stream_id, partial="tick0 tick1")
    config.register_session_writeback_owner(sid, stream_id)
    worker_done = threading.Event()

    def _released_worker():
        # What _run_agent_streaming does once the interrupt lets it unwind.
        with config._get_session_agent_lock(sid):
            streaming._finalize_cancelled_turn(models.SESSIONS[sid], stream_id=stream_id)
        worker_done.set()

    def _interrupt(_message):
        threading.Thread(target=_released_worker, daemon=True).start()
        # Give the released worker every chance to finalize first.
        worker_done.wait(2.0)

    agent.interrupt.side_effect = _interrupt
    try:
        assert streaming.cancel_stream(stream_id, reason=SHUTDOWN_SETTLE_REASON, initiator="server") is True
        assert worker_done.wait(5.0), "the released worker never finalized"
    finally:
        config.clear_session_writeback_owner_if_owned(sid, stream_id)

    msgs = _assistant(sid)
    assert [m.get("content") for m in msgs if m.get("_partial")] == ["tick0 tick1"], msgs
    markers = [m for m in msgs if str(m.get("content") or "").startswith("**Task cancelled:**")]
    assert len(markers) == 1, msgs
    assert markers[0].get("cancel_initiator") == "server", msgs
    assert not any("cancelled by the user" in str(m.get("content") or "").lower() for m in msgs), msgs
    agent.interrupt.assert_called_once_with(SHUTDOWN_SETTLE_REASON)
    # The first terminal event the client sees is the server settle.
    assert _cancel_event(q).get("cancel_initiator") == "server"
    on_disk = Session.load(sid)
    assert any(m.get("_partial") and m.get("content") == "tick0 tick1" for m in on_disk.messages)
    assert any(m.get("cancel_initiator") == "server" for m in on_disk.messages)


def test_user_stop_still_interrupts_before_session_cleanup():
    """The settle serialization is server-only: a user Stop keeps its existing
    order (interrupt first, then the session cleanup) and latency."""
    sid, stream_id = "s7293_uorder", "st7293_uorder"
    _q, agent = _live_turn(sid, stream_id, partial="tick0")
    seen = {}

    def _interrupt(_message):
        lock = config._get_session_agent_lock(sid)
        free = lock.acquire(blocking=False)
        if free:
            lock.release()
        seen["session_lock_free"] = free
        seen["active_stream_id"] = models.SESSIONS[sid].active_stream_id

    agent.interrupt.side_effect = _interrupt
    assert streaming.cancel_stream(stream_id) is True
    assert seen == {"session_lock_free": True, "active_stream_id": stream_id}


def test_user_cancel_marker_is_byte_identical():
    q, agent = _live_turn("s7293_usr", "st7293_usr")
    streaming.cancel_stream("st7293_usr")

    marker = _assistant("s7293_usr")[-1]
    assert marker["content"] == USER_CANCEL_MARKER
    assert marker["provider_details"] == "Task cancelled."
    assert "cancel_initiator" not in marker
    agent.interrupt.assert_called_once_with("Cancelled by user")
    event = _cancel_event(q)
    assert event["message"] == "Cancelled by user"
    assert "cancel_initiator" not in event


def test_reason_without_server_initiator_keeps_the_user_hint():
    """``reason`` alone changes the detail only; who stopped it is explicit."""
    content = streaming._cancelled_turn_content("Stopped from the toolbar", "Hermes")
    assert content == (
        "**Task cancelled:** Stopped from the toolbar.\n\n"
        "*The run was cancelled by the user before Hermes finished. No provider failure occurred.*"
    )
    assert streaming._cancelled_turn_hint("Hermes") == (
        "The run was cancelled by the user before Hermes finished. No provider failure occurred."
    )
    assert streaming._cancelled_turn_hint("Hermes", initiator="server") == (
        "The WebUI server stopped this run before Hermes finished; you did not cancel it. "
        "No provider failure occurred."
    )


def test_client_fallback_marker_follows_cancel_initiator():
    src = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
    start = src.find("source.addEventListener('cancel'")
    end = src.find("for(const _runJournalEventName", start)
    assert start != -1 and end != -1
    block = src[start:end]
    assert "_cancelData.cancel_initiator==='server'" in block
    assert "you did not cancel it" in block
    # The user-cancel copy is still the exact server-side user marker.
    assert (
        "`The run was cancelled by the user before ${cancelAgentName} finished. "
        "No provider failure occurred.`" in block
    )
    assert "'Task cancelled.'" in block


def test_server_settle_of_a_run_with_no_output_yet_keeps_the_turn_recoverable():
    """A run settled while still launching has streamed nothing: there is no
    partial to keep, so the marker carries the exact-stream journal recovery
    capability instead (same as a user Stop at that point). This is the
    legitimate "no partial" case the real-process tests avoid by waiting for
    the fake agent's first output before they settle."""
    _live_turn("s7293_empty", "st7293_empty", partial="", started_at=time.time())
    streaming.cancel_stream("st7293_empty", reason=SHUTDOWN_SETTLE_REASON, initiator="server")

    msgs = _assistant("s7293_empty")
    assert not [m for m in msgs if m.get("_partial")]
    marker = msgs[-1]
    assert marker.get("cancel_initiator") == "server"
    assert marker.get("_pending_journal_recovery") is True
    assert marker.get("_journal_retry_stream_id") == "st7293_empty"
    s = models.SESSIONS["s7293_empty"]
    assert any(m.get("role") == "user" and m.get("content") == "write the report" for m in s.messages)
