"""A reattached Gateway run must claim its ownership BEFORE the worker is scheduled.

Re-gate regression for PR #7302 (review 2026-10-01, finding 2 -- "CORE"):

``_resume_gateway_run_for_session()`` registered the restored run in ``STREAMS`` and
scheduled the worker, and only THEN did the worker publish its ``ACTIVE_RUNS`` row.
A run restored after a WebUI restart carries the PREVIOUS process's
``session.pending_started_at``, so the fresh-pending guard that covers the ordinary
registration gap in ``_active_stream_blocks_chat_start`` cannot cover this one: the
orphan check saw a registered stream with no live worker and an old pending
timestamp, classified the reattach as an orphan, cleared the stream plus the
Gateway state, and admitted a SECOND start while the remote run kept going.

The fix publishes the ``ACTIVE_RUNS`` row (and the retained cancel signal) on the
same ``STREAMS_LOCK -> ACTIVE_RUNS_LOCK`` edge that creates the ``STREAMS`` entry,
before the thread is scheduled, and releases the claim if the thread fails to
launch. These tests pin the invariant and its observable consequence.

Grep for the assertions, not for source strings: nothing here reads the source text.
"""
import queue
import threading
import time

import pytest

import api.config as config
import api.gateway_chat as gateway_chat
import api.routes as routes

SESSION_ID = "reattach-session"
STREAM_ID = "reattach-stream"
RUN_ID = "gateway-run-1"


class _ProbeStop(Exception):
    """Unwinds the resume path right after the publication it is being probed for."""


class _FakeSession:
    """The attributes ``_resume_gateway_run_for_session`` reads."""

    def __init__(self, *, pending_started_at):
        self.session_id = SESSION_ID
        self.active_stream_id = STREAM_ID
        self.gateway_run = {
            "stream_id": STREAM_ID,
            "run_id": RUN_ID,
        }
        self.pending_user_message = "hello"
        self.model = "test-model"
        self.model_provider = "test-provider"
        self.workspace = "test-workspace"
        self.pending_attachments = []
        self.profile = None
        # A RESTARTED session: the timestamp belongs to the previous process.
        self.pending_started_at = pending_started_at


def _reset_registries():
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    config.CANCEL_FLAGS.clear()
    config.STREAM_SESSION_OWNERS.clear()


@pytest.fixture(autouse=True)
def _isolated_registries(monkeypatch):
    _reset_registries()
    config.LAST_RUN_FINISHED_AT = None
    # Endpoint resolution touches profiles/config; irrelevant to the invariant.
    monkeypatch.setattr(
        gateway_chat, "_gateway_endpoint_for_profile", lambda profile: ("http://gw", "k")
    )
    yield
    _reset_registries()


class _RecordingThread:
    """Stands in for ``threading.Thread``: records the launch, never runs the body."""

    launched: list = []

    def __init__(self, target=None, args=(), kwargs=None, **rest):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.__class__.launched.append(self)

    def start(self):
        pass


def test_reattach_publishes_its_active_run_inside_the_streams_lock_edge(monkeypatch):
    """The ACTIVE_RUNS publication must happen while STREAMS_LOCK is held."""
    session = _FakeSession(pending_started_at=time.time() - 3600)
    observed = {}

    def probing_register_active_run(sid, **metadata):
        # A non-blocking acquire answers "did the caller already hold it?" with no timing.
        acquired = config.STREAMS_LOCK.acquire(blocking=False)
        observed["inside_streams_lock"] = not acquired
        observed["stream_id"] = sid
        observed["phase"] = metadata.get("phase")
        if acquired:
            config.STREAMS_LOCK.release()
        raise _ProbeStop

    monkeypatch.setattr(gateway_chat, "register_active_run", probing_register_active_run)
    monkeypatch.setattr(gateway_chat.threading, "Thread", _RecordingThread)

    with pytest.raises(_ProbeStop):
        gateway_chat._resume_gateway_run_for_session(session)

    assert observed.get("stream_id") == STREAM_ID, (
        "the reattach never reached the publication point, so the probe observed nothing"
    )
    assert observed["inside_streams_lock"] is True, (
        "the reattach published its active run AFTER releasing STREAMS_LOCK: a concurrent "
        "chat/start can classify it as an orphan in that gap and admit a second start"
    )
    assert observed["phase"] == "gateway-reattached"


def test_restarted_run_with_a_stale_pending_timestamp_is_not_orphaned(monkeypatch):
    """The observable consequence: the reattach survives the orphan check.

    The session's pending timestamp is an hour old (it belongs to the process that
    died), which alone is enough for ``_active_stream_blocks_chat_start`` to treat
    the stream as an orphan. The pre-scheduled ACTIVE_RUNS row is what keeps it live.
    """
    _RecordingThread.launched = []
    monkeypatch.setattr(gateway_chat.threading, "Thread", _RecordingThread)
    session = _FakeSession(pending_started_at=time.time() - 3600)

    assert gateway_chat._resume_gateway_run_for_session(session) is True

    assert STREAM_ID in config.STREAMS, "the reattach did not register its stream"
    assert STREAM_ID in config.ACTIVE_RUNS, (
        "the reattach did not claim ACTIVE_RUNS before scheduling the worker, so the "
        "orphan check has no liveness evidence for a restarted run"
    )
    assert len(_RecordingThread.launched) == 1, "the reattach worker was never scheduled"

    assert routes._active_stream_blocks_chat_start(session, STREAM_ID) is True, (
        "a reattached run whose pending timestamp predates the restart was classified as "
        "an orphan: chat/start would clear it and admit a second start"
    )
    assert STREAM_ID in config.STREAMS, "the orphan check cleared a live reattach"


