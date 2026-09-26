"""Regression for #7302: an orphaned STREAMS entry must not refuse chat/start forever.

STREAMS is only cleared by the worker's own finalization, so a hard-killed or
wedged worker leaves its entry behind and every later chat/start for that session
was refused with ``409 session already has an active stream`` — for hours, since
the SSE channel reaper never touches that registry.

Membership in STREAMS is therefore NOT evidence of a live turn. The authoritative
liveness registry is ACTIVE_RUNS (keyed by stream_id, unregistered in the worker's
outer ``finally``), and the fresh-pending guard covers the window between stream
registration and worker registration. A registered stream with no live worker and
no pending turn inside that window is an orphan: it is cleared from both
registries so the session can admit a fresh turn.

The matrix below pins every arm of that contract, plus the lock order the orphan
clear must keep (STREAMS -> ACTIVE_RUNS -> STREAM_SESSION_OWNERS) so a future
change cannot introduce an inversion.

Grep for the assertions, not for source strings: every test drives the real
decision function (and, for the two recovery tests, the real chat/start entry
point) against the real registries.
"""
import queue
import time

import pytest

import api.config as config
import api.routes as routes
from api.models import _REPAIR_STALE_PENDING_GRACE_SECONDS


class _Session:
    """Minimal session stand-in for the chat/start decision path."""

    session_id = "orphan-recovery-session"

    def __init__(
        self,
        *,
        active_stream_id=None,
        pending_user_message=None,
        pending_started_at=None,
        session_id=None,
    ):
        if session_id is not None:
            self.session_id = session_id
        self.active_stream_id = active_stream_id
        self.pending_user_message = pending_user_message
        self.pending_attachments = []
        self.pending_started_at = pending_started_at
        self.messages = []
        self.title = "Orphan Recovery"
        self.worktree_path = None
        self.workspace = None
        self.model = None
        self.model_provider = None

    def save(self, *args, **kwargs):
        return None


def _reset_registries():
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    config.STREAM_SESSION_OWNERS.clear()
    config.SESSION_AGENT_LOCKS.clear()


# ── the blocking arms ────────────────────────────────────────────────────────


def test_live_worker_keeps_the_stream_registered_and_blocks():
    """A registered stream WITH a live worker is a real turn: block, touch nothing."""
    _reset_registries()
    stream_id = "live-worker-stream"
    session = _Session(
        active_stream_id=stream_id,
        # An OLD pending turn must not downgrade a live worker to an orphan.
        pending_user_message="long running prompt",
        pending_started_at=time.time() - (_REPAIR_STALE_PENDING_GRACE_SECONDS + 600),
        session_id="live-worker-session",
    )
    config.STREAMS[stream_id] = queue.Queue()
    config.register_active_run(stream_id, session_id=session.session_id, phase="running")
    try:
        assert routes._active_stream_blocks_chat_start(session, stream_id) is True
        # Neither registry was touched: liveness is authoritative.
        assert stream_id in config.STREAMS
        assert stream_id in config.ACTIVE_RUNS
    finally:
        config.unregister_active_run(stream_id)


def test_fresh_pending_turn_blocks_in_the_registration_gap():
    """Between stream registration and worker registration neither registry proves a
    live turn, so a just-published pending turn must keep blocking duplicate starts."""
    _reset_registries()
    stream_id = "registering-stream"
    session = _Session(
        active_stream_id=stream_id,
        pending_user_message="just claimed",
        pending_started_at=time.time(),
        session_id="registration-gap-session",
    )
    config.STREAMS[stream_id] = queue.Queue()

    assert routes._active_stream_blocks_chat_start(session, stream_id) is True
    # The gap guard must NOT reap the stream it just admitted.
    assert stream_id in config.STREAMS


def test_missing_stream_id_never_blocks():
    """The caller only reaches here for a published stream id; an absent one is not a turn."""
    _reset_registries()
    session = _Session(
        pending_user_message="fresh but unowned",
        pending_started_at=time.time(),
        session_id="no-stream-session",
    )

    assert routes._active_stream_blocks_chat_start(session, None) is False


# ── the orphan arm ───────────────────────────────────────────────────────────


