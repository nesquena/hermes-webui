import json
import sqlite3
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.requires_agent_modules


class _GetHandler:
    def __init__(self, path):
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

    @property
    def query(self):
        return parse_qs(urlparse(self.path).query)

    def log_message(self, *args, **kwargs):
        pass


def _make_state_db(path: Path, sid: str, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT, started_at REAL, message_count INTEGER)"
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT)"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, title, model, started_at, message_count) VALUES (?, ?, ?, ?, ?, ?)",
        (sid, "webui", "Reconcile", "test-model", 1000.0, len(rows)),
    )
    for row in rows:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, tool_call_id, tool_calls, tool_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                row["role"],
                row["content"],
                row.get("timestamp", 1000.0),
                row.get("tool_call_id"),
                row.get("tool_calls"),
                row.get("tool_name"),
            ),
        )
    conn.commit()
    conn.close()


def _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages):
    import api.config as config
    import api.models as models
    import api.routes as routes
    import api.profiles as profiles

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
    session_dir.mkdir(parents=True, exist_ok=True)

    session = models.Session(
        session_id=sid,
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=sidecar_messages,
        created_at=1000.0,
        updated_at=1001.0,
    )
    session.save(touch_updated_at=False)
    return session


def test_api_session_includes_state_db_messages_newer_than_webui_sidecar(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_001"
    sidecar_messages = [
        {"role": "user", "content": "old user", "timestamp": 1000.0},
        {"role": "assistant", "content": "old assistant", "timestamp": 1001.0},
    ]
    _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "user", "content": "old user", "timestamp": 1000.0},
            {"role": "assistant", "content": "old assistant", "timestamp": 1001.0},
            {"role": "user", "content": "external user", "timestamp": 1002.0},
            {"role": "assistant", "content": "external assistant", "timestamp": 1003.0},
        ],
    )

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    payload = handler.response_json
    messages = payload["session"]["messages"]
    assert [m["content"] for m in messages] == [
        "old user",
        "old assistant",
        "external user",
        "external assistant",
    ]
    assert payload["session"]["message_count"] == 4


def test_state_db_reconciliation_preserves_sidecar_only_messages(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_sidecar_only"
    _install_test_session(
        monkeypatch,
        tmp_path,
        sid,
        [
            {"role": "user", "content": "sidecar-only draft", "timestamp": 999.0},
            {"role": "user", "content": "old user", "timestamp": 1000.0},
        ],
    )
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "user", "content": "old user", "timestamp": 1000.0},
            {"role": "assistant", "content": "external assistant", "timestamp": 1001.0},
        ],
    )

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 200
    messages = handler.response_json["session"]["messages"]
    assert [m["content"] for m in messages] == [
        "sidecar-only draft",
        "old user",
        "external assistant",
    ]


def test_state_db_reconciliation_does_not_collapse_repeated_content_with_different_timestamps(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_repeated"
    _install_test_session(
        monkeypatch,
        tmp_path,
        sid,
        [{"role": "assistant", "content": "same", "timestamp": 1000.0}],
    )
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "assistant", "content": "same", "timestamp": 1000.0},
            {"role": "assistant", "content": "same", "timestamp": 1001.0},
        ],
    )

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 200
    messages = handler.response_json["session"]["messages"]
    assert [m["content"] for m in messages] == ["same", "same"]
    assert [m["timestamp"] for m in messages] == [1000.0, 1001.0]


def test_state_db_reconciliation_preserves_sidecar_order_when_timestamps_collide(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_same_timestamp_order"
    _install_test_session(
        monkeypatch,
        tmp_path,
        sid,
        [
            {"role": "user", "content": "z user happened first", "timestamp": 1000},
            {"role": "assistant", "content": "a assistant happened second", "timestamp": 1000},
            {"role": "tool", "content": "m tool happened third", "timestamp": 1000, "tool_call_id": "call_1"},
        ],
    )
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "user", "content": "z user happened first", "timestamp": 1000.0},
            {"role": "assistant", "content": "a assistant happened second", "timestamp": 1000.0},
            {"role": "tool", "content": "m tool happened third", "timestamp": 1000.0, "tool_call_id": "call_1"},
        ],
    )

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 200
    messages = handler.response_json["session"]["messages"]
    assert [m["content"] for m in messages] == [
        "z user happened first",
        "a assistant happened second",
        "m tool happened third",
    ]
    assert handler.response_json["session"]["message_count"] == 3


def test_state_db_reconciliation_dedupes_numeric_equivalent_timestamps(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_numeric_timestamp"
    _install_test_session(
        monkeypatch,
        tmp_path,
        sid,
        [{"role": "assistant", "content": "same timestamp", "timestamp": 1000}],
    )
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [{"role": "assistant", "content": "same timestamp", "timestamp": 1000.0}],
    )

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 200
    messages = handler.response_json["session"]["messages"]
    assert [m["content"] for m in messages] == ["same timestamp"]
    assert handler.response_json["session"]["message_count"] == 1


def test_state_db_reconciliation_preserves_repeated_sidecar_rows(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_repeated_sidecar"
    _install_test_session(
        monkeypatch,
        tmp_path,
        sid,
        [
            {"role": "assistant", "content": "", "timestamp": 1000},
            {"role": "assistant", "content": "", "timestamp": 1000},
            {"role": "assistant", "content": "done", "timestamp": 1001},
        ],
    )
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [{"role": "assistant", "content": "", "timestamp": 1000.0}],
    )

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 200
    messages = handler.response_json["session"]["messages"]
    assert [m["content"] for m in messages] == ["", "", "done"]
    assert handler.response_json["session"]["message_count"] == 3


def test_metadata_fast_path_reports_reconciled_state_db_count(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_metadata"
    _install_test_session(
        monkeypatch,
        tmp_path,
        sid,
        [
            {"role": "user", "content": "old user", "timestamp": 1000.0},
            {"role": "assistant", "content": "old assistant", "timestamp": 1001.0},
        ],
    )
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "user", "content": "old user", "timestamp": 1000.0},
            {"role": "assistant", "content": "old assistant", "timestamp": 1001.0},
            {"role": "user", "content": "external metadata user", "timestamp": 1002.0},
            {"role": "assistant", "content": "external metadata assistant", "timestamp": 1003.0},
        ],
    )

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    session = handler.response_json["session"]
    assert session["messages"] == []
    assert session["message_count"] == 4
    assert session["last_message_at"] == 1003.0


def test_state_db_reconciliation_preserves_tool_metadata(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_tool_metadata"
    _install_test_session(
        monkeypatch,
        tmp_path,
        sid,
        [{"role": "user", "content": "old user", "timestamp": 1000.0}],
    )
    tool_calls = json.dumps([{"id": "call_1", "function": {"name": "terminal"}}])
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "user", "content": "old user", "timestamp": 1000.0},
            {
                "role": "assistant",
                "content": "used a tool",
                "timestamp": 1001.0,
                "tool_calls": tool_calls,
                "tool_name": "terminal",
            },
        ],
    )

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 200
    messages = handler.response_json["session"]["messages"]
    assert messages[-1]["content"] == "used a tool"
    assert messages[-1]["tool_name"] == "terminal"
    assert messages[-1]["tool_calls"] == [{"id": "call_1", "function": {"name": "terminal"}}]
