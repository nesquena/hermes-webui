import json
import queue
import sys
import types
from pathlib import Path

import pytest

from api import models
from api.process_event_utils import build_active_turn_token
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
    msg_text="Do the long task.",
    pending_started_at=1.0,
    current_turn_user_idx=None,
    turn_id="turn-current",
    agent_results=None,
    enable_auth_retry=False,
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
    session_messages = list(prior_messages or [])
    session_context_messages = list(prior_context_messages or [])
    for history in (session_messages, session_context_messages):
        for message in history:
            if isinstance(message, dict) and message.get("role") == "user":
                message.setdefault("timestamp", 1.0)
                message.setdefault("_source", "webui")
                message.setdefault("attachments", [])
    session = Session(
        session_id=session_id,
        title="Tool limit test",
        workspace=str(tmp_path),
        model="gpt-4o",
        messages=session_messages,
        context_messages=session_context_messages,
    )
    session.active_stream_id = stream_id
    session.pending_user_message = msg_text
    session.pending_started_at = pending_started_at
    session.save()
    models.SESSIONS[session_id] = session
    streaming.SESSIONS[session_id] = session
    event_queue = queue.Queue()
    streaming.STREAMS[stream_id] = event_queue
    result_queue = list(agent_results or [])

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
            self._persist_user_message_idx = current_turn_user_idx
            self._current_turn_id = turn_id if current_turn_user_idx is not None else ""

        def run_conversation(self, **kwargs):
            if result_queue:
                next_result = result_queue.pop(0)
                if isinstance(next_result, BaseException):
                    raise next_result
                return next_result
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
        if enable_auth_retry:
            m.setattr(
                streaming,
                "_attempt_credential_self_heal",
                lambda *_args, **_kwargs: {"api_key": "retry-key", "provider": "openai"},
            )
        streaming._run_agent_streaming(
            session_id=session_id,
            msg_text=msg_text,
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


@pytest.mark.parametrize("marker", ["_verification_stop_synthetic", "_pre_verify_synthetic"])
@pytest.mark.parametrize("delta_only", [False, True])
def test_verification_nudge_is_removed_while_corrective_followup_persists(
    tmp_path,
    monkeypatch,
    marker,
    delta_only,
):
    corrective = "Verification failed. I fixed the parser and reran the tests."
    prior_turn = [
        {"role": "user", "content": "Fix the failing test."},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "terminal"}]},
        {
            "role": "tool",
            "tool_call_id": "terminal",
            "content": "Tests passed.",
            "verification_evidence": {
                "status": "passed",
                "kind": "terminal",
                "scope": "workspace",
                "canonical_command": "pytest tests/test_app.py -q",
            },
        },
        {"role": "assistant", "content": "The failing test is fixed."},
    ]
    result_messages = [
        {"role": "user", "content": "[System: verify the workspace]", marker: True},
        {"role": "assistant", "content": corrective},
    ]
    if not delta_only:
        result_messages = prior_turn + result_messages
    result = {"messages": result_messages}

    _events, payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        result,
        prior_messages=prior_turn,
        prior_context_messages=prior_turn,
        msg_text="Fix the failing test.",
        current_turn_user_idx=0,
    )

    expected_sequence = [
        (message["role"], message.get("content"))
        for message in prior_turn
    ] + [("assistant", corrective)]
    for field in ("messages", "context_messages"):
        serialized = json.dumps(payload[field])
        assert marker not in serialized
        assert "[System: verify the workspace]" not in serialized
        assert [
            (message["role"], message.get("content"))
            for message in payload[field]
        ] == expected_sequence
        assert [message.get("content") for message in payload[field]].count(
            "Fix the failing test."
        ) == 1


