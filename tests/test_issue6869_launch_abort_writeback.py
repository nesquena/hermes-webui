"""Regression tests for #6869: launch-failure must clear SESSION_WRITEBACK_OWNERS.

Follow-up to #6636 (shipped in exp-v0.52.187), which fixed the cancel-finalizer
stale-write race and cleared ``SESSION_WRITEBACK_OWNERS`` on the two Gateway
paths that fire in normal use. A Codex re-gate identified the remaining leak
sites of the same class, and the review round that followed found three more
(one of them bricking):

1. ``_prepare_chat_start_session_for_stream`` (api/routes.py) registers the
   writeback owner before any preparation work. A throw anywhere after that
   registration — ``s.save()``, the retained-row validation, provisional-title
   preparation, the eager checkpoint — used to bypass the save-only catch and
   leak the owner.
2. ``_start_chat_stream_for_session`` starts the worker thread; a ``Thread(...)``
   construction or ``thr.start()`` failure used to leave ``STREAMS``, stream
   ownership, the writeback owner and the persisted ``active_stream_id`` behind,
   so every later send for that session returned 409 (bricked until restart).
3. ``_handle_btw`` and ``_handle_background`` register owners independently and
   had no abort cleanup at all; a background failure also left the tracked task
   permanently ``running``.

The fix is one shared launch-abort helper — ``_cleanup_chat_start_launch_failure`` — that
unwinds the registries, the thread state and the session reference together
(compare-and-clear so a successor's claim is never touched), called from every
one of those sites.
"""

import threading
import time
from unittest.mock import Mock

import pytest

import api.config as config
import api.models as models
import api.routes as routes
from api.models import Session


@pytest.fixture(autouse=True)
def _isolate_sessions(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file, raising=False)
    models.SESSIONS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()
    config.STREAMS.clear()
    config.STREAM_SESSION_OWNERS.clear()
    yield
    models.SESSIONS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()
    config.STREAMS.clear()
    config.STREAM_SESSION_OWNERS.clear()


def _make_session(sid: str) -> Session:
    s = Session(session_id=sid, messages=[])
    models.SESSIONS[sid] = s
    return s


def _reset_cfg_state() -> None:
    """Clear the process-wakeup / deferred-wakeup shared state for a test.

    Local copy of the helper in ``tests/test_wakeup_defer_race.py`` so the
    launch-abort file stays self-contained: the wakeup retry path reads
    ``DEFERRED_PROCESS_WAKEUPS``, ``PENDING_BG_TASK_COMPLETIONS`` and
    ``ACTIVE_RUNS`` from ``api.config`` and a stale row from another test
    would make the exactly-once assertions flaky.
    """
    from api import config as _cfg

    if hasattr(_cfg, "ACTIVE_RUNS"):
        with _cfg.ACTIVE_RUNS_LOCK:
            _cfg.ACTIVE_RUNS.clear()
    with _cfg.STREAMS_LOCK:
        _cfg.STREAMS.clear()
    _cfg.PENDING_BG_TASK_COMPLETIONS.clear()
    with _cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
        _cfg.DEFERRED_PROCESS_WAKEUPS.clear()


def test_save_throw_after_register_clears_writeback_owner(monkeypatch):
    """#6869: ``_prepare_chat_start_session_for_stream`` registers the
    writeback owner before any preparation work. A save() throw must not leak
    the entry."""
    s = _make_session("save-throw")
    stream_id = "stream-save-throw"
    # Pre-register so we can assert the abort path cleared it.
    config.register_session_writeback_owner(s.session_id, stream_id)
    assert config.SESSION_WRITEBACK_OWNERS.get(s.session_id) == stream_id

    s.save = Mock(side_effect=RuntimeError("disk full"))

    with pytest.raises(RuntimeError, match="disk full"):
        routes._prepare_chat_start_session_for_stream(
            s,
            msg="hello",
            attachments=[],
            workspace="/tmp",
            model="m",
            model_provider="p",
            stream_id=stream_id,
        )

    # The fix: writeback owner is cleared because the failed stream still
    # owned it (compare-and-clear).
    assert config.SESSION_WRITEBACK_OWNERS.get(s.session_id) is None


def test_save_throw_does_not_touch_other_sessions_owner(monkeypatch):
    """#6869: the launch-abort helper only clears the entry for the failed
    stream's session. An unrelated session with its own writeback owner must
    be left alone."""
    s_failed = _make_session("session-failed")
    s_other = _make_session("session-other")
    failed_stream = "stream-failed"
    other_stream = "stream-other"
    config.register_session_writeback_owner(s_failed.session_id, failed_stream)
    config.register_session_writeback_owner(s_other.session_id, other_stream)

    s_failed.save = Mock(side_effect=RuntimeError("disk full"))
    with pytest.raises(RuntimeError):
        routes._prepare_chat_start_session_for_stream(
            s_failed,
            msg="hello",
            attachments=[],
            workspace="/tmp",
            model="m",
            model_provider="p",
            stream_id=failed_stream,
        )

    # The failed session's writeback owner is cleared.
    assert config.SESSION_WRITEBACK_OWNERS.get(s_failed.session_id) is None
    # The unrelated session's owner is left alone — compare-and-clear
    # is per-session via the session_id key.
    assert config.SESSION_WRITEBACK_OWNERS.get(s_other.session_id) == other_stream


def test_save_success_does_not_clear_writeback_owner(monkeypatch):
    """#6869 regression guard: a successful save() (no throw) must leave
    the writeback owner in place — it is the caller's job to clear it
    when the stream actually starts. Pin the happy path so a future
    change does not over-eagerly clear on every save()."""
    s = _make_session("save-success")
    stream_id = "stream-save-success"
    config.register_session_writeback_owner(s.session_id, stream_id)

    s.save = Mock()  # no side_effect — succeeds

    routes._prepare_chat_start_session_for_stream(
        s,
        msg="hello",
        attachments=[],
        workspace="/tmp",
        model="m",
        model_provider="p",
        stream_id=stream_id,
    )

    assert config.SESSION_WRITEBACK_OWNERS.get(s.session_id) == stream_id


def test_pre_save_exception_clears_writeback_owner(monkeypatch):
    """#6869 round 2: an exception raised during preparation BEFORE s.save()
    must also clear the owner. The old fix only wrapped the save() call, so a
    throw at provisional-title preparation leaked the entry. Inject one via
    the eager-session-save checkpoint, which runs before the save."""
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "eager")

    def _boom(*a, **kw):
        raise RuntimeError("provisional title failed")

    monkeypatch.setattr(
        routes, "_checkpoint_user_message_for_eager_session_save", _boom
    )

    s = _make_session("pre-save-throw")
    stream_id = "stream-pre-save-throw"
    config.register_session_writeback_owner(s.session_id, stream_id)
    assert config.SESSION_WRITEBACK_OWNERS.get(s.session_id) == stream_id

    with pytest.raises(RuntimeError, match="provisional title failed"):
        routes._prepare_chat_start_session_for_stream(
            s,
            msg="hello",
            attachments=[],
            workspace="/tmp",
            model="m",
            model_provider="p",
            stream_id=stream_id,
        )

    assert config.SESSION_WRITEBACK_OWNERS.get(s.session_id) is None
    # The persisted pending state must not keep pointing at the dead stream.
    assert s.active_stream_id is None
    assert s.pending_user_message is None


