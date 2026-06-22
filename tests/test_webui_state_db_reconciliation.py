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


def test_tail_cancelled_partial_blocks_state_db_replay():
    from api.models import merge_session_messages_append_only

    sidecar = [
        {"role": "user", "content": "cancelled turn", "timestamp": 1000.0},
        {"role": "assistant", "content": "partial answer", "_partial": True, "timestamp": 1001.0},
        {"role": "assistant", "content": "Task cancelled: stopped", "_error": True, "timestamp": 1002.0},
    ]
    state = [
        {"role": "user", "content": "cancelled turn", "timestamp": 1000.0},
        {"role": "assistant", "content": "partial answer", "timestamp": 1001.0},
        {"role": "assistant", "content": "Task cancelled: stopped", "timestamp": 1002.0},
        {"role": "assistant", "content": "raw replay after cancel", "timestamp": 1003.0},
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert [msg["content"] for msg in merged] == [
        "cancelled turn",
        "partial answer",
        "Task cancelled: stopped",
    ]


def test_historical_cancelled_partial_does_not_disable_later_state_db_merge():
    from api.models import merge_session_messages_append_only

    sidecar = [
        {"role": "user", "content": "cancelled turn", "timestamp": 1000.0},
        {"role": "assistant", "content": "partial answer", "_partial": True, "timestamp": 1001.0},
        {"role": "assistant", "content": "Task cancelled: stopped", "_error": True, "timestamp": 1002.0},
        {"role": "user", "content": "later user", "timestamp": 1003.0},
        {"role": "assistant", "content": "later answer", "timestamp": 1004.0},
    ]
    state = [
        {"role": "user", "content": "cancelled turn", "timestamp": 1000.0},
        {"role": "assistant", "content": "partial answer", "timestamp": 1001.0},
        {"role": "assistant", "content": "Task cancelled: stopped", "timestamp": 1002.0},
        {"role": "user", "content": "later user", "timestamp": 1003.0},
        {"role": "assistant", "content": "later answer", "timestamp": 1004.0},
        {"role": "user", "content": "state db only user", "timestamp": 1005.0},
        {"role": "assistant", "content": "state db only answer", "timestamp": 1006.0},
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert [msg["content"] for msg in merged][-2:] == [
        "state db only user",
        "state db only answer",
    ]


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


def test_metadata_poll_uses_sidecar_message_count_for_external_updates(monkeypatch, tmp_path):
    """Active-session external refresh relies on metadata-only counts.

    When no session index exists, metadata-only loads may fall back to
    _metadata_message_count=None. The refresh poll must still report the real
    sidecar message count; otherwise an external session JSON update can be
    invisible until a full reload.
    """
    import api.routes as routes

    sid = "webui_reconcile_metadata_sidecar"
    sidecar_messages = [
        {"role": "user", "content": "before external update", "timestamp": 1000.0},
        {"role": "assistant", "content": "externally appended", "timestamp": 1001.0},
    ]
    _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    session = handler.response_json["session"]
    assert session["message_count"] == 2
    assert session["last_message_at"] == 1001.0


def test_deferred_session_model_resolution_uses_profile_provider(monkeypatch, tmp_path):
    """Deferred GET /api/session resolution must repair against profile config."""
    import api.profiles as profiles
    import api.routes as routes

    sid = "webui_profile_resolve_model_001"
    session = _install_test_session(monkeypatch, tmp_path, sid, [])
    session.model = "openai/gpt-5.4-mini"
    session.model_provider = None
    session.profile = "anthropic"
    session.save(touch_updated_at=False)

    profile_home = tmp_path / "profiles" / "anthropic"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n"
        "  provider: anthropic\n"
        "  default: claude-sonnet-4.6\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: profile_home,
        raising=False,
    )
    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda: {
            "active_provider": "openai-codex",
            "default_model": "gpt-5.5",
            "groups": [],
        },
    )
    monkeypatch.setattr(
        routes,
        "_resolve_context_length_for_session_model",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "anthropic")

    session_path = tmp_path / "sessions" / f"{sid}.json"
    before = session_path.read_text(encoding="utf-8")

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=0&resolve_model=1")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    payload = handler.response_json["session"]
    assert payload["model"] == "claude-sonnet-4.6"
    assert payload["model_provider"] == "anthropic"
    assert session_path.read_text(encoding="utf-8") == before


