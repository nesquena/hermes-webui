"""Regression coverage for CLI session tool-call metadata import (#1772)."""

from __future__ import annotations

import json
import sqlite3

import api.models as models


def _patch_active_home(monkeypatch, home):
    import api.profiles as profiles

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: None)


def _create_state_db_with_tool_turn(path, session_id="cli_tool_session_001"):
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL,
            token_count INTEGER,
            finish_reason TEXT,
            reasoning TEXT,
            reasoning_details TEXT,
            codex_reasoning_items TEXT,
            reasoning_content TEXT,
            codex_message_items TEXT
        )
        """
    )
    tool_calls = [
        {
            "id": "call_123",
            "type": "function",
            "function": {
                "name": "terminal",
                "arguments": json.dumps({"command": "printf ok"}),
            },
        }
    ]
    conn.execute(
        """
        INSERT INTO messages (
            session_id, role, content, tool_calls, timestamp, reasoning, reasoning_content
        ) VALUES (?, 'assistant', '', ?, 1.0, 'Need a shell check', 'Need a shell check')
        """,
        (session_id, json.dumps(tool_calls)),
    )
    conn.execute(
        """
        INSERT INTO messages (
            session_id, role, content, tool_call_id, tool_name, timestamp
        ) VALUES (?, 'tool', ?, 'call_123', 'terminal', 2.0)
        """,
        (session_id, json.dumps({"output": "ok"})),
    )
    conn.commit()
    conn.close()
    return tool_calls


def test_get_cli_session_messages_preserves_tool_call_metadata(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _patch_active_home(monkeypatch, hermes_home)
    expected_tool_calls = _create_state_db_with_tool_turn(hermes_home / "state.db")

    messages = models.get_cli_session_messages("cli_tool_session_001")

    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == ""
    assert messages[0]["tool_calls"] == expected_tool_calls
    assert messages[0]["reasoning"] == "Need a shell check"
    assert messages[0]["reasoning_content"] == "Need a shell check"
    assert messages[1]["role"] == "tool"
    assert messages[1]["tool_call_id"] == "call_123"
    assert messages[1]["tool_name"] == "terminal"
    assert messages[1]["name"] == "terminal"
    assert json.loads(messages[1]["content"])["output"] == "ok"


def test_existing_cli_import_refreshes_same_length_tool_metadata(monkeypatch):
    """Previously imported CLI sessions with stripped metadata must be rebuilt.

    The broken importer saved the same assistant/tool rows without tool_calls,
    tool_call_id, or tool_name. A later import after the loader fix has the same
    message count, so the refresh path must still replace the stripped messages.
    """
    import api.routes as routes

    session_id = "existing_cli_tool_session_001"
    stripped = [
        {"role": "assistant", "content": "", "timestamp": 1.0},
        {"role": "tool", "content": json.dumps({"output": "ok"}), "timestamp": 2.0},
    ]
    enriched = [
        {
            "role": "assistant",
            "content": "",
            "timestamp": 1.0,
            "tool_calls": [{"id": "call_123", "function": {"name": "terminal", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "content": json.dumps({"output": "ok"}),
            "timestamp": 2.0,
            "tool_call_id": "call_123",
            "tool_name": "terminal",
            "name": "terminal",
        },
    ]

    class FakeSession:
        def __init__(self):
            self.messages = list(stripped)
            self.source_tag = "cli"
            self.raw_source = "cli"
            self.session_source = "cli"
            self.source_label = "CLI"
            self.parent_session_id = None
            self.is_cli_session = True

        def compact(self):
            return {"session_id": session_id, "title": "Imported CLI"}

        def save(self, touch_updated_at=False):
            save_calls.append(touch_updated_at)

    save_calls = []
    existing = FakeSession()
    monkeypatch.setattr(routes.Session, "load", classmethod(lambda _cls, sid: existing if sid == session_id else None))
    monkeypatch.setattr(routes, "require", lambda body, *keys: None)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload)
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda sid: enriched if sid == session_id else [])
    monkeypatch.setattr(routes, "get_cli_sessions", lambda: [{"session_id": session_id, "source_tag": "cli", "raw_source": "cli", "session_source": "cli", "source_label": "CLI"}])

    response = routes._handle_session_import_cli(object(), {"session_id": session_id})

    assert response["imported"] is False
    assert existing.messages == enriched
    assert response["session"]["messages"] == enriched
    assert save_calls == [False]
