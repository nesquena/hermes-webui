"""#8986: a session-list build that rewrites the session index itself must still be cached.

The build stores its payload under the source stamp read BEFORE the build (#8694's certification
fix). One stamp slot is the session index (``_index.json``), and the builder itself rewrites it
when an imported sidecar's agent row is gone: ``all_sessions()`` re-adds the on-disk sidecar to the
index and the CLI/Desktop orphan passes prune it again. Certified with its pre-build value, that
slot made every stored entry source-stale at birth, so every ``GET /api/sessions`` was a blocking
full rebuild (8 GETs -> 8 builds). Master read the stamp after the build (1 build). The index slot
is now read after the build; the state.db slots stay pre-build.

Found by the senior review of the "simplify" commit; the probe recipe is the reviewer's.
"""
from __future__ import annotations

import json
import sqlite3
import time
from io import BytesIO
from urllib.parse import urlparse

import pytest

import api.config as config
import api.models as models
import api.routes as routes


class _Handler:
    def __init__(self, path):
        self.path = path
        self.command = "GET"
        self.headers = {"Content-Length": "0"}
        self.rfile = BytesIO(b"")
        self.wfile = BytesIO()
        self.client_address = ("127.0.0.1", 1)
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, *_a):
        pass

    def end_headers(self):
        pass

    def log_message(self, *_a, **_k):
        pass

    def _safe_webui_print(self, *_a, **_k):
        pass


def _store(tmp_path, monkeypatch, show_cli):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    index = sessions / "_index.json"
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    gateway = tmp_path / "gw.json"
    gateway.write_text("{}")
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, started_at REAL)")
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
    )
    conn.execute("INSERT INTO sessions VALUES ('agent_present','cli','present',1000)")
    conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('agent_present','user','x',1000)")
    conn.commit()
    conn.close()
    for module in (models, routes):
        monkeypatch.setattr(module, "SESSION_DIR", sessions, raising=False)
        monkeypatch.setattr(module, "SESSION_INDEX_FILE", index, raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", sessions, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index, raising=False)
    monkeypatch.setattr(routes, "SETTINGS_FILE", settings)
    monkeypatch.setattr(routes, "_gateway_session_metadata_path", lambda: gateway)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: db)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db, raising=False)
    monkeypatch.setattr(models, "_agent_state_db_path", lambda profile=None: db, raising=False)
    monkeypatch.setattr(models, "_strict_agent_state_db_path", lambda profile=None: db, raising=False)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": show_cli})
    models.SESSIONS.clear()
    routes._clear_session_list_cache()
    return index


def _session(tmp_path, sid, title, ts, **extra):
    models.Session(
        session_id=sid,
        title=title,
        workspace=str(tmp_path),
        messages=[
            {"role": "user", "content": "q", "timestamp": ts},
            {"role": "assistant", "content": "r", "timestamp": ts + 1},
        ],
        created_at=ts,
        updated_at=ts + 1,
        **extra,
    ).save(touch_updated_at=False)


@pytest.fixture
def builds(monkeypatch):
    count = {"n": 0}
    real = routes._build_session_list_cache_payload

    def counting(*a, **k):
        count["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(routes, "_build_session_list_cache_payload", counting)
    return count


def _get_sessions(times):
    seen = []
    for _ in range(times):
        handler = _Handler("/api/sessions")
        routes.handle_get(handler, urlparse(handler.path))
        assert handler.status == 200
        seen.append(sorted(r["session_id"] for r in json.loads(handler.wfile.getvalue())["sessions"]))
        time.sleep(0.05)
    time.sleep(0.3)  # let a background rebuild, if any, finish
    return seen


@pytest.mark.parametrize(
    "show_cli, orphan_source",
    [(True, None), (True, "cli"), (False, "desktop")],
    ids=["clean-store", "orphaned-cli-sidecar", "orphaned-desktop-sidecar-cli-off"],
)
def test_back_to_back_gets_reuse_one_build(tmp_path, monkeypatch, builds, show_cli, orphan_source):
    _store(tmp_path, monkeypatch, show_cli)
    _session(tmp_path, "webui_native_one", "Native", 1000.0)
    if orphan_source:
        _session(
            tmp_path,
            f"imported_{orphan_source}_gone",
            f"Imported {orphan_source} gone",
            900.0,
            session_source=orphan_source,
            source_tag=orphan_source,
            raw_source=orphan_source,
            is_cli_session=(orphan_source == "cli"),
        )
    models.SESSIONS.clear()
    routes._clear_session_list_cache()

    seen = _get_sessions(8)

    # Master: one build serves all eight GETs inside the TTL. The pre-build index slot made
    # every GET a blocking rebuild (8 builds) on a store with an orphaned imported sidecar.
    assert builds["n"] <= 2, (builds["n"], seen)
    assert all(rows == seen[0] for rows in seen), seen


def test_the_state_db_slots_stay_pre_build():
    """Only the index slot is taken after the build; a state.db commit mid-build stays visible."""
    pre = ("db-pre", "wal-pre", "gw-pre", "idx-pre", "settings-pre", "fp-pre", "allp-pre", 1)
    post = ("db-post", "wal-post", "gw-post", "idx-post", "settings-post", "fp-post", "allp-post", 2)
    original = routes._session_list_cache_source_stamp_snapshot
    try:
        routes._session_list_cache_source_stamp_snapshot = lambda _key: post
        assert routes._session_list_cache_certified_stamp(("k",), pre) == (
            "db-pre", "wal-pre", "gw-pre", "idx-post", "settings-pre", "fp-pre", "allp-pre", 1
        )
        assert routes._session_list_cache_certified_stamp(("k",), None) is None
        routes._session_list_cache_source_stamp_snapshot = lambda _key: None
        assert routes._session_list_cache_certified_stamp(("k",), pre) == pre
        routes._session_list_cache_source_stamp_snapshot = lambda _key: post[:7]  # streaming 7-tuple
        assert routes._session_list_cache_certified_stamp(("k",), pre) == pre
    finally:
        routes._session_list_cache_source_stamp_snapshot = original
