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


def _append_state_db_rows(path: Path, sid: str, rows):
    conn = sqlite3.connect(path)
    try:
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
        conn.execute(
            "UPDATE sessions SET message_count = (SELECT COUNT(*) FROM messages WHERE session_id = ?) WHERE id = ?",
            (sid, sid),
        )
        conn.commit()
    finally:
        conn.close()


def _disjoint_parent_boundary(parent, child_messages):
    from api.models import _compression_child_messages_digest

    return {
        "parent_session_id": parent.session_id,
        "message_count": len(parent.messages),
        "parent_updated_at": parent.updated_at,
        "child_message_count": len(child_messages),
        "child_messages_digest": _compression_child_messages_digest(child_messages),
        "canonical_parent_proof": True,
    }


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


def _large_timestamped_sidecar_messages(count=500):
    return [
        {"role": "user", "content": f"sidecar {idx}", "timestamp": float(idx)}
        for idx in range(count)
    ]


def test_sidebar_state_db_overlay_preserves_numeric_actual_count():
    import api.models as models

    sid = "webui_float_actual_count"
    sessions = [
        {
            "session_id": sid,
            "source_tag": "webui",
            "message_count": 2,
            "actual_message_count": 5.0,
            "last_message_at": 1001.0,
            "updated_at": 1001.0,
        }
    ]

    models._apply_sidebar_state_db_override_metadata(
        sessions,
        {
            sid: {
                "_state_db_source": "webui",
                "_state_db_message_count": 4,
                "_state_db_last_message_at": 1003.0,
            }
        },
    )

    assert sessions[0]["message_count"] == 4
    assert sessions[0]["actual_message_count"] == 5


def test_sidebar_state_db_overlay_counts_subagent_child_5308():
    """#5308: a delegated subagent child whose stale sidecar reports
    message_count == 0 must receive its real state.db message count so the
    front-end sidebar visibility predicate does not drop the row (the subagent
    session vanishing regression). The overlay applies to source == 'subagent'
    just like 'webui', while the subagent source classification is preserved.
    """
    import api.models as models

    sid = "subagent_child_5308"
    sessions = [
        {
            "session_id": sid,
            "source_tag": "subagent",
            "raw_source": "subagent",
            "session_source": "other",
            "relationship_type": "child_session",
            "parent_session_id": "parent_abc",
            "message_count": 0,
            "actual_message_count": 0,
            "last_message_at": 0,
            "updated_at": 0,
        }
    ]

    models._apply_sidebar_state_db_override_metadata(
        sessions,
        {
            sid: {
                "_state_db_source": "subagent",
                "_state_db_message_count": 6,
                "_state_db_last_message_at": 2002.0,
            }
        },
    )

    # Real state.db count is now overlaid (was 0) -> row survives the sidebar
    # visibility predicate instead of vanishing.
    assert sessions[0]["message_count"] == 6
    assert sessions[0]["actual_message_count"] == 6
    assert sessions[0]["last_message_at"] == 2002.0
    # Subagent classification is preserved (source-tag reassignment stays
    # WebUI-only) — the child does not get re-tagged as a webui session.
    assert sessions[0]["source_tag"] == "subagent"
    assert sessions[0].get("is_cli_session") is not False


def test_sidebar_state_db_overlay_does_not_count_foreign_cli_source_5308():
    """Guard the #5308 overlay scope: a non-webui, non-subagent foreign source
    (e.g. a messaging/cron/tui CLI row) must NOT get the count overlay — only
    WebUI-owned rows and delegated subagent children do.
    """
    import api.models as models

    sid = "cron_row_5308"
    sessions = [
        {
            "session_id": sid,
            "source_tag": "cron",
            "message_count": 0,
            "actual_message_count": 0,
            "last_message_at": 0,
            "updated_at": 0,
        }
    ]

    models._apply_sidebar_state_db_override_metadata(
        sessions,
        {
            sid: {
                "_state_db_source": "cron",
                "_state_db_message_count": 9,
                "_state_db_last_message_at": 3003.0,
            }
        },
    )

    # cron is neither webui nor subagent -> count overlay must NOT apply.
    assert sessions[0]["message_count"] == 0
    assert sessions[0]["actual_message_count"] == 0


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


def test_state_db_duplicate_backfills_turn_duration():
    from api.models import merge_session_messages_append_only

    sidecar = [
        {"role": "assistant", "content": "final answer", "timestamp": 1001.0},
    ]
    state = [
        {
            "role": "assistant",
            "content": "final answer",
            "timestamp": 1001.0,
            "_turnDuration": 42.5,
        },
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert len(merged) == 1
    assert merged[0]["_turnDuration"] == 42.5


def test_api_sessions_overlays_webui_state_db_summary_after_desktop_append(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_desktop_sidebar_reconcile"
    sidecar_messages = [
        {"role": "user", "content": "old user", "timestamp": 1000.0},
        {"role": "assistant", "content": "old assistant", "timestamp": 1001.0},
    ]
    _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(tmp_path / "state.db", sid, list(sidecar_messages))
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": False})
    routes._clear_session_list_cache()

    first = _GetHandler("/api/sessions?sidebar_source=webui")
    routes.handle_get(first, urlparse(first.path))
    assert first.status == 200
    first_row = next(row for row in first.response_json["sessions"] if row["session_id"] == sid)
    assert first_row["message_count"] == 2

    # Simulate the official Hermes Desktop App continuing the same WebUI-origin
    # Hermes Agent session and settling its final rows into state.db. The second
    # request happens immediately, so it only updates if the WebUI sidebar cache
    # observes state.db changes even when the CLI/external-session tab is hidden.
    _append_state_db_rows(
        tmp_path / "state.db",
        sid,
        [
            {"role": "user", "content": "desktop user", "timestamp": 1002.0},
            {"role": "assistant", "content": "desktop assistant", "timestamp": 1003.0},
        ],
    )

    second = _GetHandler("/api/sessions?sidebar_source=webui")
    routes.handle_get(second, urlparse(second.path))
    assert second.status == 200
    row = next(row for row in second.response_json["sessions"] if row["session_id"] == sid)
    assert row["message_count"] == 4
    assert row["last_message_at"] == 1003.0
    assert row["updated_at"] == 1003.0


def test_api_session_full_load_does_not_duplicate_state_db_prefix(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_desktop_full_reconcile"
    sidecar_messages = [
        {"role": "user", "content": "turn 1", "timestamp": 1000.0},
        {"role": "assistant", "content": "answer 1", "timestamp": 1001.0},
        {"role": "user", "content": "turn 2", "timestamp": 1002.0},
    ]
    desktop_tail = [
        {"role": "assistant", "content": "answer 2 from desktop", "timestamp": 1003.0},
        {"role": "user", "content": "desktop follow-up", "timestamp": 1004.0},
        {"role": "assistant", "content": "desktop final", "timestamp": 1005.0},
    ]
    _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(tmp_path / "state.db", sid, sidecar_messages + desktop_tail)

    handler = _GetHandler(f"/api/session?session_id={sid}&messages=1&resolve_model=0")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    messages = handler.response_json["session"]["messages"]
    assert [m["content"] for m in messages] == [
        "turn 1",
        "answer 1",
        "turn 2",
        "answer 2 from desktop",
        "desktop follow-up",
        "desktop final",
    ]
    assert handler.response_json["session"]["message_count"] == 6


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


def test_state_db_reader_can_filter_by_timestamp_floor(monkeypatch, tmp_path):
    import api.models as models

    sid = "webui_reconcile_since_001"
    _install_test_session(monkeypatch, tmp_path, sid, [])
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "user", "content": "old", "timestamp": 10.0},
            {"role": "assistant", "content": "kept", "timestamp": 20.0},
            {"role": "user", "content": "also kept", "timestamp": 30.0},
        ],
    )

    messages = models.get_state_db_session_messages(sid, since_timestamp=20.0)

    assert [m["content"] for m in messages] == ["kept", "also kept"]


def test_state_db_reader_since_timestamp_keeps_null_timestamp_rows(monkeypatch, tmp_path):
    import api.models as models

    sid = "webui_reconcile_since_null_001"
    _install_test_session(monkeypatch, tmp_path, sid, [])
    _make_state_db(
        tmp_path / "state.db",
        sid,
        [
            {"role": "user", "content": "old", "timestamp": 10.0},
            {"role": "assistant", "content": "null timestamp kept", "timestamp": None},
            {"role": "assistant", "content": "kept", "timestamp": 20.0},
        ],
    )

    messages = models.get_state_db_session_messages(sid, since_timestamp=20.0)

    assert [m["content"] for m in messages] == ["null timestamp kept", "kept"]


