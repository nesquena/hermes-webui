"""Neo Agents Activity — SSE endpoint for pixel-agents visualization.

Provides real-time agent presence via polling state.db every few seconds.
Emits ServerMessage events that the pixel-agents React bundle understands.
"""

import json
import time
from pathlib import Path

SPRITES_BUNDLE_PATH = Path(__file__).parent.parent / "static" / "agents-app" / "sprites-bundle.json"
POLL_INTERVAL = 5  # seconds between state.db polls

_sprites_cache = None


def _load_sprites():
    global _sprites_cache
    if _sprites_cache is None and SPRITES_BUNDLE_PATH.exists():
        _sprites_cache = json.loads(SPRITES_BUNDLE_PATH.read_text())
    return _sprites_cache


def _get_state_db_path():
    import os
    hermes_home = os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))
    return str(Path(hermes_home) / "state.db")


def get_active_sessions():
    """Read active sessions from both WebUI (in-memory STREAMS) and state.db.

    Returns a list of dicts with keys: id, title, source, parent_session_id.
    """
    results = []

    # 1. WebUI active streams (in-memory)
    try:
        from api.config import STREAMS, STREAMS_LOCK, STREAM_LIVE_TOOL_CALLS
        with STREAMS_LOCK:
            active_stream_ids = set(STREAMS.keys())
        for stream_id in active_stream_ids:
            results.append({
                "id": stream_id,
                "title": None,
                "source": "webui",
                "parent_session_id": None,
            })
    except Exception:
        pass

    # 2. CLI/Telegram sessions from state.db (started in last hour, not ended)
    import sqlite3
    db_path = _get_state_db_path()
    if Path(db_path).exists():
        try:
            conn = sqlite3.connect(db_path, timeout=1)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            cutoff = time.time() - 3600
            rows = conn.execute(
                "SELECT id, title, source, parent_session_id, started_at "
                "FROM sessions WHERE ended_at IS NULL AND started_at > ? "
                "ORDER BY started_at",
                (cutoff,)
            ).fetchall()
            conn.close()
            for r in rows:
                results.append(dict(r))
        except Exception:
            pass

    return results


def generate_init_events():
    """Yield SSE-formatted init events for a new client connection."""
    sprites = _load_sprites()

    # Settings
    yield _sse_data({"type": "settingsLoaded", "soundEnabled": False})

    # Sprites
    if sprites:
        chars = sprites.get("characters")
        if chars and chars.get("characters"):
            yield _sse_data({"type": "characterSpritesLoaded", "characters": chars["characters"]})
        walls = sprites.get("walls")
        if walls and walls.get("sprites"):
            yield _sse_data({"type": "wallTilesLoaded", "sprites": walls["sprites"]})
        floors = sprites.get("floors")
        if floors and floors.get("sprites"):
            yield _sse_data({"type": "floorTilesLoaded", "sprites": floors["sprites"]})
        furniture = sprites.get("furniture")
        if furniture:
            yield _sse_data({
                "type": "furnitureAssetsLoaded",
                "catalog": furniture.get("catalog", []),
                "sprites": furniture.get("sprites", {}),
            })

    # Existing agents (non-child sessions = top-level agents)
    sessions = get_active_sessions()
    agent_ids, folder_names, session_map = _build_agent_list(sessions)

    # If no active sessions, show a demo agent so the panel isn't empty
    # Use high ID (9999) to avoid conflicts with real agents in stream_activity
    if not agent_ids:
        agent_ids = [9999]
        folder_names = {9999: "Neo"}

    yield _sse_data({
        "type": "existingAgents",
        "agents": agent_ids,
        "folderNames": folder_names,
        "agentMeta": {},
    })

    # Layout (must come after existingAgents)
    layout = sprites.get("layout") if sprites else None
    version = 1 if layout else 0
    yield _sse_data({"type": "layoutLoaded", "layout": layout, "version": version})


def stream_activity(write_fn):
    """Poll state.db and emit agentCreated/agentClosed events in real-time.

    Args:
        write_fn: callable that takes bytes and writes to the SSE response.
                  Should raise on client disconnect.
    """
    known_sessions = {}  # session_id -> agent_id
    next_id = [1]
    demo_active = [True]  # tracks whether demo agent 9999 is showing

    # Seed known_sessions from current state so we don't re-emit existingAgents
    sessions = get_active_sessions()
    for s in sessions:
        if not s.get("parent_session_id"):
            known_sessions[s["id"]] = next_id[0]
            next_id[0] += 1
    if known_sessions:
        demo_active[0] = False

    def _poll():
        sessions = get_active_sessions()
        current_ids = set()
        events = []

        for s in sessions:
            sid = s["id"]
            current_ids.add(sid)
            if sid not in known_sessions:
                # New session appeared — remove demo if active
                if demo_active[0]:
                    events.append({"type": "agentClosed", "id": 9999})
                    demo_active[0] = False

                aid = next_id[0]
                next_id[0] += 1
                known_sessions[sid] = aid
                name = s.get("title") or s.get("source") or "Neo"
                if not s.get("parent_session_id"):
                    events.append({"type": "agentCreated", "id": aid, "folderName": name})
                else:
                    parent_sid = s["parent_session_id"]
                    parent_aid = known_sessions.get(parent_sid)
                    if parent_aid:
                        events.append({
                            "type": "agentToolStart",
                            "id": parent_aid,
                            "toolId": f"subtask-{sid[:8]}",
                            "status": f"Subtask: {name}",
                        })

        # Check for closed sessions
        closed = set(known_sessions.keys()) - current_ids
        for sid in closed:
            aid = known_sessions.pop(sid)
            events.append({"type": "agentClosed", "id": aid})

        # If all real agents gone, bring back demo
        if not known_sessions and not demo_active[0]:
            events.append({"type": "agentCreated", "id": 9999, "folderName": "Neo"})
            demo_active[0] = True

        return events

    # Poll loop
    while True:
        time.sleep(POLL_INTERVAL)
        try:
            events = _poll()
            for ev in events:
                write_fn(_sse_data(ev))
            # Heartbeat even if no events (keeps connection alive)
            if not events:
                write_fn(SSE_HEARTBEAT)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            break


def _build_agent_list(sessions):
    """Build agent ID list from sessions."""
    agent_ids = []
    folder_names = {}
    session_map = {}
    next_id = 1
    for s in sessions:
        if not s.get("parent_session_id"):
            agent_ids.append(next_id)
            folder_names[next_id] = s.get("title") or s.get("source") or "Neo"
            session_map[s["id"]] = next_id
            next_id += 1
    return agent_ids, folder_names, session_map


def _sse_data(data: dict) -> bytes:
    return f"data: {json.dumps(data)}\n\n".encode()


SSE_HEARTBEAT = b": heartbeat\n\n"