def test_reattach_releases_its_claim_when_the_thread_cannot_be_started(monkeypatch):
    """A real ``Thread.start()`` failure must leave no ownership behind."""
    monkeypatch.setattr(gateway_chat, "_gateway_endpoint_for_profile", lambda profile: ("http://gw", "k"))
    session = _FakeSession(pending_started_at=time.time() - 3600)

    class _ExplodingStartThread:
        """Constructs fine (like the real thread object) and fails on start()."""

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(gateway_chat.threading, "Thread", _ExplodingStartThread)

    assert gateway_chat._resume_gateway_run_for_session(session) is False

    assert STREAM_ID not in config.STREAMS, "a failed reattach left the stream registered"
    assert STREAM_ID not in config.ACTIVE_RUNS, (
        "a failed reattach left an ACTIVE_RUNS claim: the session would look busy forever"
    )
    assert config.stream_owner_session_id(STREAM_ID) is None, (
        "a failed reattach left the stream owner entry behind"
    )
    assert STREAM_ID not in config.CANCEL_FLAGS, "a failed reattach left its cancel flag behind"


def _detach_the_stream_like_stop() -> None:
    """Reproduce ``cancel_stream()``: mark the row cancelling, pop the transport."""
    config.update_active_run(STREAM_ID, phase="cancelling", cancelled_at=time.time())
    with config.STREAMS_LOCK:
        config.STREAMS.pop(STREAM_ID, None)
        config.CANCEL_FLAGS.pop(STREAM_ID, None)


def test_stop_before_worker_admission_retires_the_prepublished_claim(monkeypatch):
    """reattach-scheduled -> Stop -> worker admitted: no ghost row, no false busy.

    ``cancel_stream()`` leaves the ACTIVE_RUNS row in ``phase="cancelling"`` for the
    worker's ``finally`` to retire, but a worker that is cancelled before it admits
    the stream takes the q-is-None early return, which never reaches that ``finally``.
    The claim published before the worker was scheduled must be retired there, or the
    row reports false liveness (``_run_lifecycle_health``) and delays wakeups because
    ``LAST_RUN_FINISHED_AT`` never advances.
    """
    _RecordingThread.launched = []
    monkeypatch.setattr(gateway_chat.threading, "Thread", _RecordingThread)
    session = _FakeSession(pending_started_at=time.time() - 3600)

    assert gateway_chat._resume_gateway_run_for_session(session) is True
    assert STREAM_ID in config.ACTIVE_RUNS, "the reattach did not publish its claim"
    worker = _RecordingThread.launched[-1]

    _detach_the_stream_like_stop()
    gateway_chat._run_gateway_chat_streaming(*worker.args, **worker.kwargs)

    assert STREAM_ID not in config.STREAMS
    assert STREAM_ID not in config.ACTIVE_RUNS, (
        "the pre-admission claim survived a Stop: a cancelled ghost row reports false "
        "liveness and keeps the session looking busy"
    )
    assert config.stream_owner_session_id(STREAM_ID) is None
    assert STREAM_ID not in gateway_chat._STREAM_RUN_LIFECYCLE
    assert STREAM_ID not in gateway_chat._STREAM_RUN_IDS
    assert STREAM_ID not in gateway_chat._STREAM_ENDPOINTS

    health = routes._run_lifecycle_health()
    assert health["active_runs"] == 0, "a ghost active run is still reported as busy"
    assert health["last_run_finished_at"] is not None, (
        "the wakeup clock never advanced, so background wakeups stay delayed"
    )
    assert routes._active_stream_blocks_chat_start(session, STREAM_ID) is False, (
        "the retired stream still blocks chat/start"
    )


def test_stop_teardown_never_deletes_a_successors_active_run_row(monkeypatch):
    """The retirement is by identity: a successor's row for the same id survives."""
    _RecordingThread.launched = []
    monkeypatch.setattr(gateway_chat.threading, "Thread", _RecordingThread)
    session = _FakeSession(pending_started_at=time.time() - 3600)

    assert gateway_chat._resume_gateway_run_for_session(session) is True
    worker = _RecordingThread.launched[-1]

    # A successor claims the same stream id before our worker unwinds.
    config.ACTIVE_RUNS[STREAM_ID] = {
        "stream_id": STREAM_ID,
        "phase": "gateway-reattached",
        "claim_token": "successor-token",
    }
    with config.STREAMS_LOCK:
        config.STREAMS.pop(STREAM_ID, None)

    gateway_chat._run_gateway_chat_streaming(*worker.args, **worker.kwargs)

    survivor = config.ACTIVE_RUNS.get(STREAM_ID)
    assert survivor is not None, (
        "the early-return teardown deleted a successor's active-run row"
    )
    assert survivor.get("claim_token") == "successor-token", (
        "the early-return teardown deleted a successor's active-run row"
    )


def test_second_reattach_of_the_same_stream_is_refused(monkeypatch):
    """The registration guard still refuses a stream that is already registered."""
    monkeypatch.setattr(gateway_chat.threading, "Thread", _RecordingThread)
    session = _FakeSession(pending_started_at=time.time() - 3600)
    config.STREAMS[STREAM_ID] = queue.Queue()

    assert gateway_chat._resume_gateway_run_for_session(session) is False
    assert STREAM_ID not in config.ACTIVE_RUNS, (
        "the refused reattach published an active run it does not own"
    )
