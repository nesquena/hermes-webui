"""
Tests for /api/stats/* endpoints (section 2 of
openspec/changes/add-dashboards-and-pixel-office/tasks.md).

Strategy:
  1. Scaffold tests (module import, handler exposure, route registration)
  2. Pure-function tests: feed a temp sqlite DB to the _build_* functions
  3. Handler tests: fake handler object; verify auth, X-Cache, empty-DB fallback,
     cache HIT/MISS, refresh=1 bypass, window clamping

We intentionally do NOT spin up an HTTP server — the handlers are called
directly with a minimal BaseHTTPRequestHandler-like stub.
"""
import io
import pathlib
import sqlite3
import time
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()


# ── Scaffold (retained from stage 1) ───────────────────────────────────────

def test_stats_module_importable():
    import api.stats  # noqa: F401


def test_stats_handlers_exposed():
    from api import stats
    for name in (
        "handle_stats_summary",
        "handle_stats_timeseries",
        "handle_stats_response_time",
        "handle_stats_heatmap",
        "handle_stats_models",
    ):
        assert callable(getattr(stats, name, None))


def test_stats_routes_registered():
    src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    for path in (
        '"/api/stats/summary"',
        '"/api/stats/timeseries"',
        '"/api/stats/response-time"',
        '"/api/stats/heatmap"',
        '"/api/stats/models"',
    ):
        assert path in src


