"""#7358: recoverable failed tool results must not stream as ``is_error: false``.

The structured ``tool_complete_callback`` path emits ``is_error: False``
unconditionally, so the browser WebUI and every native client render a failed
tool as **Completed**. The Agent already computes the canonical failure bit with
``agent.display._detect_tool_failure`` and hands it to the sibling
``tool_progress_callback``; only the structured projection drops it.

These tests pin the structured path onto the same canonical classifier.
"""

from __future__ import annotations

import copy
import json
import queue
import sys
import types
import uuid
from unittest import mock

import api.streaming as streaming


_FAILED_RESULT = {"success": False, "error": "HTTP 433: usage allowance exceeded"}
_FAILED_RESULT_JSON = json.dumps(_FAILED_RESULT)
_OK_RESULT = {"success": True, "output": "12 rows"}
# Regression guard for "avoid classifying a result as failed merely because its
# text contains the word error" (issue #7358).
_OK_RESULT_MENTIONING_ERROR = {"success": True, "output": "scanned 3 error logs, none fatal"}


# ── Classifier ────────────────────────────────────────────────────────────


def test_failed_object_result_is_classified_as_error():
    assert streaming._tool_result_is_error("browser_exec", _FAILED_RESULT) is True


def test_failed_json_string_result_is_classified_as_error():
    assert streaming._tool_result_is_error("browser_exec", _FAILED_RESULT_JSON) is True


def test_successful_result_is_not_classified_as_error():
    assert streaming._tool_result_is_error("browser_exec", _OK_RESULT) is False


def test_successful_payload_mentioning_error_is_not_misclassified():
    assert streaming._tool_result_is_error("browser_exec", _OK_RESULT_MENTIONING_ERROR) is False
    assert streaming._tool_result_is_error(
        "browser_exec", {"success": True, "output": "searched 4 error reports"}
    ) is False


def test_failed_status_field_is_classified_as_error():
    assert streaming._tool_result_is_error(
        "web_search", {"status": "failed", "detail": "quota exhausted"}
    ) is True


def test_terminal_non_zero_exit_code_is_classified_as_error():
    assert streaming._tool_result_is_error(
        "terminal", {"exit_code": 1, "stdout": "partial"}
    ) is True


def test_terminal_zero_exit_code_is_not_classified_as_error():
    assert streaming._tool_result_is_error(
        "terminal", {"exit_code": 0, "stdout": "all good"}
    ) is False


def test_empty_error_field_alone_is_not_a_failure():
    # A structured but empty error field carries no failure signal.
    assert streaming._tool_result_is_error(
        "browser_exec", {"success": True, "error": ""}
    ) is False


def test_guardrail_refusal_is_not_reported_as_a_failed_call():
    # Agent core deliberately reports a harness refusal of a redundant call as
    # NOT a failure; the structured rules must not relabel it as Failed.
    assert streaming._tool_result_is_error(
        "browser_exec",
        {"guardrail_refusal": True, "success": False, "error": "repeated identical read"},
    ) is False


def test_landed_file_mutation_is_not_reported_as_failed():
    assert streaming._tool_result_is_error(
        "write_file", json.dumps({"bytes_written": 12, "path": "/tmp/x"})
    ) is False
    assert streaming._tool_result_is_error(
        "patch", json.dumps({"success": True, "status": "error-free"})
    ) is False


def test_none_and_non_json_results_are_not_classified_as_error():
    assert streaming._tool_result_is_error("browser_exec", None) is False
    assert streaming._tool_result_is_error("browser_exec", "plain text output") is False


def test_error_preview_stays_bounded():
    long_error = json.dumps({"success": False, "error": "x" * 50_000})
    snippet = streaming._tool_result_snippet(long_error)
    assert len(snippet) <= streaming._TOOL_RESULT_SNIPPET_MAX


def test_error_preview_prefers_the_error_text():
    snippet = streaming._tool_result_snippet(_FAILED_RESULT)
    assert "usage allowance exceeded" in snippet


# ── Persisted / replayed state ────────────────────────────────────────────


def test_persisted_tool_calls_retain_failed_state():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "browser_exec", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": _FAILED_RESULT_JSON},
    ]
    tool_calls = streaming._extract_tool_calls_from_messages(messages)
    assert [tc["is_error"] for tc in tool_calls] == [True]