def test_thread_start_throw_clears_writeback_owner(monkeypatch):
    """#6869: ``_start_chat_stream_for_session`` registers the writeback
    owner via ``_prepare_chat_start_session_for_stream``, then starts the
    worker thread. If ``thr.start()`` raises, the launch-abort helper unwinds
    every registry the half-launched stream touched."""
    sid = "thread-start-throw"
    stream_id = "stream-thread-start-throw"

    s = _make_session(sid)
    # Pre-register so we can verify the abort path cleared it.
    config.register_session_writeback_owner(sid, stream_id)

    # Stub the worker thread so thr.start() raises. We patch
    # ``threading.Thread`` in the routes module so the call inside
    # ``_start_chat_stream_for_session`` hits our stub.
    class _StubThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread start failed")

    monkeypatch.setattr(routes.threading, "Thread", _StubThread)

    # Drive the function. We need to bypass the active-stream guard
    # (active_stream_id must be None or stale) and the path that
    # returns early on regeneration; with no ``regeneration`` and a
    # fresh session, the function reaches the ``thr.start()`` call.
    # Stub the agent-runtime barrier so we don't depend on Gateway
    # availability.
    monkeypatch.setattr(
        routes,
        "_agent_runtime_barrier_response",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        routes,
        "_active_stream_blocks_chat_start",
        lambda *a, **kw: False,
    )
    monkeypatch.setattr(
        routes,
        "_is_hidden_empty_session",
        lambda s: False,
    )
    monkeypatch.setattr(
        routes,
        "_run_agent_streaming",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        routes,
        "webui_gateway_chat_enabled",
        lambda *a, **kw: False,
    )
    # Bypass the sidecar / state-writer hooks to keep the test tight.
    monkeypatch.setattr(
        routes,
        "_get_session_agent_lock",
        lambda sid: threading.RLock(),
    )

    with pytest.raises(RuntimeError, match="thread start failed"):
        routes._start_chat_stream_for_session(
            s,
            msg="hello",
            workspace="/tmp",
            model="m",
            model_provider="p",
        )

    # The fix: writeback owner is cleared by the launch-abort helper.
    assert config.SESSION_WRITEBACK_OWNERS.get(sid) is None
    # The bricking half of the same defect: the dead channel and the
    # persisted stream reference must both be gone, or the next send for
    # this session returns 409.
    assert stream_id not in config.STREAMS
    assert config.stream_owner_session_id(stream_id) is None
    assert s.active_stream_id is None


def test_thread_construction_failure_unwinds_registries(monkeypatch):
    """#6869 round 2 (CORE): a ``threading.Thread(...)`` *construction* failure
    — not just a ``start()`` failure — leaves the same brick behind. The
    launch-abort helper must cover construction too."""
    sid = "thread-construct-throw"
    stream_id = "stream-thread-construct-throw"

    s = _make_session(sid)
    config.register_session_writeback_owner(sid, stream_id)

    class _ThrowingThreadCtor:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("thread construction failed")

    monkeypatch.setattr(routes.threading, "Thread", _ThrowingThreadCtor)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
    monkeypatch.setattr(routes, "_active_stream_blocks_chat_start", lambda *a, **kw: False)
    monkeypatch.setattr(routes, "_is_hidden_empty_session", lambda s: False)
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda *a, **kw: False)
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda sid: threading.RLock())

    with pytest.raises(RuntimeError, match="thread construction failed"):
        routes._start_chat_stream_for_session(
            s,
            msg="hello",
            workspace="/tmp",
            model="m",
            model_provider="p",
        )

    assert config.SESSION_WRITEBACK_OWNERS.get(sid) is None
    assert stream_id not in config.STREAMS
    assert config.stream_owner_session_id(stream_id) is None
    assert s.active_stream_id is None


def test_abort_helper_leaves_successor_owner_untouched():
    """#6869: the launch-abort helper is compare-and-clear on the writeback
    owner. If a successor turn was admitted while the failed stream unwound,
    its claim must survive."""
    sid = "successor-race"
    dead_stream = "stream-dead"
    live_stream = "stream-live"
    s = _make_session(sid)
    config.register_session_writeback_owner(sid, live_stream)

    # A successor claimed the session between registration and abort.
    s.active_stream_id = live_stream
    config.register_stream_owner(live_stream, sid)
    with config.STREAMS_LOCK:
        config.STREAMS[dead_stream] = object()

    routes._cleanup_chat_start_launch_failure(s, dead_stream, reset_session=True)

    assert config.SESSION_WRITEBACK_OWNERS.get(sid) == live_stream
    assert s.active_stream_id == live_stream
    # The dead stream's own channel is still removed.
    assert dead_stream not in config.STREAMS


def test_btw_launch_failure_unwinds_registries(monkeypatch):
    """#6869 round 2: ``_handle_btw`` had no abort cleanup at all. A thread
    start failure must not leave the ephemeral session pointing at a dead
    stream."""
    parent = _make_session("btw-parent")
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda *a, **kw: False)
    # sid-aware resolver: the handler looks up the parent, and the merged
    # cleanup helper re-resolves the *ephemeral* session canonically.
    monkeypatch.setattr(
        routes, "get_session",
        lambda sid, metadata_only=False: models.SESSIONS.get(sid) or parent,
    )
    monkeypatch.setattr(routes, "bad", lambda h, m, status=400: {"status": status})
    monkeypatch.setattr(
        routes, "j", lambda h, payload, status=200: {"status": status, "payload": payload}
    )

    class _StubThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("btw thread start failed")

    monkeypatch.setattr(routes.threading, "Thread", _StubThread)

    with pytest.raises(RuntimeError, match="btw thread start failed"):
        routes._handle_btw(
            object(),
            {"session_id": "btw-parent", "question": "what?"},
        )

    # Every half-launched btw stream is unwound: no orphan owners anywhere.
    assert config.SESSION_WRITEBACK_OWNERS == {}
    assert config.STREAMS == {}
    assert config.STREAM_SESSION_OWNERS == {}
    for ephemeral in models.SESSIONS.values():
        assert getattr(ephemeral, "active_stream_id", None) is None


