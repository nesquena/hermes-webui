#!/usr/bin/env python3
"""
Hermes WebUI MCP Server — exposes project and session management
as MCP tools for any MCP-compatible agent.

Option A rewrite (2026-05-08): imports api.models and api.profiles
directly from the webui codebase, using canonical helpers for
locking, profile scoping, index consistency, and validation.

    pip install mcp       # one-time setup
    python3 mcp_server.py # start via stdio

MCP config for Hermes Agent (add to config.yaml):
    mcp_servers:
      hermes-webui:
        command: /path/to/venv/bin/python3
        args: [/path/to/hermes-webui/mcp_server.py]
        env:
          HERMES_WEBUI_PASSWORD: your_password

Profile override (optional):
        args: [/path/to/hermes-webui/mcp_server.py, --profile, myprofile]

AI-authoring disclosure: this file was rewritten by MILO (Hermes Agent)
under human direction, per maintainer guidelines for #1616.
"""

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ── Ensure the repo root is on sys.path so api.* imports work ─────────────
_REPO_ROOT = Path(__file__).parent.resolve()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ── CLI: optional --profile override ──────────────────────────────────────
_profile_arg: str | None = None
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--profile", type=str, default=None)
_args, _unknown = _parser.parse_known_args()
_profile_arg = _args.profile

# ── Import webui canonical modules (after path setup) ─────────────────────
import api.config as _cfg
from api.config import (
    STATE_DIR, SESSION_DIR, SESSION_INDEX_FILE, PROJECTS_FILE, HOME,
)
from api.models import load_projects, save_projects
from api.profiles import get_active_profile_name, _is_root_profile

# ── Apply --profile override before any module uses get_active_profile_name
if _profile_arg is not None:
    import api.profiles as _profiles
    _profiles._active_profile = _profile_arg

# ── API auth state ─────────────────────────────────────────────────────────
# Mirror the env-var contract used by api/config.py:32-33 so a non-default
# WebUI port/host (e.g. when 8787 is held by another service on the host)
# Just Works without configuration drift between the WebUI process and MCP.
WEBUI_HOST = os.environ.get("HERMES_WEBUI_HOST", "127.0.0.1")
WEBUI_PORT = os.environ.get("HERMES_WEBUI_PORT", "8787")
WEBUI_URL = f"http://{WEBUI_HOST}:{WEBUI_PORT}"
_auth_cookie: str | None = None
_auth_expires: float = 0  # unix timestamp after which we re-auth

server = Server("hermes-webui")


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers — filesystem (project CRUD via canonical api.models)
# ═══════════════════════════════════════════════════════════════════════════

def _active_profile() -> str:
    """Shorthand for the current profile name (--profile or auto-detected)."""
    return get_active_profile_name() or 'default'


def _validate_color(color: str | None) -> str | None:
    """Return an error string if color is invalid, else None."""
    if color is not None and not re.match(r"^#[0-9a-fA-F]{3,8}$", color):
        return "Invalid color format (use #RGB, #RRGGBB, or #RRGGBBAA)"
    return None


def _profiles_match(row_profile: str | None, active: str | None) -> bool:
    """Cross-profile ownership check — mirrors api/routes.py:_profiles_match (#1614).

    A row with no profile (None or empty) is treated as belonging to the root
    profile ('default'), per the canonical webapp convention. This keeps the
    MCP visibility model identical to the HTTP API: a non-root profile cannot
    see legacy untagged projects, only the root profile (or a renamed-root
    alias) can.
    """
    row = row_profile or 'default'
    act = active or 'default'
    if row == act:
        return True
    if _is_root_profile(row) and _is_root_profile(act):
        return True
    return False


