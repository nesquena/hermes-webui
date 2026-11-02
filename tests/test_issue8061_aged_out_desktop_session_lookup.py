import io
import json
import sqlite3
import time
from types import SimpleNamespace
from pathlib import Path
import pytest
from api import agent_sessions, models, routes
from api.models import SESSIONS, Session
from tests.test_pr7555_cli_session_availability import (
    _configure_real_single_profile,
    _insert_session,
    _make_state_db,
)


class _FakeHandler:
    def __init__(self, body: bytes = b"{}"):
        self.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
        }
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass


def _capture_post(monkeypatch, body):
    captured: dict = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body)

    def _j(handler, payload, status=200, extra_headers=None, **_kw):
        captured["payload"] = payload
        captured["status"] = status
        return True

    def _bad(handler, msg, status=400, **_kw):
        captured["payload"] = {"error": msg}
        captured["status"] = status
        return True

    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(routes, "bad", _bad)
    return captured


def test_aged_out_desktop_session_lookup_succeeds(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    # Create 25 desktop sessions
    _make_state_db(db, sessions=25, messages_per_session=1, source="desktop", session_source="desktop")
    _configure_real_single_profile(monkeypatch, tmp_path, db)
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 20)

    # 1. get_cli_sessions returns only the most recent 20
    recent = models.get_cli_sessions()
    recent_sids = {r["session_id"] for r in recent}
    assert len(recent) <= 20
    assert "cli_perf_0000" not in recent_sids
    assert "cli_perf_0024" in recent_sids

    # 2. _lookup_cli_session_metadata finds the aged-out session outside the window (#8061)
    aged_meta = routes._lookup_cli_session_metadata("cli_perf_0000")
    assert aged_meta.get("session_id") == "cli_perf_0000"
    assert aged_meta.get("source_tag") == "desktop"

    # 3. Nonexistent session still returns {}
    assert routes._lookup_cli_session_metadata("nonexistent-sid") == {}

    # 4. get_cli_session_by_id source filtering
    assert models.get_cli_session_by_id("cli_perf_0000", "desktop")["session_id"] == "cli_perf_0000"
    assert models.get_cli_session_by_id("cli_perf_0000", "oneshot") == {}


def test_aged_out_local_sources_positive_controls(tmp_path, monkeypatch):
    """Local sources (desktop, tui, cli, acp, oneshot) aged outside the window are discovered."""
    db = tmp_path / "state.db"
    _make_state_db(db, sessions=25, messages_per_session=1, source="desktop", session_source="desktop")
    _configure_real_single_profile(monkeypatch, tmp_path, db)
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 20)

    conn = sqlite3.connect(str(db))
    base_time = time.time() - 1000
    local_cases = ["desktop", "tui", "cli", "acp", "oneshot"]
    for src in local_cases:
        sid = f"aged_{src}_sid"
        conn.execute(
            "INSERT INTO sessions (id, source, session_source, title, model, started_at, message_count) "
            "VALUES (?, ?, ?, ?, 'openai/gpt-5', ?, 1)",
            (sid, src, src, f"Title {src}", base_time),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, 'user', 'hi', ?)",
            (f"msg_{src}", sid, base_time),
        )
    conn.commit()
    conn.close()

    for src in local_cases:
        sid = f"aged_{src}_sid"
        meta = routes._lookup_cli_session_metadata(sid)
        assert meta.get("session_id") == sid, f"Local source {src} must be discovered when aged out"
        assert meta.get("source_tag") == src


def test_aged_out_foreign_sources_refusal(tmp_path, monkeypatch):
    """Foreign sources (unknown, platformless gateway, external_agent) aged outside the window are not discovered."""
    db = tmp_path / "state.db"
    _make_state_db(db, sessions=25, messages_per_session=1, source="desktop", session_source="desktop")
    _configure_real_single_profile(monkeypatch, tmp_path, db)
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 20)

    conn = sqlite3.connect(str(db))
    base_time = time.time() - 2000
    foreign_cases = [
        ("aged_unknown_sid", "unknown", "unknown"),
        ("aged_gateway_sid", "gateway", "gateway"),
        ("aged_ext_agent_sid", "external_agent", "external_agent"),
    ]
    for sid, src, sess_src in foreign_cases:
        conn.execute(
            "INSERT INTO sessions (id, source, session_source, title, model, started_at, message_count) "
            "VALUES (?, ?, ?, ?, 'openai/gpt-5', ?, 1)",
            (sid, src, sess_src, f"Title {src}", base_time),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, 'user', 'hi', ?)",
            (f"msg_{sid}", sid, base_time),
        )
    conn.commit()
    conn.close()

    for sid, src, _ in foreign_cases:
        meta = routes._lookup_cli_session_metadata(sid)
        assert meta == {}, f"Foreign source {src} ({sid}) must NOT be discovered by _lookup_cli_session_metadata"


