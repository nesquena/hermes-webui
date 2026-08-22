"""Regression tests for #5937/#6586 — terminal backend isolation generation protocol.

The 2026-08-03 redesign replaced the thread-local ``_turn_state`` (which carried
the calling turn's backend generation) with an explicit
``contextvars.ContextVar`` (``_TURN_BACKEND_GENERATION``): the Agent dispatches
parallel-safe tool calls on child threads and propagates ContextVars
(``contextvars.copy_context()``), NOT arbitrary thread-locals — so a
thread-local slot was invisible inside tool workers, and acquisition there fell
back to the process-global per-task owner, letting a concurrent profile change
reuse state outside the calling turn's captured generation.

The new protocol, driven by the REAL ``api.streaming`` functions:

* ``_begin_turn_generation(profile_runtime_env)`` — ONE ``_backend_generation_lock``
  transaction: identity compare/publish (``_publish_backend_identity``),
  exact-generation capture, ContextVar bind, active-use increment.  A failed
  transition RAISES (fail-closed) and binds nothing, so the production
  try/finally's ``_end_turn_generation()`` no-ops.
* ``_end_turn_generation()`` / ``_unregister_turn_generation()`` — reads the
  SAME captured generation from the ContextVar, clears it, decrements active
  use, and drains deferred stale evictions.
* ``_validate_backend_ownership`` — ONE shared acquisition boundary for
  terminal / file / code-execution.  Compares the live env's tag against the
  CALLING TURN's captured (ContextVar) generation — never the process-global
  current generation, never the recorded owner.  An untagged env is retired
  (fail-closed) for registered WebUI turns; an env still owned by an active
  old-generation turn is waited on (bounded) instead of torn down.
* ``_retire_generation_env`` — compare-and-remove under the registry lock; the
  exact removed object is cleaned OUTSIDE the lock, so a replacement installed
  under the same key is never torn down.

These tests drive the REAL ``api.streaming`` functions against a faithful fake
of the agent's ``tools.terminal_tool`` contract (non-reentrant lock,
lock-taking accessors, registry dicts), so they are RED on the buggy head and
GREEN after the fix.
"""
from __future__ import annotations

import contextvars
import importlib
import os
import sys
import threading
import time
import types

import pytest


class _FakeEnv:
    """Minimal stand-in for an agent BaseEnvironment."""

    def __init__(self, generation=None):
        self.cleaned = False
        self.stopped = False
        self._webui_backend_generation = generation

    def cleanup(self):
        self.cleaned = True

    def stop(self):
        self.stopped = True


class _FakeFileOps:
    """Minimal stand-in for ShellFileOperations (the file_ops cache value)."""

    def __init__(self, env_obj):
        self.env = env_obj
        self.cwd = getattr(env_obj, "cwd", None)
        self._webui_backend_generation = None


def _build_fake_terminal_tool_module() -> types.ModuleType:
    """Build a fake ``tools.terminal_tool`` faithful to the installed agent:

    - ``_env_lock`` is a plain non-reentrant ``threading.Lock``
    - ``get_active_env()`` acquires ``_env_lock`` internally (the exact
      accessor shape that deadlocked the buggy guard)
    - registries ``_active_environments`` / ``_last_activity``
    - ``_get_env_config()`` reads process-global ``os.environ`` (the
      pre-guard construction authority — the guard's wrapped
      ``_get_env_config`` overrides it with the turn's immutable snapshot)
    - ``_create_environment()`` is the SINGLE creation funnel (the guard
      wraps it to tag every env created during a WebUI turn) and records the
      config it was called with
    - ``terminal_tool()`` acquires/reuses the env and executes the command
      (records it) — the guard splits env acquisition from execution
    """
    mod = types.ModuleType("tools.terminal_tool")
    mod._env_lock = threading.Lock()
    mod._active_environments = {}
    mod._last_activity = {}
    mod.terminal_tool_calls = []
    mod.created_configs = []
    mod.executed_envs = []

    def _resolve_container_task_id(task_id):
        return task_id or "default"

    def _get_env_config():
        # Faithful to tools/terminal_tool.py:_get_env_config — reads the
        # process-global env (the authority the turn snapshot replaces).
        return {
            "env_type": os.getenv("TERMINAL_ENV", "local"),
            "docker_image": os.getenv("TERMINAL_DOCKER_IMAGE", "img"),
            "singularity_image": os.getenv("TERMINAL_SINGULARITY_IMAGE", "simg"),
            "modal_image": os.getenv("TERMINAL_MODAL_IMAGE", "mimg"),
            "daytona_image": os.getenv("TERMINAL_DAYTONA_IMAGE", "dimg"),
            "vercel_runtime": os.getenv("TERMINAL_VERCEL_RUNTIME", "").strip(),
            "cwd": os.getenv("TERMINAL_CWD", "/root"),
            "host_cwd": None,
            "timeout": int(os.getenv("TERMINAL_TIMEOUT", "180")),
            "lifetime_seconds": int(os.getenv("TERMINAL_LIFETIME_SECONDS", "300")),
            "ssh_host": os.getenv("TERMINAL_SSH_HOST", ""),
            "ssh_user": os.getenv("TERMINAL_SSH_USER", ""),
            "ssh_port": int(os.getenv("TERMINAL_SSH_PORT", "22")),
            "ssh_key": os.getenv("TERMINAL_SSH_KEY", ""),
            "ssh_persistent": os.getenv(
                "TERMINAL_SSH_PERSISTENT", "true").lower() in {"true", "1", "yes"},
            "local_persistent": os.getenv(
                "TERMINAL_LOCAL_PERSISTENT", "false").lower() in {"true", "1", "yes"},
            "modal_mode": "auto",
            "container_cpu": float(os.getenv("TERMINAL_CONTAINER_CPU", "1")),
            "container_memory": int(os.getenv("TERMINAL_CONTAINER_MEMORY", "5120")),
            "container_disk": int(os.getenv("TERMINAL_CONTAINER_DISK", "51200")),
            "container_persistent": os.getenv(
                "TERMINAL_CONTAINER_PERSISTENT", "true").lower() in {"true", "1", "yes"},
            "docker_volumes": [],
            "docker_env": {},
            "docker_run_as_host_user": False,
            "docker_network": True,
            "docker_extra_args": [],
            "docker_shm_size": "1g",
            "docker_persist_across_processes": True,
            "docker_orphan_reaper": True,
            "docker_forward_env": [],
            "docker_mount_cwd_to_workspace": False,
        }

    def _create_environment(
        env_type="local", image="", cwd="/root", timeout=180,
        ssh_config=None, container_config=None, local_config=None,
        task_id="default", host_cwd=None,
    ):
        # The SINGLE creation funnel shared by terminal/file/code-execution
        # (faithful to tools/terminal_tool.py:_create_environment — the
        # guard's wrapper tags the returned env with the calling turn's
        # generation).  Records what it was called with so tests can prove
        # construction consumed the turn's immutable config.
        streaming = importlib.import_module("api.streaming")
        env = _FakeEnv(generation=_tagged_generation(streaming))
        env.backend_env_type = env_type
        env.backend_cwd = cwd
        mod.created_configs.append({
            "env_type": env_type,
            "image": image,
            "cwd": cwd,
            "timeout": timeout,
            "ssh_config": ssh_config,
            "container_config": container_config,
            "local_config": local_config,
            "task_id": task_id,
            "host_cwd": host_cwd,
        })
        return env

    def get_active_env(task_id):
        # Faithful to tools/terminal_tool.py:~1736-1740 — takes the lock.
        lookup = _resolve_container_task_id(task_id)
        with mod._env_lock:
            return (
                mod._active_environments.get(lookup)
                or mod._active_environments.get(task_id)
            )

    def cleanup_vm(task_id, *, force_remove=False):
        with mod._env_lock:
            env = mod._active_environments.pop(task_id, None)
            mod._last_activity.pop(task_id, None)
        if env is not None:
            if hasattr(env, "cleanup"):
                env.cleanup()
            elif hasattr(env, "stop"):
                env.stop()

    def _cleanup_inactive_envs(lifetime_seconds=300):
        # Faithful to tools/terminal_tool.py:_cleanup_inactive_envs — the
        # idle reaper the installed terminal_tool starts on every call.  It
        # pops stale envs DIRECTLY from the registry (never through the
        # WebUI retirement paths) and tears them down outside the lock.
        current_time = time.time()
        envs_to_stop = []
        with mod._env_lock:
            for task_id, last_time in list(mod._last_activity.items()):
                if current_time - last_time > lifetime_seconds:
                    env = mod._active_environments.pop(task_id, None)
                    mod._last_activity.pop(task_id, None)
                    if env is not None:
                        envs_to_stop.append((task_id, env))
        for _task_id, env in envs_to_stop:
            if hasattr(env, "cleanup"):
                env.cleanup()
            elif hasattr(env, "stop"):
                env.stop()

    def terminal_tool(
        command, background=False, timeout=None, task_id=None,
        session_id=None, force=False, workdir=None, pty=False,
        notify_on_complete=False, watch_patterns=None,
    ):
        # Execution half of the installed tool: reuses the env the guarded
        # acquisition installed and executes the command against it.
        mod.terminal_tool_calls.append(command)
        eff = _resolve_container_task_id(task_id)
        with mod._env_lock:
            env = (
                mod._active_environments.get(eff)
                or mod._active_environments.get(task_id)
            )
        mod.executed_envs.append((command, env))
        return '{"output": "ok", "exit_code": 0}'

    mod._resolve_container_task_id = _resolve_container_task_id
    mod._get_env_config = _get_env_config
    mod._create_environment = _create_environment
    mod.get_active_env = get_active_env
    mod.cleanup_vm = cleanup_vm
    mod._cleanup_inactive_envs = _cleanup_inactive_envs
    mod.terminal_tool = terminal_tool
    return mod


