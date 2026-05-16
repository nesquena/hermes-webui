"""Regression coverage for stitched full-transcript loading across session segments."""

from __future__ import annotations

import sqlite3

import api.models as models
import api.routes as routes



def test_session_endpoint_merges_sidecar_and_lineage_messages_for_cli_sessions(monkeypatch):
    class DummySession:
        def __init__(self):
            self.messages = [{"role": "assistant", "content": "sidecar tail", "timestamp": 10.0}]
            self.tool_calls = []
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.model = "openai/gpt-5"
            self.session_id = "tip"

        def compact(self):
            return {"session_id": "tip", "title": "Tip", "model": "openai/gpt-5"}

    captured = {}

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: DummySession())
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda s: None)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {"session_source": "messaging"})
    monkeypatch.setattr(routes, "_is_messaging_session_record", lambda s: True)
    monkeypatch.setattr(
        routes,
        "get_cli_session_messages",
        lambda sid: [
            {"role": "user", "content": "root user", "timestamp": 1.0},
            {"role": "assistant", "content": "tip assistant", "timestamp": 2.0},
        ],
    )
    monkeypatch.setattr(routes, "_resolve_effective_session_model_for_display", lambda s: getattr(s, "model", None))
    monkeypatch.setattr(routes, "_resolve_effective_session_model_provider_for_display", lambda s: None)
    monkeypatch.setattr(routes, "_merge_cli_sidebar_metadata", lambda raw, meta: raw)
    monkeypatch.setattr(routes, "redact_session_data", lambda raw: raw)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200: captured.setdefault("payload", payload))

    class Handler:
        pass

    class Parsed:
        path = "/api/session"
        query = "session_id=tip"

    routes.handle_get(Handler(), Parsed())

    session = captured["payload"]["session"]
    assert [m["content"] for m in session["messages"]] == [
        "root user",
        "tip assistant",
        "sidecar tail",
    ]


def test_session_endpoint_preserves_distinct_messages_with_different_ids(monkeypatch):
    class DummySession:
        def __init__(self):
            self.messages = [
                {
                    "id": "sidecar-retry",
                    "role": "user",
                    "content": "retry the same request",
                    "timestamp": 2.0,
                }
            ]
            self.tool_calls = []
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.context_length = 0
            self.threshold_tokens = 0
            self.last_prompt_tokens = 0
            self.model = "openai/gpt-5"
            self.session_id = "tip"

        def compact(self):
            return {"session_id": "tip", "title": "Tip", "model": "openai/gpt-5"}

    captured = {}

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: DummySession())
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda s: None)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {"session_source": "messaging"})
    monkeypatch.setattr(routes, "_is_messaging_session_record", lambda s: True)
    monkeypatch.setattr(
        routes,
        "get_cli_session_messages",
        lambda sid: [
            {"role": "user", "content": "root user", "timestamp": 1.0},
            {
                "id": "cli-retry",
                "role": "user",
                "content": "retry the same request",
                "timestamp": 2.0,
            },
        ],
    )
    monkeypatch.setattr(routes, "_resolve_effective_session_model_for_display", lambda s: getattr(s, "model", None))
    monkeypatch.setattr(routes, "_resolve_effective_session_model_provider_for_display", lambda s: None)
    monkeypatch.setattr(routes, "_merge_cli_sidebar_metadata", lambda raw, meta: raw)
    monkeypatch.setattr(routes, "redact_session_data", lambda raw: raw)
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200: captured.setdefault("payload", payload))

    class Handler:
        pass

    class Parsed:
        path = "/api/session"
        query = "session_id=tip"

    routes.handle_get(Handler(), Parsed())

    session = captured["payload"]["session"]
    retry_messages = [m for m in session["messages"] if m.get("content") == "retry the same request"]
    assert [m.get("id") for m in retry_messages] == ["cli-retry", "sidecar-retry"]



def test_cli_continuation_session_opens_nonempty(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            parent_session_id TEXT,
            started_at REAL,
            ended_at REAL,
            end_reason TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            name TEXT,
            reasoning TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO sessions (id, source, parent_session_id, started_at, ended_at, end_reason)
        VALUES
            ('parent-session', 'telegram', NULL, 100.0, 200.0, 'cli_close'),
            ('child-session', 'telegram', 'parent-session', 201.0, NULL, NULL)
        """
    )
    conn.execute(
        """
        INSERT INTO messages (session_id, role, content, timestamp)
        VALUES
            ('parent-session', 'user', 'parent turn', '2026-05-14 10:00:01'),
            ('child-session', 'assistant', 'child reply', '2026-05-14 10:01:01')
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(models, '_active_state_db_path', lambda: db_path)

    messages = models.get_cli_session_messages('child-session')

    assert [message['content'] for message in messages] == ['parent turn', 'child reply']
