"""A stream that was registered but whose worker has not been admitted yet must not
be reaped as an orphan, however old its pending turn is.

Re-gate regression for PR #7302 (review 2026-10-02T08:59:35Z, finding 5 -- "CORE"):

``_active_stream_blocks_chat_start()`` keeps a registered stream only while
``ACTIVE_RUNS`` holds a live-worker row or ``_pending_turn_in_registration_window()``
is true -- and that window requires a fresh ``pending_started_at``
(``_REPAIR_STALE_PENDING_GRACE_SECONDS``, 30 s). Both launch sites register the stream
and schedule the worker BEFORE the worker admits itself, and the regeneration site
holds that worker at ``release_worker.wait()`` while ``s.save()`` runs. A save slower
than the grace window -- or a turn whose pending timestamp was persisted earlier --
therefore leaves a stream that is genuinely launching, with no worker row and a stale
pending timestamp: the orphan check clears it and the first worker exits without
running its turn. master never clears a registered stream on age alone, so this is a
regression introduced by the orphan cleanup in this branch.

The fix publishes a launch-phase ownership claim atomically with the ``STREAMS``
registration and keeps it until worker admission, so the orphan check must keep the
stream while that claim exists, whatever the pending age.

The two tests below are the two rows of the maintainer's re-gate table: 5 s of pending
age is inside the registration window and must block; 35 s of pending age is past it
and must STILL block while the launch phase is in flight.

Grep for the assertions, not for source strings: nothing here reads the source text.
"""
import io
import threading
import time

import pytest

import api.config as config
import api.routes as routes
from api.models import _REPAIR_STALE_PENDING_GRACE_SECONDS

SESSION_ID = "launch-session"
STREAM_ID = "launch-stream"


class _LaunchSession:
    """The attributes the registration window and the orphan check read."""

    def __init__(self, *, pending_age_seconds):
        self.session_id = SESSION_ID
        self.active_stream_id = STREAM_ID
        self.pending_user_message = "hello"
        self.pending_started_at = time.time() - pending_age_seconds
        # Surface the real launch path and the stale-stream repair read.
        self.messages = []
        self.title = ""
        self.worktree_path = None
        self.pending_attachments = []
        self.pending_user_source = "webui"
        self.attachments = []
        self.profile = None
        self.model = "test-model"
        self.model_provider = "test-provider"
        self.workspace = "test-workspace"

    def save(self, *args, **kwargs):  # the launch persists; this fake must not touch disk
        return None


def _reset_registries():
    config.STREAMS.clear()
    config.ACTIVE_RUNS.clear()
    config.CANCEL_FLAGS.clear()
    config.STREAM_SESSION_OWNERS.clear()
    # Pre-fix heads have no claim API: the barrier test must still reach its
    # assertion there (that failure IS the RED), so this stays optional.
    getattr(config, "PRE_ADMISSION_CLAIMS", {}).clear()


@pytest.fixture(autouse=True)
def _isolated_registries():
    _reset_registries()
    yield
    _reset_registries()


def _simulate_launch_registration(session):
    """What both launch sites do before scheduling their worker.

    A channel is created, the stream is registered with its owner, and the
    launch-phase ownership claim is published -- all in the same ``STREAMS_LOCK``
    critical section the fix uses. The worker has been scheduled but has NOT
    admitted itself yet, so there is deliberately no ``ACTIVE_RUNS`` row.

    On a pre-fix head the claim API does not exist: the launch phase then degrades
    to exactly the bug (registered stream, stale pending turn, no worker row) and
    the barrier assertion below fails. That is the RED this file must reproduce by
    itself, from the committed file, without a stale log.
    """
    with config.STREAMS_LOCK:
        config.STREAMS[STREAM_ID] = config.create_stream_channel()
        publish = getattr(config, "publish_pre_admission_claim", None)
        if publish is not None:
            publish(STREAM_ID, streams_lock_held=True)
    config.register_stream_owner(STREAM_ID, session.session_id)


_REQUIRES_CLAIM_API = pytest.mark.skipif(
    not hasattr(config, "PRE_ADMISSION_CLAIMS"),
    reason="pre-admission claim fix not present (pre-fix RED)",
)


