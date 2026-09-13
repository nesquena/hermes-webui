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


def test_manual_cron_event_profile_uses_job_profile(monkeypatch):
    from api import routes

    monkeypatch.setattr(routes, "_available_cron_profile_names", lambda: {"default", "research"})

    assert routes._event_profile_for_cron_job({"profile": "research"}) == "research"
    assert routes._event_profile_for_cron_job({"profile": " default "}) == "default"
    assert routes._event_profile_for_cron_job({"profile": ""}) is None
    assert routes._event_profile_for_cron_job({"profile": "deleted"}) is None


def test_webui_routes_scheduler_lifecycle_to_pinned_child(tmp_path, monkeypatch):
    """The scheduler adapter must not hold the parent profile lock."""
    import types

    from api import profiles as p

    default_home = tmp_path / "home"
    research_home = default_home / "profiles" / "research"
    research_home.mkdir(parents=True)
    events = []

    class Ctx:
        def __init__(self, home):
            self.home = str(home)

        def __enter__(self):
            events.append(("enter", self.home))
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append(("exit", self.home))
            return False

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_scheduler = types.ModuleType("cron.scheduler")
    original_run_one_job = lambda job: events.append(("run", job["id"])) or True
    cron_scheduler.run_one_job = original_run_one_job

    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", cron_scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", default_home)
    monkeypatch.setattr(p, "cron_profile_context_for_home", Ctx)
    monkeypatch.setattr(p, "publish_session_list_changed", lambda reason: events.append(("publish", reason)))
    monkeypatch.setattr(
        "api.cron_runtime.run_cron_in_profile_subprocess",
        lambda job, home, operation, *, args=(), kwargs=None, cancel_event=None: (
            events.append(("spawn", str(home), operation))
            or original_run_one_job(job)
        ),
    )

    p.install_cron_scheduler_profile_isolation()

    assert cron_scheduler.run_one_job({"id": "job1575", "profile": "research"}) is True
    assert events == [
        ("spawn", str(research_home), "run_one_job"),
        ("run", "job1575"),
        ("publish", "cron_complete"),
    ]


def test_scheduler_run_one_job_wrapper_does_not_reenter_child_context(tmp_path, monkeypatch):
    """A child-side lifecycle call delegates to the captured original.

    The scheduler safety wrapper must detect that existing context and delegate
    directly, otherwise the non-reentrant env lock would deadlock or override the
    manual execution profile.
    """
    import types

    from api import profiles as p

    events = []

    class Ctx:
        def __init__(self, home):
            self.home = str(home)

        def __enter__(self):
            events.append(("unexpected-enter", self.home))
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append(("unexpected-exit", self.home))
            return False

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_scheduler = types.ModuleType("cron.scheduler")
    cron_scheduler.run_one_job = lambda job: events.append(("run", job["id"])) or True

    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", cron_scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", tmp_path / "home")
    monkeypatch.setattr(p, "cron_profile_context_for_home", Ctx)
    monkeypatch.setattr(p, "publish_session_list_changed", lambda reason: events.append(("unexpected-publish", reason)))
    child_token = p._cron_child_execution.set(True)

    try:
        p.install_cron_scheduler_profile_isolation()

        assert cron_scheduler.run_one_job({"id": "manual1575", "profile": "research"}) is True
    finally:
        p._cron_child_execution.reset(child_token)
    assert events == [("run", "manual1575")]


def test_scheduled_fire_reader_enters_while_child_remains_blocked(tmp_path, monkeypatch):
    """The scheduled adapter leaves the parent cron lock available."""
    import threading
    import types

    from api import profiles as p

    home = tmp_path / "home"
    started = threading.Event()
    release = threading.Event()
    reader_entered = threading.Event()
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    scheduler = types.ModuleType("cron.scheduler")
    scheduler.run_one_job = lambda job: True
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", home)

    def child_boundary(
        job, profile_home, operation, *, args=(), kwargs=None, cancel_event=None
    ):
        started.set()
        assert release.wait(2)
        return True

    monkeypatch.setattr("api.cron_runtime.run_cron_in_profile_subprocess", child_boundary)
    p.install_cron_scheduler_profile_isolation()

    fire = threading.Thread(target=scheduler.run_one_job, args=({"id": "blocked"},))
    fire.start()
    assert started.wait(2)

    def reader():
        with p.cron_profile_context_for_home(home):
            reader_entered.set()

    contender = threading.Thread(target=reader)
    contender.start()
    assert reader_entered.wait(0.5), "reader remained blocked by the scheduled fire"
    release.set()
    fire.join(2)
    contender.join(2)
    assert not fire.is_alive()
    assert not contender.is_alive()


