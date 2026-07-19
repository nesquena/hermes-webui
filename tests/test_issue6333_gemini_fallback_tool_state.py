"""Regression coverage for #6333 Gemini fallback replay sanitization."""
import pathlib
import sys


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))


def _unsigned_tool_history():
    return [
        {"role": "user", "content": "look up the order"},
        {
            "role": "assistant",
            "content": "Looking that up.",
            "tool_calls": [
                {
                    "id": "call_glm_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"id":"7"}'},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_glm_1",
            "name": "lookup",
            "content": "order 7: shipped",
        },
        {"role": "user", "content": "thanks, and the next one?"},
    ]


def test_google_fallback_prunes_unsigned_tool_call_and_linked_tool_row():
    from api.streaming import _sanitize_messages_for_api

    result = _sanitize_messages_for_api(
        _unsigned_tool_history(),
        effective_provider="google",
    )
    assert not any(
        m.get("role") == "tool" and m.get("tool_call_id") == "call_glm_1"
        for m in result
    ), "linked tool row must be pruned with an unsigned Gemini-unsafe tool call"
    for message in result:
        if message.get("role") != "assistant":
            continue
        assert not message.get("tool_calls"), (
            "Google-targeted replay must prune unsigned historical tool calls. "
            f"Got: {message}"
        )


def test_configured_google_fallback_prunes_unsigned_history_before_switch():
    from api.streaming import _sanitize_messages_for_api

    result = _sanitize_messages_for_api(
        _unsigned_tool_history(),
        cfg={
            "fallback_providers": [
                {"provider": "google", "model": "gemini-2.5-flash"},
            ],
        },
        effective_provider="z-ai",
    )
    assert not any(m.get("role") == "tool" for m in result)
    assert all(
        not m.get("tool_calls")
        for m in result
        if m.get("role") == "assistant"
    )


def test_gemini_alias_provider_also_prunes_unsigned_history():
    from api.streaming import _sanitize_messages_for_api

    result = _sanitize_messages_for_api(
        _unsigned_tool_history(),
        effective_provider="gemini",
    )
    assert not any(m.get("role") == "tool" for m in result)
    assert all(
        not m.get("tool_calls")
        for m in result
        if m.get("role") == "assistant"
    )


def test_google_fallback_keeps_signed_call_and_prunes_unsigned_sibling():
    from api.streaming import _sanitize_messages_for_api

    messages = [
        {"role": "user", "content": "first"},
        {
            "role": "assistant",
            "content": "signed turn",
            "tool_calls": [
                {
                    "id": "call_signed",
                    "type": "function",
                    "function": {"name": "safe", "arguments": "{}"},
                    "extra_content": {"google": {"thought_signature": "sig-function"}},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_signed",
            "name": "safe",
            "content": "ok",
        },
        {"role": "user", "content": "second"},
        {
            "role": "assistant",
            "content": "unsigned turn",
            "tool_calls": [
                {
                    "id": "call_unsigned",
                    "type": "function",
                    "function": {"name": "risky", "arguments": "{}"},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_unsigned",
            "name": "risky",
            "content": "ok",
        },
    ]
    result = _sanitize_messages_for_api(messages, effective_provider="google")
    kept_call_ids = {
        tc.get("id")
        for message in result
        if message.get("role") == "assistant"
        for tc in (message.get("tool_calls") or [])
    }
    kept_tool_ids = {
        message.get("tool_call_id")
        for message in result
        if message.get("role") == "tool"
    }
    assert "call_signed" in kept_call_ids
    assert "call_signed" in kept_tool_ids
    assert "call_unsigned" not in kept_call_ids
    assert "call_unsigned" not in kept_tool_ids


def test_non_google_provider_preserves_unsigned_history():
    from api.streaming import _sanitize_messages_for_api

    for provider in (None, "openai", "anthropic", "z-ai"):
        result = _sanitize_messages_for_api(
            _unsigned_tool_history(),
            effective_provider=provider,
        )
        kept_call_ids = {
            tc.get("id")
            for message in result
            if message.get("role") == "assistant"
            for tc in (message.get("tool_calls") or [])
        }
        kept_tool_ids = {
            message.get("tool_call_id")
            for message in result
            if message.get("role") == "tool"
        }
        assert "call_glm_1" in kept_call_ids, (
            f"provider {provider!r} must preserve current replay behavior"
        )
        assert "call_glm_1" in kept_tool_ids, (
            f"provider {provider!r} must keep the linked tool row"
        )
