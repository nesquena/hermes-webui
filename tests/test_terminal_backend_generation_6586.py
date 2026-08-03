"""Regression tests for #5937/#6586 — terminal backend isolation generation protocol.

The re-gate review (nesquena-hermes, PR #6586) found two blockers in the
first re-push:

1. **Deterministic deadlock.**  ``api.streaming._wrapped_terminal_tool()``
   entered ``tt._env_lock`` and then called ``tt.get_active_env()``.  The
   installed agent defines ``_env_lock = threading.Lock()`` (non-reentrant)
   and ``get_active_env()`` acquires that same lock, so the first guarded
   terminal call blocked forever.

2. **Ownership was never published.**  ``_mark_env_backend_generation()``
   had no caller; ``_invalidate_stale_terminal_backend()`` popped the
   generation record *before* reading it, so ``recorded`` was always ``None``
   and the stale check returned early — neither ``cleanup_vm()`` nor
   ``clear_file_ops_cache()`` was ever reached.  ``_refresh_env_generation_tag()``
   also could not tag an environment because it depended on that absent record.

The 2026-08-02 re-gate review found further blockers, all fixed here:

3. **Deferred retire never published.**  ``_invalidate_stale_terminal_backend()``
   assigned ``_pending_eviction_generation`` without a ``global`` declaration,
   so the deferral was a local write and the pending retire was lost.  Pending
   stale generations are now a SET (``_pending_eviction_generations``),
   drained by ``_drain_pending_evictions()``.

4. **File and execute_code acquisition bypassed the guard.**  Only
   ``terminal_tool.terminal_tool`` was wrapped; ``file_tools._get_file_ops()``
   and ``code_execution_tool._get_or_create_env()`` reused/created
   ``_active_environments`` directly.  All three paths now share ONE
   acquisition boundary (``_validate_backend_ownership``), and envs are tagged
   at creation via the shared ``_create_environment`` funnel.

5. **Active-use deferral admitted cross-backend reuse.**  Acquisition compared
   the live env's tag against the process-global *recorded* owner, which the
   deferred path never updated.  Acquisition now compares against the CALLING
   TURN's captured (thread-local) generation — carried immutably from turn
   setup — and the identity transition + ownership registration are atomic.
   A new-backend turn neither reuses nor tears down an env an active
   old-backend turn still owns; it waits (bounded) for that turn to drain.

6. **Read/retire gap.**  ``get_active_env()`` then ``cleanup_vm()`` allowed a
   replacement installed under the same key to be removed.  Retire is now a
   compare-and-remove under the registry lock that cleans up the exact removed
   object OUTSIDE the lock.

7. **Registration was not inside a guaranteed outer try/finally.**  The
   production call site now registers via ``_begin_turn_generation()`` as the
   first statements of the try whose finally calls ``_end_turn_generation()``,
   so a raising agent body can never leak an active-use registration.

These tests drive the REAL ``api.streaming`` functions against a faithful
fake of the agent's ``tools.terminal_tool`` contract (non-reentrant lock,
lock-taking accessors, registry dicts), so they are RED on the buggy head
and GREEN after the fix.
"""
from __future__ import annotations

import importlib
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

    def __init__(self, env):
        self.env = env
        self.cwd = getattr(env, "cwd", None)
        self._webui_backend_generation = None


def _build_fake_terminal_tool_module() -> types.ModuleType:
    """Build a fake ``tools.terminal_tool`` faithful to the installed agent:

    - ``_env_lock`` is a plain non-reentrant ``threading.Lock``
    - ``get_active_env()`` acquires ``_env_lock`` internally (the exact
      accessor shape that deadlocked the buggy guard)
    - registries ``_active_environments`` / ``_last_activity``
    """
    mod = types.ModuleType("tools.terminal_tool")
    mod._env_lock = threading.Lock()
    mod._active_environments = {}
    mod._last_activity = {}
    mod.terminal_tool_calls = []

    def _resolve_container_task_id(task_id):
        return task_id or "default"

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

    def terminal_tool(
        command, background=False, timeout=None, task_id=None,
        session_id=None, force=False, workdir=None, pty=False,
        notify_on_complete=False, watch_patterns=None,
    ):
        mod.terminal_tool_calls.append(command)
        return '{"output": "ok", "exit_code": 0}'

    mod._resolve_container_task_id = _resolve_container_task_id
    mod.get_active_env = get_active_env
    mod.cleanup_vm = cleanup_vm
    mod.terminal_tool = terminal_tool
    return mod


