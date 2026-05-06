import multiprocessing
import sys
import threading
import types
from pathlib import Path


def _install_fake_cron(monkeypatch, run_job, events):
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []

    cron_jobs = types.ModuleType("cron.jobs")
    cron_jobs.HERMES_DIR = Path("/tmp/hermes")
    cron_jobs.CRON_DIR = cron_jobs.HERMES_DIR / "cron"
    cron_jobs.JOBS_FILE = cron_jobs.CRON_DIR / "jobs.json"
    cron_jobs.OUTPUT_DIR = cron_jobs.CRON_DIR / "output"
    cron_jobs.save_job_output = lambda job_id, output: events.append(("save", job_id, output))
    cron_jobs.mark_job_run = lambda job_id, success, error=None: events.append(("mark", job_id, success, error))

    cron_scheduler = types.ModuleType("cron.scheduler")
    cron_scheduler._hermes_home = Path("/tmp/hermes")
    cron_scheduler._LOCK_DIR = cron_scheduler._hermes_home / "cron"
    cron_scheduler._LOCK_FILE = cron_scheduler._LOCK_DIR / ".tick.lock"
    cron_scheduler.run_job = run_job

    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)
    monkeypatch.setitem(sys.modules, "cron.scheduler", cron_scheduler)
    return cron_jobs, cron_scheduler


def _write_fake_large_payload_cron_package(root: Path):
    cron_dir = root / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "__init__.py").write_text("", encoding="utf-8")
    (cron_dir / "jobs.py").write_text(
        "from pathlib import Path\n"
        "HERMES_DIR = Path('/tmp/hermes')\n"
        "CRON_DIR = HERMES_DIR / 'cron'\n"
        "JOBS_FILE = CRON_DIR / 'jobs.json'\n"
        "OUTPUT_DIR = CRON_DIR / 'output'\n",
        encoding="utf-8",
    )
    (cron_dir / "scheduler.py").write_text(
        "from pathlib import Path\n"
        "_hermes_home = Path('/tmp/hermes')\n"
        "_LOCK_DIR = _hermes_home / 'cron'\n"
        "_LOCK_FILE = _LOCK_DIR / '.tick.lock'\n"
        "def run_job(job):\n"
        "    payload = 'x' * 200_000\n"
        "    return True, payload, payload, None\n",
        encoding="utf-8",
    )


def _large_cron_payload_runner(fake_pkg_root, profile_home, result_queue):
    try:
        import api.routes as routes

        # api.routes/config may prepend the real hermes-agent path while importing.
        # Re-prepend the fake cron package afterward and clear any already-loaded
        # cron modules so the helper's child process imports the large-payload fake.
        sys.path.insert(0, str(fake_pkg_root))
        for module_name in ("cron.scheduler", "cron.jobs", "cron"):
            sys.modules.pop(module_name, None)

        success, output, final_response, error = routes._run_cron_job_in_profile_subprocess(
            {"id": "large-payload"}, Path(profile_home)
        )
        result_queue.put(("ok", success, len(output), len(final_response), error))
    except BaseException as exc:  # pragma: no cover - surfaced in parent process
        import traceback

        result_queue.put(("error", repr(exc), traceback.format_exc()))


def test_manual_cron_subprocess_drains_large_result_before_join(tmp_path):
    """A >100 KB result must not deadlock the parent before it can persist output."""
    fake_pkg_root = tmp_path / "fake-cron-pkg"
    _write_fake_large_payload_cron_package(fake_pkg_root)

    # Use fork only for the outer test harness so this pytest module does not
    # need to be importable as a package. The product helper under test owns its
    # own multiprocessing context.
    ctx = multiprocessing.get_context("fork")
    result_queue = ctx.Queue()
    runner = ctx.Process(
        target=_large_cron_payload_runner,
        args=(fake_pkg_root, tmp_path / "exec-profile", result_queue),
    )
    runner.start()
    runner.join(10)
    if runner.is_alive():
        runner.terminate()
        runner.join(5)
        result_queue.close()
        result_queue.join_thread()
        raise AssertionError(
            "manual cron subprocess deadlocked on a >100 KB Queue payload; "
            "the parent must drain result_queue before process.join()"
        )

    try:
        result = result_queue.get(timeout=2)
    finally:
        result_queue.close()
        result_queue.join_thread()
    assert result == ("ok", True, 200_000, 200_000, None)


def test_manual_cron_run_does_not_hold_profile_lock_for_job_duration(tmp_path, monkeypatch):
    """A long manual run must not freeze unrelated cron/profile operations.

    The parent WebUI process still needs the cron profile lock for short metadata
    writes, but the potentially minutes-long run_job body should execute outside
    that process-wide critical section.
    """
    import api.routes as routes
    from api.profiles import cron_profile_context_for_home

    events = []
    run_started = threading.Event()
    release_run = threading.Event()

    def fake_run_job_subprocess(job, execution_profile_home):
        events.append(("run", job["id"], str(execution_profile_home)))
        run_started.set()
        assert release_run.wait(2), "test timed out waiting to release fake cron run"
        return True, "output", "final", None

    _install_fake_cron(monkeypatch, lambda job: (True, "unused", "unused", None), events)
    monkeypatch.setattr(routes, "_run_cron_job_in_profile_subprocess", fake_run_job_subprocess)

    job_home = tmp_path / "owner"
    exec_home = tmp_path / "exec"
    other_home = tmp_path / "other"

    routes._mark_cron_running("job1574")
    worker = threading.Thread(
        target=routes._run_cron_tracked,
        args=({"id": "job1574"}, job_home, exec_home),
    )
    worker.start()
    assert run_started.wait(2), "fake run_job did not start"

    contender_entered = threading.Event()

    def contender():
        with cron_profile_context_for_home(other_home):
            events.append(("contender", str(other_home)))
            contender_entered.set()

    contender_thread = threading.Thread(target=contender)
    contender_thread.start()

    assert contender_entered.wait(0.5), (
        "cron_profile_context_for_home stayed blocked while run_job was active; "
        "the global cron profile lock is still held for the full job duration"
    )

    release_run.set()
    worker.join(2)
    contender_thread.join(2)

    assert not worker.is_alive()
    assert not contender_thread.is_alive()
    assert ("run", "job1574", str(exec_home)) in events
    assert ("save", "job1574", "output") in events
    assert ("mark", "job1574", True, None) in events
    assert routes._is_cron_running("job1574") == (False, 0.0)


def test_cron_job_subprocess_executes_under_selected_profile_home(tmp_path, monkeypatch):
    import api.routes as routes

    def fake_run_job(job):
        import cron.scheduler as scheduler

        return True, str(scheduler._hermes_home), "final", None

    events = []
    _, cron_scheduler = _install_fake_cron(monkeypatch, fake_run_job, events)
    exec_home = tmp_path / "exec-profile"

    success, output, final_response, error = routes._run_cron_job_in_profile_subprocess(
        {"id": "job1574"}, exec_home
    )

    assert success is True
    assert output == str(exec_home)
    assert final_response == "final"
    assert error is None
    assert cron_scheduler._hermes_home == Path("/tmp/hermes")
