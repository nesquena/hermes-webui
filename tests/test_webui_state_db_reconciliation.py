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


def _state_db_source_metadata(source):
    from api.agent_sessions import normalize_agent_session_source

    source_meta = normalize_agent_session_source(source)
    return {
        "_state_db_source": source,
        "_state_db_source_tag": source,
        "_state_db_raw_source": source_meta["raw_source"],
        "_state_db_session_source": source_meta["session_source"],
        "_state_db_source_label": source_meta["source_label"],
    }


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


def test_sidebar_state_db_overlay_reclassifies_authoritative_subagent_rows():
    import api.models as models

    def sidebar_row(sid, source=None, *, session_source=None, source_label=None, is_cli_session=True):
        row = {"session_id": sid, "is_cli_session": is_cli_session, "read_only": False}
        if source:
            row.update(
                source_tag=source,
                raw_source=source,
                session_source=session_source,
                source_label=source_label,
            )
        return row

    sessions = [
        sidebar_row(
            "row-01", "webui", session_source="webui", source_label="WebUI", is_cli_session=False
        ),
        sidebar_row("row-02", "fork", session_source="other", source_label="Fork"),
        sidebar_row("row-03"),
        sidebar_row("row-04", "tui", session_source="cli", source_label="TUI"),
    ]
    subagent_metadata = {
        sid: _state_db_source_metadata("subagent")
        for sid in ("row-01", "row-02", "row-03")
    }
    subagent_metadata["row-04"] = _state_db_source_metadata("tui")

    models._apply_sidebar_state_db_override_metadata(sessions, subagent_metadata)

    observed = [
        (
            session.get("source_tag"), session.get("raw_source"),
            session.get("session_source"), session.get("source_label"),
            session["is_cli_session"], session["read_only"],
        )
        for session in sessions
    ]
    assert observed[:3] == [("subagent", "subagent", "other", "Subagent", False, True)] * 3
    assert observed[3] == ("tui", "tui", "cli", "TUI", True, False)


def test_api_sessions_bulk_uses_batched_subagent_metadata_without_row_probes(
    monkeypatch, tmp_path
):
    import api.models as models
    import api.routes as routes

    db_path = tmp_path / "state.db"
    stale_rows = [
        {
            "session_id": f"row-{index:02d}",
            "source_tag": "webui",
            "raw_source": "webui",
            "session_source": "webui",
            "source_label": "WebUI",
        }
        for index in range(32)
    ]
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)")
        conn.executemany(
            "INSERT INTO sessions (id, source) VALUES (?, 'subagent')",
            ((row["session_id"],) for row in stale_rows),
        )
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

    def fake_all_sessions(**_kwargs):
        rows = [
            dict(row, is_cli_session=True, read_only=False, message_count=1)
            for row in stale_rows
        ]
        models._apply_sidebar_state_db_overrides(rows)
        return rows

    monkeypatch.setattr(routes, "all_sessions", fake_all_sessions)
    monkeypatch.setattr(routes, "_enrich_sidebar_lineage_metadata", lambda _rows: None)
    monkeypatch.setattr(routes, "_reconcile_stale_stream_state_for_session_rows", lambda _rows: False)
    monkeypatch.setattr(routes, "_prune_orphaned_webui_zero_message_sessions", lambda rows, **_kwargs: list(rows))
    single_row_probes = []

    def record_single_row_probe(sid):
        single_row_probes.append(sid)
        return True

    monkeypatch.setattr(routes, "_is_subagent_child_session_id", record_single_row_probe)

    payload = routes._build_session_list_cache_payload(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
    )

    rows = {row["session_id"]: row for row in payload["sessions"]}
    assert single_row_probes == []
    assert set(rows) == {row["session_id"] for row in stale_rows}
    for row in rows.values():
        assert (
            row["source_tag"],
            row["raw_source"],
            row["session_source"],
            row["source_label"],
        ) == ("subagent", "subagent", "other", "Subagent")
        assert row["read_only"] is True
        assert row["is_cli_session"] is False


def test_sidebar_override_reader_uses_one_connection_and_500_id_chunks(
    monkeypatch, tmp_path
):
    import api.models as models

    db_path = tmp_path / "state.db"
    session_ids = [f"row-{index:04d}" for index in range(1001)]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)"
        )
        conn.executemany(
            "INSERT INTO sessions (id, source) VALUES (?, 'subagent')",
            ((sid,) for sid in session_ids),
        )

    connections = []
    session_queries = []
    real_open = models.open_state_db_readonly

    def tracked_open(path):
        connection = real_open(path)
        connections.append(connection)

        def trace(statement):
            if "FROM sessions s" in statement and "s.id IN" in statement:
                session_queries.append(statement)

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(models, "open_state_db_readonly", tracked_open)
    overrides = models._read_state_db_sidebar_overrides(db_path, set(session_ids))

    assert len(connections) == 1
    assert len(session_queries) == 3
    assert set(overrides) == set(session_ids)
    assert overrides[session_ids[0]] == _state_db_source_metadata("subagent")


def test_sidebar_override_reader_recovers_sources_after_rich_reader_error(
    monkeypatch, tmp_path
):
    import api.models as models

    db_path = tmp_path / "state.db"
    session_ids = [f"row-{index:04d}" for index in range(1001)]
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT)")
        conn.executemany(
            "INSERT INTO sessions (id, source) VALUES (?, 'subagent')",
            ((sid,) for sid in session_ids),
        )

    class FailingCursor:
        def __init__(self, cursor):
            self._cursor = cursor

        def execute(self, statement, parameters=()):
            if "sqlite_master" in statement:
                raise sqlite3.OperationalError("controlled optional schema failure")
            self._cursor.execute(statement, parameters)
            return self

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    first_closed = []

    class FailingConnection:
        def __init__(self, connection):
            self._connection = connection

        @property
        def row_factory(self):
            return self._connection.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self._connection.row_factory = value

        def cursor(self):
            return FailingCursor(self._connection.cursor())

        def close(self):
            first_closed.append(True)
            self._connection.close()

    connections = []
    source_only_queries = []
    second_connection = None
    real_open = models.open_state_db_readonly

    def tracked_open(path):
        nonlocal second_connection
        connection = real_open(path)
        connections.append(connection)
        if len(connections) == 1:
            return FailingConnection(connection)
        second_connection = connection

        def trace(statement):
            normalized = " ".join(statement.split()).lower()
            if normalized.startswith("select id, source from sessions where id in ("):
                source_only_queries.append(statement)

        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(models, "open_state_db_readonly", tracked_open)
    overrides = models._read_state_db_sidebar_overrides(db_path, set(session_ids))

    assert len(connections) == 2
    assert first_closed == [True]
    assert second_connection is not None
    with pytest.raises(sqlite3.ProgrammingError):
        second_connection.execute("SELECT 1")
    assert len(source_only_queries) == 3
    assert [
        statement.split("IN (", 1)[1].split(")", 1)[0].count(",") + 1
        for statement in source_only_queries
    ] == [500, 500, 1]
    assert set(overrides) == set(session_ids)
    assert overrides[session_ids[0]] == _state_db_source_metadata("subagent")
    assert overrides[session_ids[-1]] == _state_db_source_metadata("subagent")


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
                **_state_db_source_metadata("subagent"),
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
    # Subagent classification is authoritative and view-only — the child does
    # not get re-tagged as a WebUI or writable CLI session.
    assert sessions[0]["source_tag"] == "subagent"
    assert sessions[0]["is_cli_session"] is False
    assert sessions[0]["read_only"] is True


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

    floor, returned_sidecar = routes._state_db_since_timestamp_for_limited_display(
        session,
        30,
    )

    assert floor is None
    assert returned_sidecar == sidecar_messages
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

    floor, returned_sidecar = routes._state_db_since_timestamp_for_limited_display(
        session,
        30,
    )

    assert floor is None
    assert returned_sidecar == sidecar_messages
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

    floor, returned_sidecar = routes._state_db_since_timestamp_for_limited_display(
        session,
        30,
    )

    assert floor == 200.0
    assert returned_sidecar == sidecar_messages
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

    floor, returned_sidecar = routes._state_db_since_timestamp_for_limited_display(
        session,
        30,
    )

    assert floor is None
    assert returned_sidecar == sidecar_messages
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

    floor, returned_sidecar = routes._state_db_since_timestamp_for_limited_display(
        session,
        30,
    )

    assert floor is None
    assert returned_sidecar == sidecar_messages
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

    floor, returned_sidecar = routes._state_db_since_timestamp_for_limited_display(
        session,
        30,
    )

    assert floor is None
    assert returned_sidecar == sidecar_messages


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