def test_in_chat_run_suspends_parent_tool_context_before_child(monkeypatch, tmp_path):
    """The existing in-chat context must not span the lifecycle child."""
    import types

    from api import profiles as p

    home = tmp_path / "home"
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    scheduler = types.ModuleType("cron.scheduler")
    scheduler.run_one_job = lambda job: True
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", home)
    observed = []

    def child_boundary(
        job, profile_home, operation, *, args=(), kwargs=None, cancel_event=None
    ):
        observed.append((operation, p._cron_env_lock.locked(), p._cron_profile_context_depth()))
        return True

    monkeypatch.setattr("api.cron_runtime.run_cron_in_profile_subprocess", child_boundary)
    p.install_cron_scheduler_profile_isolation()

    with p.cron_profile_context_for_home(home):
        assert scheduler.run_one_job({"id": "chat-run", "profile": "default"}) is True

    assert observed == [("run_one_job", False, 0)]
    assert not p._cron_env_lock.locked()


def test_in_chat_run_reacquires_profile_for_post_child_settlement(monkeypatch, tmp_path):
    """Post-run status reads remain inside the selected profile context."""
    import types

    from api import profiles as p

    home = tmp_path / "home"
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    scheduler = types.ModuleType("cron.scheduler")
    scheduler.run_one_job = lambda job: True
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", home)
    observed = []

    def child_boundary(
        job, profile_home, operation, *, args=(), kwargs=None, cancel_event=None
    ):
        assert not p._cron_env_lock.locked()
        return True

    monkeypatch.setattr("api.cron_runtime.run_cron_in_profile_subprocess", child_boundary)
    p.install_cron_scheduler_profile_isolation()

    with p.cron_profile_context_for_home(home):
        assert scheduler.run_one_job({"id": "chat-settle", "profile": "default"}) is True
        observed.append((os.environ.get("HERMES_HOME"), p._cron_profile_context_depth()))

    assert observed == [(str(home), 1)]
    assert not p._cron_env_lock.locked()


def test_in_chat_run_without_profile_inherits_selected_home_before_suspend(
    monkeypatch, tmp_path
):
    """A no-profile in-chat fire captures the selected home before suspension and restores it."""
    import types

    from api import profiles as p

    selected_home = tmp_path / "selected"
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    scheduler = types.ModuleType("cron.scheduler")
    scheduler.run_one_job = lambda job: True
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", tmp_path / "default")
    observed = []

    def child_boundary(
        job, profile_home, operation, *, args=(), kwargs=None, cancel_event=None
    ):
        observed.append((profile_home, operation, p._cron_profile_context_depth()))
        return True

    monkeypatch.setattr("api.cron_runtime.run_cron_in_profile_subprocess", child_boundary)
    p.install_cron_scheduler_profile_isolation()

    with p.cron_profile_context_for_home(selected_home):
        assert scheduler.run_one_job({"id": "chat-no-profile"}) is True
        assert os.environ["HERMES_HOME"] == str(selected_home)
        assert p._cron_profile_context_depth() == 1

    assert observed == [(selected_home, "run_one_job", 0)]
    assert not p._cron_env_lock.locked()