def test_persisted_tool_calls_retain_successful_state():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "browser_exec", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(_OK_RESULT)},
    ]
    tool_calls = streaming._extract_tool_calls_from_messages(messages)
    assert [tc["is_error"] for tc in tool_calls] == [False]


def test_live_tool_call_fallback_retains_failed_state():
    live = [{"name": "browser_exec", "args": {}, "tid": "c1", "done": True, "is_error": True}]
    messages = [
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "content": json.dumps(_FAILED_RESULT_JSON)},
    ]
    tool_calls = streaming._extract_tool_calls_from_messages(messages, live)
    assert [tc["is_error"] for tc in tool_calls] == [True]


# ── End-to-end structured-callback SSE projection ─────────────────────────


_MISSING = object()


def _run_turn(structured_tools, final_messages=None):
    """Drive ``_run_agent_streaming`` with a fake Agent; return its SSE events.

    ``structured_tools`` is a list of ``(tool_call_id, name, args, result)``
    tuples delivered through the real ``tool_complete_callback`` so the
    structured projection in ``api/streaming.py`` is what gets exercised.
    """
    run_id = uuid.uuid4().hex[:12]

    class FakeSession:
        def __init__(self):
            self.session_id = f"issue7358_{run_id}"
            self.title = "7358"
            self.workspace = "/tmp"
            self.model = "gpt-test"
            self.model_provider = None
            self.profile = None
            self.personality = None
            self.messages = []
            self.context_messages = []
            self.input_tokens = self.output_tokens = self.estimated_cost = 0
            self.cache_read_tokens = self.cache_write_tokens = 0
            self.tool_calls = []
            self.gateway_routing = None
            self.gateway_routing_history = None
            self.active_stream_id = ""
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = self.threshold_tokens = self.last_prompt_tokens = 0
            self.llm_title_generated = True

        def save(self, *args, **kwargs):
            pass

        def compact(self):
            return {"session_id": self.session_id, "title": self.title, "workspace": "/tmp",
                    "model": "gpt-test", "created_at": 0, "updated_at": 0, "pinned": False,
                    "archived": False, "project_id": None, "profile": None,
                    "input_tokens": 0, "output_tokens": 0, "estimated_cost": 0,
                    "cache_read_tokens": 0, "cache_write_tokens": 0, "model_provider": None,
                    "context_messages": [], "messages": [], "thinking_level": None}

    class ScriptedAgent:
        def __init__(self, model=None, provider=None, base_url=None, platform=None,
                     quiet_mode=False, enabled_toolsets=None, fallback_model=None,
                     session_id=None, session_db=None, prefill_messages=None,
                     stream_delta_callback=None, reasoning_callback=None,
                     tool_progress_callback=None, tool_start_callback=None,
                     tool_complete_callback=None, clarify_callback=None,
                     interim_assistant_callback=None, **_kwargs):
            self.cb = {"reasoning": reasoning_callback, "progress": tool_progress_callback,
                       "start": tool_start_callback, "complete": tool_complete_callback,
                       "token": stream_delta_callback, "interim": interim_assistant_callback}
            self.context_compressor = None
            self.session_prompt_tokens = self.session_completion_tokens = 0
            self.session_estimated_cost_usd = 0
            self.session_cache_read_tokens = self.session_cache_write_tokens = 0
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            for tool_call_id, name, args, result in structured_tools:
                # tool_executor order: progress 'tool.started', then the
                # structured start callback, then the structured complete one.
                self.cb["progress"]("tool.started", name, "args", args)
                self.cb["start"](tool_call_id, name, args)
                self.cb["complete"](tool_call_id, name, args, result)
            if final_messages is not None:
                return {"messages": kwargs.get("conversation_history", []) + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                ] + copy.deepcopy(final_messages)}
            return {"messages": kwargs.get("conversation_history", []) + [
                {"role": "user", "content": kwargs["persist_user_message"]},
                {"role": "assistant", "content": "done", "reasoning": None},
            ]}

        def interrupt(self, _message):
            pass

    fake_session = FakeSession()
    stream_id = f"stream_issue7358_{run_id}"
    fake_session.active_stream_id = stream_id
    runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    runtime_payload = {"provider": "openai", "base_url": None, "api_mode": "chat_completions",
                       "command": None, "args": [], "credential_pool": None}
    runtime_module.__dict__["resolve_runtime_provider"] = mock.Mock(return_value=runtime_payload)
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__dict__["runtime_provider"] = runtime_module
    hermes_state = types.ModuleType("hermes_state")
    hermes_state.__dict__["SessionDB"] = mock.Mock(return_value=None)
    injected = {"hermes_cli": hermes_cli, "hermes_cli.runtime_provider": runtime_module,
                "hermes_state": hermes_state}
    saved_mods = {k: sys.modules.get(k, _MISSING) for k in injected}
    sys.modules.update(injected)
    q = queue.Queue()
    try:
        with mock.patch.object(streaming, "get_session", return_value=fake_session), \
             mock.patch.object(streaming, "_get_ai_agent", return_value=ScriptedAgent), \
             mock.patch.object(streaming, "resolve_model_provider",
                               return_value=("gpt-test", "openai", None)), \
             mock.patch("api.config.get_config", return_value={}), \
             mock.patch("api.config._resolve_cli_toolsets", return_value=[]):
            streaming.STREAMS[stream_id] = q
            streaming._run_agent_streaming(session_id=fake_session.session_id, msg_text="go",
                                           model="gpt-test", workspace="/tmp",
                                           stream_id=stream_id)
    finally:
        streaming.STREAMS.pop(stream_id, None)
        for k, prev in saved_mods.items():
            if prev is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = prev

    events = []
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            break
        events.append((item[0], item[1]))
    return events


