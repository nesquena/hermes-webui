"""Regression coverage for compression-exhausted stream finalization."""

import copy
import json
import queue
import sys
import types
from pathlib import Path

from api import models, streaming
from api.models import Session
from api.streaming import (
    _agent_result_terminal_failure,
    _session_lacks_final_assistant_answer,
)

ROOT = Path(__file__).resolve().parents[1]


def _read(relpath: str) -> str:
    return (ROOT / relpath).read_text(encoding="utf-8")


def test_compression_exhausted_after_session_rotation_preserves_snapshot_and_errors_on_continuation(
    tmp_path, monkeypatch
):
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
    old_sid = "old_sid"
    new_sid = "new_sid"
    stream_id = "stream-compression-exhausted"
    session = Session(
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

    old_payload = json.loads((session_dir / f"{old_sid}.json").read_text(encoding="utf-8"))
    new_payload = json.loads((session_dir / f"{new_sid}.json").read_text(encoding="utf-8"))
    assert old_payload["pre_compression_snapshot"] is True
    assert old_payload["active_stream_id"] is None
    assert old_payload["pending_user_message"] is None
    assert new_payload["session_id"] == new_sid
    assert new_payload["parent_session_id"] == old_sid
    assert new_payload["pre_compression_snapshot"] is False
    assert new_payload["messages"][-1]["_error"] is True
    assert "Context compression exhausted" in new_payload["messages"][-1]["content"]
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


def test_exception_apperror_payload_includes_session_id_before_enqueue():
    src = _read("api/streaming.py")
    start = src.find("_error_payload = _provider_error_payload(err_str, _exc_type, _exc_hint)")
    assert start != -1, "exception apperror payload path not found"
    end = src.find("put('apperror', _error_payload)", start)
    assert end != -1, "exception apperror enqueue not found"
    block = src[start:end]

    assert "_error_payload['session_id'] = getattr(s, 'session_id', session_id)" in block
    assert "_error_payload['old_session_id'] = session_id" in block
