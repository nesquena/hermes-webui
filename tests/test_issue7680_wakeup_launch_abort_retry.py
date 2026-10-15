"""Regression tests for #7680: the launch-abort wakeup retry must be OPT-IN and
BOUNDED (maintainer CORE finding, 2026-10-01).

The maintainer's finding on the previous revision: the launch-abort re-arm
keyed on ``source == "process_wakeup"``, but TWO independent delivery paths
start a turn with that source:

  1. the process-completion path (``_start_server_side_wakeup_turn``) — has NO
     other retry, so a launch abort MUST re-arm it or the completion is lost;
  2. the async-delegation completion path
     (``_start_server_side_async_delegation``) — already owns a DURABLE
     claim/retry (``release_async_delegation_delivery(..., retryable=True)`` →
     ``_retry_unclaimed_async_delegation_event``).

Re-arming for both delivered ONE completion through TWO independent retry
paths (durable retry + deferred prompt + timer). The fix makes the re-arm an
explicit opt-in flag (``rearm_deferred_wakeup=True``) passed ONLY by the
process-completion path.

The retry is also BOUNDED: the first abort (``retry_attempt=0``) schedules
exactly one timer; a launch failure on that retry (``retry_attempt=1``) keeps
the prompt queued but never schedules again, so a persistent launch failure
cannot spin a new timer every ``_DEFERRED_WAKEUP_RETRY_DELAY_SECS`` forever.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import api.background_process as bp
import api.config as config
import api.routes as routes
import api.turn_journal as turn_journal


# --------------------------------------------------------------------------
# Shared state / fakes
# --------------------------------------------------------------------------


def _reset_wakeup_state() -> None:
    """Clear the process-wakeup shared state so exactly-once assertions hold."""
    config.PENDING_BG_TASK_COMPLETIONS.clear()
    with config.DEFERRED_PROCESS_WAKEUPS_LOCK:
        config.DEFERRED_PROCESS_WAKEUPS.clear()
    with routes._DEFERRED_WAKEUP_RETRY_TIMERS_LOCK:
        routes._DEFERRED_WAKEUP_RETRY_TIMERS.clear()


class _FakeTimer:
    """Stand-in for ``threading.Timer``: records the delay, fires on demand."""

    instances: list = []

    def __init__(self, delay, fn, args=(), kwargs=None):
        self.delay = delay
        self.fn = fn
        self.args = args
        self.kwargs = kwargs or {}
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
def fake_timer(monkeypatch):
    _FakeTimer.instances = []
    monkeypatch.setattr(routes.threading, "Timer", _FakeTimer)
    yield _FakeTimer.instances
    for timer in list(routes._DEFERRED_WAKEUP_RETRY_TIMERS.values()):
        try:
            timer.cancel()
        except Exception:
            pass
    routes._DEFERRED_WAKEUP_RETRY_TIMERS.clear()


class _SyncThread:
    """Thread stub that runs its target synchronously inside ``start()``."""

    def __init__(self, target=None, args=(), kwargs=None, **extra):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.daemon = bool(extra.get("daemon", False))

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def _make_session(session_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        title="Existing",
        active_stream_id=None,
        pending_user_message=None,
        pending_attachments=[],
        pending_started_at=None,
        pending_user_source=None,
        messages=[],
        workspace="/tmp",
        model="old-model",
        model_provider=None,
        worktree_path=None,
        profile=None,
        save=lambda *a, **kw: None,
    )


def _install_launch_abort(monkeypatch, sid: str):
    """Drive ``_start_chat_stream_for_session`` through the REAL worker-start
    abort: register state exactly as the prepare step does, then make the
    worker thread's ``start()`` raise."""
    holder = {"attempts": 0}

    class _FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            holder["attempts"] += 1
            raise RuntimeError("thread launch failed (simulated)")

    session = _make_session(sid)
    canonical = {sid: session}

    def fake_prepare(s, *, stream_id, **kwargs):
        s.active_stream_id = stream_id
        s.pending_user_message = kwargs["msg"]
        s.pending_started_at = 1.0
        config.register_session_writeback_owner(s.session_id, stream_id)
        config.register_stream_owner(stream_id, s.session_id)
        config.STREAMS[stream_id] = object()

    monkeypatch.setattr(routes.threading, "Thread", _FailingThread)
    monkeypatch.setattr(
        routes, "_prepare_chat_start_session_for_stream", fake_prepare
    )
    monkeypatch.setattr(routes, "set_last_workspace", lambda *a, **k: None)
    monkeypatch.setattr(routes, "_is_hidden_empty_session", lambda s: False)
    monkeypatch.setattr(
        routes, "get_session", lambda sid_, metadata_only=False: canonical[sid_]
    )
    monkeypatch.setattr(
        routes, "_get_session_agent_lock", lambda sid_: threading.RLock()
    )
    monkeypatch.setattr(
        routes, "_active_stream_blocks_chat_start", lambda *a, **k: False
    )
    monkeypatch.setattr(
        routes, "_active_run_stream_for_session", lambda sid_: None
    )
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *a, **k: None)
    monkeypatch.setattr(
        turn_journal, "append_turn_journal_event", lambda *a, **k: {}
    )
    return session, holder