def _tagged_generation(streaming) -> int:
    """Generation a fake creation path tags a fresh env with: the calling
    turn's captured generation when present, else the current global — the
    same policy the production ``_create_environment`` wrapper applies."""
    turn_gen = streaming._get_turn_generation()
    return turn_gen if turn_gen is not None else streaming._get_current_backend_generation()


class _FakeDockerEnv:
    """Faithful stand-in for the agent's ``DockerEnvironment`` teardown
    contract: ``cleanup(force_remove=True)`` starts a daemon stop/remove
    thread and returns immediately; ``wait_for_cleanup()`` joins it.
    """

    def __init__(self, generation=None, removal_seconds=0.1):
        self._webui_backend_generation = generation
        self.cleanup_force_remove = None
        self.removal_started = threading.Event()
        self.removal_done = threading.Event()
        self._cleanup_thread = None
        self._removal_delay = removal_seconds

    def cleanup(self, *, force_remove=False):
        self.cleanup_force_remove = force_remove
        self.removal_started.set()

        def _do_removal():
            time.sleep(self._removal_delay)
            self.removal_done.set()

        self._cleanup_thread = threading.Thread(target=_do_removal, daemon=True)
        self._cleanup_thread.start()

    def wait_for_cleanup(self, timeout=30.0):
        thread = getattr(self, "_cleanup_thread", None)
        if thread is None or not thread.is_alive():
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()


@pytest.fixture(autouse=True)
def _isolate_generation_state(monkeypatch):
    """Reset the streaming generation globals around every test."""
    streaming = importlib.import_module("api.streaming")
    with streaming._backend_generation_lock:
        streaming._current_backend_generation = 0
        streaming._env_backend_generations.clear()
        streaming._file_backend_generations.clear()
        streaming._last_backend_identity = None
        streaming._terminal_env_guard_installed = False
        streaming._active_turn_generations.clear()
        streaming._pending_eviction_generations = set()
    with streaming._in_flight_env_cleanups_lock:
        streaming._in_flight_env_cleanups.clear()
        streaming._in_flight_env_cleanup_failures.clear()
    streaming._TURN_BACKEND_GENERATION.set(None)
    streaming._TURN_BACKEND_CONFIG.set(None)
    yield


@pytest.fixture()
def fake_terminal_tool(monkeypatch):
    """Install the faithful fake agent terminal module + file_tools +
    code_execution_tool (all three paths the guard must cover)."""
    fake = _build_fake_terminal_tool_module()

    fake_file_tools = types.ModuleType("tools.file_tools")
    fake_file_tools.clear_file_ops_cache_calls = []
    fake_file_tools._file_ops_lock = threading.Lock()
    fake_file_tools._file_ops_cache = {}

    def clear_file_ops_cache(task_id):
        with fake_file_tools._file_ops_lock:
            if task_id:
                fake_file_tools._file_ops_cache.pop(task_id, None)
            else:
                fake_file_tools._file_ops_cache.clear()
        fake_file_tools.clear_file_ops_cache_calls.append(task_id)

    def _get_file_ops(task_id="default"):
        # Faithful to tools/file_tools.py:_get_file_ops: ensure the terminal
        # env exists (creating + tagging it via the SINGLE creation funnel),
        # then build/cache a file_ops wrapper around it.  The fast path
        # returns a cached wrapper whenever a live env occupies the slot —
        # it never re-verifies that the wrapper references that env.
        eff = fake._resolve_container_task_id(task_id)
        with fake._env_lock:
            env = fake._active_environments.get(eff)
            if env is None:
                config = fake._get_env_config()
                env = fake._create_environment(
                    env_type=config["env_type"], image="",
                    cwd=config.get("cwd", "/root"),
                    timeout=config.get("timeout", 180),
                    task_id=eff,
                )
                fake._active_environments[eff] = env
                fake._last_activity[eff] = time.time()
        with fake_file_tools._file_ops_lock:
            cached = fake_file_tools._file_ops_cache.get(eff)
        if cached is not None:
            return cached
        file_ops = _FakeFileOps(env)
        with fake_file_tools._file_ops_lock:
            fake_file_tools._file_ops_cache[eff] = file_ops
        return file_ops

    fake_file_tools.clear_file_ops_cache = clear_file_ops_cache
    fake_file_tools._get_file_ops = _get_file_ops

    fake_code_exec = types.ModuleType("tools.code_execution_tool")

    def _get_or_create_env(task_id):
        # Faithful to tools/code_execution_tool.py:_get_or_create_env.
        eff = fake._resolve_container_task_id(task_id)
        with fake._env_lock:
            env = fake._active_environments.get(eff)
            if env is None:
                config = fake._get_env_config()
                env = fake._create_environment(
                    env_type=config["env_type"], image="",
                    cwd=config.get("cwd", "/root"),
                    timeout=config.get("timeout", 180),
                    task_id=eff,
                )
                fake._active_environments[eff] = env
                fake._last_activity[eff] = time.time()
            return env, fake._get_env_config()["env_type"]

    fake_code_exec._get_or_create_env = _get_or_create_env

    fake.file_tools = fake_file_tools
    fake.code_execution_tool = fake_code_exec

    monkeypatch.setitem(sys.modules, "tools.terminal_tool", fake)
    monkeypatch.setitem(sys.modules, "tools.file_tools", fake_file_tools)
    monkeypatch.setitem(sys.modules, "tools.code_execution_tool", fake_code_exec)
    return fake


def test_guard_does_not_deadlock_on_first_terminal_call(fake_terminal_tool, monkeypatch):
    """Blocker 1: the pre-flight guard must not re-acquire the non-reentrant
    ``_env_lock`` via ``get_active_env()``.

    A real (non-reentrant) lock + a lock-taking accessor deadlock the first
    guarded call.  The guard must read the registry inline under the lock it
    already holds (or read first, lock second) — never call
    ``get_active_env()`` while holding ``_env_lock``.
    """
    streaming = importlib.import_module("api.streaming")

    streaming._install_terminal_env_generation_guard()

    # A live env under the CURRENT generation — the normal steady-state path.
    fake_terminal_tool._active_environments["default"] = _FakeEnv(generation=0)
    streaming._mark_env_backend_generation("default", 0)

    result = {}

    def _call():
        try:
            out = fake_terminal_tool.terminal_tool(
                "echo hi", task_id=None, session_id="s1"
            )
            result["ok"] = out
        except Exception as exc:  # pragma: no cover - failure path
            result["error"] = repr(exc)

    t = threading.Thread(target=_call)
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive(), (
        "guard deadlocked: _wrapped_terminal_tool entered tt._env_lock and "
        "called tt.get_active_env() which re-acquires the same non-reentrant lock"
    )
    assert "ok" in result
    assert result["ok"] == '{"output": "ok", "exit_code": 0}'
    assert fake_terminal_tool.terminal_tool_calls == ["echo hi"]


def test_guard_evicts_stale_generation_env_without_deadlock(fake_terminal_tool):
    """The guard must evict an env tagged with a stale generation and let the
    original tool recreate it — without deadlocking on _env_lock."""
    streaming = importlib.import_module("api.streaming")

    # Simulate: env was created under generation 0, backend identity changed,
    # generation bumped to 1.  The recorded owner is the NEW generation.
    stale_env = _FakeEnv(generation=0)
    fake_terminal_tool._active_environments["default"] = stale_env
    streaming._mark_env_backend_generation("default", 1)
    streaming._current_backend_generation = 1

    streaming._install_terminal_env_generation_guard()

    result = {}

    def _call():
        try:
            result["ok"] = fake_terminal_tool.terminal_tool(
                "whoami", task_id=None, session_id="s2"
            )
        except Exception as exc:  # pragma: no cover - failure path
            result["error"] = repr(exc)

    t = threading.Thread(target=_call)
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive(), "guard deadlocked on stale-env eviction path"

    # The stale gen-0 env was retired from the registry before the original
    # tool ran; the shared primitive installed a fresh env for the tool to
    # reuse (get-or-create in ONE transaction).
    live = fake_terminal_tool._active_environments.get("default")
    assert live is not None and live is not stale_env
    assert live._webui_backend_generation == 1
    assert stale_env.cleaned
    # Ownership for the replacement was published.
    assert streaming._get_expected_backend_generation("default") == 1
    assert result.get("ok") == '{"output": "ok", "exit_code": 0}'