def test_background_launch_failure_unwinds_registries_and_fails_task(monkeypatch):
    """#6869 round 2: ``_handle_background`` had no abort cleanup, and a failure
    also left the tracked task permanently ``running`` — the frontend poll never
    saw a result."""
    parent = _make_session("bg-parent")
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
    # sid-aware: the handler looks up the parent, and the merged cleanup helper
    # re-resolves the *hidden bg* session canonically.
    monkeypatch.setattr(
        routes, "get_session",
        lambda sid, metadata_only=False: models.SESSIONS.get(sid) or parent,
    )
    monkeypatch.setattr(routes, "bad", lambda h, m, status=400: {"status": status})
    monkeypatch.setattr(
        routes, "j", lambda h, payload, status=200: {"status": status, "payload": payload}
    )

    class _StubThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("bg thread start failed")

    monkeypatch.setattr(routes.threading, "Thread", _StubThread)

    with pytest.raises(RuntimeError, match="bg thread start failed"):
        routes._handle_background(
            object(),
            {"session_id": "bg-parent", "prompt": "do a thing"},
        )

    # Registries are unwound.
    assert config.SESSION_WRITEBACK_OWNERS == {}
    assert config.STREAMS == {}
    assert config.STREAM_SESSION_OWNERS == {}
    for bg_session in models.SESSIONS.values():
        assert getattr(bg_session, "active_stream_id", None) is None

    # The tracked task must not be stranded in "running".
    import api.background as background

    tasks = background.get_background_tasks("bg-parent")
    assert tasks, "aborted background task must still be tracked"
    assert all(t["status"] != "running" for t in tasks), (
        "an aborted background task stayed 'running' forever"
    )


def test_source_launch_abort_helper_is_shared_across_all_sites(monkeypatch):
    """#6869 source guard: every launch-failure site must route through the
    shared ``_cleanup_chat_start_launch_failure`` helper rather than a bespoke cleanup.
    Pin the source so a future refactor cannot reintroduce a fourth bespoke
    try/except that forgets one of the registries."""
    src = routes.__file__
    with open(src, "r", encoding="utf-8") as fh:
        text = fh.read()

    def _block(name):
        start = text.index(f"def {name}(")
        end = text.index("\ndef ", start + 1)
        return text[start:end]

    # The helper itself exists and clears the writeback owner.
    helper = _block("_cleanup_chat_start_launch_failure")
    assert "clear_session_writeback_owner_if_owned" in helper, (
        "_cleanup_chat_start_launch_failure must clear the writeback owner"
    )
    assert "unregister_stream_owner" in helper, (
        "_cleanup_chat_start_launch_failure must unregister the stream owner"
    )
    assert "STREAMS.pop" in helper, (
        "_cleanup_chat_start_launch_failure must drop the dead stream channel"
    )

    # Every launch-failure site calls the helper.
    for site in (
        "_prepare_chat_start_session_for_stream",
        "_start_chat_stream_for_session",
        "_handle_btw",
        "_handle_background",
    ):
        assert "_cleanup_chat_start_launch_failure(" in _block(site), (
            f"{site} must route launch failures through _cleanup_chat_start_launch_failure (#6869)"
        )

    # The old bespoke per-site cleanup must be gone from the two chat-start
    # functions — the helper is the single cleanup path.
    for site in (
        "_prepare_chat_start_session_for_stream",
        "_start_chat_stream_for_session",
    ):
        assert "clear_session_writeback_owner_if_owned" not in _block(site), (
            f"{site} still clears the writeback owner inline instead of using "
            "the shared helper (#6869)"
        )


# ---------------------------------------------------------------------------
# #7680 re-gate (9/22) — BRICK deadlock + wakeup-lost findings.
# ---------------------------------------------------------------------------


def test_abort_with_lock_held_does_not_self_deadlock(monkeypatch):
    """#7680 finding 1 (BRICK): the abort path inside
    ``_prepare_chat_start_session_for_stream`` previously re-acquired the
    per-session lock that the chat-start loop already held — a plain
    ``threading.Lock`` (not RLock) self-deadlocked, bricking the session
    until process restart.

    The fix threads ``lock_held=True`` through the abort helper so the reset
    runs without the redundant ``with`` block. This test reproduces the
    BRICK with the **real** ``threading.Lock`` returned by
    ``_get_session_agent_lock`` (the prior launch tests substituted
    ``threading.RLock`` and missed it) and asserts the helper completes in
    bounded time.
    """
    s = _make_session("brick-repro")
    stream_id = "stream-brick"
    real_lock = config._get_session_agent_lock(s.session_id)
    assert isinstance(real_lock, type(threading.Lock())), (
        "test guard: the session lock must remain a plain threading.Lock — "
        "switching to RLock would silently mask this deadlock"
    )

    # Hold the real lock from the test thread, exactly as the chat-start
    # loop does. The abort must not block on acquire.
    acquired = real_lock.acquire(timeout=2.0)
    assert acquired, "test setup: failed to acquire the real session lock"
    try:
        s.active_stream_id = stream_id
        config.register_session_writeback_owner(s.session_id, stream_id)

        # Bound the abort call. Without ``lock_held=True`` this would
        # self-deadlock and the test would hit pytest's hang-detector.
        completed = threading.Event()
        result_box = {}

        def _run():
            try:
                routes._cleanup_chat_start_launch_failure(
                    s, stream_id, reset_session=True, lock_held=True
                )
                result_box["ok"] = True
            except BaseException as exc:  # pragma: no cover — defensive
                result_box["err"] = exc
            finally:
                completed.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        assert completed.wait(timeout=2.0), (
            "abort hung while caller held the session lock — "
            "_cleanup_chat_start_launch_failure re-acquired the same threading.Lock "
            "and self-deadlocked (#7680 BRICK)"
        )
        assert result_box.get("ok"), result_box.get("err")
    finally:
        real_lock.release()

    # A follow-up caller must be able to acquire the lock within bounded
    # time. The previous code path left ``active_stream_id`` set, so the
    # next chat-start for this session would 409.
    assert s.active_stream_id is None
    assert config.SESSION_WRITEBACK_OWNERS.get(s.session_id) is None
    assert real_lock.acquire(timeout=1.0), (
        "follow-up caller could not re-acquire the session lock — "
        "the abort path failed to release state cleanly (#7680)"
    )
    real_lock.release()


