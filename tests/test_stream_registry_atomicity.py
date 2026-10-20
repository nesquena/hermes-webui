"""The re-gate's two scheduler races must each be ONE atomic transition.

Re-gate regressions for PR #7302 (review 2026-09-23, items 2 and 3). Both were races
between a DECISION and the ACTION that followed it in a different lock acquisition:

  * Item 2 (gateway claim). ``_run_gateway_chat_streaming`` resolved its stream with
    ``peek_stream()``, released ``STREAMS_LOCK``, and only then published itself in
    ``ACTIVE_RUNS``. The orphan cleanup this PR adds to ``_active_stream_blocks_chat_start``
    runs in exactly that window, so it could clear the starting stream's entry (and owner)
    while the worker went on to publish itself active -- running a turn with no
    transport/owner state, which allows overlapping turns and duplicate provider/tool
    effects.

  * Item 3 (reaper). ``_reaper_loop`` asked ``reaper_should_collect()`` and closed the
    channel in a later acquisition. A stalled subscriber can drain and accept a completion
    in that gap, which clears its stall evidence, yet the stale decision still closed the
    channel and evicted the just-delivered completion to make room for the sentinel.

The fix has the same shape in both places: revalidate and claim under the same lock edge
the rest of the lifecycle uses (``STREAMS_LOCK -> ACTIVE_RUNS_LOCK``, and
``SESSION_CHANNELS_LOCK -> SessionChannel._lock``). The tests below pin that invariant
directly -- a non-blocking probe of the lock the transition must already hold, and an
Event-barrier that holds the critical section open -- plus the observable consequence each
race had: a worker publishing itself without ownership, and a subscriber drain reported as
complete inside the transition.

Grep for the assertions, not for source strings: nothing here reads the source text.
"""
import queue
import threading
import time

import pytest

import api.background_process as bp
import api.config as config
import api.gateway_chat as gateway_chat

SESSION_ID = "atomicity-session"


def _saturate_and_stall(ch, maxsize: int = 2, events: int = 8):
    """Subscribe, fill the queue, and record a stall run (positive death evidence)."""
    q = ch.subscribe(maxsize=maxsize)
    for i in range(events):
        ch.emit("bg_task_output", {"i": i})
    return q


def _stalled_now() -> float:
    """A ``now`` past the stall window, so stall evidence counts as death."""
    return time.time() + float(config.SESSION_CHANNEL_SUBSCRIBER_STALL_SECS) + 1.0


# ── item 2: the gateway claim ────────────────────────────────────────────────


class _ProbeStop(Exception):
    """Unwinds the worker right after the publication it is being probed for."""


def test_gateway_worker_publishes_its_active_run_inside_the_streams_lock_edge(monkeypatch):
    """The ACTIVE_RUNS publication must happen while STREAMS_LOCK is held."""
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    stream_id = "atomic-claim-stream"
    config.STREAMS[stream_id] = queue.Queue()

    observed = {}

    def probing_register_active_run(sid, **metadata):
        # A non-blocking acquire answers "did the caller already hold it?" with no timing.
        acquired = config.STREAMS_LOCK.acquire(blocking=False)
        observed["inside_streams_lock"] = not acquired
        observed["stream_id"] = sid
        if acquired:
            config.STREAMS_LOCK.release()
        raise _ProbeStop

    monkeypatch.setattr(gateway_chat, "register_active_run", probing_register_active_run)

    with pytest.raises(_ProbeStop):
        gateway_chat._run_gateway_chat_streaming(
            SESSION_ID, "hello", "test-model", "test-workspace", stream_id
        )

    assert observed.get("stream_id") == stream_id, (
        "the worker never reached the publication point, so the probe observed nothing"
    )
    assert observed["inside_streams_lock"] is True, (
        "the worker published its active run AFTER releasing STREAMS_LOCK: a concurrent "
        "chat/start can clear the stream as an orphan in that gap and the worker keeps "
        "running with no transport/owner state"
    )


def test_gateway_worker_fails_closed_when_the_stream_is_cleared_before_the_claim(monkeypatch):
    """Losing the race must leave no active run and no leaked owner entry."""
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    config.STREAM_SESSION_OWNERS.clear()
    stream_id = "raced-claim-stream"
    config.STREAMS[stream_id] = queue.Queue()
    config.STREAM_SESSION_OWNERS[stream_id] = SESSION_ID  # the route layer registered it

    real_peek = gateway_chat.peek_stream
    real_register = gateway_chat.register_active_run
    published = []

    def peek_then_lose_the_race(sid):
        q = real_peek(sid)  # the snapshot this worker saw
        config.STREAMS.pop(sid, None)  # a concurrent chat/start clears it as an orphan
        return q

    def recording_register(sid, **metadata):
        published.append(sid)
        return real_register(sid, **metadata)

    monkeypatch.setattr(gateway_chat, "peek_stream", peek_then_lose_the_race)
    monkeypatch.setattr(gateway_chat, "register_active_run", recording_register)

    gateway_chat._run_gateway_chat_streaming(
        SESSION_ID, "hello", "test-model", "test-workspace", stream_id
    )

    # The publication itself is the assertion: it must not happen once the stream
    # is gone. Asserting only on the final ACTIVE_RUNS state would pass on the
    # pre-fix tree too, because the worker's own teardown finally unregisters it
    # again by the time this returns.
    assert published == [], (
        "the worker published itself active after losing ownership of the stream: the "
        "turn runs with no transport/owner state (overlapping turns, duplicate effects)"
    )
    assert stream_id not in config.ACTIVE_RUNS, (
        "the worker published itself active after losing ownership of the stream"
    )
    assert stream_id not in config.STREAM_SESSION_OWNERS, (
        "the pre-start exit path leaked the stream owner entry"
    )