# --------------------------------------------------------------------------
# CORE: the re-arm must be OPT-IN (maintainer's required regression)
# --------------------------------------------------------------------------


def test_delegation_launch_failure_does_not_rearm(monkeypatch, fake_timer):
    """A launch failure on the async-delegation delivery path must schedule
    NO deferred prompt and NO retry timer.

    ``_start_server_side_async_delegation`` starts its turn with
    ``source="process_wakeup"`` but does NOT opt into the re-arm — it already
    owns a durable claim/retry. Keying the re-arm on the source alone made one
    completion eligible for the durable retry AND a deferred prompt AND a
    timer (#7680 CORE).
    """
    sid = "wakeup-delegation-abort"
    _reset_wakeup_state()
    session, holder = _install_launch_abort(monkeypatch, sid)
    prompt = (
        "[IMPORTANT: Background process proc-del completed (exit_code=0).\n"
        "Command: true\nOutput:\ndone]"
    )

    with pytest.raises(RuntimeError, match="thread launch failed"):
        routes._start_chat_stream_for_session(
            session,
            msg=prompt,
            workspace="/tmp",
            model="m",
            model_provider="p",
            source="process_wakeup",
            process_id="proc-del",
            # rearm_deferred_wakeup deliberately omitted — exactly what
            # _start_server_side_async_delegation passes.
        )

    assert holder["attempts"] == 1, "the worker launch must have been attempted"
    queued = config.DEFERRED_PROCESS_WAKEUPS.get(sid)
    assert not queued, (
        "the delegation path owns a durable retry; re-arming here delivered "
        f"one completion twice (#7680 CORE). queued={queued!r}"
    )
    assert _FakeTimer.instances == [], "no retry timer may be scheduled"
    assert sid not in routes._DEFERRED_WAKEUP_RETRY_TIMERS


def test_process_completion_launch_failure_rearms_one_bounded_retry(
    monkeypatch, fake_timer
):
    """The process-completion path DOES opt in: a launch abort queues the
    prompt and schedules exactly one bounded retry — it has no other retry, so
    without this the completion would be lost."""
    sid = "wakeup-completion-abort"
    _reset_wakeup_state()
    session, holder = _install_launch_abort(monkeypatch, sid)
    prompt = (
        "[IMPORTANT: Background process proc-c completed (exit_code=0).\n"
        "Command: true\nOutput:\ndone]"
    )

    with pytest.raises(RuntimeError, match="thread launch failed"):
        routes._start_chat_stream_for_session(
            session,
            msg=prompt,
            workspace="/tmp",
            model="m",
            model_provider="p",
            source="process_wakeup",
            process_id="proc-c",
            retry_attempt=0,
            rearm_deferred_wakeup=True,
        )

    assert holder["attempts"] == 1
    with config.DEFERRED_PROCESS_WAKEUPS_LOCK:
        entries = list(config.DEFERRED_PROCESS_WAKEUPS.get(sid) or [])
    assert len(entries) == 1, "the aborted wakeup prompt must stay queued"
    assert entries[0]["process_id"] == "proc-c"
    assert "proc-c" in entries[0]["wakeup_prompt"]

    assert len(_FakeTimer.instances) == 1, (
        f"exactly one bounded retry must be scheduled, got {len(_FakeTimer.instances)}"
    )
    timer = _FakeTimer.instances[0]
    assert timer.started, "the retry timer must be started"
    assert timer.daemon, "the retry timer must not block interpreter exit"
    assert 0 < timer.delay <= 30, f"retry delay must be short, got {timer.delay}"
    assert sid in routes._DEFERRED_WAKEUP_RETRY_TIMERS

    # A second abort while the first retry is still pending coalesces.
    session.active_stream_id = None
    with pytest.raises(RuntimeError, match="thread launch failed"):
        routes._start_chat_stream_for_session(
            session,
            msg=prompt,
            workspace="/tmp",
            model="m",
            model_provider="p",
            source="process_wakeup",
            process_id="proc-c",
            retry_attempt=0,
            rearm_deferred_wakeup=True,
        )
    assert len(_FakeTimer.instances) == 1, "a pending retry must not be duplicated"
    with config.DEFERRED_PROCESS_WAKEUPS_LOCK:
        entries = list(config.DEFERRED_PROCESS_WAKEUPS.get(sid) or [])
    assert len(entries) == 1, "the duplicate abort must not double-queue the prompt"


# --------------------------------------------------------------------------
# CORE: the retry must be BOUNDED (no infinite 2s loop)
# --------------------------------------------------------------------------