def test_chat_start_with_raising_prep_completes_under_real_lock(monkeypatch):
    """#7680 finding 1 (BRICK, end-to-end): drive
    ``_start_chat_stream_for_session`` with the real session lock and a
    raising ``s.save()``. The handler must return the original error in
    bounded time — not deadlock — and a follow-up send for the same
    session must succeed.

    Mirrors the maintainer's repro recipe:
    ``Codex drove the real handler with a preparation step that raises
    (for example, ``_checkpoint_user_message_for_eager_session_save`` or
    ``s.save()`` failing on a full disk or a permissions error)``.
    """
    s = _make_session("eager-repro")
    s.workspace = "/tmp"

    # Force the eager save-mode path so ``_checkpoint_user_message_for_eager_session_save``
    # is exercised; that helper also calls ``s.save()`` so the same Mock
    # failure cascades.
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "eager")

    # The save failure the maintainer called out: full disk / permissions
    # error during ``s.save()``. The eager helper also calls ``s.save()``;
    # making ``s.save`` itself raise is the single point of failure.
    s.save = Mock(side_effect=RuntimeError("disk full"))

    real_lock = config._get_session_agent_lock(s.session_id)
    assert isinstance(real_lock, type(threading.Lock()))

    # The chat-start loop holds the lock while it calls
    # ``_prepare_chat_start_session_for_stream``. Simulate that exactly by
    # acquiring the real lock from this thread before entry.
    with real_lock:
        completed = threading.Event()
        result_box = {}

        def _run():
            try:
                # ``_start_chat_stream_for_session`` tries the lock
                # itself; under the BRICK repro the lock is already held
                # by THIS test thread, so the inner attempt would block.
                # We instead call the inner step directly to assert the
                # abort path doesn't self-deadlock when the caller is
                # already inside the lock.
                routes._prepare_chat_start_session_for_stream(
                    s,
                    msg="hello",
                    attachments=[],
                    workspace="/tmp",
                    model="m",
                    model_provider="p",
                    stream_id="stream-eager-fail",
                )
            except RuntimeError as exc:
                result_box["err"] = exc
            finally:
                completed.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        assert completed.wait(timeout=3.0), (
            "_prepare_chat_start_session_for_stream hung while the session "
            "lock was held — the abort path re-acquired the same lock and "
            "self-deadlocked (#7680 BRICK)"
        )
        assert isinstance(result_box.get("err"), RuntimeError), (
            f"expected the original disk-full error to propagate, got: "
            f"{result_box!r}"
        )

    # The abort ran, the lock is released, and the session is no longer
    # bricked: a follow-up send can acquire the lock.
    assert s.active_stream_id is None
    assert config.SESSION_WRITEBACK_OWNERS.get(s.session_id) is None
    assert real_lock.acquire(timeout=1.0), (
        "session lock still held after the abort — follow-up send would "
        "hang (#7680 BRICK)"
    )
    real_lock.release()


def test_abort_preserves_process_wakeup_on_failure(monkeypatch):
    """#7680 findings 1+2 (SILENT): a failed worker start on the
    process-wakeup path left the wakeup drain with nothing to retry —
    ``PENDING_BG_TASK_COMPLETIONS`` was already consumed upstream, the
    abort cleared ``pending_user_message``, and the ``submitted`` turn
    journal event had no terminal sibling. The background result was lost
    with no retry.

    The fix: when ``preserve_wakeup=True`` is set and the abort happens
    with a non-empty ``pending_user_message``, the helper

    (1) **persists the prompt** via ``record_deferred_wakeup`` (finding 1;
        the bare ``PENDING_BG_TASK_COMPLETIONS`` re-arm is a no-op without
        a payload — the prompt must go to
        ``DEFERRED_PROCESS_WAKEUPS``), AND
    (2) appends an ``interrupted`` journal event via
        ``append_turn_journal_event_for_stream`` so it closes the SAME
        turn as the prior ``submitted`` event (finding 2; using the
        legacy ``append_turn_journal_event`` would mint a brand new
        ``turn_id`` and leave the ``submitted(turn A)`` event half-open
        forever).
    """
    import api.turn_journal as turn_journal

    s = _make_session("wakeup-preserve")
    wakeup_prompt = (
        "[IMPORTANT: Background process proc-abc123 completed (exit_code=0).\n"
        "Command: sleep 1\n"
        "Output:\nhello]"
    )
    s.pending_user_message = wakeup_prompt
    s.active_stream_id = "stream-wakeup-fail"
    config.register_session_writeback_owner(s.session_id, s.active_stream_id)

    # Capture journal events so we can assert the ``interrupted`` one.
    captured = []

    def _capture_append(sid, event):
        captured.append((sid, event))
        return event

    def _capture_append_for_stream(sid, stream_id_arg, event, **kwargs):
        captured.append((sid, event))
        return event

    monkeypatch.setattr(routes, "_rearm_process_wakeup_after_launch_failure",
                        routes._rearm_process_wakeup_after_launch_failure)
    monkeypatch.setattr(turn_journal, "append_turn_journal_event", _capture_append)
    monkeypatch.setattr(
        turn_journal, "append_turn_journal_event_for_stream",
        _capture_append_for_stream,
    )

    # Pre-condition: the marker is NOT in PENDING_BG_TASK_COMPLETIONS yet
    # (it was consumed upstream before the worker started).
    assert s.session_id not in config.PENDING_BG_TASK_COMPLETIONS

    routes._cleanup_chat_start_launch_failure(
        s,
        "stream-wakeup-fail",
        reset_session=True,
        lock_held=False,
        preserve_wakeup=True,
    )

    # Finding 1: a deferred prompt is now recorded for the session so the
    # wakeup turn-teardown / next-turn drain has a real payload to
    # redeliver. (Capture the prompt locally before the helper resets it.)
    from api import config as _cfg

    deferred = _cfg.DEFERRED_PROCESS_WAKEUPS.get(s.session_id) or []
    assert deferred, (
        "aborted wakeup did not call record_deferred_wakeup — the prompt "
        "would be lost with no retry (#7680 finding 1, SILENT)"
    )
    assert any(
        d.get("wakeup_prompt") == wakeup_prompt for d in deferred
    ), "the deferred entry's wakeup_prompt must match the actual prompt"
    # process_id is recovered from the pinned wakeup format.
    assert any(
        d.get("process_id") == "proc-abc123" for d in deferred
    ), "the deferred entry must carry the recovered process_id"

    # The legacy PENDING_BG_TASK_COMPLETIONS marker is also re-armed
    # (preserved behavior for any drain path that does not consult
    # DEFERRED_PROCESS_WAKEUPS).
    assert s.session_id in config.PENDING_BG_TASK_COMPLETIONS

    # Finding 2: the journal event closes the right turn.
    interrupted = [e for _sid, e in captured if e.get("event") == "interrupted"]
    assert interrupted, (
        "aborted wakeup turn did not append an 'interrupted' journal event "
        "— the 'submitted' sibling would stay half-open (#7680 finding 2)"
    )
    assert interrupted[0].get("reason") == "launch_failure"
    assert interrupted[0].get("stream_id") == "stream-wakeup-fail"

    # The writeback owner and the persisted stream id are still cleared
    # so a follow-up send does not see the dead channel.
    assert config.SESSION_WRITEBACK_OWNERS.get(s.session_id) is None
    assert s.active_stream_id is None


