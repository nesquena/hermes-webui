"""
Hermes Web UI -- Stats aggregation API.

Read-only SQL aggregation over state.db for the Insights panel.
All queries use SELECT-only connections (file:...?mode=ro URI).
Each profile has an independent DB path resolved via api.profiles.

Endpoints served (registered in api/routes.py handle_get):
- GET /api/stats/summary
- GET /api/stats/timeseries
- GET /api/stats/response-time
- GET /api/stats/heatmap
- GET /api/stats/models

All responses are JSON; all queries go through a 30s in-memory TTL cache
keyed by (endpoint, query_string, profile_hermes_home). The ?refresh=1
query parameter bypasses the cache. Responses carry X-Cache: HIT|MISS.

See openspec/changes/add-dashboards-and-pixel-office/design.md (D1, D6)
and specs/insights-panel/spec.md for the contract.
"""
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30
RESPONSE_TIME_FILTER_SECONDS = 600  # drop deltas longer than 10 minutes


# ── DB connection ──────────────────────────────────────────────────────────

def _get_profile_db_path():
    """Resolve state.db path for the currently active profile.
    Returns a pathlib.Path, or None if the DB file does not exist.
    """
    try:
        from api.profiles import get_active_hermes_home
        home = Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        home = Path(os.getenv('HERMES_HOME', str(Path.home() / '.hermes'))).expanduser().resolve()
    db_path = home / 'state.db'
    return db_path if db_path.exists() else None


def _open_state_db_readonly(db_path):
    """Open state.db with mode=ro URI. SQLite rejects any write on this conn."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


# ── TTL cache ──────────────────────────────────────────────────────────────

_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key, ttl):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        ts, val = entry
        if time.time() - ts > ttl:
            _CACHE.pop(key, None)
            return None
        return val


def _cache_set(key, val):
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), val)


def _cache_clear_all():
    """Exposed for tests."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ── Auth ──────────────────────────────────────────────────────────────────

def _require_auth(handler):
    """Returns True if request passes auth (or auth is disabled); else writes
    401 and returns False. Mirrors the pattern at api/routes.py:1670.
    """
    from api.auth import is_auth_enabled, parse_cookie, verify_session
    if not is_auth_enabled():
        return True
    cv = parse_cookie(handler)
    if cv and verify_session(cv):
        return True
    handler.send_response(401)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(b'{"error":"Authentication required"}')))
    handler.end_headers()
    handler.wfile.write(b'{"error":"Authentication required"}')
    return False


# ── JSON response with X-Cache ─────────────────────────────────────────────

def _j_cached(handler, payload, cache_hit, status=200):
    from api.helpers import _security_headers
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Cache', 'HIT' if cache_hit else 'MISS')
    _security_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


# ── Query parsing ──────────────────────────────────────────────────────────

def _qs(parsed):
    return parse_qs(parsed.query or '')


def _qs_int(qs, key, default, min_val=1, max_val=None):
    raw = qs.get(key, [str(default)])[0]
    try:
        v = int(raw)
    except (ValueError, TypeError):
        v = default
    if v < min_val:
        v = min_val
    if max_val is not None and v > max_val:
        v = max_val
    return v


def _qs_str(qs, key, default, allowed=None):
    v = qs.get(key, [default])[0]
    if allowed and v not in allowed:
        v = default
    return v


def _qs_bool(qs, key):
    v = qs.get(key, [''])[0]
    return v in ('1', 'true', 'True', 'yes')


# ── Handler plumbing ───────────────────────────────────────────────────────

def _serve(handler, parsed, query_key, builder, ttl=CACHE_TTL_SECONDS):
    """Common: auth gate, profile DB, cache lookup, builder, cache write."""
    if not _require_auth(handler):
        return True
    db_path = _get_profile_db_path()
    qs = _qs(parsed)
    refresh = _qs_bool(qs, 'refresh')
    cache_params = tuple(sorted((k, tuple(v)) for k, v in qs.items() if k != 'refresh'))
    cache_key = (str(db_path) if db_path else None, query_key, cache_params)
    if db_path is None:
        empty = builder(None, qs)
        _j_cached(handler, empty, False)
        return True
    if not refresh:
        cached = _cache_get(cache_key, ttl)
        if cached is not None:
            _j_cached(handler, cached, True)
            return True
    conn = _open_state_db_readonly(db_path)
    try:
        result = builder(conn, qs)
    finally:
        conn.close()
    _cache_set(cache_key, result)
    _j_cached(handler, result, False)
    return True


# ── /api/stats/summary ─────────────────────────────────────────────────────