@_REQUIRES_CLAIM_API
def test_fresh_pending_launch_blocks_a_duplicate_start():
    """Re-gate table, row 5 s: still inside the grace window -> blocked."""
    session = _LaunchSession(pending_age_seconds=5)
    _simulate_launch_registration(session)

    assert routes._active_stream_blocks_chat_start(session, STREAM_ID) is True
    assert STREAM_ID in config.STREAMS


def test_stale_pending_launch_is_not_orphaned_before_admission():
    """Re-gate table, row 31 s: past the grace window, launch phase still in flight.

    This is the regression. master keeps the stream here; this branch reaps it.
    """
    session = _LaunchSession(
        pending_age_seconds=_REPAIR_STALE_PENDING_GRACE_SECONDS + 5
    )
    _simulate_launch_registration(session)

    blocked = routes._active_stream_blocks_chat_start(session, STREAM_ID)

    assert blocked is True, (
        "chat/start was admitted over a stream that is still in its launch phase: "
        "the first worker would exit without running its turn"
    )
    assert STREAM_ID in config.STREAMS, (
        "the orphan check cleared a stream whose launch phase was still in flight"
    )
    assert config.STREAM_SESSION_OWNERS.get(STREAM_ID) == SESSION_ID, (
        "the orphan check released the owner of a stream that was still launching"
    )


@_REQUIRES_CLAIM_API
def test_admission_retires_the_claim_and_a_later_orphan_is_still_reaped():
    """The claim must not survive worker admission, or a dead stream would lock out.

    Once the worker is admitted, ownership lives in ``ACTIVE_RUNS``; the claim is
    retired by the admission path. A stream that then loses both its worker row
    and every launch claim is a genuine orphan again and must be reaped -- the
    claim must not resurrect the 409 lockout the orphan cleanup was added to fix.
    """
    session = _LaunchSession(
        pending_age_seconds=_REPAIR_STALE_PENDING_GRACE_SECONDS + 5
    )
    _simulate_launch_registration(session)

    assert (
        config.retire_pre_admission_claim_if_owned(STREAM_ID) is True
    ), "the launch-phase claim was not retired at admission"

    assert routes._active_stream_blocks_chat_start(session, STREAM_ID) is False, (
        "a stream with no worker row and no launch claim was kept: the orphan "
        "cleanup is dead and every later chat/start would 409"
    )
    assert STREAM_ID not in config.STREAMS


@_REQUIRES_CLAIM_API
def test_retiring_a_claim_by_identity_never_touches_a_successor():
    """A stale retire must not clear the claim a successor published."""
    successor_token = config.publish_pre_admission_claim(STREAM_ID)

    assert (
        config.retire_pre_admission_claim_if_owned(
            STREAM_ID, claim_token="a-superseded-token"
        )
        is False
    )
    assert config.PRE_ADMISSION_CLAIMS.get(STREAM_ID) == successor_token, (
        "retiring a superseded token cleared the successor's launch claim"
    )

    assert (
        config.retire_pre_admission_claim_if_owned(
            STREAM_ID, claim_token=successor_token
        )
        is True
    )
    assert STREAM_ID not in config.PRE_ADMISSION_CLAIMS


@_REQUIRES_CLAIM_API
def test_the_canonical_teardown_retires_the_claim():
    """No leak: the single teardown entry point retires the claim with the stream."""
    config.publish_pre_admission_claim(STREAM_ID)
    assert STREAM_ID in config.PRE_ADMISSION_CLAIMS

    config.release_stream_owned_registries(STREAM_ID, session_id=SESSION_ID)

    assert STREAM_ID not in config.PRE_ADMISSION_CLAIMS, (
        "a claim outlived the canonical teardown of its stream"
    )


class _ExplodingThread:
    """Constructs fine (like the real thread object) and fails on ``start()``."""

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")


