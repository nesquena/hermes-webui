"""Regression coverage for #6333 Gemini fallback request-boundary replay repair."""
import copy
import pathlib
import sys
import types


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))


GOOGLE_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


def _repair(messages, *, model, base_url):
    from api.streaming import _repair_google_gemini_current_turn_tool_state_for_request

    return _repair_google_gemini_current_turn_tool_state_for_request(
        messages,
        model=model,
        base_url=base_url,
    )


def _prior_and_current_turn_history():
    return [
        {"role": "user", "content": "look up the first order"},
        {
            "role": "assistant",
            "content": "Found it.",
            "tool_calls": [
                {
                    "id": "call_prior",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"id":"1"}'},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_prior",
            "name": "lookup",
            "content": "order 1: shipped",
        },
        {"role": "user", "content": "now check the next one"},
        {
            "role": "assistant",
            "content": "Checking the next order.",
            "tool_calls": [
                {
                    "id": "call_current",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"id":"2"}'},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_current",
            "name": "lookup",
            "content": "order 2: pending",
        },
    ]


def test_gemini3_request_repairs_only_current_turn_and_leaves_input_untouched():
    messages = _prior_and_current_turn_history()
    original = copy.deepcopy(messages)

    result = _repair(
        messages,
        model="gemini-3-flash",
        base_url=GOOGLE_OPENAI_BASE,
    )

    assert messages == original, "request-boundary repair must not mutate canonical history"

    prior_assistant = result[1]
    assert prior_assistant.get("tool_calls"), "prior completed turns must stay intact"
    assert any(
        m.get("role") == "tool" and m.get("tool_call_id") == "call_prior"
        for m in result
    ), "prior completed tool rows must stay intact"

    current_assistant = next(
        m for m in result
        if m.get("role") == "assistant" and m.get("content") == "Checking the next order."
    )
    assert "tool_calls" not in current_assistant, "current-turn unsigned Gemini 3 tool state must be stripped from the request copy"
    assert not any(
        m.get("role") == "tool" and m.get("tool_call_id") == "call_current"
        for m in result
    ), "linked current-turn tool rows must be removed with the stripped group"


def test_gemini3_parallel_group_keeps_unsigned_siblings_when_first_call_is_signed():
    messages = [
        {"role": "user", "content": "check both orders"},
        {
            "role": "assistant",
            "content": "Checking both.",
            "tool_calls": [
                {
                    "id": "call_signed",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"id":"1"}'},
                    "extra_content": {"google": {"thought_signature": "sig-1"}},
                },
                {
                    "id": "call_unsigned",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"id":"2"}'},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_signed",
            "name": "lookup",
            "content": "order 1: shipped",
        },
        {
            "role": "tool",
            "tool_call_id": "call_unsigned",
            "name": "lookup",
            "content": "order 2: pending",
        },
    ]

    result = _repair(
        messages,
        model="gemini-3-flash",
        base_url=GOOGLE_OPENAI_BASE,
    )

    assistant = next(m for m in result if m.get("role") == "assistant")
    kept_ids = [tc.get("id") for tc in assistant.get("tool_calls") or []]
    assert kept_ids == ["call_signed", "call_unsigned"], (
        "a signed first Gemini call must keep the whole parallel group"
    )
    kept_tool_ids = [
        m.get("tool_call_id")
        for m in result
        if m.get("role") == "tool"
    ]
    assert kept_tool_ids == ["call_signed", "call_unsigned"]


def test_gemini25_request_does_not_prune_unsigned_current_turn_history():
    messages = _prior_and_current_turn_history()

    result = _repair(
        messages,
        model="gemini-2.5-flash",
        base_url=GOOGLE_OPENAI_BASE,
    )

    assert result == messages, "Gemini 2.5 is out of scope for the strict current-turn repair"


def test_non_google_route_does_not_prune_gemini_named_model_history():
    messages = _prior_and_current_turn_history()

    result = _repair(
        messages,
        model="gemini-3-flash",
        base_url="https://openrouter.ai/api/v1",
    )

    assert result == messages, "only Google Gemini requests should receive the request-boundary repair"


def test_transport_wrapper_repairs_google_request_copy_without_mutating_input(monkeypatch):
    from api.streaming import _install_gemini_request_boundary_wrapper
    import api.streaming as streaming

    agent_pkg = types.ModuleType("agent")
    agent_pkg.__path__ = []
    transports_pkg = types.ModuleType("agent.transports")
    transports_pkg.__path__ = []
    chat_module = types.ModuleType("agent.transports.chat_completions")

    class ChatCompletionsTransport:
        def build_kwargs(self, model, messages, tools=None, **params):
            return {
                "model": model,
                "messages": messages,
                "tools": tools,
                **params,
            }

    chat_module.ChatCompletionsTransport = ChatCompletionsTransport
    transports_pkg.chat_completions = chat_module
    agent_pkg.transports = transports_pkg
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.transports", transports_pkg)
    monkeypatch.setitem(sys.modules, "agent.transports.chat_completions", chat_module)

    streaming._GEMINI_REQUEST_BOUNDARY_WRAPPER_INSTALLED = False

    messages = _prior_and_current_turn_history()
    original = copy.deepcopy(messages)

    _install_gemini_request_boundary_wrapper()
    transport = ChatCompletionsTransport()
    kwargs = transport.build_kwargs(
        model="gemini-3-flash",
        messages=messages,
        tools=None,
        base_url=GOOGLE_OPENAI_BASE,
    )

    result = kwargs["messages"]
    assert messages == original, "the transport wrapper must repair only the outbound request copy"
    assert any(
        m.get("role") == "tool" and m.get("tool_call_id") == "call_prior"
        for m in result
    ), "prior completed turns must stay intact through the wrapped build path"
    assert not any(
        m.get("role") == "tool" and m.get("tool_call_id") == "call_current"
        for m in result
    ), "the wrapped build path must strip the current-turn unsigned group before Gemini sees it"