def _build_summary(conn, qs):
    if conn is None:
        return _empty_summary()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            COALESCE(SUM(input_tokens), 0) AS input,
            COALESCE(SUM(output_tokens), 0) AS output,
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read,
            COALESCE(SUM(reasoning_tokens), 0) AS reasoning,
            COALESCE(SUM(estimated_cost_usd), 0.0) AS cost,
            COALESCE(SUM(message_count), 0) AS msgs
        FROM sessions
    """)
    row = cur.fetchone()
    cur.execute("SELECT MAX(timestamp) AS last_ts FROM messages")
    last_ts_row = cur.fetchone()
    last_ts = last_ts_row["last_ts"] if last_ts_row else None
    try:
        from api.config import SESSIONS
        webui_active = len(SESSIONS)
    except Exception:
        webui_active = 0
    return {
        "total_messages": int(row["msgs"]),
        "total_input_tokens": int(row["input"]),
        "total_output_tokens": int(row["output"]),
        "total_cache_read_tokens": int(row["cache_read"]),
        "total_reasoning_tokens": int(row["reasoning"]),
        "total_cost_usd": float(row["cost"]),
        "active_webui_sessions": webui_active,
        "last_activity_ts": float(last_ts) if last_ts is not None else None,
    }


def _empty_summary():
    try:
        from api.config import SESSIONS
        webui_active = len(SESSIONS)
    except Exception:
        webui_active = 0
    return {
        "total_messages": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_reasoning_tokens": 0,
        "total_cost_usd": 0.0,
        "active_webui_sessions": webui_active,
        "last_activity_ts": None,
    }


def handle_stats_summary(handler, parsed):
    return _serve(handler, parsed, 'summary', _build_summary)


# ── /api/stats/timeseries ─────────────────────────────────────────────────

_GRANULARITY_FMT = {
    'day': '%Y-%m-%d',
    'week': '%Y-W%W',
    'month': '%Y-%m',
}


def _build_timeseries(conn, qs):
    granularity = _qs_str(qs, 'granularity', 'day', allowed=set(_GRANULARITY_FMT.keys()))
    window = _qs_int(qs, 'window', 30, min_val=1, max_val=365)
    source = _qs_str(qs, 'source', 'total', allowed={'total', 'split'})
    fmt = _GRANULARITY_FMT[granularity]
    if conn is None:
        return {"granularity": granularity, "window": window, "source": source, "points": []}
    cutoff = time.time() - window * 86400
    cur = conn.cursor()
    if source == 'total':
        cur.execute("""
            SELECT strftime(?, timestamp, 'unixepoch') AS bucket,
                   COALESCE(SUM(token_count), 0) AS total,
                   COUNT(*) AS msgs
            FROM messages
            WHERE timestamp > ?
            GROUP BY bucket
            ORDER BY bucket
        """, (fmt, cutoff))
        points = [{
            "date": r["bucket"],
            "total": int(r["total"] or 0),
            "count": int(r["msgs"] or 0),
        } for r in cur.fetchall()]
    else:
        cur.execute("""
            SELECT strftime(?, started_at, 'unixepoch') AS bucket,
                   COALESCE(SUM(input_tokens), 0) AS input,
                   COALESCE(SUM(output_tokens), 0) AS output,
                   COALESCE(SUM(cache_read_tokens), 0) AS cache_read,
                   COALESCE(SUM(reasoning_tokens), 0) AS reasoning,
                   COALESCE(SUM(estimated_cost_usd), 0.0) AS cost
            FROM sessions
            WHERE started_at > ?
            GROUP BY bucket
            ORDER BY bucket
        """, (fmt, cutoff))
        points = [{
            "date": r["bucket"],
            "input": int(r["input"] or 0),
            "output": int(r["output"] or 0),
            "cache_read": int(r["cache_read"] or 0),
            "reasoning": int(r["reasoning"] or 0),
            "cost_usd": float(r["cost"] or 0.0),
        } for r in cur.fetchall()]
    return {
        "granularity": granularity,
        "window": window,
        "source": source,
        "points": points,
    }


def handle_stats_timeseries(handler, parsed):
    return _serve(handler, parsed, 'timeseries', _build_timeseries)


# ── /api/stats/response-time ──────────────────────────────────────────────

_RT_BUCKETS = [
    ('0-1s', 0.0, 1.0),
    ('1-3s', 1.0, 3.0),
    ('3-10s', 3.0, 10.0),
    ('10-30s', 10.0, 30.0),
    ('30s+', 30.0, float('inf')),
]


def _rt_bucket_index(delta):
    for i, (_label, lo, hi) in enumerate(_RT_BUCKETS):
        if lo <= delta < hi:
            return i
    return len(_RT_BUCKETS) - 1  # fallback to last bucket (should not hit)


def _build_response_time(conn, qs):
    window = _qs_int(qs, 'window', 30, min_val=1, max_val=365)
    if conn is None:
        return {"window": window, "total": 0, "buckets": [
            {"label": lbl, "count": 0, "min_ms": None, "max_ms": None}
            for (lbl, _, _) in _RT_BUCKETS
        ]}
    cutoff = time.time() - window * 86400
    cur = conn.cursor()
    cur.execute("""
        SELECT session_id, role, timestamp
        FROM messages
        WHERE timestamp > ? AND role IN ('user', 'assistant')
        ORDER BY session_id, timestamp
    """, (cutoff,))
    counts = [0] * len(_RT_BUCKETS)
    mins = [float('inf')] * len(_RT_BUCKETS)
    maxs = [0.0] * len(_RT_BUCKETS)
    total = 0
    current_sid = None
    last_user_ts = None
    for row in cur:
        if row["session_id"] != current_sid:
            current_sid = row["session_id"]
            last_user_ts = None
        if row["role"] == 'user':
            last_user_ts = row["timestamp"]
        elif row["role"] == 'assistant' and last_user_ts is not None:
            delta = row["timestamp"] - last_user_ts
            last_user_ts = None
            if delta <= 0 or delta > RESPONSE_TIME_FILTER_SECONDS:
                continue
            idx = _rt_bucket_index(delta)
            counts[idx] += 1
            if delta < mins[idx]:
                mins[idx] = delta
            if delta > maxs[idx]:
                maxs[idx] = delta
            total += 1
    buckets = []
    for i, (label, _lo, _hi) in enumerate(_RT_BUCKETS):
        if counts[i]:
            buckets.append({
                "label": label,
                "count": counts[i],
                "min_ms": int(mins[i] * 1000),
                "max_ms": int(maxs[i] * 1000),
            })
        else:
            buckets.append({"label": label, "count": 0, "min_ms": None, "max_ms": None})
    return {"window": window, "total": total, "buckets": buckets}


def handle_stats_response_time(handler, parsed):
    return _serve(handler, parsed, 'response-time', _build_response_time)


# ── /api/stats/heatmap ─────────────────────────────────────────────────────

def _build_heatmap(conn, qs):
    window = _qs_int(qs, 'window', 7, min_val=1, max_val=90)
    cells = [[0] * 24 for _ in range(7)]
    if conn is None:
        return {"window": window, "cells": cells}
    cutoff = time.time() - window * 86400
    cur = conn.cursor()
    cur.execute("""
        SELECT CAST(strftime('%w', timestamp, 'unixepoch') AS INTEGER) AS wd,
               CAST(strftime('%H', timestamp, 'unixepoch') AS INTEGER) AS hr,
               COUNT(*) AS msgs
        FROM messages
        WHERE timestamp > ?
        GROUP BY wd, hr
    """, (cutoff,))
    for row in cur:
        wd = row["wd"]
        hr = row["hr"]
        if wd is None or hr is None:
            continue
        if 0 <= wd < 7 and 0 <= hr < 24:
            cells[wd][hr] = int(row["msgs"] or 0)
    return {"window": window, "cells": cells}


def handle_stats_heatmap(handler, parsed):
    return _serve(handler, parsed, 'heatmap', _build_heatmap)


# ── /api/stats/models ─────────────────────────────────────────────────────

def _build_models(conn, qs):
    window = _qs_int(qs, 'window', 30, min_val=1, max_val=365)
    if conn is None:
        return {"window": window, "total_tokens": 0, "models": []}
    cutoff = time.time() - window * 86400
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(model, 'unknown') AS model,
               COALESCE(SUM(input_tokens), 0) AS input,
               COALESCE(SUM(output_tokens), 0) AS output,
               COALESCE(SUM(message_count), 0) AS msgs,
               COALESCE(SUM(estimated_cost_usd), 0.0) AS cost
        FROM sessions
        WHERE started_at > ?
        GROUP BY model
        ORDER BY (COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0)) DESC
    """, (cutoff,))
    models = [{
        "model": r["model"],
        "input_tokens": int(r["input"] or 0),
        "output_tokens": int(r["output"] or 0),
        "message_count": int(r["msgs"] or 0),
        "estimated_cost_usd": float(r["cost"] or 0.0),
    } for r in cur.fetchall()]
    total = sum(m["input_tokens"] + m["output_tokens"] for m in models)
    for m in models:
        m["pct"] = round((m["input_tokens"] + m["output_tokens"]) / total * 100, 2) if total > 0 else 0.0
    return {"window": window, "total_tokens": total, "models": models}


def handle_stats_models(handler, parsed):
    return _serve(handler, parsed, 'models', _build_models)
