import json
import queue
import sqlite3
from collections import OrderedDict
from pathlib import Path

import pytest

pytestmark = pytest.mark.requires_agent_modules


def _make_state_db(path: Path, sid: str, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, started_at REAL, message_count INTEGER)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, message_count) VALUES (?, ?, ?, ?, ?, ?)",
        (sid, "webui", "Context Reconcile", "test-model", 1000.0, len(rows)),
    )
    for row in rows:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sid, row["role"], row["content"], row.get("timestamp", 1000.0)),
        )
    conn.commit()
    conn.close()


def test_next_webui_turn_context_includes_state_db_external_messages(monkeypatch, tmp_path):
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.streaming as streaming
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()

    sid = "webui_context_reconcile_001"
    sidecar_messages = [
        {"role": "user", "content": "old user", "timestamp": 1000.0},
        {"role": "assistant", "content": "old assistant", "timestamp": 1001.0},
    ]
    session = Session(
        session_id=sid,
        title="Context Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=list(sidecar_messages),
        context_messages=list(sidecar_messages),
    )
    session.active_stream_id = "stream-context-reconcile"
    session.pending_user_message = "new webui turn"
    session.pending_started_at = 1004.0
    session.save(touch_updated_at=False)
    models.SESSIONS[sid] = session

    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "user", "content": "old user", "timestamp": 1000.0},
            {"role": "assistant", "content": "old assistant", "timestamp": 1001.0},
            {"role": "user", "content": "external gateway user", "timestamp": 1002.0},
            {"role": "assistant", "content": "external gateway assistant", "timestamp": 1003.0},
        ],
    )

    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            self.session_id = sid
            self.context_compressor = None
            self.ephemeral_system_prompt = None

        def run_conversation(self, **kwargs):
            captured["conversation_history"] = kwargs.get("conversation_history")
            history = kwargs.get("conversation_history") or []
            return {
                "completed": True,
                "final_response": "ok",
                "messages": history + [
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {"role": "assistant", "content": "ok"},
                ],
            }

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *args, **kwargs: ("test-model", None, None))
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *args, **kwargs: [])

    stream_id = "stream-context-reconcile"
    config.STREAMS[stream_id] = queue.Queue()
    try:
        streaming._run_agent_streaming(
            session_id=sid,
            msg_text="new webui turn",
            model="test-model",
            workspace=str(tmp_path),
            stream_id=stream_id,
            attachments=[],
        )
    finally:
        config.STREAMS.pop(stream_id, None)

    history_contents = [m.get("content") for m in captured.get("conversation_history") or []]
    assert history_contents == [
        "old user",
        "old assistant",
        "external gateway user",
        "external gateway assistant",
    ]