def test_guard_compares_against_turn_captured_generation(fake_terminal_tool):
    """The acquisition boundary must compare against the CALLING TURN's
    captured generation — not the recorded owner (which a deferred transition
    could leave stale) and not the process-global current generation.

    Here the recorded owner is still 0 (the worst case the old code produced)
    while the env is tagged 0 and the calling turn is registered under gen 1:
    the guard must still refuse the env and retire it.
    """
    streaming = importlib.import_module("api.streaming")
    streaming._install_terminal_env_generation_guard()

    host_a_env = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    streaming._last_backend_identity = streaming._compute_backend_identity(host_a_env)
    streaming._env_backend_generations["default"] = 0
    streaming._current_backend_generation = 1
    env_a = _FakeEnv(generation=0)
    fake_terminal_tool._active_environments["default"] = env_a

    result = {}

    def _turn_b():
        # Same identity as _last_backend_identity → no bump → captures gen 1.
        streaming._begin_turn_generation(host_a_env)          # gen 1
        try:
            result["out"] = fake_terminal_tool.terminal_tool(
                "whoami", task_id=None, session_id="s3"
            )
        except Exception as exc:  # pragma: no cover - failure path
            result["error"] = repr(exc)
        finally:
            streaming._end_turn_generation()

    t = threading.Thread(target=_turn_b)
    t.start()
    t.join(timeout=10.0)
    assert not t.is_alive()
    # The gen-0 env was NOT reused by the gen-1 turn and was retired; the
    # gen-1 turn executed against a fresh env installed by the transaction.
    live = fake_terminal_tool._active_environments.get("default")
    assert live is not None and live is not env_a
    assert live._webui_backend_generation == 1
    assert env_a.cleaned
    assert result.get("out") == '{"output": "ok", "exit_code": 0}'


def test_begin_turn_publishes_ownership_after_identity_change(fake_terminal_tool):
    """After a backend identity change the begin transaction must (a) retire
    the stale env, (b) PUBLISH the new generation as owner so the next refresh
    can tag and future acquisitions can compare."""
    streaming = importlib.import_module("api.streaming")

    env_a = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    env_b = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-b"}
    assert streaming._compute_backend_identity(env_a) != streaming._compute_backend_identity(env_b)

    # Turn A: env created under gen 0, ownership published.
    streaming._last_backend_identity = streaming._compute_backend_identity(env_a)
    streaming._env_backend_generations["default"] = 0
    fake_terminal_tool._active_environments["default"] = _FakeEnv(generation=0)

    # Turn B: profile switches to a different SSH host — the begin transaction
    # compares/publishes the identity, bumps to gen 1 and retires the stale env.
    try:
        streaming._begin_turn_generation(env_b)

        # Stale env (gen 0) retired; new generation (1) published as owner.
        assert "default" not in fake_terminal_tool._active_environments
        assert streaming._env_backend_generations.get("default") == 1
        assert streaming._current_backend_generation == 1
    finally:
        streaming._end_turn_generation()


def test_refresh_tags_env_and_publishes_ownership(fake_terminal_tool):
    """_refresh_env_generation_tag publishes ownership ONLY for the env the
    calling turn actually acquired/used: the live object must already be
    tagged with the turn's captured generation (creation-time tagging or
    same-generation reuse).  Untagged/foreign envs are never touched."""
    streaming = importlib.import_module("api.streaming")

    host_a_env = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    streaming._last_backend_identity = streaming._compute_backend_identity(host_a_env)
    streaming._current_backend_generation = 0

    try:
        streaming._begin_turn_generation(host_a_env)          # gen 0 (no bump)
        env = _FakeEnv(generation=0)          # tagged by this turn's creation
        fake_terminal_tool._active_environments["default"] = env

        streaming._refresh_env_generation_tag()

        assert env._webui_backend_generation == 0
        assert streaming._env_backend_generations.get("default") == 0
    finally:
        streaming._end_turn_generation()


def test_refresh_does_not_tag_env_without_active_turn(fake_terminal_tool):
    """No turn bound (begin failed / non-WebUI): the refresh must NO-OP —
    it never tags or publishes an env it has no provenance over."""
    streaming = importlib.import_module("api.streaming")

    env = _FakeEnv(generation=None)
    fake_terminal_tool._active_environments["default"] = env

    streaming._refresh_env_generation_tag()

    assert env._webui_backend_generation is None
    assert "default" not in streaming._env_backend_generations


def test_finally_does_not_retag_foreign_env(fake_terminal_tool):
    """Must-fix 2: a finishing turn that acquired nothing must NOT retag an
    env created by another turn (the A-no-env/B-create/A-finish schedule).

    Turn A (gen 0) begins while no env exists; turn B (gen 1) begins after
    an identity switch and creates a gen-1 env; A finishes WITHOUT ever
    acquiring an environment.  A's finally (refresh + unregister) must leave
    B's live env tagged gen 1 and must NOT let the deferred gen-0 drain
    retire it while B is still using it.

    Two REAL threads: the ContextVar is per-context, so both turns must live
    on separate threads (nesting begins on one thread is not a valid
    schedule)."""
    streaming = importlib.import_module("api.streaming")

    host_a_env = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    host_b_env = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-b"}
    streaming._last_backend_identity = streaming._compute_backend_identity(host_a_env)
    streaming._current_backend_generation = 0

    a_registered = threading.Event()
    b_go = threading.Event()
    b_created = threading.Event()
    a_may_finish = threading.Event()
    b_may_finish = threading.Event()
    a_done = threading.Event()
    b_done = threading.Event()

    def _turn_a():
        streaming._begin_turn_generation(host_a_env)          # gen 0 (no bump)
        a_registered.set()
        assert a_may_finish.wait(timeout=15)
        # A's finally: NEVER retag B's env, then release the registration.
        streaming._refresh_env_generation_tag()
        streaming._end_turn_generation()
        a_done.set()

    def _turn_b():
        assert b_go.wait(timeout=15)
        streaming._begin_turn_generation(host_b_env)          # gen 1
        env_b = _FakeEnv(generation=1)
        fake_terminal_tool._active_environments["default"] = env_b
        b_created.set()
        assert b_may_finish.wait(timeout=15)
        streaming._end_turn_generation()
        b_done.set()

    ta = threading.Thread(target=_turn_a)
    tb = threading.Thread(target=_turn_b)
    ta.start()
    assert a_registered.wait(timeout=15)
    tb.start()
    b_go.set()
    assert b_created.wait(timeout=15)

    # A finishes WITHOUT acquiring anything.
    a_may_finish.set()
    assert a_done.wait(timeout=15)

    # B's live env was NOT retagged as gen 0 and NOT retired by the drain.
    env_b = fake_terminal_tool._active_environments.get("default")
    assert env_b is not None
    assert env_b._webui_backend_generation == 1
    assert not env_b.cleaned
    assert streaming._env_backend_generations.get("default") == 1
    assert streaming._pending_eviction_generations == set()

    b_may_finish.set()
    assert b_done.wait(timeout=15)
    ta.join(timeout=15)
    tb.join(timeout=15)
    assert not ta.is_alive() and not tb.is_alive()


def test_unknown_ownership_is_isolated_not_accepted_or_destroyed(fake_terminal_tool):
    """Unknown ownership must be isolated or enrolled, not accepted and not
    destroyed speculatively: a registered turn NEVER reuses an untagged env
    and NEVER tears it down (it may be in active use by an untracked
    caller) — the exact object is dropped from the registry untouched."""
    streaming = importlib.import_module("api.streaming")
    streaming._install_terminal_env_generation_guard()

    host_a_env = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    streaming._last_backend_identity = streaming._compute_backend_identity(host_a_env)
    streaming._current_backend_generation = 0

    untagged = _FakeEnv(generation=None)   # unknown provenance, possibly in use
    fake_terminal_tool._active_environments["default"] = untagged

    try:
        streaming._begin_turn_generation(host_a_env)          # gen 0
        out = fake_terminal_tool.terminal_tool("whoami", session_id="s-u")
    finally:
        streaming._end_turn_generation()

    # Isolated: the untagged env is no longer served by the registry and is
    # NOT cleaned up (unknown ownership is never destroyed speculatively);
    # the turn executes against a fresh env installed by the transaction.
    live = fake_terminal_tool._active_environments.get("default")
    assert live is not None and live is not untagged
    assert live._webui_backend_generation == 0
    assert not untagged.cleaned and not untagged.stopped
    assert out == '{"output": "ok", "exit_code": 0}'