@pytest.mark.parametrize('limit', ['', '&msg_limit=10'])
def test_session_get_keeps_lcm_state_rows_context_only(monkeypatch, tmp_path, limit):
    import api.models as models
    import api.routes as routes

    sid = 'lcm_display_projection'
    prompt = {'role': 'user', 'content': 'Current prompt', 'timestamp': 1000.0}
    marker = {'role': 'user', 'content': '[Recent Summary (d0, node 418)]', 'timestamp': 1001.0}
    answer = {'role': 'assistant', 'content': 'Current answer', 'timestamp': 1002.0}
    session = _install_test_session(monkeypatch, tmp_path, sid, [prompt])
    _make_state_db(tmp_path / 'state.db', sid, [prompt, marker, answer])
    handler = _GetHandler(f'/api/session?session_id={sid}&messages=1&resolve_model=0{limit}')

    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 200
    assert handler.response_json["session"]["message_count"] == 2
    assert [m['content'] for m in handler.response_json['session']['messages']] == [
        prompt['content'], answer['content'],
    ]
    assert marker['content'] in [m['content'] for m in models.reconciled_state_db_messages_for_session(
        session, prefer_context=True,
    )]


@pytest.mark.parametrize('limit', ['', '&msg_limit=10'])
def test_legacy_lcm_sidecar_empty_state_get_is_display_only(monkeypatch, tmp_path, limit):
    import api.routes as routes
    import api.models as models

    marker = {'role': 'user', 'content': '[Recent Summary (d0, node 418)]', 'timestamp': 1000}
    owned = dict(marker, timestamp=1001, _active_turn_token='stream_1:1001')
    answer = {'role': 'assistant', 'content': 'Answer', 'timestamp': 1002}
    session = _install_test_session(monkeypatch, tmp_path, 'legacy_lcm', [marker, owned, answer])
    session.context_messages = [marker, owned, answer]
    session.save()
    handler = _GetHandler(f'/api/session?session_id={session.session_id}&messages=1&resolve_model=0{limit}')
    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 200
    rows = handler.response_json['session']['messages']
    assert [m['content'] for m in rows] == [owned['content'], answer['content']]
    assert models.Session.load(session.session_id).context_messages == [marker, owned, answer]


def test_lcm_parent_lineage_fork_persists_clean_display(monkeypatch, tmp_path):
    import api.models as models
    import api.routes as routes

    marker = {'role': 'user', 'content': '[Recent Summary (d0, node 418)]', 'timestamp': 1000}
    owned = dict(marker, timestamp=1001, _active_turn_token='stream_1:1001')
    answer = {'role': 'assistant', 'content': 'Answer', 'timestamp': 1002}
    parent = _install_test_session(monkeypatch, tmp_path, 'lcm_parent', [marker, owned])
    child = _install_test_session(monkeypatch, tmp_path, 'lcm_child', [answer])
    child.parent_session_id = parent.session_id
    child.context_messages = [marker, owned, answer]
    child.save()
    monkeypatch.setattr(routes, 'get_session', lambda sid, **k: parent if sid == parent.session_id else child)
    monkeypatch.setattr(routes, '_check_csrf', lambda handler: True)
    monkeypatch.setattr(routes, 'read_body', lambda handler: {'session_id': child.session_id})
    handler = _GetHandler('/api/session/branch')
    routes.handle_post(handler, urlparse(handler.path))
    assert handler.status == 200
    forked = models.Session.load(handler.response_json['session_id'])
    assert forked.messages == [owned, answer]
    assert child.context_messages == [marker, owned, answer]


def test_lcm_fork_user_boundary_retains_owner(monkeypatch, tmp_path):
    import api.models as models
    import api.routes as routes

    marker = {'role': 'user', 'content': '[Recent Summary (d0, node 418)]', 'timestamp': 1000}
    owned = dict(marker, _active_turn_token='stream_1:1000')
    answer = {'role': 'assistant', 'content': 'Answer', 'timestamp': 1001}
    source = _install_test_session(monkeypatch, tmp_path, 'lcm_boundary', [owned, answer])
    source.context_messages = [marker, owned, answer]
    source.save()
    monkeypatch.setattr(routes, 'get_session', lambda *a, **k: source)
    monkeypatch.setattr(routes, '_check_csrf', lambda handler: True)
    monkeypatch.setattr(routes, 'read_body', lambda handler: {
        'session_id': source.session_id, 'keep_count': 1,
    })
    handler = _GetHandler('/api/session/branch')
    routes.handle_post(handler, urlparse(handler.path))
    assert handler.status == 200
    forked = models.Session.load(handler.response_json['session_id'])
    assert forked.messages == [owned]
    assert forked.context_messages == [marker, owned]
    assert source.context_messages == [marker, owned, answer]


@pytest.mark.parametrize('role', ['user', 'assistant'])
@pytest.mark.parametrize('action', ['get', 'branch'])
@pytest.mark.parametrize('with_sidecar', [False, True])
def test_lcm_messaging_display_and_branch(monkeypatch, tmp_path, role, action, with_sidecar):
    import api.models as models
    import api.routes as routes

    marker = {'role': role, 'content': '[Recent Summary (d0, node 418)]', 'timestamp': 1000}
    owned = dict(marker, role='user', timestamp=1001, _active_turn_token='stream_1:1001')
    answer = {'role': 'assistant', 'content': 'Answer', 'timestamp': 1002}
    source = _install_test_session(monkeypatch, tmp_path, 'lcm_messaging', [owned] if with_sidecar else [])
    source.session_source = 'messaging'
    source.context_messages = [marker, owned, answer]
    source.save()
    monkeypatch.setattr(routes, 'get_session', lambda *a, **k: source)
    monkeypatch.setattr(routes, 'get_cli_session_messages', lambda *a, **k: [marker, owned, answer])
    monkeypatch.setattr(routes, '_lookup_cli_session_metadata', lambda *a, **k: {})
    if action == 'get':
        handler = _GetHandler(f'/api/session?session_id={source.session_id}&messages=1&resolve_model=0')
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200
        assert [m['content'] for m in handler.response_json['session']['messages']] == [owned['content'], 'Answer']
    else:
        monkeypatch.setattr(routes, '_check_csrf', lambda handler: True)
        monkeypatch.setattr(routes, 'read_body', lambda handler: {'session_id': source.session_id})
        handler = _GetHandler('/api/session/branch')
        routes.handle_post(handler, urlparse(handler.path))
        assert handler.status == 200
        forked = models.Session.load(handler.response_json['session_id'])
        assert forked.messages == [owned, answer]
        assert forked.context_messages == [marker, owned, answer]
    assert models.Session.load(source.session_id).context_messages == [marker, owned, answer]


@pytest.mark.parametrize('action', ['get', 'branch'])
def test_messaging_merge_keeps_current_owner_before_same_timestamp_answer(monkeypatch, tmp_path, action):
    import api.models as models
    import api.routes as routes

    prior = {'role': 'user', 'content': 'Prior request', 'timestamp': 1}
    prior_answer = {'role': 'assistant', 'content': 'Prior answer', 'timestamp': 1.5}
    envelope = {'role': 'user', 'content': '[Recent Summary (d0, node 418)]', 'timestamp': 1}
    owner = dict(envelope, timestamp=2, _active_turn_token='current:2')
    answer = {'role': 'assistant', 'content': 'Current answer', 'timestamp': 2}
    expected = [prior, prior_answer, owner, answer]
    source = _install_test_session(monkeypatch, tmp_path, 'messaging_order', [prior, envelope, owner, answer])
    source.session_source = 'messaging'
    source.context_messages = [prior, prior_answer, envelope, owner, answer]
    source.save()
    monkeypatch.setattr(routes, 'get_session', lambda *a, **k: source)
    monkeypatch.setattr(routes, 'get_cli_session_messages', lambda *a, **k: expected)
    monkeypatch.setattr(routes, '_lookup_cli_session_metadata', lambda *a, **k: {})
    if action == 'get':
        handler = _GetHandler(f'/api/session?session_id={source.session_id}&messages=1&resolve_model=0')
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200
        assert [(row['role'], row['content']) for row in handler.response_json['session']['messages']] == [
            (row['role'], row['content']) for row in expected
        ]
    else:
        monkeypatch.setattr(routes, '_check_csrf', lambda handler: True)
        monkeypatch.setattr(routes, 'read_body', lambda handler: {'session_id': source.session_id})
        handler = _GetHandler('/api/session/branch')
        routes.handle_post(handler, urlparse(handler.path))
        assert handler.status == 200
        fork = models.Session.load(handler.response_json['session_id'])
        assert fork.messages == expected
        assert fork.context_messages == source.context_messages


