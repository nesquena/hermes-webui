"""Regression tests for manual WebUI cron runs."""

import sys
import types


def test_manual_cron_run_saves_output_and_marks_job(monkeypatch):
    import api.routes as routes

    calls = []

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []

    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.save_job_output = lambda job_id, output: calls.append(
        ("save", job_id, output)
    )
    cron_jobs.mark_job_run = lambda job_id, success, error=None: calls.append(
        ("mark", job_id, success, error)
    )

    cron_scheduler = types.ModuleType("cron.scheduler")
    cron_scheduler.run_job = lambda job: (True, "manual output", "done", None)

    monkeypatch.setattr(
        routes,
        "_run_cron_job_in_profile_subprocess",
        lambda job, execution_profile_home: cron_scheduler.run_job(job),
    )
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)
    monkeypatch.setitem(sys.modules, "cron.scheduler", cron_scheduler)

    routes._mark_cron_running("job123")
    routes._run_cron_tracked({"id": "job123"})

    assert calls == [
        ("save", "job123", "manual output"),
        ("mark", "job123", True, None),
    ]
    assert routes._is_cron_running("job123") == (False, 0.0)


def test_manual_cron_run_marks_empty_response_as_failure(monkeypatch):
    import api.routes as routes

    calls = []

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []

    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.save_job_output = lambda job_id, output: calls.append(
        ("save", job_id, output)
    )
    cron_jobs.mark_job_run = lambda job_id, success, error=None: calls.append(
        ("mark", job_id, success, error)
    )

    cron_scheduler = types.ModuleType("cron.scheduler")
    cron_scheduler.run_job = lambda job: (True, "manual output", "", None)

    monkeypatch.setattr(
        routes,
        "_run_cron_job_in_profile_subprocess",
        lambda job, execution_profile_home: cron_scheduler.run_job(job),
    )
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)
    monkeypatch.setitem(sys.modules, "cron.scheduler", cron_scheduler)

    routes._mark_cron_running("job-empty")
    routes._run_cron_tracked({"id": "job-empty"})

    assert calls[0] == ("save", "job-empty", "manual output")
    assert calls[1][0:3] == ("mark", "job-empty", False)
    assert "empty response" in calls[1][3]
    assert routes._is_cron_running("job-empty") == (False, 0.0)