@_REQUIRES_CLAIM_API
def test_a_failed_launch_retires_its_claim_and_does_not_lock_the_stream(monkeypatch):
    """A launch whose worker thread never starts must not strand its claim.

    ``_cleanup_chat_start_launch_failure()`` clears the stream registries directly and
    never goes through the canonical release funnel, so the launch-phase claim has to
    be retired there too. A stranded claim keeps the orphan check blocking that stream
    id whatever the pending age -- a permanent lockout for the session, not just a
    leaked entry (re-gate finding 3).
    """
    import types as _types

    import api.session_ops as session_ops
    import api.turn_journal as turn_journal

    monkeypatch.setattr(routes.threading, "Thread", _ExplodingThread)
    monkeypatch.setattr(
        routes.uuid, "uuid4", lambda: _types.SimpleNamespace(hex=STREAM_ID)
    )
    monkeypatch.setattr(
        routes, "_prepare_chat_start_session_for_stream", lambda *a, **k: None
    )
    monkeypatch.setattr(session_ops, "snapshot_session_state", lambda s: {})
    monkeypatch.setattr(session_ops, "restore_session_state", lambda *a, **k: None)
    monkeypatch.setattr(turn_journal, "append_turn_journal_event", lambda *a, **k: {})
    monkeypatch.setattr(routes, "set_last_workspace", lambda *a, **k: None)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda cfg: False)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda sid: threading.Lock())

    session = _LaunchSession(
        pending_age_seconds=_REPAIR_STALE_PENDING_GRACE_SECONDS + 5
    )
    monkeypatch.setattr(routes, "get_session", lambda sid: session)

    with pytest.raises(RuntimeError):
        routes._start_chat_stream_for_session(
            session,
            msg="hello",
            attachments=[],
            workspace="test-workspace",
            model="test-model",
            model_provider="test-provider",
            external_runtime_owned=False,
        )

    assert STREAM_ID not in config.STREAMS, (
        "the failed launch left its stream registered"
    )
    assert STREAM_ID not in config.PRE_ADMISSION_CLAIMS, (
        "the failed launch stranded its launch-phase claim: the orphan check keeps a "
        "claimed stream alive whatever the pending age, so this stream id is locked "
        "out for the life of the process"
    )
    assert routes._active_stream_blocks_chat_start(session, STREAM_ID) is False, (
        "a chat/start is still blocked by a stream whose worker never started"
    )


class _RecordingHandler:
    """Minimal HTTP handler stub: the launch routes only write a JSON body."""

    def __init__(self):
        self.wfile = io.BytesIO()
        self.headers = {}

    def send_response(self, *args, **kwargs):
        pass

    def send_header(self, *args, **kwargs):
        pass

    def end_headers(self, *args, **kwargs):
        pass

    def close_connection(self):
        pass


def _drive_reaper_until(predicate, timeout: float = 0.6, interval: float = 0.02):
    """Run the REAL ``_reaper_loop`` in a thread until ``predicate()`` holds."""
    from api import background_process as bp

    original = bp._REAPER_INTERVAL_SECS
    bp._REAPER_INTERVAL_SECS = interval
    bp.start_session_channel_reaper()
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


def _stub_launch_route(monkeypatch, *, session):
    """Wire the common collaborators of the ``/btw`` and ``/background`` routes."""
    import types as _types

    import api.background as background
    import api.models as models

    _GatedThread.launched = []
    # Replace routes' VIEW of the threading module, NOT the module itself: patching
    # ``routes.threading.Thread`` mutates the shared module object, which also stubs
    # the reaper's own thread inside api.background_process -- and then the sweep
    # under test never runs (and stop_session_channel_reaper trips on a stub with no
    # is_alive()).
    monkeypatch.setattr(
        routes,
        "threading",
        _types.SimpleNamespace(
            Thread=_GatedThread, Lock=threading.Lock, Event=threading.Event
        ),
    )
    monkeypatch.setattr(
        routes.uuid, "uuid4", lambda: _types.SimpleNamespace(hex=STREAM_ID)
    )
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **k: None)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda sid: False)
    monkeypatch.setattr(routes, "get_session", lambda sid: session)
    # The hidden session is created server-side; keep it in memory.
    monkeypatch.setattr(
        models, "new_session", lambda **kwargs: _LaunchSession(pending_age_seconds=5)
    )
    monkeypatch.setattr(background, "track_btw", lambda *a, **k: None)
    monkeypatch.setattr(background, "track_background", lambda *a, **k: None)
    return background