def test_abort_without_preserve_wakeup_leaves_marker_alone(monkeypatch):
    """#7680: ``preserve_wakeup=False`` (the default) must NOT re-arm the
    drain. A non-wakeup chat-start failure has no business touching
    ``PENDING_BG_TASK_COMPLETIONS``."""
    s = _make_session("no-preserve")
    s.pending_user_message = "regular user message"
    s.active_stream_id = "stream-no-wakeup"

    routes._cleanup_chat_start_launch_failure(
        s,
        "stream-no-wakeup",
        reset_session=True,
    )

    assert s.session_id not in config.PENDING_BG_TASK_COMPLETIONS
    assert s.active_stream_id is None


# ---------------------------------------------------------------------------
# #7680 re-gate (9/23) — wakeup prompt lost, wrong-turn closed, /btw &
# /background still bypass cleanup.
# ---------------------------------------------------------------------------


def test_rearm_wakeup_calls_record_deferred_wakeup(monkeypatch):
    """#7680 finding 1 (SILENT) regression: launching the launch-abort
    helper (``_cleanup_chat_start_launch_failure``) with
    ``preserve_wakeup=True`` for a session whose ``pending_user_message``
    is a pinned-shape wakeup prompt must (a) call
    ``record_deferred_wakeup`` so ``DEFERRED_PROCESS_WAKEUPS`` carries a
    redeliverable payload, and (b) recover the process_id from the
    pinned format. The old fix re-armed only the bare
    ``PENDING_BG_TASK_COMPLETIONS`` telemetry flag — drain had nothing
    to redeliver (``deferred_prompts=null``, ``zero turns started``).
    """
    s = _make_session("wakeup-direct")
    wakeup_prompt = (
        "[IMPORTANT: Background process proc-xyz789 completed (exit_code=2).\n"
        "Command: ls /tmp\n"
        "Output:\nfile1\nfile2]"
    )
    s.pending_user_message = wakeup_prompt
    s.active_stream_id = "stream-direct-wakeup"
    config.register_session_writeback_owner(s.session_id, s.active_stream_id)

    # Reset state so we observe the call cleanly.
    from api import config as _cfg

    _cfg.DEFERRED_PROCESS_WAKEUPS.pop(s.session_id, None)
    _cfg.PENDING_BG_TASK_COMPLETIONS.discard(s.session_id)

    # Spy on record_deferred_wakeup by patching the module attribute
    # that ``_rearm_process_wakeup_after_launch_failure`` imports at
    # call time.
    import api.background_process as _bp

    captured_calls: list[tuple[str, str, str]] = []

    def _capturing_record(sid_arg, pid_arg, prompt_arg):
        captured_calls.append((sid_arg, pid_arg, prompt_arg))
        return True

    monkeypatch.setattr(_bp, "record_deferred_wakeup", _capturing_record)

    # Drive the full canonical helper — this is the path the launch
    # failure actually exercises (``preserve_wakeup=True`` is set by
    # ``_prepare_chat_start_session_for_stream`` for the
    # ``process_wakeup`` source).
    routes._cleanup_chat_start_launch_failure(
        s,
        s.active_stream_id,
        reset_session=True,
        lock_held=False,
        preserve_wakeup=True,
    )

    assert captured_calls, (
        "preserve_wakeup launch-abort did not invoke record_deferred_wakeup "
        "at all — the prompt would be lost with no retry (#7680 finding 1, "
        "SILENT)"
    )
    sid_arg, pid_arg, prompt_arg = captured_calls[0]
    assert sid_arg == s.session_id
    assert pid_arg == "proc-xyz789", (
        f"process_id not recovered from the wakeup format: got {pid_arg!r}, "
        "expected 'proc-xyz789'"
    )
    assert prompt_arg == wakeup_prompt

    # And the helper's final state: marker re-armed, writeback owner
    # cleared, active_stream_id cleared. (DEFERRED_PROCESS_WAKEUPS
    # itself is populated by the real record_deferred_wakeup which we
    # patched out — the captured call above is the proof the fix made
    # the call.)
    assert s.session_id in config.PENDING_BG_TASK_COMPLETIONS
    assert config.SESSION_WRITEBACK_OWNERS.get(s.session_id) is None
    assert s.active_stream_id is None


def test_interrupted_journal_event_closes_existing_turn(monkeypatch):
    """#7680 finding 2 (SILENT) regression: when the abort fires for a
    stream that already has a ``submitted`` event in the turn journal, the
    ``interrupted`` event MUST land on the same ``turn_id`` so the turn
    closes (instead of becoming ``submitted(turn A) + interrupted(turn B)``
    with turn A forever pending).

    Drive this by writing a real ``submitted`` event with
    ``stream_id=stream-X`` to the journal, then invoking the abort
    helper with ``preserve_wakeup=True``. Pin both
    ``append_turn_journal_event`` and ``append_turn_journal_event_for_stream``
    so the test sees which one the helper used.
    """
    import api.turn_journal as turn_journal

    s = _make_session("turn-close")
    s.pending_user_message = (
        "[IMPORTANT: Background process proc-close completed (exit_code=0).\n"
        "Command: echo hi\n"
        "Output:\nhi]"
    )
    s.active_stream_id = "stream-close"
    config.register_session_writeback_owner(s.session_id, s.active_stream_id)

    # Write a real ``submitted`` event for stream-close so the journal
    # has a turn to close. The real append_turn_journal_event is fine
    # here — we only want to assert what the *helper* writes.
    submitted = turn_journal.append_turn_journal_event(
        s.session_id,
        {
            "event": "submitted",
            "stream_id": "stream-close",
            "turn_id": "turn-pre-existing",
        },
    )
    assert submitted.get("turn_id") == "turn-pre-existing"

    # Capture every journal write the helper performs.
    captured: list[dict] = []

    def _capture_append(sid, event, **kwargs):
        captured.append(dict(event))
        return event

    def _capture_append_for_stream(sid, stream_id_arg, event, **kwargs):
        # Mark which helper the call came from so the assertion can pin
        # the fix.
        event = dict(event)
        event["_via"] = "for_stream"
        captured.append(event)
        return event

    import api.turn_journal as _tj

    monkeypatch.setattr(_tj, "append_turn_journal_event", _capture_append)
    monkeypatch.setattr(
        _tj, "append_turn_journal_event_for_stream",
        _capture_append_for_stream,
    )

    routes._cleanup_chat_start_launch_failure(
        s,
        "stream-close",
        reset_session=True,
        lock_held=False,
        preserve_wakeup=True,
    )

    interrupted = [e for e in captured if e.get("event") == "interrupted"]
    assert interrupted, (
        "abort did not write an 'interrupted' event — the half-open "
        "turn would read as in-flight forever (#7680 finding 2)"
    )
    # The fix must use ``append_turn_journal_event_for_stream`` so the
    # existing turn_id is reused, NOT ``append_turn_journal_event``
    # which would mint a brand new turn_id.
    via = interrupted[0].get("_via")
    assert via == "for_stream", (
        f"abort wrote 'interrupted' via {via!r} — the legacy "
        "append_turn_journal_event mints a fresh turn_id and leaves "
        "submitted(turn A) half-open forever (#7680 finding 2)"
    )
    # And the actual turn_id re-uses the prior ``turn-pre-existing`` (the
    # real for_stream helper looks it up; the captured event already
    # contains the resolved turn_id).
    # We can't assert the exact id here (the journal write path inside
    # the for_stream helper is patched away), but we can assert the
    # helper was called with our stream_id so the for_stream resolver
    # is on the path.
    assert interrupted[0].get("stream_id") == "stream-close"


