"""
Tests for agent-activity + surfaces endpoints (sections 3-4 of
openspec/changes/add-dashboards-and-pixel-office/tasks.md).

Layers:
  1. Scaffold (module import, handler exposure, route registration, docstring contract)
  2. Pure derive_state rules
  3. Pure build_* functions against a seeded tmp state.db
  4. Handler-level (auth 401, cache HIT/MISS, refresh bypass, empty DB,
     profile-scoped enumeration, field contract)
"""
import io
import json
import pathlib
import sqlite3
import time
from urllib.parse import urlparse

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()


# ── 1. Scaffold ────────────────────────────────────────────────────────────

def test_agent_activity_module_importable():
    import api.agent_activity  # noqa: F401


def test_agent_activity_handlers_exposed():
    from api import agent_activity
    for name in (
        "derive_state",
        "build_surface_snapshot",
        "build_surfaces_cards",
        "build_surface_expand",
        "handle_agent_activity",
        "handle_agent_activity_stream",
        "handle_surfaces",
    ):
        assert callable(getattr(agent_activity, name, None))


def test_agent_activity_routes_registered():
    src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    for path in (
        '"/api/agent-activity"',
        '"/api/agent-activity/stream"',
        '"/api/surfaces"',
    ):
        assert path in src


def test_agent_activity_contract_no_runtime_fields_in_docstring():
    import api.agent_activity as m
    doc = (m.__doc__ or "").lower()
    assert "current_tool" in doc or "current tool" in doc
    assert "pending" in doc


# ── 2. derive_state ────────────────────────────────────────────────────────

@pytest.mark.parametrize("last,now,expected", [
    (None,    1_000_000, 'offline'),
    (999_999, 1_000_000, 'working'),    # 1s
    (999_970, 1_000_000, 'working'),    # 30s
    (999_940, 1_000_000, 'working'),    # 60s - border: spec '< 60s -> working', 60s+ -> waiting
    (999_930, 1_000_000, 'waiting'),    # 70s
    (999_700, 1_000_000, 'waiting'),    # 300s - border
    (999_600, 1_000_000, 'idle'),       # 400s
    (910_000, 1_000_000, 'idle'),       # 25h -> still within 24h? 90000s = 25h > 24h -> offline
    (913_600, 1_000_000, 'offline'),    # 86400s -> offline
])
def test_derive_state_boundaries(last, now, expected):
    from api.agent_activity import derive_state
    # Note: the border assertions above encode the INCLUSIVE-of-lower rule:
    #   age in [0, 60)    -> working
    #   age in [60, 300)  -> waiting
    #   age in [300, 86400) -> idle
    #   else              -> offline
    if last is None:
        assert derive_state(last, now) == expected
        return
    age = now - last
    if age < 60:
        expected = 'working'
    elif age < 300:
        expected = 'waiting'
    elif age < 86400:
        expected = 'idle'
    else:
        expected = 'offline'
    assert derive_state(last, now) == expected


def test_derive_state_future_timestamp_clock_skew():
    """Negative ages (future timestamp) should be treated as fresh, not offline."""
    from api.agent_activity import derive_state
    assert derive_state(1_000_050, 1_000_000) == 'working'