@pytest.mark.parametrize('split', [0, 1])
@pytest.mark.parametrize('multipart', [False, True])
@pytest.mark.parametrize('provenance', [{}, {'id': 'same'}, {'_row_id': 7, 'api_content': 'provider'}])
@pytest.mark.parametrize('tokens,heading,distinct', [
    ((None, 'owner:100'), '[Recent Summary (d0, node 418)]', True),
    (('old:100', 'new:100'), 'Repeated request', True),
    ((None, None), 'Repeated request', False),
    (('owner:100', 'owner:100'), '[Recent Summary (d0, node 418)]', False),
])
def test_append_only_merge_respects_turn_identity(split, multipart, provenance, tokens, heading, distinct):
    import copy
    import api.models as models

    content = [{'type': 'input_text', 'input_text': heading}] if multipart else heading
    rows = [dict(provenance, role='user', content=content, timestamp=100) for _ in tokens]
    for row, token in zip(rows, tokens, strict=True):
        if token:
            row['_active_turn_token'] = token
    expected = rows if distinct else rows[:1]
    assert models.merge_session_messages_append_only(
        copy.deepcopy(rows[:split]), copy.deepcopy(rows[split:]),
    ) == expected
    session = models.Session(messages=copy.deepcopy(rows[:split]))
    context = models.reconciled_state_db_messages_for_session(
        session, prefer_context=True, state_messages=copy.deepcopy(rows[split:]),
    )
    assert context == expected
    display = models.reconciled_state_db_messages_for_session(
        session, state_messages=copy.deepcopy(rows[split:]),
    )
    assert display == [row for row in expected if not models.is_lcm_context_recovery_marker(row)]


@pytest.mark.parametrize('history_size', [0, 1001])
def test_large_exact_tokenless_mirror_merge(history_size):
    import api.models as models

    owner = {'role': 'user', 'content': 'x' * 200001, 'timestamp': 10000, '_active_turn_token': 'owner:10000'}
    replay = {key: value for key, value in owner.items() if key != '_active_turn_token'}
    history = [{'role': 'user', 'content': f'History {i}', 'timestamp': i} for i in range(history_size)]
    assert models.merge_session_messages_append_only([*history, owner], [replay]) == [*history, owner]


@pytest.mark.parametrize('action', ['get', 'branch'])
def test_large_ordinary_messaging_replay_is_one_turn(monkeypatch, tmp_path, action):
    import api.models as models
    import api.routes as routes

    owner = {'role': 'user', 'content': 'x' * 200001, 'timestamp': 100, '_active_turn_token': 'owner:100'}
    replay = {key: value for key, value in owner.items() if key != '_active_turn_token'}
    source = _install_test_session(monkeypatch, tmp_path, 'large_mirror', [owner])
    source.session_source = 'messaging'
    source.context_messages = [owner]
    source.save()
    monkeypatch.setattr(routes, 'get_session', lambda *a, **k: source)
    monkeypatch.setattr(routes, 'get_cli_session_messages', lambda *a, **k: [replay])
    monkeypatch.setattr(routes, '_lookup_cli_session_metadata', lambda *a, **k: {})
    if action == 'get':
        handler = _GetHandler(f'/api/session?session_id={source.session_id}&messages=1&resolve_model=0')
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200
        rows = handler.response_json['session']['messages']
        assert len(rows) == 1
        assert rows[0]['content'] == owner['content']
    else:
        monkeypatch.setattr(routes, '_check_csrf', lambda handler: True)
        monkeypatch.setattr(routes, 'read_body', lambda handler: {'session_id': source.session_id})
        handler = _GetHandler('/api/session/branch')
        routes.handle_post(handler, urlparse(handler.path))
        assert handler.status == 200
        fork = models.Session.load(handler.response_json['session_id'])
        assert fork.messages == [owner]
        assert fork.context_messages == [owner]


@pytest.mark.parametrize('action', ['lineage', 'get', 'branch'])
def test_lineage_same_timestamp_owner_precedes_answer(monkeypatch, tmp_path, action):
    import api.models as models
    import api.routes as routes

    owner = {'role': 'user', 'content': 'Request', 'timestamp': 100, '_active_turn_token': 'owner:100'}
    answer = {'role': 'assistant', 'content': 'Answer', 'timestamp': 100}
    parent = _install_test_session(monkeypatch, tmp_path, 'tie_parent', [owner])
    child = _install_test_session(monkeypatch, tmp_path, 'tie_child', [answer])
    child.parent_session_id = parent.session_id
    child.context_messages = [owner, answer]
    child.save()
    monkeypatch.setattr(routes, 'get_session', lambda sid, **k: parent if sid == parent.session_id else child)
    if action == 'lineage':
        assert routes._merged_webui_lineage_messages_for_display(child) == [owner, answer]
    elif action == 'get':
        handler = _GetHandler(f'/api/session?session_id={child.session_id}&messages=1&resolve_model=0')
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200
        assert [row['content'] for row in handler.response_json['session']['messages']] == ['Request', 'Answer']
    else:
        monkeypatch.setattr(routes, '_check_csrf', lambda handler: True)
        monkeypatch.setattr(routes, 'read_body', lambda handler: {'session_id': child.session_id})
        handler = _GetHandler('/api/session/branch')
        routes.handle_post(handler, urlparse(handler.path))
        assert handler.status == 200
        fork = models.Session.load(handler.response_json['session_id'])
        assert fork.messages == [owner, answer]
        assert fork.context_messages == [owner, answer]


@pytest.mark.parametrize('action', ['get', 'branch'])
@pytest.mark.parametrize('marker', [False, True])
@pytest.mark.parametrize('sidecar_longer', [False, True])
def test_messaging_tokenless_mirror_preserves_owner_order(monkeypatch, tmp_path, action, marker, sidecar_longer):
    import api.models as models
    import api.routes as routes

    text = '[Recent Summary (d0, node 418)]' if marker else 'Continue'
    earlier = dict(role='user', content='Earlier request', timestamp=1)
    owner = dict(role='user', content=text, timestamp=2, _active_turn_token='owner:2')
    replay = {key: value for key, value in owner.items() if key != '_active_turn_token'}
    answer = dict(role='assistant', content='Current answer', timestamp=2)
    expected = [earlier, owner, answer]
    source = _install_test_session(monkeypatch, tmp_path, 'mirror_order', expected if sidecar_longer else [owner])
    source.session_source = 'messaging'
    source.context_messages = [earlier, replay, owner, answer] if marker else expected
    source.save()
    cli = [replay, answer] if sidecar_longer else [earlier, replay, answer]
    monkeypatch.setattr(routes, 'get_session', lambda *a, **k: source)
    monkeypatch.setattr(routes, 'get_cli_session_messages', lambda *a, **k: cli)
    monkeypatch.setattr(routes, '_lookup_cli_session_metadata', lambda *a, **k: {})
    assert routes._merged_session_messages_for_display(source, cli) == expected
    if action == 'get':
        handler = _GetHandler(f'/api/session?session_id={source.session_id}&messages=1&resolve_model=0')
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200
        assert [row['content'] for row in handler.response_json['session']['messages']] == [row['content'] for row in expected]
    else:
        monkeypatch.setattr(routes, '_check_csrf', lambda handler: True)
        monkeypatch.setattr(routes, 'read_body', lambda handler: {'session_id': source.session_id})
        handler = _GetHandler('/api/session/branch')
        routes.handle_post(handler, urlparse(handler.path))
        assert handler.status == 200
        fork = models.Session.load(handler.response_json['session_id'])
        assert fork.messages == expected
        assert fork.context_messages == source.context_messages


@pytest.mark.parametrize('length', [8, 200001])
def test_append_merge_promotes_mirror_without_collapsing_conflicting_owner(length):
    import api.models as models

    replay = dict(role='user', content='x' * length, timestamp=100)
    old = dict(replay, _active_turn_token='old:100')
    new = dict(replay, _active_turn_token='new:100')
    assert models.merge_session_messages_append_only([dict(replay)], [old, new]) == [old, new]


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('shape', ['short', 'large', 'multipart', 'envelope', 'conflict', 'same_token'])
def test_empty_primary_merge_preserves_owner_identity(reverse, shape):
    import copy
    import api.models as models

    text = '[Recent Summary (d0, node 418)]' if shape == 'envelope' else 'x' * (200001 if shape == 'large' else 8)
    content = [{'type': 'text', 'text': text}] if shape == 'multipart' else text
    first = dict(role='user', content=content, timestamp=100)
    owner = dict(first, _active_turn_token='current:100')
    if shape in ('conflict', 'same_token'):
        first['_active_turn_token'] = 'old:100' if shape == 'conflict' else 'current:100'
    pair = [owner, first] if reverse else [first, owner]
    answer = dict(role='assistant', content='Historical answer', timestamp=100)
    rows = [pair[0], answer, pair[1]]
    expected = rows if shape in ('envelope', 'conflict') else [owner, answer]
    assert models.merge_session_messages_append_only([], copy.deepcopy(rows)) == expected


