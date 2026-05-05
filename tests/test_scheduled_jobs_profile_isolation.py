"""Regression test: /api/crons must read jobs.json from the *active profile*.

Before the fix, `cron.jobs.list_jobs()` resolved HERMES_HOME from os.environ
at call time, ignoring the WebUI's per-request thread-local profile. So the
Scheduled Jobs panel showed the process-default profile's jobs regardless of
which profile the user had selected in the cookie.

This test writes two distinct jobs.json files (default + a named profile),
then verifies `cron_profile_context` pins the cron.jobs call to the named
profile's file.
"""
import json
import os
import pathlib
import sys
import threading
from unittest import mock

import pytest

# Ensure both repos are importable.
WEBUI_ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENT_ROOT = pathlib.Path(os.environ.get("HERMES_AGENT_ROOT", pathlib.Path.home() / "hermes-agent"))
for p in (str(WEBUI_ROOT), str(AGENT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _write_jobs(home: pathlib.Path, jobs: list):
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps({"jobs": jobs}), encoding="utf-8"
    )


def test_cron_profile_context_pins_profile_home(tmp_path, monkeypatch):
    """The context manager should swap cron.jobs to read from the named profile."""
    pytest.importorskip("cron.jobs")  # auto-skip when hermes-agent is unavailable

    default_home = tmp_path / "default_home"
    meow_home = tmp_path / "default_home" / "profiles" / "meow"

    _write_jobs(default_home, [{"id": "d1", "name": "default-job"}])
    _write_jobs(meow_home, [{"id": "m1", "name": "meow-job"}])

    # Point base at default_home; HERMES_HOME env starts at default.
    monkeypatch.setenv("HERMES_HOME", str(default_home))

    from api import profiles as p

    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", default_home)

    # Baseline: no context → default profile.
    from cron.jobs import list_jobs
    # Force cron.jobs to re-evaluate its cached constants for this test run.
    import cron.jobs as _cj
    _cj.HERMES_DIR = default_home
    _cj.CRON_DIR = default_home / "cron"
    _cj.JOBS_FILE = _cj.CRON_DIR / "jobs.json"
    _cj.OUTPUT_DIR = _cj.CRON_DIR / "output"

    jobs_before = list_jobs(include_disabled=True)
    assert any(j["id"] == "d1" for j in jobs_before), \
        f"Expected default-profile job before entering context, got {jobs_before}"

    # Simulate a request with TLS profile = 'meow'.
    p.set_request_profile("meow")
    try:
        with p.cron_profile_context():
            jobs_inside = list_jobs(include_disabled=True)
            assert any(j["id"] == "m1" for j in jobs_inside), \
                f"Expected meow-profile job inside context, got {jobs_inside}"
            assert not any(j["id"] == "d1" for j in jobs_inside), \
                "Default-profile job leaked into meow context"
    finally:
        p.clear_request_profile()

    # After the context exits, we should be back to default.
    jobs_after = list_jobs(include_disabled=True)
    assert any(j["id"] == "d1" for j in jobs_after), \
        f"Expected default-profile job after exiting context, got {jobs_after}"


def test_cron_profile_context_for_home_pins_explicit_home(tmp_path):
    """Thread variant: pin by explicit path (no TLS)."""
    pytest.importorskip("cron.jobs")  # auto-skip when hermes-agent is unavailable

    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    _write_jobs(home_a, [{"id": "a1", "name": "A"}])
    _write_jobs(home_b, [{"id": "b1", "name": "B"}])

    # Start with env pointing at A.
    prev = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home_a)
    try:
        import cron.jobs as _cj
        _cj.HERMES_DIR = home_a
        _cj.CRON_DIR = home_a / "cron"
        _cj.JOBS_FILE = _cj.CRON_DIR / "jobs.json"
        _cj.OUTPUT_DIR = _cj.CRON_DIR / "output"

        from cron.jobs import list_jobs
        from api.profiles import cron_profile_context_for_home

        assert any(j["id"] == "a1" for j in list_jobs(include_disabled=True))

        with cron_profile_context_for_home(home_b):
            jobs_inside = list_jobs(include_disabled=True)
            assert any(j["id"] == "b1" for j in jobs_inside), jobs_inside
            assert not any(j["id"] == "a1" for j in jobs_inside), jobs_inside

        # Restored to A.
        assert any(j["id"] == "a1" for j in list_jobs(include_disabled=True))
    finally:
        if prev is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = prev


