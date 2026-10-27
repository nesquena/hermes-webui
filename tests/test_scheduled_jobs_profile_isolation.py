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


def test_manual_cron_event_profile_uses_job_profile(monkeypatch):
    from api import routes

    monkeypatch.setattr(routes, "_available_cron_profile_names", lambda: {"default", "research"})

    assert routes._event_profile_for_cron_job({"profile": "research"}) == "research"
    assert routes._event_profile_for_cron_job({"profile": " default "}) == "default"
    assert routes._event_profile_for_cron_job({"profile": ""}) is None
    assert routes._event_profile_for_cron_job({"profile": "deleted"}) is None


def test_webui_installs_profile_context_on_in_process_scheduler_run_job(tmp_path, monkeypatch):
    """If WebUI ever runs cron.scheduler.tick in-process, scheduled run_job calls
    must execute under the job's selected profile home, not the process-global
    HERMES_HOME that happened to be active when the scheduler thread fired.
    """
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
    cron_scheduler.run_job = lambda job: events.append(("run", job["id"])) or "ok"

    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", cron_scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", default_home)
    monkeypatch.setattr(p, "cron_profile_context_for_home", Ctx)
    monkeypatch.setattr(p, "publish_session_list_changed", lambda reason: events.append(("publish", reason)))

    p.install_cron_scheduler_profile_isolation()

    assert cron_scheduler.run_job({"id": "job1575", "profile": "research"}) == "ok"
    assert events == [
        ("enter", str(research_home)),
        ("run", "job1575"),
        ("exit", str(research_home)),
        ("publish", "cron_complete"),
    ]