@pytest.mark.parametrize('action', ['get', 'branch'])
@pytest.mark.parametrize('sidecar_owned', [False, True])
def test_messaging_union_keeps_one_owned_mirror(monkeypatch, tmp_path, action, sidecar_owned):
    import api.models as models
    import api.routes as routes

    earlier = dict(role='user', content='Earlier', timestamp=1)
    replay = dict(role='user', content='Continue', timestamp=2)
    owner = dict(replay, _active_turn_token='current:2')
    answer = dict(role='assistant', content='Answer', timestamp=2)
    expected = [earlier, owner, answer]
    source = _install_test_session(monkeypatch, tmp_path, 'union_mirror', [owner if sidecar_owned else replay])
    source.session_source = 'messaging'
    source.context_messages = list(expected)
    source.save()
    cli = [earlier, replay if sidecar_owned else owner, answer]
    monkeypatch.setattr(routes, 'get_session', lambda *a, **k: source)
    monkeypatch.setattr(routes, 'get_cli_session_messages', lambda *a, **k: cli)
    monkeypatch.setattr(routes, '_lookup_cli_session_metadata', lambda *a, **k: {})
    assert routes._merged_session_messages_for_display(source, cli) == expected
    if action == 'get':
        handler = _GetHandler(f'/api/session?session_id={source.session_id}&messages=1&resolve_model=0')
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200
        assert [m['content'] for m in handler.response_json['session']['messages']] == ['Earlier', 'Continue', 'Answer']
    else:
        monkeypatch.setattr(routes, '_check_csrf', lambda handler: True)
        monkeypatch.setattr(routes, 'read_body', lambda handler: {'session_id': source.session_id})
        handler = _GetHandler('/api/session/branch')
        routes.handle_post(handler, urlparse(handler.path))
        assert handler.status == 200
        fork = models.Session.load(handler.response_json['session_id'])
        assert fork.messages == expected
        assert fork.context_messages == expected


@pytest.mark.parametrize('sidecar_owned', [False, True])
def test_sidecar_primary_preserves_distinct_ids_and_mirror_order(sidecar_owned):
    from types import SimpleNamespace
    import api.routes as routes

    earlier = dict(role='user', content='Earlier', timestamp=1)
    replay = dict(role='user', content='Continue', timestamp=2)
    owner = dict(replay, _active_turn_token='current:2')
    answer = dict(role='assistant', content='Answer', timestamp=2)
    cli_retry = dict(role='user', content='Retry', timestamp=3, id='cli-retry')
    sidecar_retry = dict(cli_retry, id='sidecar-retry')
    session = SimpleNamespace(messages=[owner if sidecar_owned else replay, sidecar_retry])
    cli = [earlier, replay if sidecar_owned else owner, answer, cli_retry]
    assert routes._merged_session_messages_for_display(session, cli) == [
        earlier, owner, answer, sidecar_retry, cli_retry,
    ]


@pytest.mark.parametrize('action', ['lineage', 'get', 'branch'])
@pytest.mark.parametrize('parent_owned', [False, True])
@pytest.mark.parametrize('marker', [False, True])
def test_lineage_mirror_keeps_owner(monkeypatch, tmp_path, action, parent_owned, marker):
    import api.models as models
    import api.routes as routes

    text = '[Recent Summary (d0, node 418)]' if marker else 'Continue'
    replay = dict(role='user', content=text, timestamp=100)
    owner = dict(replay, _active_turn_token='current:100')
    answer = dict(role='assistant', content='Current answer', timestamp=100)
    parent = _install_test_session(monkeypatch, tmp_path, 'mirror_parent', [owner if parent_owned else replay])
    child = _install_test_session(monkeypatch, tmp_path, 'mirror_child', [replay if parent_owned else owner, answer])
    child.parent_session_id = parent.session_id
    child.context_messages = [replay, owner, answer] if marker else [owner, answer]
    child.save()
    monkeypatch.setattr(routes, 'get_session', lambda sid, **k: parent if sid == parent.session_id else child)
    if action == 'lineage':
        assert routes._merged_webui_lineage_messages_for_display(child) == [owner, answer]
    elif action == 'get':
        handler = _GetHandler(f'/api/session?session_id={child.session_id}&messages=1&resolve_model=0')
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200
        assert [m['content'] for m in handler.response_json['session']['messages']] == [text, 'Current answer']
    else:
        monkeypatch.setattr(routes, '_check_csrf', lambda handler: True)
        monkeypatch.setattr(routes, 'read_body', lambda handler: {'session_id': child.session_id})
        handler = _GetHandler('/api/session/branch')
        routes.handle_post(handler, urlparse(handler.path))
        assert handler.status == 200
        fork = models.Session.load(handler.response_json['session_id'])
        assert fork.messages == [owner, answer]
        assert fork.context_messages == child.context_messages


@pytest.mark.parametrize('identity_field,identities', [
    ('_active_turn_token', ('old:100', 'new:100')),
    ('id', ('parent-user', 'child-user')),
])
def test_lineage_keeps_distinct_same_time_identities(monkeypatch, tmp_path, identity_field, identities):
    import api.routes as routes

    first = dict(role='user', content='Continue', timestamp=100)
    current = dict(first)
    first[identity_field], current[identity_field] = identities
    answer = dict(role='assistant', content='Current answer', timestamp=100)
    parent = _install_test_session(monkeypatch, tmp_path, 'distinct_parent', [first])
    child = _install_test_session(monkeypatch, tmp_path, 'distinct_child', [current, answer])
    child.parent_session_id = parent.session_id
    monkeypatch.setattr(routes, 'get_session', lambda sid, **k: parent if sid == parent.session_id else child)
    assert routes._merged_webui_lineage_messages_for_display(child) == [first, current, answer]


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('same_time', [False, True])
@pytest.mark.parametrize('action', ['merge', 'get', 'branch'])
def test_distinct_ids_are_not_prefix_replays(monkeypatch, tmp_path, reverse, same_time, action):
    import api.models as models
    import api.routes as routes

    earlier = dict(role='user', content='Continue', timestamp=1, id='cli-user')
    answer = dict(role='assistant', content='Answer', timestamp=1 if same_time else 2)
    later = dict(role='user', content='Continue', timestamp=1 if same_time else 3, id='sidecar-user')
    primary, incoming = ([later], [earlier, answer]) if reverse else ([earlier, answer], [later])
    if action == 'merge':
        merged = models.merge_session_messages_append_only(primary, incoming)
        assert [m['id'] for m in merged if m['role'] == 'user'] == ([later['id'], earlier['id']] if reverse else [earlier['id'], later['id']])
        return
    source = _install_test_session(monkeypatch, tmp_path, 'distinct_prefix', primary)
    source.session_source = 'messaging'
    source.context_messages = [earlier, answer, later]
    source.save()
    monkeypatch.setattr(routes, 'get_session', lambda *a, **k: source)
    monkeypatch.setattr(routes, 'get_cli_session_messages', lambda *a, **k: incoming)
    monkeypatch.setattr(routes, '_lookup_cli_session_metadata', lambda *a, **k: {})
    # Sidecar is authoritative on equal timestamps, independent of source length.
    expected = [later, earlier, answer] if reverse and same_time else [earlier, answer, later]
    if action == 'get':
        handler = _GetHandler(f'/api/session?session_id={source.session_id}&messages=1&resolve_model=0')
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200
        rows = handler.response_json['session']['messages']
        assert [m.get('id') for m in rows] == [m.get('id') for m in expected]
    else:
        monkeypatch.setattr(routes, '_check_csrf', lambda handler: True)
        monkeypatch.setattr(routes, 'read_body', lambda handler: {'session_id': source.session_id})
        handler = _GetHandler('/api/session/branch')
        routes.handle_post(handler, urlparse(handler.path))
        assert handler.status == 200
        assert models.Session.load(handler.response_json['session_id']).messages == expected


@pytest.mark.parametrize('same_time', [False, True])
def test_durable_row_ids_are_not_prefix_replays(same_time):
    import api.models as models

    first = dict(role='user', content='Continue', timestamp=1, _row_id=7)
    later = dict(first, timestamp=1 if same_time else 3, _row_id=8)
    assert models.merge_session_messages_append_only([first], [later]) == [first, later]


@pytest.mark.parametrize('action', ['get', 'branch'])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('same_time', [False, True])
def test_mixed_messaging_union_preserves_middle_exchange(monkeypatch, tmp_path, action, reverse, same_time):
    import api.models as models
    import api.routes as routes

    first = dict(role='user', content='First request', timestamp=1, id='first')
    first_answer = dict(role='assistant', content='Initial response', timestamp=2)
    owner = dict(role='user', content='Middle request', timestamp=3, _active_turn_token='middle:3')
    answer = dict(role='assistant', content='Middle response', timestamp=3 if same_time else 4)
    tail = dict(role='user', content='Latest request', timestamp=5, id='tail')
    long, short = [first, first_answer, tail], [owner, answer]
    sidecar, cli = (long, short) if reverse else (short, long)
    expected = [first, first_answer, owner, answer, tail]
    source = _install_test_session(monkeypatch, tmp_path, 'mixed_union', sidecar)
    source.session_source = 'messaging'
    source.context_messages = list(expected)
    source.save()
    monkeypatch.setattr(routes, 'get_session', lambda *a, **k: source)
    monkeypatch.setattr(routes, 'get_cli_session_messages', lambda *a, **k: cli)
    monkeypatch.setattr(routes, '_lookup_cli_session_metadata', lambda *a, **k: {})
    assert routes._merged_session_messages_for_display(source, cli) == expected
    if action == 'get':
        handler = _GetHandler(f'/api/session?session_id={source.session_id}&messages=1&resolve_model=0')
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200
        assert [m['content'] for m in handler.response_json['session']['messages']] == [m['content'] for m in expected]
    else:
        monkeypatch.setattr(routes, '_check_csrf', lambda handler: True)
        monkeypatch.setattr(routes, 'read_body', lambda handler: {'session_id': source.session_id})
        handler = _GetHandler('/api/session/branch')
        routes.handle_post(handler, urlparse(handler.path))
        assert handler.status == 200
        assert models.Session.load(handler.response_json['session_id']).messages == expected


