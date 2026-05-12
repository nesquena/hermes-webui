"""Neo Dashboard summary helpers."""

import time
from pathlib import Path
from typing import Any

from api.projects import load_project_store


def _as_ts(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _project_status(project: dict) -> str:
    return str(project.get("status") or project.get("state") or "backlog").strip().lower()


def _is_active_project(project: dict) -> bool:
    if project.get("archived"):
        return False
    return _project_status(project) != "arquivado"


def _task_status(task: dict) -> str:
    return str(task.get("status") or "backlog").strip().lower()


def _is_active_task(task: dict) -> bool:
    return not bool(task.get("archived"))


def _first_day_of_month(ts: float) -> float:
    local = time.localtime(ts)
    return time.mktime((local.tm_year, local.tm_mon, 1, 0, 0, 0, 0, 0, -1))


def _process_cmdlines() -> list[str]:
    """Read command lines of all running processes via /proc."""
    cmdlines = []
    proc = Path("/proc")
    for pid_dir in proc.iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            raw = (pid_dir / "cmdline").read_bytes()
        except Exception:
            continue
        if not raw:
            continue
        cmd = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()
        if cmd:
            cmdlines.append(cmd)
    return cmdlines


def build_agents_health_summary() -> dict:
    """Calculate real health of operational components."""
    cmdlines = _process_cmdlines()
    lower = [c.lower() for c in cmdlines]

    components = [
        {"id": "webui", "online": True, "source": "current_process"},
        {
            "id": "gateway",
            "online": any(
                ("gateway" in c and "hermes" in c) or "gateway/run.py" in c
                for c in lower
            ),
            "source": "procfs",
        },
        {
            "id": "cron",
            "online": any(
                ("cron" in c or "scheduler" in c) and "hermes" in c
                for c in lower
            ),
            "source": "procfs",
        },
        {
            "id": "subagents",
            "online": any("subagent" in c or "delegate" in c for c in lower),
            "source": "procfs",
        },
    ]

    return {
        "online": sum(1 for c in components if c["online"]),
        "total": len(components),
        "components": components,
    }


def build_dashboard_summary() -> dict:
    """Build KPI summary from real project/task data (schema v2)."""
    now = time.time()
    month_start = _first_day_of_month(now)
    yesterday = now - 86400
    week_ago = now - (7 * 86400)

    store = load_project_store()
    projects = [p for p in store.get("projects", []) if isinstance(p, dict)]
    tasks = [t for t in store.get("tasks", []) if isinstance(t, dict)]

    active_projects = [p for p in projects if _is_active_project(p)]
    in_progress_tasks = [
        t for t in tasks
        if _is_active_task(t) and _task_status(t) == "em_andamento"
    ]
    completed_recent_tasks = [
        t for t in tasks
        if _is_active_task(t)
        and _task_status(t) == "concluido"
        and _as_ts(t.get("completed_at") or t.get("updated_at") or t.get("created_at")) >= week_ago
    ]

    active_delta = sum(
        1 for p in active_projects if _as_ts(p.get("created_at")) >= month_start
    )
    progress_delta = sum(
        1 for t in in_progress_tasks
        if _as_ts(t.get("updated_at") or t.get("created_at")) >= yesterday
    )
    completed_delta = len(completed_recent_tasks)

    agents = build_agents_health_summary()

    return {
        "updated_at": now,
        "cards": [
            {
                "id": "active_projects",
                "label_key": "kpi_active_projects",
                "value": len(active_projects),
                "delta_key": "kpi_delta_this_month",
                "delta_value": active_delta,
                "panel": "projects",
            },
            {
                "id": "tasks_in_progress",
                "label_key": "kpi_tasks_in_progress",
                "value": len(in_progress_tasks),
                "delta_key": "kpi_delta_since_yesterday",
                "delta_value": progress_delta,
                "panel": "projects",
            },
            {
                "id": "completed",
                "label_key": "kpi_completed",
                "value": len(completed_recent_tasks),
                "delta_key": "kpi_delta_this_week",
                "delta_value": completed_delta,
                "panel": "projects",
            },
            {
                "id": "agents_online",
                "label_key": "kpi_agents_online",
                "value": agents["online"],
                "delta_key": "kpi_all_operational",
                "delta_value": None,
                "panel": "skills",
            },
        ],
        "agents": agents,
    }
