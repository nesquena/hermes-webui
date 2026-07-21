"""Regression coverage for compression-exhausted stream finalization."""

import base64
import copy
import json
import queue
import sys
import threading
import types
from pathlib import Path

import pytest

from api import models, session_media, streaming
from api.models import Session
from api.streaming import (
    _agent_result_terminal_failure,
    _session_lacks_final_assistant_answer,
)

ROOT = Path(__file__).resolve().parents[1]


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def test_compression_media_clone_failure_keeps_source_identity(monkeypatch):
    session = Session(
        session_id="old-media-session",
        messages=[{"role": "user", "content": "history"}],
        context_messages=[{"role": "user", "content": "history"}],
    )

    def fail_clone(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(session_media, "clone_session_media_references", fail_clone)

    with pytest.raises(RuntimeError, match="compression continuation"):
        streaming._clone_session_media_for_compression_rotation(
            session,
            "old-media-session",
            "new-media-session",
        )

    assert session.session_id == "old-media-session"


def test_compression_publication_failure_rolls_back_only_reserved_destination(
    tmp_path, monkeypatch
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    models.SESSIONS.clear()
    models._SESSION_PUBLICATION_DELETED.clear()
    models._SESSION_PUBLICATION_GENERATIONS.clear()
    models._active_destination_reservations().clear()
    old_sid = "compression-old-rollback"
    new_sid = "compression-new-rollback"
    image_raw = b"\x89PNG\r\n\x1a\n" + (b"x" * (70 * 1024))
    image_data_url = "data:image/png;base64," + base64.b64encode(image_raw).decode("ascii")
    session = Session(
        session_id=old_sid,
        messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": image_data_url}}]}],
        context_messages=[],
    )
    session.save(skip_index=True)
    old_generation = session._publication_generation
    original_save = Session.save

    def publish_then_fail(self, *args, **kwargs):
        original_save(self, *args, **kwargs)
        if self is session and self.session_id == new_sid:
            raise OSError("fail after continuation publication")

    monkeypatch.setattr(Session, "save", publish_then_fail)
    with pytest.raises(OSError, match="continuation publication"):
        streaming._publish_compression_continuation(session, old_sid, new_sid, None)

    assert session.session_id == old_sid
    assert session._publication_generation is old_generation
    assert (session_dir / f"{old_sid}.json").exists()
    assert not (session_dir / f"{new_sid}.json").exists()
    assert not session_media._session_media_dir(new_sid).exists()


def test_compression_destination_collision_never_overwrites_existing_owner(
    tmp_path, monkeypatch
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    models.SESSIONS.clear()
    models._SESSION_PUBLICATION_DELETED.clear()
    models._SESSION_PUBLICATION_GENERATIONS.clear()
    models._active_destination_reservations().clear()
    old_sid = "compression-old-collision"
    new_sid = "compression-owned-target"
    session = Session(session_id=old_sid, messages=[{"role": "user", "content": "old"}])
    session.save(skip_index=True)
    existing_path = session_dir / f"{new_sid}.json"
    existing_bytes = b'{"session_id":"compression-owned-target","messages":[{"role":"user","content":"owner"}]}'
    existing_path.write_bytes(existing_bytes)

    with pytest.raises(models.SessionDestinationCollisionError):
        streaming._publish_compression_continuation(session, old_sid, new_sid, None)

    assert session.session_id == old_sid
    assert existing_path.read_bytes() == existing_bytes


def test_compression_registry_failure_rolls_back_all_migrations_before_commit(
    tmp_path, monkeypatch
):
    import api.config as live_config

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    old_sid = "compression-migrate-old"
    new_sid = "compression-migrate-new"
    stream_id = "compression-migrate-stream"
    session = Session(
        session_id=old_sid,
        messages=[{"role": "user", "content": "history"}],
    )
    session.save(skip_index=True)
    models.SESSIONS.clear()
    models.SESSIONS[old_sid] = session
    agent_lock = threading.Lock()

    class Agent:
        session_id = new_sid

    agent = Agent()
    streaming.SESSION_AGENT_LOCKS.clear()
    streaming.SESSION_AGENT_LOCKS[old_sid] = agent_lock
    streaming.STREAMS.clear()
    streaming.STREAMS[stream_id] = {"session_id": old_sid}
    with live_config.STREAM_SESSION_OWNERS_LOCK:
        live_config.STREAM_SESSION_OWNERS[stream_id] = old_sid
    with live_config.ACTIVE_RUNS_LOCK:
        live_config.ACTIVE_RUNS[stream_id] = {"session_id": old_sid}
    runtime_evictions = []
    monkeypatch.setattr(
        live_config,
        "_evict_session_agent",
        lambda sid: runtime_evictions.append(sid),
    )
    monkeypatch.setattr(
        streaming,
        "_evict_sessions_over_cap",
        lambda: (_ for _ in ()).throw(OSError("injected registry failure")),
    )

    try:
        with pytest.raises(OSError, match="registry failure"):
            streaming._publish_compression_continuation(
                session,
                old_sid,
                new_sid,
                None,
                agent=agent,
                agent_lock=agent_lock,
                stream_id=stream_id,
            )

        assert session.session_id == old_sid
        assert models.SESSIONS.get(old_sid) is session
        assert new_sid not in models.SESSIONS
        assert streaming.SESSION_AGENT_LOCKS.get(old_sid) is agent_lock
        assert new_sid not in streaming.SESSION_AGENT_LOCKS
        assert streaming.STREAMS[stream_id]["session_id"] == old_sid
        assert live_config.STREAM_SESSION_OWNERS[stream_id] == old_sid
        assert live_config.ACTIVE_RUNS[stream_id]["session_id"] == old_sid
        assert agent.session_id == old_sid
        assert new_sid not in runtime_evictions
        assert not (session_dir / f"{new_sid}.json").exists()
        assert not (session_dir / "_compression_transactions" / f"{new_sid}.json").exists()
    finally:
        models.SESSIONS.clear()
        streaming.SESSION_AGENT_LOCKS.clear()
        streaming.STREAMS.clear()
        with live_config.STREAM_SESSION_OWNERS_LOCK:
            live_config.STREAM_SESSION_OWNERS.pop(stream_id, None)
        with live_config.ACTIVE_RUNS_LOCK:
            live_config.ACTIVE_RUNS.pop(stream_id, None)


def test_compression_failure_after_source_archival_restores_exact_source(
    tmp_path, monkeypatch
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    models.SESSIONS.clear()
    old_sid = "compression-archive-old"
    new_sid = "compression-archive-new"
    stream_id = "compression-archive-stream"
    session = Session(
        session_id=old_sid,
        messages=[{"role": "user", "content": "history"}],
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "continue"
    session.pending_attachments = [{"name": "pending.txt"}]
    session.pending_started_at = 123.0
    session.pending_user_source = "webui"
    session.save()
    models.SESSIONS[old_sid] = session
    source_before = session.path.read_bytes()
    index_before = models.SESSION_INDEX_FILE.read_bytes()
    observed = {}

    def fail_after_archive(_session, source_sid, _destination_sid):
        archived = json.loads(
            (session_dir / f"{source_sid}.json").read_text(encoding="utf-8")
        )
        observed["source_archived"] = archived["pre_compression_snapshot"] is True
        observed["runtime_cleared"] = archived["active_stream_id"] is None
        raise OSError("injected post-archive failure")

    monkeypatch.setattr(
        streaming,
        "_clone_session_media_for_compression_rotation",
        fail_after_archive,
    )

    with pytest.raises(OSError, match="post-archive failure"):
        streaming._publish_compression_continuation(
            session,
            old_sid,
            new_sid,
            None,
        )

    assert observed == {"source_archived": True, "runtime_cleared": True}
    assert session.path.read_bytes() == source_before
    assert models.SESSION_INDEX_FILE.read_bytes() == index_before
    assert session.session_id == old_sid
    assert session.active_stream_id == stream_id
    assert session.pending_user_message == "continue"
    assert session.pending_attachments == [{"name": "pending.txt"}]
    assert session.pending_started_at == 123.0
    assert session.pending_user_source == "webui"
    assert not (session_dir / f"{new_sid}.json").exists()
    assert not session_media._session_media_dir(new_sid).exists()
    assert not (session_dir / "_compression_transactions" / f"{new_sid}.json").exists()


def test_streaming_rotation_failure_after_source_archival_restores_transaction(
    tmp_path, monkeypatch
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    models.SESSIONS.clear()
    streaming.SESSIONS.clear()
    streaming.STREAMS.clear()
    streaming.AGENT_INSTANCES.clear()
    streaming.SESSION_AGENT_LOCKS.clear()
    old_sid = "compression-integration-old"
    new_sid = "compression-integration-new"
    stream_id = "compression-integration-stream"
    session = Session(
        session_id=old_sid,
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[{"role": "user", "content": "history"}],
        context_messages=[{"role": "user", "content": "history"}],
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "continue"
    session.pending_attachments = [{"name": "pending.txt"}]
    session.pending_started_at = 123.0
    session.pending_user_source = "webui"
    session.save()
    models.SESSIONS[old_sid] = session
    streaming.SESSIONS[old_sid] = session
    event_queue = queue.Queue()
    streaming.STREAMS[stream_id] = event_queue
    observed = {}

    class FakeAgent:
        def __init__(self, session_id=None, stream_delta_callback=None, **_kwargs):
            self.session_id = session_id
            self.stream_delta_callback = stream_delta_callback
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            self.session_id = new_sid
            return {
                "messages": [
                    {
                        "role": "user",
                        "content": kwargs.get("persist_user_message", ""),
                    },
                    {"role": "assistant", "content": "continued"},
                ]
            }

        def interrupt(self, _message):
            return None

    def fail_after_archive(_session, source_sid, destination_sid):
        archived = json.loads(
            (session_dir / f"{source_sid}.json").read_text(encoding="utf-8")
        )
        observed["archived_before_failure"] = (
            archived["pre_compression_snapshot"] is True
            and archived["active_stream_id"] is None
        )
        media_dir = session_media._session_media_dir(destination_sid)
        media_dir.mkdir(parents=True)
        (media_dir / "staged.png").write_bytes(b"staged")
        raise OSError("integration failure after source archival")

    real_publish = streaming._publish_compression_continuation

    def checked_publish(*args, **kwargs):
        source_before = (session_dir / f"{old_sid}.json").read_bytes()
        index_before = models.SESSION_INDEX_FILE.read_bytes()
        runtime_before = (
            session.session_id,
            session.active_stream_id,
            session.pending_user_message,
            copy.deepcopy(session.pending_attachments),
            session.pending_started_at,
            session.pending_user_source,
        )
        try:
            return real_publish(*args, **kwargs)
        except OSError:
            observed["source_exact"] = (
                (session_dir / f"{old_sid}.json").read_bytes() == source_before
            )
            observed["index_exact"] = (
                models.SESSION_INDEX_FILE.read_bytes() == index_before
            )
            observed["runtime_exact"] = runtime_before == (
                session.session_id,
                session.active_stream_id,
                session.pending_user_message,
                session.pending_attachments,
                session.pending_started_at,
                session.pending_user_source,
            )
            observed["destination_retired"] = (
                not (session_dir / f"{new_sid}.json").exists()
                and not session_media._session_media_dir(new_sid).exists()
            )
            raise

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()
    with monkeypatch.context() as patcher:
        patcher.setattr(streaming, "get_session", lambda _sid: session)
        patcher.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
        patcher.setattr(
            streaming,
            "resolve_model_provider",
            lambda *_args, **_kwargs: ("gpt-4o", "openai", None),
        )
        patcher.setattr(
            streaming,
            "_clone_session_media_for_compression_rotation",
            fail_after_archive,
        )
        patcher.setattr(
            streaming,
            "_publish_compression_continuation",
            checked_publish,
        )
        patcher.setattr("api.config.get_config", lambda *_args, **_kwargs: {})
        patcher.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
        patcher.setitem(sys.modules, "hermes_state", fake_hermes_state)
        streaming._run_agent_streaming(
            session_id=old_sid,
            msg_text="continue",
            model="gpt-4o",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )

    assert observed == {
        "archived_before_failure": True,
        "source_exact": True,
        "index_exact": True,
        "runtime_exact": True,
        "destination_retired": True,
    }


def test_compression_exhausted_after_session_rotation_preserves_snapshot_and_errors_on_continuation(
    tmp_path, monkeypatch
):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(session_media, "STATE_DIR", tmp_path)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)
    models.SESSIONS.clear()
    streaming.SESSIONS.clear()
    streaming.STREAMS.clear()
    streaming.AGENT_INSTANCES.clear()
    streaming.SESSION_AGENT_LOCKS.clear()
    old_sid = "old_sid"
    new_sid = "new_sid"
    stream_id = "stream-compression-exhausted"
    image_raw = b"\x89PNG\r\n\x1a\n" + (b"\0" * (70 * 1024))
    image_data_url = "data:image/png;base64," + base64.b64encode(image_raw).decode("ascii")
    image_message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "historical image"},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ],
    }
    session = Session(
        session_id=old_sid,
        title="Compression test",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[copy.deepcopy(image_message)],
        context_messages=[copy.deepcopy(image_message)],
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "Do the long task."
    session.pending_started_at = 1.0
    session.save()
    models.SESSIONS[old_sid] = session
    streaming.SESSIONS[old_sid] = session
    event_queue = queue.Queue()
    streaming.STREAMS[stream_id] = event_queue

    class FakeAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            api_key=None,
            platform=None,
            quiet_mode=False,
            enabled_toolsets=None,
            fallback_model=None,
            session_id=None,
            session_db=None,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            interim_assistant_callback=None,
            clarify_callback=None,
            **kwargs,
        ):
            self.session_id = session_id
            self.stream_delta_callback = stream_delta_callback
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            if self.stream_delta_callback:
                self.stream_delta_callback("I am still working through the files.")
            self.session_id = new_sid
            self._last_error = "Context length exceeded: cannot compress further."
            return {
                "failed": True,
                "partial": True,
                "compression_exhausted": True,
                "error": "Context length exceeded: cannot compress further.",
                "messages": [
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {"role": "assistant", "content": "I am still working through the files."},
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
                    {"role": "tool", "tool_call_id": "call_1", "content": "large output"},
                ],
            }

        def interrupt(self, _message):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()

    with monkeypatch.context() as m:
        m.setattr(streaming, "get_session", lambda _sid: session)
        m.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
        m.setattr(streaming, "resolve_model_provider", lambda *_args, **_kwargs: ("gpt-4o", "openai", None))
        m.setattr("api.config.get_config", lambda *_args, **_kwargs: {})
        m.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
        m.setitem(sys.modules, "hermes_state", fake_hermes_state)
        streaming._run_agent_streaming(
            session_id=old_sid,
            msg_text="Do the long task.",
            model="gpt-4o",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )

    events = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())
    apperror_payloads = [payload for event, payload in events if event == "apperror"]
    assert apperror_payloads, "expected apperror SSE payload"
    payload = apperror_payloads[-1]
    assert payload["type"] == "compression_exhausted"
    assert payload["session"]["session_id"] == new_sid
    assert payload["old_session_id"] == old_sid
    assert payload["new_session_id"] == new_sid
    assert payload["recommended_recovery_action"] == "start_focused_continuation"
    assert payload["compression_recovery"]["terminal_state"] == "compression_exhausted"
    assert payload["compression_recovery"]["source_session_id"] == new_sid

    old_payload = json.loads((session_dir / f"{old_sid}.json").read_text(encoding="utf-8"))
    new_payload = json.loads((session_dir / f"{new_sid}.json").read_text(encoding="utf-8"))
    assert old_payload["pre_compression_snapshot"] is True
    assert old_payload["active_stream_id"] is None
    assert old_payload["pending_user_message"] is None
    assert new_payload["session_id"] == new_sid
    assert new_payload["parent_session_id"] == old_sid
    assert new_payload["pre_compression_snapshot"] is False
    assert new_payload["recommended_recovery_action"] == "start_focused_continuation"
    assert new_payload["compression_recovery"]["recommended_action"] == "start_focused_continuation"
    assert new_payload["messages"][-1]["_error"] is True
    assert new_payload["messages"][-1]["_compressionRecovery"]["recommended_action"] == "start_focused_continuation"
    assert "Context compression exhausted" in new_payload["messages"][-1]["content"]
    assert "webui-media://" in json.dumps(new_payload["messages"])
    destination_files = list(session_media._session_media_dir(new_sid).iterdir())
    assert len(destination_files) == 1
    assert destination_files[0].read_bytes() == image_raw
    session_media.remove_session_media(old_sid)
    continuation = Session.load(new_sid)
    hydrated_messages = session_media.hydrate_session_media_urls(continuation.messages, new_sid)
    assert image_data_url in json.dumps(hydrated_messages)
    hydrated_context = session_media.hydrate_session_media_urls(continuation.context_messages, new_sid)
    assert image_data_url in json.dumps(hydrated_context)
    assert old_sid not in streaming.SESSIONS
    assert streaming.SESSIONS[new_sid].session_id == new_sid


def test_compression_exhausted_result_is_terminal_failure_even_after_streamed_text():
    result = {
        "failed": True,
        "partial": True,
        "compression_exhausted": True,
        "error": "Context length exceeded: 119,194 tokens. Cannot compress further.",
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "I am still working through the files."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "large output"},
        ],
    }

    assert _agent_result_terminal_failure(result) is True
    assert _session_lacks_final_assistant_answer(result["messages"]) is True