@pytest.mark.parametrize('action', ['lineage', 'get', 'branch'])
@pytest.mark.parametrize('parent_owned', [False, True])
@pytest.mark.parametrize('same_time', [False, True])
def test_lineage_compatible_prefix_preserves_child_tail(monkeypatch, tmp_path, action, parent_owned, same_time):
    import api.models as models
    import api.routes as routes

    replay = dict(role='user', content='Continue', timestamp=100)
    owner = dict(replay, _active_turn_token='current:100')
    history = dict(role='assistant', content='Prior response', timestamp=100)
    tail = [dict(role='user', content='Next question'), dict(role='assistant', content='Next response')]
    if same_time:
        tail = [dict(row, timestamp=100) for row in tail]
    parent = _install_test_session(monkeypatch, tmp_path, 'prefix_parent', [owner if parent_owned else replay, history])
    child = _install_test_session(monkeypatch, tmp_path, 'prefix_child', [replay if parent_owned else owner, history, *tail])
    child.parent_session_id = parent.session_id
    expected = [owner, history, *tail]
    child.context_messages = list(expected)
    child.save()
    monkeypatch.setattr(routes, 'get_session', lambda sid, **k: parent if sid == parent.session_id else child)
    if action == 'lineage':
        assert routes._merged_webui_lineage_messages_for_display(child) == expected
    elif action == 'get':
        handler = _GetHandler(f'/api/session?session_id={child.session_id}&messages=1&resolve_model=0')
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200
        assert [m['content'] for m in handler.response_json['session']['messages']] == [m['content'] for m in expected]
    else:
        monkeypatch.setattr(routes, '_check_csrf', lambda handler: True)
        monkeypatch.setattr(routes, 'read_body', lambda handler: {'session_id': child.session_id})
        handler = _GetHandler('/api/session/branch')
        routes.handle_post(handler, urlparse(handler.path))
        assert handler.status == 200
        fork = models.Session.load(handler.response_json['session_id'])
        assert fork.messages == expected
        assert fork.context_messages == expected


@pytest.mark.parametrize('shape', ['overlap', 'missing', 'numeric', 'tools', 'tool_block'])
@pytest.mark.parametrize('action', ['direct', 'get', 'branch', 'lineage'])
def test_display_union_stable_identity(shape, action, monkeypatch, tmp_path):
    import api.models as models
    import api.routes as routes
    from api.models import merge_session_display_messages

    def row(role, content, **kw):
        return dict(role=role, content=content, timestamp=1, **kw)

    a, aa, b, bb, c, cc = [row(role, text) for role, text in
                            [('user', 'A'), ('assistant', 'a'), ('user', 'B'),
                             ('assistant', 'b'), ('user', 'C'), ('assistant', 'c')]]
    if shape == 'overlap':
        primary, incoming, expected = [a, aa, b, bb], [b, bb, c, cc], [a, aa, b, bb, c, cc]
    elif shape == 'missing':
        bb.pop('timestamp')
        b['timestamp'] = 2
        primary, incoming, expected = [a, aa], [b, bb], [a, aa, b, bb]
    elif shape == 'numeric':
        owner = dict(a, timestamp=1.0, _active_turn_token='current:1', _turnDuration=9)
        primary, incoming, expected = [a, aa], [owner, c], [owner, aa, c]
    elif shape == 'tool_block':
        prior_owner = row('user', 'Earlier prompt', _active_turn_token='prior:1')
        call = row('assistant', '', tool_calls=[
            {'id': 'call-1', 'type': 'function', 'function': {'name': 'first'}},
            {'id': 'call-2', 'type': 'function', 'function': {'name': 'second'}},
        ])
        first_result = row('tool', 'first result', tool_call_id='call-1')
        second_result = row('tool', 'second result', tool_call_id='call-2')
        unrelated_result = row('tool', 'unrelated result', tool_call_id='call-other')
        owner = row('user', 'New prompt', _active_turn_token='current:1')
        answer = row('assistant', 'New answer')
        primary = [prior_owner, call, first_result, second_result, unrelated_result, answer]
        incoming = [dict(call), owner, dict(answer)]
        expected = [prior_owner, call, first_result, second_result, owner, unrelated_result, answer]
    else:
        a = row('tool', 'same', tool_call_id='first')
        b = row('tool', 'same', tool_call_id='second')
        primary, incoming, expected = [a], [b], [a, b]

    def assert_tool_block_order(rows):
        visible = [(r['role'], r.get('content'), r.get('tool_call_id')) for r in rows]
        assert visible == [
            (r['role'], r.get('content'), r.get('tool_call_id')) for r in expected
        ]
        assert visible[1:4] == [
            ('assistant', '', None),
            ('tool', 'first result', 'call-1'),
            ('tool', 'second result', 'call-2'),
        ]
        prompt_index = next(i for i, item in enumerate(rows) if item.get('content') == 'New prompt')
        answer_index = next(i for i, item in enumerate(rows) if item.get('content') == 'New answer')
        assert prompt_index > 3
        assert prompt_index < answer_index

    if action == 'direct':
        merged = merge_session_display_messages(primary, incoming)
        assert merged == expected
        if shape == 'tool_block':
            assert_tool_block_order(merged)
        return
    if shape != 'tool_block':
        assert merge_session_display_messages(primary, incoming) == expected
    source = _install_test_session(monkeypatch, tmp_path, 'stable_union', primary)
    source.session_source = 'messaging'
    source.context_messages = list(expected)
    source.save()
    monkeypatch.setattr(routes, 'get_session', lambda *a, **k: source)
    monkeypatch.setattr(routes, 'get_cli_session_messages', lambda *a, **k: incoming)
    monkeypatch.setattr(routes, '_lookup_cli_session_metadata', lambda *a, **k: {})
    if action == 'lineage':
        child = _install_test_session(monkeypatch, tmp_path, 'stable_child', incoming)
        child.parent_session_id = source.session_id
        source.session_source = 'webui'
        source.save()
        merged = routes._merged_webui_lineage_messages_for_display(child)
        assert merged == expected
        if shape == 'tool_block':
            assert_tool_block_order(merged)
    elif action == 'get':
        handler = _GetHandler(f'/api/session?session_id={source.session_id}&messages=1&resolve_model=0')
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200
        rows = handler.response_json['session']['messages']
        assert [(r['role'], r['content'], r.get('tool_call_id')) for r in rows] == [
            (r['role'], r['content'], r.get('tool_call_id')) for r in expected]
        if shape == 'tool_block':
            assert_tool_block_order(rows)
    else:
        monkeypatch.setattr(routes, '_check_csrf', lambda handler: True)
        monkeypatch.setattr(routes, 'read_body', lambda handler: {'session_id': source.session_id})
        handler = _GetHandler('/api/session/branch')
        routes.handle_post(handler, urlparse(handler.path))
        assert handler.status == 200
        forked = models.Session.load(handler.response_json['session_id']).messages
        assert forked == expected
        if shape == 'tool_block':
            assert_tool_block_order(forked)