def test_retry_attempt_marker_does_not_reschedule(monkeypatch, fake_timer):
    """A launch failure on the retry's OWN pass (``retry_attempt=1``) must keep
    the prompt queued and schedule NO further timer.

    The retry timer pops its own handle before draining, so the registry
    coalesce guard cannot stop a reschedule — the attempt marker must be
    load-bearing, or a persistent launch failure spins a new timer every
    ``_DEFERRED_WAKEUP_RETRY_DELAY_SECS`` forever.
    """
    sid = "wakeup-retry-marker"
    _reset_wakeup_state()
    session, holder = _install_launch_abort(monkeypatch, sid)
    prompt = (
        "[IMPORTANT: Background process proc-c completed (exit_code=1).\n"
        "Command: false\nOutput:\nfailed]"
    )

    with pytest.raises(RuntimeError, match="thread launch failed"):
        routes._start_chat_stream_for_session(
            session,
            msg=prompt,
            workspace="/tmp",
            model="m",
            model_provider="p",
            source="process_wakeup",
            process_id="proc-c",
            retry_attempt=1,
            rearm_deferred_wakeup=True,
        )

    with config.DEFERRED_PROCESS_WAKEUPS_LOCK:
        entries = list(config.DEFERRED_PROCESS_WAKEUPS.get(sid) or [])
    assert len(entries) == 1, "the failed retry must keep the prompt queued"
    assert _FakeTimer.instances == [], (
        "no further timer may be scheduled after the retry's own failure "
        "(#7680 CORE: the bounded retry looped)"
    )
    assert sid not in routes._DEFERRED_WAKEUP_RETRY_TIMERS


def test_retry_timer_drains_once_as_attempt_1(monkeypatch, fake_timer):
    """The timer fires the drain exactly once, carrying the attempt marker,
    and drops its own handle so a later fire cannot re-enter."""
    sid = "wakeup-retry-fire"
    _reset_wakeup_state()
    drain_calls = []
    monkeypatch.setattr(
        bp,
        "drain_deferred_wakeups_for_session",
        lambda sid_, **kw: drain_calls.append(
            (sid_, int(kw.get("retry_attempt") or 0))
        )
        or 0,
    )

    routes._rearm_process_wakeup_after_launch_failure(
        sid,
        "stream-wakeup-retry-fire",
        wakeup_prompt="[bg complete] task_id=proc-2 result=done",
        process_id="proc-2",
    )
    assert len(_FakeTimer.instances) == 1
    timer = _FakeTimer.instances[0]

    # Fire the timer body directly — the fake only records.
    timer.fn(*timer.args)

    assert drain_calls == [(sid, 1)], (
        f"the retry must drain exactly once as an attempt-1 delivery, got {drain_calls!r}"
    )
    assert sid not in routes._DEFERRED_WAKEUP_RETRY_TIMERS, (
        "the fired retry must drop its handle or it can re-enter the drain"
    )


def test_timer_start_failure_releases_registry_entry(monkeypatch):
    """A ``Timer.start()`` failure must not leak its registry entry: the entry
    is installed before ``start()`` (outside the lock), and the pop-on-fire
    callback never runs if ``start()`` raised. A leaked entry is a session that
    can never schedule a retry again."""
    sid = "wakeup-timer-start-fail"
    _reset_wakeup_state()

    class _StartFailsTimer:
        def __init__(self, delay, fn, args=(), kwargs=None):
            self.daemon = False

        def start(self):
            raise RuntimeError("can't start new thread")

        def cancel(self):
            pass

        def is_alive(self):
            return False

    monkeypatch.setattr(routes.threading, "Timer", _StartFailsTimer)

    # Must not raise: _schedule_deferred_wakeup_retry swallows and logs.
    routes._schedule_deferred_wakeup_retry(sid)

    assert sid not in routes._DEFERRED_WAKEUP_RETRY_TIMERS, (
        "a failed start() left a dead timer handle in the registry"
    )


# --------------------------------------------------------------------------
# Delivery-path hardening: deleted session (404) and launch 5xx
# --------------------------------------------------------------------------


def test_deleted_session_wakeup_discards_queued_state(monkeypatch):
    """404 session-not-found is TERMINAL: queued wakeup state for the removed
    session is cleared and NOT re-queued."""
    sid = "wakeup-deleted-session"
    _reset_wakeup_state()
    bp.record_deferred_wakeup(sid, "proc-x", "prompt-x")
    config.PENDING_BG_TASK_COMPLETIONS.add(sid)
    assert config.DEFERRED_PROCESS_WAKEUPS.get(sid)

    monkeypatch.setattr(bp.threading, "Thread", _SyncThread)
    monkeypatch.setattr(
        routes,
        "start_session_turn",
        lambda *a, **k: {"_status": 404, "error": "Session not found"},
    )

    bp._start_server_side_wakeup_turn(sid, "prompt-x", process_id="proc-x")

    assert not config.DEFERRED_PROCESS_WAKEUPS.get(sid), (
        "a deleted session must not retain an unexpiring queued prompt"
    )
    assert sid not in config.PENDING_BG_TASK_COMPLETIONS
    assert sid not in routes._DEFERRED_WAKEUP_RETRY_TIMERS


