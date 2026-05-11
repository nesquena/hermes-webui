import multiprocessing
import os
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



def _write_spawn_fake_agent(root: Path, *, run_job_body: str):
    root.mkdir(parents=True, exist_ok=True)
    (root / "run_agent.py").write_text("", encoding="utf-8")
    cron_dir = root / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
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
        f"{run_job_body}",
        encoding="utf-8",
    )


def _activate_spawn_fake_agent(fake_agent_root: Path):
    fake_path = str(fake_agent_root)
    os.environ["HERMES_WEBUI_AGENT_DIR"] = fake_path
    existing = os.environ.get("PYTHONPATH", "")
    parts = [
        p
        for p in existing.split(os.pathsep)
        if p and ("hermes-agent" not in p or p == fake_path)
    ]
    os.environ["PYTHONPATH"] = os.pathsep.join([fake_path, *[p for p in parts if p != fake_path]])
    sys.path[:] = [
        p
        for p in sys.path
        if not p or "hermes-agent" not in p or p == fake_path
    ]
    if fake_path not in sys.path:
        sys.path.insert(0, fake_path)
    for module_name in (
        "cron.scheduler",
        "cron.jobs",
        "cron",
        "api.routes",
        "api.profiles",
        "api.config",
    ):
        sys.modules.pop(module_name, None)


def _real_hermes_agent_editable_install_present() -> bool:
    """Detect a developer-machine editable install of hermes-agent.

    The two tests that spawn a real subprocess + import the fake `cron.scheduler`
    from ``HERMES_WEBUI_AGENT_DIR`` only work when the spawn child does NOT have
    a competing real `cron.scheduler` reachable via the venv's editable finder.
    On CI runners (and most production installs) there's no editable install,
    so the fake at ``fake_agent_root`` is the only `cron.scheduler` Python can
    resolve; on a maintainer's dev machine an editable install of hermes-agent
    is registered through a `.pth` file in site-packages, and the spawn child
    will resolve the real `cron.scheduler` first — which then fails because the
    real `run_job` requires a configured inference provider.

    Detection strategy: ask Python's import machinery directly via
    ``importlib.util.find_spec`` whether `cron.scheduler` is currently
    resolvable. If yes AND the resolved origin is outside any tmp dir
    (i.e., not a fake we just wrote), assume a competing real install is
    present. This is more robust than name-pattern matching against
    site-packages entries, which misses PEP 660 schemes (hatchling/poetry)
    and legacy egg-links.
    """
    try:
        import importlib.util
        spec = importlib.util.find_spec("cron.scheduler")
    except Exception:
        return False
    if spec is None or not spec.origin:
        return False
    origin = str(spec.origin)
    # Tests write fake cron.scheduler under tmp_path; tmp paths shouldn't
    # count as a "real" competing install. Treat anything outside common tmp
    # roots as a real install that will out-resolve the fake.
    tmp_prefixes = ("/tmp/", "/var/folders/", os.path.expandvars("$TMPDIR/") if os.environ.get("TMPDIR") else "")
    return not any(p and origin.startswith(p) for p in tmp_prefixes)


def _large_cron_payload_runner(profile_home, result_queue):
    try:
        fake_agent_root = Path(profile_home).parent / "fake-agent"
        _write_spawn_fake_agent(
            fake_agent_root,
            run_job_body=(
                "    payload = 'x' * 200_000\n"
                "    return True, payload, payload, None\n"
            ),
        )
        _activate_spawn_fake_agent(fake_agent_root)
        import api.routes as routes

        success, output, final_response, error = routes._run_cron_job_in_profile_subprocess(
            {"id": "large-payload"}, Path(profile_home)
        )
        result_queue.put(("ok", success, len(output), len(final_response), error))
    except BaseException as exc:  # pragma: no cover - surfaced in parent process
        import traceback

        result_queue.put(("error", repr(exc), traceback.format_exc()))


def _selected_profile_home_runner(profile_home, result_queue):
    try:
        fake_agent_root = Path(profile_home).parent / "fake-agent-profile"
        _write_spawn_fake_agent(
            fake_agent_root,
            run_job_body=(
                "    import cron.scheduler as scheduler\n"
                "    return True, str(scheduler._hermes_home), 'final', None\n"
            ),
        )
        _activate_spawn_fake_agent(fake_agent_root)
        import api.routes as routes

        success, output, final_response, error = routes._run_cron_job_in_profile_subprocess(
            {"id": "job1574"}, Path(profile_home)
        )
        result_queue.put(("ok", success, output, final_response, error))
    except BaseException as exc:  # pragma: no cover - surfaced in parent process
        import traceback

        result_queue.put(("error", repr(exc), traceback.format_exc()))


