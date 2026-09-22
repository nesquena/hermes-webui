"""Regression tests for #3134: expose recent cron execution health on /api/crons."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.cron_health import (
    RECENT_FAILED_RATIO,
    RECENT_MIN_TOTAL,
    is_recently_degraded,
    recent_rollups_for_jobs,
)


ROOT = Path(__file__).resolve().parent.parent
PANELS_JS = ROOT / "static" / "panels.js"
NODE = shutil.which("node")


def _cron_helper_source() -> str:
    src = PANELS_JS.read_text(encoding="utf-8")
    start = src.index("function _isRecurringCronJob")
    end = src.index("async function loadCrons", start)
    return src[start:end]


def _run_node(script: str) -> str:
    proc = subprocess.run(
        [NODE, "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _create_executions_db(db_path: Path, rows: list[tuple]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE executions (
              id TEXT PRIMARY KEY,
              job_id TEXT NOT NULL,
              source TEXT NOT NULL,
              process_id TEXT NOT NULL,
              pid INTEGER NOT NULL,
              process_started_at INTEGER,
              status TEXT NOT NULL,
              claimed_at TEXT NOT NULL,
              started_at TEXT,
              finished_at TEXT,
              error TEXT
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO executions (
              id, job_id, source, process_id, pid, status,
              claimed_at, started_at, finished_at, error
            ) VALUES (?, ?, 'scheduler', 'proc', 1, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def _iso(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


@pytest.fixture
def flapping_job_db(tmp_path: Path) -> tuple[Path, str]:
    job_id = "flap-job"
    rows = []
    for idx in range(9):
        rows.append(
            (
                f"fail-{idx}",
                job_id,
                "failed",
                _iso(600 - idx),
                _iso(600 - idx),
                _iso(599 - idx),
                "Script exited with code 1",
            )
        )
    rows.append(
        (
            "ok-latest",
            job_id,
            "completed",
            _iso(1),
            _iso(1),
            _iso(0),
            None,
        )
    )
    db_path = tmp_path / "cron" / "executions.db"
    _create_executions_db(db_path, rows)
    return tmp_path, job_id


def test_recent_rollup_counts_failures_despite_latest_success(flapping_job_db):
    home, job_id = flapping_job_db
    rollups = recent_rollups_for_jobs(home, [job_id])
    recent = rollups[job_id]

    assert recent["total"] == 10
    assert recent["failed"] == 9
    assert recent["consecutive_failures"] == 0
    assert is_recently_degraded(recent) is True


def test_recent_rollup_ignores_single_failure_in_large_window(tmp_path: Path):
    job_id = "mostly-ok"
    rows = []
    for idx in range(8):
        rows.append(
            (
                f"ok-{idx}",
                job_id,
                "completed",
                _iso(120 - idx),
                _iso(120 - idx),
                _iso(119 - idx),
                None,
            )
        )
    rows.append(
        (
            "fail-once",
            job_id,
            "failed",
            _iso(30),
            _iso(30),
            _iso(29),
            "boot blip",
        )
    )
    _create_executions_db(tmp_path / "cron" / "executions.db", rows)
    recent = recent_rollups_for_jobs(tmp_path, [job_id])[job_id]

    assert recent["total"] == 9
    assert recent["failed"] == 1
    assert is_recently_degraded(recent) is False


def test_api_crons_attaches_recent_rollups(monkeypatch, flapping_job_db):
    import api.profiles as profiles
    import api.routes as routes

    home, job_id = flapping_job_db
    current_home = {"value": None}
    jobs_by_home = {
        str(home): [
            {
                "id": job_id,
                "name": "Flapping job",
                "enabled": True,
                "state": "scheduled",
                "last_status": "ok",
            }
        ],
    }

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.list_jobs = lambda include_disabled=True: [
        dict(jobs_by_home[str(home)][0])
    ]
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)

    class _Ctx:
        def __init__(self, profile_home):
            self.profile_home = profile_home
            self.prev = None

        def __enter__(self):
            self.prev = current_home["value"]
            current_home["value"] = str(self.profile_home)
            return self

        def __exit__(self, exc_type, exc, tb):
            current_home["value"] = self.prev
            return False

    handler = SimpleNamespace(
        status=None,
        response_headers=[],
        wfile=BytesIO(),
        send_response=lambda status: setattr(handler, "status", status),
        send_header=lambda key, value: handler.response_headers.append((key, value)),
        end_headers=lambda: None,
    )

    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "alpha")
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [{"name": "alpha", "visible": True}])
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _name: home)
    monkeypatch.setattr(profiles, "cron_profile_context_for_home", _Ctx)

    assert routes.handle_get(handler, SimpleNamespace(path="/api/crons", query="")) is not False
    body = json.loads(handler.wfile.getvalue().decode("utf-8"))

    assert handler.status == 200
    job = body["jobs"][0]
    assert job["last_status"] == "ok"
    assert job["recent"]["failed"] == 9
    assert job["recent"]["total"] == 10
    assert is_recently_degraded(job["recent"]) is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_cron_status_meta_marks_flapping_job_degraded_not_active():
    script = _cron_helper_source() + r"""
function t(key){ return key; }
const healthy = {
  id: 'healthy',
  enabled: true,
  state: 'scheduled',
  last_status: 'ok',
  recent: { window_hours: 24, total: 10, failed: 1, consecutive_failures: 0 },
};
const flapping = {
  id: 'flapping',
  enabled: true,
  state: 'scheduled',
  last_status: 'ok',
  recent: { window_hours: 24, total: 10, failed: 9, consecutive_failures: 0 },
};
console.log(JSON.stringify({
  healthy: _cronStatusMeta(healthy),
  flapping: _cronStatusMeta(flapping),
}));
"""
    states = json.loads(_run_node(script))

    assert states["healthy"]["state"] == "active"
    assert states["healthy"]["listClass"] == "active"
    assert states["flapping"]["state"] == "degraded"
    assert states["flapping"]["listClass"] == "attention"
    assert states["flapping"]["label"] == "cron_status_degraded"


def test_recent_degraded_threshold_constants_match_ticket_direction():
    assert RECENT_MIN_TOTAL >= 5
    assert RECENT_FAILED_RATIO >= 0.5