def test_btw_save_failure_after_register_unwinds_registries(monkeypatch):
    """#7680 finding 3 (SILENT) regression: ``_handle_btw`` used to do
    ``ephemeral.save()`` *after* registering the writeback owner but
    *before* the launch-failure try/except. A throw on that second save
    (disk full, permissions error) orphaned the writeback owner and
    pinned the ephemeral session to a dead stream. The fix moves the
    save into the same try block as the rest of the launch, so the
    canonical ``_cleanup_chat_start_launch_failure`` helper unwinds
    every registry.
    """
    parent = _make_session("btw-savefail-parent")
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda *a, **kw: False)
    monkeypatch.setattr(
        routes, "get_session",
        lambda sid, metadata_only=False: models.SESSIONS.get(sid) or parent,
    )
    monkeypatch.setattr(routes, "bad", lambda h, m, status=400: {"status": status})
    monkeypatch.setattr(
        routes, "j", lambda h, payload, status=200: {"status": status, "payload": payload}
    )

    # Stub the Session class so the second ephemeral.save() raises. The
    # first save (before the writeback owner is registered) must still
    # succeed; only the second one (after) must fail.
    from api import models as _models

    class _BoomSession(_models.Session):
        save_call_count = 0

        def save(self, *a, **kw):
            type(self).save_call_count += 1
            if type(self).save_call_count == 1:
                return super().save(*a, **kw)
            raise RuntimeError("ephemeral save failed (disk full)")

    _BoomSession.save_call_count = 0
    monkeypatch.setattr(_models, "new_session", _BoomSession)

    with pytest.raises(RuntimeError, match="ephemeral save failed"):
        routes._handle_btw(
            object(),
            {"session_id": "btw-savefail-parent", "question": "what?"},
        )

    # The fix: the launch-abort helper unwinds every registry even when
    # the failure is on the *post-registration* save.
    assert config.SESSION_WRITEBACK_OWNERS == {}, (
        "ephemeral save failure after writeback-owner register leaked the "
        "owner (writeback owner: "
        f"{dict(config.SESSION_WRITEBACK_OWNERS)!r})"
    )
    assert config.STREAMS == {}
    assert config.STREAM_SESSION_OWNERS == {}
    for ephemeral in models.SESSIONS.values():
        assert getattr(ephemeral, "active_stream_id", None) is None, (
            "ephemeral session kept an active_stream_id after save failure "
            "— the next send would 409 on the dead channel"
        )


def test_background_save_failure_after_register_unwinds_registries(monkeypatch):
    """#7680 finding 3 (SILENT) regression: ``_handle_background`` used
    to do ``bg.save()`` after registering the writeback owner but before
    the launch-failure try/except. A throw on that save orphaned the
    writeback owner. The fix moves the save into the same try block as
    the rest of the launch.
    """
    parent = _make_session("bg-savefail-parent")
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
    monkeypatch.setattr(
        routes, "get_session",
        lambda sid, metadata_only=False: models.SESSIONS.get(sid) or parent,
    )
    monkeypatch.setattr(routes, "bad", lambda h, m, status=400: {"status": status})
    monkeypatch.setattr(
        routes, "j", lambda h, payload, status=200: {"status": status, "payload": payload}
    )

    from api import models as _models

    class _BoomBgSession(_models.Session):
        save_call_count = 0

        def save(self, *a, **kw):
            type(self).save_call_count += 1
            if type(self).save_call_count == 1:
                # The first save (creating the hidden session) succeeds.
                return super().save(*a, **kw)
            raise RuntimeError("bg save failed (disk full)")

    _BoomBgSession.save_call_count = 0
    monkeypatch.setattr(_models, "new_session", _BoomBgSession)

    with pytest.raises(RuntimeError, match="bg save failed"):
        routes._handle_background(
            object(),
            {"session_id": "bg-savefail-parent", "prompt": "do a thing"},
        )

    # The fix: the second bg.save() (after writeback-owner register) is
    # now inside the try block, so the abort unwinds the owner.
    assert config.SESSION_WRITEBACK_OWNERS == {}, (
        "bg save failure after writeback-owner register leaked the owner "
        f"(owners: {dict(config.SESSION_WRITEBACK_OWNERS)!r})"
    )
    assert config.STREAMS == {}
    assert config.STREAM_SESSION_OWNERS == {}
    for bg_session in models.SESSIONS.values():
        assert getattr(bg_session, "active_stream_id", None) is None

    # The tracked task is not stranded in "running" — track_background
    # was never reached (the save() throw happened before it), so there
    # are no tasks for this parent. The point is that the abort didn't
    # leave any side-effect state behind.
    import api.background as background

    tasks = background.get_background_tasks("bg-savefail-parent")
    assert tasks == [], (
        f"unexpected tracked tasks after save failure: {tasks!r}"
    )


def test_background_thread_construct_failure_unwinds_registries(monkeypatch):
    """#7680 finding 3 (SILENT) regression: ``_handle_background`` used to
    construct ``threading.Thread(...)`` OUTSIDE the launch-failure
    try/except block. A throw at Thread construction (extremely rare in
    practice but reachable under resource exhaustion) leaked the
    writeback owner. The fix moves the constructor into the same try
    block as the rest of the launch.
    """
    parent = _make_session("bg-threadfail-parent")
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **kw: None)
    monkeypatch.setattr(
        routes, "get_session",
        lambda sid, metadata_only=False: models.SESSIONS.get(sid) or parent,
    )
    monkeypatch.setattr(routes, "bad", lambda h, m, status=400: {"status": status})
    monkeypatch.setattr(
        routes, "j", lambda h, payload, status=200: {"status": status, "payload": payload}
    )

    class _ThrowingThreadCtor:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("thread construction failed (resource exhaustion)")

    monkeypatch.setattr(routes.threading, "Thread", _ThrowingThreadCtor)

    with pytest.raises(RuntimeError, match="thread construction failed"):
        routes._handle_background(
            object(),
            {"session_id": "bg-threadfail-parent", "prompt": "do a thing"},
        )

    # The fix: thread construction is inside the same try block, so
    # the launch-abort helper unwinds the owner.
    assert config.SESSION_WRITEBACK_OWNERS == {}, (
        "thread construction failure leaked the writeback owner "
        "(owners: "
        f"{dict(config.SESSION_WRITEBACK_OWNERS)!r})"
    )
    assert config.STREAMS == {}
    assert config.STREAM_SESSION_OWNERS == {}
    for bg_session in models.SESSIONS.values():
        assert getattr(bg_session, "active_stream_id", None) is None

    # The tracked task must not be stranded in "running" either.
    import api.background as background

    tasks = background.get_background_tasks("bg-threadfail-parent")
    assert tasks, "aborted background task must still be tracked"
    assert all(t["status"] != "running" for t in tasks)