@pytest.mark.parametrize('action', ['helper', 'get', 'branch', 'lineage'])
@pytest.mark.parametrize('timestamps', [(1, 2, 3, 4), (.1, .2, .3, .4), (1, 1, 1.2, 1.4)])
@pytest.mark.parametrize(
    'timestamp_fields', [('timestamp', 'timestamp'), ('_ts', 'timestamp'), ('timestamp', '_ts')],
)
def test_display_union_keeps_newer_repeated_owned_exchange(
    monkeypatch, tmp_path, action, timestamps, timestamp_fields,
):
    import api.models as models
    import api.routes as routes

    primary = [
        dict(role='user', content='Repeat', **{timestamp_fields[0]: timestamps[0]}),
        dict(role='assistant', content='Done', **{timestamp_fields[0]: timestamps[1]}),
    ]
    incoming = [
        dict(
            role='user', content='Repeat', _active_turn_token='turn-new',
            **{timestamp_fields[1]: timestamps[2]},
        ),
        dict(role='assistant', content='Done', **{timestamp_fields[1]: timestamps[3]}),
    ]

    def assert_display(rows):
        assert [
            (row['role'], row['content'], row.get('_ts', row.get('timestamp'))) for row in rows
        ] == [
            ('user', 'Repeat', timestamps[0]),
            ('assistant', 'Done', timestamps[1]),
            ('user', 'Repeat', timestamps[2]),
            ('assistant', 'Done', timestamps[3]),
        ]
        assert [row.get('_active_turn_token') for row in rows] == [
            None, None, 'turn-new', None,
        ]

    if action == 'helper':
        assert_display(models.merge_session_display_messages(primary, incoming))
        return

    source = _install_test_session(monkeypatch, tmp_path, 'repeated_owned', primary)
    source.session_source = 'messaging'
    source.context_messages = list(primary)
    source.save()
    monkeypatch.setattr(routes, 'get_session', lambda *a, **k: source)
    monkeypatch.setattr(routes, 'get_cli_session_messages', lambda *a, **k: incoming)
    monkeypatch.setattr(routes, '_lookup_cli_session_metadata', lambda *a, **k: {})

    if action == 'get':
        assert_display(routes._merged_session_messages_for_display(source, incoming))
        handler = _GetHandler(
            f'/api/session?session_id={source.session_id}&messages=1&resolve_model=0'
        )
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200
        rows = handler.response_json['session']['messages']
        assert [
            (row['role'], row['content'], row.get('_ts', row.get('timestamp'))) for row in rows
        ] == [
            ('user', 'Repeat', timestamps[0]),
            ('assistant', 'Done', timestamps[1]),
            ('user', 'Repeat', timestamps[2]),
            ('assistant', 'Done', timestamps[3]),
        ]
        assert all('_active_turn_token' not in row for row in rows)
    elif action == 'branch':
        monkeypatch.setattr(routes, '_check_csrf', lambda handler: True)
        monkeypatch.setattr(
            routes, 'read_body', lambda handler: {'session_id': source.session_id}
        )
        handler = _GetHandler('/api/session/branch')
        routes.handle_post(handler, urlparse(handler.path))
        assert handler.status == 200
        assert_display(models.Session.load(handler.response_json['session_id']).messages)
    else:
        child = _install_test_session(monkeypatch, tmp_path, 'repeated_owned_child', incoming)
        child.parent_session_id = source.session_id
        child.save()
        monkeypatch.setattr(
            routes,
            'get_session',
            lambda sid, **k: source if sid == source.session_id else child,
        )
        assert_display(routes._merged_webui_lineage_messages_for_display(child))


@pytest.mark.parametrize('timestamp_field', ['timestamp', '_ts'])
def test_strict_prefix_distinguishes_unequal_timestamps(timestamp_field):
    from api.models import _session_messages_have_prefix

    first = dict(role='user', content='Repeat', **{timestamp_field: 1})
    second = dict(role='user', content='Repeat', **{timestamp_field: 3})
    assert not _session_messages_have_prefix([first], [second])


def test_display_union_near_cumulative_probe_bound(monkeypatch):
    import api.models as models

    rows = [dict(role='user' if i % 2 == 0 else 'assistant', content=str(i), timestamp=1)
            for i in range(5000)]
    probes = 0
    original = models._message_timestamp_as_float
    def counted(row):
        nonlocal probes
        probes += 1
        return original(row)
    monkeypatch.setattr(models, '_message_timestamp_as_float', counted)
    merged = models.merge_session_display_messages(rows[:-2], rows[2:])
    assert merged == rows
    assert probes < 100000


@pytest.mark.parametrize('kind', ['multiplicity', 'owned_order', 'undated'])
def test_display_union_preserves_primary_and_incoming_order(kind):
    from api.models import merge_session_display_messages
    primary = [dict(role=role, content=text, timestamp=1) for role, text in
               [('user', 'U1'), ('assistant', 'A1'), ('user', 'U2'), ('assistant', 'A2')]]
    if kind == 'multiplicity':
        primary[2:] = [dict(primary[0]), dict(primary[1])]
    if kind == 'owned_order':
        primary[0]['_active_turn_token'] = 'one:1'
        primary[2]['_active_turn_token'] = 'two:1'
    tail = [dict(role='user', content='Next', timestamp=2), dict(role='assistant', content='Reply', timestamp=2)]
    if kind == 'undated':
        tail[0].pop('timestamp')
    incoming = [dict(role='user', content='Earlier', timestamp=0)] + tail if kind == 'undated' else tail
    expected = incoming[:1] + primary + tail if kind == 'undated' else primary + tail
    assert merge_session_display_messages(primary, incoming) == expected


@pytest.mark.parametrize('action', ['helper', 'get', 'branch'])
def test_messaging_display_union_honors_truncation(monkeypatch, tmp_path, action):
    import api.models as models
    import api.routes as routes
    primary = [dict(role='user', content=text, timestamp=i) for i, text in enumerate('ABC', 1)]
    incoming = [primary[0], dict(role='assistant', content='Deleted', timestamp=4)]
    session = _install_test_session(monkeypatch, tmp_path, 'union_truncated', primary)
    session.session_source = 'messaging'
    session.truncation_watermark = session.truncation_boundary = 3
    session.context_messages = list(primary)
    session.save()
    monkeypatch.setattr(routes, 'get_session', lambda *a, **k: session)
    monkeypatch.setattr(routes, 'get_cli_session_messages', lambda *a, **k: incoming)
    monkeypatch.setattr(routes, '_lookup_cli_session_metadata', lambda *a, **k: {})
    if action == 'helper':
        assert routes._merged_session_messages_for_display(session, incoming) == primary
    elif action == 'get':
        handler = _GetHandler(f'/api/session?session_id={session.session_id}&messages=1&resolve_model=0')
        routes.handle_get(handler, urlparse(handler.path))
        assert [r['content'] for r in handler.response_json['session']['messages']] == list('ABC')
    else:
        monkeypatch.setattr(routes, '_check_csrf', lambda h: True)
        monkeypatch.setattr(routes, 'read_body', lambda h: {'session_id': session.session_id})
        handler = _GetHandler('/api/session/branch')
        routes.handle_post(handler, urlparse(handler.path))
        assert models.Session.load(handler.response_json['session_id']).messages == primary


@pytest.mark.parametrize('field', ['api_content', '_active_turn_token'])
@pytest.mark.parametrize('count', [128, 512, 2048])
def test_empty_primary_private_identity_probe_growth(monkeypatch, field, count):
    import api.models as models
    original = models._message_private_identity_compatible
    probes = 0
    def counted(a, b, **kw):
        nonlocal probes
        probes += 1
        assert probes < count * 8
        return original(a, b, **kw)
    monkeypatch.setattr(models, '_message_private_identity_compatible', counted)
    rows = [dict(role='user', content='Same', timestamp=1, **{field: str(i)}) for i in range(count)]
    assert models.merge_session_messages_append_only([], rows + [dict(r) for r in rows]) == rows
    assert probes < count * 8


def test_display_union_promotes_inserted_mirror_without_dropping_primary_repeats():
    from api.models import merge_session_display_messages
    history = dict(role='assistant', content='Prior', timestamp=1)
    user = dict(role='user', content='Current', timestamp=2)
    answer = dict(role='assistant', content='Answer', timestamp=2)
    owner = dict(user, _active_turn_token='current:2')
    assert merge_session_display_messages([history, dict(history)], [user, answer, owner]) == [history, history, owner, answer]


def test_empty_primary_partial_identity_finds_late_mirror():
    from api.models import merge_session_messages_append_only
    rows = [dict(role='user', content='Same', timestamp=1, api_content=str(i),
                 _active_turn_token=f'turn:{i}') for i in range(100)]
    mirror = dict(rows[-1])
    mirror.pop('_active_turn_token')
    assert merge_session_messages_append_only([], rows + [mirror]) == rows


@pytest.mark.parametrize('field', ['api_content', '_active_turn_token', 'id', '_row_id'])
def test_empty_primary_anonymous_row_accepts_only_one_private_claim(field):
    from api.models import merge_session_messages_append_only
    bare = dict(role='user', content='Same', timestamp=1)
    first = dict(bare, **{field: 1 if field == '_row_id' else 'a'})
    second = dict(bare, **{field: 2 if field == '_row_id' else 'b'})
    rows = merge_session_messages_append_only([], [bare, first, second, dict(first), dict(second)])
    if field in {'id', '_row_id'}:
        assert rows == [bare, first, second]
    else:
        assert len(rows) == 2
        assert rows[0] is bare
        assert rows[1] is second


@pytest.mark.parametrize('field', ['id', '_row_id', '_active_turn_token', None])
def test_display_restamp_requires_shared_strong_identity(field):
    from api.models import merge_session_display_messages
    primary = [dict(role='assistant', content=text, timestamp=i) for i, text in enumerate('ABC', 1)]
    if field:
        for i, row in enumerate(primary):
            row[field] = i + 1 if field == '_row_id' else f'identity-{i}'
    incoming = [dict(row, timestamp=row['timestamp'] + .1) for row in primary[1:]]
    tail = dict(role='assistant', content='D', timestamp=4.1)
    merged = merge_session_display_messages(primary, incoming + [tail])
    assert merged == (primary + [tail] if field else [primary[0], primary[1], incoming[0], primary[2], incoming[1], tail])


