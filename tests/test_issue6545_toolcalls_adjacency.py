"""Tests for #6545: adjacency-based orphaned tool_calls stripping.

Pass 3 of _sanitize_messages_for_api / _api_safe_message_positions kept a
tool_call whenever its tool-role response existed ANYWHERE in the clean list.
After an interrupted MCP tool round, a replayed/out-of-order insert can leave a
stale tool_call on a later assistant message whose response sits far earlier in
the transcript. The downstream agent repair then re-derives tool_calls from the
trailing sequence and produces ``tool_calls: []``, which strict providers
(DeepSeek V4, Console Go) reject with HTTP 400 (#6545).

A call is now kept only when its tool-role response IMMEDIATELY follows the
assistant message — the adjacency rule strict providers enforce anyway. These
tests pin the three fixtures requested in the issue review: a valid same-turn
call/result pair, duplicate call IDs across separate turns, and a
duplicate-only (content-empty) assistant message.
"""

from api.streaming import _sanitize_messages_for_api, _api_safe_message_positions


def _tc(call_id):
    return {"id": call_id, "type": "function", "function": {"name": "f", "arguments": "{}"}}


def _tool(call_id):
    return {"role": "tool", "tool_call_id": call_id, "content": "ok"}


def _assistant(content, call_ids=()):
    msg = {"role": "assistant", "content": content}
    if call_ids:
        msg["tool_calls"] = [_tc(c) for c in call_ids]
    return msg


def _user(content="u"):
    return {"role": "user", "content": content}


def _assistant_messages(out, content):
    return [m for m in out if m.get("role") == "assistant" and m.get("content") == content]


# ── _sanitize_messages_for_api ──────────────────────────────────────────


def test_valid_same_turn_pair_survives():
    """A call whose tool response immediately follows stays intact."""
    msgs = [
        _user(),
        _assistant("first", ["call_1"]),
        _tool("call_1"),
        _user(),
    ]
    out = _sanitize_messages_for_api(msgs)
    first = _assistant_messages(out, "first")[0]
    assert [tc["id"] for tc in first["tool_calls"]] == ["call_1"]


def test_stale_call_with_remote_response_stripped():
    """A call id whose response exists earlier (non-adjacent) is stripped.

    This is the #6545 crash shape: after an interrupted tool round the final
    assistant message re-carries a call id answered ~2 turns earlier. The
    response exists in the list, but not immediately after this message.
    """
    msgs = [
        _user(),
        _assistant("first", ["call_1"]),
        _tool("call_1"),
        _user(),
        _assistant("second stale", ["call_1"]),
        _user(),
    ]
    out = _sanitize_messages_for_api(msgs)
    stale = _assistant_messages(out, "second stale")[0]
    assert "tool_calls" not in stale
    # the original turn keeps its call
    first = _assistant_messages(out, "first")[0]
    assert [tc["id"] for tc in first["tool_calls"]] == ["call_1"]


def test_duplicate_ids_across_separate_turns():
    """Same call id in two turns: only the adjacency-satisfying one survives."""
    msgs = [
        _user(),
        _assistant("t1", ["call_1"]),
        _tool("call_1"),
        _user(),
        _assistant("t2", ["call_2"]),
        _tool("call_2"),
        _user(),
        _assistant("t3 stale", ["call_1"]),
        _user(),
    ]
    out = _sanitize_messages_for_api(msgs)
    assert [tc["id"] for tc in _assistant_messages(out, "t1")[0]["tool_calls"]] == ["call_1"]
    assert [tc["id"] for tc in _assistant_messages(out, "t2")[0]["tool_calls"]] == ["call_2"]
    assert "tool_calls" not in _assistant_messages(out, "t3 stale")[0]


def test_duplicate_only_content_empty_assistant_dropped():
    """All calls stripped AND no content -> the assistant message is dropped."""
    msgs = [
        _user(),
        _assistant("first", ["call_1"]),
        _tool("call_1"),
        _user(),
        _assistant("", ["call_1"]),  # duplicate-only, empty content
        _user(),
    ]
    out = _sanitize_messages_for_api(msgs)
    # no assistant message with empty content may survive
    assert not any(
        m.get("role") == "assistant" and not str(m.get("content") or "").strip()
        for m in out
    )
    # and the original turn still has its call
    first = _assistant_messages(out, "first")[0]
    assert [tc["id"] for tc in first["tool_calls"]] == ["call_1"]


def test_duplicate_only_content_present_keeps_text():
    """All calls stripped but content present -> message survives without key."""
    msgs = [
        _user(),
        _assistant("first", ["call_1"]),
        _tool("call_1"),
        _user(),
        _assistant("I have the answer", ["call_1"]),
        _user(),
    ]
    out = _sanitize_messages_for_api(msgs)
    kept = _assistant_messages(out, "I have the answer")[0]
    assert "tool_calls" not in kept
    assert kept["content"] == "I have the answer"


# ── _api_safe_message_positions (mirror) ────────────────────────────────


def test_positions_mirror_strips_non_adjacent():
    msgs = [
        _user(),
        _assistant("first", ["call_1"]),
        _tool("call_1"),
        _user(),
        _assistant("stale", ["call_1"]),
        _user(),
    ]
    out = _api_safe_message_positions(msgs)
    stale = [m for _i, m in out if m.get("content") == "stale"][0]
    assert "tool_calls" not in stale
    first = [m for _i, m in out if m.get("content") == "first"][0]
    assert [tc["id"] for tc in first["tool_calls"]] == ["call_1"]


def test_positions_mirror_keeps_same_turn_pair():
    msgs = [
        _user(),
        _assistant("first", ["call_1"]),
        _tool("call_1"),
        _user(),
    ]
    out = _api_safe_message_positions(msgs)
    first = [m for _i, m in out if m.get("content") == "first"][0]
    assert [tc["id"] for tc in first["tool_calls"]] == ["call_1"]