@pytest.mark.parametrize("marker", ["_verification_stop_synthetic", "_pre_verify_synthetic"])
def test_deferred_repeated_old_prompt_marked_followup_materializes_current_turn(
    tmp_path,
    monkeypatch,
    marker,
):
    prompt = "Fix the failing test."
    old_turn = [
        {"role": "user", "content": prompt, "timestamp": 1.0},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]
    corrective = "Verification failed. I fixed the parser and reran the tests."
    result = {
        "messages": old_turn + [
            {"role": "user", "content": "[System: verify the workspace]", marker: True},
            {"role": "assistant", "content": corrective},
        ],
    }

    _events, payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        result,
        prior_messages=old_turn,
        prior_context_messages=old_turn,
        msg_text=prompt,
        pending_started_at=2.0,
        current_turn_user_idx=len(old_turn),
    )

    expected = [
        ("user", prompt),
        ("assistant", "The earlier attempt is complete."),
        ("user", prompt),
        ("assistant", corrective),
    ]
    for field in ("messages", "context_messages"):
        assert [(message["role"], message.get("content")) for message in payload[field]] == expected
        assert [message.get("content") for message in payload[field]].count(prompt) == 2
        assert marker not in json.dumps(payload[field])


@pytest.mark.parametrize("marker", ["_verification_stop_synthetic", "_pre_verify_synthetic"])
def test_same_second_repeated_prompt_keeps_current_user_turn(tmp_path, monkeypatch, marker):
    prompt = "Fix the failing test."
    old_turn = [
        {"role": "user", "content": prompt, "timestamp": 1.1},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]
    corrective = "Verification failed. I fixed the parser and reran the tests."
    result = {
        "messages": old_turn + [
            {"role": "user", "content": "[System: verify the workspace]", marker: True},
            {"role": "assistant", "content": corrective},
        ],
    }

    _events, payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        result,
        prior_messages=old_turn,
        prior_context_messages=old_turn,
        msg_text=prompt,
        pending_started_at=1.9,
        current_turn_user_idx=len(old_turn),
    )

    expected = [
        ("user", prompt),
        ("assistant", "The earlier attempt is complete."),
        ("user", prompt),
        ("assistant", corrective),
    ]
    for field in ("messages", "context_messages"):
        assert [(message["role"], message.get("content")) for message in payload[field]] == expected
        assert [message.get("content") for message in payload[field]].count(prompt) == 2
        assert marker not in json.dumps(payload[field])


@pytest.mark.parametrize("marker", ["_verification_stop_synthetic", "_pre_verify_synthetic"])
def test_eager_exact_checkpoint_does_not_duplicate_repeated_prompt(
    tmp_path,
    monkeypatch,
    marker,
):
    prompt = "Fix the failing test."
    old_turn = [
        {"role": "user", "content": prompt, "timestamp": 1.0},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]
    current_checkpoint = [
        {
            "role": "user",
            "content": prompt,
            "timestamp": 2.0,
            "_source": "webui",
            "attachments": [],
            "_active_turn_token": "stream-tool-limit:2",
        },
    ]
    corrective = "Verification failed. I fixed the parser and reran the tests."
    result = {
        "messages": [
            {"role": "user", "content": "[System: verify the workspace]", marker: True},
            {"role": "assistant", "content": corrective},
        ]
    }

    _events, payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        result,
        prior_messages=old_turn,
        prior_context_messages=old_turn + current_checkpoint,
        msg_text=prompt,
        pending_started_at=2.0,
        current_turn_user_idx=2,
    )

    expected = [
        ("user", prompt),
        ("assistant", "The earlier attempt is complete."),
        ("user", prompt),
        ("assistant", corrective),
    ]
    for field in ("messages", "context_messages"):
        assert [(message["role"], message.get("content")) for message in payload[field]] == expected
        assert [message.get("content") for message in payload[field]].count(prompt) == 2
        assert marker not in json.dumps(payload[field])


