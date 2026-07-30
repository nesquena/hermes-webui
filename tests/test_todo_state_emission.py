import json

from api import helpers
from api.todo_state import EVENT_NAME, emit_todo_state


def _payload(todos):
    return json.dumps({"todos": todos, "summary": {"total": len(todos)}})


def test_emit_todo_state_emits_full_snapshot_with_session_tags():
    events = []

    emitted = emit_todo_state(
        lambda event, data: events.append((event, data)),
        name="todo",
        function_result=_payload([{"id": "1", "content": "ship", "status": "pending"}]),
        session_id="session-1",
        stream_id="stream-1",
    )

    assert emitted is True
    assert len(events) == 1
    event, data = events[0]
    assert event == EVENT_NAME
    assert data["session_id"] == "session-1"
    assert data["stream_id"] == "stream-1"
    assert data["source"] == "tool"
    assert data["todos"][0]["content"] == "ship"
    assert data["summary"]["total"] == 1
    assert data["version"] == 1
    assert isinstance(data["ts"], float)


def test_emit_todo_state_ignores_non_todo_or_bad_payload():
    events = []

    assert emit_todo_state(lambda event, data: events.append((event, data)), name="web_search", function_result=_payload([]), session_id="s", stream_id="st") is False
    assert emit_todo_state(lambda event, data: events.append((event, data)), name="todo", function_result="not json", session_id="s", stream_id="st") is False
    assert emit_todo_state(lambda event, data: events.append((event, data)), name="todo", function_result='{"todos":"not-list"}', session_id="s", stream_id="st") is False
    assert events == []


def test_emit_todo_state_redacts_before_sse(monkeypatch):
    events = []

    def fake_redact(value, *, _enabled=None):
        redacted = dict(value)
        redacted["todos"] = [{"id": "1", "content": "[REDACTED]", "status": "pending"}]
        return redacted

    monkeypatch.setattr(helpers, "_redact_value", fake_redact)

    emitted = emit_todo_state(
        lambda event, data: events.append((event, data)),
        name="todo",
        function_result=_payload([{"id": "1", "content": "secret token", "status": "pending"}]),
        session_id="s",
        stream_id="st",
    )

    assert emitted is True
    assert events[0][1]["todos"][0]["content"] == "[REDACTED]"


def test_emit_todo_state_threads_redaction_enabled_flag_once(monkeypatch):
    events = []
    settings_calls = []
    redaction_enabled_args = []

    def fake_load_settings():
        settings_calls.append("load_settings")
        return {"api_redact_enabled": False}

    def fake_redact(value, *, _enabled=None):
        redaction_enabled_args.append(_enabled)
        return dict(value)

    monkeypatch.setattr("api.config.load_settings", fake_load_settings)
    monkeypatch.setattr(helpers, "_redact_value", fake_redact)

    emitted = emit_todo_state(
        lambda event, data: events.append((event, data)),
        name="todo",
        function_result=_payload([{"id": "1", "content": "ordinary task", "status": "pending"}]),
        session_id="s",
        stream_id="st",
    )

    assert emitted is True
    assert len(events) == 1
    assert settings_calls == ["load_settings"]
    assert redaction_enabled_args == [False]
