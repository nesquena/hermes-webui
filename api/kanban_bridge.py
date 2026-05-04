"""Read-only Hermes Kanban bridge for the WebUI.

This module exposes a small WebUI-native API under ``/api/kanban/*`` while
keeping Hermes Agent's ``hermes_cli.kanban_db`` as the only source of truth.
The first integration is deliberately read-only; write/move semantics can be
added in later focused PRs.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from urllib.parse import parse_qs, unquote

from api.helpers import bad, j

BOARD_COLUMNS = ["triage", "todo", "ready", "running", "blocked", "done"]
_TASK_PREFIX = "/api/kanban/tasks/"


def _kb():
    from hermes_cli import kanban_db as kb

    return kb


def _conn():
    kb = _kb()
    kb.init_db()
    return kb.connect()


def _obj_dict(value):
    if value is None:
        return None
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return dict(getattr(value, "__dict__", {}))


def _task_dict(task):
    data = _obj_dict(task)
    if not data:
        return data
    try:
        age = _kb().task_age(task)
    except Exception:
        age = None
    data["age_seconds"] = age
    data["age"] = age
    data.setdefault("progress", None)
    return data


def _latest_event_id(conn) -> int:
    try:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS latest FROM task_events").fetchone()
        return int(row["latest"] or 0)
    except Exception:
        return 0


def _bool_query(parsed, name: str, default: bool = False) -> bool:
    raw = (parse_qs(parsed.query or "").get(name) or [None])[0]
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _str_query(parsed, name: str):
    raw = (parse_qs(parsed.query or "").get(name) or [None])[0]
    return str(raw).strip() or None if raw is not None else None


def _int_query(parsed, name: str, default=None, *, minimum=None, maximum=None):
    raw = _str_query(parsed, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _task_link_counts(conn, tasks):
    counts = {task.id: {"parents": 0, "children": 0} for task in tasks}
    try:
        rows = conn.execute("SELECT parent_id, child_id FROM task_links").fetchall()
    except Exception:
        return counts
    for row in rows:
        counts.setdefault(row["parent_id"], {"parents": 0, "children": 0})["children"] += 1
        counts.setdefault(row["child_id"], {"parents": 0, "children": 0})["parents"] += 1
    return counts


def _comment_counts(conn):
    try:
        rows = conn.execute(
            "SELECT task_id, COUNT(*) AS n FROM task_comments GROUP BY task_id"
        ).fetchall()
    except Exception:
        return {}
    return {row["task_id"]: int(row["n"] or 0) for row in rows}


def _board_payload(parsed):
    kb = _kb()
    tenant = _str_query(parsed, "tenant")
    assignee = _str_query(parsed, "assignee")
    include_archived = _bool_query(parsed, "include_archived", False)
    since = _int_query(parsed, "since", None, minimum=0)

    with _conn() as conn:
        latest_event_id = _latest_event_id(conn)
        if since is not None and since >= latest_event_id:
            return {"changed": False, "latest_event_id": latest_event_id, "read_only": True}

        tasks = kb.list_tasks(
            conn,
            tenant=tenant,
            assignee=assignee,
            include_archived=include_archived,
        )
        link_counts = _task_link_counts(conn, tasks)
        comment_counts = _comment_counts(conn)

        def row(task):
            data = _task_dict(task)
            data["link_counts"] = link_counts.get(task.id, {"parents": 0, "children": 0})
            data["comment_count"] = comment_counts.get(task.id, 0)
            return data

        columns = [
            {"name": name, "tasks": [row(task) for task in tasks if task.status == name]}
            for name in BOARD_COLUMNS
        ]
        if include_archived:
            columns.append({
                "name": "archived",
                "tasks": [row(task) for task in tasks if task.status == "archived"],
            })
        return {
            "columns": columns,
            "tenants": sorted({task.tenant for task in tasks if getattr(task, "tenant", None)}),
            "assignees": sorted({task.assignee for task in tasks if getattr(task, "assignee", None)}),
            "latest_event_id": latest_event_id,
            "changed": True,
            "read_only": True,
            "filters": {
                "tenant": tenant,
                "assignee": assignee,
                "include_archived": include_archived,
            },
        }


def _links_for(conn, task_id: str) -> dict:
    kb = _kb()
    return {
        "parents": kb.parent_ids(conn, task_id),
        "children": kb.child_ids(conn, task_id),
    }


def _task_detail_payload(task_id: str):
    kb = _kb()
    with _conn() as conn:
        task = kb.get_task(conn, task_id)
        if not task:
            return None
        return {
            "task": _task_dict(task),
            "comments": [_obj_dict(c) for c in kb.list_comments(conn, task_id)],
            "events": [_obj_dict(e) for e in kb.list_events(conn, task_id)],
            "links": _links_for(conn, task_id),
            "runs": [_obj_dict(r) for r in kb.list_runs(conn, task_id)],
            "read_only": True,
        }


def _events_payload(parsed):
    since = _int_query(parsed, "since", 0, minimum=0)
    limit = _int_query(parsed, "limit", 200, minimum=1, maximum=200)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, task_id, run_id, kind, payload, created_at "
            "FROM task_events WHERE id > ? ORDER BY id ASC LIMIT ?",
            (since, limit),
        ).fetchall()
        events = []
        cursor = since
        for row in rows:
            try:
                payload = json.loads(row["payload"]) if row["payload"] else None
            except Exception:
                payload = None
            events.append({
                "id": row["id"],
                "task_id": row["task_id"],
                "run_id": row["run_id"],
                "kind": row["kind"],
                "payload": payload,
                "created_at": row["created_at"],
            })
            cursor = int(row["id"])
        latest = _latest_event_id(conn)
        if not events:
            cursor = latest if since >= latest else since
        return {"events": events, "cursor": cursor, "latest_event_id": cursor, "read_only": True}


def _config_payload():
    return {"columns": BOARD_COLUMNS, "read_only": True}


def handle_kanban_get(handler, parsed) -> bool:
    path = parsed.path
    if path == "/api/kanban/board":
        return j(handler, _board_payload(parsed)) or True
    if path == "/api/kanban/config":
        return j(handler, _config_payload()) or True
    if path == "/api/kanban/events":
        return j(handler, _events_payload(parsed)) or True
    if path.startswith(_TASK_PREFIX):
        task_id = unquote(path[len(_TASK_PREFIX):]).strip("/")
        if not task_id or "/" in task_id:
            return False
        payload = _task_detail_payload(task_id)
        if payload is None:
            return bad(handler, "task not found", status=404)
        return j(handler, payload) or True
    return False