def test_metadata_poll_prefers_sidecar_count_when_index_is_stale(monkeypatch, tmp_path):
    """A stale sidebar index must not hide externally appended sidecar turns."""
    import api.config as config
    import api.routes as routes

    sid = "webui_reconcile_metadata_stale_index"
    sidecar_messages = [
        {"role": "user", "content": "before stale index", "timestamp": 1000.0},
        {"role": "assistant", "content": "new sidecar turn", "timestamp": 1001.0},
    ]
    _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    config.SESSION_INDEX_FILE.write_text(
        json.dumps([{"session_id": sid, "message_count": 1}]),
        encoding="utf-8",
    )

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    session = handler.response_json["session"]
    assert session["message_count"] == 2
    assert session["last_message_at"] == 1001.0


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


def test_state_db_reconciliation_dedupes_same_second_state_rows(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_fractional_state_timestamp"
    _install_test_session(
        monkeypatch,
        tmp_path,
        sid,
        [
            {"role": "user", "content": "hi", "timestamp": 1779300509},
            {"role": "assistant", "content": "Hi there", "timestamp": 1779300509},
        ],
    )
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "user", "content": "hi", "timestamp": 1779300509.52663},
            {"role": "assistant", "content": "Hi there", "timestamp": 1779300509.52718},
        ],
    )

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 200
    session = handler.response_json["session"]
    assert [m["role"] for m in session["messages"]] == ["user", "assistant"]
    assert [m["content"] for m in session["messages"]] == ["hi", "Hi there"]
    assert session["message_count"] == 2


def test_state_db_reconciliation_preserves_same_second_state_repeats(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_fractional_state_repeats"
    _install_test_session(
        monkeypatch,
        tmp_path,
        sid,
        [{"role": "user", "content": "start", "timestamp": 1779300508}],
    )
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "assistant", "content": "Still working", "timestamp": 1779300509.12663},
            {"role": "assistant", "content": "Still working", "timestamp": 1779300509.82718},
        ],
    )

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 200
    session = handler.response_json["session"]
    assert [m["content"] for m in session["messages"]] == [
        "start",
        "Still working",
        "Still working",
    ]
    assert session["message_count"] == 3


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


def test_cancelled_partial_sidecar_owns_display_over_state_db_replay(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_cancel_partial_display_owner"
    partial_text = (
        "I am reading the RFC and current implementation.\n\n"
        "Baseline confirmed: the assistant turn must preserve visible process rows."
    )
    replay_fragment = "Baseline confirmed: the assistant turn must preserve visible process rows."
    sidecar_messages = [
        {"role": "user", "content": "review the current anchor slice", "timestamp": 1000.0},
        {
            "role": "assistant",
            "content": partial_text,
            "timestamp": 1001.0,
            "_partial": True,
            "_partial_tool_calls": [
                {"tid": "call_1", "name": "terminal", "done": True, "snippet": "pytest output"}
            ],
        },
        {
            "role": "assistant",
            "content": "**Task cancelled:** Task cancelled.",
            "timestamp": 1002.0,
            "_error": True,
            "provider_details_label": "Cancellation details",
        },
    ]
    _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "user", "content": "review the current anchor slice", "timestamp": 1000.0},
            {
                "role": "assistant",
                "content": partial_text,
                "timestamp": 1001.1,
                "tool_calls": json.dumps([{"id": "call_1", "function": {"name": "terminal"}}]),
            },
            {"role": "tool", "content": "pytest output", "timestamp": 1001.2, "tool_call_id": "call_1"},
            {
                "role": "assistant",
                "content": replay_fragment,
                "timestamp": 1001.3,
                "tool_calls": json.dumps([{"id": "call_2", "function": {"name": "terminal"}}]),
            },
        ],
    )

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    session = handler.response_json["session"]
    messages = session["messages"]
    assert [m["content"] for m in messages] == [m["content"] for m in sidecar_messages]
    assert session["message_count"] == 3
    assert sum(1 for m in messages if replay_fragment in (m.get("content") or "")) == 1


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