def _load_index() -> list:
    """Read the session index. Falls back to empty list on failure."""
    if not SESSION_INDEX_FILE.exists():
        return []
    try:
        return json.loads(SESSION_INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _session_compact(row: dict) -> dict:
    """Lightweight compact representation of a session index entry."""
    return {
        "session_id": row.get("session_id"),
        "title": row.get("title"),
        "project_id": row.get("project_id"),
        "workspace": row.get("workspace"),
        "model": row.get("model"),
        "message_count": row.get("message_count", 0),
        "source_tag": row.get("source_tag"),
        "is_cli_session": row.get("is_cli_session", False),
        "profile": row.get("profile"),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers — HTTP API (for mutations that need cache sync)
# ═══════════════════════════════════════════════════════════════════════════

def _api_password() -> str | None:
    """Return the plaintext webui password from HERMES_WEBUI_PASSWORD, or None.

    settings.json stores only the bcrypt hash, which the login endpoint cannot
    accept — it calls verify_password(plaintext) against the stored hash. So
    there's no usable fallback when the env var is unset; the MCP simply runs
    in unauthenticated mode and any auth-protected mutation will fail clearly
    with the server's 401 instead of silently sending an unusable hash.
    """
    pw = os.environ.get("HERMES_WEBUI_PASSWORD", "").strip()
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
#  Tool handlers — read-only (filesystem, profile-aware)
# ═══════════════════════════════════════════════════════════════════════════

async def handle_list_projects(_arguments: dict) -> list[TextContent]:
    """List all projects with session counts, scoped to active profile."""
    projects = load_projects()
    active = _active_profile()
    index = _load_index()

    # Session counts per project (from index)
    counts: dict[str, int] = {}
    for s in index:
        pid = s.get("project_id")
        if pid:
            counts[pid] = counts.get(pid, 0) + 1

    result = []
    for p in projects:
        # Profile filter: legacy untagged rows are treated as 'default' by
        # _profiles_match, so non-root profiles correctly hide them.
        if not _profiles_match(p.get("profile"), active):
            continue
        entry = dict(p)
        entry["session_count"] = counts.get(p["project_id"], 0)
        result.append(entry)

    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


async def handle_list_sessions(arguments: dict) -> list[TextContent]:
    """List sessions, optionally filtered by project or unassigned status."""
    project_id = arguments.get("project_id")
    unassigned = arguments.get("unassigned", False)
    limit = max(1, min(500, arguments.get("limit", 50)))
    active = _active_profile()

    index = _load_index()
    sessions = [_session_compact(s) for s in index if s.get("session_id")]

    # Filter by profile: legacy untagged rows are treated as 'default' by
    # _profiles_match (canonical convention), so non-root profiles hide them.
    sessions = [s for s in sessions if _profiles_match(s.get("profile"), active)]

    if unassigned:
        sessions = [s for s in sessions if not s["project_id"]]
    elif project_id:
        sessions = [s for s in sessions if s["project_id"] == project_id]

    sessions = sessions[:limit]
    return [TextContent(type="text", text=json.dumps(sessions, ensure_ascii=False, indent=2))]


# ═══════════════════════════════════════════════════════════════════════════
#  Tool handlers — project CRUD (canonical helpers, profile-scoped)
# ═══════════════════════════════════════════════════════════════════════════

async def handle_create_project(arguments: dict) -> list[TextContent]:
    """Create a new project (profile-scoped, exact-match title collision)."""
    name = arguments.get("name", "").strip()[:128]
    if not name:
        return [TextContent(type="text", text=json.dumps(
            {"error": "name is required"}, ensure_ascii=False))]

    color = arguments.get("color")
    color_err = _validate_color(color)
    if color_err:
        return [TextContent(type="text", text=json.dumps(
            {"error": color_err}, ensure_ascii=False))]

    active = _active_profile()
    projects = load_projects()

    # Title collision: exact match (consistent with ensure_cron_project)
    if any(p.get("name") == name and _profiles_match(p.get("profile"), active)
           for p in projects):
        return [TextContent(type="text", text=json.dumps(
            {"error": f"Project '{name}' already exists"}, ensure_ascii=False))]

    proj = {
        "project_id": uuid.uuid4().hex[:12],
        "name": name,
        "color": color,
        "profile": active,
        "created_at": time.time(),
    }
    projects.append(proj)
    save_projects(projects)

    proj["session_count"] = 0
    return [TextContent(type="text", text=json.dumps(proj, ensure_ascii=False, indent=2))]


async def handle_rename_project(arguments: dict) -> list[TextContent]:
    """Rename a project and optionally change its color (profile-checked)."""
    project_id = arguments.get("project_id")
    name = arguments.get("name", "").strip()[:128]
    if not project_id or not name:
        return [TextContent(type="text", text=json.dumps(
            {"error": "project_id and name are required"}, ensure_ascii=False))]

    color = arguments.get("color")
    color_err = _validate_color(color)
    if color_err:
        return [TextContent(type="text", text=json.dumps(
            {"error": color_err}, ensure_ascii=False))]

    active = _active_profile()
    projects = load_projects()
    proj = next((p for p in projects if p["project_id"] == project_id), None)
    if not proj:
        return [TextContent(type="text", text=json.dumps(
            {"error": "Project not found"}, ensure_ascii=False))]

    # #1614: profile ownership check
    if not _profiles_match(proj.get("profile"), active):
        return [TextContent(type="text", text=json.dumps(
            {"error": "Project not found"}, ensure_ascii=False))]

    proj["name"] = name
    if color is not None:
        proj["color"] = color
    save_projects(projects)
    return [TextContent(type="text", text=json.dumps(proj, ensure_ascii=False, indent=2))]


async def handle_delete_project(arguments: dict) -> list[TextContent]:
    """Delete a project and unassign all its sessions (profile-checked)."""
    project_id = arguments.get("project_id")
    if not project_id:
        return [TextContent(type="text", text=json.dumps(
            {"error": "project_id is required"}, ensure_ascii=False))]

    active = _active_profile()
    projects = load_projects()
    proj = next((p for p in projects if p["project_id"] == project_id), None)
    if not proj:
        return [TextContent(type="text", text=json.dumps(
            {"error": "Project not found"}, ensure_ascii=False))]

    # #1614: profile ownership check
    if not _profiles_match(proj.get("profile"), active):
        return [TextContent(type="text", text=json.dumps(
            {"error": "Project not found"}, ensure_ascii=False))]

    projects = [p for p in projects if p["project_id"] != project_id]
    save_projects(projects)

    # Unassign sessions only when we can do it cache-safely via the HTTP API.
    # The previous filesystem fallback wrote session_data directly with
    # os.replace(), which bypassed _write_session_index() in api/models.py
    # and left _index.json holding the stale project_id — a running WebUI
    # would still group those sessions under the deleted project until a
    # subsequent re-compact. Even calling Session.save() in-process would
    # not help because the WebUI's SESSIONS dict cache (a separate process)
    # still has the old project_id and overwrites our update on its next
    # save. The HTTP API is the only cache-safe path; without auth we
    # refuse and surface the limitation so the operator can act.
    has_auth = bool(_api_password())
    if not has_auth:
        return [TextContent(type="text", text=json.dumps({
            "ok": True,
            "deleted": proj["name"],
            "unassigned_sessions": 0,
            "warning": "Set HERMES_WEBUI_PASSWORD to unassign sessions; "
                       "without auth the session index cannot be safely "
                       "updated and direct filesystem writes would cause "
                       "index drift in a running WebUI.",
        }, ensure_ascii=False))]

    unassigned = 0
    if SESSION_DIR.exists():
        for p in SESSION_DIR.glob("*.json"):
            if p.name.startswith("_"):
                continue
            try:
                session_data = json.loads(p.read_text(encoding="utf-8"))
                if session_data.get("project_id") == project_id:
                    sid = p.stem
                    result = _api_post("/api/session/move",
                                       {"session_id": sid, "project_id": None})
                    if "ok" in result or "session" in result:
                        unassigned += 1
            except Exception:
                pass

    return [TextContent(type="text", text=json.dumps({
        "ok": True,
        "deleted": proj["name"],
        "unassigned_sessions": unassigned,
    }, ensure_ascii=False))]


# ═══════════════════════════════════════════════════════════════════════════
#  Tool handlers — mutations (HTTP API with auth, cache-safe)
# ═══════════════════════════════════════════════════════════════════════════

async def handle_rename_session(arguments: dict) -> list[TextContent]:
    """Rename a session via the authenticated webui API (cache-safe)."""
    session_id = arguments.get("session_id")
    title = arguments.get("title", "").strip()[:80]
    if not session_id or not title:
        return [TextContent(type="text", text=json.dumps(
            {"error": "session_id and title are required"}, ensure_ascii=False))]

    result = _api_post("/api/session/rename",
                       {"session_id": session_id, "title": title})
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
    """Assign a session to a project via the authenticated webui API (cache-safe)."""
    session_id = arguments.get("session_id")
    project_id = arguments.get("project_id")  # None/null = unassign
    if not session_id:
        return [TextContent(type="text", text=json.dumps(
            {"error": "session_id is required"}, ensure_ascii=False))]

    # If project_id is provided, verify it exists and is profile-accessible
    if project_id is not None:
        projects = load_projects()
        active = _active_profile()
        target = next((p for p in projects if p["project_id"] == project_id), None)
        if not target:
            return [TextContent(type="text", text=json.dumps(
                {"error": "Project not found"}, ensure_ascii=False))]
        # #1614: refuse moves into projects owned by another profile
        if not _profiles_match(target.get("profile"), active):
            return [TextContent(type="text", text=json.dumps(
                {"error": "Project not found"}, ensure_ascii=False))]

    result = _api_post("/api/session/move",
                       {"session_id": session_id, "project_id": project_id})
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
        description="List all session projects with their IDs, names, colors, and session counts (scoped to active profile).",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="create_project",
        description="Create a new project for organizing sessions (profile-scoped).",
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
        description="Rename a project and optionally change its color (profile-checked).",
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
        description="Delete a project and unassign all its sessions (profile-checked).",
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
        description="Rename a session (updates sidebar via authenticated API, cache-safe).",
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
        description="Assign a session to a project. Pass project_id=null to unassign. Uses authenticated API for cache safety (profile-checked).",
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
        description="List sessions, optionally filtered by project or unassigned status (profile-scoped).",
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
        return [TextContent(type="text", text=json.dumps(
            {"error": f"Unknown tool: {name}"}, ensure_ascii=False))]
    return await handler(arguments)


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
