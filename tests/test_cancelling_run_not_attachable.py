"""A cancelling run is lifecycle-busy but NOT attachable live UI work.

``ACTIVE_RUNS`` tracks worker lifecycle. ``cancel_stream()`` deliberately keeps
the row as ``phase="cancelling"`` while the worker unwinds so a successor turn
cannot start on top of it. The client, however, has already reached a terminal
state for that stream: its run journal ends in a terminal event.

Before this fix the recovery lookups treated every same-session ``ACTIVE_RUNS``
row as attachable, so an idle session whose sidecar had already cleared
``active_stream_id`` still received a recovered ``server_turn_started`` for the
cancelled stream on EVERY ``/api/session/stream`` subscription. The client
attached, replayed the terminal event, tore the renderer down, resubscribed, and
the server replayed the same frame again — an endless attach/replay loop.

These tests pin both directions of the resulting contract:

* browser recovery excludes cancelling rows, while busy checks still keep a
  FRESH cancellation busy (a successor must not overlap the unwinding worker);
* a cancelling row past the bounded unwind window with no live ``STREAMS``
  channel is reclaimed, so a wedged worker cannot suppress wakeups forever;
* a cancelling row that still owns a live ``STREAMS`` channel is NOT reclaimed
  on age alone.
"""

import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from api import background_process as bp  # noqa: E402
from api import config as cfg  # noqa: E402
from api.session_ops import _live_active_stream_id  # noqa: E402


def _clear(stream_id: str) -> None:
    cfg.unregister_active_run(stream_id)
    with cfg.STREAMS_LOCK:
        cfg.STREAMS.pop(stream_id, None)


def test_running_run_is_still_attachable_and_busy():
    """Control: an ordinary running turn keeps both semantics unchanged."""
    sid = "sess-running-control"
    stream_id = "stream-running-control"
    cfg.register_active_run(stream_id, session_id=sid, phase="running")
    try:
        assert bp.active_stream_id_for_session(sid) == stream_id
        assert bp._session_has_active_turn(sid) is True
        assert _live_active_stream_id(
            SimpleNamespace(session_id=sid, active_stream_id=stream_id)
        ) == stream_id
    finally:
        _clear(stream_id)


def test_fresh_cancelling_run_is_not_attachable_but_stays_busy():
    """The loop-producing case: recovery must not replay a cancelled run.

    The same row must still count as busy so a successor cannot start while the
    cancelled worker is unwinding.
    """
    sid = "sess-cancelling-fresh"
    stream_id = "stream-cancelling-fresh"
    cfg.register_active_run(
        stream_id,
        session_id=sid,
        phase="cancelling",
        cancelled_at=time.time() - 5.0,
    )
    try:
        assert bp.active_stream_id_for_session(sid) is None
        assert bp._session_has_active_turn(sid) is True
        assert stream_id in cfg.ACTIVE_RUNS
    finally:
        _clear(stream_id)


def test_hidden_tab_status_does_not_resurrect_a_cancelling_run():
    """/api/session/status must not hand a cancelling stream to the poller."""
    sid = "sess-cancelling-status"
    stream_id = "stream-cancelling-status"
    cfg.register_active_run(stream_id, session_id=sid, phase="cancelling")
    try:
        assert _live_active_stream_id(
            SimpleNamespace(session_id=sid, active_stream_id=stream_id)
        ) is None
    finally:
        _clear(stream_id)


def test_hidden_tab_status_ignores_cancelling_run_with_live_stream_channel():
    """A cancelling row is terminal for the client even if STREAMS still holds it."""
    sid = "sess-cancelling-status-live-channel"
    stream_id = "stream-cancelling-status-live-channel"
    cfg.register_active_run(stream_id, session_id=sid, phase="cancelling")
    with cfg.STREAMS_LOCK:
        cfg.STREAMS[stream_id] = object()
    try:
        assert _live_active_stream_id(
            SimpleNamespace(session_id=sid, active_stream_id=stream_id)
        ) is None
    finally:
        _clear(stream_id)


def test_stale_cancelling_orphan_stops_blocking_and_is_reclaimed():
    """Past the unwind window with no live channel, the row is an orphan."""
    sid = "sess-cancelling-stale"
    stream_id = "stream-cancelling-stale"
    cfg.register_active_run(
        stream_id,
        session_id=sid,
        phase="cancelling",
        cancelled_at=time.time() - 240.0,
    )
    cfg.register_stream_owner(stream_id, sid)
    try:
        assert bp._session_has_active_turn(sid) is False
        assert stream_id not in cfg.ACTIVE_RUNS
        assert cfg.stream_owner_session_id(stream_id) is None
    finally:
        _clear(stream_id)


def test_stale_cancelling_run_with_live_channel_is_not_reclaimed():
    """Reverse control: age alone must not reap a row that still owns a channel."""
    sid = "sess-cancelling-stale-live-channel"
    stream_id = "stream-cancelling-stale-live-channel"
    cfg.register_active_run(
        stream_id,
        session_id=sid,
        phase="cancelling",
        cancelled_at=time.time() - 240.0,
    )
    with cfg.STREAMS_LOCK:
        cfg.STREAMS[stream_id] = object()
    try:
        assert bp._session_has_active_turn(sid) is True
        assert stream_id in cfg.ACTIVE_RUNS
    finally:
        _clear(stream_id)


def test_cancel_staleness_anchors_on_cancel_time_not_run_start():
    """A long-running turn cancelled just now must not read as stale."""
    entry = {
        "session_id": "sess-anchor",
        "phase": "cancelling",
        "started_at": time.time() - 4000.0,
        "cancelled_at": time.time() - 5.0,
    }
    assert cfg.active_run_cancel_is_stale(entry, grace_seconds=180.0) is False

    legacy = {
        "session_id": "sess-anchor-legacy",
        "phase": "cancelling",
        "started_at": time.time() - 4000.0,
    }
    assert cfg.active_run_cancel_is_stale(legacy, grace_seconds=180.0) is True

    running = {
        "session_id": "sess-anchor-running",
        "phase": "running",
        "started_at": time.time() - 4000.0,
    }
    assert cfg.active_run_cancel_is_stale(running, grace_seconds=180.0) is False


def test_attachability_predicate_edges():
    """Non-dict/opaque entries stay attachable; only cancelling is excluded."""
    assert cfg.active_run_is_attachable({"phase": "running"}) is True
    assert cfg.active_run_is_attachable({"phase": "cancelling"}) is False
    assert cfg.active_run_is_attachable({"phase": " cancelling "}) is False
    assert cfg.active_run_is_attachable({}) is True
    assert cfg.active_run_is_attachable(object()) is True
