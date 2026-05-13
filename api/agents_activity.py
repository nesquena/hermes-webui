"""Neo Agents Activity — SSE endpoint for pixel-agents visualization."""

import json
import time
from pathlib import Path

SPRITES_BUNDLE_PATH = Path(__file__).parent.parent / "static" / "agents-app" / "sprites-bundle.json"

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
    """Read active (non-ended) sessions from state.db."""
    import sqlite3
    db_path = _get_state_db_path()
    if not Path(db_path).exists():
        return []
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT session_id, project, parent_session_id, started_at "
            "FROM sessions WHERE ended_at IS NULL ORDER BY started_at"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


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
    agent_ids = []
    folder_names = {}
    next_id = 1
    for s in sessions:
        if not s.get("parent_session_id"):
            agent_ids.append(next_id)
            folder_names[next_id] = s.get("project") or "Neo"
            next_id += 1

    # If no active sessions, show a demo agent so the panel isn't empty
    if not agent_ids:
        agent_ids = [1]
        folder_names = {1: "Neo"}

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


def _sse_data(data: dict) -> bytes:
    return f"data: {json.dumps(data)}\n\n".encode()


SSE_HEARTBEAT = b": heartbeat\n\n"