@pytest.mark.parametrize("handle", ["adapters", "loop", "both"])
def test_cron_child_request_rejects_live_gateway_handles_before_spawn(
    monkeypatch, handle
):
    from api import cron_runtime

    live_kwargs = {"adapters": None, "loop": None, "verbose": True}
    if handle in {"adapters", "both"}:
        live_kwargs["adapters"] = object()
    if handle in {"loop", "both"}:
        live_kwargs["loop"] = object()

    monkeypatch.setattr(
        cron_runtime.multiprocessing,
        "get_context",
        lambda name: pytest.fail("live handle request reached process creation"),
    )
    with pytest.raises(RuntimeError, match="cannot carry live"):
        cron_runtime.run_cron_in_profile_subprocess(
            {"id": "runtime-kwarg"}, None, "run_one_job", kwargs=live_kwargs
        )
    assert cron_runtime._child_kwargs_for_operation(
        "run_one_job", {"adapters": None, "loop": None, "verbose": True}
    ) == {"adapters": None, "loop": None, "verbose": True}
    assert cron_runtime._serialize_child_request(
        {"id": "runtime-kwarg"}, (), {"verbose": True}
    )
    with pytest.raises(RuntimeError, match="non-serializable"):
        cron_runtime._serialize_child_request(
            {"id": "runtime-kwarg"}, (), {"runtime": object()}
        )


def test_two_overlapping_scheduled_children_leave_profile_readers_responsive(
    tmp_path, monkeypatch
):
    """Two profile-owned child boundaries must not share the parent lock."""
    import types

    from api import profiles as p

    default_home = tmp_path / "home"
    homes = {
        "alpha": default_home / "profiles" / "alpha",
        "beta": default_home / "profiles" / "beta",
    }
    for home in homes.values():
        home.mkdir(parents=True)

    scheduler = types.ModuleType("cron.scheduler")
    scheduler.run_one_job = lambda job: True
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", default_home)

    started = {profile: threading.Event() for profile in homes}
    release = {profile: threading.Event() for profile in homes}

    def child_boundary(
        job, profile_home, operation, *, args=(), kwargs=None, cancel_event=None
    ):
        profile = job["profile"]
        assert pathlib.Path(profile_home) == homes[profile]
        assert operation == "run_one_job"
        started[profile].set()
        assert release[profile].wait(2)
        return profile

    monkeypatch.setattr("api.cron_runtime.run_cron_in_profile_subprocess", child_boundary)
    monkeypatch.setattr(p, "publish_session_list_changed", lambda *args, **kwargs: None)
    p.install_cron_scheduler_profile_isolation()

    workers = [
        threading.Thread(
            target=scheduler.run_one_job,
            args=({"id": profile, "profile": profile},),
        )
        for profile in homes
    ]
    for worker in workers:
        worker.start()
    assert all(started[profile].wait(2) for profile in homes)

    readers = []
    reader_entered = {profile: threading.Event() for profile in homes}
    for profile, home in homes.items():
        reader = threading.Thread(
            target=lambda profile=profile, home=home: _read_profile(home, reader_entered[profile])
        )
        readers.append(reader)
        reader.start()

    assert all(reader_entered[profile].wait(0.5) for profile in homes)
    for event in release.values():
        event.set()
    for worker in workers + readers:
        worker.join(2)
        assert not worker.is_alive()
    assert not p._cron_env_lock.locked()


def _read_profile(home, entered):
    from api import profiles as p

    with p.cron_profile_context_for_home(home):
        entered.set()


def test_install_scheduler_refuses_legacy_run_job(monkeypatch, tmp_path):
    import types

    from api import profiles as p

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    scheduler = types.ModuleType("cron.scheduler")
    scheduler.run_job = lambda job: job["id"]
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", tmp_path / "home")

    with pytest.raises(RuntimeError, match="unsupported Agent version"):
        p.install_cron_scheduler_profile_isolation()
    assert scheduler.run_job({"id": "legacy"}) == "legacy"


def test_install_scheduler_legacy_refusal_is_explicit_on_repeat(monkeypatch, tmp_path):
    import types

    from api import profiles as p

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    scheduler = types.ModuleType("cron.scheduler")
    scheduler.run_job = lambda job: job["id"]
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", tmp_path / "home")
    with pytest.raises(RuntimeError, match="unsupported Agent version"):
        p.install_cron_scheduler_profile_isolation()
    with pytest.raises(RuntimeError, match="unsupported Agent version"):
        p.install_cron_scheduler_profile_isolation()
    assert scheduler.run_job({"id": "legacy-repeat"}) == "legacy-repeat"