@pytest.mark.parametrize('action', ['helper', 'get', 'branch'])
def test_display_unambiguous_idless_restamped_replay(monkeypatch, tmp_path, action):
    import api.models as models
    import api.routes as routes
    from types import SimpleNamespace

    primary = [
        dict(role='assistant', content='prior answer', timestamp=100.0),
        dict(role='user', content='first prompt', timestamp=101.0),
        dict(role='assistant', content='first answer', timestamp=101.0),
        dict(role='user', content='second prompt', timestamp=101.0),
        dict(role='assistant', content='second answer', timestamp=101.0),
    ]
    incoming = [
        dict(role='user', content='first prompt', timestamp=101.1),
        dict(
            role='assistant', content='first answer', timestamp=101.2,
            _turnUsage={'output_tokens': 7}, _turnDuration=12,
        ),
        dict(role='user', content='second prompt', timestamp=101.3),
        dict(role='assistant', content='second answer', timestamp=101.4),
    ]
    assert all('id' not in row and 'message_id' not in row for row in primary + incoming)
    expected = [(row['role'], row['content'], row['timestamp']) for row in primary]

    if action == 'helper':
        rows = routes._merged_session_messages_for_display(
            SimpleNamespace(messages=primary), incoming,
        )
    else:
        source = _install_test_session(monkeypatch, tmp_path, 'idless_restamped', primary)
        source.session_source = 'messaging'
        source.context_messages = list(primary)
        source.save()
        monkeypatch.setattr(routes, 'get_session', lambda *a, **k: source)
        monkeypatch.setattr(routes, 'get_cli_session_messages', lambda *a, **k: incoming)
        monkeypatch.setattr(routes, '_lookup_cli_session_metadata', lambda *a, **k: {})
        if action == 'get':
            handler = _GetHandler(
                f'/api/session?session_id={source.session_id}&messages=1&resolve_model=0'
            )
            routes.handle_get(handler, urlparse(handler.path))
            assert handler.status == 200
            rows = handler.response_json['session']['messages']
        else:
            monkeypatch.setattr(routes, '_check_csrf', lambda _handler: True)
            monkeypatch.setattr(routes, 'read_body', lambda _handler: {
                'session_id': source.session_id, 'keep_count': len(primary) + len(incoming),
            })
            handler = _GetHandler('/api/session/branch')
            routes.handle_post(handler, urlparse(handler.path))
            assert handler.status == 200
            rows = models.Session.load(handler.response_json['session_id']).messages

    assert len(rows) == 5
    assert [(row['role'], row['content'], row['timestamp']) for row in rows] == expected
    assert all('_active_turn_token' not in row for row in rows)
    assert '_turnUsage' not in rows[2]
    assert '_turnDuration' not in rows[2]
    if action == 'helper':
        truncated = models.merge_session_display_messages(
            primary,
            incoming + [dict(role='user', content='Deleted suffix', timestamp=101.5)],
            truncation_watermark=101.4,
        )
        assert [(row['role'], row['content'], row['timestamp']) for row in truncated] == expected
        assert '_turnUsage' not in truncated[2]
        assert '_turnDuration' not in truncated[2]


@pytest.mark.parametrize('primary,incoming', [
    (
        [
            dict(role='user', content='first', timestamp=1.0),
            dict(role='assistant', content='inserted middle', timestamp=1.1),
            dict(role='assistant', content='last', timestamp=1.2),
        ],
        [
            dict(role='user', content='first', timestamp=2.0),
            dict(role='assistant', content='last', timestamp=2.1),
        ],
    ),
    (
        [
            dict(role='user', content='repeat', timestamp=1.0),
            dict(role='assistant', content='done', timestamp=1.1),
            dict(role='user', content='repeat', timestamp=1.2),
            dict(role='assistant', content='done', timestamp=1.3),
        ],
        [
            dict(role='user', content='repeat', timestamp=2.0),
            dict(role='assistant', content='done', timestamp=2.1),
        ],
    ),
    (
        [
            dict(role='user', content='owned', timestamp=1.0),
            dict(role='assistant', content='answer', timestamp=1.1),
        ],
        [
            dict(role='user', content='owned', timestamp=2.0, _active_turn_token='source-only'),
            dict(role='assistant', content='answer', timestamp=2.1),
        ],
    ),
    (
        [
            dict(role='user', content='run', timestamp=1.0),
            dict(role='assistant', content='', timestamp=1.1, tool_calls=[
                {'id': 'call', 'type': 'function', 'function': {'name': 'read_file', 'arguments': {'path': 'old'}}},
            ]),
        ],
        [
            dict(role='user', content='run', timestamp=2.0),
            dict(role='assistant', content='', timestamp=2.1, tool_calls=[
                {'id': 'call', 'type': 'function', 'function': {'name': 'read_file', 'arguments': {'path': 'new'}}},
            ]),
        ],
    ),
])
def test_display_idless_replay_rejects_noncontiguous_or_ambiguous_sequences(primary, incoming):
    from api.models import merge_session_display_messages

    assert len(merge_session_display_messages(primary, incoming)) == len(primary) + len(incoming)


def test_display_blank_separator_dedupe_requires_safe_same_turn_identity():
    from api.models import merge_session_display_messages

    first = dict(role='assistant', content='', timestamp=7, id='message-a')
    second = dict(role='assistant', content='', timestamp=7, id='message-b')
    assert merge_session_display_messages([first, second], []) == [first, second]
    assert merge_session_display_messages([first, dict(first, _turnDuration=4)], []) == [first]

    anonymous = dict(role='assistant', content='', timestamp=7)
    assert merge_session_display_messages([anonymous, dict(anonymous)], []) == [anonymous]
    assert merge_session_display_messages([first, dict(anonymous)], []) == [first, dict(anonymous)]


def test_display_blank_assistant_dedupe_identity_work_is_bounded(monkeypatch):
    import api.models as models

    count = 1024
    rows = [
        dict(role='assistant', content='', timestamp=7, id=f'blank-{index}')
        for index in range(count)
    ]
    original = models._message_private_identity_key
    probes = 0

    def counted(row):
        nonlocal probes
        probes += 1
        return original(row)

    monkeypatch.setattr(models, '_message_private_identity_key', counted)
    assert models.merge_session_display_messages(rows, []) == rows
    assert probes <= count * 4


def test_display_blank_assistant_dedupe_respects_private_claims():
    from api.models import merge_session_display_messages

    blank = dict(role='assistant', content='', timestamp=7)
    same_id = dict(blank, id='same')
    owned_same_id = dict(same_id, _active_turn_token='owner:7')
    merged = merge_session_display_messages([same_id, owned_same_id], [])
    assert merged == [same_id]
    assert same_id['_active_turn_token'] == 'owner:7'

    distinct_claim_pairs = [
        [blank, dict(blank, _active_turn_token='owner:7')],
        [dict(blank, _active_turn_token='owner:a'), dict(blank, _active_turn_token='owner:b')],
        [dict(blank, api_content='one'), dict(blank, api_content='two')],
        [dict(blank, id='same', _active_turn_token='owner:a'),
         dict(blank, id='same', _active_turn_token='owner:b')],
        [dict(blank, id='same', _active_turn_token='owner:a'),
         dict(blank, id='same', _active_turn_token='owner:b'), dict(blank, id='same')],
        [dict(blank, id='same', api_content='one'), dict(blank, id='same', api_content='two')],
        [dict(blank, _row_id=1), dict(blank, _row_id=2)],
        [dict(blank, id='bad', message_id='other'), dict(blank, id='bad', message_id='other')],
        [dict(blank, tool_name='read_file'), dict(blank, tool_name='read_file')],
        [dict(blank, _partial_tool_calls=[{'id': 'call-a'}]),
         dict(blank, _partial_tool_calls=[{'id': 'call-a'}])],
    ]
    for rows in distinct_claim_pairs:
        assert merge_session_display_messages(rows, []) == rows


def test_display_truncation_prefilter_does_not_promote_cross_time_metadata():
    from api.models import merge_session_display_messages

    primary = [
        dict(role='user', content='Keep prompt', timestamp=100),
        dict(role='assistant', content='Keep answer', timestamp=100),
    ]
    incoming = [
        dict(role='user', content='Keep prompt', timestamp=101, _active_turn_token='source:101'),
        dict(role='assistant', content='Keep answer', timestamp=102, _turnUsage={'output_tokens': 3}),
        dict(role='user', content='Deleted suffix', timestamp=103),
    ]

    merged = merge_session_display_messages(primary, incoming, truncation_watermark=102)

    assert [(row['role'], row['content'], row['timestamp']) for row in merged] == [
        ('user', 'Keep prompt', 100),
        ('assistant', 'Keep answer', 100),
        ('user', 'Keep prompt', 101),
        ('assistant', 'Keep answer', 102),
    ]
    assert '_active_turn_token' not in merged[0]
    assert '_turnUsage' not in merged[1]
    assert merged[2]['_active_turn_token'] == 'source:101'
    assert merged[3]['_turnUsage'] == {'output_tokens': 3}

    same_time = [dict(role='assistant', content='Same answer', timestamp=100)]
    mirror = dict(same_time[0], _turnDuration=4)
    assert merge_session_display_messages(
        same_time, [mirror], truncation_watermark=100,
    )[0]['_turnDuration'] == 4