# ── 3. Pure builders against a tmp DB ──────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    model TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    message_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL,
    title TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER
);
"""


def seed_db(path, now=None):
    """Three surfaces:
      - 'webui'    session, last message 30s ago      (working)
      - 'telegram' session, last message 200s ago     (waiting)
      - 'cli'      session, last message 20 hours ago (idle)
    """
    if now is None:
        now = time.time()
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_SQL)
    con.executemany(
        "INSERT INTO sessions (id, source, model, started_at, message_count, "
        "title) VALUES (?,?,?,?,?,?)",
        [
            ("w1", "webui",    "gpt-4o",         now - 3600,  2, "web"),
            ("t1", "telegram", "claude-opus-4",  now - 3600,  2, "tg"),
            ("c1", "cli",      "gpt-4o",         now - 90000, 2, "cli"),
        ],
    )
    con.executemany(
        "INSERT INTO messages (session_id, role, timestamp, token_count) VALUES (?,?,?,?)",
        [
            ("w1", "user",      now - 35, 10),
            ("w1", "assistant", now - 30, 20),   # webui last_active = now - 30 -> working
            ("t1", "user",      now - 205, 15),
            ("t1", "assistant", now - 200, 25),  # telegram last_active = now - 200 -> waiting
            ("c1", "user",      now - 72005, 8),
            ("c1", "assistant", now - 72000, 12),  # cli last_active = now - 72000 (20h) -> idle
        ],
    )
    con.commit(); con.close()


class FakeHandler:
    def __init__(self):
        self.status = None
        self.headers_sent = []
        self.wfile = io.BytesIO()

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.headers_sent.append((k, v))

    def end_headers(self):
        pass

    def header(self, name):
        for k, v in self.headers_sent:
            if k.lower() == name.lower():
                return v
        return None


def _call(handler_fn, path, query=""):
    parsed = urlparse(f"{path}?{query}")
    h = FakeHandler()
    handler_fn(h, parsed)
    body = h.wfile.getvalue()
    payload = json.loads(body) if body else None
    return h, payload


@pytest.fixture
def fake_db(tmp_path, monkeypatch):
    """Point stats._get_profile_db_path + auth gate + clear caches."""
    from api import stats, agent_activity
    dbp = tmp_path / "state.db"
    seed_db(dbp)
    monkeypatch.setattr(stats, "_get_profile_db_path", lambda: dbp)
    import api.auth as auth
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    stats._cache_clear_all()
    agent_activity._cache_clear_all()
    yield dbp
    stats._cache_clear_all()
    agent_activity._cache_clear_all()


def test_build_surface_snapshot_three_surfaces_with_correct_states(fake_db):
    from api.agent_activity import build_surface_snapshot
    snap = build_surface_snapshot(fake_db)
    assert set(snap.keys()) == {"surfaces", "generated_at", "profile"}
    by_src = {s["source"]: s for s in snap["surfaces"]}
    assert set(by_src.keys()) == {"webui", "telegram", "cli"}
    assert by_src["webui"]["state"] == "working"
    assert by_src["telegram"]["state"] == "waiting"
    assert by_src["cli"]["state"] == "idle"


def test_build_snapshot_has_active_webui_sessions_only_on_webui(fake_db):
    from api.agent_activity import build_surface_snapshot
    snap = build_surface_snapshot(fake_db)
    by_src = {s["source"]: s for s in snap["surfaces"]}
    assert "active_webui_sessions" in by_src["webui"]
    assert "active_webui_sessions" not in by_src["telegram"]
    assert "active_webui_sessions" not in by_src["cli"]


def test_build_snapshot_never_exposes_runtime_fields(fake_db):
    from api.agent_activity import build_surface_snapshot
    snap = build_surface_snapshot(fake_db)
    banned = {"current_tool", "pending_count", "is_running_tool"}
    for entry in snap["surfaces"]:
        assert banned.isdisjoint(entry.keys()), f"leaked field in {entry}"


def test_build_snapshot_empty_db_returns_empty_surfaces():
    from api.agent_activity import build_surface_snapshot
    snap = build_surface_snapshot(None)
    assert snap["surfaces"] == []
    assert snap["generated_at"] > 0
    assert snap["profile"]


def test_build_snapshot_message_count_24h(fake_db):
    from api.agent_activity import build_surface_snapshot
    snap = build_surface_snapshot(fake_db)
    by_src = {s["source"]: s for s in snap["surfaces"]}
    # webui has 2 messages in last 24h, telegram 2, cli 0 (its messages are 20h ago = in 24h! let me reconsider)
    # 20h < 24h, so cli messages ARE in the 24h window.
    assert by_src["webui"]["message_count_24h"] == 2
    assert by_src["telegram"]["message_count_24h"] == 2
    assert by_src["cli"]["message_count_24h"] == 2


def test_build_surface_expand_returns_five_sessions(fake_db, tmp_path):
    """Seed 7 webui sessions; expand returns only 5 most recent."""
    from api.agent_activity import build_surface_expand
    # Start fresh DB
    dbp = tmp_path / "many.db"
    con = sqlite3.connect(dbp)
    con.executescript(SCHEMA_SQL)
    now = time.time()
    for i in range(7):
        con.execute(
            "INSERT INTO sessions (id, source, model, started_at, message_count, title) "
            "VALUES (?,'telegram','m',?,?,?)",
            (f"s{i}", now - i * 10, i, f"session {i}"),
        )
        con.execute(
            "INSERT INTO messages (session_id, role, timestamp, token_count) VALUES (?,?,?,?)",
            (f"s{i}", "user", now - i * 10, 1),
        )
    con.commit(); con.close()
    out = build_surface_expand(dbp, "telegram")
    assert len(out["sessions"]) == 5
    # Titles are dicts with session_id, title, model, message_count, last_activity
    assert {"session_id", "title", "model", "message_count", "last_activity"} <= set(out["sessions"][0].keys())


def test_build_surface_expand_unknown_source_returns_empty(fake_db):
    from api.agent_activity import build_surface_expand
    out = build_surface_expand(fake_db, "bogus")
    assert out == {"sessions": []}


def test_build_surface_expand_no_db_returns_empty():
    from api.agent_activity import build_surface_expand
    assert build_surface_expand(None, "webui") == {"sessions": []}


def test_build_surface_expand_empty_source_returns_empty(fake_db):
    from api.agent_activity import build_surface_expand
    assert build_surface_expand(fake_db, "") == {"sessions": []}


# ── 4. Handler-level ───────────────────────────────────────────────────────

def test_handle_agent_activity_returns_200_and_three_surfaces(fake_db):
    from api import agent_activity
    h, body = _call(agent_activity.handle_agent_activity, "/api/agent-activity")
    assert h.status == 200
    assert len(body["surfaces"]) == 3


def test_handle_agent_activity_cache_miss_then_hit(fake_db):
    from api import agent_activity
    h1, _ = _call(agent_activity.handle_agent_activity, "/api/agent-activity")
    assert h1.header("X-Cache") == "MISS"
    h2, _ = _call(agent_activity.handle_agent_activity, "/api/agent-activity")
    assert h2.header("X-Cache") == "HIT"


def test_handle_agent_activity_refresh_bypasses_cache(fake_db):
    from api import agent_activity
    _call(agent_activity.handle_agent_activity, "/api/agent-activity")  # prime
    h, _ = _call(agent_activity.handle_agent_activity, "/api/agent-activity", "refresh=1")
    assert h.header("X-Cache") == "MISS"


def test_handle_agent_activity_401_when_unauthenticated(monkeypatch, tmp_path):
    from api import agent_activity, stats
    dbp = tmp_path / "state.db"
    seed_db(dbp)
    monkeypatch.setattr(stats, "_get_profile_db_path", lambda: dbp)
    import api.auth as auth
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda h: None)
    monkeypatch.setattr(auth, "verify_session", lambda c: False)
    agent_activity._cache_clear_all()
    h, _ = _call(agent_activity.handle_agent_activity, "/api/agent-activity")
    assert h.status == 401


def test_handle_agent_activity_contract_webui_only_has_active_sessions(fake_db):
    from api import agent_activity
    _h, body = _call(agent_activity.handle_agent_activity, "/api/agent-activity")
    webui = next(s for s in body["surfaces"] if s["source"] == "webui")
    tg = next(s for s in body["surfaces"] if s["source"] == "telegram")
    assert "active_webui_sessions" in webui
    assert "active_webui_sessions" not in tg


def test_handle_agent_activity_contract_no_runtime_fields_in_response(fake_db):
    from api import agent_activity
    _h, body = _call(agent_activity.handle_agent_activity, "/api/agent-activity")
    banned = {"current_tool", "pending_count", "is_running_tool"}
    for surface in body["surfaces"]:
        assert banned.isdisjoint(surface.keys()), f"leaked in {surface}"


def test_handle_agent_activity_empty_db_returns_200(monkeypatch):
    from api import agent_activity, stats
    monkeypatch.setattr(stats, "_get_profile_db_path", lambda: None)
    import api.auth as auth
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    agent_activity._cache_clear_all()
    h, body = _call(agent_activity.handle_agent_activity, "/api/agent-activity")
    assert h.status == 200
    assert body["surfaces"] == []


def test_handle_surfaces_snapshot_returns_three(fake_db):
    from api import agent_activity
    h, body = _call(agent_activity.handle_surfaces, "/api/surfaces")
    assert h.status == 200
    assert len(body["surfaces"]) == 3


def test_handle_surfaces_expand_returns_sessions(fake_db):
    from api import agent_activity
    h, body = _call(agent_activity.handle_surfaces, "/api/surfaces", "source=webui&expand=1")
    assert h.status == 200
    assert "sessions" in body
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["session_id"] == "w1"


def test_handle_surfaces_expand_unknown_source_returns_empty_sessions(fake_db):
    from api import agent_activity
    h, body = _call(agent_activity.handle_surfaces, "/api/surfaces", "source=bogus&expand=1")
    assert h.status == 200
    assert body == {"sessions": []}


def test_handle_surfaces_expand_separate_cache(fake_db):
    """Expand cache is independent of snapshot cache."""
    from api import agent_activity
    _call(agent_activity.handle_surfaces, "/api/surfaces")  # snapshot MISS
    h, _ = _call(agent_activity.handle_surfaces, "/api/surfaces", "source=webui&expand=1")
    assert h.header("X-Cache") == "MISS"


def test_handle_surfaces_401_when_unauthenticated(monkeypatch, tmp_path):
    from api import agent_activity, stats
    dbp = tmp_path / "state.db"
    seed_db(dbp)
    monkeypatch.setattr(stats, "_get_profile_db_path", lambda: dbp)
    import api.auth as auth
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda h: None)
    monkeypatch.setattr(auth, "verify_session", lambda c: False)
    agent_activity._cache_clear_all()
    h, _ = _call(agent_activity.handle_surfaces, "/api/surfaces")
    assert h.status == 401


def test_handle_agent_activity_stream_missing_watcher_returns_503(monkeypatch, fake_db):
    """SSE without a started watcher should 503, not crash."""
    from api import agent_activity
    import api.gateway_watcher as gw
    monkeypatch.setattr(gw, "get_watcher", lambda: None)
    h, _ = _call(agent_activity.handle_agent_activity_stream, "/api/agent-activity/stream")
    assert h.status == 503


# ── 5. Snapshot signature / delta helpers ──────────────────────────────────

def test_snapshot_signature_ignores_generated_at():
    from api.agent_activity import _snapshot_signature
    a = {"surfaces": [{"source": "webui", "state": "working", "last_active_ts": 1.0,
                        "message_count_24h": 2, "tokens_24h": 10}],
         "generated_at": 100}
    b = {"surfaces": a["surfaces"], "generated_at": 9999}
    assert _snapshot_signature(a) == _snapshot_signature(b)


def test_compute_delta_reports_new_and_mutated_and_removed():
    from api.agent_activity import _compute_delta
    prev = {"surfaces": [
        {"source": "webui", "state": "working", "last_active_ts": 10,
         "message_count_24h": 1, "tokens_24h": 1, "active_webui_sessions": 1},
        {"source": "cli", "state": "idle", "last_active_ts": 1,
         "message_count_24h": 0, "tokens_24h": 0},
    ], "generated_at": 100, "profile": "default"}
    cur = {"surfaces": [
        {"source": "webui", "state": "waiting", "last_active_ts": 20,  # mutated
         "message_count_24h": 1, "tokens_24h": 1, "active_webui_sessions": 1},
        {"source": "telegram", "state": "working", "last_active_ts": 30,  # new
         "message_count_24h": 2, "tokens_24h": 5},
    ], "generated_at": 200, "profile": "default"}
    delta = _compute_delta(prev, cur)
    srcs = [s["source"] for s in delta["surfaces"]]
    assert "webui" in srcs
    assert "telegram" in srcs
    assert "cli" in srcs  # removed → reported as offline
    cli_entry = next(s for s in delta["surfaces"] if s["source"] == "cli")
    assert cli_entry["state"] == "offline"
