from __future__ import annotations

import queue
import sqlite3
from collections import OrderedDict
from pathlib import Path

import pytest


pytestmark = pytest.mark.requires_agent_modules


CLI_PROMPT = "Reply with exactly CLI-ORIGIN-OK and nothing else."
CLI_REPLY = "CLI-ORIGIN-OK"
WEBUI_FOLLOWUP = "What exact string did you just reply with? Answer with only that string."


def _make_cli_continuation_state_db(path: Path, *, parent_sid: str, child_sid: str, workspace: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            model TEXT,
            cwd TEXT,
            started_at REAL,
            ended_at REAL,
            end_reason TEXT,
            parent_session_id TEXT,
            message_count INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO sessions (
            id, source, title, model, cwd, started_at, ended_at, end_reason, parent_session_id, message_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (parent_sid, "cli", "CLI parent", "test-model", workspace, 1.0, 2.0, "cli_close", None, 2),
    )
    conn.execute(
        """
        INSERT INTO sessions (
            id, source, title, model, cwd, started_at, ended_at, end_reason, parent_session_id, message_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (child_sid, "cli", "CLI child", "test-model", workspace, 3.0, None, None, parent_sid, 0),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (parent_sid, "user", CLI_PROMPT, 1.0),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (parent_sid, "assistant", CLI_REPLY, 2.0),
    )
    conn.commit()
    conn.close()


def _install_cli_continuity_env(monkeypatch, tmp_path):
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes
    import api.streaming as streaming

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    state_db = tmp_path / "state.db"

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: state_db, raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(streaming, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file, raising=False)

    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.SESSION_AGENT_LOCKS.clear()
    streaming.STREAMS.clear()
    streaming.CANCEL_FLAGS.clear()
    streaming.AGENT_INSTANCES.clear()
    streaming.STREAM_PARTIAL_TEXT.clear()
    streaming.STREAM_REASONING_TEXT.clear()
    streaming.STREAM_LIVE_TOOL_CALLS.clear()

    return config, models, routes, streaming, state_db


def _capture_streaming_history(monkeypatch, config, streaming, session, *, stream_id: str, tmp_path: Path):
    captured: dict[str, list] = {}

    class FakeAgent:
        def __init__(self, **_kwargs):
            self.session_id = session.session_id
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.session_cache_read_tokens = 0
            self.session_cache_write_tokens = 0
            self._last_error = None

        def run_conversation(self, **kwargs):
            history = list(kwargs.get("conversation_history") or [])
            captured["conversation_history"] = history
            return {
                "completed": True,
                "final_response": "ok",
                "messages": history + [
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {"role": "assistant", "content": "ok"},
                ],
            }

        def interrupt(self, _message):
            return None

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: FakeAgent)
    monkeypatch.setattr(streaming, "resolve_model_provider", lambda *args, **kwargs: ("test-model", None, None))
    monkeypatch.setattr(streaming, "get_config", lambda: {})
    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(config, "_resolve_cli_toolsets", lambda *args, **kwargs: [])

    session.active_stream_id = stream_id
    session.pending_user_message = WEBUI_FOLLOWUP
    session.pending_started_at = 10.0
    session.save(touch_updated_at=False)
    config.STREAMS[stream_id] = queue.Queue()
    try:
        streaming._run_agent_streaming(
            session_id=session.session_id,
            msg_text=WEBUI_FOLLOWUP,
            model="test-model",
            workspace=str(tmp_path),
            stream_id=stream_id,
            attachments=[],
        )
    finally:
        config.STREAMS.pop(stream_id, None)
    return captured.get("conversation_history") or []


def _history_contents(history):
    return [msg.get("content") for msg in history if isinstance(msg, dict)]


def test_first_webui_followup_receives_immediate_cli_assistant_context(monkeypatch, tmp_path):
    config, models, routes, streaming, state_db = _install_cli_continuity_env(monkeypatch, tmp_path)
    sid = "issue5270_cli_child_fresh"
    _make_cli_continuation_state_db(
        state_db,
        parent_sid="issue5270_cli_parent_fresh",
        child_sid=sid,
        workspace=str(tmp_path),
    )

    session, reason = routes._claim_or_synthesize_cli_session(sid)

    assert reason == "materialized"
    assert session is not None
    assert session.read_only is False

    session.save(touch_updated_at=False)
    models.SESSIONS[sid] = session
    history = _capture_streaming_history(
        monkeypatch,
        config,
        streaming,
        session,
        stream_id="stream-issue5270-fresh",
        tmp_path=tmp_path,
    )

    assert _history_contents(history) == [CLI_PROMPT, CLI_REPLY]


def test_already_claimed_cli_sidecar_still_sees_cli_prior_assistant_on_first_webui_turn(monkeypatch, tmp_path):
    config, models, routes, streaming, state_db = _install_cli_continuity_env(monkeypatch, tmp_path)
    sid = "issue5270_cli_child_claimed"
    _make_cli_continuation_state_db(
        state_db,
        parent_sid="issue5270_cli_parent_claimed",
        child_sid=sid,
        workspace=str(tmp_path),
    )

    stale_sidecar = models.Session(
        session_id=sid,
        title="Claimed CLI child",
        workspace=str(tmp_path),
        model="test-model",
        messages=[
            {"role": "user", "content": CLI_PROMPT, "timestamp": 1.0},
        ],
        context_messages=[
            {"role": "user", "content": CLI_PROMPT, "timestamp": 1.0},
        ],
        is_cli_session=True,
        source_tag="cli",
        raw_source="cli",
        session_source="cli",
        source_label="CLI",
        read_only=False,
    )
    stale_sidecar.save(touch_updated_at=False)
    models.SESSIONS[sid] = stale_sidecar

    session = routes._get_or_materialize_session(sid, refresh_cli_messages=True)
    history = _capture_streaming_history(
        monkeypatch,
        config,
        streaming,
        session,
        stream_id="stream-issue5270-claimed",
        tmp_path=tmp_path,
    )

    assert _history_contents(history) == [CLI_PROMPT, CLI_REPLY]


def test_chat_start_refreshes_cli_messages_before_first_webui_turn(monkeypatch, tmp_path):
    _config, models, routes, _streaming, _state_db = _install_cli_continuity_env(monkeypatch, tmp_path)

    session = models.Session(
        session_id="issue5270_cli_child_chat_start",
        title="Claimed CLI child",
        workspace=str(tmp_path),
        model="test-model",
        messages=[{"role": "user", "content": CLI_PROMPT, "timestamp": 1.0}],
        context_messages=[{"role": "user", "content": CLI_PROMPT, "timestamp": 1.0}],
        is_cli_session=True,
        source_tag="cli",
        raw_source="cli",
        session_source="cli",
        source_label="CLI",
        read_only=False,
    )

    seen: dict[str, bool] = {}

    def _fake_get_or_materialize_session(sid, *, refresh_cli_messages=False):
        seen["refresh_cli_messages"] = refresh_cli_messages
        assert sid == session.session_id
        return session

    def _fake_start_run(s, **kwargs):
        assert s is session
        assert seen["refresh_cli_messages"] is True
        return {"ok": True}

    monkeypatch.setattr(routes, "_get_or_materialize_session", _fake_get_or_materialize_session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda *args, **kwargs: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *args, **kwargs: ("test-model", None, "test-model"),
    )
    monkeypatch.setattr(routes, "_start_run", _fake_start_run)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200: {"status": status, **payload})

    response = routes._handle_chat_start(
        None,
        {
            "session_id": session.session_id,
            "message": WEBUI_FOLLOWUP,
        },
    )

    assert seen["refresh_cli_messages"] is True
    assert response["ok"] is True
    assert response["status"] == 200


def test_regular_cli_sessions_remain_writable_after_fix(monkeypatch, tmp_path):
    _config, _models, routes, _streaming, state_db = _install_cli_continuity_env(monkeypatch, tmp_path)
    sid = "issue5270_cli_child_writable"
    _make_cli_continuation_state_db(
        state_db,
        parent_sid="issue5270_cli_parent_writable",
        child_sid=sid,
        workspace=str(tmp_path),
    )

    session, reason = routes._claim_or_synthesize_cli_session(sid)

    assert reason == "materialized"
    assert session is not None
    assert session.read_only is False
    assert session.is_cli_session is True