# ── item 3: the reaper transition ────────────────────────────────────────────


def test_reaper_revalidates_stall_evidence_under_the_channel_lock(monkeypatch):
    """Revalidation must run under the same ``_lock`` a draining emit uses."""
    ch = bp.SessionChannel("revalidation-under-lock")
    _saturate_and_stall(ch)

    observed = {}
    locked_variant = getattr(ch, "_dead_subscriber_signal_locked", None)
    public_variant = ch._dead_subscriber_signal

    def _record_under_lock():
        acquired = ch._lock.acquire(blocking=False)
        observed["under_lock"] = not acquired
        if acquired:
            ch._lock.release()

    def probe_locked(now):
        _record_under_lock()
        return locked_variant(now) if locked_variant is not None else True

    def probe_public(now):
        # Present on both trees; the pre-fix path calls this one with the lock RELEASED.
        observed.setdefault("under_lock", False)
        _record_under_lock()
        return public_variant(now)

    if locked_variant is not None:
        monkeypatch.setattr(ch, "_dead_subscriber_signal_locked", probe_locked)
    monkeypatch.setattr(ch, "_dead_subscriber_signal", probe_public)

    assert ch.reaper_should_collect(_stalled_now()) is True
    assert observed.get("under_lock") is True, (
        "the stall evidence was revalidated OUTSIDE ch._lock: a subscriber drain that "
        "clears it can interleave between the reaper's decision and its close"
    )


def test_a_drain_cannot_slip_between_the_reaper_decision_and_the_close(monkeypatch):
    """Barrier: the transition is held open, and the draining emit cannot get in."""
    ch = bp.SessionChannel("barrier-transition")
    q = _saturate_and_stall(ch)
    now = _stalled_now()

    real_decision = ch._reaper_should_collect_locked
    inside = threading.Event()
    release = threading.Event()

    def paused_decision(probe_now):
        verdict = real_decision(probe_now)  # the decision, taken under ch._lock
        if verdict:
            inside.set()
            release.wait(5.0)  # hold the critical section open for the drain
        return verdict

    monkeypatch.setattr(ch, "_reaper_should_collect_locked", paused_decision)

    result = {}

    def collect():
        result["collected"] = ch.collect_if_eligible(now, "reaper")

    collector = threading.Thread(target=collect, daemon=True)
    collector.start()
    assert inside.wait(5.0), "the reaper never entered its atomic transition"

    delivered = {}

    def drain():
        try:
            q.get_nowait()  # the stalled subscriber drains...
            delivered["delivered"] = ch.emit("process_complete", {"ok": True})
        finally:
            delivered["returned"] = True

    drainer = threading.Thread(target=drain, daemon=True)
    drainer.start()
    drainer.join(0.5)
    assert delivered.get("returned") is not True, (
        "the subscriber's drain ran inside the reaper's transition: the completion it "
        "delivers can be evicted by the close sentinel"
    )

    release.set()
    collector.join(5.0)
    drainer.join(5.0)

    assert result.get("collected") is True
    assert delivered.get("returned") is True, (
        "the drain never completed once the transition was released"
    )


def test_atomic_collect_refuses_a_channel_whose_stall_evidence_cleared():
    """A drain that landed first keeps the channel alive, and nothing is evicted."""
    ch = bp.SessionChannel("cleared-evidence")
    q = _saturate_and_stall(ch)
    now = _stalled_now()

    q.get_nowait()  # the stalled subscriber drains...
    assert ch.emit("process_complete", {"ok": True}) == 1  # ...and accepts a completion

    assert ch.collect_if_eligible(now, "reaper") is False
    assert ch.closed is False

    remaining = []
    while True:
        try:
            remaining.append(q.get_nowait())
        except queue.Empty:
            break
    assert ("process_complete", {"ok": True}) in remaining, (
        "the completion the subscriber received must not be evicted by a sentinel"
    )


def test_atomic_collect_closes_the_channel_and_signals_the_subscriber():
    """The atomic path still performs the close semantics it replaced."""
    ch = bp.SessionChannel("atomic-collect")
    q = _saturate_and_stall(ch)

    assert ch.collect_if_eligible(_stalled_now(), "reaper") is True
    assert ch.closed is True

    drained = []
    while True:
        try:
            drained.append(q.get_nowait())
        except queue.Empty:
            break
    assert None in drained, "collect() must deliver the end-of-stream sentinel"

    # A closed channel stays collectible -- the same transition is what pops its
    # registry entry -- but a second claim must not signal the subscribers again.
    assert ch.collect_if_eligible(_stalled_now(), "reaper") is True
    assert q.qsize() == 0, "an already-collected channel must not signal twice"
