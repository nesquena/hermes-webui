"""Regression tests for manual WebUI cron runs."""



def test_manual_cron_run_saves_output_and_marks_job(monkeypatch):
    import api.routes as routes

    calls = []

    cron_jobs = type("CronJobs", (), {})()
    cron_jobs.save_job_output = lambda job_id, output: calls.append(
        ("save", job_id, output)
    )
    cron_jobs.mark_job_run = lambda job_id, success, error=None: calls.append(
        ("mark", job_id, success, error)
    )

    monkeypatch.setitem(__import__("sys").modules, "cron.jobs", cron_jobs)
    monkeypatch.setattr(
        routes,
        "_run_cron_job_in_profile_subprocess",
        lambda job, execution_profile_home: (True, "manual output", "done", None),
    )

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

    cron_jobs = type("CronJobs", (), {})()
    cron_jobs.save_job_output = lambda job_id, output: calls.append(
        ("save", job_id, output)
    )
    cron_jobs.mark_job_run = lambda job_id, success, error=None: calls.append(
        ("mark", job_id, success, error)
    )

    monkeypatch.setitem(__import__("sys").modules, "cron.jobs", cron_jobs)
    monkeypatch.setattr(
        routes,
        "_run_cron_job_in_profile_subprocess",
        lambda job, execution_profile_home: (True, "manual output", "", None),
    )

    routes._mark_cron_running("job-empty")
    routes._run_cron_tracked({"id": "job-empty"})

    assert calls[0] == ("save", "job-empty", "manual output")
    assert calls[1][0:3] == ("mark", "job-empty", False)
    assert "empty response" in calls[1][3]
    assert routes._is_cron_running("job-empty") == (False, 0.0)