def _tagged_generation(streaming) -> int:
    """Generation a fake creation path tags a fresh env with: the calling
    turn's captured generation when present, else the current global — the
    same policy the production ``_create_environment`` wrapper applies."""
    turn_gen = getattr(streaming._turn_state, "backend_generation", None)
    return turn_gen if turn_gen is not None else streaming._get_current_backend_generation()


def _install_creating_terminal_tool(fake_terminal_tool, streaming):
    """Replace the stub terminal_tool with one that creates an env when the
    registry is empty (mirroring the agent's create-if-missing acquisition),
    tagging it like the production ``_create_environment`` wrapper.  Must run
    BEFORE ``_install_terminal_env_generation_guard()`` so the guard wraps it.
    """
    def terminal_tool(
        command, background=False, timeout=None, task_id=None,
        session_id=None, force=False, workdir=None, pty=False,
        notify_on_complete=False, watch_patterns=None,
    ):
        fake_terminal_tool.terminal_tool_calls.append(command)
        with fake_terminal_tool._env_lock:
            env = fake_terminal_tool._active_environments.get("default")
            if env is None:
                env = _FakeEnv(generation=_tagged_generation(streaming))
                fake_terminal_tool._active_environments["default"] = env
                fake_terminal_tool._last_activity["default"] = time.time()
        return '{"output": "ok", "exit_code": 0}'

    fake_terminal_tool.terminal_tool = terminal_tool


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
    streaming._turn_state.backend_generation = None
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
        # env exists (creating + tagging it), then build/cache a file_ops
        # wrapper around it.
        streaming = importlib.import_module("api.streaming")
        eff = fake._resolve_container_task_id(task_id)
        with fake._env_lock:
            env = fake._active_environments.get(eff)
            if env is None:
                env = _FakeEnv(generation=_tagged_generation(streaming))
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
        streaming = importlib.import_module("api.streaming")
        eff = fake._resolve_container_task_id(task_id)
        with fake._env_lock:
            env = fake._active_environments.get(eff)
            if env is None:
                env = _FakeEnv(generation=_tagged_generation(streaming))
                fake._active_environments[eff] = env
                fake._last_activity[eff] = time.time()
            return env, "ssh"

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
    fake_terminal_tool._active_environments["default"] = _FakeEnv(generation=0)
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

    # The stale env was retired from the registry before the original tool ran.
    assert "default" not in fake_terminal_tool._active_environments
    # Ownership for the replacement was published.
    assert streaming._get_expected_backend_generation("default") == 1
    assert result.get("ok") == '{"output": "ok", "exit_code": 0}'


def test_guard_compares_against_turn_captured_generation(fake_terminal_tool):
    """Blocker 3 (focused): acquisition must compare against the CALLING
    TURN's captured generation — not the recorded owner (which a deferred
    transition could leave stale) and not the process-global current
    generation.

    Here the recorded owner is still 0 (the worst case the old code produced)
    while the env is tagged 0 and the calling turn is registered under gen 1:
    the guard must still refuse the env and retire it.
    """
    streaming = importlib.import_module("api.streaming")
    streaming._install_terminal_env_generation_guard()

    streaming._env_backend_generations["default"] = 0
    streaming._current_backend_generation = 1
    env_a = _FakeEnv(generation=0)
    fake_terminal_tool._active_environments["default"] = env_a

    result = {}

    def _turn_b():
        streaming._register_turn_generation()          # gen 1
        try:
            result["out"] = fake_terminal_tool.terminal_tool(
                "whoami", task_id=None, session_id="s3"
            )
        except Exception as exc:  # pragma: no cover - failure path
            result["error"] = repr(exc)
        finally:
            streaming._unregister_turn_generation()

    t = threading.Thread(target=_turn_b)
    t.start()
    t.join(timeout=10.0)
    assert not t.is_alive()
    # The gen-0 env was NOT reused by the gen-1 turn and was retired.
    assert "default" not in fake_terminal_tool._active_environments
    assert env_a.cleaned
    assert result.get("out") == '{"output": "ok", "exit_code": 0}'


