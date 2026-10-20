"""Senior gate pin for #8194: the two model-facing projections must agree row-for-row."""
from api.streaming import _api_safe_message_positions, _sanitize_messages_for_api


def _call(cid):
    return {"id": cid, "type": "function", "function": {"name": "t", "arguments": "{}"}}


def test_positions_projection_mirrors_sanitize_after_adjacent_repair():
    rows = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [_call("a")]},
        {"role": "user", "content": "interruption"},
        {"role": "tool", "tool_call_id": "a", "content": "late"},
        {"role": "assistant", "content": "done"},
    ]
    sanitized = [m["role"] for m in _sanitize_messages_for_api(rows)]
    positioned = [m["role"] for _i, m in _api_safe_message_positions(rows)]
    assert positioned == sanitized, (positioned, sanitized)
    assert "tool" not in positioned, "a result whose call was repaired away must not survive"