def test_the_btw_launch_publishes_a_claim_the_sweep_must_respect(monkeypatch):
    """``/btw`` registers a stream whose worker has only been scheduled.

    That window -- registered stream, no ``ACTIVE_RUNS`` row -- is exactly what the
    reaper's sweep hunts for. Before this fix ``/btw`` published no launch-phase
    claim, so the sweep harvested the stream and the worker started with nothing to
    run: the claim went from decorative-for-chat/start to load-bearing (finding B).
    """
    session = _LaunchSession(pending_age_seconds=5)
    _stub_launch_route(monkeypatch, session=session)

    routes._handle_btw(
        _RecordingHandler(), {"session_id": SESSION_ID, "question": "what time is it"}
    )

    assert STREAM_ID in config.STREAMS, "the /btw launch did not register its stream"
    assert len(_GatedThread.launched) == 1, "the /btw launch never scheduled its worker"
    assert STREAM_ID in config.PRE_ADMISSION_CLAIMS, (
        "the /btw launch registered a stream with no launch-phase claim: the reaper's "
        "sweep classifies it as dead and harvests it before the worker runs the task"
    )

    harvested = _drive_reaper_until(lambda: STREAM_ID not in config.STREAMS)
    assert harvested is False, (
        "the sweep harvested a /btw stream that was still launching"
    )
    assert STREAM_ID in config.STREAMS


def test_the_background_launch_publishes_a_claim_the_sweep_must_respect(monkeypatch):
    """``/api/background`` has the same registration-then-admission window."""
    session = _LaunchSession(pending_age_seconds=5)
    background = _stub_launch_route(monkeypatch, session=session)
    monkeypatch.setattr(background, "complete_background", lambda *a, **k: None)

    import api.session_ops as session_ops

    monkeypatch.setattr(session_ops, "snapshot_session_state", lambda s: {})

    routes._handle_background(
        _RecordingHandler(), {"session_id": SESSION_ID, "prompt": "summarise this"}
    )

    assert STREAM_ID in config.STREAMS, (
        "the background launch did not register its stream"
    )
    assert STREAM_ID in config.PRE_ADMISSION_CLAIMS, (
        "the background launch registered a stream with no launch-phase claim: the "
        "sweep harvests it and the worker starts with nothing to run"
    )

    harvested = _drive_reaper_until(lambda: STREAM_ID not in config.STREAMS)
    assert harvested is False, (
        "the sweep harvested a background stream that was still launching"
    )


class _GatedThread:
    """Stands in for ``threading.Thread``: records the launch, never runs the body.

    Its recorded instance IS the "worker scheduled but not admitted yet" state the
    launch phase consists of, held open with no timing: the test decides when the
    admission happens, so the barrier is deterministic.
    """

    launched: list = []

    def __init__(self, target=None, args=(), kwargs=None, **rest):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.__class__.launched.append(self)

    def start(self):
        pass