def test_limited_display_with_precomputed_sidecar_keeps_empty_state_db_guard(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_empty_state_guard"
    sidecar_messages = [
        {"role": "user", "content": "sidecar only", "timestamp": 10.0},
    ]
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)

    messages = routes._limited_webui_messages_for_display_with_sidecar(
        session,
        list(sidecar_messages),
        [],
    )

    assert messages == sidecar_messages


def test_msg_limit_session_load_reads_only_recent_state_db_tail(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_limited_tail"
    sidecar_messages = [
        {"role": "user", "content": f"sidecar {idx}", "timestamp": float(idx)}
        for idx in range(500)
    ]
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(
        tmp_path / "state.db",
        sid,
        sidecar_messages
        + [
            {"role": "user", "content": "external user", "timestamp": 500.0},
            {"role": "assistant", "content": "external answer", "timestamp": 501.0},
        ],
    )

    real_reader = routes.get_state_db_session_messages
    full_state_messages = real_reader(sid)
    full_all_messages = routes._limited_webui_messages_for_display(
        session,
        full_state_messages,
    )
    expected_window, expected_offset = routes._message_window_for_display(
        full_all_messages,
        msg_limit=30,
    )
    captured = {}

    def wrapped_reader(*args, **kwargs):
        captured["since_timestamp"] = kwargs.get("since_timestamp")
        messages = real_reader(*args, **kwargs)
        captured["row_count"] = len(messages)
        return messages

    monkeypatch.setattr(routes, "get_state_db_session_messages", wrapped_reader)

    handler = _GetHandler(
        f"/api/session?session_id={sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    assert captured["since_timestamp"] == 200.0
    assert captured["row_count"] == 302
    messages = handler.response_json["session"]["messages"]
    assert messages == expected_window
    assert handler.response_json["session"]["_messages_offset"] == expected_offset
    assert messages[0]["content"] == "sidecar 472"
    assert messages[-2]["content"] == "external user"
    assert messages[-1]["content"] == "external answer"


def test_msg_limit_large_cumulative_compression_child_falls_back_when_ambiguous(
    monkeypatch,
    tmp_path,
):
    import api.models as models
    import api.routes as routes

    parent_sid = "parent_compression_snapshot"
    parent_messages = [
        {"role": "user", "content": f"parent {idx}", "timestamp": float(idx)}
        for idx in range(120)
    ]
    parent = _install_test_session(monkeypatch, tmp_path, parent_sid, parent_messages)
    parent.pre_compression_snapshot = True
    parent.updated_at = 200.0
    parent.save(touch_updated_at=False)

    child_messages = parent_messages + [
        {"role": "user", "content": f"sidecar {idx}", "timestamp": 300.0 + idx}
        for idx in range(500)
    ]
    session = models.Session(
        session_id="cumulative_child",
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=child_messages,
        parent_session_id=parent_sid,
        created_at=300.0,
        updated_at=900.0,
    )
    session.save(touch_updated_at=False)

    real_lineage_loader = routes._webui_sidecar_lineage_messages_for_display
    captured = {"lineage_loads": 0}

    def wrapped_lineage_loader(*args, **kwargs):
        captured["lineage_loads"] += 1
        return real_lineage_loader(*args, **kwargs)

    monkeypatch.setattr(routes, "_webui_sidecar_lineage_messages_for_display", wrapped_lineage_loader)

    since_timestamp, selected_sidecar, base_offset = routes._state_db_since_timestamp_for_limited_display(
        session,
        msg_limit=30,
    )

    assert since_timestamp is None
    assert captured["lineage_loads"] == 1
    assert selected_sidecar == child_messages
    assert base_offset == 0


@pytest.mark.parametrize(
    ("tool_payload_size", "expected_since_timestamp"),
    [
        (32, None),
        (510_000, 1.0),
    ],
)
def test_msg_limit_byte_threshold_preserves_base_offset_contract(
    monkeypatch,
    tmp_path,
    tool_payload_size,
    expected_since_timestamp,
):
    """Both byte-gate branches must keep the compression offset return shape."""
    import api.routes as routes

    sid = f"byte_sidecar_contract_{tool_payload_size}"
    sidecar_messages = [
        {"role": "user", "content": "start", "timestamp": 1.0},
        {"role": "tool", "content": "x" * tool_payload_size, "timestamp": 2.0},
    ]
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(tmp_path / "state.db", sid, sidecar_messages)

    since_timestamp, selected_sidecar, base_offset = (
        routes._state_db_since_timestamp_for_limited_display(
            session,
            msg_limit=30,
        )
    )

    assert since_timestamp == expected_since_timestamp
    assert selected_sidecar == sidecar_messages
    assert base_offset == 0


def test_msg_limit_compression_child_fast_path_preserves_pagination_cursor(
    monkeypatch,
    tmp_path,
):
    import api.models as models
    import api.routes as routes

    parent_sid = "parent_compression_snapshot_cursor"
    child_sid = "continuation_child_cursor"
    parent_messages = [
        {"role": "user", "content": f"parent {idx}", "timestamp": float(idx)}
        for idx in range(120)
    ]
    child_messages = [
        {"role": "user", "content": f"child {idx}", "timestamp": 300.0 + idx}
        for idx in range(500)
    ]
    parent = _install_test_session(monkeypatch, tmp_path, parent_sid, parent_messages)
    parent.pre_compression_snapshot = True
    parent.updated_at = 200.0
    parent.save(touch_updated_at=False)
    child = models.Session(
        session_id=child_sid,
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=child_messages,
        parent_session_id=parent_sid,
        compression_disjoint_parent_boundary=_disjoint_parent_boundary(parent, child_messages),
        created_at=300.0,
        updated_at=900.0,
    )
    child.save(touch_updated_at=False)
    _make_state_db(tmp_path / "state.db", child_sid, child_messages)

    real_lineage_loader = routes._webui_sidecar_lineage_messages_for_display
    captured = {"lineage_loads": 0}

    def wrapped_lineage_loader(*args, **kwargs):
        captured["lineage_loads"] += 1
        return real_lineage_loader(*args, **kwargs)

    monkeypatch.setattr(routes, "_webui_sidecar_lineage_messages_for_display", wrapped_lineage_loader)

    first = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(first, urlparse(first.path))

    assert first.status == 200
    assert captured["lineage_loads"] == 0
    first_payload = first.response_json["session"]
    assert first_payload["message_count"] == len(parent_messages) + len(child_messages)
    assert first_payload["_messages_offset"] == len(parent_messages) + 470
    assert [msg["content"] for msg in first_payload["messages"]] == [
        f"child {idx}" for idx in range(470, 500)
    ]

    second = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0"
        f"&msg_limit=30&msg_before={first_payload['_messages_offset']}"
    )
    routes.handle_get(second, urlparse(second.path))

    assert second.status == 200
    assert captured["lineage_loads"] == 1
    second_payload = second.response_json["session"]
    assert second_payload["message_count"] == len(parent_messages) + len(child_messages)
    assert second_payload["_messages_offset"] == len(parent_messages) + 440
    assert [msg["content"] for msg in second_payload["messages"]] == [
        f"child {idx}" for idx in range(440, 470)
    ]


def test_msg_limit_partial_parent_suffix_replay_falls_back_with_contiguous_pagination(
    monkeypatch,
    tmp_path,
):
    """A stale disjoint marker must not authorize a child rewritten with a parent suffix."""
    import api.models as models
    import api.routes as routes

    parent_sid = "parent_compression_snapshot_suffix_replay"
    child_sid = "continuation_child_suffix_replay"
    parent_messages = [
        {"role": "user", "content": f"parent {idx}", "timestamp": float(idx)}
        for idx in range(1000)
    ]
    new_child_messages = [
        {"role": "assistant", "content": f"child {idx}", "timestamp": 1000.0 + idx}
        for idx in range(400)
    ]
    child_messages = parent_messages[-100:] + new_child_messages
    parent = _install_test_session(monkeypatch, tmp_path, parent_sid, parent_messages)
    parent.pre_compression_snapshot = True
    parent.updated_at = 800.0
    parent.save(touch_updated_at=False)
    child = models.Session(
        session_id=child_sid,
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=new_child_messages,
        parent_session_id=parent_sid,
        compression_disjoint_parent_boundary=_disjoint_parent_boundary(
            parent,
            new_child_messages,
        ),
        created_at=900.0,
        updated_at=1500.0,
    )
    child.save(touch_updated_at=False)
    # Simulate a later recovery rewrite that replays part of the parent while
    # retaining the once-valid marker. The marker's child digest/count must make
    # this fail closed instead of restoring the 1,500-row metadata overcount.
    child.messages = child_messages
    child.save(touch_updated_at=False)
    _make_state_db(tmp_path / "state.db", child_sid, child_messages)

    real_lineage_loader = routes._webui_sidecar_lineage_messages_for_display
    real_session_load = models.Session.load.__func__
    captured = {"lineage_loads": 0, "parent_full_loads": 0}

    def wrapped_lineage_loader(*args, **kwargs):
        captured["lineage_loads"] += 1
        return real_lineage_loader(*args, **kwargs)

    def wrapped_session_load(cls, sid):
        if sid == parent_sid:
            captured["parent_full_loads"] += 1
        return real_session_load(cls, sid)

    monkeypatch.setattr(routes, "_webui_sidecar_lineage_messages_for_display", wrapped_lineage_loader)
    monkeypatch.setattr(models.Session, "load", classmethod(wrapped_session_load))

    first = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(first, urlparse(first.path))

    assert first.status == 200
    assert captured["lineage_loads"] >= 1
    assert captured["parent_full_loads"] >= 1
    first_payload = first.response_json["session"]
    assert first_payload["message_count"] == 1400
    assert first_payload["_messages_offset"] == 1370
    assert [msg["content"] for msg in first_payload["messages"]] == [
        f"child {idx}" for idx in range(370, 400)
    ]

    second = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0"
        f"&msg_limit=30&msg_before={first_payload['_messages_offset']}"
    )
    routes.handle_get(second, urlparse(second.path))

    assert second.status == 200
    second_payload = second.response_json["session"]
    assert second_payload["message_count"] == 1400
    assert second_payload["_messages_offset"] == 1340
    assert [msg["content"] for msg in second_payload["messages"]] == [
        f"child {idx}" for idx in range(340, 370)
    ]


