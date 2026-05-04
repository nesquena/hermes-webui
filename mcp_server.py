#!/usr/bin/env python3
"""
Hermes WebUI MCP Server — exposes project and session management
as MCP tools for any MCP-compatible agent.

Uses the same JSON state files as the Hermes WebUI.
No HTTP, no auth — filesystem only. Works even when the webapp is off.

    pip install mcp       # one-time setup
    python3 mcp_server.py # start via stdio

MCP config for Hermes Agent (add to config.yaml):
    mcp_servers:
      hermes-webui:
        command: "python3"
        args: ["/path/to/hermes-webui/mcp_server.py"]
"""

import json
import os
import re
import time
import uuid
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ── State paths (same discovery as api/config.py) ──────────────────────────
HOME = Path.home()
STATE_DIR = Path(
    os.getenv("HERMES_WEBUI_STATE_DIR", str(HOME / ".hermes" / "webui"))
).expanduser()
PROJECTS_FILE = STATE_DIR / "projects.json"
SESSION_DIR = STATE_DIR / "sessions"
SESSION_INDEX = SESSION_DIR / "_index.json"

server = Server("hermes-webui")

# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _load_projects() -> list:
    """Load project list from disk. Returns list of project dicts."""
    if not PROJECTS_FILE.exists():
        return []
    try:
        return json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_projects(projects: list) -> None:
    """Atomically write project list to disk."""
    tmp = PROJECTS_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps(projects, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, PROJECTS_FILE)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _load_index() -> list:
    """Load session index. Falls back to empty list."""
    if not SESSION_INDEX.exists():
        return []
    try:
        return json.loads(SESSION_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return []


def _validate_session_id(sid: str) -> bool:
    """Reject obviously invalid session IDs (path traversal guard)."""
    return bool(sid and all(c in "0123456789abcdefghijklmnopqrstuvwxyz_-" for c in sid))


def _read_session_json(sid: str) -> dict | None:
    """Read a session JSON file. Returns None on any error."""
    if not _validate_session_id(sid):
        return None
    path = SESSION_DIR / f"{sid}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_session_json(sid: str, data: dict) -> None:
    """Atomically write a session JSON file."""
    if not _validate_session_id(sid):
        raise ValueError(f"Invalid session ID: {sid}")
    path = SESSION_DIR / f"{sid}.json"
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _patch_index_entry(sid: str, updates: dict) -> None:
    """Update project_id and title in the session index so list_sessions stays current."""
    index = _load_index()
    for s in index:
        if s.get("session_id") == sid:
            for k, v in updates.items():
                s[k] = v
            tmp = SESSION_INDEX.with_suffix(".tmp")
            try:
                tmp.write_text(
                    json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                os.replace(tmp, SESSION_INDEX)
            except Exception:
                tmp.unlink(missing_ok=True)
            return
    # Entry not in index — nothing to patch (will appear on next index rebuild)


def _session_compact(row: dict) -> dict:
    """Extract the fields the agent cares about from an index row."""
    return {
        "session_id": row.get("session_id"),
        "title": row.get("title"),
        "project_id": row.get("project_id"),
        "workspace": row.get("workspace"),
        "model": row.get("model"),
        "message_count": row.get("message_count", 0),
        "source_tag": row.get("source_tag"),
        "is_cli_session": row.get("is_cli_session", False),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  Tools
# ═══════════════════════════════════════════════════════════════════════════

async def handle_list_projects(_arguments: dict) -> list[TextContent]:
    """List all projects with session counts."""
    projects = _load_projects()
    index = _load_index()

    # Count sessions per project
    counts: dict[str, int] = {}
    for s in index:
        pid = s.get("project_id")
        if pid:
            counts[pid] = counts.get(pid, 0) + 1

    result = []
    for p in projects:
        entry = dict(p)
        entry["session_count"] = counts.get(p["project_id"], 0)
        result.append(entry)

    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


async def handle_create_project(arguments: dict) -> list[TextContent]:
    """Create a new project."""
    name = arguments.get("name", "").strip()[:128]
    if not name:
        return [TextContent(type="text", text=json.dumps({"error": "name is required"}, ensure_ascii=False))]

    color = arguments.get("color")
    if color and not re.match(r"^#[0-9a-fA-F]{3,8}$", color):
        return [TextContent(type="text", text=json.dumps({"error": "Invalid color format (use #RGB, #RRGGBB, or #RRGGBBAA)"}, ensure_ascii=False))]

    projects = _load_projects()

    # Reject duplicate names
    if any(p.get("name", "").lower() == name.lower() for p in projects):
        return [TextContent(type="text", text=json.dumps({"error": f"Project '{name}' already exists"}, ensure_ascii=False))]

    proj = {
        "project_id": uuid.uuid4().hex[:12],
        "name": name,
        "color": color,
        "created_at": time.time(),
    }
    projects.append(proj)
    _save_projects(projects)

    proj["session_count"] = 0
    return [TextContent(type="text", text=json.dumps(proj, ensure_ascii=False, indent=2))]


async def handle_rename_project(arguments: dict) -> list[TextContent]:
    """Rename a project and optionally change its color."""
    project_id = arguments.get("project_id")
    name = arguments.get("name", "").strip()[:128]

    if not project_id or not name:
        return [TextContent(type="text", text=json.dumps({"error": "project_id and name are required"}, ensure_ascii=False))]

    color = arguments.get("color")
    if color and not re.match(r"^#[0-9a-fA-F]{3,8}$", color):
        return [TextContent(type="text", text=json.dumps({"error": "Invalid color format"}, ensure_ascii=False))]

    projects = _load_projects()
    proj = next((p for p in projects if p["project_id"] == project_id), None)
    if not proj:
        return [TextContent(type="text", text=json.dumps({"error": "Project not found"}, ensure_ascii=False))]

    proj["name"] = name
    if color is not None:
        proj["color"] = color
    _save_projects(projects)

    return [TextContent(type="text", text=json.dumps(proj, ensure_ascii=False, indent=2))]


async def handle_delete_project(arguments: dict) -> list[TextContent]:
    """Delete a project and unassign its sessions."""
    project_id = arguments.get("project_id")
    if not project_id:
        return [TextContent(type="text", text=json.dumps({"error": "project_id is required"}, ensure_ascii=False))]

    projects = _load_projects()
    proj = next((p for p in projects if p["project_id"] == project_id), None)
    if not proj:
        return [TextContent(type="text", text=json.dumps({"error": "Project not found"}, ensure_ascii=False))]

    # Remove project
    projects = [p for p in projects if p["project_id"] != project_id]
    _save_projects(projects)

    # Unassign sessions from this project (read JSON files directly — the
    # index may be stale since move_session doesn't update it)
    unassigned = 0
    if SESSION_DIR.exists():
        for p in SESSION_DIR.glob("*.json"):
            if p.name.startswith("_"):
                continue
            try:
                session_data = json.loads(p.read_text(encoding="utf-8"))
                if session_data.get("project_id") == project_id:
                    session_data["project_id"] = None
                    _write_session_json(p.stem, session_data)
                    _patch_index_entry(p.stem, {"project_id": None})
                    unassigned += 1
            except Exception:
                pass

    return [TextContent(type="text", text=json.dumps({
        "ok": True,
        "deleted": proj["name"],
        "unassigned_sessions": unassigned,
    }, ensure_ascii=False))]


async def handle_move_session(arguments: dict) -> list[TextContent]:
    """Assign a session to a project (or unassign with null project_id)."""
    session_id = arguments.get("session_id")
    project_id = arguments.get("project_id")  # None / null means unassign

    if not session_id:
        return [TextContent(type="text", text=json.dumps({"error": "session_id is required"}, ensure_ascii=False))]

    # If project_id is explicitly provided, verify it exists
    if project_id is not None:
        projects = _load_projects()
        if not any(p["project_id"] == project_id for p in projects):
            return [TextContent(type="text", text=json.dumps({"error": "Project not found"}, ensure_ascii=False))]

    session_data = _read_session_json(session_id)
    if session_data is None:
        return [TextContent(type="text", text=json.dumps({"error": "Session not found"}, ensure_ascii=False))]

    session_data["project_id"] = project_id
    _write_session_json(session_id, session_data)
    _patch_index_entry(session_id, {"project_id": project_id})

    return [TextContent(type="text", text=json.dumps({
        "ok": True,
        "session_id": session_id,
        "project_id": project_id,
        "title": session_data.get("title"),
    }, ensure_ascii=False))]


async def handle_list_sessions(arguments: dict) -> list[TextContent]:
    """List sessions, optionally filtered by project or unassigned."""
    project_id = arguments.get("project_id")
    unassigned = arguments.get("unassigned", False)
    limit = max(1, min(500, arguments.get("limit", 50)))

    index = _load_index()
    sessions = [_session_compact(s) for s in index if s.get("session_id")]

    # Filter
    if unassigned:
        sessions = [s for s in sessions if not s["project_id"]]
    elif project_id:
        sessions = [s for s in sessions if s["project_id"] == project_id]

    sessions = sessions[:limit]

    return [TextContent(type="text", text=json.dumps(sessions, ensure_ascii=False, indent=2))]


# ═══════════════════════════════════════════════════════════════════════════
#  MCP Server wiring
# ═══════════════════════════════════════════════════════════════════════════

TOOLS = [
    Tool(
        name="list_projects",
        description="List all session projects with their IDs, names, colors, and session counts.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="create_project",
        description="Create a new project for organizing sessions.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name (max 128 chars)"},
                "color": {"type": "string", "description": "Optional hex color (#RGB, #RRGGBB, or #RRGGBBAA)"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="rename_project",
        description="Rename a project and optionally change its color.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "12-char project ID"},
                "name": {"type": "string", "description": "New name (max 128 chars)"},
                "color": {"type": "string", "description": "Optional new hex color"},
            },
            "required": ["project_id", "name"],
        },
    ),
    Tool(
        name="delete_project",
        description="Delete a project and unassign all its sessions.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "12-char project ID to delete"},
            },
            "required": ["project_id"],
        },
    ),
    Tool(
        name="move_session",
        description="Assign a session to a project. Pass project_id=null to unassign.",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID"},
                "project_id": {"type": ["string", "null"], "description": "Project ID (or null to unassign)"},
            },
            "required": ["session_id", "project_id"],
        },
    ),
    Tool(
        name="list_sessions",
        description="List sessions, optionally filtered by project or unassigned status.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Filter sessions by project ID"},
                "unassigned": {"type": "boolean", "description": "Show only sessions with no project"},
                "limit": {"type": "integer", "description": "Max results (default: 50, max: 500)"},
            },
            "required": [],
        },
    ),
]

HANDLERS = {
    "list_projects": handle_list_projects,
    "create_project": handle_create_project,
    "rename_project": handle_rename_project,
    "delete_project": handle_delete_project,
    "move_session": handle_move_session,
    "list_sessions": handle_list_sessions,
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    handler = HANDLERS.get(name)
    if not handler:
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False))]
    return await handler(arguments)


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