def test_orphan_stream_is_cleared_from_both_registries():
    """No worker, no pending turn: the entry is an orphan and must be dropped from
    STREAMS *and* STREAM_SESSION_OWNERS, otherwise the session 409s forever."""
    _reset_registries()
    stream_id = "orphaned-stream"
    session = _Session(active_stream_id=stream_id, session_id="orphan-session")
    config.STREAMS[stream_id] = queue.Queue()
    config.register_stream_owner(stream_id, session.session_id)

    assert routes._active_stream_blocks_chat_start(session, stream_id) is False
    assert stream_id not in config.STREAMS
    assert config.stream_owner_session_id(stream_id) is None


def test_stale_pending_turn_is_not_evidence_of_a_live_worker():
    """Past the grace window a pending turn is what a crashed turn leaves behind."""
    _reset_registries()
    stream_id = "crashed-turn-stream"
    session = _Session(
        active_stream_id=stream_id,
        pending_user_message="crashed prompt",
        pending_started_at=time.time() - (_REPAIR_STALE_PENDING_GRACE_SECONDS + 5),
        session_id="crashed-turn-session",
    )
    config.STREAMS[stream_id] = queue.Queue()
    config.register_stream_owner(stream_id, session.session_id)

    assert routes._active_stream_blocks_chat_start(session, stream_id) is False
    assert stream_id not in config.STREAMS
    assert config.stream_owner_session_id(stream_id) is None


def test_pending_turn_without_a_timestamp_does_not_block():
    """A pending turn with no timestamp cannot prove freshness, so it cannot block."""
    _reset_registries()
    stream_id = "untimestamped-stream"
    session = _Session(
        active_stream_id=stream_id,
        pending_user_message="no timestamp",
        pending_started_at=None,
        session_id="untimestamped-session",
    )
    config.STREAMS[stream_id] = queue.Queue()

    assert routes._active_stream_blocks_chat_start(session, stream_id) is False
    assert stream_id not in config.STREAMS


def test_unreadable_liveness_registry_fails_closed():
    """Unknown liveness is not an orphan: an unreadable ACTIVE_RUNS must not reap."""

    class _ExplodingLock:
        def __enter__(self):
            raise RuntimeError("liveness registry unreadable")

        def __exit__(self, exc_type, exc, tb):
            return False

    _reset_registries()
    stream_id = "unknown-liveness-stream"
    session = _Session(active_stream_id=stream_id, session_id="unknown-liveness-session")
    config.STREAMS[stream_id] = queue.Queue()
    routes_active_runs_lock = routes.ACTIVE_RUNS_LOCK
    routes.ACTIVE_RUNS_LOCK = _ExplodingLock()
    try:
        assert routes._active_stream_blocks_chat_start(session, stream_id) is True
        assert stream_id in config.STREAMS
    finally:
        routes.ACTIVE_RUNS_LOCK = routes_active_runs_lock


def test_orphan_clear_is_scoped_to_the_exact_stream_id():
    """A start that registered a NEW stream concurrently must survive the reap of the
    old id: the clear is scoped to the stream id under decision, never to the session."""
    _reset_registries()
    orphan_stream, concurrent_stream = "orphan-stream", "concurrent-stream"
    config.STREAMS[orphan_stream] = queue.Queue()
    config.STREAMS[concurrent_stream] = queue.Queue()
    config.register_stream_owner(orphan_stream, "scope-session")
    config.register_stream_owner(concurrent_stream, "scope-session")
    # A concurrent start already republished the session's active stream.
    session = _Session(active_stream_id=concurrent_stream, session_id="scope-session")

    assert routes._active_stream_blocks_chat_start(session, orphan_stream) is False
    assert orphan_stream not in config.STREAMS
    assert config.stream_owner_session_id(orphan_stream) is None
    # The concurrent registration is untouched.
    assert concurrent_stream in config.STREAMS
    assert config.stream_owner_session_id(concurrent_stream) == "scope-session"


# ── lock order ───────────────────────────────────────────────────────────────