def test_msg_limit_canonicalized_parent_falls_back_with_contiguous_pagination(
    monkeypatch,
    tmp_path,
):
    """A raw parent that canonicalizes shorter must never mint a fast-path offset."""
    import api.models as models
    import api.routes as routes

    parent_sid = "parent_compression_snapshot_canonicalized"
    child_sid = "continuation_child_canonicalized"
    parent_messages = [
        {"role": "user", "content": f"parent {idx}", "timestamp": float(idx)}
        for idx in range(999)
    ]
    parent_messages.insert(470, dict(parent_messages[470]))
    child_messages = [
        {"role": "assistant", "content": f"child {idx}", "timestamp": 1000.0 + idx}
        for idx in range(500)
    ]
    parent = _install_test_session(monkeypatch, tmp_path, parent_sid, parent_messages)
    parent.pre_compression_snapshot = True
    parent.updated_at = 800.0
    parent.save(touch_updated_at=False)

    child = models.Session(
        session_id=child_sid,
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=child_messages,
        parent_session_id=parent_sid,
        compression_disjoint_parent_boundary={
            key: value
            for key, value in _disjoint_parent_boundary(parent, child_messages).items()
            if key != "canonical_parent_proof"
        },
        created_at=1000.0,
        updated_at=1600.0,
    )
    child.save(touch_updated_at=False)
    _make_state_db(tmp_path / "state.db", child_sid, child_messages)

    real_lineage_loader = routes._webui_sidecar_lineage_messages_for_display
    captured = {"lineage_loads": 0}

    def wrapped_lineage_loader(*args, **kwargs):
        captured["lineage_loads"] += 1
        return real_lineage_loader(*args, **kwargs)

    monkeypatch.setattr(routes, "_webui_sidecar_lineage_messages_for_display", wrapped_lineage_loader)

    first = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(first, urlparse(first.path))

    assert first.status == 200
    assert captured["lineage_loads"] >= 1
    first_payload = first.response_json["session"]
    assert first_payload["message_count"] == 1499
    assert first_payload["_messages_offset"] == 1469
    assert [msg["content"] for msg in first_payload["messages"]] == [
        f"child {idx}" for idx in range(470, 500)
    ]

    second = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0"
        f"&msg_limit=30&msg_before={first_payload['_messages_offset']}"
    )
    routes.handle_get(second, urlparse(second.path))

    assert second.status == 200
    second_payload = second.response_json["session"]
    assert second_payload["message_count"] == 1499
    assert second_payload["_messages_offset"] == 1439
    first_contents = {msg["content"] for msg in first_payload["messages"]}
    second_contents = {msg["content"] for msg in second_payload["messages"]}
    assert not first_contents.intersection(second_contents)
    assert [msg["content"] for msg in second_payload["messages"]] == [
        f"child {idx}" for idx in range(440, 470)
    ]


def test_msg_limit_truncated_parent_metadata_falls_back_to_full_lineage(
    monkeypatch,
    tmp_path,
):
    """A truncated metadata-only parent cannot supply canonical display coordinates."""
    import api.models as models
    import api.routes as routes

    parent_sid = "parent_compression_snapshot_truncated"
    child_sid = "continuation_child_truncated_parent"
    parent_messages = [
        {"role": "user", "content": f"parent {idx}", "timestamp": float(idx)}
        for idx in range(120)
    ]
    child_messages = [
        {"role": "assistant", "content": f"child {idx}", "timestamp": 300.0 + idx}
        for idx in range(500)
    ]
    parent = _install_test_session(monkeypatch, tmp_path, parent_sid, parent_messages)
    parent.pre_compression_snapshot = True
    parent.truncation_watermark = 50.0
    parent.truncation_boundary = 50.0
    parent.updated_at = 200.0
    parent.save(touch_updated_at=False)
    child = models.Session(
        session_id=child_sid,
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=child_messages,
        parent_session_id=parent_sid,
        compression_disjoint_parent_boundary=_disjoint_parent_boundary(parent, child_messages),
        created_at=300.0,
        updated_at=900.0,
    )
    child.save(touch_updated_at=False)
    _make_state_db(tmp_path / "state.db", child_sid, child_messages)

    real_lineage_loader = routes._webui_sidecar_lineage_messages_for_display
    captured = {"lineage_loads": 0}

    def wrapped_lineage_loader(*args, **kwargs):
        captured["lineage_loads"] += 1
        return real_lineage_loader(*args, **kwargs)

    monkeypatch.setattr(routes, "_webui_sidecar_lineage_messages_for_display", wrapped_lineage_loader)

    first = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(first, urlparse(first.path))

    assert first.status == 200
    assert captured["lineage_loads"] >= 1
    payload = first.response_json["session"]
    assert payload["message_count"] == 551
    assert payload["_messages_offset"] == 521
    assert [msg["content"] for msg in payload["messages"]] == [
        f"child {idx}" for idx in range(470, 500)
    ]


@pytest.mark.parametrize(
    ("child_todo_state", "expected_todo_ids"),
    [
        (None, ["parent-todo"]),
        pytest.param(
            "large-parent",
            [f"parent-todo-{idx}" for idx in range(20)],
            id="large-parent-todo-over-64KiB",
        ),
        ("task", ["child-todo"]),
        ("empty", []),
    ],
)
def test_msg_limit_compression_child_fast_path_preserves_parent_todo_state_without_lineage(
    monkeypatch,
    tmp_path,
    child_todo_state,
    expected_todo_ids,
):
    import api.models as models
    import api.routes as routes

    parent_sid = "parent_compression_snapshot_todo"
    child_sid = "continuation_child_todo"
    parent_messages = [
        {"role": "user", "content": f"parent {idx}", "timestamp": float(idx)}
        for idx in range(120)
    ]
    parent_todos = [
        {"id": "parent-todo", "content": "survive compression", "status": "pending"}
    ]
    if child_todo_state == "large-parent":
        parent_todos = [
            {"id": f"parent-todo-{idx}", "content": "x" * 4000, "status": "pending"}
            for idx in range(20)
        ]
    parent_messages[100] = {
        "role": "tool",
        "content": json.dumps({"todos": parent_todos}),
        "timestamp": 100.0,
    }
    child_messages = [
        {"role": "assistant", "content": f"child {idx}", "timestamp": 300.0 + idx}
        for idx in range(500)
    ]
    if child_todo_state not in (None, "large-parent"):
        child_todos = []
        if child_todo_state == "task":
            child_todos = [
                {"id": "child-todo", "content": "new continuation state", "status": "in_progress"}
            ]
        child_messages[100] = {
            "role": "tool",
            "content": json.dumps({"todos": child_todos}),
            "timestamp": 400.0,
        }

    parent = _install_test_session(monkeypatch, tmp_path, parent_sid, parent_messages)
    parent.pre_compression_snapshot = True
    parent.updated_at = 200.0
    parent.save(touch_updated_at=False)
    child = models.Session(
        session_id=child_sid,
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=child_messages,
        parent_session_id=parent_sid,
        compression_disjoint_parent_boundary=_disjoint_parent_boundary(parent, child_messages),
        created_at=300.0,
        updated_at=900.0,
    )
    child.save(touch_updated_at=False)
    _make_state_db(tmp_path / "state.db", child_sid, child_messages)

    real_lineage_loader = routes._webui_sidecar_lineage_messages_for_display
    real_session_load = models.Session.load.__func__
    captured = {"lineage_loads": 0, "parent_full_loads": 0}

    def wrapped_lineage_loader(*args, **kwargs):
        captured["lineage_loads"] += 1
        return real_lineage_loader(*args, **kwargs)

    def wrapped_session_load(cls, sid):
        if sid == parent_sid:
            captured["parent_full_loads"] += 1
        return real_session_load(cls, sid)

    monkeypatch.setattr(routes, "_webui_sidecar_lineage_messages_for_display", wrapped_lineage_loader)
    monkeypatch.setattr(models.Session, "load", classmethod(wrapped_session_load))

    handler = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    assert captured["lineage_loads"] == 0
    assert captured["parent_full_loads"] == 0
    payload = handler.response_json["session"]
    assert payload["message_count"] == len(parent_messages) + len(child_messages)
    assert payload["_messages_offset"] == len(parent_messages) + 470
    assert [todo["id"] for todo in payload["todo_state"]["todos"]] == expected_todo_ids