def test_terminal_failure_gates_shape_check_to_no_streamed_text():
    src = _read("api/streaming.py")
    start = src.find("_is_agent_result_terminal = _agent_result_terminal_failure(result)")
    assert start != -1, "terminal failure result assignment not found"
    end = src.find("if _terminal_failure:", start)
    assert end != -1, "terminal failure guard not found"
    block = src[start:end]

    assert "_is_agent_result_terminal = _agent_result_terminal_failure(result)" in block
    assert "_is_agent_result_terminal" in block
    assert "_saved_transcript_lacks_final_answer" in block
    assert "_classification['type'] not in {'cancelled', 'interrupted'}" in block
    assert "not _token_sent" not in block
    assert "_session_lacks_final_assistant_answer(_all_result_messages)" not in block


def test_completed_tool_tail_without_final_assistant_is_not_successful_done():
    messages = [
        {"role": "user", "content": "Run the tool then answer."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ]

    assert _session_lacks_final_assistant_answer(messages) is True


def test_assistant_content_with_tool_calls_is_not_final_answer():
    messages = [
        {"role": "user", "content": "Search, then answer."},
        {
            "role": "assistant",
            "content": "I found a likely source and will inspect it.",
            "tool_calls": [{"id": "call_1"}],
        },
    ]

    assert _session_lacks_final_assistant_answer(messages) is True


def test_context_compaction_marker_is_not_final_answer():
    messages = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
        },
    ]

    assert _session_lacks_final_assistant_answer(messages) is True