@pytest.mark.parametrize('action', ['helper', 'get', 'branch'])
@pytest.mark.parametrize('boundary', [None, 102])
def test_display_truncation_does_not_reintroduce_matched_rows_after_watermark(
    monkeypatch, tmp_path, action, boundary,
):
    import api.models as models
    import api.routes as routes

    primary = [
        dict(role='user', content='Keep prompt', timestamp=100),
        dict(role='assistant', content='Keep answer', timestamp=100),
    ]
    incoming = [
        dict(role='user', content='Keep prompt', timestamp=103, _active_turn_token='source:103'),
        dict(role='assistant', content='Keep answer', timestamp=104, _turnUsage={'output_tokens': 3}),
    ]

    session = _install_test_session(monkeypatch, tmp_path, 'matched_beyond_watermark', primary)
    session.session_source = 'messaging'
    session.truncation_watermark = 102
    session.truncation_boundary = boundary
    session.context_messages = list(primary)
    session.save()
    monkeypatch.setattr(routes, 'get_session', lambda *_a, **_k: session)
    monkeypatch.setattr(routes, 'get_cli_session_messages', lambda *_a, **_k: incoming)
    monkeypatch.setattr(routes, '_lookup_cli_session_metadata', lambda *_a, **_k: {})

    if action == 'helper':
        merged = routes._merged_session_messages_for_display(session, incoming)
    elif action == 'get':
        handler = _GetHandler(
            f'/api/session?session_id={session.session_id}&messages=1&resolve_model=0'
        )
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200
        merged = handler.response_json['session']['messages']
    else:
        monkeypatch.setattr(routes, '_check_csrf', lambda _handler: True)
        monkeypatch.setattr(routes, 'read_body', lambda _handler: {
            'session_id': session.session_id, 'keep_count': len(primary),
        })
        handler = _GetHandler('/api/session/branch')
        routes.handle_post(handler, urlparse(handler.path))
        assert handler.status == 200
        merged = models.Session.load(handler.response_json['session_id']).messages

    assert merged == primary
    assert '_active_turn_token' not in primary[0]
    assert '_turnUsage' not in primary[1]


def test_display_truncation_keeps_advanced_post_edit_tail():
    from api.models import merge_session_display_messages

    primary = [dict(role='user', content='Edited prompt', timestamp=102)]
    incoming = [
        dict(primary[0]),
        dict(role='assistant', content='New answer', timestamp=103),
    ]

    merged = merge_session_display_messages(
        primary, incoming, truncation_watermark=102, truncation_boundary=101,
    )

    assert merged == primary + [incoming[1]]


def test_display_truncation_zero_watermark_keeps_empty_transcript():
    from api.models import merge_session_display_messages

    incoming = [dict(role='user', content='Deleted prompt', timestamp=1)]

    assert merge_session_display_messages(
        [], incoming, truncation_watermark=0,
    ) == []


def test_lineage_get_preserves_same_time_tool_only_partial_assistant_rows(monkeypatch, tmp_path):
    import api.routes as routes

    parent_row = dict(role='user', content='Run both tools', timestamp=1)
    partials = [
        dict(
            role='assistant', content='', timestamp=2, _partial=True,
            _partial_tool_calls=[{'id': 'tool-a', 'name': 'read_file', 'arguments': {'path': 'a.txt'}}],
        ),
        dict(
            role='assistant', content='', timestamp=2, _partial=True,
            _partial_tool_calls=[{'id': 'tool-b', 'name': 'read_file', 'arguments': {'path': 'b.txt'}}],
        ),
    ]
    parent = _install_test_session(monkeypatch, tmp_path, 'partial_lineage_parent', [parent_row])
    child = _install_test_session(monkeypatch, tmp_path, 'partial_lineage_child', partials)
    child.parent_session_id = parent.session_id
    child.session_source = 'webui'
    child.save()
    monkeypatch.setattr(
        routes, 'get_session',
        lambda sid, **_k: child if sid == child.session_id else parent,
    )
    monkeypatch.setattr(routes, '_session_requires_cli_metadata_lookup', lambda _session: False)
    monkeypatch.setattr(routes, 'get_state_db_session_messages', lambda *_a, **_k: [])

    handler = _GetHandler(
        f'/api/session?session_id={child.session_id}&messages=1&resolve_model=0'
    )
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    rows = handler.response_json['session']['messages']
    assert [
        row['_partial_tool_calls'][0]['id']
        for row in rows if row.get('_partial_tool_calls')
    ] == [
        'tool-a', 'tool-b',
    ]


@pytest.mark.parametrize('action', ['helper', 'get', 'branch'])
def test_display_api_content_does_not_identify_restamped_owner(
    monkeypatch, tmp_path, action,
):
    import api.models as models
    import api.routes as routes

    old_owner = dict(
        role='user', content='Continue', timestamp=100, api_content='Continue',
    )
    owner = dict(
        role='user', content='Continue', timestamp=200, api_content='Continue',
        _active_turn_token='run:2',
    )
    answer = dict(role='assistant', content='Okay', timestamp=201)
    primary = [old_owner]
    incoming = [owner, answer]
    expected = [old_owner, owner, answer]

    if action == 'helper':
        merged = models.merge_session_display_messages(primary, incoming)
        assert merged == expected
        assert merged[0].get('_active_turn_token') is None
        assert merged[1]['_active_turn_token'] == 'run:2'
        return

    source = _install_test_session(monkeypatch, tmp_path, 'api_content_restamp', primary)
    source.session_source = 'messaging'
    source.context_messages = list(primary)
    source.save()
    monkeypatch.setattr(routes, 'get_session', lambda *a, **k: source)
    monkeypatch.setattr(routes, 'get_cli_session_messages', lambda *a, **k: incoming)
    monkeypatch.setattr(routes, '_lookup_cli_session_metadata', lambda *a, **k: {})
    expected_public = [
        ('user', 'Continue', 100),
        ('user', 'Continue', 200),
        ('assistant', 'Okay', 201),
    ]

    if action == 'get':
        handler = _GetHandler(
            f'/api/session?session_id={source.session_id}&messages=1&resolve_model=0'
        )
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200
        rows = handler.response_json['session']['messages']
        assert [
            (row['role'], row['content'], row.get('timestamp')) for row in rows
        ] == expected_public
    else:
        monkeypatch.setattr(routes, '_check_csrf', lambda handler: True)
        monkeypatch.setattr(routes, 'read_body', lambda handler: {'session_id': source.session_id})
        handler = _GetHandler('/api/session/branch')
        routes.handle_post(handler, urlparse(handler.path))
        assert handler.status == 200
        fork = models.Session.load(handler.response_json['session_id'])
        assert fork.messages == expected


def test_display_api_content_does_not_merge_repeated_assistant_output():
    from api.models import merge_session_display_messages

    old = dict(role='assistant', content='Okay', timestamp=100, api_content='Okay')
    replay = dict(old, timestamp=200)
    assert merge_session_display_messages([old], [replay]) == [old, replay]


def test_display_same_timestamp_api_content_remains_compatible():
    from api.models import merge_session_display_messages

    original = dict(role='user', content='Continue', timestamp=100, api_content='Continue')
    mirror = dict(original, _turnDuration=12)
    merged = merge_session_display_messages([original], [mirror])
    assert merged == [original]
    assert merged[0]['_turnDuration'] == 12


def test_display_prefix_does_not_claim_restamped_anonymous_answer():
    from api.models import merge_session_display_messages
    first = dict(role='assistant', content='Done', timestamp=1)
    second = dict(first, timestamp=2)
    tail = dict(role='user', content='Next', timestamp=3)
    assert merge_session_display_messages([first], [second, tail]) == [first, second, tail]


@pytest.mark.parametrize('count', [128, 512, 5000])
def test_display_repeated_content_distinct_times_probe_bound(monkeypatch, count):
    import api.models as models
    original = models._message_private_identity_compatible
    probes = 0
    def counted(a, b, **kw):
        nonlocal probes
        probes += 1
        assert probes < count * 12
        return original(a, b, **kw)
    monkeypatch.setattr(models, '_message_private_identity_compatible', counted)
    primary = [dict(role='assistant', content='Same', timestamp=i * 2) for i in range(count)]
    incoming = [dict(role='assistant', content='Same', timestamp=i * 2 + 1) for i in range(count)]
    lead = dict(role='user', content='Lead', timestamp=-1)
    tail = dict(role='user', content='Tail', timestamp=count * 2)
    expected = [lead] + [row for pair in zip(primary, incoming, strict=True) for row in pair] + [tail]
    assert models.merge_session_display_messages([lead] + primary, incoming + [tail]) == expected
