"""Neo local-first project command center storage."""

import json
import re
import time
import uuid
from copy import deepcopy
from typing import Any

from api.config import PROJECTS_FILE

SCHEMA_VERSION = 2
PROJECT_STATUS_VALUES = {"ativo", "arquivado"}
TASK_STATUS_VALUES = {"backlog", "em_andamento", "em_revisao", "concluido"}
PRIORITY_VALUES = {"baixa", "media", "alta"}
DEFAULT_TASK_STATUS = "backlog"
DEFAULT_PRIORITY = "media"
DEFAULT_CATEGORY = "Docs"
DEFAULT_OWNER = "jr"
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def _now() -> float:
    return time.time()


def _empty_store() -> dict:
    return {"schema_version": SCHEMA_VERSION, "sources": [], "projects": [], "tasks": [], "activity": []}


def _normalize_refs(value: Any) -> dict:
    refs = value if isinstance(value, dict) else {}
    return {
        "github": list(refs.get("github") or []),
        "obsidian": list(refs.get("obsidian") or []),
        "sessions": list(refs.get("sessions") or []),
    }


def _normalize_external_ref(value: Any) -> dict | None:
    if not isinstance(value, dict) or not value:
        return None
    return {
        "type": str(value.get("type") or "local").strip().lower() or "local",
        "source_id": value.get("source_id") or None,
        "key": str(value.get("key") or "").strip(),
        "url": str(value.get("url") or "").strip(),
        "status": str(value.get("status") or "").strip(),
        "synced_at": value.get("synced_at") if value.get("synced_at") else None,
    }


def _normalize_project(project: Any) -> dict | None:
    if not isinstance(project, dict):
        return None
    name = str(project.get("name") or "").strip()
    if not name:
        return None
    created_at = float(project.get("created_at") or _now())
    updated_at = float(project.get("updated_at") or created_at)
    archived = bool(project.get("archived") or project.get("status") == "arquivado")
    color = project.get("color") or "#00E5FF"
    if color and not _HEX_COLOR_RE.match(str(color)):
        color = "#00E5FF"
    return {
        "project_id": str(project.get("project_id") or project.get("id") or f"prj_{uuid.uuid4().hex[:12]}"),
        "name": name[:128],
        "description": str(project.get("description") or "")[:1000],
        "domain": str(project.get("domain") or "projetos")[:64],
        "status": "arquivado" if archived else str(project.get("status") or "ativo"),
        "color": color,
        "default_source_id": project.get("default_source_id") or None,
        "refs": _normalize_refs(project.get("refs")),
        "created_at": created_at,
        "updated_at": updated_at,
        "archived": archived,
    }


def _normalize_task(task: Any) -> dict | None:
    if not isinstance(task, dict):
        return None
    title = str(task.get("title") or "").strip()
    if not title:
        return None
    status = str(task.get("status") or DEFAULT_TASK_STATUS).strip().lower()
    if status not in TASK_STATUS_VALUES:
        status = DEFAULT_TASK_STATUS
    priority = str(task.get("priority") or DEFAULT_PRIORITY).strip().lower()
    if priority not in PRIORITY_VALUES:
        priority = DEFAULT_PRIORITY
    try:
        progress = max(0, min(100, int(task.get("progress") or 0)))
    except (TypeError, ValueError):
        progress = 0
    created_at = float(task.get("created_at") or _now())
    updated_at = float(task.get("updated_at") or created_at)
    return {
        "task_id": str(task.get("task_id") or task.get("id") or f"tsk_{uuid.uuid4().hex[:12]}"),
        "project_id": str(task.get("project_id") or ""),
        "title": title[:180],
        "description": str(task.get("description") or "")[:2000],
        "status": status,
        "priority": priority,
        "category": str(task.get("category") or DEFAULT_CATEGORY)[:64],
        "owner": str(task.get("owner") or DEFAULT_OWNER)[:64],
        "progress": progress,
        "due_date": str(task.get("due_date") or "")[:32],
        "external_ref": _normalize_external_ref(task.get("external_ref")),
        "refs": _normalize_refs(task.get("refs")),
        "created_at": created_at,
        "updated_at": updated_at,
        "archived": bool(task.get("archived", False)),
    }