def test_metadata_fast_path_excludes_state_db_rows_filtered_by_reconciliation(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_metadata_filtered"
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
            # This stale state.db-only row is older than the newest sidecar
            # timestamp and lacks an explicit message id, so the full
            # append-only merge filters it out. The metadata path must report
            # the same count/last timestamp or sidebar refresh polling loops.
            {"role": "tool", "content": "stale state row", "timestamp": 1000.5},
        ],
    )

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    session = handler.response_json["session"]
    assert session["messages"] == []
    assert session["message_count"] == 2
    assert session["last_message_at"] == 1001.0


def test_api_session_reload_drops_stale_cached_user_tail_after_saved_assistant(monkeypatch, tmp_path):
    import api.models as models
    import api.routes as routes

    sid = "webui_reconcile_cached_user_tail"
    _install_test_session(
        monkeypatch,
        tmp_path,
        sid,
        [
            {"role": "user", "content": "please audit phase c", "timestamp": 1000.0},
            {"role": "assistant", "content": "final audit complete", "timestamp": 1001.0},
        ],
    )
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "user", "content": "please audit phase c", "timestamp": 1000.0},
            {"role": "assistant", "content": "final audit complete", "timestamp": 1001.0},
        ],
    )

    cached = models.Session.load(sid)
    cached.messages.append(
        {
            "role": "user",
            "content": "please audit phase c",
            "timestamp": 1002.0,
        }
    )
    cached.pending_user_message = None
    cached.active_stream_id = None
    models.SESSIONS[sid] = cached

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    messages = handler.response_json["session"]["messages"]
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "final audit complete"
    assert handler.response_json["session"]["message_count"] == 2


def test_get_session_reloads_equal_count_cached_user_tail_after_saved_assistant(monkeypatch, tmp_path):
    import api.models as models

    sid = "webui_reconcile_equal_count_user_tail"
    disk = _install_test_session(
        monkeypatch,
        tmp_path,
        sid,
        [
            {"role": "user", "content": "review anchor scene", "timestamp": 1000.0},
            {"role": "assistant", "content": "review complete", "timestamp": 1001.0},
        ],
    )
    disk.anchor_activity_scenes = {
        "assistant-final": {
            "version": "anchor_activity_scene_record_v1",
            "message_index": 1,
            "message_ref": "assistant-final",
            "stream_id": "stream-equal-count",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "activity_rows": [{"row_id": "tool-1", "role": "tool"}],
                "final_answer": "review complete",
            },
            "updated_at": 1002.0,
        }
    }
    disk.save(touch_updated_at=False)

    cached = models.Session(
        session_id=sid,
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=[
            {"role": "user", "content": "review anchor scene", "timestamp": 1000.0},
            {"role": "user", "content": "You've reached the maximum number of tool-calling iterations.", "timestamp": 1001.0},
        ],
        created_at=1000.0,
        updated_at=1001.0,
    )
    models.SESSIONS[sid] = cached

    loaded = models.get_session(sid)

    assert loaded.messages[-1]["role"] == "assistant"
    assert loaded.messages[-1]["content"] == "review complete"
    assert "assistant-final" in loaded.anchor_activity_scenes
    assert models.SESSIONS[sid] is loaded


def test_get_session_keeps_equal_count_newer_cached_user_tail(monkeypatch, tmp_path):
    import api.models as models

    sid = "webui_reconcile_equal_count_newer_user_tail"
    _install_test_session(
        monkeypatch,
        tmp_path,
        sid,
        [
            {"role": "user", "content": "old prompt", "timestamp": 1000.0},
            {"role": "assistant", "content": "old answer", "timestamp": 1001.0},
        ],
    )

    cached = models.Session(
        session_id=sid,
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=[
            {"role": "user", "content": "old prompt", "timestamp": 1000.0},
            {"role": "user", "content": "new prompt before stream id", "timestamp": 1002.0},
        ],
        created_at=1000.0,
        updated_at=1002.0,
    )
    models.SESSIONS[sid] = cached

    loaded = models.get_session(sid)

    assert loaded is cached
    assert loaded.messages[-1]["role"] == "user"
    assert loaded.messages[-1]["content"] == "new prompt before stream id"
    assert models.SESSIONS[sid] is cached


