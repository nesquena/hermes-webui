"""#8986 simplify: the session-list cache rebuilds as soon as its source stamp moves.

The reviewed head of #8986 carried a server-side "rate floor": a cached sidebar
payload at most 10 s old was served again ("held") when only the session index,
the gateway metadata or settings had moved. Nine WebUI ``/api/session/*`` and
related POSTs save the session through ``s.save()`` + ``_publish_saved_session``
without publishing ``sessions_changed``. Their only trace is the rewritten
session index, so the very next ``GET /api/sessions`` served the OLD row for up
to 10 s. Master rebuilds at once. The owner chose to drop the hold and keep the
client-side SSE throttle plus the pre-build-stamp certification.

These tests drive the real ``POST`` handlers and the real ``GET /api/sessions``
handler in-process: no stubbed stamp, no stubbed cache.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

import api.config as config
import api.models as models
import api.routes as routes


class _Handler:
    """Minimal request handler for in-process ``handle_get`` / ``handle_post`` calls."""

    def __init__(self, path: str, body: dict | None = None):
        raw = json.dumps(body or {}).encode("utf-8")
        self.path = path
        self.command = "POST" if body is not None else "GET"
        self.headers = {"Content-Length": str(len(raw)), "Content-Type": "application/json"}
        self.rfile = BytesIO(raw)
        self.wfile = BytesIO()
        self.client_address = ("127.0.0.1", 12345)
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def log_message(self, *args, **kwargs):
        pass

    def _safe_webui_print(self, *args, **kwargs):
        pass

    @property
    def response_json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


@pytest.fixture
def sidebar(tmp_path, monkeypatch):
    """An isolated session store whose sidebar is served by the real handler and cache."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")
    gateway_file = tmp_path / "gateway-sessions.json"
    gateway_file.write_text("{}")
    for module in (models, routes):
        monkeypatch.setattr(module, "SESSION_DIR", session_dir, raising=False)
        monkeypatch.setattr(module, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(config, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_file, raising=False)
    monkeypatch.setattr(routes, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(routes, "_gateway_session_metadata_path", lambda: gateway_file)
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(models, "_active_state_db_path", lambda: tmp_path / "state.db", raising=False)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(routes, "load_settings", lambda: {"show_cli_sessions": False})
    # POST side effects outside the session save under test.
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid: {})
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda p, profile=None: p)
    monkeypatch.setattr(routes, "set_last_workspace", lambda *a, **kw: None)
    monkeypatch.setattr(routes, "_resolve_context_length_for_session_model", lambda *a, **kw: 0, raising=False)
    monkeypatch.setattr(config, "_evict_session_agent", lambda *a, **kw: None)
    models.SESSIONS.clear()
    routes._clear_session_list_cache()

    session = models.Session(
        session_id="d3_saved_without_list_event",
        title="Saved without a list event",
        workspace=str(tmp_path),
        model="model-before",
        personality="persona-before",
        messages=[
            {"role": "user", "content": "one", "timestamp": 1000.0},
            {"role": "assistant", "content": "two", "timestamp": 1001.0},
            {"role": "user", "content": "three", "timestamp": 1002.0},
            {"role": "assistant", "content": "four", "timestamp": 1003.0},
        ],
        created_at=1000.0,
        updated_at=1003.0,
    )
    session.save(touch_updated_at=False)
    models.SESSIONS.clear()
    yield SimpleNamespace(sid=session.session_id, tmp_path=tmp_path, index_file=index_file)
    models.SESSIONS.clear()
    routes._clear_session_list_cache()


def _sidebar_row(sid: str) -> dict | None:
    handler = _Handler("/api/sessions?sidebar_source=webui")
    routes.handle_get(handler, urlparse(handler.path))
    assert handler.status == 200, handler.wfile.getvalue()[:500]
    rows = [row for row in handler.response_json["sessions"] if row.get("session_id") == sid]
    assert len(rows) <= 1
    return rows[0] if rows else None


def _post(path: str, body: dict) -> _Handler:
    handler = _Handler(path, body)
    routes.handle_post(handler, urlparse(path))
    assert handler.status == 200, handler.wfile.getvalue()[:500]
    return handler