def test_context_compaction_marker_before_final_text_is_successful_answer():
    messages = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
        },
        {"role": "assistant", "content": "Here is the final answer."},
    ]

    assert _session_lacks_final_assistant_answer(messages) is False


def test_context_compaction_marker_before_tool_tail_is_not_final_answer():
    messages = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary",
        },
        {
            "role": "assistant",
            "content": "I will inspect the result.",
            "tool_calls": [{"id": "call_1"}],
        },
    ]

    assert _session_lacks_final_assistant_answer(messages) is True


def test_final_assistant_text_is_successful_terminal_answer():
    messages = [
        {"role": "user", "content": "Run the tool then answer."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "assistant", "content": "Here is the final answer."},
    ]

    assert _session_lacks_final_assistant_answer(messages) is False


def test_assistant_tool_call_turn_followed_by_final_text_is_successful_answer():
    messages = [
        {"role": "user", "content": "Search, then answer."},
        {
            "role": "assistant",
            "content": "I found a likely source and will inspect it.",
            "tool_calls": [{"id": "call_1"}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "assistant", "content": "Here is the final answer."},
    ]

    assert _session_lacks_final_assistant_answer(messages) is False


def test_compression_exhausted_apperror_clears_reference_ui_and_labels_error():
    src = _read("static/messages.js")
    start = src.find("source.addEventListener('apperror'")
    assert start != -1, "apperror listener not found"
    end = src.find("source.addEventListener('warning'", start)
    assert end != -1, "warning listener after apperror not found"
    block = src[start:end]

    assert "const isCompressionExhausted=d.type==='compression_exhausted';" in block
    assert "isCompressionExhausted?'Context compression exhausted'" in block
    assert "if(typeof clearCompressionUi==='function') clearCompressionUi();" in block
    assert "window._compressionUi=null;" in block
    assert "const eventSid=d.old_session_id||d.session_id||'';" in block
    assert "const continuationSid=(d.session&&d.session.session_id)||d.new_session_id||d.continuation_session_id||'';" in block
    assert "if(d.session&&typeof d.session==='object')" in block
    assert "S.session=d.session;" in block


def test_apperror_matches_only_current_or_continuation_session_for_background_errors():
    src = _read("static/messages.js")
    start = src.find("source.addEventListener('apperror'")
    assert start != -1, "apperror listener not found"
    end = src.find("source.addEventListener('warning'", start)
    assert end != -1, "warning listener after apperror not found"
    block = src[start:end]

    assert "const eventSid=d.old_session_id||d.session_id||'';" in block
    assert "const continuationSid=(d.session&&d.session.session_id)||d.new_session_id||d.continuation_session_id||'';" in block
    assert "const eventMatchesCurrent=!!(currentSid&&(eventSid===currentSid||continuationSid===currentSid));" in block


def test_apperror_payload_enriched_before_enqueue(tmp_path, monkeypatch):
    class _CaptureQueue:
        def __init__(self):
            self.events = []

        def put_nowait(self, item):
            event, payload = item
            self.events.append((event, payload, copy.deepcopy(payload)))

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir)
    models.SESSIONS.clear()
    streaming.SESSIONS.clear()
    streaming.STREAMS.clear()
    streaming.AGENT_INSTANCES.clear()
    streaming.SESSION_AGENT_LOCKS.clear()

    old_sid = "old_sid_capture"
    new_sid = "new_sid_capture"
    stream_id = "stream-compression-exhausted-capture"
    session = models.Session(
        session_id=old_sid,
        title="Compression test",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=[],
        context_messages=[],
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "Do the long task."
    session.pending_started_at = 1.0
    session.save()
    models.SESSIONS[old_sid] = session
    streaming.SESSIONS[old_sid] = session
    captured = _CaptureQueue()
    streaming.STREAMS[stream_id] = captured

    class FakeAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            api_key=None,
            platform=None,
            quiet_mode=False,
            enabled_toolsets=None,
            fallback_model=None,
            session_id=None,
            session_db=None,
            stream_delta_callback=None,
            reasoning_callback=None,
            tool_progress_callback=None,
            interim_assistant_callback=None,
            clarify_callback=None,
            **kwargs,
        ):
            self.session_id = session_id
            self.stream_delta_callback = stream_delta_callback
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = "Context length exceeded: cannot compress further."

        def run_conversation(self, **kwargs):
            if self.stream_delta_callback:
                self.stream_delta_callback("I am still working through the files.")
            self.session_id = new_sid
            return {
                "failed": True,
                "partial": True,
                "compression_exhausted": True,
                "error": "Context length exceeded: cannot compress further.",
                "messages": [
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {"role": "assistant", "content": "I am still working through the files."},
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
                    {"role": "tool", "tool_call_id": "call_1", "content": "large output"},
                ],
            }

        def interrupt(self, _message):
            return None

    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda *_args, **_kwargs: object()

    with monkeypatch.context() as m:
        m.setattr(streaming, "get_session", lambda _sid: session)
        m.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
        m.setattr(streaming, "resolve_model_provider", lambda *_args, **_kwargs: ("gpt-4o", "openai", None))
        m.setitem(sys.modules, "hermes_state", fake_hermes_state)
        m.setattr("api.config.get_config", lambda *_args, **_kwargs: {})
        m.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
        m.setattr(streaming, "redact_session_data", lambda s: s)

        streaming._run_agent_streaming(
            session_id=old_sid,
            msg_text="Do the long task.",
            model="gpt-4o",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )

    apperror_payloads = [
        (payload, payload_before)
        for event, payload, payload_before in captured.events
        if event == "apperror"
    ]
    assert apperror_payloads, "expected apperror SSE payload"
    payload_after, payload_before = apperror_payloads[-1]
    assert payload_after == payload_before, "apperror payload changed after enqueue"
    assert payload_after["session_id"] == new_sid
    assert payload_after["old_session_id"] == old_sid
    assert payload_after["new_session_id"] == new_sid
    assert payload_after["recommended_recovery_action"] == "start_focused_continuation"
    assert payload_after["compression_recovery"]["recommended_action"] == "start_focused_continuation"


def test_exception_apperror_payload_includes_session_id_before_enqueue():
    src = _read("api/streaming.py")
    start = src.find("_error_payload = _provider_error_payload(err_str, _exc_type, _exc_hint)")
    assert start != -1, "exception apperror payload path not found"
    end = src.find("put('apperror', _error_payload)", start)
    assert end != -1, "exception apperror enqueue not found"
    block = src[start:end]

    assert "_error_payload['session_id'] = getattr(s, 'session_id', session_id)" in block
    assert "_error_payload['old_session_id'] = session_id" in block