# ── Helpers ────────────────────────────────────────────────────────────────

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
CREATE INDEX idx_sessions_source ON sessions(source);
CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
"""


def seed_db(path, now=None):
    """Seed a realistic fixture:
      - session webui-1: 100 input + 50 output, 3 messages (user, assistant, user)
      - session cli-1:   200 input + 120 output, 4 messages
      - 1 response-time pair with delta = 2.5s
      - 1 response-time pair with delta = 45s (goes to 30s+ bucket)
    """
    if now is None:
        now = time.time()
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_SQL)
    con.execute(
        "INSERT INTO sessions (id, source, model, started_at, message_count, "
        "input_tokens, output_tokens, cache_read_tokens, reasoning_tokens, "
        "estimated_cost_usd, title) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("webui-1", "webui", "gpt-4o", now - 7200, 3, 100, 50, 10, 5, 0.02, "wui"),
    )
    con.execute(
        "INSERT INTO sessions (id, source, model, started_at, message_count, "
        "input_tokens, output_tokens, cache_read_tokens, reasoning_tokens, "
        "estimated_cost_usd, title) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("cli-1", "cli", "claude-opus-4", now - 86400, 4, 200, 120, 0, 0, 0.05, "cli"),
    )
    # webui-1 messages: user@-60, assistant@-57.5 (delta 2.5s), user@-50
    con.execute("INSERT INTO messages (session_id, role, timestamp, token_count) VALUES (?,?,?,?)",
                ("webui-1", "user", now - 60, 20))
    con.execute("INSERT INTO messages (session_id, role, timestamp, token_count) VALUES (?,?,?,?)",
                ("webui-1", "assistant", now - 57.5, 40))
    con.execute("INSERT INTO messages (session_id, role, timestamp, token_count) VALUES (?,?,?,?)",
                ("webui-1", "user", now - 50, 15))
    # cli-1 messages: user@-200, assistant@-155 (delta 45s)
    con.execute("INSERT INTO messages (session_id, role, timestamp, token_count) VALUES (?,?,?,?)",
                ("cli-1", "user", now - 200, 100))
    con.execute("INSERT INTO messages (session_id, role, timestamp, token_count) VALUES (?,?,?,?)",
                ("cli-1", "assistant", now - 155, 120))
    con.execute("INSERT INTO messages (session_id, role, timestamp, token_count) VALUES (?,?,?,?)",
                ("cli-1", "user", now - 140, 60))
    con.execute("INSERT INTO messages (session_id, role, timestamp, token_count) VALUES (?,?,?,?)",
                ("cli-1", "assistant", now - 138, 50))
    con.commit()
    con.close()


class FakeHandler:
    """Bare-minimum stand-in for BaseHTTPRequestHandler just for header capture."""
    def __init__(self):
        self.status = None
        self.headers_sent = []
        self.wfile = io.BytesIO()
        self.body = b""

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


def _call(handler_fn, query_string=""):
    """Call a handler with a fresh FakeHandler + parsed URL. Returns (handler, json)."""
    import json
    parsed = urlparse(f"/api/stats/x?{query_string}")
    h = FakeHandler()
    handler_fn(h, parsed)
    body = h.wfile.getvalue()
    payload = json.loads(body) if body else None
    return h, payload


@pytest.fixture
def fake_db(tmp_path, monkeypatch):
    """Point api.stats._get_profile_db_path at a tmp state.db and clear cache."""
    from api import stats
    dbp = tmp_path / "state.db"
    seed_db(dbp)
    monkeypatch.setattr(stats, "_get_profile_db_path", lambda: dbp)
    # Disable auth for the duration of the test
    import api.auth as auth
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    stats._cache_clear_all()
    yield dbp
    stats._cache_clear_all()


# ── Pure builder tests ─────────────────────────────────────────────────────

def test_build_summary_sums_sessions_and_max_timestamp(fake_db):
    from api import stats
    con = stats._open_state_db_readonly(fake_db)
    try:
        out = stats._build_summary(con, {})
    finally:
        con.close()
    assert out["total_messages"] == 7  # 3 + 4
    assert out["total_input_tokens"] == 300  # 100 + 200
    assert out["total_output_tokens"] == 170
    assert out["total_cache_read_tokens"] == 10
    assert out["total_reasoning_tokens"] == 5
    assert out["total_cost_usd"] == pytest.approx(0.07, abs=1e-6)
    assert out["last_activity_ts"] is not None
    assert isinstance(out["active_webui_sessions"], int)


def test_build_summary_empty_db_structure():
    """Empty state.db path -> conn is None -> structured zero payload."""
    from api import stats
    out = stats._build_summary(None, {})
    assert out["total_messages"] == 0
    assert out["total_input_tokens"] == 0
    assert out["last_activity_ts"] is None


def test_build_timeseries_total_vs_split(fake_db):
    from api import stats
    con = stats._open_state_db_readonly(fake_db)
    try:
        total = stats._build_timeseries(con, {"source": ["total"], "window": ["30"]})
        split = stats._build_timeseries(con, {"source": ["split"], "window": ["30"]})
    finally:
        con.close()
    assert total["source"] == "total"
    assert split["source"] == "split"
    # total draws from messages.token_count
    total_sum = sum(p["total"] for p in total["points"])
    assert total_sum == 20 + 40 + 15 + 100 + 120 + 60 + 50
    # split draws input/output from sessions
    input_sum = sum(p["input"] for p in split["points"])
    assert input_sum == 300


def test_build_timeseries_granularity_normalized(fake_db):
    from api import stats
    con = stats._open_state_db_readonly(fake_db)
    try:
        out = stats._build_timeseries(con, {"granularity": ["month"]})
    finally:
        con.close()
    assert out["granularity"] == "month"
    # bucket format includes YYYY-MM
    for p in out["points"]:
        assert len(p["date"]) == 7 and p["date"][4] == "-"


def test_build_timeseries_window_clamped_high(fake_db):
    from api import stats
    con = stats._open_state_db_readonly(fake_db)
    try:
        out = stats._build_timeseries(con, {"window": ["9999"]})
    finally:
        con.close()
    assert out["window"] == 365


def test_build_response_time_buckets_correctly(fake_db):
    from api import stats
    con = stats._open_state_db_readonly(fake_db)
    try:
        out = stats._build_response_time(con, {})
    finally:
        con.close()
    # We seeded: delta=2.5s (bucket 1-3s) and delta=45s (bucket 30s+)
    # There's also user@-140 followed by assistant@-138 in cli-1 -> delta=2s (bucket 1-3s)
    by_label = {b["label"]: b for b in out["buckets"]}
    assert by_label["1-3s"]["count"] == 2
    assert by_label["30s+"]["count"] == 1
    assert by_label["0-1s"]["count"] == 0
    assert out["total"] == 3


def test_build_response_time_filters_over_10min(fake_db, tmp_path):
    """Delta > 600s must be dropped."""
    from api import stats
    dbp = tmp_path / "long.db"
    con = sqlite3.connect(dbp)
    con.executescript(SCHEMA_SQL)
    con.execute("INSERT INTO sessions (id, source, started_at) VALUES ('s', 'cli', ?)", (time.time(),))
    now = time.time()
    con.execute("INSERT INTO messages (session_id, role, timestamp, token_count) VALUES ('s','user',?,0)", (now - 800,))
    con.execute("INSERT INTO messages (session_id, role, timestamp, token_count) VALUES ('s','assistant',?,0)", (now - 100,))
    con.commit(); con.close()
    con = stats._open_state_db_readonly(dbp)
    try:
        out = stats._build_response_time(con, {})
    finally:
        con.close()
    assert out["total"] == 0


def test_build_heatmap_7x24_shape(fake_db):
    from api import stats
    con = stats._open_state_db_readonly(fake_db)
    try:
        out = stats._build_heatmap(con, {})
    finally:
        con.close()
    assert len(out["cells"]) == 7
    assert all(len(row) == 24 for row in out["cells"])
    assert sum(sum(row) for row in out["cells"]) > 0


def test_build_models_groups_and_ranks(fake_db):
    from api import stats
    con = stats._open_state_db_readonly(fake_db)
    try:
        out = stats._build_models(con, {})
    finally:
        con.close()
    models = out["models"]
    assert len(models) == 2
    # claude-opus-4 has more tokens (320 vs 150), so comes first
    assert models[0]["model"] == "claude-opus-4"
    assert models[0]["input_tokens"] == 200
    assert models[0]["output_tokens"] == 120
    # pct sums to 100
    assert sum(m["pct"] for m in models) == pytest.approx(100.0, abs=0.1)


# ── Handler-level tests (auth, cache, refresh) ─────────────────────────────

def test_handler_returns_401_when_auth_enabled_and_no_cookie(monkeypatch, tmp_path):
    """If auth is enabled and no valid cookie is present, 401 with no body JSON."""
    from api import stats
    dbp = tmp_path / "state.db"
    seed_db(dbp)
    monkeypatch.setattr(stats, "_get_profile_db_path", lambda: dbp)
    import api.auth as auth
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "parse_cookie", lambda handler: None)
    monkeypatch.setattr(auth, "verify_session", lambda cookie: False)
    h, body = _call(stats.handle_stats_summary)
    assert h.status == 401


def test_handler_cache_miss_then_hit(fake_db):
    from api import stats
    h1, _ = _call(stats.handle_stats_summary)
    assert h1.header("X-Cache") == "MISS"
    h2, _ = _call(stats.handle_stats_summary)
    assert h2.header("X-Cache") == "HIT"


def test_handler_refresh_bypasses_cache(fake_db):
    from api import stats
    h1, _ = _call(stats.handle_stats_summary)
    assert h1.header("X-Cache") == "MISS"
    h2, _ = _call(stats.handle_stats_summary, "refresh=1")
    assert h2.header("X-Cache") == "MISS"


def test_handler_empty_db_returns_zero_structure(monkeypatch):
    """If state.db does not exist, handler returns a zero-structure payload."""
    from api import stats
    monkeypatch.setattr(stats, "_get_profile_db_path", lambda: None)
    import api.auth as auth
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    stats._cache_clear_all()
    h, body = _call(stats.handle_stats_summary)
    assert h.status == 200
    assert body["total_messages"] == 0
    assert body["last_activity_ts"] is None


def test_handler_window_clamp_accepted(fake_db):
    """Oversized window param must be clamped silently, not 4xx."""
    from api import stats
    h, body = _call(stats.handle_stats_timeseries, "window=9999")
    assert h.status == 200
    assert body["window"] == 365


def test_handler_all_five_endpoints_return_200(fake_db):
    from api import stats
    for fn in (
        stats.handle_stats_summary,
        stats.handle_stats_timeseries,
        stats.handle_stats_response_time,
        stats.handle_stats_heatmap,
        stats.handle_stats_models,
    ):
        h, body = _call(fn)
        assert h.status == 200, f"{fn.__name__} returned {h.status}"
        assert isinstance(body, dict)


def test_readonly_connection_rejects_writes(fake_db):
    """Sanity: the connection api.stats opens cannot execute INSERT."""
    from api import stats
    con = stats._open_state_db_readonly(fake_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO sessions (id, source, started_at) VALUES ('x', 'x', 0)")
    finally:
        con.close()