# ── #7680 finding 3: the requeued wakeup must have a delivery path ──────────
# Recording the deferred wakeup (finding 1) fixes the LOSS half; nothing
# delivers it, because drain_deferred_wakeups_for_session's only caller is
# the turn-teardown hook and a worker that never started has no teardown.
# The fix schedules ONE bounded daemon-timer retry that calls the drain
# once the session is idle. These tests pin the schedule + exactly-once
# behavior with a fake timer (no real sleeps).


class _FakeTimer:
    """Stand-in for threading.Timer: records the delay, fires on demand."""

    instances: list = []

    def __init__(self, delay, fn, args=()):
        self.delay = delay
        self.fn = fn
        self.args = args
        self.started = False
        self.cancelled = False
        self.daemon = False
        _FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def is_alive(self):
        return self.started and not self.cancelled


@pytest.fixture
def _fake_timer(monkeypatch):
    _FakeTimer.instances = []
    monkeypatch.setattr(routes.threading, "Timer", _FakeTimer)
    yield _FakeTimer.instances
    # Sweep any pending handles so a late fire can't touch shared state.
    for t in list(routes._DEFERRED_WAKEUP_RETRY_TIMERS.values()):
        try:
            t.cancel()
        except Exception:
            pass
    routes._DEFERRED_WAKEUP_RETRY_TIMERS.clear()


def test_launch_abort_schedules_bounded_wakeup_retry(monkeypatch, _fake_timer):
    """#7680 finding 3: a launch abort on a process-wakeup path must schedule
    exactly one bounded drain retry — the deferred prompt has a delivery
    path even though the worker never started (no teardown will fire)."""
    s = _make_session("wakeup-retry")
    drain_calls = []

    import api.background_process as bp

    monkeypatch.setattr(
        bp, "drain_deferred_wakeups_for_session",
        lambda sid: drain_calls.append(sid) or 0,
    )
    # Not busy: the retry drain must actually be able to fire.
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda sid: False)

    routes._rearm_process_wakeup_after_launch_failure(
        s,
        "wakeup-retry",
        "stream-wakeup-retry",
        pending_user_message="[bg complete] task_id=proc-1 result=done",
    )

    assert len(_fake_timer) == 1, (
        f"launch abort must schedule exactly one bounded retry, got {len(_fake_timer)}"
    )
    timer = _fake_timer[0]
    assert timer.started, "the retry timer must be started"
    assert timer.daemon, "the retry timer must not block interpreter exit"
    assert 0 < timer.delay <= 30, f"retry delay must be short, got {timer.delay}"

    # The prompt is recorded AND the retry is registered on the module map.
    entries = bp.claim_deferred_wakeups("wakeup-retry")
    assert len(entries) == 1, "the wakeup prompt must stay queued for the retry"
    assert "proc-1" in str(entries[0].get("wakeup_prompt") or "")

    # A second abort while the first retry is still pending coalesces.
    routes._rearm_process_wakeup_after_launch_failure(
        s,
        "wakeup-retry",
        "stream-wakeup-retry",
        pending_user_message="[bg complete] task_id=proc-1 result=done",
    )
    assert len(_fake_timer) == 1, "a pending retry must not be duplicated"


def test_wakeup_retry_drains_once_and_does_not_loop(monkeypatch, _fake_timer):
    """#7680 finding 3: when the timer fires, it drains exactly once and drops
    its handle — if the retry's own launch fails, the drain re-defers the
    prompt but nothing reschedules, so there is no retry loop."""
    s = _make_session("wakeup-retry-fire")
    drain_calls = []

    import api.background_process as bp

    monkeypatch.setattr(
        bp, "drain_deferred_wakeups_for_session",
        lambda sid: drain_calls.append(sid) or 0,
    )

    routes._rearm_process_wakeup_after_launch_failure(
        s,
        "wakeup-retry-fire",
        "stream-wakeup-retry-fire",
        pending_user_message="[bg complete] task_id=proc-2 result=done",
    )
    timer = _fake_timer[0]

    # Fire the timer body directly (the fake timer only records).
    timer.fn(*timer.args)

    assert drain_calls == ["wakeup-retry-fire"], (
        f"the retry must drain exactly once, got {drain_calls!r}"
    )
    # The handle is dropped: a later fire cannot re-enter the drain.
    assert "wakeup-retry-fire" not in routes._DEFERRED_WAKEUP_RETRY_TIMERS


# ── #7680 finding 3 (maintainer's required regression tests) ────────────────
# The maintainer asked for BEHAVIORAL proof, not just schedule inspection:
#   1) inject a worker-start failure on a process-wakeup launch, send NO
#      further user turn, assert exactly one wakeup turn runs within the
#      retry window.
#   2) make the retry's launch fail too, and assert the prompt stays queued
#      with no infinite retry and no second wakeup turn.
#
# Both drive the REAL retry path end-to-end: a real daemon timer (patched to
# fire instantly) + the real `drain_deferred_wakeups_for_session` claiming a
# real `DEFERRED_PROCESS_WAKEUPS` entry. Only `start_session_turn` is faked,
# exactly like tests/test_wakeup_defer_race.py does.


class _InstantTimer:
    """threading.Timer stand-in that fires its body immediately on start()."""

    instances: list = []

    def __init__(self, delay, fn, args=(), kwargs=None):
        self.delay = delay
        self.fn = fn
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = False
        self.cancelled = False
        self.fired = False
        _InstantTimer.instances.append(self)

    def start(self):
        self.fired = True
        # Real timers run the body on a separate thread; do the same so the
        # drain's own daemon thread can race the assertions the way it does
        # in production (the tests wait on holder["event"] afterwards).
        threading.Thread(target=self.fn, args=self.args, kwargs=self.kwargs,
                         daemon=True).start()

    def cancel(self):
        self.cancelled = True

    def is_alive(self):
        return self.fired and not self.cancelled


def _install_wakeup_launch(monkeypatch, sid, *, fail_launch):
    """Patch ``api.routes.start_session_turn`` so a wakeup launch can fail.

    The drain helper resolves ``start_session_turn`` from ``api.routes``
    inside its daemon thread, so patching the attribute there is what that
    thread picks up at call time.

    Returns the holder recording every attempted wakeup turn.
    """
    holder = {"calls": [], "event": threading.Event()}

    def _fake_start_session_turn(session_id, message, *, source="process_wakeup"):
        holder["calls"].append(
            {"session_id": session_id, "message": message, "source": source}
        )
        holder["event"].set()
        if fail_launch is not None and fail_launch():
            # A worker-start failure surfaces as a raised exception OR a 5xx
            # response; the drain's `_start_server_side_wakeup_turn` runner
            # catches the raise and only re-defers on a 409, so model the
            # hard-fail as a raise to prove the retry leaves the prompt
            # queued rather than looping.
            raise RuntimeError("worker start failed")
        return {"stream_id": "wakeup-stream", "session_id": session_id, "_status": 200}

    monkeypatch.setattr(routes, "start_session_turn", _fake_start_session_turn)
    return holder


