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


def test_state_db_delta_after_context_allows_recovered_turn_prefix():
    from api.models import state_db_delta_after_context

    sidecar_context = [
        {
            "role": "user",
            "content": "alright gateway restarted, lets give it a live test...",
            "_recovered": True,
        },
        {
            "role": "assistant",
            "content": (
                "**Response interrupted.**\n\n"
                "The live response stream stopped before this turn finished. "
                "The user message above was preserved, but no agent output was recovered."
            ),
            "_error": True,
            "type": "interrupted",
        },
    ]
    state_messages = [
        {
            "role": "user",
            "content": "alright gateway restarted, lets give it a live test...",
            "timestamp": 1.0,
        },
        {"role": "assistant", "content": "old assistant", "timestamp": 2.0},
        {"role": "user", "content": "new prompt", "timestamp": 3.0},
    ]

    delta = state_db_delta_after_context(sidecar_context, state_messages)

    assert [m.get("content") for m in delta] == ["old assistant", "new prompt"]


def test_state_db_delta_after_context_does_not_promote_unrelated_prefix_as_recovered():
    from api.models import state_db_delta_after_context

    sidecar_context = [
        {"role": "user", "content": "hi", "_recovered": True},
        {"role": "user", "content": "hello"},
    ]
    state_messages = [
        {"role": "user", "content": "hello", "timestamp": 1.0},
        {"role": "assistant", "content": "response", "timestamp": 2.0},
    ]

    delta = state_db_delta_after_context(sidecar_context, state_messages)

    assert [m.get("content") for m in delta] == ["hello", "response"]


def test_webui_streaming_normalizes_trailing_prefill_user_before_current_turn(monkeypatch, tmp_path):
    import api.config as config
    import api.models as models
    import api.streaming as streaming
    from api.models import new_session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()

    captured = {}

    class FakeAgent:
        def __init__(self, prefill_messages=None, **kwargs):
            captured["prefill_messages"] = kwargs.get("prefill_messages")
            if prefill_messages is not None:
                captured["prefill_messages"] = prefill_messages
            self.context_compressor = None
            self.ephemeral_system_prompt = None

        def run_conversation(self, **kwargs):
            return {
                "completed": True,
                "final_response": "ok",
                "messages": [
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {"role": "assistant", "content": "ok"},
                ],
            }

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *args, **kwargs: ("test-model", None, None))
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *args, **kwargs: [])
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda cfg: {
        "status": "loaded",
        "source": "test",
        "label": "test",
        "message_count": 2,
        "messages": [
            {"role": "assistant", "content": "prefill summary"},
            {"role": "user", "content": "webui session context"},
        ],
    })

    s = new_session(workspace=str(tmp_path))
    stream_id = "stream-prefill-boundary-normalization"
    s.active_stream_id = stream_id
    s.pending_user_message = "new webui turn"
    s.pending_started_at = 0.0
    s.save(touch_updated_at=False)
    models.SESSIONS[s.session_id] = s

    config.STREAMS[stream_id] = queue.Queue()
    try:
        streaming._run_agent_streaming(
            session_id=s.session_id,
            msg_text="new webui turn",
            model="test-model",
            workspace=str(tmp_path),
            stream_id=stream_id,
            attachments=[],
        )
    finally:
        config.STREAMS.pop(stream_id, None)

    prefill = captured.get("prefill_messages") or []
    assert prefill == [{"role": "assistant", "content": "prefill summary"}]
