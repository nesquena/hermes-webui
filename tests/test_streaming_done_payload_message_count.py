"""Regression coverage for settled SSE payload message counts."""

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
