"""Neo Dashboard summary helpers."""

import time
from typing import Any

from api.models import load_projects


def _as_ts(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _project_status(project: dict) -> str:
    return str(project.get("status") or project.get("state") or "backlog").strip().lower()


def _is_active_project(project: dict) -> bool:
    return _project_status(project) != "arquivado"


def _first_day_of_month(ts: float) -> float:
    local = time.localtime(ts)
    return time.mktime((local.tm_year, local.tm_mon, 1, 0, 0, 0, 0, 0, -1))


def build_dashboard_summary() -> dict:
    """Build the HU-03.4 KPI summary from local project data."""
    now = time.time()
    month_start = _first_day_of_month(now)
    yesterday = now - 86400
    week_ago = now - (7 * 86400)

    projects = [p for p in load_projects() if isinstance(p, dict)]
    active_projects = [p for p in projects if _is_active_project(p)]
    in_progress = [p for p in projects if _project_status(p) == "em_andamento"]
    completed = [p for p in projects if _project_status(p) == "concluido"]

    active_delta = sum(1 for p in active_projects if _as_ts(p.get("created_at")) >= month_start)
    progress_delta = sum(1 for p in in_progress if _as_ts(p.get("updated_at") or p.get("created_at")) >= yesterday)
    completed_delta = sum(1 for p in completed if _as_ts(p.get("completed_at") or p.get("updated_at") or p.get("created_at")) >= week_ago)

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
                "value": len(in_progress),
                "delta_key": "kpi_delta_since_yesterday",
                "delta_value": progress_delta,
                "panel": "projects",
            },
            {
                "id": "completed",
                "label_key": "kpi_completed",
                "value": len(completed),
                "delta_key": "kpi_delta_this_week",
                "delta_value": completed_delta,
                "panel": "projects",
            },
            {
                "id": "agents_online",
                "label_key": "kpi_agents_online",
                "value": 1,
                "delta_key": "kpi_all_operational",
                "delta_value": None,
                "panel": "skills",
            },
        ],
    }