def test_scheduler_run_job_wrapper_does_not_reenter_manual_cron_context(tmp_path, monkeypatch):
    """Manual /api/crons/run already pins run_job before calling it.

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
    cron_scheduler.run_job = lambda job: events.append(("run", job["id"])) or "ok"

    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.scheduler", cron_scheduler)
    monkeypatch.setattr(p, "_DEFAULT_HERMES_HOME", tmp_path / "home")
    monkeypatch.setattr(p, "cron_profile_context_for_home", Ctx)
    monkeypatch.setattr(p, "publish_session_list_changed", lambda reason: events.append(("unexpected-publish", reason)))
    monkeypatch.setattr(p._tls, "cron_profile_depth", 1, raising=False)

    p.install_cron_scheduler_profile_isolation()

    assert cron_scheduler.run_job({"id": "manual1575", "profile": "research"}) == "ok"
    assert events == [("run", "manual1575")]


def test_cron_worker_does_not_silently_fall_back_on_profile_context_failure():
    """The subprocess target must not fall back to an unpinned cron run.

    A silent fallback would leave the job running against process-global
    HERMES_HOME, silently corrupting cross-profile state — the same class of bug
    as #1573. The child process may report the exception to the parent, but it
    must not continue into run_job outside the requested profile context.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "api" / "routes.py").read_text(encoding="utf-8")

    idx = src.find("def _cron_job_subprocess_main(job")
    assert idx != -1, "_cron_job_subprocess_main not found"
    body = src[idx : idx + 2000]

    assert "with cron_profile_context_for_home(execution_profile_home):" in body
    assert "result = _run()" in body
    assert "ctx = None" not in body
    assert "except Exception" not in body[:body.find("with cron_profile_context_for_home")], (
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
            return entry if name == entry.name else None

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


def test_streaming_cronjob_wrapper_tolerates_agent_without_dynamic_schema_overrides(monkeypatch):
    """Older hermes-agent ToolEntry/register() lack ``dynamic_schema_overrides`` (#8186).

    Installing the wrapper must not raise AttributeError (which aborted every
    chat stream) and must not pass the unknown kwarg to an older register().
    """
    import types

    from api import streaming as st

    def original_handler(args, **kwargs):
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

    entry = Entry()
    registered = []

    class Registry:
        def get_entry(self, name):
            return entry

        def register(self, name, toolset, schema, handler, check_fn=None,
                     requires_env=None, is_async=False, description="", emoji="",
                     max_result_size_chars=None):
            registered.append(name)
            entry.handler = handler

    tools_pkg = types.ModuleType("tools")
    registry_mod = types.ModuleType("tools.registry")
    registry_mod.__dict__["registry"] = Registry()
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.registry", registry_mod)
    monkeypatch.setattr(st, "_STREAMING_CRONJOB_WRAPPER_INSTALLED", False)

    st._install_streaming_cronjob_profile_wrapper()

    assert registered == ["cronjob"]
    assert st._STREAMING_CRONJOB_WRAPPER_INSTALLED is True
    assert entry.handler({"action": "list"}) == "ok"


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
            return entry if name == entry.name else None

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
            return entry if name == entry.name else None

        def register(self, **kwargs):
            entry.handler = kwargs["handler"]

    registry_mod = types.ModuleType("tools.registry")
    registry_mod.__dict__["registry"] = Registry()
    monkeypatch.setitem(sys.modules, "tools.registry", registry_mod)
    monkeypatch.setattr(st, "_STREAMING_CRONJOB_WRAPPER_INSTALLED", False)

    st._install_streaming_cronjob_profile_wrapper()

    assert entry.handler({"action": "list"}) == "ok"
    assert events == [("handler", "list")]


def test_streaming_cronjob_wrapper_wraps_the_renamed_cronjob_manage_tool(tmp_path, monkeypatch):
    """Current hermes-agent registers ``cronjob_manage``, not ``cronjob`` (#8189).

    Looking up the old name alone made the wrapper return at its debug log, so a
    cron tool call made from a chat ran against the default profile.
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
        events.append(("handler", st._STREAMING_CRON_PROFILE_HOME.get()))
        return "ok"

    class Entry:
        name = "cronjob_manage"
        toolset = "cronjob"
        schema = {"name": "cronjob_manage"}
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
            return entry if name == entry.name else None

        def register(self, **kwargs):
            events.append(("register", kwargs["name"]))
            entry.handler = kwargs["handler"]

    registry_mod = types.ModuleType("tools.registry")
    registry_mod.__dict__["registry"] = Registry()
    monkeypatch.setitem(sys.modules, "tools.registry", registry_mod)
    monkeypatch.setattr(st, "_STREAMING_CRONJOB_WRAPPER_INSTALLED", False)
    monkeypatch.setattr(p, "cron_profile_context_for_home", Ctx)

    st._install_streaming_cronjob_profile_wrapper()

    assert events == [("register", "cronjob_manage")]
    assert st._STREAMING_CRONJOB_WRAPPER_INSTALLED is True

    token = st._STREAMING_CRON_PROFILE_HOME.set(str(profile_home))
    try:
        assert entry.handler({"action": "list"}) == "ok"
    finally:
        st._STREAMING_CRON_PROFILE_HOME.reset(token)

    assert events == [
        ("register", "cronjob_manage"),
        ("enter", str(profile_home)),
        ("handler", str(profile_home)),
        ("exit", str(profile_home)),
    ]


def test_streaming_cronjob_wrapper_wraps_both_names_when_both_are_registered(monkeypatch):
    """A build that keeps the old name next to the new one must wrap both.

    Each wrapped handler must call its own original: the loop that wraps them
    would otherwise let the last entry's handler win for all of them.
    """
    import types

    from api import streaming as st

    events = []

    def make_entry(name):
        entry = types.SimpleNamespace(
            name=name,
            toolset="cronjob",
            schema={"name": name},
            check_fn=None,
            requires_env=[],
            is_async=False,
            description="",
            emoji="⏰",
            max_result_size_chars=None,
            dynamic_schema_overrides=None,
        )
        entry.handler = (lambda args, _name=name, **kwargs: "ok:%s" % _name)
        return entry

    entries = {name: make_entry(name) for name in st._STREAMING_CRONJOB_TOOL_NAMES}

    class Registry:
        def get_entry(self, name):
            return entries.get(name)

        def register(self, **kwargs):
            events.append(("register", kwargs["name"]))
            entries[kwargs["name"]].handler = kwargs["handler"]

    registry_mod = types.ModuleType("tools.registry")
    registry_mod.__dict__["registry"] = Registry()
    monkeypatch.setitem(sys.modules, "tools.registry", registry_mod)
    monkeypatch.setattr(st, "_STREAMING_CRONJOB_WRAPPER_INSTALLED", False)

    st._install_streaming_cronjob_profile_wrapper()

    assert events == [("register", "cronjob_manage"), ("register", "cronjob")]
    # No streaming profile is set, so each wrapper must call its own original.
    assert entries["cronjob_manage"].handler({}) == "ok:cronjob_manage"
    assert entries["cronjob"].handler({}) == "ok:cronjob"


def test_streaming_cronjob_wrapper_never_propagates_a_registration_failure(monkeypatch):
    """A future agent shape change must not abort the chat stream (#8186)."""
    import types

    from api import streaming as st

    entry = types.SimpleNamespace(
        name="cronjob_manage",
        toolset="cronjob",
        schema={"name": "cronjob_manage"},
        handler=lambda args, **kwargs: "ok",
        check_fn=None,
        requires_env=[],
        is_async=False,
        description="",
        emoji="⏰",
        max_result_size_chars=None,
        dynamic_schema_overrides=None,
    )
    calls = []
    warnings = []

    class Registry:
        def get_entry(self, name):
            return entry if name == entry.name else None

        def register(self, **kwargs):
            calls.append(kwargs["name"])
            raise AttributeError("unexpected agent shape")

    registry_mod = types.ModuleType("tools.registry")
    registry_mod.__dict__["registry"] = Registry()
    monkeypatch.setitem(sys.modules, "tools.registry", registry_mod)
    monkeypatch.setattr(st, "_STREAMING_CRONJOB_WRAPPER_INSTALLED", False)
    monkeypatch.setattr(st, "_STREAMING_CRONJOB_WRAPPER_WARNED", False)
    monkeypatch.setattr(st.logger, "warning", lambda *args, **kwargs: warnings.append(args))

    st._install_streaming_cronjob_profile_wrapper()

    assert calls == ["cronjob_manage"]
    assert len(warnings) == 1
    # The warning is logged once, but the failure is not recorded as installed:
    # the next chat stream retries, so one bad registration cannot pin every
    # later job to the default profile (#8201).
    assert st._STREAMING_CRONJOB_WRAPPER_WARNED is True
    assert st._STREAMING_CRONJOB_WRAPPER_INSTALLED is False


def test_streaming_cronjob_wrapper_retries_after_a_failed_install(monkeypatch):
    """A later stream retries an install that failed once (#8201).

    Master retried and wrote the next chat-created job into the named profile;
    recording the failure as "installed" left every later job in the default
    profile's cron store.
    """
    import types

    from api import streaming as st

    entry = types.SimpleNamespace(
        name="cronjob_manage",
        toolset="cronjob",
        schema={"name": "cronjob_manage"},
        handler=lambda args, **kwargs: "ok",
        check_fn=None,
        requires_env=[],
        is_async=False,
        description="",
        emoji="⏰",
        max_result_size_chars=None,
        dynamic_schema_overrides=None,
    )
    calls = []
    warnings = []
    registry_state = {"fail": True}

    class Registry:
        def get_entry(self, name):
            return entry if name == entry.name else None

        def register(self, **kwargs):
            calls.append(kwargs["name"])
            if registry_state["fail"]:
                raise AttributeError("unexpected agent shape")
            entry.handler = kwargs["handler"]

    registry_mod = types.ModuleType("tools.registry")
    registry_mod.__dict__["registry"] = Registry()
    monkeypatch.setitem(sys.modules, "tools.registry", registry_mod)
    monkeypatch.setattr(st, "_STREAMING_CRONJOB_WRAPPER_INSTALLED", False)
    monkeypatch.setattr(st, "_STREAMING_CRONJOB_WRAPPER_WARNED", False)
    monkeypatch.setattr(st.logger, "warning", lambda *args, **kwargs: warnings.append(args))

    st._install_streaming_cronjob_profile_wrapper()
    assert st._STREAMING_CRONJOB_WRAPPER_INSTALLED is False

    # Same process, next chat stream, agent shape now accepted.
    registry_state["fail"] = False
    st._install_streaming_cronjob_profile_wrapper()

    assert calls == ["cronjob_manage", "cronjob_manage"]
    assert len(warnings) == 1
    assert st._STREAMING_CRONJOB_WRAPPER_INSTALLED is True
    assert entry.handler({"action": "list"}) == "ok"

    # A third stream is a no-op now that the wrapper is installed.
    st._install_streaming_cronjob_profile_wrapper()
    assert calls == ["cronjob_manage", "cronjob_manage"]


def test_streaming_cronjob_wrapper_wraps_the_global_entry_not_a_profile_override(monkeypatch):
    """A profile-scoped override must not be promoted to every profile (#8201).

    ``registry.get_entry()`` resolves the merged view, where the active profile's
    scoped override wins over the global slot. Re-registering that entry without a
    scope moved one profile's handler into the global slot, so every other profile
    ran it. Only the global slot may be wrapped.
    """
    import types

    from api import streaming as st

    events = []

    def make_entry(handler):
        return types.SimpleNamespace(
            name="cronjob_manage",
            toolset="cronjob",
            schema={"name": "cronjob_manage"},
            handler=handler,
            check_fn=None,
            requires_env=[],
            is_async=False,
            description="",
            emoji="⏰",
            max_result_size_chars=None,
            dynamic_schema_overrides=None,
        )

    global_entry = make_entry(lambda args, **kwargs: "ok:global")
    scoped_entry = make_entry(lambda args, **kwargs: "ok:scoped")

    class Registry:
        def get_entry(self, name):
            # Merged view: this process profile overrides the global entry.
            return scoped_entry if name == scoped_entry.name else None

        def snapshot_registration(self, name, *, scope=None):
            events.append(("snapshot", name, scope))
            return global_entry if name == global_entry.name else None

        def register(self, **kwargs):
            events.append(("register", kwargs["name"]))
            global_entry.handler = kwargs["handler"]

    registry_mod = types.ModuleType("tools.registry")
    registry_mod.__dict__["registry"] = Registry()
    monkeypatch.setitem(sys.modules, "tools.registry", registry_mod)
    monkeypatch.setattr(st, "_STREAMING_CRONJOB_WRAPPER_INSTALLED", False)
    monkeypatch.setattr(st, "_STREAMING_CRONJOB_WRAPPER_WARNED", False)

    st._install_streaming_cronjob_profile_wrapper()

    assert events == [
        ("snapshot", "cronjob_manage", None),
        ("snapshot", "cronjob", None),
        ("register", "cronjob_manage"),
    ]
    assert st._STREAMING_CRONJOB_WRAPPER_INSTALLED is True
    # The global handler is the one that got wrapped...
    assert global_entry.handler({}) == "ok:global"
    # ...and the profile's own override was left alone.
    assert scoped_entry.handler({}) == "ok:scoped"


def test_streaming_cronjob_wrapper_names_cover_the_installed_agent_tool():
    """The lookup names must cover what the installed hermes-agent registers.

    Skips when hermes-agent is not importable, which is the case in the WebUI
    venv; on a machine that has it, this fails if the tool is renamed again.
    """
    pytest.importorskip("tools.registry")

    from api import streaming as st

    try:
        import tools.cronjob_tools  # noqa: F401  (registers the tool on import)
    except Exception:  # pragma: no cover - depends on the installed agent
        pytest.skip("hermes-agent cronjob tool module unavailable")

    from tools.registry import registry

    assert any(
        registry.get_entry(name) is not None
        for name in st._STREAMING_CRONJOB_TOOL_NAMES
    ), "the wrapper would never install for the installed agent"


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