@pytest.mark.parametrize("handle_names", [("adapters",), ("loop",), ("adapters", "loop")])
def test_scheduler_live_gateway_handles_preserve_parent_run_one_job(
    monkeypatch, tmp_path, handle_names
):
    import types

    from api import profiles as p

    scheduler = types.ModuleType("cron.scheduler")
    calls = []

    def original_run_one_job(job, *args, **kwargs):
        calls.append((job, args, kwargs, os.environ.get("HERMES_HOME")))
        return "delivered"

    scheduler.run_one_job = original_run_one_job
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", tmp_path / "home")
    handles = {name: object() for name in ("adapters", "loop")}
    cancel_event = object()
    published = []
    monkeypatch.setattr(
        p, "publish_session_list_changed", lambda *args, **kwargs: published.append((args, kwargs))
    )

    p.install_cron_scheduler_profile_isolation()

    kwargs = {name: handles[name] for name in handle_names}
    kwargs.update(cancel_event=cancel_event, verbose=True, future_flag="kept")
    assert scheduler.run_one_job({"id": "live", "profile": "default"}, "positional", **kwargs) == "delivered"
    assert calls == [(
        {"id": "live", "profile": "default"},
        ("positional",),
        kwargs,
        str(tmp_path / "home"),
    )]
    assert published == [(('cron_complete',), {'profile': 'default'})]


def test_scheduler_live_gateway_handles_preserve_agent_delivery_and_settlement(
    monkeypatch, tmp_path
):
    import types

    from api import profiles as p

    scheduler = types.ModuleType("cron.scheduler")
    home = tmp_path / "home"
    adapter, loop = object(), object()
    events = []
    scheduler.run_one_job = None
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(
        scheduler,
        "claim_dispatch",
        lambda job_id: events.append(("claim", job_id)) or True,
        raising=False,
    )
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda job, **kwargs: (True, "output", "response", None),
        raising=False,
    )
    monkeypatch.setattr(
        scheduler,
        "save_job_output",
        lambda job_id, output: events.append(("save", job_id, output)) or home / "output",
        raising=False,
    )
    monkeypatch.setattr(
        scheduler,
        "_deliver_result",
        lambda job, content, adapters=None, loop=None: events.append(
            ("deliver", job["id"], content, adapters, loop)
        ) or None,
        raising=False,
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda job_id, success, error=None, delivery_error=None: events.append(
            ("mark", job_id, success, error, delivery_error)
        ),
        raising=False,
    )
    monkeypatch.setattr(scheduler, "_is_interrupted", lambda job_id: False, raising=False)
    monkeypatch.setattr(
        scheduler, "_consume_interrupted_flag", lambda job_id: False, raising=False
    )
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: home, raising=False)
    monkeypatch.setattr(p, "publish_session_list_changed", lambda *a, **k: None)

    def agent_run_one_job(job, *, adapters=None, loop=None):
        if adapters is not adapter or loop is not loop_handle:
            raise AssertionError("Agent delivery requires the live gateway handles")
        if not scheduler.claim_dispatch(job["id"]):
            return False
        success, output, response, error = scheduler.run_job(job)
        scheduler.save_job_output(job["id"], output)
        scheduler._deliver_result(job, response, adapters=adapters, loop=loop)
        scheduler.mark_job_run(job["id"], success, error, None)
        return success

    loop_handle = loop
    scheduler.run_one_job = agent_run_one_job
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    p.install_cron_scheduler_profile_isolation()

    assert scheduler.run_one_job(
        {"id": "live-delivery", "profile": "default"},
        adapters=adapter,
        loop=loop,
    ) is True
    assert events == [
        ("claim", "live-delivery"),
        ("save", "live-delivery", "output"),
        ("deliver", "live-delivery", "response", adapter, loop),
        ("mark", "live-delivery", True, None, None),
    ]


def test_scheduler_live_gateway_handles_do_not_trust_copied_stack_without_depth(
    monkeypatch, tmp_path
):
    import contextvars
    import types

    from api import profiles as p

    home = tmp_path / "home"
    stale_home = tmp_path / "stale"
    scheduler = types.ModuleType("cron.scheduler")
    observed = []
    scheduler.run_one_job = lambda job, **kwargs: observed.append(
        (os.environ.get("HERMES_HOME"), p._cron_profile_context_depth(), p._cron_env_lock.locked())
    ) or True
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(p, "publish_session_list_changed", lambda *a, **k: None)
    p.install_cron_scheduler_profile_isolation()

    with p.cron_profile_context_for_home(stale_home):
        copied_context = contextvars.copy_context()

    assert p._cron_profile_context_depth() == 0
    copied_context.run(
        scheduler.run_one_job,
        {"id": "copied-live", "profile": "default"},
        loop=object(),
    )
    copied_context.run(
        scheduler.run_one_job,
        {"id": "copied-live-without-profile"},
        loop=object(),
    )
    assert observed == [(str(home), 1, True), (str(home), 1, True)]
    assert not p._cron_env_lock.locked()


