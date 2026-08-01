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

These tests drive the REAL ``api.streaming`` functions against a faithful
fake of the agent's ``tools.terminal_tool`` contract (non-reentrant lock,
lock-taking accessors, registry dicts), so they are RED on the buggy head
and GREEN after the fix.
"""
from __future__ import annotations

import importlib
import sys
import threading
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
        streaming._pending_eviction_generation = None
    yield


@pytest.fixture()
def fake_terminal_tool(monkeypatch):
    """Install the faithful fake agent terminal module + file_tools."""
    fake = _build_fake_terminal_tool_module()
    fake_file_tools = types.ModuleType("tools.file_tools")
    fake_file_tools.clear_file_ops_cache_calls = []

    def clear_file_ops_cache(task_id):
        fake_file_tools.clear_file_ops_cache_calls.append(task_id)

    fake_file_tools.clear_file_ops_cache = clear_file_ops_cache

    monkeypatch.setitem(sys.modules, "tools.terminal_tool", fake)
    monkeypatch.setitem(sys.modules, "tools.file_tools", fake_file_tools)
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

    # Not retired yet — deferred.
    assert "default" in fake_terminal_tool._active_environments
    assert streaming._pending_eviction_generation == 0

    # The older turn exits → the deferred retire is drained.
    streaming._active_turn_generations.pop(0, None)
    streaming._drain_pending_eviction()

    assert "default" not in fake_terminal_tool._active_environments
    assert streaming._pending_eviction_generation is None


def test_register_unregister_turn_generation_roundtrip(fake_terminal_tool):
    """The turn lifecycle hooks bump/decrement the active-use counter and
    drain deferred evictions on the last exit."""
    streaming = importlib.import_module("api.streaming")

    streaming._register_turn_generation()
    assert streaming._active_turn_generations.get(0) == 1

    # Second concurrent turn on the same generation.
    streaming._register_turn_generation()
    assert streaming._active_turn_generations.get(0) == 2

    streaming._unregister_turn_generation()
    assert streaming._active_turn_generations.get(0) == 1

    streaming._unregister_turn_generation()
    assert 0 not in streaming._active_turn_generations


def test_guard_installs_only_once(fake_terminal_tool):
    """Installing the guard is idempotent — no wrapper-on-wrapper stacking."""
    streaming = importlib.import_module("api.streaming")

    streaming._install_terminal_env_generation_guard()
    first_wrapper = fake_terminal_tool.terminal_tool
    streaming._install_terminal_env_generation_guard()
    assert fake_terminal_tool.terminal_tool is first_wrapper
