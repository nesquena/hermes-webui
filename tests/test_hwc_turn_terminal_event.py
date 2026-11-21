"""hwc: a rejected error writeback emits apperror and does not save.

``active_stream_id is None`` on the worker-held session is not proof that
object is still canonical. Cancel clears the field, eviction reloads a
different instance, and a rename after Stop must survive the old worker.
"""

import json
import queue
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import api.config as config
import api.models as models
import api.streaming as streaming
from api.models import Session
from api.session_ops import apply_session_title_rename

STREAMING = Path(__file__).resolve().parents[1] / "api" / "streaming.py"
MARKER = "Skipping stale stream error writeback"
NEW_TITLE = "New user title after Stop"
OLD_TITLE = "Old title before Stop"
# #9098 item 2: a rotation of the worker-held session id, applied inside the
# failing title-thread hook. The closing stream_end must name the id the
# worker held, not this rotated s.session_id.
ROTATED_SID = "post_success_initial_title-rotated"


def _stale_error_branch(src: str) -> str:
    start = src.find(MARKER)
    assert start >= 0, "stale error writeback marker missing"
    lines = []
    for line in src[start:].splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
        if stripped == "return":
            return "\n".join(lines)
    raise AssertionError("stale error writeback branch has no return")


def test_stale_error_writeback_emits_apperror_without_saving_snapshot():
    branch = _stale_error_branch(STREAMING.read_text(encoding="utf-8"))
    assert "put('apperror'" in branch
    assert "stale_writeback_skipped" in branch
    assert ".save(" not in branch
    assert "pending_user_message = None" not in branch
    assert "if getattr(s, 'active_stream_id', None) is None:" not in branch