@pytest.mark.parametrize("marker", ["_verification_stop_synthetic", "_pre_verify_synthetic"])
def test_eager_checkpoint_reused_by_exact_token(tmp_path, monkeypatch, marker):
    prompt = "Fix the failing test."
    old_turn = [
        {"role": "user", "content": prompt, "timestamp": 1.1},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]
    current_checkpoint = [
        {
            "role": "user",
            "content": prompt,
            "timestamp": 1.9,
            "_source": "webui",
            "attachments": [],
            "_active_turn_token": build_active_turn_token("stream-tool-limit", 1.9),
        },
    ]
    corrective = "Verification failed. I fixed the parser and reran the tests."
    result = {
        "messages": [
            {"role": "user", "content": "[System: verify the workspace]", marker: True},
            {"role": "assistant", "content": corrective},
        ]
    }

    _events, payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        result,
        prior_messages=old_turn,
        prior_context_messages=old_turn + current_checkpoint,
        msg_text=prompt,
        pending_started_at=1.9,
        current_turn_user_idx=2,
    )

    expected = [
        ("user", prompt),
        ("assistant", "The earlier attempt is complete."),
        ("user", prompt),
        ("assistant", corrective),
    ]
    for field in ("messages", "context_messages"):
        assert [(message["role"], message.get("content")) for message in payload[field]] == expected
        assert [message.get("content") for message in payload[field]].count(prompt) == 2
        assert marker not in json.dumps(payload[field])


@pytest.mark.parametrize("marker", ["_verification_stop_synthetic", "_pre_verify_synthetic"])
def test_tool_limit_exit_uses_shared_settlement_owner(tmp_path, monkeypatch, marker):
    prompt = "Fix the failing test."
    old_turn = [
        {"role": "user", "content": prompt, "timestamp": 1.1},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]
    corrective = "Verification failed. I fixed the parser and reran the tests."
    result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "messages": old_turn + [
            {"role": "user", "content": "[System: verify the workspace]", marker: True},
            {"role": "assistant", "content": corrective},
        ],
    }
    settle_calls = []
    original = streaming._settle_result_messages

    def _spy(*args, **kwargs):
        settle_calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(streaming, "_settle_result_messages", _spy)
    _events, payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        result,
        prior_messages=old_turn,
        prior_context_messages=old_turn,
        msg_text=prompt,
        pending_started_at=1.9,
        current_turn_user_idx=len(old_turn),
    )

    assert len(settle_calls) == 1
    assert [(message["role"], message.get("content")) for message in payload["messages"]] == [
        ("user", prompt),
        ("assistant", "The earlier attempt is complete."),
        ("user", prompt),
        ("assistant", corrective),
    ]


@pytest.mark.parametrize("marker", ["_verification_stop_synthetic", "_pre_verify_synthetic"])
def test_shared_settlement_owner_handles_exception_style_delta(tmp_path, monkeypatch, marker):
    prompt = "Fix the failing test."
    old_turn = [
        {"role": "user", "content": prompt, "timestamp": 1.1},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]
    corrective = "Verification failed. I fixed the parser and reran the tests."
    heal_result = {
        "messages": old_turn + [
            {"role": "user", "content": "[System: verify the workspace]", marker: True},
            {"role": "assistant", "content": corrective},
        ],
    }
    session = types.SimpleNamespace(messages=list(old_turn), context_messages=list(old_turn))
    streaming._settle_result_messages(
        session,
        old_turn,
        old_turn,
        heal_result["messages"],
        prompt,
        "webui",
        {
            "token": "stream-tool-limit:1.9",
            "text": prompt,
            "timestamp": 1.9,
            "source": "webui",
            "attachments": [],
            "current_turn_user_idx": len(old_turn),
            "turn_id": "turn-current",
        },
    )

    assert [(message["role"], message.get("content")) for message in session.messages] == [
        ("user", prompt),
        ("assistant", "The earlier attempt is complete."),
        ("user", prompt),
        ("assistant", corrective),
    ]


def test_missing_agent_authority_fails_closed(tmp_path, monkeypatch):
    prompt = "Fix the failing test."
    old_turn = [
        {"role": "user", "content": prompt, "timestamp": 1.1},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]
    result = {
        "messages": old_turn + [
            {"role": "user", "content": "[System: verify the workspace]", "_verification_stop_synthetic": True},
            {"role": "assistant", "content": "Verification failed. I fixed the parser and reran the tests."},
        ],
    }

    _events, payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        result,
        prior_messages=old_turn,
        prior_context_messages=old_turn,
        msg_text=prompt,
        pending_started_at=1.9,
        current_turn_user_idx=None,
    )

    assert [(message["role"], message.get("content")) for message in payload["messages"]] == [
        ("user", prompt),
        ("assistant", "The earlier attempt is complete."),
        ("user", prompt),
        ("assistant", "Verification failed. I fixed the parser and reran the tests."),
    ]