def test_wakeup_launch_5xx_keeps_prompt_queued(monkeypatch):
    """A 5xx launch keeps the prompt queued for a later delivery — the entry
    was already claimed by the drain, so dropping it here loses the wakeup
    permanently."""
    sid = "wakeup-5xx"
    _reset_wakeup_state()
    monkeypatch.setattr(bp.threading, "Thread", _SyncThread)
    monkeypatch.setattr(
        routes,
        "start_session_turn",
        lambda *a, **k: {"_status": 500, "error": "launch blew up"},
    )

    bp._start_server_side_wakeup_turn(sid, "prompt-5xx", process_id="proc-5xx")

    with config.DEFERRED_PROCESS_WAKEUPS_LOCK:
        entries = list(config.DEFERRED_PROCESS_WAKEUPS.get(sid) or [])
    assert len(entries) == 1, "a 5xx launch must keep the wakeup queued"
    assert entries[0]["process_id"] == "proc-5xx"


def test_wakeup_launch_raised_keeps_prompt_queued(monkeypatch):
    """A launch that RAISES is the same loss case as a 5xx."""
    sid = "wakeup-raised"
    _reset_wakeup_state()

    def _boom(*a, **k):
        raise RuntimeError("session load exploded")

    monkeypatch.setattr(bp.threading, "Thread", _SyncThread)
    monkeypatch.setattr(routes, "start_session_turn", _boom)

    bp._start_server_side_wakeup_turn(sid, "prompt-raised", process_id="proc-r")

    with config.DEFERRED_PROCESS_WAKEUPS_LOCK:
        entries = list(config.DEFERRED_PROCESS_WAKEUPS.get(sid) or [])
    assert len(entries) == 1, "a raised launch must keep the wakeup queued"
    assert entries[0]["process_id"] == "proc-r"


# --------------------------------------------------------------------------
# record_deferred_wakeup: empty process_id dedupes on prompt text
# --------------------------------------------------------------------------


def test_record_deferred_wakeup_empty_id_dedupes_on_prompt():
    """An EMPTY ``process_id`` is not "no identity" — it is what a multi-line
    heredoc command's display text parses to, and callers that must still dedup
    (the launch-abort re-arm) yield it. Such entries key on the prompt text:
    an exact duplicate collapses, two different id-less wakeups both survive.
    """
    _reset_wakeup_state()
    sid = "wakeup-empty-id"

    assert bp.record_deferred_wakeup(sid, "", "same prompt") is True
    assert bp.record_deferred_wakeup(sid, "", "same prompt") is True
    with config.DEFERRED_PROCESS_WAKEUPS_LOCK:
        assert len(config.DEFERRED_PROCESS_WAKEUPS[sid]) == 1, (
            "an exact duplicate with an empty id must collapse"
        )
    bp.claim_deferred_wakeups(sid)

    bp.record_deferred_wakeup(sid, "", "prompt A")
    bp.record_deferred_wakeup(sid, "", "prompt B")
    with config.DEFERRED_PROCESS_WAKEUPS_LOCK:
        assert len(config.DEFERRED_PROCESS_WAKEUPS[sid]) == 2, (
            "two DIFFERENT id-less wakeups must both survive"
        )
    bp.claim_deferred_wakeups(sid)


def test_record_deferred_wakeup_nonempty_id_dedupes_regardless_of_prompt():
    """A non-empty ``process_id`` is the authoritative immutable identity, so
    the same id collapses even if the prompt text differs (display formatting
    drift must not double-deliver one completion)."""
    _reset_wakeup_state()
    sid = "wakeup-nonempty-id"

    bp.record_deferred_wakeup(sid, "proc-1", "first rendering")
    bp.record_deferred_wakeup(sid, "proc-1", "second rendering")
    with config.DEFERRED_PROCESS_WAKEUPS_LOCK:
        assert len(config.DEFERRED_PROCESS_WAKEUPS[sid]) == 1
    bp.claim_deferred_wakeups(sid)

# --------------------------------------------------------------------------
# /btw + /background: the post-registration launch steps must unwind
# (maintainer's 2026-09-24 review: "resolved" — kept alive on the rework)
# --------------------------------------------------------------------------


def _btw_fixtures(monkeypatch, parent_sid):
    """Common stubs so a /btw or /background launch can run without a server."""
    import api.models as models

    models.SESSIONS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()
    config.STREAM_SESSION_OWNERS.clear()
    with config.STREAMS_LOCK:
        config.STREAMS.clear()

    parent = _make_session(parent_sid)
    models.SESSIONS[parent_sid] = parent

    monkeypatch.setattr(
        routes, "_agent_runtime_barrier_response", lambda **kw: None
    )
    monkeypatch.setattr(
        routes, "_session_is_subagent_view_only", lambda *a, **kw: False
    )
    # sid-aware resolver: handlers look up the parent, and the cleanup helper
    # re-resolves the ephemeral/bg session canonically.
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: models.SESSIONS.get(sid) or parent,
    )
    monkeypatch.setattr(routes, "bad", lambda h, m, status=400: {"status": status})
    monkeypatch.setattr(
        routes,
        "j",
        lambda h, payload, status=200: {"status": status, "payload": payload},
    )
    return parent