def test_msg_limit_compression_child_legacy_parent_todo_falls_back_to_lineage(
    monkeypatch,
    tmp_path,
):
    import api.models as models
    import api.routes as routes

    parent_sid = "legacy_parent_compression_snapshot_todo"
    child_sid = "legacy_continuation_child_todo"
    parent_messages = [
        {"role": "user", "content": f"parent {idx}", "timestamp": float(idx)}
        for idx in range(120)
    ]
    parent_messages[100] = {
        "role": "tool",
        "content": json.dumps({
            "todos": [
                {"id": "legacy-parent-todo", "content": "preserve legacy state", "status": "pending"}
            ]
        }),
        "timestamp": 100.0,
    }
    child_messages = [
        {"role": "assistant", "content": f"child {idx}", "timestamp": 300.0 + idx}
        for idx in range(500)
    ]

    parent = _install_test_session(monkeypatch, tmp_path, parent_sid, parent_messages)
    parent.pre_compression_snapshot = True
    parent.updated_at = 200.0
    parent.save(touch_updated_at=False)
    legacy_payload = json.loads(parent.path.read_text(encoding="utf-8"))
    legacy_payload.pop("todo_state", None)
    parent.path.write_text(json.dumps(legacy_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    child = models.Session(
        session_id=child_sid,
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=child_messages,
        parent_session_id=parent_sid,
        compression_disjoint_parent_boundary=_disjoint_parent_boundary(parent, child_messages),
        created_at=300.0,
        updated_at=900.0,
    )
    child.save(touch_updated_at=False)
    _make_state_db(tmp_path / "state.db", child_sid, child_messages)

    real_lineage_loader = routes._webui_sidecar_lineage_messages_for_display
    captured = {"lineage_loads": 0}

    def wrapped_lineage_loader(*args, **kwargs):
        captured["lineage_loads"] += 1
        return real_lineage_loader(*args, **kwargs)

    monkeypatch.setattr(routes, "_webui_sidecar_lineage_messages_for_display", wrapped_lineage_loader)

    handler = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    assert captured["lineage_loads"] >= 1
    payload = handler.response_json["session"]
    assert payload["message_count"] == len(parent_messages) + len(child_messages)
    assert payload["todo_state"]["todos"][0]["id"] == "legacy-parent-todo"


def test_msg_limit_single_hop_fast_path_falls_back_for_state_db_only_prefix_row(
    monkeypatch,
    tmp_path,
):
    import api.models as models
    import api.routes as routes

    parent_sid = "parent_compression_snapshot_state_prefix"
    child_sid = "continuation_child_state_prefix"
    parent_messages = [
        {"role": "user", "content": f"parent {idx}", "timestamp": float(idx)}
        for idx in range(120)
    ]
    child_messages = [
        {"role": "user", "content": f"child {idx}", "timestamp": 300.0 + idx}
        for idx in range(500)
    ]
    state_only_prefix = {"role": "user", "content": "state.db only bridge", "timestamp": 250.0}
    parent = _install_test_session(monkeypatch, tmp_path, parent_sid, parent_messages)
    parent.pre_compression_snapshot = True
    parent.updated_at = 200.0
    parent.save(touch_updated_at=False)
    child = models.Session(
        session_id=child_sid,
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=child_messages,
        parent_session_id=parent_sid,
        compression_disjoint_parent_boundary=_disjoint_parent_boundary(parent, child_messages),
        created_at=300.0,
        updated_at=900.0,
    )
    child.save(touch_updated_at=False)
    _make_state_db(tmp_path / "state.db", child_sid, [state_only_prefix] + child_messages)

    real_lineage_loader = routes._webui_sidecar_lineage_messages_for_display
    captured = {"lineage_loads": 0}

    def wrapped_lineage_loader(*args, **kwargs):
        captured["lineage_loads"] += 1
        return real_lineage_loader(*args, **kwargs)

    monkeypatch.setattr(routes, "_webui_sidecar_lineage_messages_for_display", wrapped_lineage_loader)

    first = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(first, urlparse(first.path))

    expected_count = len(parent_messages) + 1 + len(child_messages)
    assert first.status == 200
    assert captured["lineage_loads"] >= 1
    first_payload = first.response_json["session"]
    assert first_payload["message_count"] == expected_count
    assert first_payload["_messages_offset"] == len(parent_messages) + 1 + 470
    assert [msg["content"] for msg in first_payload["messages"]] == [
        f"child {idx}" for idx in range(470, 500)
    ]

    second = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0"
        f"&msg_limit=30&msg_before={first_payload['_messages_offset']}"
    )
    routes.handle_get(second, urlparse(second.path))

    assert second.status == 200
    second_payload = second.response_json["session"]
    assert second_payload["message_count"] == expected_count
    assert second_payload["_messages_offset"] == len(parent_messages) + 1 + 440
    assert [msg["content"] for msg in second_payload["messages"]] == [
        f"child {idx}" for idx in range(440, 470)
    ]


def test_msg_limit_single_hop_fast_path_revalidates_late_state_db_prefix_row(
    monkeypatch,
    tmp_path,
):
    import api.models as models
    import api.routes as routes

    parent_sid = "parent_compression_snapshot_late_state_prefix"
    child_sid = "continuation_child_late_state_prefix"
    parent_messages = [
        {"role": "user", "content": f"parent {idx}", "timestamp": float(idx)}
        for idx in range(120)
    ]
    child_messages = [
        {"role": "user", "content": f"child {idx}", "timestamp": 300.0 + idx}
        for idx in range(500)
    ]
    state_only_prefix = {"role": "user", "content": "late state.db bridge", "timestamp": 250.0}
    parent = _install_test_session(monkeypatch, tmp_path, parent_sid, parent_messages)
    parent.pre_compression_snapshot = True
    parent.updated_at = 200.0
    parent.save(touch_updated_at=False)
    child = models.Session(
        session_id=child_sid,
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=child_messages,
        parent_session_id=parent_sid,
        compression_disjoint_parent_boundary=_disjoint_parent_boundary(parent, child_messages),
        created_at=300.0,
        updated_at=900.0,
    )
    child.save(touch_updated_at=False)
    _make_state_db(tmp_path / "state.db", child_sid, child_messages)

    real_lineage_loader = routes._webui_sidecar_lineage_messages_for_display
    captured = {"lineage_loads": 0}

    def wrapped_lineage_loader(*args, **kwargs):
        captured["lineage_loads"] += 1
        return real_lineage_loader(*args, **kwargs)

    monkeypatch.setattr(routes, "_webui_sidecar_lineage_messages_for_display", wrapped_lineage_loader)

    since_timestamp, selected_sidecar, base_offset = (
        routes._state_db_since_timestamp_for_limited_display(
            child,
            msg_limit=30,
        )
    )
    assert since_timestamp is None
    assert selected_sidecar == child_messages
    assert base_offset == len(parent_messages)
    assert captured["lineage_loads"] == 0

    real_reader = routes.get_state_db_session_messages
    injected = {"done": False}

    def wrapped_reader(sid, *args, **kwargs):
        if sid == child_sid and not injected["done"]:
            _append_state_db_rows(tmp_path / "state.db", child_sid, [state_only_prefix])
            injected["done"] = True
        return real_reader(sid, *args, **kwargs)

    monkeypatch.setattr(routes, "get_state_db_session_messages", wrapped_reader)

    first = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(first, urlparse(first.path))

    expected_count = len(parent_messages) + 1 + len(child_messages)
    assert first.status == 200
    assert injected["done"] is True
    assert captured["lineage_loads"] >= 1
    first_payload = first.response_json["session"]
    assert first_payload["message_count"] == expected_count
    assert first_payload["_messages_offset"] == len(parent_messages) + 1 + 470
    assert [msg["content"] for msg in first_payload["messages"]] == [
        f"child {idx}" for idx in range(470, 500)
    ]

    second = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0"
        f"&msg_limit=30&msg_before={first_payload['_messages_offset']}"
    )
    routes.handle_get(second, urlparse(second.path))

    assert second.status == 200
    second_payload = second.response_json["session"]
    assert second_payload["message_count"] == expected_count
    assert second_payload["_messages_offset"] == len(parent_messages) + 1 + 440
    first_contents = {msg["content"] for msg in first_payload["messages"]}
    second_contents = {msg["content"] for msg in second_payload["messages"]}
    assert not first_contents.intersection(second_contents)
    assert [msg["content"] for msg in second_payload["messages"]] == [
        f"child {idx}" for idx in range(440, 470)
    ]