def test_untagged_file_cache_entry_is_evicted_for_registered_turn(fake_terminal_tool):
    """Untagged file-cache entries fail CLOSED for a registered turn: the
    cached wrapper with no provenance is dropped before the original
    _get_file_ops runs, so it is never served to the turn."""
    streaming = importlib.import_module("api.streaming")
    streaming._install_terminal_env_generation_guard()

    host_a_env = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    streaming._last_backend_identity = streaming._compute_backend_identity(host_a_env)
    streaming._current_backend_generation = 0

    # Seed an UNTAGGED cached file_ops plus a live env for it to wrap.
    env = _FakeEnv(generation=0)
    fake_terminal_tool._active_environments["default"] = env
    stale_ops = _FakeFileOps(env)
    fake_terminal_tool.file_tools._file_ops_cache["default"] = stale_ops

    try:
        streaming._begin_turn_generation(host_a_env)          # gen 0
        ops = fake_terminal_tool.file_tools._get_file_ops("default")
    finally:
        streaming._end_turn_generation()

    assert ops is not stale_ops, "untagged cached file_ops must not be served"
    assert ops._webui_backend_generation == 0
    assert streaming._file_backend_generations.get("default") == 0


def test_empty_slot_acquisition_is_one_transaction(fake_terminal_tool):
    """Must-fix 1: validation and acquisition are ONE transaction.

    Generations A (gen 0) and B (gen 1) both race an EMPTY slot through a
    gate-controlled creation funnel.  On the buggy head (validate, release,
    then call the original acquirer) both validate the empty slot, A creates
    its env, and B's original fast path reuses A's object — B executes
    against A's generation.  With the transaction, B's reuse/create is
    atomic with its re-validation, so each turn executes only against an env
    tagged with ITS OWN captured generation.  The gate lives in the SINGLE
    creation funnel (``_create_environment``) — the real shared acquisition
    primitive the terminal wrapper drives.
    """
    streaming = importlib.import_module("api.streaming")

    host_a_env = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    host_b_env = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-b"}
    streaming._last_backend_identity = streaming._compute_backend_identity(host_a_env)
    streaming._current_backend_generation = 0

    first_gate = threading.Event()
    second_gate = threading.Event()
    used_tags = {}
    created_envs = []
    orig_create = fake_terminal_tool._create_environment

    def gated_create(
        env_type="local", image="", cwd="/root", timeout=180,
        ssh_config=None, container_config=None, local_config=None,
        task_id="default", host_cwd=None,
    ):
        # Two-stage gate in the creation funnel: on the buggy head both turns
        # can be mid-acquisition simultaneously (each validated the empty
        # slot); on the fixed head only the lock holder ever reaches creation.
        assert first_gate.wait(timeout=20), "first gate never opened"
        env = orig_create(
            env_type=env_type, image=image, cwd=cwd, timeout=timeout,
            ssh_config=ssh_config, container_config=container_config,
            local_config=local_config, task_id=task_id, host_cwd=host_cwd,
        )
        created_envs.append(env)
        assert second_gate.wait(timeout=20), "second gate never opened"
        return env

    fake_terminal_tool._create_environment = gated_create

    def recording_terminal_tool(
        command, background=False, timeout=None, task_id=None,
        session_id=None, force=False, workdir=None, pty=False,
        notify_on_complete=False, watch_patterns=None,
    ):
        # Execution half: records the generation of the env it executed
        # against (the env the guarded acquisition installed).
        with fake_terminal_tool._env_lock:
            env = fake_terminal_tool._active_environments.get("default")
        used_tags[command] = getattr(env, "_webui_backend_generation", None)
        return '{"output": "ok", "exit_code": 0}'

    fake_terminal_tool.terminal_tool = recording_terminal_tool
    streaming._install_terminal_env_generation_guard()

    a_result, b_result = {}, {}

    def _turn_a():
        streaming._begin_turn_generation(host_a_env)          # gen 0 (no bump)
        try:
            a_result["out"] = fake_terminal_tool.terminal_tool("cmd-a")
        except Exception as exc:  # pragma: no cover - failure path
            a_result["error"] = repr(exc)
        finally:
            streaming._refresh_env_generation_tag()
            streaming._end_turn_generation()

    def _turn_b():
        streaming._begin_turn_generation(host_b_env)          # gen 1
        try:
            b_result["out"] = fake_terminal_tool.terminal_tool("cmd-b")
        except Exception as exc:  # pragma: no cover - failure path
            b_result["error"] = repr(exc)
        finally:
            streaming._refresh_env_generation_tag()
            streaming._end_turn_generation()

    ta = threading.Thread(target=_turn_a)
    tb = threading.Thread(target=_turn_b)
    ta.start()
    time.sleep(0.2)      # A reaches the first gate inside its transaction
    tb.start()
    time.sleep(0.5)      # B: fixed head blocks (drain/lock); buggy head reaches gate
    first_gate.set()
    time.sleep(0.2)
    second_gate.set()
    ta.join(timeout=25)
    tb.join(timeout=25)
    assert not ta.is_alive() and not tb.is_alive()

    # Each turn executed against an env tagged with ITS OWN generation —
    # never against the other turn's object.
    assert used_tags.get("cmd-a") == 0, used_tags
    assert used_tags.get("cmd-b") == 1, used_tags
    assert "error" not in a_result and "error" not in b_result
    # B's env is the live one; A's env was retired only after A's turn exited.
    live = fake_terminal_tool._active_environments.get("default")
    assert live is not None and live._webui_backend_generation == 1
    assert len(created_envs) == 2
    assert created_envs[0].cleaned, "A's stale env must be retired after drain"


def test_begin_turn_skips_retire_when_env_replaced_by_newer_generation(fake_terminal_tool):
    """A concurrent turn already replaced the env with a newer generation:
    the begin transaction must NOT retire that newer env."""
    streaming = importlib.import_module("api.streaming")

    env_a = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    env_b = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-b"}
    streaming._last_backend_identity = streaming._compute_backend_identity(env_a)

    # Recorded owner is gen 0 (stale), but the registry already holds a
    # gen-1 env from a concurrent turn — the transition must leave it alone.
    streaming._env_backend_generations["default"] = 0
    fake_terminal_tool._active_environments["default"] = _FakeEnv(generation=1)

    try:
        streaming._begin_turn_generation(env_b)

        assert "default" in fake_terminal_tool._active_environments
        assert streaming._current_backend_generation == 1
    finally:
        streaming._end_turn_generation()


def test_active_use_defers_retire_until_turn_exits(fake_terminal_tool):
    """Active-use ownership: a stale env still executing an older turn must
    not be torn down; the retire is deferred until the turn exits."""
    streaming = importlib.import_module("api.streaming")

    env_a = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    env_b = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-b"}
    streaming._last_backend_identity = streaming._compute_backend_identity(env_a)
    streaming._env_backend_generations["default"] = 0
    fake_terminal_tool._active_environments["default"] = _FakeEnv(generation=0)

    # An older turn is still executing under gen 0.
    streaming._active_turn_generations[0] = 1

    try:
        streaming._begin_turn_generation(env_b)

        # Not retired yet — deferred (every pending generation, not a scalar).
        assert "default" in fake_terminal_tool._active_environments
        assert streaming._pending_eviction_generations == {0}

        # The older turn exits → the deferred retire is drained.
        streaming._active_turn_generations.pop(0, None)
        streaming._drain_pending_evictions()

        assert "default" not in fake_terminal_tool._active_environments
        assert streaming._pending_eviction_generations == set()
    finally:
        streaming._end_turn_generation()


def test_begin_end_turn_generation_roundtrip(fake_terminal_tool):
    """The turn lifecycle hooks bump/decrement the active-use counter and
    drain deferred evictions on the last exit.

    Uses a faithful TWO-THREAD schedule: the ContextVar is per-context, so a
    single thread can only hold one registration at a time — nesting two
    begins on one thread is not a valid schedule and left {0: 1} behind.
    """
    streaming = importlib.import_module("api.streaming")

    host_a_env = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    streaming._last_backend_identity = streaming._compute_backend_identity(host_a_env)

    a_registered = threading.Event()
    b_go = threading.Event()
    b_registered = threading.Event()
    a_go = threading.Event()
    b_may_finish = threading.Event()
    a_done = threading.Event()
    b_done = threading.Event()

    def _turn_a():
        streaming._begin_turn_generation(host_a_env)          # gen 0
        a_registered.set()
        assert a_go.wait(timeout=10)
        streaming._end_turn_generation()
        a_done.set()

    def _turn_b():
        assert b_go.wait(timeout=10)
        streaming._begin_turn_generation(host_a_env)          # gen 0
        b_registered.set()
        assert b_may_finish.wait(timeout=10)
        streaming._end_turn_generation()
        b_done.set()

    ta = threading.Thread(target=_turn_a)
    tb = threading.Thread(target=_turn_b)
    ta.start()
    assert a_registered.wait(timeout=10)
    assert streaming._active_turn_generations.get(0) == 1

    tb.start()
    b_go.set()
    assert b_registered.wait(timeout=10)
    # Second concurrent turn on the same generation (separate thread).
    assert streaming._active_turn_generations.get(0) == 2

    a_go.set()
    assert a_done.wait(timeout=10)
    # B is still registered — only A's registration was released.
    assert streaming._active_turn_generations.get(0) == 1

    b_may_finish.set()
    assert b_done.wait(timeout=10)
    ta.join(timeout=10)
    tb.join(timeout=10)
    assert not ta.is_alive() and not tb.is_alive()
    assert 0 not in streaming._active_turn_generations