def test_btw_thread_start_failure_unwinds_registries(monkeypatch):
    """A /btw thread-start failure must not leave the ephemeral session
    pointing at a dead stream (no abort cleanup existed before #6869)."""
    import api.models as models

    _reset_wakeup_state()
    _btw_fixtures(monkeypatch, "btw-parent")

    class _StubThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("btw thread start failed")

    monkeypatch.setattr(routes.threading, "Thread", _StubThread)

    with pytest.raises(RuntimeError, match="btw thread start failed"):
        routes._handle_btw(object(), {"session_id": "btw-parent", "question": "what?"})

    assert config.SESSION_WRITEBACK_OWNERS == {}
    assert config.STREAM_SESSION_OWNERS == {}
    with config.STREAMS_LOCK:
        assert config.STREAMS == {}
    for ephemeral in models.SESSIONS.values():
        assert getattr(ephemeral, "active_stream_id", None) is None


def test_btw_save_failure_after_register_unwinds_registries(monkeypatch):
    """An injected save failure on the SECOND /btw save (after the writeback
    owner was registered) must still unwind it."""
    import api.models as models

    _reset_wakeup_state()
    _btw_fixtures(monkeypatch, "btw-parent-save")

    saved = {"n": 0}
    real_save = models.Session.save

    # The /btw handler saves the ephemeral session twice: once for the title /
    # inherited context (before the writeback owner is registered) and once
    # after. The ephemeral id is a generated uuid, so key on call ordinal, not
    # on the id prefix. Only this test's ephemeral saves hit Session.save in
    # this path.
    def _flaky_save(self, *a, **kw):
        saved["n"] += 1
        if saved["n"] >= 2:
            raise OSError("disk full")
        return real_save(self, *a, **kw)

    monkeypatch.setattr(models.Session, "save", _flaky_save)

    with pytest.raises(OSError, match="disk full"):
        routes._handle_btw(
            object(), {"session_id": "btw-parent-save", "question": "what?"}
        )

    assert config.SESSION_WRITEBACK_OWNERS == {}, (
        "an injected save failure leaked the writeback owner"
    )
    assert config.STREAM_SESSION_OWNERS == {}
    with config.STREAMS_LOCK:
        assert config.STREAMS == {}


def test_background_thread_start_failure_unwinds_registries_and_fails_task(monkeypatch):
    """A /background thread-start failure must unwind the registries AND settle
    the tracked task — otherwise the frontend poll waits forever."""
    import api.background as background
    import api.models as models

    _reset_wakeup_state()
    _btw_fixtures(monkeypatch, "bg-parent")

    class _StubThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("bg thread start failed")

    monkeypatch.setattr(routes.threading, "Thread", _StubThread)

    with pytest.raises(RuntimeError, match="bg thread start failed"):
        routes._handle_background(
            object(), {"session_id": "bg-parent", "prompt": "do a thing"}
        )

    assert config.SESSION_WRITEBACK_OWNERS == {}
    assert config.STREAM_SESSION_OWNERS == {}
    with config.STREAMS_LOCK:
        assert config.STREAMS == {}
    for bg_session in models.SESSIONS.values():
        assert getattr(bg_session, "active_stream_id", None) is None

    tasks = background.get_background_tasks("bg-parent")
    assert tasks, "the aborted background task must still be tracked"
    assert all(t["status"] != "running" for t in tasks), (
        "an aborted background task stayed 'running' forever"
    )


def test_background_thread_construct_failure_unwinds_registries(monkeypatch):
    """A ``threading.Thread(...)`` CONSTRUCTION failure in /background must be
    covered too — the constructor runs inside the guarded block."""
    import api.models as models

    _reset_wakeup_state()
    _btw_fixtures(monkeypatch, "bg-parent-ctor")

    class _ThrowingThreadCtor:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("bg thread construction failed")

    monkeypatch.setattr(routes.threading, "Thread", _ThrowingThreadCtor)

    with pytest.raises(RuntimeError, match="bg thread construction failed"):
        routes._handle_background(
            object(), {"session_id": "bg-parent-ctor", "prompt": "do a thing"}
        )

    assert config.SESSION_WRITEBACK_OWNERS == {}
    assert config.STREAM_SESSION_OWNERS == {}
    with config.STREAMS_LOCK:
        assert config.STREAMS == {}
    for bg_session in models.SESSIONS.values():
        assert getattr(bg_session, "active_stream_id", None) is None

# --------------------------------------------------------------------------
# Source guard: the opt-in wiring itself must stay pinned
# --------------------------------------------------------------------------