@pytest.mark.parametrize("marker", ["_verification_stop_synthetic", "_pre_verify_synthetic"])
def test_synthetic_only_verification_delta_preserves_prior_histories(tmp_path, monkeypatch, marker):
    prior_turn = [
        {
            "role": "user",
            "content": "Fix the failing test.",
            "_active_turn_token": build_active_turn_token("stream-tool-limit", 1.0),
        },
        {"role": "assistant", "content": [{"type": "tool_use", "name": "terminal"}]},
        {
            "role": "tool",
            "tool_call_id": "terminal",
            "content": "Tests passed.",
            "verification_evidence": {
                "status": "passed",
                "kind": "terminal",
                "scope": "workspace",
                "canonical_command": "pytest tests/test_app.py -q",
            },
        },
        {"role": "assistant", "content": "The failing test is fixed."},
    ]
    result = {
        "messages": [
            {"role": "user", "content": "[System: verify the workspace]", marker: True},
        ],
    }

    _events, payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        result,
        prior_messages=prior_turn,
        prior_context_messages=prior_turn,
        msg_text="Fix the failing test.",
        current_turn_user_idx=0,
    )

    for field in ("messages", "context_messages"):
        assert payload[field] == payload["messages"]
        serialized = json.dumps(payload[field])
        assert marker not in serialized
        assert "[System: verify the workspace]" not in serialized
        assert "The failing test is fixed." in serialized


def test_streaming_tool_limit_partial_without_final_answer_emits_no_final_apperror(tmp_path, monkeypatch):
    result = {
        "status": "partial",
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


def test_streaming_tool_limit_with_fallback_final_response_surfaces_closure_text(tmp_path, monkeypatch):
    """#5494 — handle_max_iterations() guarantees a non-empty ``final_response``
    on iteration-limit exhaustion. This test pins the WebUI contract that,
    when ``messages`` ends without a final assistant turn and ``final_response``
    is set, the user sees that closure text instead of a bare
    ``tool_limit_reached`` error. Serves both as a live-bug fix pin and as a
    regression guard for the agent's "delivered final_response ⇒ assistant
    row" invariant: if a future agent regression drops that invariant, this
    test still passes because the WebUI honors the contract locally.
    """
    graceful = "I reached the iteration limit and couldn't generate a summary."
    result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "final_response": graceful,
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    # The user sees the graceful fallback, not a bare tool_limit_reached error.
    # Either (a) we synthesized the fallback here, or (b) the agent guarantee
    # already added an assistant row and `_mark_latest_assistant_tool_limit_status`
    # attached the status card. Both routes satisfy the contract.
    assert not [
        ap for ev, ap in events if ev == "apperror"
        and ap.get("type") == "tool_limit_reached"
    ], "expected no tool_limit_reached apperror when fallback was returned"
    done_payloads = [payload for event, payload in events if event == "done"]
    assert done_payloads, "expected done SSE payload"
    assert done_payloads[-1]["terminal_state"] == "tool_limit_reached"

    # Fallback text is shown as a final assistant message and is annotated
    # with the status card so the UI can render the 'limit reached' chip.
    assistant = payload["messages"][-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == graceful
    assert assistant["_terminal_state"] == "tool_limit_reached"
    assert assistant["_statusCard"]["title"] == "Tool iteration limit reached"
    # Synthetic scaffolding turn was still dropped, even after fallback injection.
    assert all(
        message.get("content") != streaming._MAX_ITERATION_SUMMARY_REQUEST
        for message in payload["messages"]
    )


def test_streaming_tool_limit_with_fallback_does_not_double_inject_when_assistant_exists(tmp_path, monkeypatch):
    """#5494 — when the agent already appended a model-generated summary
    AND ``final_response`` carries the same text, the WebUI must not duplicate
    the assistant turn. Pins the no-op contract on the synthesis path.
    """
    summary = "I reached the limit; here is the summary."
    result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "final_response": summary,
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "user", "content": streaming._MAX_ITERATION_SUMMARY_REQUEST},
            {"role": "assistant", "content": summary},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    done_payloads = [payload for event, payload in events if event == "done"]
    assert done_payloads
    assistant_msgs = [
        m for m in payload["messages"]
        if m.get("role") == "assistant" and m.get("content") == summary
    ]
    assert len(assistant_msgs) == 1, "fallback must not duplicate the existing summary"
    assert assistant_msgs[0]["_terminal_state"] == "tool_limit_reached"


def test_maybe_inject_max_iteration_summary_fallback_unit():
    """Unit-level coverage for the injection helper."""
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ]
    graceful = "I reached the iteration limit and couldn't generate a summary."
    result = {"final_response": graceful}

    injected = streaming._maybe_inject_max_iteration_summary_fallback(messages, result)

    assert injected[-1]["role"] == "assistant"
    assert injected[-1]["content"] == graceful
    assert injected[-1]["_max_iteration_summary_fallback"] is True