def test_stale_worker_after_stop_rename_keeps_title_and_emits_apperror(tmp_path, monkeypatch):
    """Real worker, cancel, eviction, cold reload, rename, then the old worker.

    The rejected snapshot still has ``active_stream_id is None``. Saving it
    would put the pre-Stop title back on the sidecar and the session index
    while the resident object keeps the rename.
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "_sync_sidecar_from_state_db_if_newer", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "api.state_sync.sync_session_cwd_background",
        lambda *_a, **_k: None,
    )

    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.ACTIVE_RUNS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    config.SESSION_WRITEBACK_OWNERS.clear()
    try:
        config.SESSION_AGENT_CACHE.clear()
    except Exception:
        pass

    sid = "stale_title_after_stop"
    stream_id = "stream-stale-title"
    msg_text = "hello before stop"
    captured = Session(
        session_id=sid,
        title=OLD_TITLE,
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": "earlier turn", "timestamp": 1.0}],
    )
    captured.active_stream_id = stream_id
    captured.pending_user_message = msg_text
    captured.pending_started_at = time.time()
    captured.pending_user_source = "webui"
    captured.manual_title = False
    captured.save()
    models.SESSIONS[sid] = captured
    event_queue = queue.Queue()
    config.STREAMS[stream_id] = event_queue
    config.register_stream_owner(stream_id, sid)

    reached = threading.Event()
    release = threading.Event()
    original_payload = streaming._provider_error_payload
    paused_once = {"done": False}

    def _pause_after_cancel_check(*args, **kwargs):
        # First call on this path is after ``if cancel_event.is_set()`` and
        # before the session lock / writeback decision.
        if not paused_once["done"]:
            paused_once["done"] = True
            reached.set()
            assert release.wait(20), "stale worker was not released"
        return original_payload(*args, **kwargs)

    class FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id") or sid
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None

        def run_conversation(self, **_kwargs):
            raise RuntimeError("synthetic agent failure")

        def interrupt(self, _message=None):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_a, **_k: object()

    monkeypatch.setattr(streaming, "_provider_error_payload", _pause_after_cancel_check)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda *_a, **_k: ("gpt-4o", "openai", None),
    )
    monkeypatch.setattr("api.config.get_config", lambda *_a, **_k: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_a, **_k: [])
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)

    errors = []

    def _run():
        try:
            streaming._run_agent_streaming(
                session_id=sid,
                msg_text=msg_text,
                model="gpt-4o",
                workspace=str(tmp_path),
                stream_id=stream_id,
            )
        except Exception as exc:  # pragma: no cover - worker swallows its own errors
            errors.append(exc)

    worker = threading.Thread(target=_run, name="stale-title-worker", daemon=True)
    worker.start()
    try:
        assert reached.wait(30), "worker did not reach the post-cancel-check pause"
        assert streaming.cancel_stream(stream_id) is True
        assert captured.active_stream_id is None

        filler = Session(
            session_id="filler_keep_resident",
            title="Filler",
            workspace=str(tmp_path),
            messages=[{"role": "user", "content": "keep", "timestamp": 1.0}],
        )
        filler.save()
        with models.LOCK:
            models.SESSIONS[filler.session_id] = filler
            models.SESSIONS.move_to_end(filler.session_id)
            evicted = models._evict_sessions_over_cap(1)
        assert evicted >= 1
        assert sid not in models.SESSIONS

        cold = models.get_session(sid)
        assert cold is not captured
        apply_session_title_rename(cold, NEW_TITLE)
        cold.save()
        assert models.SESSIONS.get(sid) is cold
        sidecar_after_rename = (session_dir / f"{sid}.json").read_text(encoding="utf-8")
        assert json.loads(sidecar_after_rename)["title"] == NEW_TITLE

        release.set()
        worker.join(30)
    finally:
        release.set()
        worker.join(5)
        models.SESSIONS.clear()
        config.STREAMS.clear()
        config.CANCEL_FLAGS.clear()
        config.AGENT_INSTANCES.clear()
        config.ACTIVE_RUNS.clear()
        config.SESSION_AGENT_LOCKS.clear()
        config.SESSION_WRITEBACK_OWNERS.clear()
        try:
            config.SESSION_AGENT_CACHE.clear()
        except Exception:
            pass

    assert not worker.is_alive()
    assert errors == []
    assert captured.title == OLD_TITLE
    assert cold.title == NEW_TITLE

    sidecar = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert sidecar["title"] == NEW_TITLE
    assert sidecar["manual_title"] is True
    assert sidecar_after_rename == (session_dir / f"{sid}.json").read_text(encoding="utf-8")

    index = json.loads(index_file.read_text(encoding="utf-8"))
    row = next(entry for entry in index if entry.get("session_id") == sid)
    assert row["title"] == NEW_TITLE
    assert row["manual_title"] is True

    events = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())
    apperrors = [item[1] for item in events if item[0] == "apperror"]
    assert apperrors, "terminal apperror was not emitted"
    assert apperrors[-1].get("stale_writeback_skipped") is True
    assert apperrors[-1].get("session_id") == sid


def test_post_success_title_thread_failure_keeps_published_done(tmp_path, monkeypatch):
    """A failure after a published ``done`` must not add a stale ``apperror``.

    The success path clears ``active_stream_id`` and emits ``done`` and
    ``stream_end`` before it schedules the adaptive title refresh. If that
    thread cannot start, the exception reaches the stale-writeback branch.
    It must keep the published outcome: no ``apperror`` frame, and the run
    journal still settles on ``done`` / ``completed``. Real worker, real title
    refresh scheduling and real run journal; only the provider Agent and the
    title-refresh thread's ``Thread.start`` are replaced.
    """
    from api import run_journal

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "_sync_sidecar_from_state_db_if_newer", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "api.state_sync.sync_session_cwd_background",
        lambda *_a, **_k: None,
    )

    registries = (
        models.SESSIONS, config.STREAMS, config.CANCEL_FLAGS,
        config.AGENT_INSTANCES, config.ACTIVE_RUNS,
        config.SESSION_AGENT_LOCKS, config.SESSION_WRITEBACK_OWNERS,
    )
    for registry in registries:
        registry.clear()

    sid = "post_success_title_refresh"
    stream_id = "stream-post-success"
    msg_text = "hello after a generated title"
    session = Session(
        session_id=sid,
        title="Weekly planning notes",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[],
    )
    session.active_stream_id = stream_id
    session.pending_user_message = msg_text
    session.pending_started_at = time.time()
    session.pending_user_source = "webui"
    session.manual_title = False
    session.llm_title_generated = True
    session.save()
    models.SESSIONS[sid] = session
    event_queue = queue.Queue()
    config.STREAMS[stream_id] = event_queue
    config.register_stream_owner(stream_id, sid)

    provider_calls = []

    class FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id") or sid
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None

        def run_conversation(self, **_kwargs):
            provider_calls.append("run")
            return {"messages": [
                {"role": "user", "content": msg_text},
                {"role": "assistant", "content": "Finished."},
            ]}

        def interrupt(self, _message=None):
            return None

    title_thread_starts = []
    original_start = threading.Thread.start

    def _fail_only_title_refresh_start(thread):
        if getattr(thread, "_target", None) is streaming._run_background_title_refresh:
            title_thread_starts.append(thread.name)
            raise RuntimeError("can't start new thread")
        return original_start(thread)

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_a, **_k: object()

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda *_a, **_k: ("gpt-4o", "openai", None),
    )
    monkeypatch.setattr("api.config.get_config", lambda *_a, **_k: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_a, **_k: [])
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)
    monkeypatch.setattr(streaming, "_get_title_refresh_interval", lambda: 1)
    monkeypatch.setattr(threading.Thread, "start", _fail_only_title_refresh_start)

    try:
        streaming._run_agent_streaming(
            session_id=sid,
            msg_text=msg_text,
            model="gpt-4o",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )
    finally:
        for registry in registries:
            registry.clear()

    frames = []
    while not event_queue.empty():
        frames.append(event_queue.get_nowait())
    names = [item[0] for item in frames]

    assert provider_calls == ["run"]
    # The failure really happened, once, at the title-refresh thread start.
    assert len(title_thread_starts) == 1, names
    terminal_frames = [n for n in names if n in {"done", "stream_end", "cancel", "apperror"}]
    assert terminal_frames == ["done", "stream_end"], names

    journal = run_journal.read_run_events(sid, stream_id, session_dir=session_dir)
    assert not [row for row in journal["events"] if row.get("event") == "apperror"]
    terminal = run_journal.select_authoritative_terminal_event(journal["events"])
    assert terminal is not None and terminal["event"] == "done", terminal
    assert terminal.get("terminal_state") == "completed", terminal
    summary = run_journal.find_run_summary(stream_id, session_dir=session_dir)
    assert summary["terminal_state"] == "completed", summary

    sidecar = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert sidecar.get("active_stream_id") is None
    assert any(
        m.get("role") == "assistant" and m.get("content") == "Finished."
        for m in sidecar["messages"]
    )


def test_post_success_initial_title_thread_failure_still_closes_the_stream(tmp_path, monkeypatch):
    """The initial title thread owns ``stream_end``; if it can't start, close anyway.

    When the first exchange is eligible for background title generation, the
    success path emits ``done`` and hands ``stream_end`` to the title thread.
    If that thread cannot start, the exception reaches the stale-writeback
    branch after ``done`` but before any ``stream_end``. Keeping the published
    ``done`` must not leave the transport open: the branch emits the missing
    ``stream_end`` (with the original session id), still no ``apperror``.
    """
    from api import run_journal

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "_sync_sidecar_from_state_db_if_newer", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "api.state_sync.sync_session_cwd_background",
        lambda *_a, **_k: None,
    )

    registries = (
        models.SESSIONS, config.STREAMS, config.CANCEL_FLAGS,
        config.AGENT_INSTANCES, config.ACTIVE_RUNS,
        config.SESSION_AGENT_LOCKS, config.SESSION_WRITEBACK_OWNERS,
    )
    for registry in registries:
        registry.clear()

    sid = "post_success_initial_title"
    stream_id = "stream-post-success-initial"
    msg_text = "hello after a generated title"
    session = Session(
        session_id=sid,
        title="Weekly planning notes",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[],
    )
    session.active_stream_id = stream_id
    session.pending_user_message = msg_text
    session.pending_started_at = time.time()
    session.pending_user_source = "webui"
    session.manual_title = False
    session.llm_title_generated = True
    session.save()
    models.SESSIONS[sid] = session
    event_queue = queue.Queue()
    config.STREAMS[stream_id] = event_queue
    config.register_stream_owner(stream_id, sid)

    provider_calls = []

    class FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id") or sid
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None

        def run_conversation(self, **_kwargs):
            provider_calls.append("run")
            return {"messages": [
                {"role": "user", "content": msg_text},
                {"role": "assistant", "content": "Finished."},
            ]}

        def interrupt(self, _message=None):
            return None

    title_thread_starts = []
    original_start = threading.Thread.start

    def _fail_only_title_refresh_start(thread):
        if getattr(thread, "_target", None) is streaming._run_background_title_update:
            title_thread_starts.append(thread.name)
            # #9098 item 2: rotate the worker-held session id inside the
            # failing hook. The branch that closes the transport after the
            # published ``done`` must name the id the worker held (its
            # ``session_id`` argument), not the rotated ``s.session_id`` —
            # a rename after eviction must not close the stream on the wrong
            # session.
            _rotated = models.SESSIONS.get(sid)
            if _rotated is not None:
                _rotated.session_id = ROTATED_SID
            raise RuntimeError("can't start new thread")
        return original_start(thread)

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_a, **_k: object()

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(
        streaming,
        "resolve_model_provider",
        lambda *_a, **_k: ("gpt-4o", "openai", None),
    )
    monkeypatch.setattr("api.config.get_config", lambda *_a, **_k: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_a, **_k: [])
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)
    monkeypatch.setattr(streaming, "_background_title_generation_eligible", lambda _s: True)
    monkeypatch.setattr(threading.Thread, "start", _fail_only_title_refresh_start)

    try:
        streaming._run_agent_streaming(
            session_id=sid,
            msg_text=msg_text,
            model="gpt-4o",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )
    finally:
        for registry in registries:
            registry.clear()

    frames = []
    while not event_queue.empty():
        frames.append(event_queue.get_nowait())
    names = [item[0] for item in frames]

    assert provider_calls == ["run"]
    # The failure really happened, once, at the initial title thread start.
    assert len(title_thread_starts) == 1, names
    terminal_frames = [n for n in names if n in {"done", "stream_end", "cancel", "apperror"}]
    assert terminal_frames == ["done", "stream_end"], names
    stream_end = [item[1] for item in frames if item[0] == "stream_end"]
    assert stream_end == [{"session_id": sid}], stream_end

    journal = run_journal.read_run_events(sid, stream_id, session_dir=session_dir)
    assert not [row for row in journal["events"] if row.get("event") == "apperror"]
    terminal = run_journal.select_authoritative_terminal_event(journal["events"])
    assert terminal is not None and terminal["event"] == "done", terminal
    assert terminal.get("terminal_state") == "completed", terminal
    summary = run_journal.find_run_summary(stream_id, session_dir=session_dir)
    assert summary["terminal_state"] == "completed", summary

    sidecar = json.loads((session_dir / f"{sid}.json").read_text(encoding="utf-8"))
    assert sidecar.get("active_stream_id") is None
    assert any(
        m.get("role") == "assistant" and m.get("content") == "Finished."
        for m in sidecar["messages"]
    )


class _HoldingQueue:
    """Proxy that holds the first cancel frame so the worker's error lands first.

    ``cancel_stream`` reads the queue from STREAMS at cancel time, while the
    worker has held the real queue since admission. Swapping the STREAMS entry
    for this proxy before the cancel lets the worker's ``apperror`` reach the
    client first; ``release_held`` then delivers the cancel frame, reproducing
    the error-first ordering (#9098 items 1 and 3) without any real sleep.
    """

    def __init__(self, inner):
        self._inner = inner
        self._held = None

    def put_nowait(self, item):
        if self._held is None and isinstance(item, tuple) and item and item[0] == "cancel":
            self._held = item
            return
        return self._inner.put_nowait(item)

    def note_last_event_id(self, event_id):
        return self._inner.note_last_event_id(event_id)

    def release_held(self):
        item, self._held = self._held, None
        if item is not None:
            self._inner.put_nowait(item)


def _run_stale_error_race(tmp_path, monkeypatch, sid, stream_id, during_pause, *, hold_cancel_put=False):
    """Real worker with a failing Agent, paused after its cancel check (the PR's seam); run
    ``during_pause(captured, stream_id)``, release it, and return the queued frames and session dir.

    ``hold_cancel_put`` swaps the STREAMS queue for a proxy that holds the cancel frame
    until the worker's own terminal has been queued, so the frames come back in the
    error-first ordering instead of cancel-first (#9098 item 3).
    """
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "_sync_sidecar_from_state_db_if_newer", lambda *_a, **_k: False)
    monkeypatch.setattr("api.state_sync.sync_session_cwd_background", lambda *_a, **_k: None)
    registries = (
        models.SESSIONS, config.STREAMS, config.CANCEL_FLAGS,
        config.AGENT_INSTANCES, config.ACTIVE_RUNS,
        config.SESSION_AGENT_LOCKS, config.SESSION_WRITEBACK_OWNERS,
    )
    for registry in registries:
        registry.clear()

    msg_text = "hello before stop"
    captured = Session(session_id=sid, title=OLD_TITLE, workspace=str(tmp_path), model="gpt-4o",
                       messages=[{"role": "user", "content": "earlier turn", "timestamp": 1.0}])
    captured.active_stream_id = stream_id
    captured.pending_user_message = msg_text
    captured.pending_started_at = time.time()
    captured.pending_user_source = "webui"
    captured.save()
    models.SESSIONS[sid] = captured
    event_queue = queue.Queue()
    config.STREAMS[stream_id] = event_queue
    config.register_stream_owner(stream_id, sid)

    reached, release = threading.Event(), threading.Event()
    original_payload = streaming._provider_error_payload
    paused_once = {"done": False}

    def _pause_after_cancel_check(*args, **kwargs):
        if not paused_once["done"]:
            paused_once["done"] = True
            reached.set()
            assert release.wait(20), "stale worker was not released"
        return original_payload(*args, **kwargs)

    class FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id") or sid
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None

        def run_conversation(self, **_kwargs):
            raise RuntimeError("synthetic agent failure")

        def interrupt(self, _message=None):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_a, **_k: object()
    monkeypatch.setattr(streaming, "_provider_error_payload", _pause_after_cancel_check)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *_a, **_k: ("gpt-4o", "openai", None))
    monkeypatch.setattr("api.config.get_config", lambda *_a, **_k: {})
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_a, **_k: [])
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)

    errors = []

    def _run():
        try:
            streaming._run_agent_streaming(session_id=sid, msg_text=msg_text, model="gpt-4o",
                                           workspace=str(tmp_path), stream_id=stream_id)
        except Exception as exc:  # pragma: no cover - worker swallows its own errors
            errors.append(exc)

    worker = threading.Thread(target=_run, name="stale-race-worker", daemon=True)
    worker.start()
    holding = None
    try:
        assert reached.wait(30), "worker did not reach the post-cancel-check pause"
        if hold_cancel_put:
            holding = _HoldingQueue(config.STREAMS[stream_id])
            config.STREAMS[stream_id] = holding
        during_pause(captured, stream_id)
        release.set()
        worker.join(30)
    finally:
        release.set()
        worker.join(5)
        if holding is not None:
            holding.release_held()
        for registry in registries:
            registry.clear()
    assert not worker.is_alive() and errors == []
    frames = []
    while not event_queue.empty():
        frames.append(event_queue.get_nowait())
    return frames, session_dir


@pytest.mark.parametrize("hold_cancel_put", [False, True], ids=["cancel-first", "error-first"])
def test_stop_won_stale_worker_settles_as_cancelled_not_errored(tmp_path, monkeypatch, hold_cancel_put):
    """Stop lands after the worker's cancel check but before its ownership check.

    cancel_stream already queued the live ``cancel`` frame (not journaled). The stale branch's
    ``apperror`` is the only journaled terminal, so it must carry the cancel: the run settles as
    ``interrupted-by-user``, and a tab that missed the live ``cancel`` replays a cancel, not a
    provider error.
    """
    from api import run_journal

    sid, stream_id = "stop_won_stale_worker", "stream-stop-won"

    def _stop(_captured, sid_stream):
        assert streaming.cancel_stream(sid_stream) is True

    frames, session_dir = _run_stale_error_race(
        tmp_path, monkeypatch, sid, stream_id, _stop, hold_cancel_put=hold_cancel_put,
    )
    names = [item[0] for item in frames]
    assert "cancel" in names, names
    apperrors = [item[1] for item in frames if item[0] == "apperror"]
    assert len(apperrors) == 1, names
    assert apperrors[0].get("type") == "cancelled"
    assert apperrors[0].get("message") == "Task cancelled."
    assert "cancel_initiator" not in apperrors[0]
    # #9098 item 1: the apperror carries the cancelled-turn hint the cancel frame
    # renders, so both orderings match from the first frame. No provider
    # details/reason survive the cancel classification.
    assert apperrors[0].get("hint") == streaming._cancelled_turn_hint()
    assert not {"details", "reason"} & set(apperrors[0]), apperrors[0]
    assert apperrors[0].get("stale_writeback_skipped") is True
    assert apperrors[0].get("session_id") == sid and apperrors[0].get("old_session_id") == sid

    summary = run_journal.find_run_summary(stream_id, session_dir=session_dir)
    assert summary["terminal_state"] == "interrupted-by-user", summary
    journal = run_journal.read_run_events(sid, stream_id, session_dir=session_dir)
    terminal = run_journal.select_authoritative_terminal_event(journal["events"])
    assert terminal is not None and terminal.get("terminal_state") == "interrupted-by-user", terminal


@pytest.mark.parametrize("hold_cancel_put", [False, True], ids=["cancel-first", "error-first"])
def test_server_settled_stale_worker_names_the_server(tmp_path, monkeypatch, hold_cancel_put):
    """A server settle (restart drain) that wins the same race is not labelled a user cancel."""
    from api import run_journal

    sid, stream_id = "server_settled_stale_worker", "stream-server-settle"
    reason = "The WebUI server restarted and settled this run."

    def _server_settle(_captured, sid_stream):
        assert streaming.cancel_stream(sid_stream, reason=reason,
                                       initiator=streaming.CANCEL_INITIATOR_SERVER) is True

    try:
        frames, session_dir = _run_stale_error_race(
            tmp_path, monkeypatch, sid, stream_id, _server_settle, hold_cancel_put=hold_cancel_put,
        )
    finally:
        streaming._SERVER_SETTLE_REASONS.pop(stream_id, None)
    apperrors = [item[1] for item in frames if item[0] == "apperror"]
    assert len(apperrors) == 1, [item[0] for item in frames]
    assert apperrors[0].get("type") == "cancelled"
    assert apperrors[0].get("message") == reason
    assert apperrors[0].get("cancel_initiator") == streaming.CANCEL_INITIATOR_SERVER
    # #9098 item 1: the server-settle hint rides the error-first apperror too.
    assert apperrors[0].get("hint") == streaming._cancelled_turn_hint(
        initiator=streaming.CANCEL_INITIATOR_SERVER,
    )
    assert not {"details", "reason"} & set(apperrors[0]), apperrors[0]
    assert apperrors[0].get("stale_writeback_skipped") is True
    summary = run_journal.find_run_summary(stream_id, session_dir=session_dir)
    assert summary["terminal_state"] == "interrupted-by-user", summary


@pytest.mark.parametrize("hold_cancel_put", [False, True], ids=["cancel-first", "error-first"])
def test_stale_worker_without_stop_still_settles_as_errored(tmp_path, monkeypatch, hold_cancel_put):
    """Control: a newer owner took the session (no Stop). The stale error stays a provider error."""
    from api import run_journal

    sid, stream_id = "stale_worker_no_stop", "stream-no-stop"

    def _newer_owner(captured, _sid_stream):
        captured.active_stream_id = "stream-newer-owner"

    frames, session_dir = _run_stale_error_race(
        tmp_path, monkeypatch, sid, stream_id, _newer_owner, hold_cancel_put=hold_cancel_put,
    )
    names = [item[0] for item in frames]
    assert "cancel" not in names, names
    apperrors = [item[1] for item in frames if item[0] == "apperror"]
    assert len(apperrors) == 1, names
    assert apperrors[0].get("type") != "cancelled" and apperrors[0].get("stale_writeback_skipped") is True
    assert apperrors[0].get("details") and "cancel_initiator" not in apperrors[0]   # provider error kept intact
    summary = run_journal.find_run_summary(stream_id, session_dir=session_dir)
    assert summary["terminal_state"] == "errored", summary


def test_stop_won_error_first_apperror_carries_the_cancelled_hint(tmp_path, monkeypatch):
    """#9098 item 1: the error frame that reaches the client before the cancel
    frame must already carry the cancelled-turn hint.

    When Stop wins the race the stale branch's ``apperror`` is the run's only
    journaled terminal until the live ``cancel`` frame lands. Without the hint
    the turn renders a bare "Task cancelled." for that window; with it both
    orderings render the same from the first frame. The cancel put is held
    until the worker's terminal is queued, so no real sleep is involved.
    """
    sid, stream_id = "stop_won_error_first_hint", "stream-stop-won-error-first"
    monkeypatch.setattr(streaming, "_preferred_agent_display_name_for_session", lambda _s: "Research")

    def _stop(_captured, sid_stream):
        assert streaming.cancel_stream(sid_stream) is True

    frames, _session_dir = _run_stale_error_race(
        tmp_path, monkeypatch, sid, stream_id, _stop, hold_cancel_put=True,
    )
    names = [item[0] for item in frames]
    # The ordering under test really is error-first: the apperror terminal
    # reaches the queue before the cancel frame.
    terminal_names = [n for n in names if n in {"done", "stream_end", "cancel", "apperror"}]
    assert terminal_names == ["apperror", "cancel"], terminal_names
    apperrors = [item[1] for item in frames if item[0] == "apperror"]
    assert len(apperrors) == 1, names
    assert apperrors[0].get("type") == "cancelled"
    assert apperrors[0].get("message") == "Task cancelled."
    assert apperrors[0].get("hint") == streaming._cancelled_turn_hint("Research")
    assert not {"details", "reason"} & set(apperrors[0]), apperrors[0]


def test_server_settled_error_first_apperror_hint_names_the_server(tmp_path, monkeypatch):
    """#9098 item 1, server-settle flavour: the error-first apperror's hint says
    the server stopped the run, matching the cancel frame's copy."""
    sid, stream_id = "server_settled_error_first_hint", "stream-server-settle-error-first"
    reason = "The WebUI server restarted and settled this run."

    def _server_settle(_captured, sid_stream):
        assert streaming.cancel_stream(sid_stream, reason=reason,
                                       initiator=streaming.CANCEL_INITIATOR_SERVER) is True

    try:
        frames, _session_dir = _run_stale_error_race(
            tmp_path, monkeypatch, sid, stream_id, _server_settle, hold_cancel_put=True,
        )
    finally:
        streaming._SERVER_SETTLE_REASONS.pop(stream_id, None)
    names = [item[0] for item in frames]
    terminal_names = [n for n in names if n in {"done", "stream_end", "cancel", "apperror"}]
    assert terminal_names == ["apperror", "cancel"], terminal_names
    apperrors = [item[1] for item in frames if item[0] == "apperror"]
    assert len(apperrors) == 1, names
    assert apperrors[0].get("type") == "cancelled"
    assert apperrors[0].get("message") == reason
    assert apperrors[0].get("cancel_initiator") == streaming.CANCEL_INITIATOR_SERVER
    assert apperrors[0].get("hint") == streaming._cancelled_turn_hint(
        initiator=streaming.CANCEL_INITIATOR_SERVER,
    )
    assert not {"details", "reason"} & set(apperrors[0]), apperrors[0]