def test_only_process_completion_path_opts_into_rearm():
    """Pin the wiring, not just the behaviour: the process-completion wakeup
    caller opts into the re-arm, and the async-delegation caller does NOT.

    ``_start_async_delegation_wakeup_turn`` starts its turn with the distinct
    ``source="delegation_wakeup"`` provenance (quiet-delegation: hidden rows),
    so a future refactor that re-derives the opt-in from ``source`` (or flips
    the flags) would silently reintroduce the double-retry the maintainer
    found. Assert on the call sites' source text.
    """
    import pathlib

    source = pathlib.Path(bp.__file__).read_text(encoding="utf-8")

    def _call_block(fn_name: str) -> str:
        start = source.index(f"def {fn_name}(")
        # The next top-level def marks the end of this function's body.
        end = source.find("\ndef ", start + 1)
        return source[start:end if end != -1 else len(source)]

    completion_block = _call_block("_start_server_side_wakeup_turn")
    delegation_block = _call_block("_start_async_delegation_wakeup_turn")

    assert "rearm_deferred_wakeup=True" in completion_block, (
        "the process-completion wakeup caller must opt into the launch-abort "
        "re-arm — it has no other retry, so without it the completion is lost"
    )
    assert "rearm_deferred_wakeup" not in delegation_block, (
        "the async-delegation caller owns a durable retry; opting it into the "
        "deferred-wakeup re-arm delivered one completion twice (#7680 CORE)"
    )

    # Each path keeps its OWN source provenance: the process-completion
    # caller stays ``process_wakeup``; the delegation caller uses the typed
    # ``delegation_wakeup`` stamp the hidden-row predicate keys on
    # (quiet-delegation #7882).
    assert 'source="process_wakeup"' in completion_block
    assert 'source="delegation_wakeup"' in delegation_block


def test_rearm_flag_defaults_to_off():
    """The re-arm must be inert by default: a launch failure on a plain
    (non-opted-in) turn schedules no deferred wakeup and no timer."""
    import inspect

    for fn in (
        routes._start_chat_stream_for_session,
        routes._start_run,
        routes.start_session_turn,
    ):
        sig = inspect.signature(fn)
        assert sig.parameters["rearm_deferred_wakeup"].default is False, (
            f"{fn.__name__} must default rearm_deferred_wakeup=False"
        )

def test_async_delegation_runner_passes_no_rearm_flag(monkeypatch):
    """Behaviour-level pin: the delegation runner's ACTUAL call into
    ``start_session_turn`` must not carry ``rearm_deferred_wakeup``.

    The source-text guard catches a literal wiring change; this one catches a
    refactor that builds the kwargs dynamically (e.g. spreading a shared dict
    that grew the opt-in), and it proves the runner still starts its turn with
    ``source="process_wakeup"`` — the exact collision that made keying the
    re-arm on the source alone wrong (#7680 CORE).
    """
    _reset_wakeup_state()
    seen: list = []

    def _fake_start(session_id, message, **kwargs):
        seen.append({"session_id": session_id, "message": message, **kwargs})
        # A transient refusal keeps the run on the release/retry branch, so no
        # durable-delivery fakes are needed for this wiring assertion.
        return {"_status": 409, "error": "busy", "active_stream_id": "stream-x"}

    monkeypatch.setattr(routes, "start_session_turn", _fake_start)
    monkeypatch.setattr(bp, "release_async_delegation_delivery", lambda *a, **k: None)
    monkeypatch.setattr(
        bp, "_retry_unclaimed_async_delegation_event", lambda *a, **k: None
    )
    monkeypatch.setattr(bp, "_record_async_delegation_accepted", lambda *a, **k: None)
    # Run the daemon runner synchronously so the assertion is deterministic.
    monkeypatch.setattr(bp.threading, "Thread", _SyncThread)

    try:
        bp._start_async_delegation_wakeup_turn(
            "deleg-session",
            "delegation wakeup prompt",
            delegation_id="deleg_x",
            evt={"delegation_id": "deleg_x", "session_key": "deleg-session"},
            claim="claim:x",
            process_registry=None,
        )
    finally:
        with bp._ASYNC_DELEGATION_WAKEUP_ADMISSION_LOCK:
            bp._ASYNC_DELEGATION_WAKEUP_ADMISSION_INFLIGHT.discard("deleg-session")

    assert len(seen) == 1, f"expected exactly one start_session_turn call, got {seen!r}"
    call = seen[0]
    # The delegation runner carries the distinct ``delegation_wakeup``
    # provenance (quiet-delegation #7882), not the shared process-wakeup one.
    assert call["source"] == "delegation_wakeup"
    assert "rearm_deferred_wakeup" not in call, (
        "the delegation runner must NOT opt into the launch-abort re-arm: it "
        "already owns a durable claim/retry, so the extra deferred prompt + "
        "timer delivered one completion twice (#7680 CORE). "
        f"kwargs passed: {sorted(call)!r}"
    )