def test_guard_installs_only_once(fake_terminal_tool):
    """Installing the guard is idempotent — no wrapper-on-wrapper stacking."""
    streaming = importlib.import_module("api.streaming")

    streaming._install_terminal_env_generation_guard()
    first_wrapper = fake_terminal_tool.terminal_tool
    streaming._install_terminal_env_generation_guard()
    assert fake_terminal_tool.terminal_tool is first_wrapper


def _run_two_thread_backend_switch(streaming, fake, acquire, completion_order):
    """Deterministic A-active/B-start backend-switch schedule (REAL threads).

    - Turn A begins under generation 0 (profile host-a) with a live gen-0
      env in the registry.
    - Turn B starts with profile host-b: the begin transaction bumps to gen 1
      and B captures gen 1.
    - B's acquisition (``acquire(streaming, fake)``) must never reuse env_a
      and never tear it down while A is still active.
    - completion_order=1: B's acquisition blocks (drain wait) until A exits.
    - completion_order=2: A exits first (drain retires env_a) and only then
      does B's acquisition run — it must not block at all.
    """
    host_a_env = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    host_b_env = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-b"}
    streaming._last_backend_identity = streaming._compute_backend_identity(host_a_env)
    streaming._env_backend_generations["default"] = 0
    env_a = _FakeEnv(generation=0)
    fake._active_environments["default"] = env_a

    a_registered = threading.Event()
    a_may_finish = threading.Event()
    a_finished = threading.Event()
    b_setup_done = threading.Event()
    b_may_acquire = threading.Event()
    b_result = {}

    real_wait = streaming._wait_for_generation_drain
    b_waited = threading.Event()

    def _hooked_wait(generation, timeout=None):
        b_waited.set()
        return real_wait(generation, timeout=timeout)

    streaming._wait_for_generation_drain = _hooked_wait
    try:
        def _turn_a():
            streaming._begin_turn_generation(host_a_env)      # gen 0 (no bump)
            a_registered.set()
            try:
                assert a_may_finish.wait(timeout=15), "turn A never released"
            finally:
                streaming._end_turn_generation()
                a_finished.set()

        def _turn_b():
            # Identity change + generation capture + active-use increment are
            # ONE transaction (the invalidate is folded into the begin).
            streaming._begin_turn_generation(host_b_env)      # gen 1
            b_setup_done.set()
            try:
                assert b_may_acquire.wait(timeout=15), "turn B never allowed to acquire"
                b_result["out"] = acquire(streaming, fake)
            finally:
                streaming._end_turn_generation()

        ta = threading.Thread(target=_turn_a)
        ta.start()
        # A must be registered BEFORE B's setup runs, so B's transition sees
        # an active gen-0 turn and defers the retire (deterministic schedule).
        assert a_registered.wait(timeout=15)

        tb = threading.Thread(target=_turn_b)
        tb.start()
        assert b_setup_done.wait(timeout=15)
        assert streaming._active_turn_generations.get(0) == 1
        assert streaming._active_turn_generations.get(1) == 1

        if completion_order == 2:
            # A finishes before B's acquisition: the deferred retire is drained
            # on A's exit, so B never blocks and never sees env_a.
            a_may_finish.set()
            assert a_finished.wait(timeout=15)
            assert streaming._pending_eviction_generations == set()
            b_may_acquire.set()
            tb.join(timeout=15)
            ta.join(timeout=15)
            assert not tb.is_alive() and not ta.is_alive()
            assert not b_waited.is_set(), "order 2 must not block on drain"
        else:
            # B's acquisition starts while A is active: it must block on the
            # drain (never reuse/tear down env_a), then proceed after A exits.
            b_may_acquire.set()
            assert b_waited.wait(timeout=15), "B's acquisition did not defer to A's drain"
            assert fake._active_environments.get("default") is env_a, (
                "B must not tear down env_a while turn A is still active")
            assert streaming._pending_eviction_generations == {0}
            a_may_finish.set()
            ta.join(timeout=15)
            tb.join(timeout=15)
            assert not tb.is_alive() and not ta.is_alive()

        b_result["env_a"] = env_a
        b_result["env_a_cleaned"] = env_a.cleaned
        b_result["live"] = fake._active_environments.get("default")
        return b_result
    finally:
        streaming._wait_for_generation_drain = real_wait


@pytest.mark.parametrize("completion_order", [1, 2])
@pytest.mark.parametrize("tool", ["terminal", "file", "code_exec"])
def test_two_thread_backend_switch_no_reuse_or_teardown(
    tool, completion_order, fake_terminal_tool,
):
    """REAL two-thread A-active/B-start coverage for EVERY acquisition path
    (terminal, file, execute_code) and BOTH completion orders: the new-backend
    turn must neither reuse nor tear down the old-backend env."""
    streaming = importlib.import_module("api.streaming")
    streaming._install_terminal_env_generation_guard()

    def acquire(streaming, fake):
        if tool == "terminal":
            return fake.terminal_tool("echo B", session_id="b")
        if tool == "file":
            return fake.file_tools._get_file_ops("default")
        return fake.code_execution_tool._get_or_create_env("default")

    result = _run_two_thread_backend_switch(
        streaming, fake_terminal_tool, acquire, completion_order)

    assert result["env_a_cleaned"], f"{tool}: stale gen-0 env must be retired"
    live = result["live"]
    assert live is not None, f"{tool}: fresh env must exist after the switch"
    assert live is not result["env_a"], f"{tool}: B must not reuse A's env"
    assert live._webui_backend_generation == 1, f"{tool}: B's env must be tagged gen 1"
    assert streaming._pending_eviction_generations == set()
    if tool == "file":
        fops = fake_terminal_tool.file_tools._file_ops_cache.get("default")
        assert fops is not None and fops._webui_backend_generation == 1
    if tool == "code_exec":
        assert isinstance(result["out"], tuple) and result["out"][0] is live