def _normalize_source(source: Any) -> dict | None:
    if not isinstance(source, dict):
        return None
    name = str(source.get("name") or "").strip()
    if not name:
        return None
    return {
        "source_id": str(source.get("source_id") or f"src_{uuid.uuid4().hex[:8]}"),
        "type": str(source.get("type") or "local").strip().lower(),
        "name": name[:128],
        "base_url": str(source.get("base_url") or "").strip(),
        "sync_enabled": bool(source.get("sync_enabled", False)),
    }


def _normalize_store(raw: Any) -> dict:
    store = _empty_store()
    if isinstance(raw, list):
        projects, sources, tasks, activity = raw, [], [], []
    elif isinstance(raw, dict):
        projects = raw.get("projects") if isinstance(raw.get("projects"), list) else []
        sources = raw.get("sources") if isinstance(raw.get("sources"), list) else []
        tasks = raw.get("tasks") if isinstance(raw.get("tasks"), list) else []
        activity = raw.get("activity") if isinstance(raw.get("activity"), list) else []
    else:
        projects, sources, tasks, activity = [], [], [], []
    store["projects"] = [p for p in (_normalize_project(p) for p in projects) if p]
    store["tasks"] = [t for t in (_normalize_task(t) for t in tasks) if t]
    store["sources"] = [s for s in (_normalize_source(s) for s in sources) if s]
    store["activity"] = [a for a in activity if isinstance(a, dict)]
    return store


def load_project_store() -> dict:
    if not PROJECTS_FILE.exists():
        return _empty_store()
    try:
        raw = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        raw = _empty_store()
    return _normalize_store(raw)


def save_project_store(store: dict) -> dict:
    normalized = _normalize_store(store)
    PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROJECTS_FILE.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized


def _status_counts(tasks: list[dict]) -> dict:
    by_status = {"backlog": 0, "em_andamento": 0, "em_revisao": 0, "concluido": 0}
    for task in tasks:
        if not task.get("archived"):
            by_status[task["status"]] = by_status.get(task["status"], 0) + 1
    return {"total": sum(by_status.values()), "by_status": by_status}


def snapshot(*, include_archived: bool = False) -> dict:
    """Return the v2 snapshot used by the Projects Command Center.

    By default archived projects/tasks are filtered out so KPIs and
    counts reflect only active work. Pass ``include_archived=True`` to
    receive everything (used by the "Mostrar arquivados" toggle).
    """
    store = load_project_store()
    if include_archived:
        tasks = list(store["tasks"])
        projects = list(store["projects"])
    else:
        tasks = [t for t in store["tasks"] if not t.get("archived")]
        projects = [p for p in store["projects"] if not p.get("archived")]
    return {
        "schema_version": SCHEMA_VERSION,
        "projects": projects,
        "tasks": tasks,
        "sources": store["sources"],
        # Counts always reflect ACTIVE tasks regardless of include_archived,
        # so the status pills don't suddenly inflate when the user toggles
        # "show archived" — archived items get a badge but don't count.
        "counts": _status_counts([t for t in store["tasks"] if not t.get("archived")]),
    }


def legacy_project_list() -> list[dict]:
    return deepcopy(load_project_store()["projects"])


def create_project(body: dict, *, legacy: bool = False) -> dict:
    """Create a project.

    When ``legacy=True`` the project_id is generated in the upstream 12-char
    hex format (no prefix) so the legacy /api/projects/create contract is
    preserved. New Neo callers use ``prj_...`` prefixed IDs.
    """
    name = str(body.get("name") or "").strip()
    if not name:
        raise ValueError("name required")
    color = body.get("color") or "#00E5FF"
    if color and not _HEX_COLOR_RE.match(str(color)):
        raise ValueError("Invalid color format")
    now = _now()
    pid = uuid.uuid4().hex[:12] if legacy else f"prj_{uuid.uuid4().hex[:12]}"
    project = _normalize_project({
        "project_id": pid,
        "name": name,
        "description": body.get("description") or "",
        "domain": body.get("domain") or "projetos",
        "status": body.get("status") or "ativo",
        "color": color,
        "default_source_id": body.get("default_source_id") or None,
        "refs": body.get("refs") or {},
        "created_at": now,
        "updated_at": now,
        "archived": False,
    })
    store = load_project_store()
    store["projects"].append(project)
    save_project_store(store)
    return project