# (POST path, body beyond session_id, check on the pre-save row, check on the next row).
# A cleared session has no messages left, and the sidebar may hide it altogether.
_SAVES_WITHOUT_LIST_EVENT = [
    ("/api/session/update", {"model": "model-after"},
     lambda row: row["model"] == "model-before", lambda row: row is not None and row["model"] == "model-after"),
    ("/api/session/truncate", {"keep_count": 2},
     lambda row: row["message_count"] == 4, lambda row: row is not None and row["message_count"] == 2),
    ("/api/personality/set", {"name": ""},
     lambda row: row.get("personality") == "persona-before",
     lambda row: row is not None and not row.get("personality")),
    ("/api/session/clear", {},
     lambda row: row["message_count"] == 4, lambda row: row is None or row["message_count"] == 0),
]


@pytest.mark.parametrize(
    "path,extra,before,after",
    _SAVES_WITHOUT_LIST_EVENT,
    ids=["update", "truncate", "personality", "clear"],
)
def test_next_sidebar_read_shows_a_save_that_publishes_no_list_event(
    sidebar, monkeypatch, path, extra, before, after
):
    """D3: the GET right after the POST shows the saved value, not the cached row."""
    published = []
    real_publish = routes._publish_session_list_changed

    def _record_publish(*args, **kwargs):
        published.append(args)
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(routes, "_publish_session_list_changed", _record_publish)

    first = _sidebar_row(sidebar.sid)
    assert first is not None and before(first), first
    # Primed: a second read inside the TTL is a cache hit, so the read after the
    # save can only see it through the source stamp.
    second = _sidebar_row(sidebar.sid)
    assert second is not None and before(second), second
    index_before = sidebar.index_file.stat()

    _post(path, {"session_id": sidebar.sid, **extra})

    assert published == [], f"{path} must stay a save without a list event for this test to pin D3"
    index_after = sidebar.index_file.stat()
    assert (index_after.st_mtime_ns, index_after.st_size) != (index_before.st_mtime_ns, index_before.st_size), (
        "the save must move the session index the stamp watches"
    )
    row = _sidebar_row(sidebar.sid)
    assert after(row), f"{path}: the next GET /api/sessions served the pre-save row {row!r}"


def test_index_only_churn_rebuilds_on_the_next_read(monkeypatch):
    """Unit shape of D3: only the session index moved, so the cached payload is stale.

    The state.db stat pair, the content fingerprint and the all-profiles stamp
    are all unchanged; master rebuilds on the next request and so must this cache.
    """
    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    generation = {"index": 0}

    def _stamp(_key):
        return ((1, 1), (1, 2), (1, 3), (generation["index"], 4), (1, 5), (7, 9), None, 0)

    monkeypatch.setattr(routes, "_session_list_cache_source_stamp", _stamp)
    routes._session_list_cache_clear()
    built = []

    def builder():
        built.append(1)
        return {"sessions": [{"session_id": f"build-{len(built)}"}], "cli_count": 0}

    def served_id():
        return routes._get_cached_session_list_payload(key=key, builder=builder)["sessions"][0]["session_id"]

    try:
        assert served_id() == "build-1"
        assert served_id() == "build-1", "a second read inside the TTL is a cache hit"
        generation["index"] += 1
        assert served_id() == "build-2", "index churn was served from the old entry"
        assert len(built) == 2
    finally:
        routes._session_list_cache_clear()


# --- the pre-build-stamp certification (kept from #8986) ---------------------


def _real_state_db_env(tmp_path, monkeypatch):
    """Real SQLite state.db, real stamp producer, real cache; only file LOCATIONS are pinned."""
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT);"
            "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT);"
            "INSERT INTO sessions VALUES('old','Old title'),('new','New title');"
            "INSERT INTO messages VALUES(1,'old'),(2,'new');"
        )
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "_index.json").write_text("{}")
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    gateway = tmp_path / "gateway-sessions.json"
    gateway.write_text("{}")
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: str(db))
    monkeypatch.setattr(routes, "_gateway_session_metadata_path", lambda: gateway)
    monkeypatch.setattr(routes, "SESSION_DIR", sessions)
    monkeypatch.setattr(routes, "SETTINGS_FILE", settings)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    routes._session_list_cache_clear()
    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )

    def read():
        with sqlite3.connect(db) as conn:
            rows = conn.execute("SELECT id, title FROM sessions ORDER BY id").fetchall()
        return {
            "sessions": [{"session_id": sid, "title": title} for sid, title in rows],
            "cli_count": len(rows),
            "all_profiles": False,
            "active_profile": "default",
            "other_profile_count": 0,
        }

    def read_then_commit():
        """Read the rows, THEN let an external writer commit before the build returns."""
        observed = read()
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO sessions VALUES('third','Committed during build')")
            conn.execute("INSERT INTO messages VALUES(3,'third')")
        return observed

    return SimpleNamespace(db=db, key=key, read=read, read_then_commit=read_then_commit, gateway=gateway)


