"""Regression coverage for settled SSE payload message counts."""

import json
from pathlib import Path
from types import SimpleNamespace

from api.streaming import _session_payload_with_full_messages


STREAMING_SOURCE = Path("api/streaming.py").read_text(encoding="utf-8")


class _FakeSession(SimpleNamespace):
    def compact(self):
        return {
            "session_id": self.session_id,
            "message_count": 45,
            "title": "stale compact metadata",
        }


def test_full_message_payload_overrides_stale_compact_message_count():
    session = _FakeSession(
        session_id="child-session",
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ],
    )

    payload = _session_payload_with_full_messages(session, tool_calls=[])

    assert payload["messages"] == session.messages
    assert payload["message_count"] == len(session.messages)
    assert payload["message_count"] != session.compact()["message_count"]


def test_full_message_payload_includes_todo_state_snapshot():
    todo_result = {
        "todos": [
            {"id": "todo-1", "content": "keep workspace todos visible", "status": "in_progress"},
        ],
        "summary": {
            "total": 1,
            "pending": 0,
            "in_progress": 1,
            "completed": 0,
            "cancelled": 0,
        },
    }
    session = _FakeSession(
        session_id="todo-session",
        messages=[
            {"role": "user", "content": "plan the fix", "timestamp": 100},
            {
                "role": "tool",
                "content": json.dumps(todo_result),
                "timestamp": 101,
            },
            {"role": "assistant", "content": "done", "timestamp": 102},
        ],
    )

    payload = _session_payload_with_full_messages(session, tool_calls=[])

    assert payload["todo_state"]["todos"] == todo_result["todos"]
    assert payload["todo_state"]["summary"] == todo_result["summary"]
    assert payload["todo_state"]["version"] == 1
    assert payload["todo_state"]["ts"] == 101


def test_done_payload_uses_full_message_count_helper():
    done_idx = STREAMING_SOURCE.index("put('done', _done_payload)")
    block_start = STREAMING_SOURCE.rfind("raw_session =", 0, done_idx)
    block = STREAMING_SOURCE[block_start:done_idx]

    assert "_session_payload_with_full_messages(s, tool_calls=tool_calls)" in block
    assert "s.compact() | {'messages': s.messages" not in block


def test_apperror_payload_uses_full_message_count_helper():
    error_idx = STREAMING_SOURCE.index("put('apperror', _error_payload)")
    block_start = STREAMING_SOURCE.rfind("_error_payload['session']", 0, error_idx)
    block = STREAMING_SOURCE[block_start:error_idx]

    assert "_session_payload_with_full_messages(s, tool_calls=s.tool_calls)" in block
    assert "s.compact() | {'messages': s.messages" not in block


def test_gateway_done_payload_uses_full_message_count_helper():
    """The gateway-routed chat `done` SSE shares the settled-payload path and
    must also report a message_count matching the embedded transcript (sibling
    of the two streaming.py sites)."""
    gateway_source = Path("api/gateway_chat.py").read_text(encoding="utf-8")
    done_idx = gateway_source.index('put_gateway_event("done"')
    block_start = gateway_source.rfind("gateway_session_payload =", 0, done_idx)
    block = gateway_source[block_start:done_idx]

    assert "_session_payload_with_full_messages(s, tool_calls=[])" in block
    assert 's.compact() | {"messages": s.messages' not in block