def test_scheduler_live_gateway_handles_matching_context_stays_in_parent(
    monkeypatch, tmp_path
):
    import types

    from api import profiles as p

    home = tmp_path / "home"
    scheduler = types.ModuleType("cron.scheduler")
    calls, published = [], []
    scheduler.run_one_job = lambda job, **kwargs: calls.append(kwargs) or True
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(p, "publish_session_list_changed", lambda *a, **k: published.append(1))
    p.install_cron_scheduler_profile_isolation()

    with p.cron_profile_context_for_home(home):
        assert scheduler.run_one_job({"id": "matching", "profile": "default"}, adapters=object())
        assert os.environ["HERMES_HOME"] == str(home)
    assert len(calls) == 1
    assert len(published) == 1


def test_scheduler_live_gateway_handles_mismatched_context_fails_closed(
    monkeypatch, tmp_path
):
    import types

    from api import profiles as p

    default_home = tmp_path / "home"
    other_home = tmp_path / "other"
    scheduler = types.ModuleType("cron.scheduler")
    calls, published = [], []
    scheduler.run_one_job = lambda job, **kwargs: calls.append(1) or True
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", default_home)
    monkeypatch.setattr(p, "publish_session_list_changed", lambda *a, **k: published.append(1))
    p.install_cron_scheduler_profile_isolation()

    with p.cron_profile_context_for_home(other_home):
        with pytest.raises(RuntimeError, match="outside the active profile context"):
            scheduler.run_one_job({"id": "mismatch", "profile": "default"}, loop=object())
    assert calls == []
    assert len(published) == 1


def test_scheduler_live_gateway_handles_deleted_profile_uses_resolved_fallback(
    monkeypatch, tmp_path
):
    import types

    from api import profiles as p

    default_home = tmp_path / "home"
    scheduler = types.ModuleType("cron.scheduler")
    observed = []
    scheduler.run_one_job = lambda job, **kwargs: observed.append(
        os.environ["HERMES_HOME"]
    ) or True
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", default_home)
    monkeypatch.setattr(p, "publish_session_list_changed", lambda *a, **k: None)
    p.install_cron_scheduler_profile_isolation()

    assert scheduler.run_one_job(
        {"id": "deleted", "profile": "deleted-profile"}, loop=object()
    ) is True
    assert observed == [str(default_home)]
    assert not p._cron_env_lock.locked()


def test_scheduler_live_gateway_exception_releases_parent_lock_and_publishes(
    monkeypatch, tmp_path
):
    import types

    from api import profiles as p

    scheduler = types.ModuleType("cron.scheduler")
    scheduler.run_one_job = lambda job, **kwargs: (_ for _ in ()).throw(ValueError("delivery failed"))
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    published = []
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", tmp_path / "home")
    monkeypatch.setattr(p, "publish_session_list_changed", lambda *a, **k: published.append(1))
    p.install_cron_scheduler_profile_isolation()

    with pytest.raises(ValueError, match="delivery failed"):
        scheduler.run_one_job({"id": "failure", "profile": "default"}, adapters=object())
    assert not p._cron_env_lock.locked()
    assert len(published) == 1


def test_scheduler_child_failure_releases_lock_and_publishes(monkeypatch, tmp_path):
    import types

    from api import profiles as p

    scheduler = types.ModuleType("cron.scheduler")
    scheduler.run_one_job = lambda job, **kwargs: None
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", tmp_path / "home")
    monkeypatch.setattr(
        "api.cron_runtime.run_cron_in_profile_subprocess",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("child died")),
    )
    published = []
    monkeypatch.setattr(
        p, "publish_session_list_changed", lambda *args, **kwargs: published.append(1)
    )

    p.install_cron_scheduler_profile_isolation()

    with pytest.raises(RuntimeError, match="child died"):
        scheduler.run_one_job(
            {"id": "claimed", "profile": "default"},
        )
    assert not p._cron_env_lock.locked()
    assert published == [1]