class _LockOrderProbe:
    """Records acquire/release order and flags a nested acquisition that is not the
    canonical STREAMS -> ACTIVE_RUNS -> OWNERS progression (i.e. an inversion)."""

    def __init__(self, order):
        self.order = list(order)
        self.events = []
        self.held = []

    def wrap(self, name):
        probe = self

        class _ProbeLock:
            def __enter__(self):
                if probe.held:
                    previous = probe.held[-1]
                    if probe.order.index(name) <= probe.order.index(previous):
                        probe.events.append(("violation", (previous, name)))
                probe.events.append(("acquire", name))
                probe.held.append(name)
                return self

            def __exit__(self, exc_type, exc, tb):
                if name in probe.held:
                    probe.held.remove(name)
                probe.events.append(("release", name))
                return False

        return _ProbeLock()


def test_orphan_clear_keeps_the_lifecycle_lock_order(monkeypatch):
    """Pin the order the orphan clear must keep, so no future edit inverts it into a
    deadlock. The clear runs with STREAMS_LOCK already held, then reads ACTIVE_RUNS,
    then drops the owner — never the reverse."""
    _reset_registries()
    probe = _LockOrderProbe(
        ["STREAMS_LOCK", "ACTIVE_RUNS_LOCK", "STREAM_SESSION_OWNERS_LOCK"]
    )
    monkeypatch.setattr(routes, "STREAMS_LOCK", probe.wrap("STREAMS_LOCK"))
    monkeypatch.setattr(routes, "ACTIVE_RUNS_LOCK", probe.wrap("ACTIVE_RUNS_LOCK"))
    # unregister_stream_owner resolves the owner lock from api.config at call time.
    monkeypatch.setattr(
        config, "STREAM_SESSION_OWNERS_LOCK", probe.wrap("STREAM_SESSION_OWNERS_LOCK")
    )
    stream_id = "lock-order-stream"
    config.STREAMS[stream_id] = queue.Queue()
    config.STREAM_SESSION_OWNERS[stream_id] = "lock-order-session"
    session = _Session(active_stream_id=stream_id, session_id="lock-order-session")

    assert routes._active_stream_blocks_chat_start(session, stream_id) is False

    assert [name for kind, name in probe.events if kind == "acquire"] == [
        "STREAMS_LOCK",
        "ACTIVE_RUNS_LOCK",
        "STREAM_SESSION_OWNERS_LOCK",
    ]
    assert [event for event in probe.events if event[0] == "violation"] == []
    assert probe.held == []


# ── end to end: the session actually admits a new turn ───────────────────────


class _NoopThread:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def start(self):
        return None


def _patch_stream_start(monkeypatch, stream_ids=("new-stream",)):
    ids = iter(stream_ids)
    monkeypatch.setattr(
        routes.uuid, "uuid4", lambda: type("FakeUuid", (), {"hex": next(ids)})()
    )
    monkeypatch.setattr(routes, "set_last_workspace", lambda workspace, **_kw: None)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: queue.Queue())
    monkeypatch.setattr(routes.threading, "Thread", _NoopThread)
    # Keep the test independent of whether a Hermes Agent checkout is importable on
    # this machine: when it is, the runtime barrier answers 409 for every start and
    # would mask the decision path under test.
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kw: None)


def test_chat_start_recovers_from_an_orphaned_stream(monkeypatch, tmp_path):
    """The whole point of the fix: a session whose worker died admits the next turn
    instead of 409-ing forever."""
    _reset_registries()
    orphan_stream = "orphaned-stream"
    session = _Session(
        active_stream_id=orphan_stream,
        pending_user_message="prompt from the crashed turn",
        pending_started_at=time.time() - (_REPAIR_STALE_PENDING_GRACE_SECONDS + 600),
        session_id="recovery-session",
    )
    config.STREAMS[orphan_stream] = queue.Queue()
    config.register_stream_owner(orphan_stream, session.session_id)
    _patch_stream_start(monkeypatch)

    try:
        response = routes._start_chat_stream_for_session(
            session,
            msg="please start once",
            attachments=[],
            workspace=str(tmp_path),
            model="test-model",
            model_provider=None,
        )

        assert "error" not in response
        assert response["stream_id"] == "new-stream"
        assert session.active_stream_id == "new-stream"
        # The orphan left both registries behind.
        assert orphan_stream not in config.STREAMS
        assert config.stream_owner_session_id(orphan_stream) is None
        # The admitted turn owns the session.
        assert "new-stream" in config.STREAMS
    finally:
        config.STREAMS.clear()
        config.STREAM_SESSION_OWNERS.clear()


