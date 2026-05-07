"""Hermes Kanban bridge for the WebUI.

This module exposes a full CRUD API under ``/api/kanban/*`` while keeping
Hermes Agent's ``hermes_cli.kanban_db`` as the only source of truth.

Supported operations:
- Task CRUD (create, read, patch, bulk update, archive)
- Multi-board management (list, create, archive, switch)
- Task dependency links (create, delete)
- SSE live event stream for real-time updates
- Comments and worker dispatch integration
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from urllib.parse import parse_qs, unquote

from api.helpers import bad, j

BOARD_COLUMNS = ["triage", "todo", "ready", "running", "blocked", "done"]
_TASK_PREFIX = "/api/kanban/tasks/"


def _kb():
    from hermes_cli import kanban_db as kb

    return kb


def _resolve_board(parsed):
    """Validate and normalise a ?board=<slug> query param.

    Returns the normalised slug, or ``None`` when the caller omitted the
    param. Raises ValueError on a malformed slug so the bridge surfaces a
    clean 400 instead of a 500 from deeper in the library.
    """
    raw = (parse_qs(parsed.query or "").get("board") or [None])[0]
    return _normalise_board_or_raise(raw)


def _resolve_board_from_body(body):
    """Same contract as :func:`_resolve_board` but reads ``board`` from a
    parsed JSON body (POST / PATCH / DELETE handlers receive a dict, not
    a parsed URL). Returns ``None`` when the body did not specify a board.
    """
    if not isinstance(body, dict):
        return None
    raw = body.get("board")
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    return _normalise_board_or_raise(raw)


def _normalise_board_or_raise(raw):
    """Shared normalisation + existence check for board slugs."""
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    kb = _kb()
    try:
        normed = kb._normalize_board_slug(raw)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid board slug: {raw!r}") from exc
    if not normed:
        return None
    # Allow the default board even if it has not been materialised yet
    # (kb.init_db will create it lazily). For non-default boards, require
    # the directory exists or _conn would fail with a confusing OperationalError.
    try:
        default_slug = getattr(kb, "DEFAULT_BOARD", "default")
    except Exception:
        default_slug = "default"
    if normed != default_slug and not kb.board_exists(normed):
        raise LookupError(f"board {normed!r} does not exist")
    return normed


def _conn(board=None):
    kb = _kb()
    kb.init_db(board=board)
    return kb.connect(board=board)


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
    board = _resolve_board(parsed)
    kb = _kb()
    tenant = _str_query(parsed, "tenant")
    assignee = _str_query(parsed, "assignee")
    include_archived = _bool_query(parsed, "include_archived", False)
    only_mine = _bool_query(parsed, "only_mine", False)
    since = _int_query(parsed, "since", None, minimum=0)
    profile = None
    if only_mine and not assignee:
        try:
            from api.profiles import get_active_profile_name

            profile = get_active_profile_name() or "default"
        except Exception:
            profile = "default"
        assignee = profile

    with _conn(board=board) as conn:
        latest_event_id = _latest_event_id(conn)
        if since is not None and since >= latest_event_id:
            return {"changed": False, "latest_event_id": latest_event_id, "read_only": False}

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
            "read_only": False,
            "filters": {
                "tenant": tenant,
                "assignee": assignee,
                "include_archived": include_archived,
                "only_mine": only_mine,
                "profile": profile,
            },
        }



def _validate_status(status: str) -> str:
    value = str(status or "").strip().lower()
    allowed = set(BOARD_COLUMNS) | {"archived"}
    if value not in allowed:
        raise ValueError(f"invalid status: {value}")
    return value


def _set_status_direct(conn, task_id: str, new_status: str) -> bool:
    """Direct status write for drag-drop moves not covered by structured verbs.

    Used for ``todo <-> ready`` and ``running -> ready`` transitions. The
    structured verbs (``complete_task``, ``block_task``, ``unblock_task``,
    ``archive_task``, ``claim_task``) own their own state changes; this helper
    handles the remainder while preserving the dispatcher's contract:

    - When transitioning OFF ``running`` to anything other than the terminal
      verbs, claim_lock / claim_expires / worker_pid are nulled so the
      dispatcher doesn't see a phantom-running task. The active run (if any)
      is closed with ``outcome='reclaimed'`` so attempt history isn't
      orphaned.
    - When transitioning INTO ``running``, claim fields are preserved (this
      function is NOT used for entering 'running' — that goes through
      ``kb.claim_task()`` and the bridge rejects raw 'running' status writes
      with HTTP 400).

    Mirrors the agent dashboard plugin's ``_set_status_direct``
    (plugins/kanban/dashboard/plugin_api.py) so first-party clients see
    identical behaviour from either surface.
    """
    kb = _kb()
    with kb.write_txn(conn):
        prev = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if prev is None:
            return False
        was_running = prev["status"] == "running"
        cur = conn.execute(
            "UPDATE tasks SET status = ?, "
            "  claim_lock = CASE WHEN ? = 'running' THEN claim_lock ELSE NULL END, "
            "  claim_expires = CASE WHEN ? = 'running' THEN claim_expires ELSE NULL END, "
            "  worker_pid = CASE WHEN ? = 'running' THEN worker_pid ELSE NULL END "
            "WHERE id = ?",
            (new_status, new_status, new_status, new_status, task_id),
        )
        if cur.rowcount != 1:
            return False
        run_id = None
        if was_running and new_status != "running" and prev["current_run_id"]:
            try:
                run_id = kb._end_run(
                    conn, task_id,
                    outcome="reclaimed", status="reclaimed",
                    summary=f"status changed to {new_status} (webui/direct)",
                )
            except Exception:
                # _end_run is best-effort here; the status flip itself is
                # what matters for sidebar rendering.
                run_id = None
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, 'status', ?, ?)",
            (task_id, run_id, json.dumps({"status": new_status, "source": "webui"}), int(time.time())),
        )
    if new_status in ("done", "ready") and hasattr(kb, "recompute_ready"):
        try:
            kb.recompute_ready(conn)
        except Exception:
            pass
    return True


def _create_task_payload(body: dict, *, board=None):
    title = str(body.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    try:
        priority = int(body.get("priority") or 0)
    except (TypeError, ValueError):
        raise ValueError("priority must be an integer")
    kb = _kb()
    requested_status = body.get("status")
    with _conn(board=board) as conn:
        task_id = kb.create_task(
            conn,
            title=title,
            body=body.get("body") or None,
            assignee=body.get("assignee") or None,
            created_by=body.get("created_by") or "webui",
            tenant=body.get("tenant") or None,
            priority=priority,
            parents=body.get("parents") or (),
            triage=bool(body.get("triage") or False),
            workspace_kind=body.get("workspace_kind") or "scratch",
            workspace_path=body.get("workspace_path") or None,
            idempotency_key=body.get("idempotency_key") or None,
            max_runtime_seconds=body.get("max_runtime_seconds") or None,
            skills=body.get("skills") or None,
        )
        if requested_status:
            _patch_task(conn, task_id, {"status": requested_status})
        return {"task": _task_dict(kb.get_task(conn, task_id)), "read_only": False}


def _patch_task(conn, task_id: str, body: dict):
    kb = _kb()
    task = kb.get_task(conn, task_id)
    if not task:
        raise LookupError("task not found")

    updates = {}
    if "title" in body:
        title = str(body.get("title") or "").strip()
        if not title:
            raise ValueError("title is required")
        updates["title"] = title
    if "body" in body:
        updates["body"] = body.get("body") or None
    if "tenant" in body:
        updates["tenant"] = body.get("tenant") or None
    if "priority" in body:
        try:
            updates["priority"] = int(body.get("priority") or 0)
        except (TypeError, ValueError):
            raise ValueError("priority must be an integer")

    for field, value in updates.items():
        if hasattr(task, field):
            try:
                setattr(task, field, value)
            except Exception:
                pass
    if updates:
        assignments = ", ".join(f"{field} = ?" for field in updates)
        conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", [*updates.values(), task_id])
        if hasattr(kb, "_append_event"):
            kb._append_event(conn, task_id, "updated", {"fields": list(updates), "source": "webui"})

    if "assignee" in body:
        if not kb.assign_task(conn, task_id, body.get("assignee") or None):
            raise LookupError("task not found")

    if "status" not in body or body.get("status") in (None, ""):
        return
    status = _validate_status(body.get("status"))
    if status == "done":
        if not kb.complete_task(conn, task_id, result=body.get("result"), summary=body.get("summary")):
            raise LookupError("task not found")
    elif status == "blocked":
        if not kb.block_task(conn, task_id, reason=body.get("block_reason") or body.get("reason")):
            raise LookupError("task not found")
    elif status == "archived":
        if not kb.archive_task(conn, task_id):
            raise LookupError("task not found")
    elif status == "running":
        # The 'running' state is owned by the kanban dispatcher / claim
        # protocol — entering it via raw UPDATE bypasses claim_lock,
        # claim_expires, started_at, and worker_pid, which leaves the task
        # in a state the dispatcher treats as "phantom claimed" and may
        # reclaim or hide. Match the agent dashboard plugin's contract
        # (plugins/kanban/dashboard/plugin_api.py update_task) by rejecting
        # this transition with HTTP 400. Workers enter 'running' via
        # kb.claim_task(); UI users should use the dispatcher nudge.
        raise ValueError(
            "Cannot set status to 'running' directly; use the dispatcher/claim path"
        )
    elif status == "ready":
        # If the task is currently 'blocked', use the structured unblock
        # verb so the unblocked event fires. Otherwise it's a legitimate
        # drag-drop or click move (e.g. todo → ready, running → ready when
        # the user yanks a stuck worker back to the queue) and we use the
        # claim-aware direct status write.
        current = kb.get_task(conn, task_id)
        if not current:
            raise LookupError("task not found")
        if current.status == "blocked":
            if not kb.unblock_task(conn, task_id):
                raise LookupError("task not found")
        else:
            if not _set_status_direct(conn, task_id, "ready"):
                raise LookupError("task not found")
    elif status in ("triage", "todo"):
        # Direct status write for drag-drop moves between non-running,
        # non-terminal columns. Uses the claim-aware helper that nulls out
        # claim_lock / claim_expires / worker_pid when leaving 'running'
        # and ends any active run with outcome='reclaimed'.
        if not _set_status_direct(conn, task_id, status):
            raise LookupError("task not found")
    else:
        # _validate_status guarantees we never reach here, but be defensive.
        raise ValueError(f"unknown status: {status}")


def _patch_task_payload(task_id: str, body: dict, *, board=None):
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    kb = _kb()
    with _conn(board=board) as conn:
        _patch_task(conn, task_id, body)
        return {"task": _task_dict(kb.get_task(conn, task_id)), "read_only": False}


def _comment_payload(task_id: str, body: dict, *, board=None):
    task_id = str(task_id or "").strip()
    comment_body = str(body.get("body") or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    if not comment_body:
        raise ValueError("body is required")
    kb = _kb()
    with _conn(board=board) as conn:
        if not kb.get_task(conn, task_id):
            raise LookupError("task not found")
        comment_id = kb.add_comment(conn, task_id, body.get("author") or "webui", comment_body)
        return {"ok": True, "comment_id": comment_id, "read_only": False}


def _link_tasks_payload(body: dict, *, unlink: bool = False, board=None):
    parent_id = str(body.get("parent_id") or "").strip()
    child_id = str(body.get("child_id") or "").strip()
    if not parent_id or not child_id:
        raise ValueError("parent_id and child_id are required")
    kb = _kb()
    with _conn(board=board) as conn:
        if not kb.get_task(conn, parent_id):
            raise LookupError("parent task not found")
        if not kb.get_task(conn, child_id):
            raise LookupError("child task not found")
        if unlink:
            changed = kb.unlink_tasks(conn, parent_id, child_id)
            return {"ok": True, "changed": bool(changed), "parent_id": parent_id, "child_id": child_id, "read_only": False}
        kb.link_tasks(conn, parent_id, child_id)
        return {"ok": True, "parent_id": parent_id, "child_id": child_id, "read_only": False}

def _links_for(conn, task_id: str) -> dict:
    kb = _kb()
    return {
        "parents": kb.parent_ids(conn, task_id),
        "children": kb.child_ids(conn, task_id),
    }


def _task_detail_payload(task_id: str, *, board=None):
    kb = _kb()
    with _conn(board=board) as conn:
        task = kb.get_task(conn, task_id)
        if not task:
            return None
        return {
            "task": _task_dict(task),
            "comments": [_obj_dict(c) for c in kb.list_comments(conn, task_id)],
            "events": [_obj_dict(e) for e in kb.list_events(conn, task_id)],
            "links": _links_for(conn, task_id),
            "runs": [_obj_dict(r) for r in kb.list_runs(conn, task_id)],
            "read_only": False,
        }


def _events_payload(parsed):
    board = _resolve_board(parsed)
    since = _int_query(parsed, "since", 0, minimum=0)
    limit = _int_query(parsed, "limit", 200, minimum=1, maximum=200)
    with _conn(board=board) as conn:
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
        return {"events": events, "cursor": cursor, "latest_event_id": cursor, "read_only": False}


def _config_payload(*, board=None):
    kb = _kb()
    try:
        with _conn(board=board) as conn:
            try:
                assignees = list(kb.known_assignees(conn))
            except Exception:
                assignees = []
    except Exception:
        assignees = []
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        cfg = {}
    k_cfg = ((cfg.get("dashboard") or {}).get("kanban") or {})
    return {
        "columns": BOARD_COLUMNS,
        "assignees": assignees,
        "default_tenant": k_cfg.get("default_tenant") or "",
        "lane_by_profile": bool(k_cfg.get("lane_by_profile", True)),
        "include_archived_by_default": bool(k_cfg.get("include_archived_by_default", False)),
        "render_markdown": bool(k_cfg.get("render_markdown", True)),
        "read_only": False,
    }


def _stats_payload(*, board=None):
    kb = _kb()
    with _conn(board=board) as conn:
        if hasattr(kb, "board_stats"):
            return kb.board_stats(conn)
        rows = conn.execute(
            "SELECT status, assignee, COUNT(*) AS n FROM tasks WHERE status != 'archived' GROUP BY status, assignee"
        ).fetchall()
        by_status = {}
        by_assignee = {}
        for row in rows:
            n = int(row["n"] or 0)
            by_status[row["status"]] = by_status.get(row["status"], 0) + n
            assignee = row["assignee"] or "unassigned"
            by_assignee[assignee] = by_assignee.get(assignee, 0) + n
        return {"by_status": by_status, "by_assignee": by_assignee}


def _assignees_payload(*, board=None):
    kb = _kb()
    with _conn(board=board) as conn:
        try:
            assignees = list(kb.known_assignees(conn))
        except Exception:
            rows = conn.execute(
                "SELECT DISTINCT assignee FROM tasks WHERE assignee IS NOT NULL AND assignee != '' ORDER BY assignee"
            ).fetchall()
            assignees = [row["assignee"] for row in rows]
    return {"assignees": assignees}


def _task_log_payload(parsed, task_id: str):
    board = _resolve_board(parsed)
    kb = _kb()
    tail = _int_query(parsed, "tail", None, minimum=1, maximum=2_000_000)
    with _conn(board=board) as conn:
        if not kb.get_task(conn, task_id):
            return None
    if not hasattr(kb, "read_worker_log"):
        return {"task_id": task_id, "path": "", "exists": False, "size_bytes": 0, "content": "", "truncated": False}
    content = kb.read_worker_log(task_id, tail_bytes=tail)
    log_path = kb.worker_log_path(task_id) if hasattr(kb, "worker_log_path") else None
    try:
        size = log_path.stat().st_size if log_path and log_path.exists() else 0
    except OSError:
        size = 0
    return {
        "task_id": task_id,
        "path": str(log_path or ""),
        "exists": content is not None,
        "size_bytes": size,
        "content": content or "",
        "truncated": bool(tail and size > tail),
    }


def _bulk_tasks_payload(body: dict, *, board=None):
    ids = [str(i).strip() for i in (body.get("ids") or []) if str(i).strip()]
    if not ids:
        raise ValueError("ids is required")
    results = []
    kb = _kb()
    with _conn(board=board) as conn:
        for task_id in ids:
            entry = {"id": task_id, "ok": True}
            try:
                if not kb.get_task(conn, task_id):
                    entry.update(ok=False, error="not found")
                    results.append(entry)
                    continue
                if body.get("archive"):
                    if not kb.archive_task(conn, task_id):
                        entry.update(ok=False, error="archive refused")
                elif body.get("status") is not None:
                    _patch_task(conn, task_id, {"status": body.get("status")})
                if body.get("assignee") is not None:
                    if not kb.assign_task(conn, task_id, body.get("assignee") or None):
                        entry.update(ok=False, error="assign refused")
                if body.get("priority") is not None:
                    try:
                        priority = int(body.get("priority"))
                    except (TypeError, ValueError):
                        entry.update(ok=False, error="priority must be an integer")
                    else:
                        conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (priority, task_id))
                        if hasattr(kb, "_append_event"):
                            kb._append_event(conn, task_id, "reprioritized", {"priority": priority, "source": "webui"})
            except Exception as exc:
                entry.update(ok=False, error=str(exc))
            results.append(entry)
    return {"results": results, "read_only": False}


def _dispatch_payload(parsed):
    board = _resolve_board(parsed)
    kb = _kb()
    dry_run = _bool_query(parsed, "dry_run", False)
    max_spawn = _int_query(parsed, "max", 8, minimum=1, maximum=100)
    if not hasattr(kb, "dispatch_once"):
        raise ValueError("dispatcher is unavailable")
    with _conn(board=board) as conn:
        result = kb.dispatch_once(conn, dry_run=dry_run, max_spawn=max_spawn)
    if isinstance(result, dict):
        return result
    try:
        return asdict(result)
    except TypeError:
        return {"result": str(result)}


def _task_action_payload(task_id: str, body: dict, action: str, *, board=None):
    kb = _kb()
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    with _conn(board=board) as conn:
        if not kb.get_task(conn, task_id):
            raise LookupError("task not found")
        if action == "block":
            ok = kb.block_task(conn, task_id, reason=body.get("reason") or body.get("block_reason"))
        elif action == "unblock":
            if hasattr(kb, "unblock_task"):
                ok = kb.unblock_task(conn, task_id)
            else:
                _patch_task(conn, task_id, {"status": "ready"})
                ok = True
        else:
            raise ValueError(f"invalid action: {action}")
        if not ok:
            raise RuntimeError(f"{action} refused")
        return {"task": _task_dict(kb.get_task(conn, task_id)), "read_only": False}


# ---------------------------------------------------------------------------
# Multi-board management
# ---------------------------------------------------------------------------
# These endpoints operate on the on-disk board collection itself rather than
# on the tasks of a single board. They mirror the agent dashboard plugin's
# /boards surface (plugins/kanban/dashboard/plugin_api.py) so that the
# CLI / gateway / dashboard / WebUI all share the same active-board pointer.

def _board_meta_dict(meta):
    """Coerce the library's board metadata dict into a JSON-serialisable
    form. ``list_boards`` returns dicts with Path values for ``directory``;
    json.dumps would refuse those without help."""
    if not isinstance(meta, dict):
        return meta
    out = dict(meta)
    for key in ("directory", "db_path", "path"):
        if key in out and out[key] is not None:
            out[key] = str(out[key])
    return out


def _board_counts_for_slug(slug):
    """Per-status task counts for a board, used to populate the board
    switcher with a live "12 tasks" badge. Mirrors the agent dashboard's
    ``_board_counts`` helper. Returns an empty dict for boards whose
    sqlite file has not been materialized yet (freshly-created boards
    with no tasks)."""
    kb = _kb()
    if not kb.board_exists(slug):
        return {}
    try:
        conn = kb.connect(board=slug)
    except Exception:
        return {}
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks "
            "WHERE status != 'archived' GROUP BY status"
        ).fetchall()
        return {row["status"]: int(row["n"] or 0) for row in rows}
    except Exception:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _list_boards_payload(parsed):
    """GET /api/kanban/boards — return all boards on disk + active slug.

    Each entry includes per-status counts and an ``is_current`` flag so the
    UI can render the switcher in a single round-trip.
    """
    kb = _kb()
    include_archived = _bool_query(parsed, "include_archived", False)
    boards = kb.list_boards(include_archived=include_archived)
    try:
        current = kb.get_current_board()
    except Exception:
        current = "default"
    visible_slugs = {(_board_meta_dict(meta).get("slug")) for meta in boards}
    default_slug = getattr(kb, "DEFAULT_BOARD", "default")
    if current not in visible_slugs:
        # The on-disk active-board pointer can outlive an archived/deleted board
        # when another CLI/WebUI process removes it. Surface a valid current
        # board instead of letting the frontend pin every subsequent request to
        # a ghost slug and fail with an opaque 404.
        try:
            kb.clear_current_board()
        except Exception:
            pass
        current = default_slug
    out = []
    for raw_meta in boards:
        meta = _board_meta_dict(raw_meta)
        slug = meta.get("slug")
        if slug is None:
            continue
        meta["is_current"] = (slug == current)
        meta["counts"] = _board_counts_for_slug(slug)
        meta["total"] = sum(meta["counts"].values()) if meta["counts"] else 0
        out.append(meta)
    return {"boards": out, "current": current, "read_only": False}


def _create_board_payload(body):
    """POST /api/kanban/boards — create a new board.

    Body fields: ``slug`` (required), ``name``, ``description``, ``icon``,
    ``color``, ``switch`` (bool — set as active after creation, default false).
    Idempotent on slug — repeating returns the existing board metadata.
    """
    kb = _kb()
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    slug = str(body.get("slug") or "").strip()
    if not slug:
        raise ValueError("slug is required")
    try:
        meta = kb.create_board(
            slug,
            name=body.get("name") or None,
            description=body.get("description") or None,
            icon=body.get("icon") or None,
            color=body.get("color") or None,
        )
    except (ValueError, AttributeError) as exc:
        raise ValueError(str(exc)) from exc
    if body.get("switch"):
        try:
            kb.set_current_board(meta["slug"])
        except (ValueError, AttributeError) as exc:
            raise ValueError(str(exc)) from exc
    try:
        current = kb.get_current_board()
    except Exception:
        current = "default"
    return {"board": _board_meta_dict(meta), "current": current, "read_only": False}


def _update_board_payload(slug, body):
    """PATCH /api/kanban/boards/<slug> — update a board's display metadata.

    The slug itself is immutable (changing it would mean moving the on-disk
    directory and re-pointing every saved active-board cookie). Only
    ``name``, ``description``, ``icon``, ``color``, and ``archived`` are
    mutable here; the slug travels in the URL path.
    """
    kb = _kb()
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    try:
        normed = kb._normalize_board_slug(slug)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid board slug: {slug!r}") from exc
    if not normed or not kb.board_exists(normed):
        raise LookupError(f"board {slug!r} does not exist")
    archived = body.get("archived")
    if isinstance(archived, str):
        archived = archived.strip().lower() in {"1", "true", "yes", "on"}
    meta = kb.write_board_metadata(
        normed,
        name=body.get("name"),
        description=body.get("description"),
        icon=body.get("icon"),
        color=body.get("color"),
        archived=archived if isinstance(archived, bool) else None,
    )
    return {"board": _board_meta_dict(meta), "read_only": False}


def _delete_board_payload(slug, parsed):
    """DELETE /api/kanban/boards/<slug> — archive (default) or hard-delete.

    ``?delete=1`` is required to actually remove on-disk artefacts; without
    it the board is just marked archived in its metadata and remains
    enumerable via ``?include_archived=1`` on /boards.
    """
    kb = _kb()
    hard_delete = _bool_query(parsed, "delete", False)
    try:
        normed = kb._normalize_board_slug(slug)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid board slug: {slug!r}") from exc
    if not normed or not kb.board_exists(normed):
        raise LookupError(f"board {slug!r} does not exist")
    # Refuse to delete the default board — that would leave the system
    # without a fallback active board on next CLI / dashboard call.
    try:
        default_slug = getattr(kb, "DEFAULT_BOARD", "default")
    except Exception:
        default_slug = "default"
    if normed == default_slug:
        raise ValueError("cannot remove the default board")
    res = kb.remove_board(normed, archive=not hard_delete)
    try:
        current = kb.get_current_board()
    except Exception:
        current = "default"
    # If we just removed the active board, the library auto-falls-back to
    # default on the next get_current_board() — surface that explicitly so
    # the UI can re-fetch /board on the new active slug.
    return {
        "result": _board_meta_dict(res) if isinstance(res, dict) else res,
        "current": current,
        "read_only": False,
    }


def _switch_board_payload(slug):
    """POST /api/kanban/boards/<slug>/switch — set this board as active.

    The active-board pointer is stored on disk under ``<root>/kanban/current``
    and is shared by the CLI, gateway, dashboard, and WebUI — switching
    here switches everywhere. The UI also keeps a localStorage hint so
    that opening a fresh tab doesn't always have to round-trip to discover
    the active slug, but the on-disk pointer is the source of truth.
    """
    kb = _kb()
    try:
        normed = kb._normalize_board_slug(slug)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid board slug: {slug!r}") from exc
    if not normed or not kb.board_exists(normed):
        raise LookupError(f"board {slug!r} does not exist")
    kb.set_current_board(normed)
    return {"current": normed, "read_only": False}


# ---------------------------------------------------------------------------
# SSE event stream
# ---------------------------------------------------------------------------
# Server-Sent Events let the UI react to task transitions in real time
# without the 30s HTTP polling tax. The agent dashboard uses WebSockets
# for the same purpose; we use SSE because the WebUI's existing transport
# is a synchronous BaseHTTPServer and SSE is the right tool for
# unidirectional server-pushed event streams. The wire-level UX is
# identical from the client's perspective: events arrive within ~300ms
# of being committed to task_events.

# Polling interval matches the agent dashboard's _EVENT_POLL_SECONDS so
# write-to-receive latency is identical between the two surfaces.
_KANBAN_SSE_POLL_SECONDS = 0.3
# Heartbeat keeps proxies/CDNs from reaping the connection on idle boards.
# Identical to the approval/clarify SSE heartbeat.
_KANBAN_SSE_HEARTBEAT_SECONDS = 15.0
# Hard cap on a single SSE batch so a board with thousands of historical
# events doesn't ship them all in one frame. Same as the dashboard.
_KANBAN_SSE_BATCH_LIMIT = 200


def _kanban_sse_fetch_new(board, cursor):
    """Read events with id > cursor from the given board's task_events
    table. Returns ``(new_cursor, events_list)``. Best-effort — returns
    the input cursor and an empty list on any DB error so the SSE loop
    self-heals on transient sqlite contention rather than dropping the
    client."""
    kb = _kb()
    # Guard against a board that's been archived/removed mid-stream:
    # kb.connect(board=<slug>) auto-materialises the directory + DB on
    # first call, which would silently un-archive a board that was just
    # removed. Skip the fetch when the board no longer exists.
    if board is not None:
        try:
            default_slug = getattr(kb, "DEFAULT_BOARD", "default")
        except Exception:
            default_slug = "default"
        if board != default_slug and not kb.board_exists(board):
            return cursor, []
    try:
        conn = kb.connect(board=board)
    except Exception:
        return cursor, []
    try:
        rows = conn.execute(
            "SELECT id, task_id, run_id, kind, payload, created_at "
            "FROM task_events WHERE id > ? ORDER BY id ASC LIMIT ?",
            (int(cursor), _KANBAN_SSE_BATCH_LIMIT),
        ).fetchall()
    except Exception:
        return cursor, []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    out = []
    new_cursor = cursor
    for r in rows:
        payload = None
        try:
            raw = r["payload"]
            if raw:
                payload = json.loads(raw)
        except Exception:
            payload = None
        out.append({
            "id": int(r["id"]),
            "task_id": r["task_id"],
            "run_id": r["run_id"],
            "kind": r["kind"],
            "payload": payload,
            "created_at": int(r["created_at"]) if r["created_at"] is not None else None,
        })
        new_cursor = int(r["id"])
    return new_cursor, out


def _handle_events_sse_stream(handler, parsed):
    """GET /api/kanban/events/stream — long-lived SSE feed of task events.

    Query params:
      since=<int>   Resume from this event id. Defaults to 0 (full backlog
                    on first connect — the client should pass the latest
                    id it knows about so it does not re-receive historical
                    events.) Capped to the most recent _KANBAN_SSE_BATCH_LIMIT.
      board=<slug>  Pin the stream to a specific board. Switching boards
                    requires the client to close and re-open the stream.

    Header (set automatically by EventSource on reconnect):
      Last-Event-ID  Fallback resume cursor when ?since= is absent. The
                     server emits ``id: <event_id>`` on every events frame
                     so the browser can resume cleanly across drops without
                     re-receiving up to _KANBAN_SSE_BATCH_LIMIT events the
                     client already has.

    Mirrors the agent dashboard's WebSocket /events contract event-for-event
    so a client that handles one can handle the other with only the
    transport swapped.
    """
    try:
        board = _resolve_board(parsed)
    except (ValueError, LookupError) as exc:
        return bad(handler, str(exc), status=400 if isinstance(exc, ValueError) else 404)

    qs = parse_qs(parsed.query or "")
    # Resolution chain: ?since= query param → Last-Event-ID header → 0.
    # The Last-Event-ID header is what EventSource sends automatically on
    # reconnect; honouring it lets the browser resume cleanly without the
    # client needing to track the cursor in JS.
    since_raw = (qs.get("since") or [None])[0]
    if since_raw is None:
        try:
            since_raw = handler.headers.get("Last-Event-ID")
        except Exception:
            since_raw = None
    try:
        cursor = int(since_raw) if since_raw is not None else 0
    except (TypeError, ValueError):
        cursor = 0
    if cursor < 0:
        cursor = 0

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Connection", "keep-alive")
    handler.end_headers()

    # Send an initial frame so the client knows the connection is open
    # and learns the current cursor (in case the server already had a
    # backlog when the client first connected).
    try:
        handler.wfile.write(
            f"event: hello\ndata: {json.dumps({'cursor': cursor, 'board': board})}\n\n".encode("utf-8")
        )
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
        return True

    last_heartbeat = time.monotonic()
    try:
        while True:
            cursor, events = _kanban_sse_fetch_new(board, cursor)
            if events:
                # Emit `id: <last_event_id>` on every events frame so the
                # browser sets Last-Event-ID on auto-reconnect, letting us
                # resume from there without re-streaming the backlog.
                payload = json.dumps({"events": events, "cursor": cursor})
                frame = (
                    f"id: {cursor}\nevent: events\ndata: {payload}\n\n"
                ).encode("utf-8")
                try:
                    handler.wfile.write(frame)
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
                    return True
                last_heartbeat = time.monotonic()
            else:
                # Heartbeat keeps reverse proxies and the browser from
                # closing an idle stream. SSE comments (lines starting
                # with `:`) are ignored by EventSource.
                if (time.monotonic() - last_heartbeat) >= _KANBAN_SSE_HEARTBEAT_SECONDS:
                    try:
                        handler.wfile.write(b": keepalive\n\n")
                        handler.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
                        return True
                    last_heartbeat = time.monotonic()
            time.sleep(_KANBAN_SSE_POLL_SECONDS)
    except Exception:
        # Any other unexpected exception in the SSE loop should not bubble
        # up to the request handler (which would 500 a long-lived stream).
        return True


def handle_kanban_get(handler, parsed) -> bool:
    path = parsed.path
    try:
        # Multi-board management endpoints — these do NOT take a board arg
        # because they operate on the on-disk board collection itself, not
        # on a single board's tasks.
        if path == "/api/kanban/boards":
            return j(handler, _list_boards_payload(parsed)) or True
        if path == "/api/kanban/board":
            return j(handler, _board_payload(parsed)) or True
        if path == "/api/kanban/config":
            return j(handler, _config_payload(board=_resolve_board(parsed))) or True
        if path == "/api/kanban/stats":
            return j(handler, _stats_payload(board=_resolve_board(parsed))) or True
        if path == "/api/kanban/assignees":
            return j(handler, _assignees_payload(board=_resolve_board(parsed))) or True
        if path == "/api/kanban/events":
            return j(handler, _events_payload(parsed)) or True
        if path == "/api/kanban/events/stream":
            return _handle_events_sse_stream(handler, parsed)
        if path.startswith(_TASK_PREFIX) and path.endswith("/log"):
            task_id = unquote(path[len(_TASK_PREFIX):-len("/log")]).strip("/")
            if not task_id or "/" in task_id:
                return False
            payload = _task_log_payload(parsed, task_id)
            if payload is None:
                return bad(handler, "task not found", status=404)
            return j(handler, payload) or True
        if path.startswith(_TASK_PREFIX):
            task_id = unquote(path[len(_TASK_PREFIX):]).strip("/")
            if not task_id or "/" in task_id:
                return False
            payload = _task_detail_payload(task_id, board=_resolve_board(parsed))
            if payload is None:
                return bad(handler, "task not found", status=404)
            return j(handler, payload) or True
        return False
    except ImportError as exc:
        # hermes_cli not installed (webui-only deploy). Return a clean 503
        # "kanban unavailable" rather than a 500 so the frontend's existing
        # try/catch surfaces a useful toast.
        return bad(handler, f"kanban unavailable: {exc}", status=503)
    except LookupError as exc:
        return bad(handler, str(exc), status=404)
    except ValueError as exc:
        return bad(handler, str(exc))
    except RuntimeError as exc:
        return bad(handler, str(exc), status=409)


def handle_kanban_post(handler, parsed, body) -> bool:
    path = parsed.path
    try:
        # Multi-board management endpoints — `_create_board_payload` and
        # `_switch_board_payload` operate on the on-disk board collection,
        # not on a single board's tasks.
        if path == "/api/kanban/boards":
            return j(handler, _create_board_payload(body)) or True
        # POST /api/kanban/boards/<slug>/switch — set active board
        _BOARDS_PREFIX = "/api/kanban/boards/"
        if path.startswith(_BOARDS_PREFIX) and path.endswith("/switch"):
            slug = unquote(path[len(_BOARDS_PREFIX):-len("/switch")]).strip("/")
            if not slug or "/" in slug:
                return False
            return j(handler, _switch_board_payload(slug)) or True
        # All board-scoped writes accept a ?board=<slug> query param OR a
        # `board` field in the JSON body. Query takes precedence.
        board_q = _resolve_board(parsed)
        board_b = _resolve_board_from_body(body)
        board = board_q if board_q is not None else board_b
        if path == "/api/kanban/dispatch":
            return j(handler, _dispatch_payload(parsed)) or True
        if path == "/api/kanban/tasks/bulk":
            return j(handler, _bulk_tasks_payload(body, board=board)) or True
        if path == "/api/kanban/tasks":
            return j(handler, _create_task_payload(body, board=board)) or True
        if path == "/api/kanban/links":
            return j(handler, _link_tasks_payload(body, board=board)) or True
        if path == "/api/kanban/links/delete":
            return j(handler, _link_tasks_payload(body, unlink=True, board=board)) or True
        if path.startswith(_TASK_PREFIX) and path.endswith("/comments"):
            task_id = path[len(_TASK_PREFIX):-len("/comments")].strip("/")
            return j(handler, _comment_payload(task_id, body, board=board)) or True
        for suffix, action in (("/block", "block"), ("/unblock", "unblock")):
            if path.startswith(_TASK_PREFIX) and path.endswith(suffix):
                task_id = path[len(_TASK_PREFIX):-len(suffix)].strip("/")
                return j(handler, _task_action_payload(task_id, body, action, board=board)) or True
        if path.startswith(_TASK_PREFIX) and path.endswith("/patch"):
            task_id = path[len(_TASK_PREFIX):-len("/patch")].strip("/")
            return j(handler, _patch_task_payload(task_id, body, board=board)) or True
    except ImportError as exc:
        return bad(handler, f"kanban unavailable: {exc}", status=503)
    except LookupError as exc:
        return bad(handler, str(exc), status=404)
    except ValueError as exc:
        return bad(handler, str(exc))
    except RuntimeError as exc:
        return bad(handler, str(exc), status=409)
    return False


def handle_kanban_patch(handler, parsed, body) -> bool:
    path = parsed.path
    try:
        # /boards/<slug> routes operate on the on-disk board collection
        # itself — the slug travels in the URL path, not via ?board=. Match
        # them BEFORE resolving the board param so a stray ?board=ghost in
        # the query string doesn't 404 the legitimate `experiments` rename.
        # (Mirrors handle_kanban_post's structure — fixes asymmetry caught
        # by Opus advisor.)
        _BOARDS_PREFIX = "/api/kanban/boards/"
        if path.startswith(_BOARDS_PREFIX):
            slug = unquote(path[len(_BOARDS_PREFIX):]).strip("/")
            if not slug or "/" in slug:
                return False
            return j(handler, _update_board_payload(slug, body)) or True
        # Task-scoped writes accept ?board=<slug> (or body.board) to pin the
        # write to a specific board. Query takes precedence over body.
        board_q = _resolve_board(parsed)
        board_b = _resolve_board_from_body(body)
        board = board_q if board_q is not None else board_b
        if path.startswith(_TASK_PREFIX):
            task_id = unquote(path[len(_TASK_PREFIX):]).strip("/")
            if not task_id or "/" in task_id:
                return False
            return j(handler, _patch_task_payload(task_id, body, board=board)) or True
    except ImportError as exc:
        return bad(handler, f"kanban unavailable: {exc}", status=503)
    except LookupError as exc:
        return bad(handler, str(exc), status=404)
    except ValueError as exc:
        return bad(handler, str(exc))
    except RuntimeError as exc:
        return bad(handler, str(exc), status=409)
    return False


def handle_kanban_delete(handler, parsed, body) -> bool:
    path = parsed.path
    try:
        # Same routing reorder as PATCH: /boards/<slug> path-routed first,
        # so a stray ?board=ghost can't 404 a legitimate board archive.
        _BOARDS_PREFIX = "/api/kanban/boards/"
        if path.startswith(_BOARDS_PREFIX):
            slug = unquote(path[len(_BOARDS_PREFIX):]).strip("/")
            if not slug or "/" in slug:
                return False
            return j(handler, _delete_board_payload(slug, parsed)) or True
        board_q = _resolve_board(parsed)
        board_b = _resolve_board_from_body(body)
        board = board_q if board_q is not None else board_b
        if path == "/api/kanban/links":
            return j(handler, _link_tasks_payload(body, unlink=True, board=board)) or True
    except ImportError as exc:
        return bad(handler, f"kanban unavailable: {exc}", status=503)
    except LookupError as exc:
        return bad(handler, str(exc), status=404)
    except ValueError as exc:
        return bad(handler, str(exc))
    except RuntimeError as exc:
        return bad(handler, str(exc), status=409)
    return False
