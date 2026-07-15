import queue
import sys
import threading
import types
from pathlib import Path

import pytest

import api.config as config
import api.models as models
import api.streaming as streaming
from api.models import Session


@pytest.fixture(autouse=True)
def isolated_ephemeral_store(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    config.STREAM_REASONING_TEXT.clear()
    config.STREAM_LIVE_TOOL_CALLS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    try:
        config.SESSION_AGENT_CACHE.clear()
    except Exception:
        pass
    yield session_dir
    models.SESSIONS.clear()
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    config.STREAM_REASONING_TEXT.clear()
    config.STREAM_LIVE_TOOL_CALLS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    try:
        config.SESSION_AGENT_CACHE.clear()
    except Exception:
        pass


def _large_messages(marker):
    return [
        {
            "role": "user",
            "content": marker + ("x" * (models._SESSION_TAIL_CACHE_MIN_SOURCE_BYTES + 1024)),
            "timestamp": 1.0,
        },
        {"role": "assistant", "content": "parent answer", "timestamp": 2.0},
    ]


def _prepare_ephemeral(tmp_path, marker):
    session_id = f"btw_{marker.lower()}"
    stream_id = f"stream-{session_id}"
    session = Session(
        session_id=session_id,
        title="Ephemeral cleanup test",
        workspace=str(tmp_path),
        model="test-model",
        profile="default",
        messages=_large_messages(marker),
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "answer this aside"
    session.pending_started_at = 3.0
    session.pending_user_source = "webui"
    session.save()
    models.SESSIONS[session_id] = session
    event_queue = queue.Queue()
    streaming.STREAMS[stream_id] = event_queue
    cache_path = models.session_tail_cache_path(session_id)
    assert session.path.exists()
    assert cache_path.exists()
    return session, stream_id, event_queue, cache_path


def _run_ephemeral_worker(monkeypatch, session, stream_id, *, cancel_on_agent_init=False):
    result_messages = [
        *session.messages,
        {"role": "user", "content": "answer this aside", "timestamp": 3.0},
        {"role": "assistant", "content": "ephemeral answer", "timestamp": 4.0},
    ]

    class FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id")
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None
            if cancel_on_agent_init:
                streaming.CANCEL_FLAGS[stream_id].set()

        def run_conversation(self, **kwargs):
            return {"messages": result_messages}

        def interrupt(self, _message):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()
    with monkeypatch.context() as scoped:
        scoped.setattr(streaming, "get_session", lambda _sid: session)
        scoped.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
        scoped.setattr(
            streaming,
            "resolve_model_provider",
            lambda *_args, **_kwargs: ("test-model", "test-provider", None),
        )
        scoped.setattr("api.config.get_config", lambda *_args, **_kwargs: {})
        scoped.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
        scoped.setitem(sys.modules, "hermes_state", fake_hermes_state)
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=session.pending_user_message,
            model="test-model",
            workspace=session.workspace,
            stream_id=stream_id,
            ephemeral=True,
        )
    event_queue = streaming.STREAMS.get(stream_id)
    if event_queue is None:
        # The worker teardown removes STREAMS, but the caller still owns the queue
        # object supplied during setup.
        return result_messages
    return result_messages


def _events(event_queue):
    items = []
    while not event_queue.empty():
        item = event_queue.get_nowait()
        items.append((item[0], item[1]))
    return items


def test_btw_success_preserves_done_payload_and_removes_all_session_bytes(
    tmp_path,
    monkeypatch,
):
    session, stream_id, event_queue, cache_path = _prepare_ephemeral(tmp_path, "SUCCESS")
    result_messages = _run_ephemeral_worker(monkeypatch, session, stream_id)

    events = _events(event_queue)
    done = [payload for event, payload in events if event == "done"]
    assert done == [
        {
            "session": {"session_id": session.session_id, "messages": result_messages},
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "ephemeral": True,
            "answer": "ephemeral answer",
        }
    ]
    assert not any(event in {"cancel", "stream_end"} for event, _payload in events)
    assert not session.path.exists()
    assert not cache_path.exists()


def test_btw_agent_init_cancel_preserves_cancel_semantics_and_removes_all_session_bytes(
    tmp_path,
    monkeypatch,
):
    session, stream_id, event_queue, cache_path = _prepare_ephemeral(tmp_path, "CANCEL")
    original_messages = list(session.messages)
    _run_ephemeral_worker(
        monkeypatch,
        session,
        stream_id,
        cancel_on_agent_init=True,
    )

    events = _events(event_queue)
    cancel = [payload for event, payload in events if event == "cancel"]
    assert len(cancel) == 1
    assert cancel[0]["message"] == "Cancelled by user"
    assert not any(event in {"done", "stream_end"} for event, _payload in events)
    assert session.active_stream_id is None
    assert session.pending_user_message is None
    assert session.pending_attachments == []
    assert session.pending_started_at is None
    assert session.pending_user_source is None
    assert session.messages == original_messages
    assert not session.path.exists()
    assert not cache_path.exists()


def test_ephemeral_cleanup_joins_checkpoint_before_deleting_files(tmp_path):
    session, _stream_id, _event_queue, cache_path = _prepare_ephemeral(tmp_path, "CHECKPOINT")
    checkpoint_stop = threading.Event()
    checkpoint_entered = threading.Event()
    release_checkpoint = threading.Event()

    def late_checkpoint():
        checkpoint_entered.set()
        assert release_checkpoint.wait(timeout=5)
        session.save(touch_updated_at=False, skip_index=True)

    checkpoint_thread = threading.Thread(target=late_checkpoint)
    checkpoint_thread.start()
    assert checkpoint_entered.wait(timeout=5)
    cleanup_thread = threading.Thread(
        target=streaming._cleanup_ephemeral_session,
        kwargs={
            "session": session,
            "checkpoint_stop": checkpoint_stop,
            "checkpoint_thread": checkpoint_thread,
        },
    )
    cleanup_thread.start()
    assert checkpoint_stop.wait(timeout=5)
    assert cleanup_thread.is_alive(), "cleanup deleted before the checkpoint completed"
    release_checkpoint.set()
    cleanup_thread.join(timeout=5)
    checkpoint_thread.join(timeout=5)

    assert not cleanup_thread.is_alive()
    assert not checkpoint_thread.is_alive()
    assert not session.path.exists()
    assert not cache_path.exists()


def test_ephemeral_tail_cleanup_failure_is_nonfatal_and_still_removes_source(
    tmp_path,
    monkeypatch,
):
    session, stream_id, event_queue, cache_path = _prepare_ephemeral(tmp_path, "FAILURE")
    original_unlink = models.os.unlink

    def fail_cache_unlink(path, *args, **kwargs):
        if Path(path) == cache_path:
            raise OSError("simulated ephemeral tail-cache cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(models.os, "unlink", fail_cache_unlink)
    _run_ephemeral_worker(monkeypatch, session, stream_id)

    events = _events(event_queue)
    assert any(event == "done" and payload.get("ephemeral") is True for event, payload in events)
    assert not session.path.exists()
    assert cache_path.exists()
    assert models.read_session_tail_cache(session.session_id) is None