def test_chat_start_admits_exactly_one_turn_while_the_first_registers(monkeypatch, tmp_path):
    """The safety half of the fix: clearing orphans must not admit a second turn for
    a session that already published one. The fresh-pending arm is what holds this."""
    _reset_registries()
    session = _Session(session_id="single-admit-session")
    _patch_stream_start(monkeypatch, stream_ids=("first-stream", "second-stream"))

    try:
        first = routes._start_chat_stream_for_session(
            session,
            msg="first prompt",
            attachments=[],
            workspace=str(tmp_path),
            model="test-model",
            model_provider=None,
        )
        assert "error" not in first
        assert first["stream_id"] == "first-stream"
        assert session.pending_user_message == "first prompt"

        second = routes._start_chat_stream_for_session(
            session,
            msg="duplicate prompt",
            attachments=[],
            workspace=str(tmp_path),
            model="test-model",
            model_provider=None,
        )

        assert second["_status"] == 409
        assert second["active_stream_id"] == "first-stream"
        assert "second-stream" not in config.STREAMS
        # The admitted turn still owns the session and its prompt was not overwritten.
        assert session.active_stream_id == "first-stream"
        assert session.pending_user_message == "first prompt"
    finally:
        config.STREAMS.clear()
        config.STREAM_SESSION_OWNERS.clear()


@pytest.mark.parametrize("phase", ["starting", "running"])
def test_orphan_stream_with_a_cancelling_worker_is_not_reaped(monkeypatch, phase):
    """A worker that is mid-lifecycle (starting/running) is live even with no pending
    turn left to show for it — the reap must be driven by ACTIVE_RUNS, not pending."""
    _reset_registries()
    stream_id = f"mid-lifecycle-{phase}"
    session = _Session(active_stream_id=stream_id, session_id=f"mid-lifecycle-{phase}-session")
    config.STREAMS[stream_id] = queue.Queue()
    config.register_active_run(stream_id, session_id=session.session_id, phase=phase)
    try:
        assert routes._active_stream_blocks_chat_start(session, stream_id) is True
        assert stream_id in config.STREAMS
    finally:
        config.unregister_active_run(stream_id)


def test_orphan_clear_releases_every_stream_owned_registry():
    """Recovering an orphan must not leak the dead turn's per-stream state.

    A crashed or wedged worker never reaches its own teardown, so removing only
    STREAMS + the owner left the agent instance, cancel flag, partial/reasoning
    text, live tool calls, goal marker and last event id allocated for the life
    of the process -- one stale set per recovered orphan. The clear mirrors the
    canonical worker teardown set (api/streaming.py, api/gateway_chat.py).
    """
    _reset_registries()
    stream_id = "orphan-with-residual-state"
    session = _Session(active_stream_id=stream_id, session_id="residual-state-session")

    config.STREAMS[stream_id] = queue.Queue()
    config.AGENT_INSTANCES[stream_id] = object()
    config.CANCEL_FLAGS[stream_id] = object()
    config.STREAM_GOAL_RELATED[stream_id] = True
    config.STREAM_PARTIAL_TEXT[stream_id] = "half a turn"
    config.STREAM_REASONING_TEXT[stream_id] = "thinking..."
    config.STREAM_LIVE_TOOL_CALLS[stream_id] = [{"name": "run_shell"}]
    config.STREAM_LAST_EVENT_ID[stream_id] = "evt-1"
    config.STREAM_SESSION_OWNERS[stream_id] = session.session_id

    registries = (
        "STREAMS",
        "AGENT_INSTANCES",
        "CANCEL_FLAGS",
        "STREAM_GOAL_RELATED",
        "STREAM_PARTIAL_TEXT",
        "STREAM_REASONING_TEXT",
        "STREAM_LIVE_TOOL_CALLS",
        "STREAM_LAST_EVENT_ID",
    )
    try:
        assert routes._active_stream_blocks_chat_start(session, stream_id) is False

        leaked = sorted(
            name for name in registries if getattr(config, name).get(stream_id) is not None
        )
        assert leaked == [], f"orphan recovery left per-stream state allocated: {leaked}"
        assert stream_id not in config.STREAM_SESSION_OWNERS
    finally:
        for name in registries:
            getattr(config, name).pop(stream_id, None)
        config.STREAM_SESSION_OWNERS.pop(stream_id, None)
