"""Regression coverage for #5572 imported messaging clear semantics."""

from __future__ import annotations

import json
import sqlite3
from collections import OrderedDict
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.requires_agent_modules


class _GetHandler:
    def __init__(self, path: str):
        self.path = path
        self.headers = {}
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        self.wfile = BytesIO()
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    @property
    def response_json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))

    def log_message(self, *args, **kwargs):
        pass


def _msg(role: str, content: str, ts: float, mid: str) -> dict:
    return {"id": mid, "role": role, "content": content, "timestamp": ts}


def _install_isolated_session_env(monkeypatch, tmp_path):
    import api.config as config
    import api.models as models
    import api.profiles as profiles
    import api.routes as routes

    monkeypatch.setattr(config, "STATE_DIR", tmp_path, raising=False)
    session_dir = tmp_path / "sessions"
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json", raising=False)
    monkeypatch.setattr(models, "SESSIONS", OrderedDict(), raising=False)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    monkeypatch.setattr(config, "_evict_session_agent", lambda _sid: None, raising=False)
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _post_clear(monkeypatch, sid: str):
    import api.routes as routes

    body = b'{"session_id":"%s"}' % sid.encode("utf-8")
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    original_j = routes.j

    captured = {}

    def fake_j(_handler, payload, status=200, extra_headers=None):
        captured["payload"] = payload
        captured["status"] = status
        captured["extra_headers"] = extra_headers

    monkeypatch.setattr(routes, "j", fake_j)

    handler = SimpleNamespace(
        headers={"Content-Length": str(len(body))},
        rfile=BytesIO(body),
    )
    try:
        routes.handle_post(handler, SimpleNamespace(path="/api/session/clear"))
    finally:
        monkeypatch.setattr(routes, "j", original_j)
    return captured


def _make_state_db(path, sid: str, source: str, rows):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, started_at REAL, message_count INTEGER)"
        )
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
        )
        conn.execute(
            "INSERT INTO sessions (id, source, title, model, started_at, message_count) VALUES (?, ?, ?, ?, ?, ?)",
            (sid, source, f"Imported {source}", "test-model", 1000.0, len(rows)),
        )
        for row in rows:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                (sid, row["role"], row["content"], row["timestamp"]),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("source_tag", "source_label"),
    [("telegram", "Telegram"), ("discord", "Discord")],
)
def test_session_clear_preserves_imported_messaging_transcript_and_blocks_state_db_replay(
    monkeypatch,
    tmp_path,
    source_tag,
    source_label,
):
    import api.routes as routes
    from api.models import Session, get_cli_session_messages, merge_session_messages_append_only

    _install_isolated_session_env(monkeypatch, tmp_path)

    sid = f"issue5572_clear_{source_tag}"
    session = Session(
        session_id=sid,
        title=f"Imported {source_label}",
        workspace=str(tmp_path),
        model="test-model",
        messages=[
            _msg("user", f"{source_label} sidecar prompt", 1.0, f"{source_tag}-u1"),
            _msg("assistant", f"{source_label} sidecar reply", 2.0, f"{source_tag}-a1"),
        ],
        context_messages=[
            _msg("user", f"{source_label} sidecar prompt", 1.0, f"{source_tag}-cu1"),
            _msg("assistant", f"{source_label} sidecar reply", 2.0, f"{source_tag}-ca1"),
        ],
        tool_calls=[{"id": f"{source_tag}-tool-1", "function": {"name": "terminal"}}],
        created_at=1000.0,
        updated_at=1001.0,
        active_stream_id="stale-stream",
        pending_user_message="pending prompt",
        pending_attachments=[{"name": "pending.txt"}],
        pending_started_at=1002.0,
        pending_user_source="webui",
        is_cli_session=True,
        source_tag=source_tag,
        raw_source=source_tag,
        session_source="messaging",
        source_label=source_label,
    )
    session.save(touch_updated_at=False)

    external_messages = [
        _msg("user", f"{source_label} external prompt", 10.0, f"{source_tag}-ext-u1"),
        _msg("assistant", f"{source_label} external reply", 11.0, f"{source_tag}-ext-a1"),
    ]
    _make_state_db(tmp_path / "state.db", sid, source_tag, external_messages)

    captured = _post_clear(monkeypatch, sid)

    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["session"]["title"] == "Untitled"
    assert captured["payload"]["session"]["is_cli_session"] is True
    assert captured["payload"]["session"]["source_tag"] == source_tag
    assert captured["payload"]["session"]["raw_source"] == source_tag
    assert captured["payload"]["session"]["session_source"] == "messaging"
    assert captured["payload"]["session"]["source_label"] == source_label

    loaded = Session.load(sid)
    assert loaded is not None
    assert loaded.is_cli_session is True
    assert loaded.session_source == "messaging"
    assert loaded.source_tag == source_tag
    assert loaded.raw_source == source_tag
    assert loaded.source_label == source_label
    assert loaded.messages == []
    assert loaded.context_messages == []
    assert loaded.tool_calls == []
    assert loaded.truncation_watermark == 0.0
    assert loaded.truncation_boundary == 0.0
    assert loaded.active_stream_id is None
    assert loaded.pending_user_message is None
    assert loaded.pending_attachments == []
    assert loaded.pending_started_at is None
    assert loaded.pending_user_source is None
    assert loaded.title == "Untitled"

    # Assert on the parsed on-disk payload rather than substring-matching a
    # specific whitespace layout: session files are now written as compact
    # JSON (big-session save hotpath, 2026-07-13), so '"messages": []' with a
    # space no longer appears verbatim while the semantics are unchanged.
    persisted = json.loads(loaded.path.read_text(encoding="utf-8"))
    assert persisted["messages"] == []
    assert persisted["context_messages"] == []
    assert persisted["tool_calls"] == []
    assert persisted["truncation_watermark"] == 0.0
    assert persisted["truncation_boundary"] == 0.0
    assert persisted["pending_user_message"] is None
    assert persisted["pending_started_at"] is None
    assert persisted["pending_user_source"] is None

    state_db_messages = get_cli_session_messages(sid)
    assert [(m["role"], m["content"], m["timestamp"]) for m in state_db_messages] == [
        (m["role"], m["content"], m["timestamp"]) for m in external_messages
    ]

    handler = _GetHandler(f"/api/session?session_id={sid}&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 200
    payload_messages = handler.response_json["session"]["messages"]
    assert [(m["role"], m["content"], m["timestamp"]) for m in payload_messages] == [
        (m["role"], m["content"], m["timestamp"]) for m in external_messages
    ]

    merged = merge_session_messages_append_only(
        loaded.messages,
        state_db_messages,
        truncation_watermark=loaded.truncation_watermark,
        truncation_boundary=loaded.truncation_boundary,
    )
    assert merged == []