def test_msg_limit_single_hop_fast_path_falls_back_for_equal_timestamp_state_db_prefix(
    monkeypatch,
    tmp_path,
):
    import api.models as models
    import api.routes as routes

    parent_sid = "parent_compression_snapshot_equal_state_prefix"
    child_sid = "continuation_child_equal_state_prefix"
    parent_messages = [
        {"role": "user", "content": f"parent {idx}", "timestamp": float(idx)}
        for idx in range(120)
    ]
    child_messages = [
        {"role": "assistant", "content": f"child {idx}", "timestamp": 300.0 + idx}
        for idx in range(500)
    ]
    state_equal_prefix = {"role": "user", "content": "state.db equal bridge", "timestamp": 300.0}
    parent = _install_test_session(monkeypatch, tmp_path, parent_sid, parent_messages)
    parent.pre_compression_snapshot = True
    parent.updated_at = 200.0
    parent.save(touch_updated_at=False)
    child = models.Session(
        session_id=child_sid,
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=child_messages,
        parent_session_id=parent_sid,
        compression_disjoint_parent_boundary=_disjoint_parent_boundary(parent, child_messages),
        created_at=300.0,
        updated_at=900.0,
    )
    child.save(touch_updated_at=False)
    _make_state_db(tmp_path / "state.db", child_sid, [state_equal_prefix] + child_messages)

    real_lineage_loader = routes._webui_sidecar_lineage_messages_for_display
    captured = {"lineage_loads": 0}

    def wrapped_lineage_loader(*args, **kwargs):
        captured["lineage_loads"] += 1
        return real_lineage_loader(*args, **kwargs)

    monkeypatch.setattr(routes, "_webui_sidecar_lineage_messages_for_display", wrapped_lineage_loader)

    first = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(first, urlparse(first.path))

    expected_count = len(parent_messages) + 1 + len(child_messages)
    assert first.status == 200
    assert captured["lineage_loads"] >= 1
    first_payload = first.response_json["session"]
    assert first_payload["message_count"] == expected_count
    assert first_payload["_messages_offset"] == len(parent_messages) + 1 + 470
    assert [msg["content"] for msg in first_payload["messages"]] == [
        f"child {idx}" for idx in range(470, 500)
    ]

    second = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0"
        f"&msg_limit=30&msg_before={first_payload['_messages_offset']}"
    )
    routes.handle_get(second, urlparse(second.path))

    assert second.status == 200
    second_payload = second.response_json["session"]
    assert second_payload["message_count"] == expected_count
    assert second_payload["_messages_offset"] == len(parent_messages) + 1 + 440
    assert [msg["content"] for msg in second_payload["messages"]] == [
        f"child {idx}" for idx in range(440, 470)
    ]


@pytest.mark.parametrize(
    "insert_during_state_read",
    [
        pytest.param(False, id="present-before-validation"),
        pytest.param(True, id="inserted-after-validation"),
    ],
)
def test_msg_limit_single_hop_fast_path_falls_back_for_null_timestamp_parent_replay(
    monkeypatch,
    tmp_path,
    insert_during_state_read,
):
    """A NULL-timestamp parent replay cannot be proven safe against a child-only merge."""
    import api.models as models
    import api.routes as routes

    parent_sid = "parent_compression_snapshot_null_state_replay"
    child_sid = "continuation_child_null_state_replay"
    parent_messages = [
        {"role": "user", "content": f"parent {idx}", "timestamp": float(idx)}
        for idx in range(120)
    ]
    child_messages = [
        {"role": "user", "content": f"child {idx}", "timestamp": 300.0 + idx}
        for idx in range(500)
    ]
    null_parent_replay = {
        "role": "user",
        "content": "parent 50",
        "timestamp": None,
    }
    parent = _install_test_session(monkeypatch, tmp_path, parent_sid, parent_messages)
    parent.pre_compression_snapshot = True
    parent.updated_at = 200.0
    parent.save(touch_updated_at=False)
    child = models.Session(
        session_id=child_sid,
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=child_messages,
        parent_session_id=parent_sid,
        compression_disjoint_parent_boundary=_disjoint_parent_boundary(parent, child_messages),
        created_at=300.0,
        updated_at=900.0,
    )
    child.save(touch_updated_at=False)
    _make_state_db(
        tmp_path / "state.db",
        child_sid,
        child_messages + ([] if insert_during_state_read else [null_parent_replay]),
    )

    real_lineage_loader = routes._webui_sidecar_lineage_messages_for_display
    real_state_reader = routes.get_state_db_session_messages
    captured = {"lineage_loads": 0}
    inserted = False

    def wrapped_lineage_loader(*args, **kwargs):
        captured["lineage_loads"] += 1
        return real_lineage_loader(*args, **kwargs)

    def wrapped_state_reader(*args, **kwargs):
        nonlocal inserted
        if insert_during_state_read and not inserted:
            inserted = True
            _append_state_db_rows(
                tmp_path / "state.db",
                child_sid,
                [null_parent_replay],
            )
        return real_state_reader(*args, **kwargs)

    monkeypatch.setattr(routes, "_webui_sidecar_lineage_messages_for_display", wrapped_lineage_loader)
    monkeypatch.setattr(routes, "get_state_db_session_messages", wrapped_state_reader)

    first = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(first, urlparse(first.path))

    assert first.status == 200
    first_payload = first.response_json["session"]
    assert first_payload["message_count"] == len(parent_messages) + len(child_messages)
    assert first_payload["_messages_offset"] == len(parent_messages) + 470
    assert [msg["content"] for msg in first_payload["messages"]] == [
        f"child {idx}" for idx in range(470, 500)
    ]
    assert captured["lineage_loads"] >= 1

    second = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0"
        f"&msg_limit=30&msg_before={first_payload['_messages_offset']}"
    )
    routes.handle_get(second, urlparse(second.path))

    assert second.status == 200
    second_payload = second.response_json["session"]
    assert second_payload["message_count"] == len(parent_messages) + len(child_messages)
    assert second_payload["_messages_offset"] == len(parent_messages) + 440
    assert [msg["content"] for msg in second_payload["messages"]] == [
        f"child {idx}" for idx in range(440, 470)
    ]