def test_authenticated_write_consumer_regressions(tmp_path, monkeypatch):
    """Authenticated POST /api/session/rename refuses foreign and stale-webui rows, permits aged local rows."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    index_file = tmp_path / "session_index.json"
    db = tmp_path / "state.db"

    _make_state_db(db, sessions=25, messages_per_session=1, source="desktop", session_source="desktop")
    _configure_real_single_profile(monkeypatch, tmp_path, db)
    monkeypatch.setattr(models, "CLI_VISIBLE_SESSION_LIMIT", 20)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(models, "_get_profile_home", lambda p: tmp_path)

    conn = sqlite3.connect(str(db))
    base_time = time.time() - 3000

    # 1. Foreign-source aged rows in state.db
    foreign_sids = ["aged_rename_unknown", "aged_rename_gateway", "aged_rename_ext_agent"]
    for sid, src in zip(foreign_sids, ["unknown", "gateway", "external_agent"]):
        conn.execute(
            "INSERT INTO sessions (id, source, session_source, title, model, started_at, message_count) "
            "VALUES (?, ?, ?, ?, 'openai/gpt-5', ?, 1)",
            (sid, src, src, f"Old {src}", base_time),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, 'user', 'hello', ?)",
            (f"msg_{sid}", sid, base_time),
        )

    # 2. Stale index-marked webui row (sidecar absent)
    stale_webui_sid = "aged_rename_stale_webui"
    conn.execute(
        "INSERT INTO sessions (id, source, session_source, title, model, started_at, message_count) "
        "VALUES (?, 'webui', 'webui', 'Stale WebUI', 'openai/gpt-5', ?, 1)",
        (stale_webui_sid, base_time),
    )
    conn.execute(
        "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, 'user', 'hello', ?)",
        (f"msg_{stale_webui_sid}", stale_webui_sid, base_time),
    )
    index_file.write_text(json.dumps([{"session_id": stale_webui_sid, "source_tag": "webui"}]), encoding="utf-8")

    # 3. Local-source aged rows in state.db (positive controls)
    local_sids = ["aged_rename_desktop", "aged_rename_cli", "aged_rename_tui"]
    for sid, src in zip(local_sids, ["desktop", "cli", "tui"]):
        conn.execute(
            "INSERT INTO sessions (id, source, session_source, title, model, started_at, message_count) "
            "VALUES (?, ?, ?, ?, 'openai/gpt-5', ?, 1)",
            (sid, src, src, f"Old {src}", base_time),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, 'user', 'hello', ?)",
            (f"msg_{sid}", sid, base_time),
        )
    conn.commit()
    conn.close()

    # Verify foreign sources refuse rename with 404, no sidecar created
    for sid in foreign_sids:
        body = {"session_id": sid, "title": "New Title"}
        captured = _capture_post(monkeypatch, body)
        res = routes.handle_post(
            _FakeHandler(b'{"session_id": "' + sid.encode() + b'", "title": "New Title"}'),
            SimpleNamespace(path="/api/session/rename"),
        )
        assert res is True
        assert captured["status"] == 404, f"Foreign session {sid} rename must return 404, got {captured}"
        assert not (session_dir / f"{sid}.json").exists(), f"Sidecar must not be created for {sid}"

    # Verify stale index-marked webui row refuses rename with 404, no sidecar created
    body = {"session_id": stale_webui_sid, "title": "New Title"}
    captured = _capture_post(monkeypatch, body)
    res = routes.handle_post(
        _FakeHandler(b'{"session_id": "' + stale_webui_sid.encode() + b'", "title": "New Title"}'),
        SimpleNamespace(path="/api/session/rename"),
    )
    assert res is True
    assert captured["status"] == 404, f"Stale webui row rename must return 404, got {captured}"
    assert not (session_dir / f"{stale_webui_sid}.json").exists(), "Sidecar must not be recreated for stale webui row"

    # Verify local sources succeed with 200, sidecar materialized with new title
    for sid in local_sids:
        body = {"session_id": sid, "title": "Updated Title"}
        captured = _capture_post(monkeypatch, body)
        res = routes.handle_post(
            _FakeHandler(b'{"session_id": "' + sid.encode() + b'", "title": "Updated Title"}'),
            SimpleNamespace(path="/api/session/rename"),
        )
        assert res is True
        assert captured["status"] == 200, f"Local session {sid} rename must return 200, got {captured}"
        sidecar_file = session_dir / f"{sid}.json"
        assert sidecar_file.exists(), f"Sidecar must be created for local session {sid}"
        saved_data = json.loads(sidecar_file.read_text(encoding="utf-8"))
        assert saved_data.get("title") == "Updated Title"


def test_exclude_sources_preserved_with_wanted_session_ids(tmp_path):
    """read_importable_agent_session_rows enforces exclude_sources even when session_ids are supplied."""
    db = tmp_path / "state.db"
    _make_state_db(db, sessions=2, messages_per_session=1)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE sessions SET source = 'cron' WHERE id = 'cli_perf_0000'")
    conn.execute("UPDATE sessions SET source = 'desktop' WHERE id = 'cli_perf_0001'")
    conn.commit()
    conn.close()

    # Querying cron session with exclude_sources=('cron',) must return empty
    rows = agent_sessions.read_importable_agent_session_rows(
        db,
        exclude_sources=("cron",),
        session_ids=("cli_perf_0000",),
    )
    assert len(rows) == 0

    # Querying desktop session with exclude_sources=('cron',) returns the row
    rows = agent_sessions.read_importable_agent_session_rows(
        db,
        exclude_sources=("cron",),
        session_ids=("cli_perf_0001",),
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "cli_perf_0001"
