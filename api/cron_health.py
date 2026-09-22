"""Read-only cron execution health rollups from executions.db."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

RECENT_WINDOW_HOURS = 24
RECENT_MIN_TOTAL = 5
RECENT_FAILED_RATIO = 0.5
RECENT_CONSECUTIVE_FAILURES = 3


def _executions_db_path(hermes_home: Path) -> Path:
    return Path(hermes_home).expanduser() / "cron" / "executions.db"


def _window_cutoff_iso(window_hours: int) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    return cutoff.isoformat()


def recent_rollups_for_jobs(
    hermes_home: Path,
    job_ids: list[str],
    *,
    window_hours: int = RECENT_WINDOW_HOURS,
) -> dict[str, dict]:
    """Return recent execution stats keyed by job_id."""
    ids = [str(job_id).strip() for job_id in job_ids if str(job_id or "").strip()]
    if not ids:
        return {}

    db_path = _executions_db_path(hermes_home)
    if not db_path.is_file():
        return {}

    cutoff = _window_cutoff_iso(window_hours)
    placeholders = ",".join("?" * len(ids))
    query = f"""
        SELECT job_id, status, claimed_at, id
        FROM executions
        WHERE job_id IN ({placeholders})
          AND COALESCE(started_at, claimed_at) >= ?
        ORDER BY job_id, claimed_at DESC, id DESC
    """

    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            rows = conn.execute(query, (*ids, cutoff)).fetchall()
    except sqlite3.Error as exc:
        logger.debug("cron executions rollup unavailable for %s: %s", db_path, exc)
        return {}

    return _rollup_rows(rows, job_ids=ids, window_hours=window_hours)


def _rollup_rows(
    rows: list[tuple],
    *,
    job_ids: list[str],
    window_hours: int,
) -> dict[str, dict]:
    grouped: dict[str, list[str]] = {}
    for job_id, status, _claimed_at, _row_id in rows:
        grouped.setdefault(str(job_id), []).append(str(status))

    empty = {
        "window_hours": window_hours,
        "total": 0,
        "failed": 0,
        "consecutive_failures": 0,
    }
    out = {job_id: dict(empty) for job_id in job_ids}
    for job_id, statuses in grouped.items():
        total = len(statuses)
        failed = sum(1 for status in statuses if status == "failed")
        consecutive = 0
        for status in statuses:
            if status == "failed":
                consecutive += 1
            else:
                break
        out[job_id] = {
            "window_hours": window_hours,
            "total": total,
            "failed": failed,
            "consecutive_failures": consecutive,
        }
    return out


def is_recently_degraded(recent: dict | None) -> bool:
    if not isinstance(recent, dict):
        return False
    total = int(recent.get("total") or 0)
    failed = int(recent.get("failed") or 0)
    consecutive = int(recent.get("consecutive_failures") or 0)
    if consecutive >= RECENT_CONSECUTIVE_FAILURES:
        return True
    if total >= RECENT_MIN_TOTAL and failed / total >= RECENT_FAILED_RATIO:
        return True
    return False
