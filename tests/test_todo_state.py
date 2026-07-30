import json

from api.todo_state import VERSION, attach_todo_state, derive_todo_state, parse_todo_tool_result


def _todo_payload(todos):
    summary = {
        "total": len(todos),
        "pending": sum(1 for t in todos if t["status"] == "pending"),
        "in_progress": sum(1 for t in todos if t["status"] == "in_progress"),
        "completed": sum(1 for t in todos if t["status"] == "completed"),
        "cancelled": sum(1 for t in todos if t["status"] == "cancelled"),
    }
    return json.dumps({"todos": todos, "summary": summary}, ensure_ascii=False)


def _todo_msg(todos, timestamp=None):
    msg = {"role": "tool", "content": _todo_payload(todos)}
    if timestamp is not None:
        msg["timestamp"] = timestamp
    return msg


def test_parse_todo_tool_result_accepts_json_string_and_dict():
    raw = _todo_payload([{"id": "1", "content": "review", "status": "pending"}])

    from_string = parse_todo_tool_result(raw)
    from_dict = parse_todo_tool_result(json.loads(raw))

    assert from_string is not None
    assert from_dict is not None
    assert from_string == from_dict
    assert from_string["version"] == VERSION
    assert from_string["todos"][0]["content"] == "review"
    assert from_string["summary"]["total"] == 1


def test_parse_todo_tool_result_rejects_non_todo_shapes():
    for bad in (None, "", "not json", "{}", '{"todos":"not-list"}', [1, 2, 3]):
        assert parse_todo_tool_result(bad) is None


def test_derive_todo_state_uses_latest_tool_write_even_when_empty():
    messages = [
        _todo_msg([{"id": "old", "content": "old task", "status": "pending"}], timestamp=10),
        {"role": "assistant", "content": "done", "timestamp": 11},
        _todo_msg([], timestamp=12),
    ]

    state = derive_todo_state(messages)

    assert state is not None
    assert state["todos"] == []
    assert state["summary"] == {"total": 0, "pending": 0, "in_progress": 0, "completed": 0, "cancelled": 0}
    assert state["ts"] == 12


def test_derive_todo_state_skips_malformed_and_non_string_tool_content():
    messages = [
        _todo_msg([{"id": "good", "content": "keep", "status": "in_progress"}]),
        {"role": "tool", "content": ["multimodal parts are not todo output"]},
        {"role": "tool", "content": '{"todos": broken'},
    ]

    state = derive_todo_state(messages)

    assert state is not None
    assert state["todos"][0]["id"] == "good"


def test_derive_todo_state_recency_falls_back_to_prior_message_timestamp():
    messages = [
        _todo_msg([{"id": "old", "content": "old", "status": "pending"}], timestamp=10),
        {"role": "assistant", "content": "checkpoint", "timestamp": 20},
        _todo_msg([{"id": "new", "content": "new", "status": "completed"}]),
    ]

    state = derive_todo_state(messages)

    assert state is not None
    assert state["todos"][0]["id"] == "new"
    assert state["ts"] == 20


def test_attach_todo_state_mutates_payload_and_swallows_missing_state():
    payload: dict = {"session_id": "s1"}
    assert attach_todo_state(payload, [_todo_msg([{"id": "1", "content": "x", "status": "pending"}])]) is True
    assert payload["todo_state"]["todos"][0]["id"] == "1"

    empty_payload: dict = {"session_id": "s2"}
    assert attach_todo_state(empty_payload, [{"role": "assistant", "content": "none"}]) is False
    assert "todo_state" not in empty_payload
