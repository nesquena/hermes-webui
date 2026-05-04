#!/usr/bin/env python3
"""
Hermes WebUI MCP Server — exposes project and session management
as MCP tools for any MCP-compatible agent.

Reads state from JSON files (fast, no auth needed).
Mutations (rename, move) go through the authenticated HTTP API
to keep the webui server's in-memory SESSIONS cache in sync.

    pip install mcp       # one-time setup
    python3 mcp_server.py # start via stdio

MCP config for Hermes Agent (add to config.yaml):
    mcp_servers:
      hermes-webui:
        command: /path/to/venv/bin/python3
        args: [/path/to/hermes-webui/mcp_server.py]
        env:
          HERMES_WEBUI_PASSWORD: your_password
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

# ── API auth state ─────────────────────────────────────────────────────────
WEBUI_URL = "http://127.0.0.1:8788"
_auth_cookie: str | None = None
_auth_expires: float = 0  # unix timestamp after which we re-auth

server = Server("hermes-webui")

# ═══════════════════════════════════════════════════════════════════════════
#  Helpers — filesystem (read-only + project CRUD)
# ═══════════════════════════════════════════════════════════════════════════

def _load_projects() -> list:
    if not PROJECTS_FILE.exists():
        return []
    try:
        return json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_projects(projects: list) -> None:
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
    if not SESSION_INDEX.exists():
        return []
    try:
        return json.loads(SESSION_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return []


def _validate_session_id(sid: str) -> bool:
    return bool(sid and all(c in "0123456789abcdefghijklmnopqrstuvwxyz_-" for c in sid))


def _session_compact(row: dict) -> dict:
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
#  Helpers — HTTP API (for mutations that need cache sync)
# ═══════════════════════════════════════════════════════════════════════════

def _api_password() -> str | None:
    """Return the webui password, or None if auth is disabled."""
    pw = os.environ.get("HERMES_WEBUI_PASSWORD", "").strip()
    if not pw:
        # Also check settings.json fallback
        settings_file = STATE_DIR / "settings.json"
        if settings_file.exists():
            try:
                settings = json.loads(settings_file.read_text(encoding="utf-8"))
                pw = (settings.get("password_hash") or "").strip()
            except Exception:
                pass
    return pw or None


def _api_auth() -> str | None:
    """Authenticate and return cookie value, or None if auth disabled/fails."""
    global _auth_cookie, _auth_expires

    pw = _api_password()
    if not pw:
        return None  # auth not enabled — API calls will fail anyway

    # Reuse cookie if still valid (25 days — server issues 30-day cookies)
    if _auth_cookie and time.time() < _auth_expires:
        return _auth_cookie

    import urllib.request

    try:
        req = urllib.request.Request(
            f"{WEBUI_URL}/api/auth/login",
            data=json.dumps({"password": pw}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        cookie = resp.headers.get("Set-Cookie", "")
        if cookie:
            _auth_cookie = cookie.split(";")[0]  # "hermes_session=VALUE; ..."
            _auth_expires = time.time() + 25 * 86400  # 25 days
            return _auth_cookie
    except Exception:
        _auth_cookie = None
    return None


def _api_post(endpoint: str, body: dict) -> dict:
    """POST to webui API with auth cookie. Returns parsed JSON response."""
    import urllib.request
    import urllib.error

    cookie = _api_auth()
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie

    try:
        req = urllib.request.Request(
            f"{WEBUI_URL}{endpoint}",
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = json.loads(e.read())
        return {"error": f"API {e.code}: {err_body.get('error', 'unknown')}"}
    except Exception as e:
        return {"error": f"API unreachable: {e}"}


# ═══════════════════════════════════════════════════════════════════════════
#  Tool handlers — read-only (filesystem)
# ═══════════════════════════════════════════════════════════════════════════

async def handle_list_projects(_arguments: dict) -> list[TextContent]:
    projects = _load_projects()
    index = _load_index()
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
    name = arguments.get("name", "").strip()[:128]
    if not name:
        return [TextContent(type="text", text=json.dumps({"error": "name is required"}, ensure_ascii=False))]
    color = arguments.get("color")
    if color and not re.match(r"^#[0-9a-fA-F]{3,8}$", color):
        return [TextContent(type="text", text=json.dumps({"error": "Invalid color format (use #RGB, #RRGGBB, or #RRGGBBAA)"}, ensure_ascii=False))]
    projects = _load_projects()
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
    project_id = arguments.get("project_id")
    if not project_id:
        return [TextContent(type="text", text=json.dumps({"error": "project_id is required"}, ensure_ascii=False))]
    projects = _load_projects()
    proj = next((p for p in projects if p["project_id"] == project_id), None)
    if not proj:
        return [TextContent(type="text", text=json.dumps({"error": "Project not found"}, ensure_ascii=False))]
    projects = [p for p in projects if p["project_id"] != project_id]
    _save_projects(projects)
    # Unassign sessions — use API if auth available, filesystem otherwise
    unassigned = 0
    if SESSION_DIR.exists():
        for p in SESSION_DIR.glob("*.json"):
            if p.name.startswith("_"):
                continue
            try:
                session_data = json.loads(p.read_text(encoding="utf-8"))
                if session_data.get("project_id") == project_id:
                    sid = p.stem
                    if _api_password():
                        result = _api_post("/api/session/move", {"session_id": sid, "project_id": None})
                        if "ok" in result:
                            unassigned += 1
                    else:
                        # Filesystem fallback (may be overwritten by server cache)
                        session_data["project_id"] = None
                        tmp = p.with_suffix(".tmp")
                        tmp.write_text(json.dumps(session_data, ensure_ascii=False, indent=2), encoding="utf-8")
                        os.replace(tmp, p)
                        unassigned += 1
            except Exception:
                pass
    return [TextContent(type="text", text=json.dumps({
        "ok": True,
        "deleted": proj["name"],
        "unassigned_sessions": unassigned,
    }, ensure_ascii=False))]


async def handle_list_sessions(arguments: dict) -> list[TextContent]:
    project_id = arguments.get("project_id")
    unassigned = arguments.get("unassigned", False)
    limit = max(1, min(500, arguments.get("limit", 50)))
    index = _load_index()
    sessions = [_session_compact(s) for s in index if s.get("session_id")]
    if unassigned:
        sessions = [s for s in sessions if not s["project_id"]]
    elif project_id:
        sessions = [s for s in sessions if s["project_id"] == project_id]
    sessions = sessions[:limit]
    return [TextContent(type="text", text=json.dumps(sessions, ensure_ascii=False, indent=2))]


# ═══════════════════════════════════════════════════════════════════════════
#  Tool handlers — mutations (HTTP API with auth)
# ═══════════════════════════════════════════════════════════════════════════

async def handle_rename_session(arguments: dict) -> list[TextContent]:
    """Rename a session via the authenticated webui API."""
    session_id = arguments.get("session_id")
    title = arguments.get("title", "").strip()[:80]
    if not session_id or not title:
        return [TextContent(type="text", text=json.dumps({"error": "session_id and title are required"}, ensure_ascii=False))]
    result = _api_post("/api/session/rename", {"session_id": session_id, "title": title})
    if "error" in result:
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    session = result.get("session", {})
    return [TextContent(type="text", text=json.dumps({
        "ok": True,
        "session_id": session_id,
        "title": session.get("title", title),
        "method": "api",
    }, ensure_ascii=False, indent=2))]


async def handle_move_session(arguments: dict) -> list[TextContent]:
    """Assign a session to a project via the authenticated webui API."""
    session_id = arguments.get("session_id")
    project_id = arguments.get("project_id")  # None/null = unassign
    if not session_id:
        return [TextContent(type="text", text=json.dumps({"error": "session_id is required"}, ensure_ascii=False))]
    # If project_id is provided, verify it exists
    if project_id is not None:
        projects = _load_projects()
        if not any(p["project_id"] == project_id for p in projects):
            return [TextContent(type="text", text=json.dumps({"error": "Project not found"}, ensure_ascii=False))]
    result = _api_post("/api/session/move", {"session_id": session_id, "project_id": project_id})
    if "error" in result:
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    session = result.get("session", {})
    return [TextContent(type="text", text=json.dumps({
        "ok": True,
        "session_id": session_id,
        "project_id": project_id,
        "title": session.get("title"),
        "method": "api",
    }, ensure_ascii=False, indent=2))]


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
        name="rename_session",
        description="Rename a session (updates sidebar via authenticated API).",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID"},
                "title": {"type": "string", "description": "New title (max 80 chars)"},
            },
            "required": ["session_id", "title"],
        },
    ),
    Tool(
        name="move_session",
        description="Assign a session to a project. Pass project_id=null to unassign. Uses authenticated API for cache safety.",
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
    "rename_session": handle_rename_session,
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
