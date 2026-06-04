import json
import sqlite3
import types
from pathlib import Path
from api import routes


def test_aggregate_insights_for_home_empty(monkeypatch, tmp_path):
    # Test _aggregate_insights_for_home with empty directories
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir(parents=True)
    monkeypatch.setattr(routes, "SESSION_DIR", sess_dir)
    cutoff = 1000.0
    result = routes._aggregate_insights_for_home("default", tmp_path, cutoff)

    assert result == {
        "sessions": 0,
        "messages": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost": 0.0
    }


def test_aggregate_insights_for_home_with_data(monkeypatch, tmp_path):
    cutoff = 1000.0
    
    # 1. Create WebUI session index
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir(parents=True)
    monkeypatch.setattr(routes, "SESSION_DIR", sess_dir)
    
    # Add sessions: one within cutoff, one outside cutoff
    idx_content = [
        {
            "created_at": 1200.0,
            "updated_at": 1200.0,
            "message_count": 5,
            "input_tokens": 100,
            "output_tokens": 50,
            "estimated_cost": 0.05
        },
        {
            "created_at": 500.0,
            "updated_at": 500.0,
            "message_count": 2,
            "input_tokens": 20,
            "output_tokens": 10,
            "estimated_cost": 0.01
        }
    ]
    (sess_dir / "_index.json").write_text(json.dumps(idx_content), encoding="utf-8")

    # 2. Create state.db with sqlite sessions
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            model TEXT,
            message_count INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            estimated_cost_usd REAL,
            started_at REAL,
            ended_at REAL,
            source TEXT
        )
    """)
    # Insert one CLI session within cutoff, one WebUI session (should be excluded), one CLI session outside cutoff
    cur.execute("""
        INSERT INTO sessions (id, model, message_count, input_tokens, output_tokens, estimated_cost_usd, started_at, ended_at, source)
        VALUES 
        ('cli-1', 'gpt-4', 10, 500, 200, 0.20, 1100.0, 1100.0, 'cli'),
        ('web-1', 'gpt-4', 4, 200, 100, 0.10, 1150.0, 1150.0, 'webui'),
        ('cli-2', 'gpt-4', 1, 50, 10, 0.02, 500.0, 500.0, 'cli')
    """)
    conn.commit()
    conn.close()

    result = routes._aggregate_insights_for_home("default", tmp_path, cutoff)

    # Expected: WebUI session 1 (1200.0) + CLI session 1 (1100.0)
    # Total sessions: 2
    # Total messages: 5 (webui) + 10 (cli) = 15
    # Total input: 100 (webui) + 500 (cli) = 600
    # Total output: 50 (webui) + 200 (cli) = 250
    # Total tokens: 600 + 250 = 850
    # Total cost: 0.05 + 0.20 = 0.25
    assert result == {
        "sessions": 2,
        "messages": 15,
        "input_tokens": 600,
        "output_tokens": 250,
        "total_tokens": 850,
        "cost": 0.25
    }


def test_handle_insights_comparative_flow(monkeypatch, tmp_path):
    # Mock global paths
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir(parents=True)
    monkeypatch.setattr(routes, "SESSION_DIR", sess_dir)

    # 1. Create current and previous WebUI sessions in index
    # We want cutoff = today_midnight - (days - 1)*86400. Let's force days = 1.
    # Today midnight will be mock time.time()
    mock_now = 200000.0 # arbitrary time
    import time
    orig_time = time.time
    orig_localtime = time.localtime
    orig_mktime = time.mktime
    orig_strftime = time.strftime
    monkeypatch.setattr("time.time", lambda: mock_now)
    monkeypatch.setattr("time.localtime", lambda t: orig_localtime(t))
    monkeypatch.setattr("time.mktime", lambda t: orig_mktime(t))
    monkeypatch.setattr("time.strftime", lambda fmt, t: orig_strftime(fmt, t))

    # Calculate cutoff: mock_now midnight. Let's assume days = 1.
    # cutoff = midnight. Let's say midnight is mock_now (since we mock mktime to return t)
    # To keep it simple, let's mock routes._time to return customized values:
    # cutoff = 150000.0
    # prev_cutoff = 100000.0 (days = 1 * day_secs = 50000)
    # Let's override _handle_insights time variables by patching mock variables if needed,
    # or just mock time.localtime / time.mktime to produce desired cutoff.
    
    # Actually, routes._handle_insights does:
    # today = _time.localtime(now)
    # today_midnight = _time.mktime(...)
    # first_day_ts = today_midnight - ((days - 1) * 86400)
    # cutoff = first_day_ts
    # prev_cutoff = cutoff - (days * 86400)
    
    # If now = 200000, midnight is 172800 (or similar on UTC).
    # Let's say midnight = 172800. If days = 1:
    # cutoff = 172800. prev_cutoff = 172800 - 86400 = 86400.
    
    # Let's mock routes.parse_qs to force days = 1
    # Let's write current session (ts >= 172800) and previous session (172800 > ts >= 86400)
    idx_content = [
        # Current period (e.g. ts = 180000)
        {
            "created_at": 180000.0,
            "updated_at": 180000.0,
            "message_count": 3,
            "input_tokens": 50,
            "output_tokens": 20,
            "estimated_cost": 0.02,
            "model": "gpt-4",
            "profile": "default"
        },
        # Previous period (e.g. ts = 100000)
        {
            "created_at": 100000.0,
            "updated_at": 100000.0,
            "message_count": 2,
            "input_tokens": 30,
            "output_tokens": 10,
            "estimated_cost": 0.01,
            "model": "gpt-4",
            "profile": "default"
        },
        # Work profile session in current period (ts = 190000)
        {
            "created_at": 190000.0,
            "updated_at": 190000.0,
            "message_count": 10,
            "input_tokens": 200,
            "output_tokens": 100,
            "estimated_cost": 0.10,
            "model": "gpt-4",
            "profile": "work"
        }
    ]
    (sess_dir / "_index.json").write_text(json.dumps(idx_content), encoding="utf-8")

    # Mock _active_state_db_path to return None or empty
    monkeypatch.setattr("api.models._active_state_db_path", lambda: None)

    # Mock list_profiles_api and get_active_profile_name to test profile list
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    
    # We will define two profiles: default and work
    work_dir = tmp_path / "profiles" / "work"
    work_dir.mkdir(parents=True)
    # Create empty sessions directory for work profile
    work_sess_dir = work_dir / "sessions"
    work_sess_dir.mkdir(parents=True)

    monkeypatch.setattr("api.profiles.list_profiles_api", lambda: [
        {"name": "default", "path": str(tmp_path), "is_active": True},
        {"name": "work", "path": str(work_dir), "is_active": False}
    ])

    # Mock response helper j
    responses = []
    def mock_j(handler, payload, **kw):
        responses.append(payload)
        return True
    monkeypatch.setattr(routes, "j", mock_j)

    handler = types.SimpleNamespace()
    # Force mock parsed URL with days=1 query param
    parsed = types.SimpleNamespace(query="days=1")

    # Run handler
    result = routes._handle_insights(handler, parsed)

    assert result is True
    assert len(responses) == 1
    resp = responses[0]

    # Current period totals (from gpt-4 session at 180000.0)
    assert resp["total_sessions"] == 1
    assert resp["total_messages"] == 3
    assert resp["total_tokens"] == 70
    assert resp["total_cost"] == 0.02

    # Previous period totals (from gpt-4 session at 100000.0)
    assert resp["prev_total_sessions"] == 1
    assert resp["prev_total_messages"] == 2
    assert resp["prev_total_tokens"] == 40
    assert resp["prev_total_cost"] == 0.01

    # Profiles breakdown validation
    profiles = resp["profiles"]
    assert len(profiles) == 2
    # work has total_cost = 0.10, default has total_cost = 0.02
    # So sorted order should be work first, then default
    assert profiles[0]["name"] == "work"
    assert profiles[0]["sessions"] == 1
    assert profiles[0]["messages"] == 10
    assert profiles[0]["total_tokens"] == 300
    assert profiles[0]["cost"] == 0.10
    assert profiles[0]["is_active"] is False

    assert profiles[1]["name"] == "default"
    assert profiles[1]["sessions"] == 1
    assert profiles[1]["messages"] == 3
    assert profiles[1]["total_tokens"] == 70
    assert profiles[1]["cost"] == 0.02
    assert profiles[1]["is_active"] is True


def test_insights_cli_session_boundary(monkeypatch, tmp_path):
    cutoff = 1000.0

    # 1. Create empty WebUI index (to isolate CLI results)
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir(parents=True)
    monkeypatch.setattr(routes, "SESSION_DIR", sess_dir)
    (sess_dir / "_index.json").write_text("[]", encoding="utf-8")

    # 2. Create state.db with a CLI session straddling the boundary:
    # started_at = 990.0 (< cutoff)
    # ended_at = 1010.0 (>= cutoff)
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            model TEXT,
            message_count INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            estimated_cost_usd REAL,
            started_at REAL,
            ended_at REAL,
            source TEXT
        )
    """)
    cur.execute("""
        INSERT INTO sessions (id, model, message_count, input_tokens, output_tokens, estimated_cost_usd, started_at, ended_at, source)
        VALUES ('cli-boundary', 'gpt-4', 5, 100, 50, 0.05, 990.0, 1010.0, 'cli')
    """)
    conn.commit()
    conn.close()

    # 3. Test _aggregate_insights_for_home
    agg_result = routes._aggregate_insights_for_home("default", tmp_path, cutoff)
    assert agg_result["sessions"] == 1
    assert agg_result["messages"] == 5
    assert agg_result["total_tokens"] == 150
    assert agg_result["cost"] == 0.05

    # 4. Test _handle_insights
    monkeypatch.setattr("time.time", lambda: 1010.0)
    monkeypatch.setattr("time.mktime", lambda t: 1000.0)
    
    monkeypatch.setattr("api.profiles.get_active_profile_name", lambda: "default")
    monkeypatch.setattr("api.profiles.list_profiles_api", lambda: [
        {"name": "default", "path": str(tmp_path), "is_active": True}
    ])
    monkeypatch.setattr("api.models._active_state_db_path", lambda: db_path)

    responses = []
    monkeypatch.setattr(routes, "j", lambda handler, payload, **kw: responses.append(payload) or True)

    handler = types.SimpleNamespace()
    parsed = types.SimpleNamespace(query="days=1")

    result = routes._handle_insights(handler, parsed)
    assert result is True
    assert len(responses) == 1
    resp = responses[0]

    # CLI session should be in current period
    assert resp["total_sessions"] == 1
    assert resp["total_messages"] == 5
    assert resp["total_tokens"] == 150
    assert resp["total_cost"] == 0.05
    assert resp["prev_total_sessions"] == 0