def test_install_scheduler_fails_when_both_operations_are_missing(monkeypatch):
    import types

    from api import profiles as p

    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    scheduler = types.ModuleType("cron.scheduler")
    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", scheduler)

    with pytest.raises(RuntimeError, match="run_one_job is unavailable"):
        p.install_cron_scheduler_profile_isolation()


def test_cron_worker_does_not_silently_fall_back_on_profile_context_failure():
    """The subprocess target must not fall back to an unpinned cron run.

    A silent fallback would leave the job running against process-global
    HERMES_HOME, silently corrupting cross-profile state — the same class of bug
    as #1573. The child process may report the exception to the parent, but it
    must not continue into run_job outside the requested profile context.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "api" / "cron_runtime.py").read_text(encoding="utf-8")

    idx = src.find("def _cron_job_subprocess_main(job")
    assert idx != -1, "_cron_job_subprocess_main not found"
    body = src[idx : idx + 2000]

    assert "with _run_in_profile:" in body
    assert "result = _invoke_cron_operation" in body
    assert "_run_in_profile = None" in body
    assert "except Exception" not in body[:body.find("with _run_in_profile")], (
        "cron subprocess target appears to catch profile-context setup before "
        "entering the context; do not fall back to an unpinned run_job call."
    )


def test_streaming_cronjob_wrapper_uses_profile_context_only_for_tool_call(tmp_path, monkeypatch):
    """The chat cronjob fix must use the cron profile context at call time.

    Holding cron.jobs module globals for an entire streaming run would race with
    other profiles. The wrapper should instead enter the existing locked cron
    context only while the cronjob tool handler itself executes.
    """
    import types

    from api import profiles as p
    from api import streaming as st

    profile_home = tmp_path / "home" / "profiles" / "ops"
    events = []

    class Ctx:
        def __init__(self, home):
            self.home = str(home)

        def __enter__(self):
            events.append(("enter", self.home))
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append(("exit", self.home))
            return False

    def original_handler(args, **kwargs):
        events.append(("handler", args.get("action"), st._STREAMING_CRON_PROFILE_HOME.get()))
        return "ok"

    class Entry:
        name = "cronjob"
        toolset = "cronjob"
        schema = {"name": "cronjob"}
        handler = staticmethod(original_handler)
        check_fn = None
        requires_env = []
        is_async = False
        description = ""
        emoji = "⏰"
        max_result_size_chars = None
        dynamic_schema_overrides = None

    entry = Entry()

    class Registry:
        def get_entry(self, name):
            assert name == "cronjob"
            return entry

        def register(self, **kwargs):
            events.append(("register", kwargs["name"]))
            entry.handler = kwargs["handler"]

    tools_pkg = types.ModuleType("tools")
    registry_mod = types.ModuleType("tools.registry")
    registry_mod.__dict__["registry"] = Registry()
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.registry", registry_mod)
    monkeypatch.setattr(st, "_STREAMING_CRONJOB_WRAPPER_INSTALLED", False)
    monkeypatch.setattr(p, "cron_profile_context_for_home", Ctx)

    st._install_streaming_cronjob_profile_wrapper()
    assert events == [("register", "cronjob")]

    token = st._STREAMING_CRON_PROFILE_HOME.set(str(profile_home))
    try:
        assert entry.handler({"action": "list"}, task_id="t1") == "ok"
    finally:
        st._STREAMING_CRON_PROFILE_HOME.reset(token)

    assert events == [
        ("register", "cronjob"),
        ("enter", str(profile_home)),
        ("handler", "list", str(profile_home)),
        ("exit", str(profile_home)),
    ]


def test_streaming_cronjob_wrapper_context_survives_threadpool_context_copy(tmp_path, monkeypatch):
    """Lock the cross-thread contextvar contract used by agent tool dispatch.

    WebUI sets the active profile on the streaming thread, then the Hermes agent
    dispatches sync tools on a ThreadPoolExecutor worker under a copied
    contextvars context. The cronjob wrapper must still see the profile context
    on that worker or it silently falls back to the default profile.
    """
    import concurrent.futures
    import contextvars
    import types

    from api import profiles as p
    from api import streaming as st

    profile_home = tmp_path / "home" / "profiles" / "ops"
    events = []

    class Ctx:
        def __init__(self, home):
            self.home = str(home)

        def __enter__(self):
            events.append(("enter", self.home))
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append(("exit", self.home))
            return False

    def original_handler(args, **kwargs):
        events.append(("handler", args.get("action"), st._STREAMING_CRON_PROFILE_HOME.get()))
        return "ok"

    class Entry:
        name = "cronjob"
        toolset = "cronjob"
        schema = {"name": "cronjob"}
        handler = staticmethod(original_handler)
        check_fn = None
        requires_env = []
        is_async = False
        description = ""
        emoji = "⏰"
        max_result_size_chars = None
        dynamic_schema_overrides = None

    entry = Entry()

    class Registry:
        def get_entry(self, name):
            assert name == "cronjob"
            return entry

        def register(self, **kwargs):
            events.append(("register", kwargs["name"]))
            entry.handler = kwargs["handler"]

    registry_mod = types.ModuleType("tools.registry")
    registry_mod.__dict__["registry"] = Registry()
    monkeypatch.setitem(sys.modules, "tools.registry", registry_mod)
    monkeypatch.setattr(st, "_STREAMING_CRONJOB_WRAPPER_INSTALLED", False)
    monkeypatch.setattr(p, "cron_profile_context_for_home", Ctx)

    st._install_streaming_cronjob_profile_wrapper()
    token = st._STREAMING_CRON_PROFILE_HOME.set(str(profile_home))
    try:
        copied_context = contextvars.copy_context()
    finally:
        st._STREAMING_CRON_PROFILE_HOME.reset(token)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(copied_context.run, entry.handler, {"action": "list"})
        assert future.result(timeout=5) == "ok"

    assert events == [
        ("register", "cronjob"),
        ("enter", str(profile_home)),
        ("handler", "list", str(profile_home)),
        ("exit", str(profile_home)),
    ]


def test_streaming_cronjob_wrapper_leaves_calls_unwrapped_without_streaming_profile(monkeypatch):
    """CLI/default calls through the registered cronjob handler are unchanged."""
    import types

    from api import streaming as st

    events = []

    def original_handler(args, **kwargs):
        events.append(("handler", args.get("action")))
        return "ok"

    class Entry:
        name = "cronjob"
        toolset = "cronjob"
        schema = {"name": "cronjob"}
        handler = staticmethod(original_handler)
        check_fn = None
        requires_env = []
        is_async = False
        description = ""
        emoji = "⏰"
        max_result_size_chars = None
        dynamic_schema_overrides = None

    entry = Entry()

    class Registry:
        def get_entry(self, name):
            assert name == "cronjob"
            return entry

        def register(self, **kwargs):
            entry.handler = kwargs["handler"]

    registry_mod = types.ModuleType("tools.registry")
    registry_mod.__dict__["registry"] = Registry()
    monkeypatch.setitem(sys.modules, "tools.registry", registry_mod)
    monkeypatch.setattr(st, "_STREAMING_CRONJOB_WRAPPER_INSTALLED", False)

    st._install_streaming_cronjob_profile_wrapper()

    assert entry.handler({"action": "list"}) == "ok"
    assert events == [("handler", "list")]


def test_streaming_profile_home_mutation_avoids_long_lived_cron_cache_patch():
    """Guard the streaming integration seam for issue #4580.

    Streaming still mutates process env briefly for legacy fallbacks, but cron
    path caches must be scoped to the cronjob tool-call boundary via the
    wrapper/contextvar path — not patched for the full agent turn.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "api" / "streaming.py").read_text(encoding="utf-8")
    assert "_install_streaming_cronjob_profile_wrapper()" in src
    assert "_STREAMING_CRON_PROFILE_HOME.set(_profile_home)" in src
    assert "_STREAMING_CRON_PROFILE_HOME.reset(_streaming_cron_profile_home_token)" in src
    assert "def _patch_streaming_profile_module_caches" not in src
    assert "old_profile_module_cache_snapshot" not in src