def test_maybe_inject_max_iteration_summary_fallback_skips_when_assistant_present():
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "real summary"},
    ]
    result = {"final_response": "fallback text"}

    out = streaming._maybe_inject_max_iteration_summary_fallback(messages, result)
    assert out == messages


def test_maybe_inject_max_iteration_summary_fallback_skips_when_no_fallback():
    messages = [
        {"role": "user", "content": "Do the long task."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
    ]

    out = streaming._maybe_inject_max_iteration_summary_fallback(messages, {})
    assert out == messages

    out = streaming._maybe_inject_max_iteration_summary_fallback(
        messages, {"final_response": "   "}
    )
    assert out == messages

    out = streaming._maybe_inject_max_iteration_summary_fallback(messages, None)
    assert out == messages


def test_streaming_tool_limit_partial_with_final_answer_suppresses_false_no_response(
    tmp_path,
    monkeypatch,
):
    result = {
        "status": "partial",
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "messages": [
            {"role": "user", "content": "Do the long task."},
            {"role": "assistant", "content": "I reached the limit; here is the summary."},
        ],
    }

    events, payload = _run_streaming_with_fake_agent(tmp_path, monkeypatch, result)

    assert not [event_payload for event, event_payload in events if event == "apperror"]
    done_payloads = [event_payload for event, event_payload in events if event == "done"]
    assert done_payloads, "expected done SSE payload"
    assert done_payloads[-1]["terminal_state"] == "tool_limit_reached"
    assert done_payloads[-1]["terminal_reason"] == "max_iterations"
    assistant = next(
        message
        for message in payload["messages"]
        if message.get("role") == "assistant"
        and message.get("content") == "I reached the limit; here is the summary."
    )
    assert assistant["_terminal_state"] == "tool_limit_reached"
    assert assistant["_terminal_reason"] == "max_iterations"
    assert assistant["_statusCard"]["title"] == "Tool iteration limit reached"
    assert payload["messages"][-1] is assistant


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


def test_streaming_empty_result_same_text_does_not_treat_prior_assistant_as_current_answer(
    tmp_path,
    monkeypatch,
):
    prompt = "Fix the failing test."
    prior = [
        {"role": "user", "content": prompt, "timestamp": 1.1},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]
    result = {"messages": []}

    events, payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        result,
        prior_messages=prior,
        prior_context_messages=prior,
        msg_text=prompt,
        pending_started_at=1.9,
        current_turn_user_idx=len(prior),
    )

    apperror_payloads = [event_payload for event, event_payload in events if event == "apperror"]
    assert apperror_payloads, "expected silent-failure apperror"
    assert apperror_payloads[-1]["type"] == "no_response"
    assert not [event_payload for event, event_payload in events if event == "done"]
    assert [(message["role"], message.get("content")) for message in payload["messages"][:-1]] == [
        ("user", prompt),
        ("assistant", "The earlier attempt is complete."),
        ("user", prompt),
    ]
    assert payload["messages"][-1]["role"] == "assistant"
    assert payload["messages"][-1]["_error"] is True