def test_invalidate_publishes_ownership_after_identity_change(fake_terminal_tool):
    """Blocker 2: after a backend identity change the invalidator must (a)
    retire the stale env, (b) PUBLISH the new generation as owner so the next
    refresh can tag and future invalidations can compare."""
    streaming = importlib.import_module("api.streaming")

    # Turn A: env created under gen 0, ownership published by refresh.
    streaming._env_backend_generations["default"] = 0
    fake_terminal_tool._active_environments["default"] = _FakeEnv(generation=0)

    # Turn B: profile switches to a different SSH host — same TERMINAL_ENV,
    # different backend identity (host B).  Full fingerprint must differ.
    env_a = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"}
    env_b = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-b"}
    assert streaming._compute_backend_identity(env_a) != streaming._compute_backend_identity(env_b)

    streaming._invalidate_stale_terminal_backend(env_b)

    # Stale env (gen 0) retired; new generation (1) published as owner.
    assert "default" not in fake_terminal_tool._active_environments
    assert streaming._env_backend_generations.get("default") == 1
    assert streaming._current_backend_generation == 1


def test_refresh_tags_env_and_publishes_ownership(fake_terminal_tool):
    """Blocker 2: _refresh_env_generation_tag must tag the live env AND record
    the owner generation — the publish side of the protocol."""
    streaming = importlib.import_module("api.streaming")

    env = _FakeEnv(generation=None)
    fake_terminal_tool._active_environments["default"] = env

    streaming._refresh_env_generation_tag()

    assert env._webui_backend_generation == 0
    assert streaming._env_backend_generations.get("default") == 0


