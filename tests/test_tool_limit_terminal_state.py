import json
import queue
import sys
import types
from pathlib import Path

from api import models
from api import streaming
from api.models import Session


ROOT = Path(__file__).resolve().parents[1]


def _run_streaming_with_fake_agent(
    tmp_path,
    monkeypatch,
    agent_result,
    *,
    prior_messages=None,
    prior_context_messages=None,
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
    streaming.PENDING_GOAL_CONTINUATION.clear()
    try:
        from api.config import SESSION_AGENT_CACHE

        SESSION_AGENT_CACHE.clear()
    except Exception:
        pass

    session_id = "tool_limit_session"
    stream_id = "stream-tool-limit"
    session = Session(
        session_id=session_id,
        title="Tool limit test",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=list(prior_messages or []),
        context_messages=list(prior_context_messages or []),
    )
    session.active_stream_id = stream_id
    session.pending_user_message = "Do the long task."
    session.pending_started_at = 1.0
    session.save()
    models.SESSIONS[session_id] = session
    streaming.SESSIONS[session_id] = session
    event_queue = queue.Queue()
    streaming.STREAMS[stream_id] = event_queue

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

        def run_conversation(self, **kwargs):
            return agent_result

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
            session_id=session_id,
            msg_text="Do the long task.",
            model="gpt-4o",
            workspace=str(tmp_path),
            stream_id=stream_id,
        )

    events = []
    while not event_queue.empty():
        events.append(event_queue.get_nowait())
    payload = json.loads((session_dir / f"{session_id}.json").read_text(encoding="utf-8"))
    return events, payload


def test_synthetic_max_iteration_summary_request_is_dropped_from_agent_result():
    synthetic = {
        "role": "user",
        "content": streaming._MAX_ITERATION_SUMMARY_REQUEST,
    }
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        synthetic,
        {"role": "assistant", "content": "I reached the limit; here is the summary."},
    ]
    result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "messages": messages,
    }

    assert streaming._agent_result_tool_limit_reached(result) is True

    cleaned = streaming._drop_synthetic_max_iteration_summary_requests(
        result["messages"],
        enabled=streaming._agent_result_tool_limit_reached(result),
    )

    assert synthetic not in cleaned
    assert cleaned[-1]["role"] == "assistant"
    assert "here is the summary" in cleaned[-1]["content"]


def test_tool_limit_detection_uses_explicit_boolean_grouping():
    streaming_py = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")

    assert "or ('tool-calling iterations' in haystack and 'maximum' in haystack)" in streaming_py


def test_historical_synthetic_summary_prompt_does_not_mark_normal_result_as_tool_limit():
    result = {
        "messages": [
            {"role": "user", "content": "Earlier task."},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
            {"role": "user", "content": "Current normal task."},
            {"role": "assistant", "content": "Current task completed normally."},
        ],
    }

    assert streaming._agent_result_tool_limit_reached(result) is False


def test_tool_limit_with_final_answer_marks_latest_assistant_status_card():
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "I reached the limit; here is the summary."},
    ]

    assert streaming._session_lacks_final_assistant_answer(messages) is False
    assert streaming._mark_latest_assistant_tool_limit_status(messages) is True

    assistant = messages[-1]
    assert assistant["_terminal_state"] == "tool_limit_reached"
    assert assistant["_terminal_reason"] == "max_iterations"
    assert assistant["_statusCard"]["title"] == "Tool iteration limit reached"


def test_tool_limit_without_final_answer_is_no_final_terminal_state_after_filtering():
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
    ]

    cleaned = streaming._drop_synthetic_max_iteration_summary_requests(messages)

    assert all(
        not streaming._is_synthetic_max_iteration_summary_request(message)
        for message in cleaned
    )
    assert streaming._session_lacks_final_assistant_answer(cleaned) is True


def test_display_merge_does_not_render_synthetic_summary_prompt():
    previous_display = [{"role": "user", "content": "Do the long task."}]
    previous_context = [{"role": "user", "content": "Do the long task."}]
    result_messages = previous_context + [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
        {"role": "assistant", "content": "I reached the limit; here is the summary."},
    ]
    result_messages = streaming._drop_synthetic_max_iteration_summary_requests(
        result_messages,
        enabled=True,
    )

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "Do the long task.",
    )

    assert all(
        message.get("content") != streaming._MAX_ITERATION_SUMMARY_REQUEST
        for message in merged
    )
    assert merged[-1]["role"] == "assistant"
    assert "here is the summary" in merged[-1]["content"]