def test_manual_cron_subprocess_uses_spawn_context():
    """Manual cron subprocesses must avoid fork-from-threaded-WebUI hazards."""
    routes_src = (Path(__file__).resolve().parent.parent / "api" / "routes.py").read_text(
        encoding="utf-8"
    )
    start = routes_src.find("def _run_cron_job_in_profile_subprocess")
    assert start != -1, "_run_cron_job_in_profile_subprocess not found"
    body = routes_src[start : start + 1200]

    assert 'multiprocessing.get_context("spawn")' in body
    assert 'multiprocessing.get_context("fork")' not in body


def _run_lock_probe_with_context(context_name, target, result_queue):
    ctx = multiprocessing.get_context(context_name)
    process = ctx.Process(target=target, args=(result_queue,))
    process.start()
    try:
        acquired = result_queue.get(timeout=5)
    finally:
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    return process.exitcode, acquired


def test_spawn_context_does_not_inherit_parent_thread_locks(tmp_path):
    """Spawn starts a fresh interpreter where fork would clone a held lock."""
    helper_dir = tmp_path / "spawn_helper"
    helper_dir.mkdir()
    (helper_dir / "issue1754_lock_probe.py").write_text(
        "import threading\n"
        "LOCK = threading.Lock()\n"
        "def try_acquire(result_queue):\n"
        "    acquired = LOCK.acquire(timeout=1)\n"
        "    if acquired:\n"
        "        LOCK.release()\n"
        "    result_queue.put(acquired)\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(helper_dir))
    try:
        import issue1754_lock_probe

        issue1754_lock_probe.LOCK.acquire()
        try:
            # The held module-level lock models import/logging locks owned by a
            # sibling WebUI thread at the instant the manual cron worker starts.
            # fork clones the locked primitive into the child with no owner left
            # to release it; spawn re-imports a fresh module and can proceed.
            fork_queue = multiprocessing.get_context("fork").Queue()
            fork_exitcode, fork_acquired = _run_lock_probe_with_context(
                "fork", issue1754_lock_probe.try_acquire, fork_queue
            )
            spawn_queue = multiprocessing.get_context("spawn").Queue()
            spawn_exitcode, spawn_acquired = _run_lock_probe_with_context(
                "spawn", issue1754_lock_probe.try_acquire, spawn_queue
            )
        finally:
            issue1754_lock_probe.LOCK.release()
            for q in (locals().get("fork_queue"), locals().get("spawn_queue")):
                if q is not None:
                    q.close()
                    q.join_thread()
    finally:
        sys.modules.pop("issue1754_lock_probe", None)
        try:
            sys.path.remove(str(helper_dir))
        except ValueError:
            pass

    assert fork_exitcode == 0
    assert fork_acquired is False
    assert spawn_exitcode == 0
    assert spawn_acquired is True


def test_manual_cron_subprocess_drains_large_result_before_join(tmp_path):
    """A >100 KB result must not deadlock the parent before it can persist output."""
    if _real_hermes_agent_editable_install_present():
        import pytest as _pytest
        _pytest.skip(
            "skipped on dev machines with an editable hermes-agent install — "
            "the spawn child resolves the real cron.scheduler first instead of "
            "the fake one written under HERMES_WEBUI_AGENT_DIR. Runs cleanly on CI."
        )
    # Use fork only for the outer test harness so this pytest module does not
    # need to be importable as a package. The product helper under test owns its
    # own multiprocessing context.
    ctx = multiprocessing.get_context("fork")
    result_queue = ctx.Queue()
    runner = ctx.Process(
        target=_large_cron_payload_runner,
        args=(tmp_path / "exec-profile", result_queue),
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
    tag, success, output_len, final_response_len, error = result
    assert tag == "ok"
    assert success is True
    assert output_len == 200_000
    assert final_response_len == 200_000
    assert error is None


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
    if _real_hermes_agent_editable_install_present():
        import pytest as _pytest
        _pytest.skip(
            "skipped on dev machines with an editable hermes-agent install — "
            "the spawn child resolves the real cron.scheduler first instead of "
            "the fake one written under HERMES_WEBUI_AGENT_DIR. Runs cleanly on CI."
        )
    exec_home = tmp_path / "exec-profile"
    ctx = multiprocessing.get_context("fork")
    result_queue = ctx.Queue()
    runner = ctx.Process(
        target=_selected_profile_home_runner,
        args=(exec_home, result_queue),
    )
    runner.start()
    runner.join(10)
    if runner.is_alive():
        runner.terminate()
        runner.join(5)
        result_queue.close()
        result_queue.join_thread()
        raise AssertionError("manual cron subprocess did not finish selected-profile probe")

    try:
        result = result_queue.get(timeout=2)
    finally:
        result_queue.close()
        result_queue.join_thread()

    assert result == ("ok", True, str(exec_home), "final", None)
