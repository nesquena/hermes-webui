"""Tests for _sanitize_messages_for_api() orphaned-tool-message stripping.

Regression for issue #534: strictly-conformant providers (Mercury-2/Inception,
newer OpenAI models) reject histories containing tool-role messages whose
tool_call_id has no matching tool_calls entry in a prior assistant message.
"""

import sys
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))

from api.streaming import _sanitize_messages_for_api


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _asst_with_tool_call(call_id="call-1", call_id_key="id"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "type": "function",
                call_id_key: call_id,
                "function": {"name": "terminal", "arguments": "{}"},
            }
        ],
        "_ts": 12345,  # extra field that should be stripped
    }


def _tool_result(call_id="call-1"):
    return {"role": "tool", "tool_call_id": call_id, "content": "ok", "_ts": 12345}


def _user(text="hello"):
    return {"role": "user", "content": text, "_ts": 12345}


def _asst(text="hi"):
    return {"role": "assistant", "content": text, "_ts": 12345}


# ---------------------------------------------------------------------------
# Tests: normal valid histories are preserved
# ---------------------------------------------------------------------------


def test_valid_tool_roundtrip_preserved():
    """A linked assistant→tool pair must be kept intact."""
    msgs = [_user(), _asst_with_tool_call("call-1"), _tool_result("call-1"), _asst()]
    result = _sanitize_messages_for_api(msgs)
    roles = [m["role"] for m in result]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_extra_fields_stripped():
    """Non-API fields (_ts etc.) are always stripped."""
    msgs = [_user(), _asst()]
    result = _sanitize_messages_for_api(msgs)
    for m in result:
        assert "_ts" not in m


def test_valid_history_without_tool_messages_unchanged():
    """Plain user/assistant history with no tool calls is passed through unchanged."""
    msgs = [_user("a"), _asst("b"), _user("c"), _asst("d")]
    result = _sanitize_messages_for_api(msgs)
    assert len(result) == 4
    assert all(m["role"] in ("user", "assistant") for m in result)


def test_multiple_valid_tool_calls_preserved():
    """Multiple linked tool_call_ids in one assistant message are all preserved."""
    asst = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "type": "function",
                "id": "call-1",
                "function": {"name": "f1", "arguments": "{}"},
            },
            {
                "type": "function",
                "id": "call-2",
                "function": {"name": "f2", "arguments": "{}"},
            },
        ],
    }
    msgs = [_user(), asst, _tool_result("call-1"), _tool_result("call-2"), _asst()]
    result = _sanitize_messages_for_api(msgs)
    roles = [m["role"] for m in result]
    assert roles == ["user", "assistant", "tool", "tool", "assistant"]


# ---------------------------------------------------------------------------
# Tests: orphaned tool messages are dropped
# ---------------------------------------------------------------------------


def test_orphaned_tool_message_dropped():
    """A tool message with no matching assistant tool_call is dropped."""
    msgs = [_user(), _asst(), _tool_result("call-orphan")]
    result = _sanitize_messages_for_api(msgs)
    roles = [m["role"] for m in result]
    assert "tool" not in roles
    assert roles == ["user", "assistant"]


def test_tool_message_missing_tool_call_id_dropped():
    """A tool message with no tool_call_id at all is dropped."""
    msg = {"role": "tool", "content": "result"}
    msgs = [_user(), _asst_with_tool_call("call-1"), msg]
    result = _sanitize_messages_for_api(msgs)
    roles = [m["role"] for m in result]
    assert "tool" not in roles


def test_partially_orphaned_tool_messages():
    """In a mixed batch, only the orphaned tool messages are dropped."""
    asst = _asst_with_tool_call("call-valid")
    msgs = [
        _user(),
        asst,
        _tool_result("call-valid"),  # linked → kept
        _tool_result("call-ghost"),  # orphaned → dropped
        _asst(),
    ]
    result = _sanitize_messages_for_api(msgs)
    roles = [m["role"] for m in result]
    assert roles == ["user", "assistant", "tool", "assistant"]
    # The kept tool message has the right call_id
    tool_msgs = [m for m in result if m["role"] == "tool"]
    assert tool_msgs[0]["tool_call_id"] == "call-valid"


def test_orphaned_tool_only_history():
    """A history consisting only of orphaned tool messages returns empty."""
    msgs = [_tool_result("dangling-1"), _tool_result("dangling-2")]
    result = _sanitize_messages_for_api(msgs)
    assert result == []


# ---------------------------------------------------------------------------
# Tests: Anthropic 'call_id' field name (not OpenAI 'id')
# ---------------------------------------------------------------------------


def test_anthropic_call_id_field_recognized():
    """Anthropic tool calls use 'call_id' not 'id' — both must be recognized."""
    asst = _asst_with_tool_call("call-anthropic", call_id_key="call_id")
    msgs = [_user(), asst, _tool_result("call-anthropic"), _asst()]
    result = _sanitize_messages_for_api(msgs)
    roles = [m["role"] for m in result]
    assert roles == ["user", "assistant", "tool", "assistant"]


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------


def test_empty_messages_list():
    assert _sanitize_messages_for_api([]) == []


def test_non_dict_messages_skipped():
    """Non-dict items in the messages list are silently ignored."""
    msgs = ["not a dict", None, _user("hi"), 42]
    result = _sanitize_messages_for_api(msgs)
    assert len(result) == 1
    assert result[0]["role"] == "user"


def test_tool_calls_none_does_not_crash():
    """An assistant message with tool_calls=None is handled without crashing."""
    asst = {"role": "assistant", "content": "hello", "tool_calls": None}
    msgs = [_user(), asst, _tool_result("call-1")]
    result = _sanitize_messages_for_api(msgs)
    # call-1 has no valid parent (tool_calls=None → no IDs registered) → dropped
    roles = [m["role"] for m in result]
    assert "tool" not in roles


def test_system_messages_preserved():
    """System messages are always preserved."""
    msgs = [{"role": "system", "content": "You are helpful."}, _user(), _asst()]
    result = _sanitize_messages_for_api(msgs)
    assert result[0]["role"] == "system"
