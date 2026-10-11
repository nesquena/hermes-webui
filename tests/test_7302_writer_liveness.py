"""A subscriber that stops proving itself to the writer is dead, even when idle.

Re-gate regression for PR #7302 (greptile, finding 1 -- "sparse ghosts never
stall"):

The dead-subscriber signal required a subscriber's queue to reject broadcasts
(``queue.Full``) continuously for the whole stall window. An idle session never
fills a 64-slot queue, so a half-open socket -- the client vanished without a FIN
ever reaching us -- was never recorded as stalled and its channel zombied for the
life of the process. That is precisely the case this PR sets out to fix.

The fix adds writer-side liveness: the SSE writer marks the subscriber after every
COMPLETED write (event frame or keepalive), and a subscriber with no completed
write for ``max(STALL_SECS, 3 x keepalive)`` counts as dead. A healthy idle tab
proves itself once per keepalive interval, so it can never be collected by this
signal -- which is what the negative control below pins down.

Grep for the assertions, not for source strings: nothing here reads the source text.
"""
import time

import pytest

from api import background_process as bp
from api import config as cfg

SESSION_ID = "writer-liveness-session"

_REQUIRES_WRITER_LIVENESS = pytest.mark.skipif(
    not hasattr(bp.SessionChannel, "note_subscriber_write_ok"),
    reason="writer-liveness mark not present (pre-fix RED)",
)


def _drive_reaper_until(predicate, timeout: float = 3.0, interval: float = 0.02):
    """Run the REAL ``_reaper_loop`` in a thread until ``predicate()`` holds."""
    original = bp._REAPER_INTERVAL_SECS
    bp._REAPER_INTERVAL_SECS = interval
    started = bp.start_session_channel_reaper()
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()
    finally:
        bp.stop_session_channel_reaper()
        bp._REAPER_INTERVAL_SECS = original
        del started  # only recorded for symmetry with the start/stop pairing


def _writer_stale_after() -> float:
    """The staleness window the reaper applies to the writer mark."""
    window = float(getattr(cfg, "SESSION_CHANNEL_SUBSCRIBER_STALL_SECS", 300))
    keepalive = float(getattr(bp, "SESSION_CHANNEL_KEEPALIVE_SECS", 5.0))
    return max(window, 3.0 * keepalive)


def _expire_writer_mark(ch, q, *, seconds_ago: float) -> bool:
    """Rewind this subscriber's writer mark; False on a pre-fix head.

    Time is injected, never waited on: the test decides the age of the mark and
    then decides when the reaper evaluates it.
    """
    marks = getattr(ch, "_last_write_ok_at", None)
    if marks is None:
        return False
    marks[q] = time.time() - seconds_ago
    return True


def _ghost_subscriber():
    """The reported shape: a half-open socket whose queue is never saturated."""
    bp.SESSION_CHANNELS.pop(SESSION_ID, None)
    ch = bp.get_or_create_session_channel(SESSION_ID)
    q = ch.subscribe(maxsize=64)  # 64 slots: an idle session never fills it
    return ch, q


def test_a_silently_dead_subscriber_is_collected():
    """No completed write for the whole window means dead, queue pressure or not.

    The subscriber's queue is empty and never rejected a broadcast, so the
    queue-Full evidence can never see it; only the writer mark can.
    """
    ch, q = _ghost_subscriber()
    _expire_writer_mark(ch, q, seconds_ago=_writer_stale_after() + 1)

    collected = _drive_reaper_until(
        lambda: bp.get_session_channel(SESSION_ID) is None
    )

    assert collected, (
        "an idle subscriber whose socket stopped completing writes was never "
        "collected: a half-open tab zombies its channel for the life of the process"
    )
    assert ch.closed is True, "the collected channel never latched its close"


def test_a_subscriber_that_kept_proving_itself_is_never_collected():
    """Negative control: a live-but-quiet subscriber must survive the reaper.

    Its mark is refreshed right before the evaluation, exactly as a keepalive
    landing during the window would refresh it. Closing it here would be the
    false positive that costs a live tab its channel.
    """
    ch, q = _ghost_subscriber()
    _expire_writer_mark(ch, q, seconds_ago=0.0)  # proved alive just now

    collected = _drive_reaper_until(
        lambda: ch.closed is True, timeout=0.6, interval=0.02
    )

    assert collected is False, (
        "a subscriber that proved itself to the writer was collected anyway: a "
        "healthy but quiet tab loses its channel"
    )
    try:
        assert bp.get_session_channel(SESSION_ID) is ch
        assert ch.closed is False
    finally:
        # Greptile on #8108: don't leave a live subscribed channel in the shared
        # registry for later tests.
        ch.unsubscribe(q)
        ch.close("test cleanup")
        with bp.SESSION_CHANNELS_LOCK:
            bp.SESSION_CHANNELS.pop(SESSION_ID, None)


@_REQUIRES_WRITER_LIVENESS
def test_the_write_mark_tracks_only_attached_subscribers():
    """``note_subscriber_write_ok`` refreshes an attached queue and never a dead one."""
    ch = bp.SessionChannel(SESSION_ID)
    q = ch.subscribe()
    stale = _writer_stale_after() + 1
    _expire_writer_mark(ch, q, seconds_ago=stale)

    ch.note_subscriber_write_ok(q)

    assert (time.time() - ch._last_write_ok_at[q]) < 1.0, (
        "a completed write did not refresh the subscriber's liveness mark"
    )

    ch.unsubscribe(q)
    ch.note_subscriber_write_ok(q)

    assert q not in ch._last_write_ok_at, (
        "a detached queue kept a writer-liveness mark: the map would grow one "
        "entry per subscriber that ever attached"
    )


def test_the_mirrored_keepalive_constant_stays_inside_the_staleness_window():
    """A mirrored constant cannot check itself — this pins the coupling.

    ``SESSION_CHANNEL_KEEPALIVE_SECS`` (api/background_process.py) mirrors the SSE
    handler's real heartbeat interval (``_SSE_HEARTBEAT_INTERVAL_SECONDS`` in
    api/routes.py) as a plain number, because ``routes`` imports this module and
    importing back would be circular. If the real interval ever reached the
    writer-staleness window, a quiet HEALTHY tab would stop proving itself in time
    and the reaper would close a live connection — the failure the writer-liveness
    signal exists to avoid, inverted.
    """
    routes = pytest.importorskip("api.routes")
    real = float(routes._SSE_HEARTBEAT_INTERVAL_SECONDS)
    mirrored = float(bp.SESSION_CHANNEL_KEEPALIVE_SECS)
    stale_after = _writer_stale_after()

    assert real < stale_after, (
        f"the real keepalive interval ({real}s) is at or past the writer-staleness "
        f"window ({stale_after}s): a healthy idle subscriber would be collected"
    )
    assert mirrored == real, (
        f"SESSION_CHANNEL_KEEPALIVE_SECS ({mirrored}s) no longer matches the SSE "
        f"handler's interval ({real}s): update the mirror with the handler"
    )