def test_cron_profile_context_serializes_concurrent_access(tmp_path):
    """The lock must prevent concurrent contexts from interleaving."""
    from api.profiles import cron_profile_context_for_home

    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    home_a.mkdir()
    home_b.mkdir()

    # Ensure the context lock is released between tests.
    from api import profiles as p
    assert not p._cron_env_lock.locked(), \
        "Lock leaked from a previous test"

    observed = []
    barrier = threading.Barrier(2)

    def worker(home, tag):
        barrier.wait()
        with cron_profile_context_for_home(home):
            observed.append(("enter", tag, os.environ["HERMES_HOME"]))
            # If serialization works, the partner thread cannot be inside
            # its own context at this moment.
            observed.append(("exit", tag))

    t1 = threading.Thread(target=worker, args=(home_a, "A"))
    t2 = threading.Thread(target=worker, args=(home_b, "B"))
    t1.start(); t2.start()
    t1.join(); t2.join()

    # Every enter must be immediately followed by its matching exit (no
    # interleaving), because the lock serializes the two contexts.
    assert len(observed) == 4
    first, second, third, fourth = observed
    assert first[0] == "enter" and second[0] == "exit" and first[1] == second[1]
    assert third[0] == "enter" and fourth[0] == "exit" and third[1] == fourth[1]


def test_cron_run_does_not_silently_swallow_profile_resolution_errors():
    """_handle_cron_run must NOT silently fall through to profile_home=None
    when get_active_hermes_home() raises.

    A silent fallback would re-introduce the exact bug #1573 fixes — the
    worker thread would run unpinned against the process-global HERMES_HOME,
    silently corrupting cross-profile state. We'd rather 500 the request
    than risk that, since get_active_hermes_home() raising at all from
    inside a request handler means api.profiles is in a state we shouldn't
    be making cron decisions in.

    Source-level assertion to catch any future re-introduction of the
    over-broad except clause.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "api" / "routes.py").read_text(encoding="utf-8")

    # Locate _handle_cron_run definition; assert the spawn block does NOT
    # wrap get_active_hermes_home() in a bare except that falls back to None.
    idx = src.find("def _handle_cron_run(handler, body):")
    assert idx != -1, "_handle_cron_run not found"
    body = src[idx : idx + 4000]

    # The spawn site must call get_active_hermes_home() unguarded (no
    # try/except around it specifically), because a silent fallback to None
    # is exactly what would re-introduce #1573.
    spawn_idx = body.find("threading.Thread(target=_run_cron_tracked")
    assert spawn_idx != -1, "thread spawn not found in _handle_cron_run"

    # Look at the 1500 chars before the spawn — should NOT contain the
    # `_profile_home = None` fallback pattern.
    pre_spawn = body[max(0, spawn_idx - 1500) : spawn_idx]
    assert "_profile_home = None" not in pre_spawn, (
        "_handle_cron_run silently falls back to _profile_home=None when "
        "get_active_hermes_home() raises. That re-introduces bug #1573 — "
        "the worker thread would run unpinned against the process-global "
        "HERMES_HOME. Let the exception propagate (500 the request) rather "
        "than corrupt cross-profile state silently."
    )


def test_cron_worker_does_not_silently_fall_back_on_profile_context_failure():
    """_run_cron_tracked must NOT silently set ctx=None when
    cron_profile_context_for_home(...).__enter__() raises.

    A silent fallback in the worker thread would leave the job running
    unpinned against process-global HERMES_HOME, silently corrupting
    cross-profile state — the same class of bug as #1573. We'd rather
    let the exception propagate and kill the worker thread than risk
    that.

    Source-level assertion to catch any future re-introduction of the
    over-broad except clause around the context setup.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "api" / "routes.py").read_text(encoding="utf-8")

    idx = src.find("def _run_cron_tracked(job")
    assert idx != -1, "_run_cron_tracked not found"
    body = src[idx : idx + 2000]

    # The profile-context setup must NOT be wrapped in try/except that
    # silently falls back to ctx=None.
    assert "except Exception" not in body[:body.find("run_job(job)")], (
        "_run_cron_tracked silently falls back to ctx=None when "
        "cron_profile_context_for_home(...).__enter__() raises. That leaves "
        "the worker thread unpinned against process-global HERMES_HOME. "
        "Let the exception propagate rather than corrupt cross-profile state."
    )
