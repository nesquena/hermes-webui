"""Regression test for the v0.50.251 Opus pass-2 MUST-FIX on PR #1375.

Original PR #1375 stored cancelled tool calls under the message key
`tool_calls`. That key is whitelisted by `_API_SAFE_MSG_KEYS` so the
sanitize-for-API path forwarded them to the next-turn LLM call. But the
captured entries use the WebUI internal shape ({name, args, done,
duration, is_error}) — they don't have OpenAI/Anthropic's id +
function: {name, arguments} envelope. Strict providers (OpenAI,
Anthropic, Z.AI/GLM) would 400 on the malformed entries — turning a
"data lost on cancel" bug into a "next message returns 400" bug.

The fix renames the key to `_partial_tool_calls` (underscore-prefixed
private key NOT in the whitelist), so sanitize correctly strips it.
The UI reads it via static/messages.js.

This test pins the invariant: a partial assistant message with
`_partial_tool_calls` set must produce ZERO `tool_calls` after
sanitize-for-API.
"""

import pathlib
import sys


REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))


def test_partial_tool_calls_field_not_forwarded_to_llm():
    """The `_partial_tool_calls` field must not survive _sanitize_messages_for_api.
    Otherwise the malformed entries get sent to the LLM and cause 400 errors."""
    from api.streaming import _sanitize_messages_for_api

    messages = [
        {"role": "user", "content": "do a search"},
        {
            "role": "assistant",
            "content": "Looking up...",
            "_partial": True,
            "_partial_tool_calls": [
                {"name": "web_search", "args": {"query": "x"}, "done": False},
            ],
            "reasoning": "Let me think about this",
        },
    ]
    sanitized = _sanitize_messages_for_api(messages)
    # The partial assistant message must NOT have _partial_tool_calls (private key).
    # It must NOT have tool_calls (would bypass the rename and 400 the LLM).
    # It must NOT have reasoning (not in whitelist).
    assistant_msgs = [m for m in sanitized if m.get("role") == "assistant"]
    assert assistant_msgs, "Sanitized output must include the assistant message"
    for m in assistant_msgs:
        assert "_partial_tool_calls" not in m, (
            "Sanitize-for-API must strip _partial_tool_calls — it's a UI-only key. "
            f"Got: {m}"
        )
        assert "tool_calls" not in m, (
            "Sanitize-for-API must NOT have tool_calls on a partial message — the "
            "captured entries use WebUI shape and would 400 on strict providers. "
            f"Got: {m}"
        )
        assert "reasoning" not in m, (
            "Sanitize-for-API must strip reasoning (not in _API_SAFE_MSG_KEYS). "
            f"Got: {m}"
        )


def test_legitimate_tool_calls_are_preserved_for_completed_turns():
    """Completed assistant turns with REAL tool_calls (with id + function envelope)
    must still pass through sanitize unchanged. The rename only affects
    cancel-partial messages, not normal completed turns."""
    from api.streaming import _sanitize_messages_for_api

    messages = [
        {"role": "user", "content": "search"},
        {
            "role": "assistant",
            "content": "I'll search.",
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": '{"query":"x"}'},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_abc",
            "name": "web_search",
            "content": "result",
        },
    ]
    sanitized = _sanitize_messages_for_api(messages)
    assistant_msgs = [m for m in sanitized if m.get("role") == "assistant"]
    assert assistant_msgs, "Sanitized output must include the assistant message"
    assert assistant_msgs[0].get("tool_calls"), (
        "Legitimate tool_calls on completed turns must survive sanitize. "
        f"Got: {assistant_msgs[0]}"
    )
    # Must still have the OpenAI envelope shape
    tc = assistant_msgs[0]["tool_calls"][0]
    assert "id" in tc and "function" in tc, (
        f"tool_calls envelope must be preserved. Got: {tc}"
    )