@_REQUIRES_CLAIM_API
def test_the_real_launch_path_blocks_a_second_start_and_runs_its_worker(monkeypatch):
    """The maintainer's two-sided requirement, over the REAL ordinary-start path.

    "The second start must stay blocked and the first worker must run." This drives
    ``_start_chat_stream_for_session`` itself with its heavy collaborators stubbed
    and ``threading.Thread`` replaced by a recorder, so the launch phase is held
    open exactly as a slow ``s.save()`` holds it -- no sleeps, no timing.
    """
    import types as _types

    import api.session_ops as session_ops
    import api.turn_journal as turn_journal

    _GatedThread.launched = []
    monkeypatch.setattr(routes.threading, "Thread", _GatedThread)
    monkeypatch.setattr(
        routes.uuid,
        "uuid4",
        lambda: _types.SimpleNamespace(hex=STREAM_ID),
    )
    monkeypatch.setattr(
        routes, "_prepare_chat_start_session_for_stream", lambda *a, **k: None
    )
    monkeypatch.setattr(session_ops, "snapshot_session_state", lambda s: {})
    monkeypatch.setattr(turn_journal, "append_turn_journal_event", lambda *a, **k: {})
    monkeypatch.setattr(routes, "set_last_workspace", lambda *a, **k: None)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda cfg: False)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda sid: threading.Lock())

    session = _LaunchSession(
        pending_age_seconds=_REPAIR_STALE_PENDING_GRACE_SECONDS + 5
    )

    result = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        attachments=[],
        workspace="test-workspace",
        model="test-model",
        model_provider="test-provider",
        external_runtime_owned=False,
    )

    assert not (isinstance(result, dict) and result.get("error")), result
    assert STREAM_ID in config.STREAMS, "the real launch did not register its stream"
    assert STREAM_ID in config.PRE_ADMISSION_CLAIMS, (
        "the real launch registered a stream without publishing its launch-phase "
        "claim: the orphan check then reaps a stream that is still launching"
    )
    assert len(_GatedThread.launched) == 1, "the launch never scheduled its worker"

    # (1) The duplicate start stays blocked and the stream is kept.
    assert routes._active_stream_blocks_chat_start(session, STREAM_ID) is True, (
        "a second chat/start was admitted over the real launch phase"
    )
    assert STREAM_ID in config.STREAMS

    # (2) The worker was scheduled for THIS stream.
    worker = _GatedThread.launched[0]
    assert STREAM_ID in worker.args, (
        "the scheduled worker does not carry this stream id, so it would not run "
        "the turn the duplicate start was trying to take over"
    )

    # (3) Release the barrier -> admission: the worker publishes its ACTIVE_RUNS
    # row and retires the claim (the two operations api/streaming.py performs at
    # admission). From here the guard decides by the live worker row.
    config.register_active_run(
        STREAM_ID,
        session_id=session.session_id,
        started_at=time.time(),
        phase="starting",
    )
    assert config.retire_pre_admission_claim_if_owned(STREAM_ID) is True
    assert STREAM_ID not in config.PRE_ADMISSION_CLAIMS
    assert routes._active_stream_blocks_chat_start(session, STREAM_ID) is True, (
        "after admission the guard must block on the live ACTIVE_RUNS worker row"
    )