def _tool_completed_events(events):
    return [data for name, data in events
            if name == "tool_complete" and isinstance(data, dict)
            and data.get("event_type") == "tool.completed"]


def test_structured_callback_streams_is_error_true_for_failed_result():
    events = _run_turn([("c1", "browser_exec", {}, _FAILED_RESULT)])
    completed = _tool_completed_events(events)
    assert len(completed) == 1
    assert completed[0]["is_error"] is True, (
        "A structured tool result that represents a failure must stream "
        "is_error: true so clients render Failed, not Completed."
    )


def test_structured_callback_streams_is_error_false_for_successful_result():
    events = _run_turn([("c1", "browser_exec", {}, _OK_RESULT)])
    completed = _tool_completed_events(events)
    assert len(completed) == 1
    assert completed[0]["is_error"] is False


def test_structured_callback_keeps_the_stable_tool_call_id():
    # The structured path exists for its stable tid; the fix must not drop it.
    events = _run_turn([("c1", "browser_exec", {}, _FAILED_RESULT)])
    completed = _tool_completed_events(events)
    assert completed[0]["tid"] == "c1"


def test_failed_tool_then_successful_workaround_keeps_earlier_failure():
    events = _run_turn([
        ("c1", "browser_exec", {}, _FAILED_RESULT),
        ("c2", "browser_exec", {}, _OK_RESULT),
    ])
    completed = _tool_completed_events(events)
    assert [tc["tid"] for tc in completed] == ["c1", "c2"]
    assert [tc["is_error"] for tc in completed] == [True, False], (
        "Recovering through a different tool must not rewrite the earlier "
        "failed operation as successful."
    )


def test_json_string_failure_streams_is_error_true():
    events = _run_turn([("c1", "browser_exec", {}, _FAILED_RESULT_JSON)])
    completed = _tool_completed_events(events)
    assert completed[0]["is_error"] is True


def test_structured_error_preview_stays_bounded():
    long_error = json.dumps({"success": False, "error": "y" * 50_000})
    events = _run_turn([("c1", "browser_exec", {}, long_error)])
    completed = _tool_completed_events(events)
    assert completed[0]["is_error"] is True
    assert len(completed[0]["preview"]) <= streaming._TOOL_RESULT_SNIPPET_MAX


def test_structured_path_does_not_hardcode_is_error_false():
    """Guard the literal that caused #7358 from creeping back in."""
    src = streaming.__file__
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    start = text.find("def on_tool_complete(tool_call_id, name, args, function_result)")
    assert start != -1
    end = text.find("\n            def ", start + 1)
    block = text[start:end]
    assert "'is_error': False," not in block, (
        "The structured tool completion must classify the result instead of "
        "emitting a literal is_error: False (#7358)."
    )