def update_project(project_id: str, body: dict) -> dict:
    store = load_project_store()
    project = next((p for p in store["projects"] if p["project_id"] == project_id), None)
    if not project:
        raise KeyError("Project not found")
    if "name" in body:
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("name required")
        project["name"] = name[:128]
    for field in ("description", "domain", "default_source_id"):
        if field in body:
            project[field] = str(body.get(field) or "").strip()[:1000 if field == "description" else 128]
    if "color" in body:
        color = body.get("color") or "#00E5FF"
        if color and not _HEX_COLOR_RE.match(str(color)):
            raise ValueError("Invalid color format")
        project["color"] = color
    if "status" in body:
        status = str(body.get("status") or "").strip().lower()
        if status not in PROJECT_STATUS_VALUES:
            raise ValueError("Invalid project status")
        project["status"] = status
        project["archived"] = status == "arquivado"
    project["updated_at"] = _now()
    save_project_store(store)
    return project


def delete_project(project_id: str) -> bool:
    store = load_project_store()
    before = len(store["projects"])
    store["projects"] = [p for p in store["projects"] if p["project_id"] != project_id]
    if len(store["projects"]) == before:
        raise KeyError("Project not found")
    for task in store["tasks"]:
        if task.get("project_id") == project_id:
            task["archived"] = True
            task["updated_at"] = _now()
    save_project_store(store)
    return True


def create_task(body: dict) -> dict:
    title = str(body.get("title") or "").strip()
    if not title:
        raise ValueError("title required")
    status = str(body.get("status") or DEFAULT_TASK_STATUS).strip().lower()
    if status not in TASK_STATUS_VALUES:
        raise ValueError("Invalid task status")
    project_id = str(body.get("project_id") or "").strip()
    store = load_project_store()
    if not project_id:
        raise ValueError("project_id required")
    if not any(p["project_id"] == project_id for p in store["projects"]):
        raise ValueError("project_id not found")
    now = _now()
    task = _normalize_task({**body, "task_id": f"tsk_{uuid.uuid4().hex[:12]}", "status": status, "created_at": now, "updated_at": now})
    store["tasks"].append(task)
    save_project_store(store)
    return task


def update_task(task_id: str, body: dict) -> dict:
    store = load_project_store()
    task = next((t for t in store["tasks"] if t["task_id"] == task_id), None)
    if not task:
        raise KeyError("Task not found")
    if "status" in body:
        status = str(body.get("status") or "").strip().lower()
        if status not in TASK_STATUS_VALUES:
            raise ValueError("Invalid task status")
        task["status"] = status
    if "project_id" in body:
        project_id = str(body.get("project_id") or "").strip()
        if not project_id:
            raise ValueError("project_id required")
        if not any(p["project_id"] == project_id for p in store["projects"]):
            raise ValueError("project_id not found")
        task["project_id"] = project_id
    for field in ("title", "description", "category", "priority", "owner", "due_date"):
        if field in body:
            task[field] = str(body.get(field) or "").strip()
    if task.get("priority") not in PRIORITY_VALUES:
        raise ValueError("Invalid priority")
    if "progress" in body:
        try:
            task["progress"] = max(0, min(100, int(body.get("progress") or 0)))
        except (TypeError, ValueError):
            raise ValueError("Invalid progress")
    if "external_ref" in body:
        task["external_ref"] = _normalize_external_ref(body.get("external_ref"))
    if "refs" in body:
        task["refs"] = _normalize_refs(body.get("refs"))
    if "archived" in body:
        task["archived"] = bool(body.get("archived"))
    task["updated_at"] = _now()
    save_project_store(store)
    return task


def archive_task(task_id: str) -> dict:
    """Mark a task as archived. Idempotent. Raises KeyError if not found."""
    store = load_project_store()
    task = next((t for t in store["tasks"] if t["task_id"] == task_id), None)
    if not task:
        raise KeyError("Task not found")
    task["archived"] = True
    task["updated_at"] = _now()
    save_project_store(store)
    return task


# ── Legacy compatibility helpers ─────────────────────────────────────────────
# The upstream code (api/models.py and cron init) calls load_projects()/
# save_projects() expecting a flat list of project dicts. The new Neo schema
# v2 keeps the same on-disk file but wraps projects under {"projects": [...]}.
# These helpers let legacy callers keep working while the WebUI migrates.

def legacy_save_project_list(projects: list) -> None:
    """Persist a legacy flat project list while preserving v2 structure.

    Accepts the upstream list-of-dicts shape and merges it into the v2
    snapshot, keeping tasks/sources/activity intact.
    """
    store = load_project_store()
    normalized = [p for p in (_normalize_project(p) for p in (projects or [])) if p]
    store["projects"] = normalized
    save_project_store(store)