class _BlockingSaveSession(_LaunchSession):
    """``s.save()`` parks inside the launch phase until the test releases it.

    The regeneration site schedules its worker, then runs ``s.save()``, and only
    then calls ``release_worker.set()``. Blocking the save on an ``Event`` holds
    that window open with no timing at all, so the test -- not a sleep -- decides
    when the worker is admitted. That is the maintainer's Oct 2 schedule.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.save_started = threading.Event()
        self.release_save = threading.Event()

    def save(self, *args, **kwargs):
        self.save_started.set()
        self.release_save.wait(10)
        return None


def test_the_real_regeneration_launch_blocks_a_duplicate_start_and_admits_its_worker(
    monkeypatch,
):
    """The maintainer's regeneration barrier, over the REAL regeneration path.

    His Oct 2 case: the regeneration site holds its worker at
    ``release_worker.wait()`` while ``s.save()`` runs, then a second ``chat/start``
    arrives. The ordinary-start barrier above drives admission with a thread stub
    that never runs its body plus a hand-made ``ACTIVE_RUNS`` row -- "admission
    simulated". Here ``threading.Thread`` is the real one, the worker reaches its
    own admission step, and the two-sided requirement ("the second start must stay
    blocked and the first worker must run") is asserted over this path.
    """
    import types as _types

    import api.session_ops as session_ops
    import api.turn_journal as turn_journal

    admitted = threading.Event()
    seen = {}

    def _admitting_worker(
        session_id, msg_text, model, workspace, stream_id, attachments=None, **kwargs
    ):
        # What api/streaming.py does the moment its worker is admitted: publish the
        # ACTIVE_RUNS row and retire the launch-phase claim.
        config.register_active_run(
            stream_id,
            session_id=session_id,
            started_at=time.time(),
            phase="starting",
        )
        retire = getattr(config, "retire_pre_admission_claim_if_owned", None)
        if retire is not None:
            retire(stream_id)
        seen["stream_id"] = stream_id
        seen["msg"] = msg_text
        admitted.set()

    monkeypatch.setattr(routes, "_run_agent_streaming", _admitting_worker)
    monkeypatch.setattr(
        routes.uuid, "uuid4", lambda: _types.SimpleNamespace(hex=STREAM_ID)
    )
    monkeypatch.setattr(
        routes, "_prepare_chat_start_session_for_stream", lambda *a, **k: None
    )
    monkeypatch.setattr(routes, "set_last_workspace", lambda *a, **k: None)
    monkeypatch.setattr(
        routes, "publish_session_list_changed", lambda *a, **k: None, raising=False
    )
    monkeypatch.setattr(
        routes, "compression_recovery_payload_for_session", lambda s: None, raising=False
    )
    monkeypatch.setattr(
        routes, "clear_compression_recovery", lambda s: None, raising=False
    )
    monkeypatch.setattr(turn_journal, "append_turn_journal_event", lambda *a, **k: {})
    # The regeneration transaction itself is not what this test covers: stub its
    # three steps so the assertion is about the launch phase, and leave the launch
    # funnel (worker schedule, save, release) real.
    turn = _types.SimpleNamespace(
        revision=1, message_text="hello", attachments=[], source="webui"
    )
    monkeypatch.setattr(
        session_ops,
        "plan_regeneration",
        lambda s, expected_revision, lock_held: _types.SimpleNamespace(turn=turn),
    )
    monkeypatch.setattr(session_ops, "snapshot_regeneration_state", lambda s: {})
    monkeypatch.setattr(
        session_ops,
        "apply_regeneration_plan",
        lambda s, plan, return_context_user=False: (True, None),
    )

    session = _BlockingSaveSession(
        pending_age_seconds=_REPAIR_STALE_PENDING_GRACE_SECONDS + 5
    )
    session.messages = [_types.SimpleNamespace(role="user", text="hello")]

    result = {}

    def _launch():
        try:
            result["response"] = routes._start_regeneration_stream_locked(
                session,
                turn=turn,
                workspace="test-workspace",
                model="test-model",
                model_provider="test-provider",
                normalized_model=False,
                diag=None,
                goal_related=False,
                source="webui",
                moa_config=None,
                backend_is_gateway=False,
            )
        except BaseException as exc:  # a dying launch thread must not look like success
            result["error"] = exc

    launcher = threading.Thread(target=_launch, daemon=True)
    launcher.start()

    # (1) The launch phase is open: the stream is registered and the save is still
    # running, so the worker has been scheduled but not admitted.
    assert session.save_started.wait(10), "the regeneration launch never reached s.save()"
    assert STREAM_ID in config.STREAMS, (
        "the regeneration launch did not register its stream"
    )
    assert not admitted.is_set(), (
        "the worker ran its turn before the regeneration launch persisted its save"
    )

    # (2) The duplicate start stays blocked and the stream is kept, whatever the
    # pending age. Pre-claim heads clear it here -- this is the RED.
    assert routes._active_stream_blocks_chat_start(session, STREAM_ID) is True, (
        "a second chat/start was admitted over the regeneration launch phase: the "
        "first worker would exit without running its turn"
    )
    assert STREAM_ID in config.STREAMS, (
        "the orphan check cleared a stream whose regeneration launch was still in flight"
    )
    assert STREAM_ID in getattr(config, "PRE_ADMISSION_CLAIMS", {}), (
        "the regeneration launch registered a stream with no launch-phase claim: the "
        "orphan check clears a stream that is still launching"
    )

    # (3) Release the launch -> the real worker is admitted.
    session.release_save.set()
    assert admitted.wait(10), "the regenerated turn's worker was never admitted"
    launcher.join(10)
    assert "error" not in result, result.get("error")
    assert seen["stream_id"] == STREAM_ID, (
        "the admitted worker does not carry this stream id, so it would run a turn "
        "the duplicate start was trying to take over"
    )
    assert result["response"]["stream_id"] == STREAM_ID
    assert STREAM_ID not in getattr(config, "PRE_ADMISSION_CLAIMS", {}), (
        "the launch-phase claim survived worker admission: a dead stream would then "
        "be kept alive forever by a claim nobody retires"
    )

    # (4) After admission the liveness lives in ACTIVE_RUNS, so the guard blocks on
    # the worker row rather than on the retired claim.
    assert routes._active_stream_blocks_chat_start(session, STREAM_ID) is True, (
        "after admission the guard must block on the live ACTIVE_RUNS worker row"
    )