def test_invalidate_skips_retire_when_env_replaced_by_newer_generation(fake_terminal_tool):
    """A concurrent turn already replaced the env with a newer generation:
    the invalidator must NOT retire that newer env."""
    streaming = importlib.import_module("api.streaming")

    # Recorded owner is gen 0 (stale), but the registry already holds a
    # gen-1 env from a concurrent turn — the invalidator must leave it alone.
    streaming._env_backend_generations["default"] = 0
    fake_terminal_tool._active_environments["default"] = _FakeEnv(generation=1)

    streaming._invalidate_stale_terminal_backend(
        {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-b"}
    )

    assert "default" in fake_terminal_tool._active_environments
    assert streaming._current_backend_generation == 1


def test_active_use_defers_retire_until_turn_exits(fake_terminal_tool):
    """Active-use ownership: a stale env still executing an older turn must
    not be torn down; the retire is deferred until the turn exits."""
    streaming = importlib.import_module("api.streaming")

    streaming._env_backend_generations["default"] = 0
    fake_terminal_tool._active_environments["default"] = _FakeEnv(generation=0)

    # An older turn is still executing under gen 0.
    streaming._active_turn_generations[0] = 1

    streaming._invalidate_stale_terminal_backend(
        {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-b"}
    )

    # Not retired yet — deferred (every pending generation, not a scalar).
    assert "default" in fake_terminal_tool._active_environments
    assert streaming._pending_eviction_generations == {0}

    # The older turn exits → the deferred retire is drained.
    streaming._active_turn_generations.pop(0, None)
    streaming._drain_pending_evictions()

    assert "default" not in fake_terminal_tool._active_environments
    assert streaming._pending_eviction_generations == set()


def test_register_unregister_turn_generation_roundtrip(fake_terminal_tool):
    """The turn lifecycle hooks bump/decrement the active-use counter and
    drain deferred evictions on the last exit.

    Uses a faithful TWO-THREAD schedule: ``_turn_state`` is thread-local, so a
    single thread can only hold one registration at a time — nesting two
    registers on one thread is not a valid schedule and left {0: 1} behind.
    """
    streaming = importlib.import_module("api.streaming")

    a_registered = threading.Event()
    b_go = threading.Event()
    b_registered = threading.Event()
    a_go = threading.Event()
    b_may_finish = threading.Event()
    a_done = threading.Event()
    b_done = threading.Event()

    def _turn_a():
        streaming._register_turn_generation()          # gen 0
        a_registered.set()
        assert a_go.wait(timeout=10)
        streaming._unregister_turn_generation()
        a_done.set()

    def _turn_b():
        assert b_go.wait(timeout=10)
        streaming._register_turn_generation()          # gen 0
        b_registered.set()
        assert b_may_finish.wait(timeout=10)
        streaming._unregister_turn_generation()
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

    - Turn A registers under generation 0 (profile host-a) with a live gen-0
      env in the registry.
    - Turn B starts with profile host-b: identity change bumps to gen 1 and B
      registers gen 1.
    - B's acquisition (``acquire(streaming, fake)``) must never reuse env_a
      and never tear it down while A is still active.
    - completion_order=1: B's acquisition blocks (drain wait) until A exits.
    - completion_order=2: A exits first (drain retires env_a) and only then
      does B's acquisition run — it must not block at all.
    """
    streaming._last_backend_identity = streaming._compute_backend_identity(
        {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-a"})
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
            streaming._register_turn_generation()          # gen 0
            a_registered.set()
            try:
                assert a_may_finish.wait(timeout=15), "turn A never released"
            finally:
                streaming._unregister_turn_generation()
                a_finished.set()

        def _turn_b():
            streaming._invalidate_stale_terminal_backend(
                {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-b"})
            streaming._register_turn_generation()          # gen 1
            b_setup_done.set()
            try:
                assert b_may_acquire.wait(timeout=15), "turn B never allowed to acquire"
                b_result["out"] = acquire(streaming, fake)
            finally:
                streaming._unregister_turn_generation()

        ta = threading.Thread(target=_turn_a)
        ta.start()
        # A must be registered BEFORE B's setup runs, so B's invalidator sees
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
    if tool == "terminal":
        # Creating terminal_tool must be in place before the guard wraps it.
        _install_creating_terminal_tool(fake_terminal_tool, streaming)
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


def test_setup_exception_still_registers_and_releases_turn(fake_terminal_tool, monkeypatch):
    """Setup exceptions must not leak a turn registration: the identity
    transition is non-fatal (registration still happens) and the production
    try/finally (begin → body → end) always releases it."""
    streaming = importlib.import_module("api.streaming")

    def _boom(profile_runtime_env):
        raise RuntimeError("identity transition failed")

    monkeypatch.setattr(streaming, "_invalidate_stale_terminal_backend", _boom)

    # begin must not raise and must still register under the current gen.
    streaming._begin_turn_generation(
        {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-b"})
    assert streaming._active_turn_generations.get(0) == 1

    try:
        raise RuntimeError("agent body exploded")
    except RuntimeError:
        pass
    finally:
        streaming._end_turn_generation()

    assert 0 not in streaming._active_turn_generations
    assert getattr(streaming._turn_state, "backend_generation", None) is None


def test_replacement_between_read_and_retire_not_torn_down(fake_terminal_tool):
    """A replacement installed under the same key between the stale env's
    removal and its teardown must survive: retire cleans up the EXACT removed
    object outside the registry lock, never a by-key pop of whatever is there
    now."""
    streaming = importlib.import_module("api.streaming")

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

    streaming._invalidate_stale_terminal_backend(
        {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "host-b"}
    )

    repl = fake_terminal_tool._active_environments.get("default")
    assert repl is installed.get("repl"), "the replacement must survive the retire"
    assert repl._webui_backend_generation == 1
    assert not repl.cleaned, "the replacement must never be torn down"
    assert e0.cleaned, "the exact stale object must be retired"