def test_get_session_reloads_when_disk_adds_anchor_scene_without_new_messages(monkeypatch, tmp_path):
    import api.models as models

    sid = "webui_reconcile_anchor_scene_delta"
    _install_test_session(
        monkeypatch,
        tmp_path,
        sid,
        [
            {"role": "user", "content": "question", "timestamp": 1000.0},
            {"role": "assistant", "content": "final answer", "timestamp": 1001.0},
        ],
    )
    cached = models.Session.load(sid)
    assert cached is not None
    models.SESSIONS[sid] = cached

    disk = models.Session.load(sid)
    disk.anchor_activity_scenes = {
        "assistant-final": {
            "version": "anchor_activity_scene_record_v1",
            "message_index": 1,
            "message_ref": "assistant-final",
            "stream_id": "stream-scene-delta",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "activity_rows": [{"row_id": "tool-1", "role": "tool"}],
                "final_answer": "final answer",
            },
            "updated_at": 1002.0,
        }
    }
    disk.save(touch_updated_at=False)

    loaded = models.get_session(sid)

    assert loaded.messages[-1]["role"] == "assistant"
    assert "assistant-final" in loaded.anchor_activity_scenes
    assert models.SESSIONS[sid] is loaded


def test_get_session_reloads_when_cached_session_lags_disk(monkeypatch, tmp_path):
    import api.models as models

    sid = "webui_reconcile_cache_lags_disk"
    old_messages = [
        {"role": "user", "content": "old user", "timestamp": 1000.0},
        {"role": "assistant", "content": "old assistant", "timestamp": 1001.0},
    ]
    _install_test_session(monkeypatch, tmp_path, sid, old_messages)

    cached = models.Session.load(sid)
    assert cached is not None
    cached.active_stream_id = "stream-cache-lags-disk"
    cached.pending_user_message = "next prompt"
    models.SESSIONS[sid] = cached

    newer = models.Session(
        session_id=sid,
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=old_messages + [
            {"role": "user", "content": "new user", "timestamp": 1002.0},
            {"role": "assistant", "content": "new final answer", "timestamp": 1003.0},
        ],
        created_at=1000.0,
        updated_at=1003.0,
        active_stream_id="stream-cache-lags-disk",
        pending_user_message="next prompt",
    )
    newer.save(touch_updated_at=False)

    loaded = models.get_session(sid)

    assert [m["content"] for m in loaded.messages] == [
        "old user",
        "old assistant",
        "new user",
        "new final answer",
    ]
    assert models.SESSIONS[sid] is loaded


def test_metadata_fast_path_uses_summary_without_full_merge_for_restamped_replays(monkeypatch, tmp_path):
    """Metadata-only /api/session must not full-read and merge transcripts.

    It still must not let a restamped replay row make sidebar polling think the
    transcript is newer than the loaded sidecar conversation.
    """
    import api.routes as routes

    sid = "webui_reconcile_metadata_replay"
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
            {"role": "user", "content": "old user", "timestamp": 1002.0},
        ],
    )
    monkeypatch.setattr(
        routes,
        "get_state_db_session_messages",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("metadata-only loads must not full-read state.db messages")
        ),
    )
    monkeypatch.setattr(
        routes,
        "merge_session_messages_append_only",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("metadata-only loads must not merge full transcripts")
        ),
    )

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=0&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    session = handler.response_json["session"]
    assert session["messages"] == []
    assert session["message_count"] == 2
    assert session["last_message_at"] == 1001.0


def test_metadata_fast_path_uses_state_db_summary_for_external_growth(monkeypatch, tmp_path):
    """Metadata-only polling can detect real external growth without a full merge."""
    import api.routes as routes

    sid = "webui_reconcile_metadata_summary_growth"
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
            {"role": "user", "content": "external user", "timestamp": 1002.0},
            {"role": "assistant", "content": "external assistant", "timestamp": 1003.0},
        ],
    )
    monkeypatch.setattr(
        routes,
        "get_state_db_session_messages",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("metadata-only loads must not full-read state.db messages")
        ),
    )
    monkeypatch.setattr(
        routes,
        "merge_session_messages_append_only",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("metadata-only loads must not merge full transcripts")
        ),
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