def test_abort_retry_delivers_one_wakeup_turn_with_no_user_turn(monkeypatch):
    """Required regression test 1: worker start fails on a process-wakeup
    launch → NO further user turn → exactly one wakeup turn runs.

    The session must be genuinely idle (no ACTIVE_RUNS, no STREAMS) so the
    scheduled retry's drain can actually start the wakeup turn. The retry
    timer is patched to fire instantly, so the wait is bounded and the
    test never sleeps on a real 2s clock.
    """
    from api import background_process as bp
    from api import config as _cfg

    sid = "wakeup-e2e-deliver"
    _reset_cfg_state()
    holder = _install_wakeup_launch(monkeypatch, sid, fail_launch=None)

    # Instant retry + an idle session: the retry must be able to fire.
    _InstantTimer.instances = []
    monkeypatch.setattr(routes.threading, "Timer", _InstantTimer)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)

    s = _make_session(sid)
    s.pending_user_message = (
        "[IMPORTANT: Background process proc-e2e-1 completed (exit_code=0).\n"
        "Command: sleep 1\n"
        "Output:\ndone]"
    )
    s.active_stream_id = "stream-e2e-deliver"

    try:
        # The launch abort: the worker never started, the prompt was recorded
        # as deferred, and the retry timer was scheduled (and fired).
        routes._cleanup_chat_start_launch_failure(
            s,
            "stream-e2e-deliver",
            reset_session=True,
            lock_held=False,
            preserve_wakeup=True,
        )

        assert _InstantTimer.instances, (
            "launch abort must schedule a bounded wakeup retry (#7680 finding 3)"
        )

        # Exactly one wakeup turn runs, with NO further user turn.
        assert holder["event"].wait(timeout=3.0), (
            "no wakeup turn ran within the retry window — the deferred "
            "wakeup is still undelivered (the 9/24 maintainer finding)"
        )
        # Give any spurious second delivery a chance to (correctly) not happen.
        import time as _t

        _t.sleep(0.25)
        assert len(holder["calls"]) == 1, (
            f"expected exactly ONE wakeup turn, got {len(holder['calls'])}: "
            f"{holder['calls']!r} — the retry double-delivered"
        )
        call = holder["calls"][0]
        assert call["session_id"] == sid
        assert call["source"] == "process_wakeup"
        assert "proc-e2e-1" in call["message"]

        # The claimed prompt is gone: nothing left for another turn.
        assert bp.drain_deferred_wakeups_for_session(sid) == 0
        assert len(holder["calls"]) == 1, (
            "a subsequent drain fired a second wakeup turn — not exactly-once"
        )
        with _cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
            assert sid not in _cfg.DEFERRED_PROCESS_WAKEUPS
    finally:
        with routes._DEFERRED_WAKEUP_RETRY_TIMERS_LOCK:
            routes._DEFERRED_WAKEUP_RETRY_TIMERS.pop(sid, None)
        _reset_cfg_state()


def test_abort_retry_failure_keeps_prompt_queued_and_does_not_loop(monkeypatch):
    """Required regression test 2: the retry's own wakeup launch ALSO fails.

    The prompt must STAY queued (so a later real turn or teardown can still
    deliver it), the retry must not reschedule (no infinite loop), and no
    second wakeup turn may run.
    """
    from api import background_process as bp
    from api import config as _cfg

    sid = "wakeup-e2e-fail1"
    _reset_cfg_state()

    launch_attempts = {"n": 0}

    def _fail_launch():
        launch_attempts["n"] += 1
        return True  # every launch attempt fails

    holder = _install_wakeup_launch(monkeypatch, sid, fail_launch=_fail_launch)

    _InstantTimer.instances = []
    monkeypatch.setattr(routes.threading, "Timer", _InstantTimer)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)

    s = _make_session(sid)
    s.pending_user_message = (
        "[IMPORTANT: Background process proc-e2e-2 completed (exit_code=1).\n"
        "Command: ls /nope\n"
        "Output:\nfailed]"
    )
    s.active_stream_id = "stream-e2e-fail1"

    try:
        routes._cleanup_chat_start_launch_failure(
            s,
            "stream-e2e-fail1",
            reset_session=True,
            lock_held=False,
            preserve_wakeup=True,
        )

        assert _InstantTimer.instances, "launch abort must schedule one retry"
        assert holder["event"].wait(timeout=3.0), (
            "the retry never attempted the wakeup launch"
        )

        # The retry's launch failed. Assert the two maintainer requirements:
        #  (a) the prompt is still queued for a later delivery, and
        #  (b) nothing rescheduled / looped.
        def _requeued():
            with _cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
                return bool(_cfg.DEFERRED_PROCESS_WAKEUPS.get(sid))

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not _requeued():
            time.sleep(0.02)

        assert _requeued(), (
            "the failed retry DROPPED the prompt — it must stay queued so a "
            "later real turn / teardown can still deliver it (#7680 finding 3)"
        )
        with _cfg.DEFERRED_PROCESS_WAKEUPS_LOCK:
            entries = list(_cfg.DEFERRED_PROCESS_WAKEUPS.get(sid) or [])
        assert len(entries) == 1, (
            f"expected exactly one requeued prompt, got {len(entries)} — the "
            "retry duplicated the deferred entry"
        )
        assert "proc-e2e-2" in str(entries[0].get("wakeup_prompt") or "")

        # No loop: no second timer, no second launch attempt beyond the one
        # bounded retry (plus nothing further once its handle was dropped).
        import time as _t

        _t.sleep(0.3)
        assert launch_attempts["n"] == 1, (
            f"expected exactly ONE launch attempt, got {launch_attempts['n']} — "
            "the retry looped after its own failure"
        )
        assert len(_InstantTimer.instances) == 1, (
            "the failed retry scheduled another timer — unbounded retry loop"
        )
        with routes._DEFERRED_WAKEUP_RETRY_TIMERS_LOCK:
            assert sid not in routes._DEFERRED_WAKEUP_RETRY_TIMERS, (
                "the fired retry kept its timer handle — it can re-enter the "
                "drain and loop"
            )
    finally:
        with routes._DEFERRED_WAKEUP_RETRY_TIMERS_LOCK:
            routes._DEFERRED_WAKEUP_RETRY_TIMERS.pop(sid, None)
        _reset_cfg_state()
