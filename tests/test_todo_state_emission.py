import json

import pytest

from api import helpers
from api.todo_state import EVENT_NAME, emit_todo_state


def _payload(todos):
    return json.dumps({"todos": todos, "summary": {"total": len(todos)}})


@pytest.mark.parametrize("tool_name", ["todo", "todo_list"])
def test_emit_todo_state_emits_full_snapshot_with_session_tags(tool_name):
    events = []

    emitted = emit_todo_state(
        lambda event, data: events.append((event, data)),
        name=tool_name,
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


@pytest.mark.parametrize("tool_name", ["todo", "todo_list"])
def test_emit_todo_state_ignores_non_todo_or_bad_payload(tool_name):
    events = []

    assert emit_todo_state(lambda event, data: events.append((event, data)), name="web_search", function_result=_payload([]), session_id="s", stream_id="st") is False
    assert emit_todo_state(lambda event, data: events.append((event, data)), name=tool_name, function_result="not json", session_id="s", stream_id="st") is False
    assert emit_todo_state(lambda event, data: events.append((event, data)), name=tool_name, function_result='{"todos":"not-list"}', session_id="s", stream_id="st") is False
    assert events == []


@pytest.mark.parametrize("tool_name", ["todo", "todo_list"])
def test_emit_todo_state_redacts_before_sse(tool_name, monkeypatch):
    events = []

    def fake_redact(value, *, _enabled=None):
        redacted = dict(value)
        redacted["todos"] = [{"id": "1", "content": "[REDACTED]", "status": "pending"}]
        return redacted

    monkeypatch.setattr(helpers, "_redact_value", fake_redact)

    emitted = emit_todo_state(
        lambda event, data: events.append((event, data)),
        name=tool_name,
        function_result=_payload([{"id": "1", "content": "secret token", "status": "pending"}]),
        session_id="s",
        stream_id="st",
    )

    assert emitted is True
    assert events[0][1]["todos"][0]["content"] == "[REDACTED]"


@pytest.mark.parametrize("tool_name", ["todo", "todo_list"])
def test_emit_todo_state_threads_redaction_enabled_flag_once(tool_name, monkeypatch):
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
        name=tool_name,
        function_result=_payload([{"id": "1", "content": "ordinary task", "status": "pending"}]),
        session_id="s",
        stream_id="st",
    )

    assert emitted is True
    assert len(events) == 1
    assert settings_calls == ["load_settings"]
    assert redaction_enabled_args == [False]


@pytest.mark.parametrize("tool_name", ["todo", "todo_list"])
def test_emit_todo_state_issue8534_canonical_result(tool_name):
    """Use the exact canonical result and tags from issue #8534's reproduction."""
    events = []
    result = {
        "todos": [{"id": "demo", "content": "Verify the panel", "status": "in_progress"}],
        "summary": {"total": 1, "in_progress": 1},
    }

    assert emit_todo_state(
        lambda event, data: events.append((event, data)),
        name=tool_name,
        function_result=json.dumps(result),
        session_id="fixture-session",
        stream_id="fixture-stream",
    ) is True
    assert len(events) == 1
    event, data = events[0]
    assert event == EVENT_NAME
    assert data["todos"] == result["todos"]
    assert data["summary"] == result["summary"]
    assert data["session_id"] == "fixture-session"
    assert data["stream_id"] == "fixture-stream"


@pytest.mark.parametrize("tool_name", ["todo", "todo_list"])
def test_emit_todo_state_empty_list_clears_snapshot(tool_name):
    events = []
    assert emit_todo_state(
        lambda event, data: events.append((event, data)),
        name=tool_name,
        function_result={"todos": [], "summary": {"total": 0}},
        session_id="s",
        stream_id="st",
    ) is True
    assert len(events) == 1
    assert events[0][1]["todos"] == []
    assert events[0][1]["summary"] == {"total": 0}


@pytest.mark.parametrize("tool_name", ["todo", "todo_list"])
def test_emit_todo_state_fails_closed_when_redaction_fails(monkeypatch, tool_name):
    events = []
    redaction_calls = []

    def failing_redact(value, *, _enabled=None):
        redaction_calls.append(value)
        raise RuntimeError("redactor unavailable")

    monkeypatch.setattr(helpers, "_redact_value", failing_redact)
    assert emit_todo_state(
        lambda event, data: events.append((event, data)),
        name=tool_name,
        function_result=_payload([{"id": "1", "content": "sensitive", "status": "pending"}]),
        session_id="s",
        stream_id="st",
    ) is False
    assert len(redaction_calls) == 1
    assert events == []


@pytest.mark.parametrize("tool_name", [None, "web_search", "TODO_LIST", "", [], {}])
def test_emit_todo_state_ignores_unknown_tool_names(tool_name):
    events = []
    assert emit_todo_state(
        lambda event, data: events.append((event, data)),
        name=tool_name,
        function_result=_payload([]),
        session_id="s",
        stream_id="st",
    ) is False
    assert events == []