def test_frontend_handles_tool_limit_apperror_label():
    messages_js = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
    start = messages_js.find("source.addEventListener('apperror'")
    end = messages_js.find("source.addEventListener('warning'", start)
    assert start != -1 and end != -1
    block = messages_js[start:end]

    assert "const isToolLimitReached=d.type==='tool_limit_reached';" in block
    assert "Tool iteration limit reached" in block
    assert "Terminal state details" in block


def test_streaming_tool_limit_with_final_answer_persists_clean_done_state(tmp_path, monkeypatch):
    result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
            {"role": "assistant", "content": "I reached the limit; here is the summary."},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    done_payloads = [payload for event, payload in events if event == "done"]
    assert done_payloads, "expected done SSE payload"
    assert done_payloads[-1]["terminal_state"] == "tool_limit_reached"
    assert done_payloads[-1]["terminal_reason"] == "max_iterations"
    assert all(
        message.get("content") != streaming._MAX_ITERATION_SUMMARY_REQUEST
        for message in payload["messages"]
    )
    assert all(
        message.get("content") != streaming._MAX_ITERATION_SUMMARY_REQUEST
        for message in payload["context_messages"]
    )
    assistant = payload["messages"][-1]
    assert assistant["role"] == "assistant"
    assert assistant["_terminal_state"] == "tool_limit_reached"
    assert assistant["_statusCard"]["title"] == "Tool iteration limit reached"


def test_streaming_tool_limit_without_final_answer_emits_no_final_apperror(tmp_path, monkeypatch):
    result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    apperror_payloads = [payload for event, payload in events if event == "apperror"]
    assert apperror_payloads, "expected apperror SSE payload"
    assert apperror_payloads[-1]["type"] == "tool_limit_reached"
    assert apperror_payloads[-1]["terminal_state"] == "tool_limit_reached"
    assert payload["messages"][-1]["_error"] is True
    assert "Tool iteration limit reached" in payload["messages"][-1]["content"]
    assert all(
        message.get("content") != streaming._MAX_ITERATION_SUMMARY_REQUEST
        for message in payload["messages"]
    )


def test_streaming_tool_limit_terminal_failure_does_not_mark_final_answer(tmp_path, monkeypatch):
    result = {
        "status": "partial",
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "I reached the limit; here is the summary."},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    apperror_payloads = [payload for event, payload in events if event == "apperror"]
    assert apperror_payloads, "expected terminal-failure apperror"
    assert apperror_payloads[-1]["type"] == "tool_limit_reached"
    assert not [payload for event, payload in events if event == "done"]
    assistant = next(
        message
        for message in payload["messages"]
        if message.get("role") == "assistant"
        and message.get("content") == "I reached the limit; here is the summary."
    )
    assert "_terminal_state" not in assistant
    assert "_statusCard" not in assistant
    assert payload["messages"][-1]["_error"] is True


def test_streaming_historical_synthetic_prompt_normal_result_does_not_emit_tool_limit(tmp_path, monkeypatch):
    result = {
        "messages": [
            {"role": "user", "content": "Earlier task."},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "Current task completed normally."},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    assert not [payload for event, payload in events if event == "apperror"]
    done_payloads = [payload for event, payload in events if event == "done"]
    assert done_payloads, "expected normal done SSE payload"
    assert "terminal_state" not in done_payloads[-1]
    assert payload["messages"][-1]["role"] == "assistant"
    assert payload["messages"][-1]["content"] == "Current task completed normally."


def test_streaming_empty_result_messages_do_not_treat_prior_assistant_as_current_answer(tmp_path, monkeypatch):
    prior = [
        {"role": "user", "content": "Earlier task."},
        {"role": "assistant", "content": "Earlier answer."},
    ]
    result = {"messages": []}

    events, payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        result,
        prior_messages=prior,
        prior_context_messages=prior,
    )

    apperror_payloads = [payload for event, payload in events if event == "apperror"]
    assert apperror_payloads, "expected silent-failure apperror"
    assert apperror_payloads[-1]["type"] == "no_response"
    assert not [payload for event, payload in events if event == "done"]
    assert payload["messages"][-1]["_error"] is True