class _SelectiveThread:
    """Thread stub for the full-chain delegation test.

    The delegation runner's thread (1st ``start()``) runs synchronously so the
    test is deterministic; the agent worker start (2nd) fails like a
    thread-exhausted launcher — a real worker-start failure through the REAL
    launch-abort path.
    """

    starts = 0

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **extra):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.daemon = bool(daemon)

    def start(self):
        _SelectiveThread.starts += 1
        if _SelectiveThread.starts >= 2:
            raise RuntimeError("thread launch failed (simulated)")
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def test_delegation_launch_failure_keeps_only_its_durable_retry(
    monkeypatch, fake_timer
):
    """Full chain, the maintainer's required regression, end to end.

    A DELEGATION wakeup whose worker fails to start must schedule ONLY its
    durable retry — no deferred prompt, no timer. Drives the real delegation
    runner (``_start_async_delegation_wakeup_turn``); its turn launch dies at
    the worker start through the REAL abort path.
    """
    sid = "wakeup-delegation-full-chain"
    _reset_wakeup_state()
    session, holder = _install_launch_abort(monkeypatch, sid)

    # Re-stub threading AFTER _install_launch_abort: the delegation runner
    # runs synchronously; the agent worker's start fails. (routes.threading
    # and bp.threading are the same module object, so one patch covers both.)
    _SelectiveThread.starts = 0
    monkeypatch.setattr(routes.threading, "Thread", _SelectiveThread)

    calls: list = []

    def _delegation_start_session_turn(
        session_id, message, *, source="process_wakeup", **kw
    ):
        calls.append({"session_id": session_id, "source": source, **kw})
        # Route into the REAL launch path with exactly what the delegation
        # caller passes in production: no rearm opt-in, distinct
        # delegation_wakeup provenance.
        return routes._start_chat_stream_for_session(
            session,
            msg=message,
            workspace="/tmp",
            model="m",
            model_provider="p",
            source=source,
        )

    monkeypatch.setattr(routes, "start_session_turn", _delegation_start_session_turn)

    released: list = []
    retried: list = []
    monkeypatch.setattr(
        bp,
        "release_async_delegation_delivery",
        lambda evt, claim, **kw: released.append(kw or {}),
    )
    monkeypatch.setattr(
        bp,
        "_retry_unclaimed_async_delegation_event",
        lambda *a, **k: retried.append(True),
    )

    try:
        bp._start_async_delegation_wakeup_turn(
            sid,
            "delegation result",
            delegation_id="deleg_full",
            evt={"delegation_id": "deleg_full", "session_key": sid},
            claim="claim:full",
            process_registry=None,
        )
    finally:
        with bp._ASYNC_DELEGATION_WAKEUP_ADMISSION_LOCK:
            bp._ASYNC_DELEGATION_WAKEUP_ADMISSION_INFLIGHT.discard(sid)

    # The launch really failed at the worker start, through the real abort path:
    assert _SelectiveThread.starts == 2, "runner + worker thread starts expected"
    # The delegation caller passed the distinct delegation_wakeup provenance
    # and no rearm opt-in (quiet-delegation #7882).
    assert calls and calls[0]["source"] == "delegation_wakeup"
    assert "rearm_deferred_wakeup" not in calls[0], (
        "the delegation runner must not opt into the launch-abort re-arm"
    )
    # The abort queued NO deferred prompt and scheduled NO timer:
    with config.DEFERRED_PROCESS_WAKEUPS_LOCK:
        assert not config.DEFERRED_PROCESS_WAKEUPS.get(sid), (
            "the delegation path owns a durable retry; re-arming here would "
            "deliver one completion twice (#7680 CORE)"
        )
    assert _FakeTimer.instances == [], "no retry timer may be scheduled"
    assert sid not in routes._DEFERRED_WAKEUP_RETRY_TIMERS
    # ...and its OWN durable retry is the only retry that fired.
    assert len(released) == 1, "the durable release must still happen"
    assert len(retried) == 1, "the durable retry sweep must be armed"


# --------------------------------------------------------------------------
# End-to-end: exactly-once delivery through the REAL retry timer + drain
# --------------------------------------------------------------------------