def test_msg_limit_mutable_parent_timestamp_boundary_falls_back_to_lineage(
    monkeypatch,
    tmp_path,
):
    import api.models as models
    import api.routes as routes

    parent_sid = "parent_compression_snapshot_touched"
    child_sid = "continuation_child_touched_parent"
    parent_messages = [
        {"role": "user", "content": f"parent {idx}", "timestamp": float(idx)}
        for idx in range(120)
    ]
    child_messages = [
        {"role": "user", "content": f"child {idx}", "timestamp": 300.0 + idx}
        for idx in range(500)
    ]
    parent = _install_test_session(monkeypatch, tmp_path, parent_sid, parent_messages)
    parent.pre_compression_snapshot = True
    # A metadata/edit touch after child creation must not make the continuation
    # child look cumulative just because first_ts < parent.updated_at.
    parent.updated_at = 1000.0
    parent.save(touch_updated_at=False)
    child = models.Session(
        session_id=child_sid,
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=child_messages,
        parent_session_id=parent_sid,
        compression_disjoint_parent_boundary={
            "parent_session_id": parent_sid,
            "message_count": len(parent_messages),
            "parent_updated_at": 200.0,
            "child_message_count": len(child_messages),
            "child_messages_digest": models._compression_child_messages_digest(child_messages),
        },
        created_at=300.0,
        updated_at=900.0,
    )
    child.save(touch_updated_at=False)
    _make_state_db(tmp_path / "state.db", child_sid, child_messages)

    real_lineage_loader = routes._webui_sidecar_lineage_messages_for_display
    captured = {"lineage_loads": 0}

    def wrapped_lineage_loader(*args, **kwargs):
        captured["lineage_loads"] += 1
        return real_lineage_loader(*args, **kwargs)

    monkeypatch.setattr(routes, "_webui_sidecar_lineage_messages_for_display", wrapped_lineage_loader)

    first = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(first, urlparse(first.path))

    assert first.status == 200
    assert captured["lineage_loads"] >= 1
    first_payload = first.response_json["session"]
    assert first_payload["message_count"] == len(parent_messages) + len(child_messages)
    assert first_payload["_messages_offset"] == len(parent_messages) + 470
    assert [msg["content"] for msg in first_payload["messages"]] == [
        f"child {idx}" for idx in range(470, 500)
    ]


def test_msg_limit_multi_hop_cumulative_parent_falls_back_to_full_lineage_pagination(
    monkeypatch,
    tmp_path,
):
    import api.models as models
    import api.routes as routes

    root_sid = "root_compression_snapshot_cursor"
    parent_sid = "cumulative_parent_snapshot_cursor"
    child_sid = "multi_hop_continuation_child_cursor"
    root_messages = [
        {"role": "user", "content": f"root {idx}", "timestamp": float(idx)}
        for idx in range(100)
    ]
    parent_extra = [
        {"role": "user", "content": f"parent {idx}", "timestamp": 100.0 + idx}
        for idx in range(100)
    ]
    parent_messages = root_messages + parent_extra
    child_messages = [
        {"role": "user", "content": f"child {idx}", "timestamp": 300.0 + idx}
        for idx in range(500)
    ]
    root = _install_test_session(monkeypatch, tmp_path, root_sid, root_messages)
    root.pre_compression_snapshot = True
    root.updated_at = 100.0
    root.save(touch_updated_at=False)
    parent = models.Session(
        session_id=parent_sid,
        title="Parent snapshot",
        workspace=str(tmp_path),
        model="test-model",
        messages=parent_messages,
        parent_session_id=root_sid,
        created_at=100.0,
        updated_at=250.0,
    )
    parent.pre_compression_snapshot = True
    parent.save(touch_updated_at=False)
    child = models.Session(
        session_id=child_sid,
        title="Reconcile",
        workspace=str(tmp_path),
        model="test-model",
        messages=child_messages,
        parent_session_id=parent_sid,
        compression_disjoint_parent_boundary=_disjoint_parent_boundary(parent, child_messages),
        created_at=300.0,
        updated_at=900.0,
    )
    child.save(touch_updated_at=False)
    _make_state_db(tmp_path / "state.db", child_sid, child_messages)

    real_lineage_loader = routes._webui_sidecar_lineage_messages_for_display
    captured = {"lineage_loads": 0}

    def wrapped_lineage_loader(*args, **kwargs):
        captured["lineage_loads"] += 1
        return real_lineage_loader(*args, **kwargs)

    monkeypatch.setattr(routes, "_webui_sidecar_lineage_messages_for_display", wrapped_lineage_loader)

    first = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(first, urlparse(first.path))

    expected_count = len(parent_messages) + len(child_messages)
    assert first.status == 200
    assert captured["lineage_loads"] >= 1
    first_payload = first.response_json["session"]
    assert first_payload["message_count"] == expected_count
    assert first_payload["_messages_offset"] == len(parent_messages) + 470
    assert [msg["content"] for msg in first_payload["messages"]] == [
        f"child {idx}" for idx in range(470, 500)
    ]

    second = _GetHandler(
        f"/api/session?session_id={child_sid}&messages=1&resolve_model=0"
        f"&msg_limit=30&msg_before={first_payload['_messages_offset']}"
    )
    routes.handle_get(second, urlparse(second.path))

    assert second.status == 200
    second_payload = second.response_json["session"]
    assert second_payload["message_count"] == expected_count
    assert second_payload["_messages_offset"] == len(parent_messages) + 440
    assert [msg["content"] for msg in second_payload["messages"]] == [
        f"child {idx}" for idx in range(440, 470)
    ]


def test_msg_limit_unavailable_parent_metadata_count_falls_back_to_lineage(monkeypatch):
    import api.routes as routes

    class MetadataOnlyParent:
        pre_compression_snapshot = True
        session_source = None
        parent_session_id = None
        _metadata_message_count = None
        _loaded_metadata_only = True
        updated_at = 200.0

        def compact(self):
            return {"message_count": 0}

    direct_sidecar_messages = [
        {"role": "user", "content": f"child {idx}", "timestamp": 300.0 + idx}
        for idx in range(500)
    ]
    lineage_messages = [
        {"role": "user", "content": f"parent {idx}", "timestamp": float(idx)}
        for idx in range(120)
    ] + direct_sidecar_messages
    session = type(
        "Session",
        (),
        {
            "session_id": "metadata_count_unavailable_child",
            "messages": direct_sidecar_messages,
            "parent_session_id": "metadata_count_unavailable_parent",
            "truncation_watermark": None,
            "truncation_boundary": None,
            "profile": None,
        },
    )()
    captured = {"lineage_loaded": False}

    monkeypatch.setattr(
        routes.Session,
        "load_metadata_only",
        staticmethod(lambda _sid: MetadataOnlyParent()),
    )

    def lineage_loader(_session):
        captured["lineage_loaded"] = True
        return lineage_messages

    monkeypatch.setattr(routes, "_webui_sidecar_lineage_messages_for_display", lineage_loader)
    monkeypatch.setattr(
        routes,
        "get_state_db_session_message_keys_before_timestamp",
        lambda *_args, **_kwargs: None,
    )

    since_timestamp, selected_sidecar, base_offset = routes._state_db_since_timestamp_for_limited_display(
        session,
        msg_limit=30,
    )

    assert since_timestamp is None
    assert base_offset == 0
    assert captured["lineage_loaded"] is True
    assert selected_sidecar == lineage_messages


def test_msg_limit_hidden_heavy_compression_child_stitches_lineage_when_visible_tail_short(
    monkeypatch,
):
    import api.routes as routes

    hidden_tool_rows = [
        {"role": "tool", "content": f"hidden tool {idx}", "timestamp": float(idx)}
        for idx in range(310)
    ]
    direct_visible_rows = [
        {"role": "assistant", "content": f"child visible {idx}", "timestamp": 400.0 + idx}
        for idx in range(10)
    ]
    parent_visible_rows = [
        {"role": "user", "content": f"parent visible {idx}", "timestamp": 100.0 + idx}
        for idx in range(30)
    ]
    direct_sidecar_messages = hidden_tool_rows + direct_visible_rows
    lineage_messages = parent_visible_rows + direct_sidecar_messages
    session = type(
        "Session",
        (),
        {
            "session_id": "hidden_heavy_child",
            "messages": direct_sidecar_messages,
            "parent_session_id": "parent_compression_snapshot",
            "truncation_watermark": None,
            "truncation_boundary": None,
            "profile": None,
        },
    )()
    captured = {"lineage_loaded": False}

    def lineage_loader(_session):
        captured["lineage_loaded"] = True
        return lineage_messages

    monkeypatch.setattr(routes, "_webui_sidecar_lineage_messages_for_display", lineage_loader)
    monkeypatch.setattr(
        routes,
        "get_state_db_session_message_keys_before_timestamp",
        lambda *_args, **_kwargs: None,
    )

    since_timestamp, selected_sidecar, base_offset = routes._state_db_since_timestamp_for_limited_display(
        session,
        msg_limit=30,
    )

    assert since_timestamp is None
    assert base_offset == 0
    assert captured["lineage_loaded"] is True
    assert selected_sidecar == lineage_messages


