"""
Hermes Web UI -- Agent-activity and Surface aggregation API.

Produces per-surface activity snapshots and Surface Dashboard cards from
state.db + the webui-process SESSIONS LRU cache.

IMPORTANT: This module intentionally does NOT expose agent runtime state
(current_tool / pending_count / is_running_tool / ...). The webui process
cannot reliably observe such state through state.db or SESSIONS — those
fields live inside the hermes-agent process. Any future surface_runtime
signals require a dedicated agent<->webui IPC channel and belong in a
separate change. See design.md D9 for the rationale.

Endpoints served (registered in api/routes.py handle_get):
- GET /api/agent-activity
- GET /api/agent-activity/stream   (SSE)
- GET /api/surfaces                (snapshot + optional ?source=X&expand=1)

All handlers rely on GatewayWatcher.subscribe() for change notifications;
no additional polling thread is introduced.

See openspec/changes/add-dashboards-and-pixel-office/design.md (D3, D4, D9, D10)
and specs/agent-activity-api, surface-dashboard for the contracts.
"""
import json
import logging
import queue
import threading
import time
from urllib.parse import parse_qs

import api.stats as _stats  # noqa: F401 — referenced via _stats.NAME() so test monkeypatch works
from api.stats import (
    _open_state_db_readonly,
    _require_auth,
    _j_cached,
    _qs,
    _qs_bool,
    _qs_str,
)


def _resolve_db_path():
    """Indirect lookup so test monkeypatch on api.stats._get_profile_db_path takes effect."""
    return _stats._get_profile_db_path()

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 2
SNAPSHOT_WINDOW_24H = 86400
SSE_QUEUE_TIMEOUT = 5   # seconds; shorter than heartbeat so we can also rebuild for webui-only changes
SSE_HEARTBEAT_SECONDS = 30
EXPAND_LIMIT = 5


# ── 3.2: pure state derivation ────────────────────────────────────────────

def derive_state(last_msg_ts, now_ts):
    """Map time-since-last-message to surface state.

    Rules (design D4, agent-activity-api spec "状态推导规则"):
      < 60s               -> 'working'
      60s  ..  300s       -> 'waiting'
      300s ..  86400s     -> 'idle'
      > 86400s or None    -> 'offline'

    Intentionally does NOT take a "has_active_session" argument — webui
    cannot reliably observe active sessions for non-webui surfaces.
    """
    if last_msg_ts is None:
        return 'offline'
    age = now_ts - last_msg_ts
    if age < 0:
        # Clock skew or future timestamp — treat as fresh activity.
        return 'working'
    if age < 60:
        return 'working'
    if age < 300:
        return 'waiting'
    if age < 86400:
        return 'idle'
    return 'offline'


# ── Cache (separate namespace from stats so TTL differs) ───────────────────

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
    with _CACHE_LOCK:
        _CACHE.clear()


# ── Internal helpers ───────────────────────────────────────────────────────

def _active_profile_name():
    try:
        from api.profiles import get_active_profile_name
        return get_active_profile_name()
    except Exception:
        return 'default'


def _webui_session_count():
    try:
        from api.config import SESSIONS
        return len(SESSIONS)
    except Exception:
        return 0


def _redact_title(text):
    try:
        from api.helpers import _redact_text
        return _redact_text(text) if isinstance(text, str) else text
    except Exception:
        return text


# ── 3.1: build_surface_snapshot ────────────────────────────────────────────