class _WakeupRunnerSyncWorkerFailThread:
    """Runner threads (named) run synchronously; unnamed agent workers fail.

    ``_start_server_side_wakeup_turn`` names its thread
    ``hermes-webui-process-wakeup-<sid8>``; the agent worker thread created by
    ``_start_chat_stream_for_session`` is unnamed. Distinguishing on the name
    lets one stub drive the drain synchronously while still failing the agent
    worker start through the REAL launch-abort path.
    """

    worker_starts = 0

    def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None, **extra):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self._name = str(name or "")
        self.daemon = bool(daemon)

    def start(self):
        if self._name.startswith("hermes-webui-process-wakeup"):
            if self._target is not None:
                self._target(*self._args, **self._kwargs)
            return
        _WakeupRunnerSyncWorkerFailThread.worker_starts += 1
        raise RuntimeError("thread launch failed (simulated)")

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def test_abort_retry_delivers_exactly_one_wakeup_turn(monkeypatch, fake_timer):
    """End to end: a launch abort on the process-completion path queues the
    prompt, schedules ONE bounded retry, and firing that retry delivers
    EXACTLY ONE wakeup turn — with no user turn in between and no second
    delivery from a follow-up drain.
    """
    sid = "wakeup-e2e-exactly-once"
    _reset_wakeup_state()
    session, holder = _install_launch_abort(monkeypatch, sid)
    prompt = (
        "[IMPORTANT: Background process proc-e2e-1 completed (exit_code=0).\n"
        "Command: sleep 1\nOutput:\ndone]"
    )

    with pytest.raises(RuntimeError, match="thread launch failed"):
        routes._start_chat_stream_for_session(
            session,
            msg=prompt,
            workspace="/tmp",
            model="m",
            model_provider="p",
            source="process_wakeup",
            process_id="proc-e2e-1",
            retry_attempt=0,
            rearm_deferred_wakeup=True,
        )

    assert len(_FakeTimer.instances) == 1, (
        "the launch abort must schedule exactly one bounded retry"
    )
    timer = _FakeTimer.instances[0]

    delivered: list = []
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)
    monkeypatch.setattr(
        bp,
        "_start_server_side_wakeup_turn",
        lambda sid_, msg_, **kw: delivered.append(
            {"session_id": sid_, "message": msg_, **kw}
        ),
    )

    # Fire the retry: the REAL drain runs (claim + deliver + PENDING discard).
    timer.fn(*timer.args)

    assert len(delivered) == 1, (
        f"expected exactly ONE wakeup turn delivered, got {delivered!r} — "
        "the retry double-delivered"
    )
    assert delivered[0]["session_id"] == sid
    assert "proc-e2e-1" in delivered[0]["message"]
    assert int(delivered[0].get("retry_attempt") or 0) == 1

    # Nothing left: a subsequent drain is a no-op — not a second delivery.
    assert bp.drain_deferred_wakeups_for_session(sid) == 0
    assert len(delivered) == 1, "a follow-up drain fired a second wakeup turn"
    with config.DEFERRED_PROCESS_WAKEUPS_LOCK:
        assert not config.DEFERRED_PROCESS_WAKEUPS.get(sid)


def test_abort_retry_failure_keeps_prompt_queued_and_does_not_loop(
    monkeypatch, fake_timer
):
    """End to end: if the retry's OWN delivery also fails to launch, the prompt
    STAYS queued (a later real turn / teardown can still deliver it) and NO
    further timer is scheduled — a persistent launch failure must not spin a
    new retry every interval forever.
    """
    sid = "wakeup-e2e-retry-fails"
    _reset_wakeup_state()
    session, holder = _install_launch_abort(monkeypatch, sid)
    prompt = (
        "[IMPORTANT: Background process proc-e2e-2 completed (exit_code=1).\n"
        "Command: false\nOutput:\nfailed]"
    )

    with pytest.raises(RuntimeError, match="thread launch failed"):
        routes._start_chat_stream_for_session(
            session,
            msg=prompt,
            workspace="/tmp",
            model="m",
            model_provider="p",
            source="process_wakeup",
            process_id="proc-e2e-2",
            retry_attempt=0,
            rearm_deferred_wakeup=True,
        )
    assert len(_FakeTimer.instances) == 1
    timer = _FakeTimer.instances[0]

    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)

    launch_attempts = {"n": 0}

    def _routing_start_session_turn(
        session_id, message, *, source="process_wakeup", **kw
    ):
        launch_attempts["n"] += 1
        # Route into the REAL launch path with exactly what the production
        # wakeup caller passes, including the retry marker from the drain.
        return routes._start_chat_stream_for_session(
            session,
            msg=message,
            workspace="/tmp",
            model="m",
            model_provider="p",
            source=source,
            process_id=str(kw.get("process_id") or ""),
            retry_attempt=int(kw.get("retry_attempt") or 0),
            rearm_deferred_wakeup=bool(kw.get("rearm_deferred_wakeup")),
        )

    monkeypatch.setattr(routes, "start_session_turn", _routing_start_session_turn)

    # From here the runner thread runs synchronously while the agent worker
    # start keeps failing — the retry's own delivery fails.
    _WakeupRunnerSyncWorkerFailThread.worker_starts = 0
    monkeypatch.setattr(
        routes.threading, "Thread", _WakeupRunnerSyncWorkerFailThread
    )

    timer.fn(*timer.args)

    assert launch_attempts["n"] == 1, (
        f"the retry must attempt exactly one launch, got {launch_attempts['n']}"
    )
    assert _WakeupRunnerSyncWorkerFailThread.worker_starts == 1

    with config.DEFERRED_PROCESS_WAKEUPS_LOCK:
        entries = list(config.DEFERRED_PROCESS_WAKEUPS.get(sid) or [])
    assert len(entries) == 1, (
        "the failed retry DROPPED the prompt — it must stay queued so a later "
        "real turn / teardown can still deliver it"
    )
    assert "proc-e2e-2" in entries[0]["wakeup_prompt"]

    assert len(_FakeTimer.instances) == 1, (
        f"no further timer may be scheduled, got {len(_FakeTimer.instances)} — "
        "the bounded retry looped"
    )
    assert sid not in routes._DEFERRED_WAKEUP_RETRY_TIMERS
