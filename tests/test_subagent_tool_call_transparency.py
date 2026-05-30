import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import api.routes as routes


def _delegate_messages(*, tool_call_id="call_delegate", result_count=1):
    tool_calls = [
        {
            "id": tool_call_id,
            "call_id": tool_call_id,
            "type": "function",
            "function": {
                "name": "delegate_task",
                "arguments": json.dumps({"goal": "Run a child task"}),
            },
        }
    ]
    return [
        {"role": "assistant", "content": "", "tool_calls": tool_calls, "timestamp": 1},
        {
            "role": "tool",
            "tool_name": "delegate_task",
            "tool_call_id": tool_call_id,
            "content": json.dumps({
                "results": [
                    {"task_index": idx, "status": "completed", "summary": f"child {idx}"}
                    for idx in range(result_count)
                ]
            }),
            "timestamp": 2,
        },
    ]


def test_augment_session_tool_calls_adds_subagent_cards(monkeypatch):
    messages = _delegate_messages()
    monkeypatch.setattr(
        routes,
        "_read_subagent_activity_sessions",
        lambda *_args, **_kwargs: [
            {
                "session_id": "child-a",
                "title": "Codex child",
                "started_at": 10,
                "active": False,
                "end_reason": "completed",
            }
        ],
    )

    tool_calls = routes._augment_session_tool_calls_with_subagents(
        messages,
        [],
        session_id="parent-session",
        profile=None,
    )

    assert [tc["name"] for tc in tool_calls] == ["delegate_task", "subagent_progress"]
    delegate_call, subagent_call = tool_calls
    assert delegate_call["assistant_msg_idx"] == 0
    assert subagent_call["assistant_msg_idx"] == delegate_call["assistant_msg_idx"]
    assert subagent_call["preview"] == "Codex child"
    assert subagent_call["snippet"] == "Completed · completed · session child-a"
    assert subagent_call["done"] is True
    assert subagent_call["args"]["child_session_id"] == "child-a"
    assert subagent_call["child_session_id"] == "child-a"


def test_augment_session_tool_calls_expands_batch_delegate_results(monkeypatch):
    messages = _delegate_messages(result_count=2)
    monkeypatch.setattr(
        routes,
        "_read_subagent_activity_sessions",
        lambda *_args, **_kwargs: [
            {
                "session_id": "child-a",
                "title": "First child",
                "started_at": 10,
                "active": False,
                "end_reason": "completed",
            },
            {
                "session_id": "child-b",
                "title": "Second child",
                "started_at": 11,
                "active": True,
                "end_reason": None,
            },
        ],
    )

    tool_calls = routes._augment_session_tool_calls_with_subagents(
        messages,
        [],
        session_id="parent-session",
        profile=None,
    )

    assert [tc["name"] for tc in tool_calls] == [
        "delegate_task",
        "subagent_progress",
        "subagent_progress",
    ]
    assert tool_calls[1]["child_session_id"] == "child-a"
    assert tool_calls[1]["done"] is True
    assert tool_calls[2]["child_session_id"] == "child-b"
    assert tool_calls[2]["done"] is False
    assert tool_calls[2]["snippet"] == "Running · session child-b"


class _GetHandler:
    def __init__(self, path: str):
        self.path = path
        self.status = None
        self.headers = {}
        self.response_json = None

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    @property
    def wfile(self):
        class _Writer:
            def __init__(self, outer):
                self.outer = outer

            def write(self, data):
                self.outer.response_json = json.loads(data.decode("utf-8"))

        return _Writer(self)


def _write_cli_state_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                timestamp TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_name TEXT
            );
            """
        )
        tool_calls = [
            {
                "id": "call_delegate",
                "type": "function",
                "function": {
                    "name": "delegate_task",
                    "arguments": json.dumps({"goal": "Run a child task"}),
                },
            }
        ]
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, tool_calls) VALUES (?, ?, ?, ?, ?)",
            ("cli-subagent", "assistant", "", "2026-01-01T00:00:01Z", json.dumps(tool_calls)),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, tool_call_id, tool_name) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "cli-subagent",
                "tool",
                json.dumps({"results": [{"task_index": 0, "status": "completed"}]}),
                "2026-01-01T00:00:02Z",
                "call_delegate",
                "delegate_task",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_cli_session_route_returns_subagent_activity_tool_cards(tmp_path, monkeypatch):
    _write_cli_state_db(tmp_path / "state.db")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import api.profiles

    monkeypatch.setattr(api.profiles, "get_active_hermes_home", lambda: str(tmp_path))
    monkeypatch.setattr(
        routes,
        "_read_subagent_activity_sessions",
        lambda *_args, **_kwargs: [
            {
                "session_id": "child-route",
                "title": "Route child",
                "started_at": 10,
                "active": False,
                "end_reason": "completed",
            }
        ],
    )

    handler = _GetHandler("/api/session?session_id=cli-subagent")
    parsed = urlparse(handler.path)

    assert routes.handle_get(handler, parsed) is True
    assert handler.status == 200
    assert handler.response_json is not None
    session = handler.response_json["session"]
    assert [tc["name"] for tc in session["tool_calls"]] == ["delegate_task", "subagent_progress"]
    assert session["tool_calls"][1]["preview"] == "Route child"