def build_surface_snapshot(db_path):
    """Build the /api/agent-activity snapshot.

    Performs `SELECT DISTINCT source FROM sessions` and MAX/SUM aggregates
    every call — surface enumeration is dynamic per active profile
    (design D3). Callers should cache externally (_cache_get/_cache_set).

    Returns:
        {
          "surfaces": [...],
          "generated_at": <unix-seconds>,
          "profile": <active-profile-name>,
        }
    """
    now = time.time()
    generated = {
        "surfaces": [],
        "generated_at": now,
        "profile": _active_profile_name(),
    }
    if db_path is None:
        return generated
    conn = _open_state_db_readonly(db_path)
    try:
        cur = conn.cursor()
        cutoff_24h = now - SNAPSHOT_WINDOW_24H
        # Per-surface aggregate: last_active_ts, messages-in-24h, tokens-in-24h.
        # LEFT JOIN so surfaces with sessions but zero messages still appear.
        cur.execute(
            """
            SELECT s.source AS source,
                   MAX(m.timestamp) AS last_active_ts,
                   SUM(CASE WHEN m.timestamp > ? THEN 1 ELSE 0 END) AS msgs_24h,
                   SUM(CASE WHEN m.timestamp > ? THEN COALESCE(m.token_count, 0) ELSE 0 END) AS tokens_24h
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            GROUP BY s.source
            """,
            (cutoff_24h, cutoff_24h),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    webui_lru = _webui_session_count()
    surfaces = []
    for row in rows:
        source = row["source"]
        if not source:
            continue
        last_ts = row["last_active_ts"]
        entry = {
            "source": source,
            "state": derive_state(last_ts, now),
            "last_active_ts": float(last_ts) if last_ts is not None else None,
            "message_count_24h": int(row["msgs_24h"] or 0),
            "tokens_24h": int(row["tokens_24h"] or 0),
        }
        # active_webui_sessions is webui-only (spec agent-activity-api)
        if source == 'webui':
            entry["active_webui_sessions"] = webui_lru
        surfaces.append(entry)

    surfaces.sort(key=lambda s: (s["last_active_ts"] or 0), reverse=True)
    generated["surfaces"] = surfaces
    return generated


# Alias — surfaces dashboard uses the same card data. The ?expand=1 query
# is a separate code path (build_surface_expand).
def build_surfaces_cards(db_path):
    return build_surface_snapshot(db_path)


# ── 4.2: build_surface_expand ──────────────────────────────────────────────

def build_surface_expand(db_path, source):
    """Return the 5 most recent sessions for the given source.

    Returns {"sessions": []} for unknown source (spec: 'Scenario: 未知
    source 的展开请求'). Titles are redacted via _redact_text.
    """
    if db_path is None or not source:
        return {"sessions": []}
    conn = _open_state_db_readonly(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT s.id AS session_id,
                   s.title AS title,
                   s.model AS model,
                   s.message_count AS message_count,
                   (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id) AS last_activity
            FROM sessions s
            WHERE s.source = ?
            ORDER BY last_activity DESC, s.started_at DESC
            LIMIT {EXPAND_LIMIT}
            """,
            (source,),
        )
        sessions = [
            {
                "session_id": r["session_id"],
                "title": _redact_title(r["title"]) if r["title"] else None,
                "model": r["model"],
                "message_count": int(r["message_count"] or 0),
                "last_activity": float(r["last_activity"]) if r["last_activity"] is not None else None,
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.close()
    return {"sessions": sessions}


# ── 3.4: /api/agent-activity handler ───────────────────────────────────────

def handle_agent_activity(handler, parsed):
    if not _require_auth(handler):
        return True
    db_path = _resolve_db_path()
    qs = _qs(parsed)
    refresh = _qs_bool(qs, 'refresh')
    cache_key = ('agent-activity', str(db_path) if db_path else None)
    if not refresh:
        cached = _cache_get(cache_key, CACHE_TTL_SECONDS)
        if cached is not None:
            _j_cached(handler, cached, True)
            return True
    snap = build_surface_snapshot(db_path)
    _cache_set(cache_key, snap)
    _j_cached(handler, snap, False)
    return True


# ── 4.2: /api/surfaces handler ─────────────────────────────────────────────

def handle_surfaces(handler, parsed):
    if not _require_auth(handler):
        return True
    db_path = _resolve_db_path()
    qs = _qs(parsed)
    refresh = _qs_bool(qs, 'refresh')
    expand = _qs_bool(qs, 'expand')
    source = _qs_str(qs, 'source', '')

    if expand:
        # Scoped cache per (db_path, source).
        cache_key = ('surfaces-expand', str(db_path) if db_path else None, source)
        if not refresh:
            cached = _cache_get(cache_key, CACHE_TTL_SECONDS)
            if cached is not None:
                _j_cached(handler, cached, True)
                return True
        result = build_surface_expand(db_path, source)
        _cache_set(cache_key, result)
        _j_cached(handler, result, False)
        return True

    cache_key = ('surfaces', str(db_path) if db_path else None)
    if not refresh:
        cached = _cache_get(cache_key, CACHE_TTL_SECONDS)
        if cached is not None:
            _j_cached(handler, cached, True)
            return True
    snap = build_surfaces_cards(db_path)
    _cache_set(cache_key, snap)
    _j_cached(handler, snap, False)
    return True


# ── 3.6: /api/agent-activity/stream (SSE) ──────────────────────────────────

def _sse_write(handler, event, payload):
    body = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode('utf-8')
    handler.wfile.write(body)
    handler.wfile.flush()


def _snapshot_signature(snap):
    """Deterministic tuple-of-tuples key for change detection.

    We ignore `generated_at` (always changes) and
    `active_webui_sessions` jitters only when webui LRU size changes.
    """
    return tuple(
        (s.get("source"), s.get("state"), s.get("last_active_ts"),
         s.get("message_count_24h"), s.get("tokens_24h"),
         s.get("active_webui_sessions"))
        for s in snap.get("surfaces", [])
    )


def _compute_delta(prev_snap, cur_snap):
    """Return a snapshot-shaped dict with only the changed surface entries.

    New surfaces, disappeared surfaces, and mutated ones are included.
    """
    prev_by_src = {s["source"]: s for s in (prev_snap or {}).get("surfaces", [])}
    cur_by_src = {s["source"]: s for s in cur_snap.get("surfaces", [])}
    changed = []
    for src, cur_entry in cur_by_src.items():
        prev_entry = prev_by_src.get(src)
        if prev_entry is None or _snap_fingerprint(prev_entry) != _snap_fingerprint(cur_entry):
            changed.append(cur_entry)
    removed = [{"source": src, "state": "offline"} for src in prev_by_src if src not in cur_by_src]
    return {
        "surfaces": changed + removed,
        "generated_at": cur_snap.get("generated_at"),
        "profile": cur_snap.get("profile"),
    }


def _snap_fingerprint(entry):
    return (entry.get("state"), entry.get("last_active_ts"),
            entry.get("message_count_24h"), entry.get("tokens_24h"),
            entry.get("active_webui_sessions"))


def handle_agent_activity_stream(handler, parsed):
    if not _require_auth(handler):
        return True

    from api.gateway_watcher import get_watcher
    watcher = get_watcher()
    if watcher is None:
        from api.helpers import bad
        return bad(handler, "watcher not started", status=503)

    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream; charset=utf-8')
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('X-Accel-Buffering', 'no')
    handler.send_header('Connection', 'keep-alive')
    handler.end_headers()

    q = watcher.subscribe()
    initial_db_path = _resolve_db_path()
    initial_profile = _active_profile_name()
    try:
        snap = build_surface_snapshot(initial_db_path)
        _sse_write(handler, 'snapshot', snap)
        prev_snap = snap
        prev_sig = _snapshot_signature(snap)
        last_sent = time.time()

        while True:
            # Detect profile switch before blocking — close the connection so
            # the client reconnects under the new profile (spec: 'Profile
            # 切换断开并重新计算枚举').
            if _active_profile_name() != initial_profile or _resolve_db_path() != initial_db_path:
                _sse_write(handler, 'profile_changed', {'reason': 'profile switched'})
                break

            try:
                event = q.get(timeout=SSE_QUEUE_TIMEOUT)
            except queue.Empty:
                event = 'tick'
            if event is None:
                # Watcher is shutting down.
                break

            cur_snap = build_surface_snapshot(_resolve_db_path())
            cur_sig = _snapshot_signature(cur_snap)
            if cur_sig != prev_sig:
                delta = _compute_delta(prev_snap, cur_snap)
                _sse_write(handler, 'delta', delta)
                prev_snap = cur_snap
                prev_sig = cur_sig
                last_sent = time.time()
            elif time.time() - last_sent >= SSE_HEARTBEAT_SECONDS:
                _sse_write(handler, 'heartbeat', {'ts': int(time.time())})
                last_sent = time.time()
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception:
        logger.debug("agent-activity SSE stream error", exc_info=True)
    finally:
        watcher.unsubscribe(q)
    return True