def test_failed_identity_transition_is_fail_closed(fake_terminal_tool, monkeypatch):
    """Fail-closed transition: a raising identity publish REFUSES the turn —
    nothing is bound and no active-use registration leaks — and the guaranteed
    try/finally's ``_end_turn_generation()`` is a safe no-op afterwards."""
    streaming = importlib.import_module("api.streaming")

    def _boom(profile_runtime_env):
        raise RuntimeError("identity transition failed")

    monkeypatch.setattr(streaming, "_publish_backend_identity", _boom)

    with pytest.raises(RuntimeError, match="identity transition failed"):
        streaming._begin_turn_generation(
            {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-b"})

    # Fail-closed: nothing registered, ContextVar unbound.
    assert streaming._active_turn_generations == {}
    assert streaming._get_turn_generation() is None

    # The production finally (end_turn_generation) is a safe no-op.
    streaming._end_turn_generation()
    assert streaming._active_turn_generations == {}
    assert streaming._get_turn_generation() is None


def test_replacement_between_read_and_retire_not_torn_down(fake_terminal_tool):
    """A replacement installed under the same key between the stale env's
    removal and its teardown must survive: retire cleans up the EXACT removed
    object outside the registry lock, never a by-key pop of whatever is there
    now."""
    streaming = importlib.import_module("api.streaming")

    env_a = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    env_b = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-b"}
    streaming._last_backend_identity = streaming._compute_backend_identity(env_a)
    streaming._env_backend_generations["default"] = 0
    e0 = _FakeEnv(generation=0)
    installed = {}

    def _cleanup_installs_replacement():
        # Simulates a concurrent thread installing a fresh gen-1 env while the
        # stale env is being retired (outside the registry lock).
        e0.cleaned = True
        repl = _FakeEnv(generation=1)
        fake_terminal_tool._active_environments["default"] = repl
        installed["repl"] = repl

    e0.cleanup = _cleanup_installs_replacement
    fake_terminal_tool._active_environments["default"] = e0

    try:
        streaming._begin_turn_generation(env_b)

        repl = fake_terminal_tool._active_environments.get("default")
        assert repl is installed.get("repl"), "the replacement must survive the retire"
        assert repl._webui_backend_generation == 1
        assert not repl.cleaned, "the replacement must never be torn down"
        assert e0.cleaned, "the exact stale object must be retired"
    finally:
        streaming._end_turn_generation()


def test_terminal_validation_happens_before_execution(fake_terminal_tool):
    """Fix 1: ownership is validated BEFORE the command executes, and the
    acquisition lock is released before execution.

    The old wrapper passed the ENTIRE installed ``terminal_tool`` (which
    acquires the env AND calls ``env.execute()``) as the acquire fn, so the
    final exact-object check ran only after command side effects, while the
    acquisition lock was held across approval/execution/retries.  Here the
    REAL shared primitive and the REAL wrapper record the order: the final
    ownership check must precede the original tool's execution, and the
    ``_backend_acquisition_lock`` must NOT be held during execution.
    """
    streaming = importlib.import_module("api.streaming")
    order = []
    orig_final = streaming._final_ownership_check

    def recorded_final(tt, effective_task_id, raw_task_id, turn_gen):
        order.append("final_check")
        return orig_final(tt, effective_task_id, raw_task_id, turn_gen)

    streaming._final_ownership_check = recorded_final

    def recording_terminal_tool(
        command, background=False, timeout=None, task_id=None,
        session_id=None, force=False, workdir=None, pty=False,
        notify_on_complete=False, watch_patterns=None,
    ):
        order.append(("execution", streaming._backend_acquisition_lock.locked()))
        return '{"output": "ok", "exit_code": 0}'

    fake_terminal_tool.terminal_tool = recording_terminal_tool
    streaming._install_terminal_env_generation_guard()

    host_a_env = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    streaming._last_backend_identity = streaming._compute_backend_identity(host_a_env)
    streaming._current_backend_generation = 0

    try:
        streaming._begin_turn_generation(host_a_env)          # gen 0
        out = fake_terminal_tool.terminal_tool("echo hi", session_id="s-v")
    finally:
        streaming._end_turn_generation()

    assert order[0] == "final_check", (
        "the final exact-object ownership check must run BEFORE the command "
        f"executes (order was {order})")
    assert order[1][0] == "execution", order
    assert order[1][1] is False, (
        "the acquisition lock must be RELEASED before the command executes "
        "(it serialized terminal/file/execute-code acquisition across "
        "approval + foreground execution + retries on the buggy head)")
    assert out == '{"output": "ok", "exit_code": 0}'


def test_terminal_executes_only_against_validated_generation(fake_terminal_tool):
    """Fix 1 (behavioral): a stale foreign env is retired BEFORE the command
    runs — the command executes only against an env tagged with the calling
    turn's generation, and never against the foreign object."""
    streaming = importlib.import_module("api.streaming")
    streaming._install_terminal_env_generation_guard()

    host_a_env = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    host_b_env = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-b"}
    streaming._last_backend_identity = streaming._compute_backend_identity(host_a_env)
    streaming._current_backend_generation = 0
    foreign = _FakeEnv(generation=0)
    fake_terminal_tool._active_environments["default"] = foreign

    try:
        streaming._begin_turn_generation(host_b_env)          # gen 1
        out = fake_terminal_tool.terminal_tool("whoami", session_id="s-f")
    finally:
        streaming._end_turn_generation()

    # The foreign gen-0 env was retired and NEVER executed against; the
    # live env is the fresh turn-generation object.
    live = fake_terminal_tool._active_environments.get("default")
    assert live is not None and live is not foreign
    assert live._webui_backend_generation == 1
    assert foreign.cleaned
    assert out == '{"output": "ok", "exit_code": 0}'
    executed = fake_terminal_tool.executed_envs[-1]
    assert executed[0] == "whoami"
    assert executed[1] is not foreign
    assert executed[1]._webui_backend_generation == 1


def test_selected_profile_config_wins_over_process_global(fake_terminal_tool, monkeypatch):
    """Fix 2: construction consumes the turn's immutable normalized config —
    selected profile A wins even when a concurrent profile turn replaced
    process-global os.environ with profile B values between A's identity
    capture and A's real constructor read.

    The fingerprint, the owner publish and every real constructor path
    (``_get_env_config`` -> ``_create_environment``) consume the SAME
    normalized object bound to the turn ContextVar; os.environ is never the
    later construction authority.
    """
    streaming = importlib.import_module("api.streaming")
    streaming._install_terminal_env_generation_guard()

    profile_a = {
        "TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a",
        "TERMINAL_SSH_USER": "user-a", "TERMINAL_CWD": "/workspace-a",
    }
    profile_b = {
        "TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-b",
        "TERMINAL_SSH_USER": "user-b", "TERMINAL_CWD": "/workspace-b",
    }
    streaming._last_backend_identity = streaming._compute_backend_identity(profile_a)
    streaming._current_backend_generation = 0

    # Concurrent profile B rewrote the process-global env AFTER A's setup.
    for key, value in profile_b.items():
        monkeypatch.setenv(key, value)

    try:
        streaming._begin_turn_generation(profile_a)           # turn A bound
        # The construction authority returns A's values, not B's globals.
        config = fake_terminal_tool._get_env_config()          # wrapped
        assert config["ssh_host"] == "host-a", config
        assert config["ssh_user"] == "user-a", config
        assert config["cwd"] == "/workspace-a", config
        # The fingerprint was computed from the SAME normalized object.
        bound = streaming._get_turn_backend_config()
        assert bound is not None
        assert streaming._compute_backend_identity(bound) == streaming._last_backend_identity
        # The REAL creation funnel consumed the A config (ssh backend).
        out = fake_terminal_tool.terminal_tool("whoami", session_id="s-c")
        created = fake_terminal_tool.created_configs[-1]
        assert created["ssh_config"]["host"] == "host-a", created
        assert created["ssh_config"]["user"] == "user-a", created
        assert out == '{"output": "ok", "exit_code": 0}'
    finally:
        streaming._end_turn_generation()

    # The turn config ContextVar is cleared on teardown.
    assert streaming._get_turn_backend_config() is None


def test_file_wrapper_requires_exact_validated_env_identity(fake_terminal_tool):
    """Fix 3: the returned file_ops wrapper must reference the EXACT
    validated registry object.  The installed fast path returns a cached
    wrapper whenever a live env occupies the task slot, so a cached wrapper
    around replaced env X must never be blessed/served while registry env Y
    is what passed the ownership check (same-key, same-generation
    replacement — the tag alone cannot distinguish them)."""
    streaming = importlib.import_module("api.streaming")
    streaming._install_terminal_env_generation_guard()

    host_a_env = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    streaming._last_backend_identity = streaming._compute_backend_identity(host_a_env)
    streaming._current_backend_generation = 0

    env_x = _FakeEnv(generation=0)
    fake_terminal_tool._active_environments["default"] = env_x
    cached_x = _FakeFileOps(env_x)
    cached_x._webui_backend_generation = 0     # same-generation tag
    fake_terminal_tool.file_tools._file_ops_cache["default"] = cached_x

    try:
        streaming._begin_turn_generation(host_a_env)          # gen 0
        # Same-key replacement: env Y replaces X WITHOUT a generation bump.
        env_y = _FakeEnv(generation=0)
        fake_terminal_tool._active_environments["default"] = env_y
        ops = fake_terminal_tool.file_tools._get_file_ops("default")
    finally:
        streaming._end_turn_generation()

    assert ops is not cached_x, (
        "the cached wrapper around replaced env X must never be served")
    assert getattr(ops, "env", None) is env_y, (
        "the returned wrapper must reference the exact validated registry env Y")
    assert ops._webui_backend_generation == 0
    assert streaming._file_backend_generations.get("default") == 0
    # The stale cached wrapper was dropped from the cache.
    cached_now = fake_terminal_tool.file_tools._file_ops_cache.get("default")
    assert cached_now is None or cached_now is ops


def test_execute_code_result_requires_exact_validated_env_identity(fake_terminal_tool):
    """Fix 3 (execute_code): the result env must be the exact validated
    registry object before it is tagged or handed to the caller."""
    streaming = importlib.import_module("api.streaming")
    streaming._install_terminal_env_generation_guard()

    host_a_env = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    streaming._last_backend_identity = streaming._compute_backend_identity(host_a_env)
    streaming._current_backend_generation = 0

    try:
        streaming._begin_turn_generation(host_a_env)          # gen 0
        result = fake_terminal_tool.code_execution_tool._get_or_create_env("default")
        env, env_type = result
        live = fake_terminal_tool._active_environments["default"]
        assert env is live, "the execute-code env must be the exact registry object"
        assert env._webui_backend_generation == 0
        assert env_type == "ssh"
    finally:
        streaming._end_turn_generation()


def test_forced_docker_teardown_completes_before_replacement(fake_terminal_tool):
    """Fix 4: physical Docker invalidation is FENCED — the forced teardown
    (daemon stop/remove thread + ``wait_for_cleanup``) COMPLETES before the
    retire returns, so replacement construction can never reattach by labels
    to a still-removing container, and the stale cleanup thread can never
    remove the replacement's live container."""
    streaming = importlib.import_module("api.streaming")

    env_a = {"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_IMAGE": "img-a"}
    env_b = {"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_IMAGE": "img-b"}
    streaming._last_backend_identity = streaming._compute_backend_identity(env_a)
    streaming._env_backend_generations["default"] = 0
    stale = _FakeDockerEnv(generation=0, removal_seconds=0.4)
    fake_terminal_tool._active_environments["default"] = stale
    streaming._install_terminal_env_generation_guard()

    try:
        streaming._begin_turn_generation(env_b)   # identity change -> retire stale
        # The forced teardown COMPLETED before begin returned (the old head
        # returned immediately, leaving the daemon removal in flight):
        assert stale.removal_done.is_set(), (
            "forced Docker teardown must finish before the retire returns")
        assert stale.cleanup_force_remove is True
        assert "default" not in fake_terminal_tool._active_environments
        # Replacement construction is gated on the completed removal:
        out = fake_terminal_tool.terminal_tool("echo fresh", session_id="s-d")
        fresh = fake_terminal_tool._active_environments["default"]
        assert fresh is not stale
        assert fresh._webui_backend_generation == 1
        assert stale.removal_done.is_set()
        assert out == '{"output": "ok", "exit_code": 0}'
    finally:
        streaming._end_turn_generation()


def test_forced_docker_teardown_timeout_fails_closed(fake_terminal_tool, monkeypatch):
    """Fix 4: a forced teardown that does not finish is FAIL-CLOSED — the
    turn is refused and no replacement is created; later acquisitions for the
    same task key are refused too (a replacement must not attach to a
    container in an unknown removal state)."""
    streaming = importlib.import_module("api.streaming")
    monkeypatch.setenv("HERMES_WEBUI_BACKEND_CLEANUP_TIMEOUT", "0.05")

    env_a = {"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_IMAGE": "img-a"}
    env_b = {"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_IMAGE": "img-b"}
    streaming._last_backend_identity = streaming._compute_backend_identity(env_a)
    streaming._env_backend_generations["default"] = 0
    stuck = _FakeDockerEnv(generation=0, removal_seconds=5.0)   # >> 0.05s
    fake_terminal_tool._active_environments["default"] = stuck

    with pytest.raises(RuntimeError, match="did not finish"):
        streaming._begin_turn_generation(env_b)
    streaming._end_turn_generation()   # guaranteed outer finally: safe no-op
    assert streaming._get_turn_generation() is None
    # Fail-closed: nothing replaced the stale env, and the teardown fence
    # refuses later creation for the same key.
    assert "default" not in fake_terminal_tool._active_environments
    with pytest.raises(RuntimeError, match="fail"):
        streaming._validate_backend_ownership(fake_terminal_tool, "default", None)


def test_terminal_retirement_deferred_while_command_executes(fake_terminal_tool):
    """Blocker 1 (2026-08-06 review): acquisition and execution share ONE
    authority.  A retirement attempted while a guarded terminal command is
    executing against the validated env must be DEFERRED (execution lease) —
    the installed ``terminal_tool`` re-reads the registry by key right before
    ``env.execute()``, so replacing the object mid-command would make the
    command run against a non-validated replacement.  The command completes
    against the exact validated object; retirement proceeds afterwards.
    """
    streaming = importlib.import_module("api.streaming")
    started = threading.Event()
    release = threading.Event()
    executed_against = []

    def gated_terminal_tool(
        command, background=False, timeout=None, task_id=None,
        session_id=None, force=False, workdir=None, pty=False,
        notify_on_complete=False, watch_patterns=None,
    ):
        # Execution half of the installed tool: the SECOND by-key lookup,
        # then env.execute() against whatever it found.
        eff = fake_terminal_tool._resolve_container_task_id(task_id)
        with fake_terminal_tool._env_lock:
            env = (
                fake_terminal_tool._active_environments.get(eff)
                or fake_terminal_tool._active_environments.get(task_id)
            )
        executed_against.append(env)
        started.set()
        assert release.wait(timeout=15), "release gate never opened"
        return '{"output": "ok", "exit_code": 0}'

    fake_terminal_tool.terminal_tool = gated_terminal_tool
    streaming._install_terminal_env_generation_guard()

    env_a = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    streaming._last_backend_identity = streaming._compute_backend_identity(env_a)
    streaming._current_backend_generation = 0

    result = {}

    def _run_command():
        # The Agent's parallel-tool worker bridge runs the tool on a child
        # thread and propagates ContextVars — copy_context() is the faithful
        # shape, so the turn's captured generation reaches the wrapper.
        result["out"] = fake_terminal_tool.terminal_tool(
            "long cmd", session_id="s-l")

    try:
        streaming._begin_turn_generation(env_a)          # gen 0
        ctx = contextvars.copy_context()
        thread = threading.Thread(
            target=lambda: ctx.run(_run_command), daemon=True)
        thread.start()
        assert started.wait(timeout=15), "command never started"
        validated = fake_terminal_tool._active_environments.get("default")
        assert validated is not None
        # Mid-command retirement attempt (turn-exit drain / identity switch
        # would call exactly this path): must be DEFERRED by the lease.
        streaming._retire_generation_env(0, "default")
        assert fake_terminal_tool._active_environments.get("default") is validated
        assert not validated.cleaned
        release.set()
        thread.join(timeout=15)
        assert result["out"] == '{"output": "ok", "exit_code": 0}'
        # The second lookup found the SAME validated object — no replacement.
        assert executed_against[0] is validated
    finally:
        # Turn exit drains the deferred eviction: the now-idle env is
        # retired and physically cleaned.
        streaming._end_turn_generation()
    assert "default" not in fake_terminal_tool._active_environments
    assert validated.cleaned


def test_reaper_does_not_reap_env_mid_execution(fake_terminal_tool):
    """Blocker 1: the agent's idle reaper (``_cleanup_inactive_envs``, which
    the installed terminal_tool STARTS on every call and which pops envs
    DIRECTLY from the registry) must not remove an environment a guarded
    terminal command is executing against — the by-key second lookup would
    then create a fresh, never-validated env and run the command there.  A
    leased env is kept alive (activity refresh) until the command finishes.
    """
    streaming = importlib.import_module("api.streaming")
    started = threading.Event()
    release = threading.Event()
    executed_against = []

    def gated_terminal_tool(
        command, background=False, timeout=None, task_id=None,
        session_id=None, force=False, workdir=None, pty=False,
        notify_on_complete=False, watch_patterns=None,
    ):
        eff = fake_terminal_tool._resolve_container_task_id(task_id)
        with fake_terminal_tool._env_lock:
            env = (
                fake_terminal_tool._active_environments.get(eff)
                or fake_terminal_tool._active_environments.get(task_id)
            )
        executed_against.append(env)
        started.set()
        assert release.wait(timeout=15), "release gate never opened"
        return '{"output": "ok", "exit_code": 0}'

    fake_terminal_tool.terminal_tool = gated_terminal_tool
    streaming._install_terminal_env_generation_guard()

    env_a = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    streaming._last_backend_identity = streaming._compute_backend_identity(env_a)
    streaming._current_backend_generation = 0

    result = {}

    def _run_command():
        result["out"] = fake_terminal_tool.terminal_tool(
            "long cmd", session_id="s-r")

    try:
        streaming._begin_turn_generation(env_a)          # gen 0
        ctx = contextvars.copy_context()
        thread = threading.Thread(
            target=lambda: ctx.run(_run_command), daemon=True)
        thread.start()
        assert started.wait(timeout=15), "command never started"
        validated = fake_terminal_tool._active_environments.get("default")
        assert validated is not None
        # Force the env idle (as a long-running command with no background
        # process would be), then sweep with a 10s lifetime: at 60s idle the
        # unfenced reaper would reap it; the fence must keep the env a
        # guarded command is executing against alive.
        fake_terminal_tool._last_activity["default"] = time.time() - 60
        fake_terminal_tool._cleanup_inactive_envs(10)
        assert fake_terminal_tool._active_environments.get("default") is validated
        assert not validated.cleaned
        release.set()
        thread.join(timeout=15)
        assert result["out"] == '{"output": "ok", "exit_code": 0}'
        assert executed_against[0] is validated
    finally:
        streaming._end_turn_generation()
    # Once idle, a later sweep reaps the env normally.
    fake_terminal_tool._cleanup_inactive_envs(0)
    assert "default" not in fake_terminal_tool._active_environments
    assert validated.cleaned


def test_fail_closed_gate_refuses_registry_swap_after_validation(
    fake_terminal_tool, monkeypatch,
):
    """Blocker 1: execution never begins against a REPLACEMENT.  If a foreign
    actor swaps the registry object in the window AFTER the ownership
    transaction's final check and BEFORE the pre-execution gate, the wrapper
    refuses (fail-closed) instead of executing on the replacement.
    """
    streaming = importlib.import_module("api.streaming")
    streaming._install_terminal_env_generation_guard()

    env_a = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    streaming._last_backend_identity = streaming._compute_backend_identity(env_a)
    streaming._current_backend_generation = 0

    real_acquire = streaming._acquire_backend_object

    def swapping_acquire(
        tt, effective_task_id, raw_task_id, acquire_fn, on_acquired=None,
    ):
        result = real_acquire(
            tt, effective_task_id, raw_task_id, acquire_fn,
            on_acquired=on_acquired,
        )
        # The foreign actor acts between the transaction's final ownership
        # check and the wrapper's post-lock pre-execution gate: swap the
        # validated object for another env under the same key.
        with tt._env_lock:
            tt._active_environments[effective_task_id] = _FakeEnv(generation=0)
        return result

    monkeypatch.setattr(streaming, "_acquire_backend_object", swapping_acquire)

    try:
        streaming._begin_turn_generation(env_a)          # gen 0
        with pytest.raises(RuntimeError, match="changed after ownership validation"):
            fake_terminal_tool.terminal_tool("echo hi", session_id="s-g")
    finally:
        streaming._end_turn_generation()
    # No command ran against the replacement.
    assert fake_terminal_tool.executed_envs == []


def test_docker_teardown_verifies_physical_removal_fail_closed(
    fake_terminal_tool, monkeypatch,
):
    """Blocker 3 (2026-08-06 review): ``wait_for_cleanup()`` only proves the
    worker thread ended — the installed Docker worker ignores ``docker
    stop``/``rm -f`` return codes, so a failed removal still reports success
    while the old container and labels remain.  The retired container
    identity must be verified GONE before replacement is permitted; a
    still-existing container FAILS CLOSED and blocks later replacement.
    """
    streaming = importlib.import_module("api.streaming")

    env_a = {"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_IMAGE": "img-a"}
    env_b = {"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_IMAGE": "img-b"}
    streaming._last_backend_identity = streaming._compute_backend_identity(env_a)
    streaming._env_backend_generations["default"] = 0
    stale = _FakeDockerEnv(generation=0, removal_seconds=0.05)
    stale._container_id = "c0ffee1234567890"
    stale._docker_exe = "docker"
    fake_terminal_tool._active_environments["default"] = stale
    streaming._install_terminal_env_generation_guard()

    monkeypatch.setattr(streaming, "_docker_container_exists", lambda exe, cid: True)

    with pytest.raises(RuntimeError, match="still exists after forced teardown"):
        streaming._begin_turn_generation(env_b)
    streaming._end_turn_generation()
    # Fail-closed: the teardown fence refuses later creation for the key.
    assert "default" not in fake_terminal_tool._active_environments
    with pytest.raises(RuntimeError, match="fail"):
        streaming._validate_backend_ownership(fake_terminal_tool, "default", None)


def test_docker_teardown_physical_removal_verified_ok(fake_terminal_tool, monkeypatch):
    """Blocker 3 happy path: once the retired container identity is confirmed
    gone, replacement construction proceeds normally.
    """
    streaming = importlib.import_module("api.streaming")
    monkeypatch.setattr(streaming, "_docker_container_exists", lambda exe, cid: False)

    env_a = {"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_IMAGE": "img-a"}
    env_b = {"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_IMAGE": "img-b"}
    streaming._last_backend_identity = streaming._compute_backend_identity(env_a)
    streaming._env_backend_generations["default"] = 0
    stale = _FakeDockerEnv(generation=0, removal_seconds=0.05)
    stale._container_id = "c0ffee1234567890"
    stale._docker_exe = "docker"
    fake_terminal_tool._active_environments["default"] = stale
    streaming._install_terminal_env_generation_guard()

    try:
        streaming._begin_turn_generation(env_b)   # retire stale + create fresh
        out = fake_terminal_tool.terminal_tool("echo fresh", session_id="s-d2")
        fresh = fake_terminal_tool._active_environments["default"]
        assert fresh is not stale
        assert fresh._webui_backend_generation == 1
        assert out == '{"output": "ok", "exit_code": 0}'
    finally:
        streaming._end_turn_generation()


def test_profile_terminal_mappings_cover_canonical_schema(fake_terminal_tool):
    """Blocker 2 (2026-08-06 review): the WebUI profile snapshot must cover
    every field the agent's canonical terminal schema supports
    (``TERMINAL_CONFIG_ENV_MAP`` in hermes_cli/config.py) — a profile that
    configures e.g. ``docker_shm_size`` / ``vercel_runtime`` / ``sandbox_dir``
    in config.yaml must not silently fall back to WebUI hard-coded defaults —
    and every effective constructor input must stay in the turn's immutable
    snapshot (the same object the identity fingerprint is computed from).
    """
    import api.profiles as profiles
    streaming = importlib.import_module("api.streaming")

    # Mirror of hermes_cli/config.py:TERMINAL_CONFIG_ENV_MAP (agent side).
    canonical = {
        "backend", "modal_mode", "degraded_mode", "cwd", "timeout",
        "lifetime_seconds", "docker_image", "docker_forward_env",
        "singularity_image", "modal_image", "daytona_image", "vercel_runtime",
        "ssh_host", "ssh_user", "ssh_port", "ssh_key", "container_cpu",
        "container_memory", "container_disk", "container_persistent",
        "docker_volumes", "docker_env", "docker_mount_cwd_to_workspace",
        "docker_network", "docker_extra_args", "docker_shm_size",
        "docker_run_as_host_user", "docker_persist_across_processes",
        "docker_orphan_reaper", "sandbox_dir", "persistent_shell",
    }
    missing = canonical - set(profiles._TERMINAL_ENV_MAPPINGS)
    assert not missing, (
        "profile terminal mapping omits canonical fields: %s" % sorted(missing))

    # Every mapped canonical field must also be part of the backend identity
    # fingerprint, so the immutable turn snapshot cannot diverge from the
    # identity that was published.
    mapped_vars = {profiles._TERMINAL_ENV_MAPPINGS[k] for k in canonical}
    missing_identity = mapped_vars - set(streaming._BACKEND_IDENTITY_KEYS)
    assert not missing_identity, (
        "backend identity keys omit mapped fields: %s" % sorted(missing_identity))

    # End-to-end: a profile runtime env configuring the previously-omitted
    # fields flows through the real constructor input (the wrapped
    # _get_env_config -> _create_environment) from the SAME immutable
    # snapshot the fingerprint was computed from.
    streaming._install_terminal_env_generation_guard()
    profile_env = {
        "TERMINAL_ENV": "docker",
        "TERMINAL_DOCKER_IMAGE": "img-a",
        "TERMINAL_DOCKER_SHM_SIZE": "2g",
        "TERMINAL_DOCKER_NETWORK": "false",
        "TERMINAL_DOCKER_EXTRA_ARGS": '["--cpus=2"]',
        "TERMINAL_DOCKER_RUN_AS_HOST_USER": "true",
        "TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES": "false",
        "TERMINAL_DOCKER_ORPHAN_REAPER": "false",
        "TERMINAL_VERCEL_RUNTIME": "nodejs20",
        "TERMINAL_SANDBOX_DIR": "/sandbox",
        "TERMINAL_DEGRADED_MODE": "true",
    }
    streaming._last_backend_identity = streaming._compute_backend_identity(
        {"TERMINAL_ENV": "docker", "TERMINAL_DOCKER_IMAGE": "img-a"})
    streaming._current_backend_generation = 0
    try:
        streaming._begin_turn_generation(profile_env)
        # The fingerprint was computed from the SAME normalized object.
        bound = streaming._get_turn_backend_config()
        assert bound is not None
        assert streaming._compute_backend_identity(bound) == streaming._last_backend_identity
        # The construction authority returns the profile values, not
        # hard-coded defaults.
        config = fake_terminal_tool._get_env_config()          # wrapped
        assert config["docker_shm_size"] == "2g", config
        assert config["docker_network"] is False, config
        assert config["docker_extra_args"] == ["--cpus=2"], config
        assert config["docker_run_as_host_user"] is True, config
        assert config["docker_persist_across_processes"] is False, config
        assert config["docker_orphan_reaper"] is False, config
        assert config["vercel_runtime"] == "nodejs20", config
        # The REAL creation funnel consumed the snapshot values.
        out = fake_terminal_tool.terminal_tool("echo hi", session_id="s-p")
        created = fake_terminal_tool.created_configs[-1]
        cc = created["container_config"]
        assert cc["docker_shm_size"] == "2g", created
        assert cc["docker_network"] is False, created
        assert cc["docker_extra_args"] == ["--cpus=2"], created
        assert cc["docker_run_as_host_user"] is True, created
        assert cc["docker_persist_across_processes"] is False, created
        assert cc["docker_orphan_reaper"] is False, created
        assert cc["vercel_runtime"] == "nodejs20", created
        assert out == '{"output": "ok", "exit_code": 0}'
    finally:
        streaming._end_turn_generation()
    assert streaming._get_turn_backend_config() is None