def test_streaming_empty_result_without_exact_checkpoint_materializes_current_user(
    tmp_path,
    monkeypatch,
):
    prompt = "Fix the failing test."
    prior = [
        {"role": "user", "content": prompt, "timestamp": 1.1},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]
    result = {"messages": []}

    events, payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        result,
        prior_messages=prior,
        prior_context_messages=prior,
        msg_text=prompt,
        pending_started_at=1.9,
        current_turn_user_idx=0,
    )

    apperror_payloads = [event_payload for event, event_payload in events if event == "apperror"]
    assert apperror_payloads, "expected silent-failure apperror"
    assert apperror_payloads[-1]["type"] == "no_response"
    assert not [event_payload for event, event_payload in events if event == "done"]
    assert [(message["role"], message.get("content")) for message in payload["messages"][:-1]] == [
        ("user", prompt),
        ("assistant", "The earlier attempt is complete."),
        ("user", prompt),
    ]
    assert payload["messages"][0].get("_active_turn_token") is None
    assert payload["messages"][2]["_active_turn_token"] == build_active_turn_token(
        "stream-tool-limit",
        1.9,
    )
    assert payload["messages"][-1]["role"] == "assistant"
    assert payload["messages"][-1]["_error"] is True


def test_streaming_empty_result_divergent_context_index_does_not_reuse_historical_assistant(
    tmp_path,
    monkeypatch,
):
    prompt = "Fix the failing test."
    current_token = build_active_turn_token("stream-tool-limit", 1.9)
    prior = [
        {"role": "user", "content": prompt, "timestamp": 1.0},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]
    prior_context = [
        {"role": "user", "content": prompt, "timestamp": 1.0, "attachments": []},
        {
            "role": "user",
            "content": prompt,
            "timestamp": 1.9,
            "attachments": [],
            "_active_turn_token": current_token,
        },
    ]
    result = {"messages": []}

    events, payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        result,
        prior_messages=prior,
        prior_context_messages=prior_context,
        msg_text=prompt,
        pending_started_at=1.9,
        current_turn_user_idx=0,
    )

    apperror_payloads = [event_payload for event, event_payload in events if event == "apperror"]
    assert apperror_payloads, "expected silent-failure apperror"
    assert apperror_payloads[-1]["type"] == "no_response"
    assert not [event_payload for event, event_payload in events if event == "done"]
    assert [(message["role"], message.get("content")) for message in payload["messages"][:-1]] == [
        ("user", prompt),
        ("assistant", "The earlier attempt is complete."),
        ("user", prompt),
    ]
    assert payload["messages"][0].get("_active_turn_token") is None
    assert payload["messages"][-1]["role"] == "assistant"
    assert payload["messages"][-1]["_error"] is True


def test_streaming_tool_limit_empty_result_without_exact_checkpoint_materializes_current_user(
    tmp_path,
    monkeypatch,
):
    prompt = "Fix the failing test."
    prior = [
        {"role": "user", "content": prompt, "timestamp": 1.1},
        {"role": "assistant", "content": "The earlier attempt is complete."},
    ]
    result = {
        "turn_exit_reason": "max_iterations_reached(30/30)",
        "messages": [],
    }

    events, payload = _run_streaming_with_fake_agent(
        tmp_path,
        monkeypatch,
        result,
        prior_messages=prior,
        prior_context_messages=prior,
        msg_text=prompt,
        pending_started_at=1.9,
        current_turn_user_idx=0,
    )

    apperror_payloads = [event_payload for event, event_payload in events if event == "apperror"]
    assert apperror_payloads, "expected tool-limit apperror"
    assert apperror_payloads[-1]["type"] == "tool_limit_reached"
    assert apperror_payloads[-1]["terminal_state"] == "tool_limit_reached"
    assert not [event_payload for event, event_payload in events if event == "done"]
    assert [(message["role"], message.get("content")) for message in payload["messages"][:-1]] == [
        ("user", prompt),
        ("assistant", "The earlier attempt is complete."),
        ("user", prompt),
    ]
    assert payload["messages"][0].get("_active_turn_token") is None
    assert payload["messages"][2]["_active_turn_token"] == build_active_turn_token(
        "stream-tool-limit",
        1.9,
    )
    assert payload["messages"][-1]["role"] == "assistant"
    assert payload["messages"][-1]["_error"] is True