def _join_background_rebuilds(timeout=5.0):
    for thread in list(threading.enumerate()):
        if thread.name == "session-list-cache-rebuild":
            thread.join(timeout)


def test_a_commit_during_a_background_rebuild_is_not_certified_away(tmp_path, monkeypatch):
    """Stale-by-age entry: the background rebuild stores its payload under the PRE-build stamp."""
    env = _real_state_db_env(tmp_path, monkeypatch)
    try:
        assert len(routes._get_cached_session_list_payload(key=env.key, builder=env.read)["sessions"]) == 2
        with routes._SESSIONS_CACHE_LOCK:
            ts, stamp, payload = routes._SESSIONS_CACHE[env.key]
            routes._SESSIONS_CACHE[env.key] = (ts - routes._SESSIONS_CACHE_TTL_SECONDS - 1.0, stamp, payload)
        served_stale = routes._get_cached_session_list_payload(key=env.key, builder=env.read_then_commit)
        assert len(served_stale["sessions"]) == 2, "a stale-by-age hit is served while it rebuilds"
        _join_background_rebuilds()
        expected = env.read()
        assert len(expected["sessions"]) == 3, "the commit really landed during the background build"
        assert routes._get_cached_session_list_payload(key=env.key, builder=env.read) == expected, (
            "the background rebuild certified its pre-commit rows with a post-commit stamp"
        )
    finally:
        _join_background_rebuilds()
        routes._session_list_cache_clear()


def test_a_commit_during_the_fallback_rebuild_is_not_certified_away(tmp_path, monkeypatch):
    """Owner died without storing: the waiter's fallback build also certifies by its pre-build stamp."""
    env = _real_state_db_env(tmp_path, monkeypatch)
    event, is_owner = routes._session_list_cache_claim_rebuild(env.key)
    assert is_owner
    try:
        first = routes._get_cached_session_list_payload(key=env.key, builder=env.read_then_commit)
        assert len(first["sessions"]) == 2, "the fallback build read the pre-commit rows"
    finally:
        routes._session_list_cache_done(env.key, event)
    expected = env.read()
    assert len(expected["sessions"]) == 3
    try:
        assert routes._get_cached_session_list_payload(key=env.key, builder=env.read) == expected, (
            "the fallback rebuild certified its pre-commit rows with a post-commit stamp"
        )
    finally:
        routes._session_list_cache_clear()


def test_a_commit_hidden_by_a_colliding_stat_pair_is_not_held(tmp_path, monkeypatch):
    """R1 shape (#8987): the db/WAL stat pair collides and MAX(rowid) does not move.

    An external UPDATE leaves both unchanged; only the session index moves. With
    a hold keyed on (stat pair, fingerprint) that read was served stale for up to
    10 s. Without a hold, the moved index rebuilds on the next read, as on master.
    """
    import api.route_session_list_cache as cache_module

    env = _real_state_db_env(tmp_path, monkeypatch)
    real_path_stamp = cache_module._session_list_cache_path_stamp

    def colliding_path_stamp(path):
        if path is not None and str(path).startswith(str(env.db)):
            return (1, 1)  # db and WAL stat identical across the commit
        return real_path_stamp(path)

    monkeypatch.setattr(cache_module, "_session_list_cache_path_stamp", colliding_path_stamp)
    try:
        initial = routes._get_cached_session_list_payload(key=env.key, builder=env.read)
        before = routes._session_list_cache_source_stamp(env.key)
        with sqlite3.connect(env.db) as conn:
            conn.execute("UPDATE sessions SET title='Renamed by Desktop' WHERE id='old'")
        (routes.SESSION_DIR / "_index.json").write_text(json.dumps({"tick": 1}))
        after = routes._session_list_cache_source_stamp(env.key)
        assert before[:2] == after[:2] and before[5] == after[5], "the collision under test"
        assert before != after, "only the session index moved"
        expected = env.read()
        assert expected != initial
        assert routes._get_cached_session_list_payload(key=env.key, builder=env.read) == expected
    finally:
        routes._session_list_cache_clear()