def test_msg_limit_session_load_falls_back_with_null_state_db_timestamp(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_limited_tail_null"
    sidecar_messages = [
        {"role": "user", "content": f"sidecar {idx}", "timestamp": float(idx)}
        for idx in range(500)
    ]
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(
        tmp_path / "state.db",
        sid,
        sidecar_messages
        + [
            {
                "role": "assistant",
                "content": "state null timestamp only",
                "timestamp": None,
            },
        ],
    )

    real_reader = routes.get_state_db_session_messages
    full_state_messages = real_reader(sid)
    full_all_messages = routes._limited_webui_messages_for_display(
        session,
        full_state_messages,
    )
    expected_window, expected_offset = routes._message_window_for_display(
        full_all_messages,
        msg_limit=30,
    )
    captured = {}

    def wrapped_reader(*args, **kwargs):
        captured["since_timestamp"] = kwargs.get("since_timestamp")
        messages = real_reader(*args, **kwargs)
        captured["row_count"] = len(messages)
        return messages

    monkeypatch.setattr(routes, "get_state_db_session_messages", wrapped_reader)

    handler = _GetHandler(
        f"/api/session?session_id={sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    assert captured["since_timestamp"] is None
    assert captured["row_count"] == 501
    session_payload = handler.response_json["session"]
    assert session_payload["messages"] == expected_window
    assert session_payload["message_count"] == len(full_all_messages)
    assert session_payload["_messages_offset"] == expected_offset


def test_limited_state_db_prefix_missing_db_skips_visible_key_normalization(monkeypatch, tmp_path):
    import api.models as models
    import api.routes as routes

    sid = "webui_reconcile_prefix_missing_db"
    sidecar_messages = _large_timestamped_sidecar_messages(10_000)
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    visible_key_calls = 0
    real_visible_key = routes._session_message_visible_key

    def counted_visible_key(message):
        nonlocal visible_key_calls
        visible_key_calls += 1
        return real_visible_key(message)

    monkeypatch.setattr(routes, "_session_message_visible_key", counted_visible_key)
    monkeypatch.setattr(models, "_session_message_visible_key", counted_visible_key)

    floor, returned_sidecar, base_offset = routes._state_db_since_timestamp_for_limited_display(
        session,
        30,
    )

    assert floor is None
    assert returned_sidecar == sidecar_messages
    assert base_offset == 0
    assert visible_key_calls == 0


def test_limited_state_db_prefix_count_mismatch_skips_visible_key_normalization(monkeypatch, tmp_path):
    import api.models as models
    import api.routes as routes

    sid = "webui_reconcile_prefix_count_mismatch"
    sidecar_messages = _large_timestamped_sidecar_messages()
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(tmp_path / "state.db", sid, sidecar_messages[:10])
    visible_key_calls = 0
    real_visible_key = routes._session_message_visible_key

    def counted_visible_key(message):
        nonlocal visible_key_calls
        visible_key_calls += 1
        return real_visible_key(message)

    monkeypatch.setattr(routes, "_session_message_visible_key", counted_visible_key)
    monkeypatch.setattr(models, "_session_message_visible_key", counted_visible_key)

    floor, returned_sidecar, base_offset = routes._state_db_since_timestamp_for_limited_display(
        session,
        30,
    )

    assert floor is None
    assert returned_sidecar == sidecar_messages
    assert base_offset == 0
    assert visible_key_calls == 0


def test_limited_state_db_prefix_exact_match_runs_key_comparison(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_prefix_exact"
    sidecar_messages = _large_timestamped_sidecar_messages()
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(tmp_path / "state.db", sid, sidecar_messages)
    summary_calls = []
    key_calls = []
    visible_key_calls = 0
    real_summary_reader = routes.get_state_db_session_message_prefix_summary
    real_key_reader = routes.get_state_db_session_message_keys_before_timestamp
    real_visible_key = routes._session_message_visible_key

    def prefix_summary(*args, **kwargs):
        summary_calls.append((args, kwargs))
        return real_summary_reader(*args, **kwargs)

    def counted_key_reader(*args, **kwargs):
        key_calls.append((args, kwargs))
        return real_key_reader(*args, **kwargs)

    def counted_visible_key(message):
        nonlocal visible_key_calls
        visible_key_calls += 1
        return real_visible_key(message)

    monkeypatch.setattr(
        routes,
        "get_state_db_session_message_prefix_summary",
        prefix_summary,
        raising=False,
    )
    monkeypatch.setattr(
        routes,
        "get_state_db_session_message_keys_before_timestamp",
        counted_key_reader,
    )
    monkeypatch.setattr(routes, "_session_message_visible_key", counted_visible_key)

    floor, returned_sidecar, base_offset = routes._state_db_since_timestamp_for_limited_display(
        session,
        30,
    )

    assert floor == 200.0
    assert returned_sidecar == sidecar_messages
    assert base_offset == 0
    assert len(summary_calls) == 1
    assert len(key_calls) == 1
    assert visible_key_calls == 200


def test_limited_state_db_prefix_equal_count_different_content_falls_back(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_prefix_content_mismatch"
    sidecar_messages = _large_timestamped_sidecar_messages()
    state_messages = [dict(message) for message in sidecar_messages]
    state_messages[100]["content"] = "edited state content"
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(tmp_path / "state.db", sid, state_messages)
    summary_calls = []
    key_calls = []
    real_summary_reader = routes.get_state_db_session_message_prefix_summary
    real_key_reader = routes.get_state_db_session_message_keys_before_timestamp

    def prefix_summary(*args, **kwargs):
        summary_calls.append((args, kwargs))
        return real_summary_reader(*args, **kwargs)

    def counted_key_reader(*args, **kwargs):
        key_calls.append((args, kwargs))
        return real_key_reader(*args, **kwargs)

    monkeypatch.setattr(
        routes,
        "get_state_db_session_message_prefix_summary",
        prefix_summary,
        raising=False,
    )
    monkeypatch.setattr(
        routes,
        "get_state_db_session_message_keys_before_timestamp",
        counted_key_reader,
    )

    floor, returned_sidecar, base_offset = routes._state_db_since_timestamp_for_limited_display(
        session,
        30,
    )

    assert floor is None
    assert returned_sidecar == sidecar_messages
    assert base_offset == 0
    assert len(summary_calls) == 1
    assert len(key_calls) == 1


def test_limited_state_db_prefix_equal_empty_assistant_different_tool_calls_falls_back(
    monkeypatch,
    tmp_path,
):
    import api.routes as routes

    sid = "webui_reconcile_prefix_tool_calls_mismatch"
    sidecar_messages = _large_timestamped_sidecar_messages()
    sidecar_messages[100] = {
        "role": "assistant",
        "content": "",
        "timestamp": 100.0,
        "tool_calls": [{"id": "sidecar-call", "function": {"name": "terminal"}}],
    }
    state_messages = [dict(message) for message in sidecar_messages]
    state_messages[100] = {
        "role": "assistant",
        "content": "",
        "timestamp": 100.0,
        "tool_calls": json.dumps([{"id": "state-call", "function": {"name": "terminal"}}]),
    }
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(tmp_path / "state.db", sid, state_messages)
    summary_calls = []
    key_calls = []
    real_summary_reader = routes.get_state_db_session_message_prefix_summary
    real_key_reader = routes.get_state_db_session_message_keys_before_timestamp

    def prefix_summary(*args, **kwargs):
        summary_calls.append((args, kwargs))
        return real_summary_reader(*args, **kwargs)

    def counted_key_reader(*args, **kwargs):
        key_calls.append((args, kwargs))
        return real_key_reader(*args, **kwargs)

    monkeypatch.setattr(
        routes,
        "get_state_db_session_message_prefix_summary",
        prefix_summary,
        raising=False,
    )
    monkeypatch.setattr(
        routes,
        "get_state_db_session_message_keys_before_timestamp",
        counted_key_reader,
    )

    floor, returned_sidecar, base_offset = routes._state_db_since_timestamp_for_limited_display(
        session,
        30,
    )

    assert floor is None
    assert returned_sidecar == sidecar_messages
    assert base_offset == 0
    assert len(summary_calls) == 1
    assert len(key_calls) == 1


def test_limited_state_db_prefix_missing_sidecar_timestamp_preserves_full_fallback(
    monkeypatch,
    tmp_path,
):
    import api.routes as routes

    sid = "webui_reconcile_prefix_missing_sidecar_timestamp"
    sidecar_messages = _large_timestamped_sidecar_messages()
    sidecar_messages[100]["timestamp"] = None
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)

    def unexpected_prefix_summary(*args, **kwargs):
        raise AssertionError("missing sidecar timestamps must fall back before state.db preflight")

    monkeypatch.setattr(
        routes,
        "get_state_db_session_message_prefix_summary",
        unexpected_prefix_summary,
        raising=False,
    )

    floor, returned_sidecar, base_offset = routes._state_db_since_timestamp_for_limited_display(
        session,
        30,
    )

    assert floor is None
    assert returned_sidecar == sidecar_messages
    assert base_offset == 0


def test_msg_limit_session_load_bails_when_older_state_db_row_changes_offsets(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_limited_tail_offset_bail"
    sidecar_messages = [
        {"role": "user", "content": f"sidecar {idx}", "timestamp": float(idx)}
        for idx in range(500)
    ]
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(
        tmp_path / "state.db",
        sid,
        sidecar_messages
        + [
            {
                "role": "assistant",
                "content": "state older than floor only",
                "timestamp": 199.5,
            },
        ],
    )

    real_reader = routes.get_state_db_session_messages
    full_state_messages = real_reader(sid)
    full_all_messages = routes._limited_webui_messages_for_display(
        session,
        full_state_messages,
    )
    expected_window, expected_offset = routes._message_window_for_display(
        full_all_messages,
        msg_limit=30,
    )
    captured = {}

    def wrapped_reader(*args, **kwargs):
        captured["since_timestamp"] = kwargs.get("since_timestamp")
        return real_reader(*args, **kwargs)

    monkeypatch.setattr(routes, "get_state_db_session_messages", wrapped_reader)

    handler = _GetHandler(
        f"/api/session?session_id={sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    assert captured["since_timestamp"] is None
    session_payload = handler.response_json["session"]
    assert session_payload["messages"] == expected_window
    assert session_payload["message_count"] == len(full_all_messages)
    assert session_payload["_messages_offset"] == expected_offset


def test_msg_limit_session_load_bails_when_older_state_db_user_changes_offsets(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_limited_tail_user_offset_bail"
    sidecar_messages = [
        {"role": "user", "content": f"sidecar {idx}", "timestamp": float(idx)}
        for idx in range(500)
    ]
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(
        tmp_path / "state.db",
        sid,
        sidecar_messages
        + [
            {
                "role": "user",
                "content": "state older user than floor only",
                "timestamp": 199.5,
            },
        ],
    )

    real_reader = routes.get_state_db_session_messages
    full_state_messages = real_reader(sid)
    full_all_messages = routes._limited_webui_messages_for_display(
        session,
        full_state_messages,
    )
    expected_window, expected_offset = routes._message_window_for_display(
        full_all_messages,
        msg_limit=30,
    )
    captured = {}

    def wrapped_reader(*args, **kwargs):
        captured["since_timestamp"] = kwargs.get("since_timestamp")
        return real_reader(*args, **kwargs)

    monkeypatch.setattr(routes, "get_state_db_session_messages", wrapped_reader)

    handler = _GetHandler(
        f"/api/session?session_id={sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    assert captured["since_timestamp"] is None
    session_payload = handler.response_json["session"]
    assert session_payload["messages"] == expected_window
    assert session_payload["message_count"] == len(full_all_messages)
    assert session_payload["_messages_offset"] == expected_offset


def test_msg_limit_session_load_bails_when_prefloor_key_counts_mask_offset_change(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_limited_tail_count_mask_bail"
    sidecar_messages = [
        {"role": "user", "content": f"sidecar {idx}", "timestamp": float(idx)}
        for idx in range(500)
    ]
    state_messages = [
        msg for msg in sidecar_messages if msg["content"] != "sidecar 100"
    ]
    state_messages.append(
        {
            "role": "user",
            "content": "state masked older user than floor only",
            "timestamp": 199.5,
        }
    )
    state_messages.sort(key=lambda msg: msg["timestamp"])
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(tmp_path / "state.db", sid, state_messages)

    real_reader = routes.get_state_db_session_messages
    full_state_messages = real_reader(sid)
    full_all_messages = routes._limited_webui_messages_for_display(
        session,
        full_state_messages,
    )
    expected_window, expected_offset = routes._message_window_for_display(
        full_all_messages,
        msg_limit=30,
    )
    captured = {}

    def wrapped_reader(*args, **kwargs):
        captured["since_timestamp"] = kwargs.get("since_timestamp")
        return real_reader(*args, **kwargs)

    monkeypatch.setattr(routes, "get_state_db_session_messages", wrapped_reader)

    handler = _GetHandler(
        f"/api/session?session_id={sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    assert captured["since_timestamp"] is None
    session_payload = handler.response_json["session"]
    assert session_payload["messages"] == expected_window
    assert session_payload["message_count"] == len(full_all_messages)
    assert session_payload["_messages_offset"] == expected_offset
    assert len(full_all_messages) == len(sidecar_messages) + 1


def test_msg_limit_session_load_bails_when_prefloor_tool_calls_mask_offset_change(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_limited_tail_tool_calls_bail"
    sidecar_tool_calls = [{"id": "call_sidecar", "function": {"name": "terminal", "arguments": "{}"}}]
    state_tool_calls = [{"id": "call_state", "function": {"name": "terminal", "arguments": "{}"}}]
    sidecar_messages = [
        {"role": "user", "content": f"sidecar {idx}", "timestamp": float(idx)}
        for idx in range(500)
    ]
    sidecar_messages[100] = {
        "role": "assistant",
        "content": "",
        "timestamp": 100.0,
        "tool_calls": sidecar_tool_calls,
    }
    state_messages = [
        dict(msg, tool_calls=json.dumps(msg["tool_calls"]))
        if msg.get("tool_calls")
        else dict(msg)
        for msg in sidecar_messages
    ]
    state_messages[100] = {
        "role": "assistant",
        "content": "",
        "timestamp": 100.0,
        "tool_calls": json.dumps(state_tool_calls),
    }
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(tmp_path / "state.db", sid, state_messages)

    real_reader = routes.get_state_db_session_messages
    full_state_messages = real_reader(sid)
    full_all_messages = routes._limited_webui_messages_for_display(
        session,
        full_state_messages,
    )
    expected_window, expected_offset = routes._message_window_for_display(
        full_all_messages,
        msg_limit=30,
    )
    captured = {}

    def wrapped_reader(*args, **kwargs):
        captured["since_timestamp"] = kwargs.get("since_timestamp")
        return real_reader(*args, **kwargs)

    monkeypatch.setattr(routes, "get_state_db_session_messages", wrapped_reader)

    handler = _GetHandler(
        f"/api/session?session_id={sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    assert captured["since_timestamp"] is None
    session_payload = handler.response_json["session"]
    assert session_payload["messages"] == expected_window
    assert session_payload["message_count"] == len(full_all_messages)
    assert session_payload["_messages_offset"] == expected_offset
    assert len(full_all_messages) == len(sidecar_messages) + 1


def test_msg_limit_session_load_bails_when_truncation_boundary_is_set(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_limited_tail_boundary"
    sidecar_messages = [
        {"role": "user", "content": f"sidecar {idx}", "timestamp": float(idx)}
        for idx in range(500)
    ]
    session = _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    session.truncation_boundary = 250.0
    session.save(touch_updated_at=False)
    _make_state_db(tmp_path / "state.db", sid, sidecar_messages)

    real_reader = routes.get_state_db_session_messages
    captured = {}

    def wrapped_reader(*args, **kwargs):
        captured["since_timestamp"] = kwargs.get("since_timestamp")
        return real_reader(*args, **kwargs)

    monkeypatch.setattr(routes, "get_state_db_session_messages", wrapped_reader)

    handler = _GetHandler(
        f"/api/session?session_id={sid}&messages=1&resolve_model=0&msg_limit=30"
    )
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    assert captured["since_timestamp"] is None


def test_msg_before_session_load_keeps_full_state_db_reader(monkeypatch, tmp_path):
    import api.routes as routes

    sid = "webui_reconcile_msg_before"
    sidecar_messages = [
        {"role": "user", "content": f"sidecar {idx}", "timestamp": float(idx)}
        for idx in range(500)
    ]
    _install_test_session(monkeypatch, tmp_path, sid, sidecar_messages)
    _make_state_db(tmp_path / "state.db", sid, sidecar_messages)

    real_reader = routes.get_state_db_session_messages
    captured = {}

    def wrapped_reader(*args, **kwargs):
        captured["since_timestamp"] = kwargs.get("since_timestamp")
        return real_reader(*args, **kwargs)

    monkeypatch.setattr(routes, "get_state_db_session_messages", wrapped_reader)

    handler = _GetHandler(
        f"/api/session?session_id={sid}&messages=1&resolve_model=0&msg_before=400&msg_limit=30"
    )
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    assert captured["since_timestamp"] is None
    messages = handler.response_json["session"]["messages"]
    assert messages[0]["content"] == "sidecar 370"
    assert messages[-1]["content"] == "sidecar 399"


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
